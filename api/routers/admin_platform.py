"""AA-883 (S241 GĐ2) + AA-816 C6 — Platform › People & access: the staff directory.

  GET   /admin/platform/staff                       every CIS staff account (shared.admin_users) + counts + viewer
  GET   /admin/platform/staff/{staff_id}            one account
  POST  /admin/platform/staff                       create (C6)
  PATCH /admin/platform/staff/{staff_id}/roles      CIS role and/or Booking grant (C6)
  POST  /admin/platform/staff/{staff_id}/disable    (C6)
  POST  /admin/platform/staff/{staff_id}/enable     (C6)
  POST  /admin/platform/staff/{staff_id}/sign-out   "reset": end every session, re-enable if disabled (C6)
  POST  /admin/platform/staff/{staff_id}/sync       retry the Cognito half after a 502 saved_cognito_pending (C6)

Reads (AA-883): admin only, staff_policy staff.read (api/core/staff_authz.py POLICY); the BFF has the same rule
(frontend/lib/rbac.ts API_READ_RULES "platform"), the page is behind PAGE_RULES /admin/platform. password_hash and
the Cognito identity values are never selected; ``linked`` only says whether a Cognito identity is bound.

Writes (C6): staff.write = an owner (admin_users.is_owner) on a verified Cognito session (the staff bearer
middleware); a legacy password session, a forged x-admin-role or the machine gets 403 from staff_policy. The work
is in api/services/staff_directory.py (DB first, then Cognito; guards; audit). A Cognito failure after the DB
change answers 502 ``{"status": "saved_cognito_pending", ...}`` with the saved row; the owner retries with
``/sync`` (idempotent), or re-POSTs the same create.

The router is included without an include prefix (staff_policy reads the route template from
scope["route"].path, see staff_authz._route_template).
"""
from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import STAFF_ROLES, request_staff_identity, staff_policy
from api.routers.admin import verify_admin_secret
from api.services import staff_directory
from api.services.staff_directory import CognitoDirectory, StaffDirectoryError, WriteResult, get_cognito

router = APIRouter(prefix="/admin/platform", tags=["admin-platform"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

# The only fields a staff row returns. Selected explicitly (never `*`): password_hash, cognito_iss and cognito_sub
# stay out (``linked`` is derived in SQL).
STAFF_COLUMNS: tuple[str, ...] = (
    "id", "username", "role", "is_active", "created_at", "updated_at",
    "email", "display_name", "is_owner", "last_login_at", "linked", "booking_role", "breakglass",
)

_SELECT = (
    "SELECT u.id::text AS id, u.username, u.role::text AS role, u.is_active, u.created_at, u.updated_at, "
    "u.email, u.display_name, u.is_owner, u.last_login_at, (u.cognito_sub IS NOT NULL) AS linked, "
    "g.role AS booking_role "
    "FROM shared.admin_users u "
    "LEFT JOIN shared.staff_app_grant g ON g.admin_user_id = u.id AND g.app = 'booking'"
)

CisRole = Literal["admin", "reviewer", "content"]
BookingRole = Literal["admin", "sales", "contracting", "content_creator"]


def _staff_row(row: Any) -> dict:
    d = dict(row)
    out = {k: d.get(k) for k in STAFF_COLUMNS}
    for key in ("created_at", "updated_at", "last_login_at"):
        value = out[key]
        out[key] = value.isoformat() if value is not None else None
    for key in ("is_active", "is_owner", "linked"):
        out[key] = bool(out[key])
    out["breakglass"] = staff_directory.is_breakglass(out["username"], out["email"])
    return out


def _viewer(request: Request) -> dict:
    """Who is looking, for the UI only (the backend re-checks every write): may this session write?"""
    identity = request_staff_identity(request)
    cognito = identity is not None and getattr(identity, "auth", None) == "cognito"
    is_owner = bool(cognito and getattr(identity, "is_owner", False))
    return {
        "auth": "cognito" if cognito else "legacy",
        "is_owner": is_owner,
        "can_write": is_owner,
        "admin_id": getattr(identity, "admin_id", None) if cognito else None,
    }


@router.get("/staff", summary="AA-883 — CIS staff accounts")
async def list_staff(request: Request):
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT + " ORDER BY u.is_active DESC, u.role, lower(u.username)")
    staff = [_staff_row(r) for r in rows]
    by_role = {role: 0 for role in STAFF_ROLES}
    for s in staff:
        if s["is_active"]:
            by_role[s["role"]] = by_role.get(s["role"], 0) + 1
    return {
        "staff": staff,
        "total": len(staff),
        "active": sum(1 for s in staff if s["is_active"]),
        "active_by_role": by_role,
        "viewer": _viewer(request),
    }


def _uuid_or_404(staff_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(staff_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Staff account not found") from None


async def _read_one(pool: Any, sid: uuid.UUID) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SELECT + " WHERE u.id = $1", sid)
    return _staff_row(row) if row is not None else None


@router.get("/staff/{staff_id}", summary="AA-883 — one CIS staff account")
async def get_staff(staff_id: str, request: Request):
    row = await _read_one(request.app.state.pool, _uuid_or_404(staff_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return row


# ── Writes (AA-816 C6) ─────────────────────────────────────────────────────────────────────────────

class CreateStaffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(..., max_length=254)
    display_name: str = Field(..., max_length=200)
    role: CisRole
    booking_role: Optional[BookingRole] = None


class ChangeRolesBody(BaseModel):
    """Only the fields sent are changed; ``"booking_role": null`` removes the Booking grant."""
    model_config = ConfigDict(extra="forbid")
    role: Optional[CisRole] = None
    booking_role: Optional[BookingRole] = None


def _actor_id(request: Request) -> str:
    identity = request_staff_identity(request)
    admin_id = getattr(identity, "admin_id", None)
    if not admin_id:   # staff_policy already refused non-Cognito callers; never write without a verified actor
        raise HTTPException(status_code=403, detail="Only a platform owner can change staff accounts")
    return admin_id


_PENDING_DETAIL = ("Saved in CIS, but Cognito did not finish ({step}: {code}). The account may not sign in "
                   "correctly until you retry.")


async def _respond(request: Request, result: WriteResult, ok_status: int = 200) -> JSONResponse:
    staff = await _read_one(request.app.state.pool, uuid.UUID(result.staff_id))
    body = {
        "status": result.status,
        "operation": result.operation,
        "cognito": result.cognito,
        "steps": result.steps,
        "staff": staff,
    }
    if result.pending:
        body.update({
            "detail": _PENDING_DETAIL.format(step=result.failed_step, code=result.error_code),
            "failed_step": result.failed_step,
            "error_code": result.error_code,
            "retry": f"/admin/platform/staff/{result.staff_id}/sync",
        })
        return JSONResponse(body, status_code=502)
    return JSONResponse(body, status_code=ok_status)


def _refused(exc: StaffDirectoryError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@router.post("/staff", summary="AA-816 C6 — create a staff account (DB, then Cognito)")
async def create_staff(body: CreateStaffBody, request: Request,
                       cognito: CognitoDirectory = Depends(get_cognito)):
    try:
        result = await staff_directory.create_staff(
            request.app.state.pool, cognito, actor_id=_actor_id(request), email=body.email,
            display_name=body.display_name, role=body.role, booking_role=body.booking_role)
    except StaffDirectoryError as exc:
        raise _refused(exc) from None
    return await _respond(request, result, ok_status=201 if result.created else 200)


@router.patch("/staff/{staff_id}/roles", summary="AA-816 C6 — change the CIS role and/or the Booking grant")
async def change_staff_roles(staff_id: str, body: ChangeRolesBody, request: Request,
                             cognito: CognitoDirectory = Depends(get_cognito)):
    sid = _uuid_or_404(staff_id)
    sent = body.model_fields_set
    if "role" in sent and body.role is None:
        raise HTTPException(status_code=400, detail="The CIS role cannot be removed; pick a role")
    kwargs: dict[str, Any] = {}
    if "role" in sent:
        kwargs["role"] = body.role
    if "booking_role" in sent:
        kwargs["booking_role"] = body.booking_role
    try:
        result = await staff_directory.change_roles(
            request.app.state.pool, cognito, actor_id=_actor_id(request), staff_id=str(sid), **kwargs)
    except StaffDirectoryError as exc:
        raise _refused(exc) from None
    return await _respond(request, result)


async def _simple(request: Request, cognito: CognitoDirectory, staff_id: str, op: Any) -> JSONResponse:
    sid = _uuid_or_404(staff_id)
    try:
        result = await op(request.app.state.pool, cognito, actor_id=_actor_id(request), staff_id=str(sid))
    except StaffDirectoryError as exc:
        raise _refused(exc) from None
    return await _respond(request, result)


@router.post("/staff/{staff_id}/disable", summary="AA-816 C6 — disable (DB, AdminDisableUser, global sign-out)")
async def disable_staff(staff_id: str, request: Request, cognito: CognitoDirectory = Depends(get_cognito)):
    return await _simple(request, cognito, staff_id, staff_directory.disable_staff)


@router.post("/staff/{staff_id}/enable", summary="AA-816 C6 — enable (DB, AdminEnableUser)")
async def enable_staff(staff_id: str, request: Request, cognito: CognitoDirectory = Depends(get_cognito)):
    return await _simple(request, cognito, staff_id, staff_directory.enable_staff)


@router.post("/staff/{staff_id}/sign-out", summary="AA-816 C6 — reset: end every session, re-enable if disabled")
async def sign_out_staff(staff_id: str, request: Request, cognito: CognitoDirectory = Depends(get_cognito)):
    return await _simple(request, cognito, staff_id, staff_directory.sign_out_staff)


@router.post("/staff/{staff_id}/sync", summary="AA-816 C6 — retry: make Cognito match the directory row")
async def sync_staff(staff_id: str, request: Request, cognito: CognitoDirectory = Depends(get_cognito)):
    return await _simple(request, cognito, staff_id, staff_directory.sync_staff)
