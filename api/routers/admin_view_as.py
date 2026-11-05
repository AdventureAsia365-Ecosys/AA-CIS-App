"""AA-1035 R2 slice 2 (root ADR 0019 d5) — start, read and end a read-only "view as tenant" session.

* ``POST /admin/tenants/{tenant_id}/view-as``            — open a session (reason 3-500 characters, optional ticket,
  5-60 minutes, default 30). Capability ``tenant.view_as`` (admin, customer_success, support) and
  ``require_tenant_scope`` (a customer_success member only for its book, support also the pool). Needs a staff identity
  (the verified Cognito one, or ``x-admin-user-id`` under the main secret). Ends the caller's other open sessions
  first (one view at a time). The caller's directory role must be its sign-in role (the verified group role the
  start is authorised with): else 409 (``view_as.ROLE_MISMATCH_MESSAGE``) before any row (the gate would end such a
  session at once; scope is evaluated with the start role, so a drifted row never widens a view). Answers the
  session, the reads the caller's role may make in it (``allowed_reads``), and a short-lived read-only tenant token
  (api/core/view_as.py; claim ``srl`` = that start role); the staff BFF keeps the token in an httpOnly cookie and
  never hands it to the browser. Only the BFF may start one: the call must carry ``x-view-as-client: bff`` (403
  ``view_as_bff_only`` before any row otherwise); the generic /api/admin proxy never sets it and refuses the path.
* ``GET  /admin/view-as/sessions``                       — the caller's own sessions (admin: every session; the
  Dev-only E2E admin: its own only), newest first; ``?tenant_id=``, ``?open_only=true``.
* ``GET  /admin/view-as/sessions/{session_id}``          — one session (the banner reads it): own, or any for admin.
* ``POST /admin/view-as/sessions/{session_id}/end``      — end it: ``ended`` (own) or ``revoked`` (an admin ending
  another member's); an already-expired one is closed as ``expired``. Idempotent.

Every start and every end writes an ``acp_shared.audit_log`` row (``tenant.view_as_started`` / one
``tenant.view_as_ended`` per closed session, under that session's id, with its end reason and cause: the end route, a
new start closing the caller's other sessions, or the gate closing an expired session or one whose right was lost; no
reason text there, it is free text that may name a person and stays in the session row). Every request made with the
token is logged by the gate (api/middleware/view_as.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.core import e2e_staff, view_as
from api.core.staff_authz import TENANT_READ, StaffPrincipal, principal_from_role, request_staff_identity, staff_policy
from api.core.tenant_scope import TENANT_SCOPED, staff_id_of
from api.routers.admin import verify_admin_secret
from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log

logger = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin-view-as"], dependencies=[Depends(staff_policy)])

NO_STAFF_ID = "View as tenant needs a verified staff identity"
UNAVAILABLE = "View as tenant needs migration 364 (shared.staff_view_as_session)."
NOT_FOUND = "View-as session not found"

TENANT_SQL = (
    "SELECT tenant_id, name, slug, plan_tier::text AS plan_tier, cancelled_at, account_status, kind, is_active "
    "FROM shared.tenants WHERE tenant_id = $1"
)
STAFF_STATE_SQL = "SELECT is_active, role::text AS role FROM shared.admin_users WHERE id = $1::uuid"
# The caller's other open sessions end when a new one starts (an expired one as 'expired'); each gets its own
# tenant.view_as_ended row (cause 'replaced').
END_OTHERS_SQL = (
    "UPDATE shared.staff_view_as_session SET ended_at = greatest(now(), started_at), "
    "end_reason = CASE WHEN expires_at <= now() THEN 'expired' ELSE 'ended' END "
    "WHERE admin_user_id = $1::uuid AND ended_at IS NULL "
    "RETURNING session_id::text AS session_id, tenant_id::text AS tenant_id, end_reason"
)
INSERT_SQL = (
    "INSERT INTO shared.staff_view_as_session (admin_user_id, tenant_id, reason, ticket_ref, started_at, expires_at) "
    "VALUES ($1::uuid, $2::uuid, $3, $4, now(), now() + make_interval(mins => $5::int)) "
    "RETURNING session_id::text AS session_id, started_at, expires_at"
)
_SESSION_COLUMNS = (
    "s.session_id::text AS session_id, s.admin_user_id::text AS admin_user_id, s.tenant_id::text AS tenant_id, "
    "t.name AS tenant_name, t.slug AS tenant_slug, u.display_name AS staff_name, s.reason, s.ticket_ref, "
    "s.started_at, s.expires_at, s.ended_at, s.end_reason, (s.ended_at IS NULL AND s.expires_at > now()) AS open"
)
_SESSION_FROM = (
    "FROM shared.staff_view_as_session s JOIN shared.tenants t ON t.tenant_id = s.tenant_id "
    "JOIN shared.admin_users u ON u.id = s.admin_user_id"
)
ONE_SQL = f"SELECT {_SESSION_COLUMNS} {_SESSION_FROM} WHERE s.session_id = $1::uuid"
# $1 staff filter (NULL = every session: admin), $2 tenant filter, $3 open only, $4 limit
LIST_SQL = (
    f"SELECT {_SESSION_COLUMNS} {_SESSION_FROM} "
    "WHERE ($1::uuid IS NULL OR s.admin_user_id = $1::uuid) AND ($2::uuid IS NULL OR s.tenant_id = $2::uuid) "
    "AND (NOT $3::boolean OR (s.ended_at IS NULL AND s.expires_at > now())) "
    "ORDER BY s.started_at DESC LIMIT $4"
)
END_SQL = (
    "UPDATE shared.staff_view_as_session SET ended_at = greatest(now(), started_at), "
    "end_reason = CASE WHEN expires_at <= now() THEN 'expired' ELSE $2 END "
    "WHERE session_id = $1::uuid AND ended_at IS NULL RETURNING end_reason, ended_at"
)


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=3, max_length=500)
    ticket_ref: Optional[str] = Field(None, max_length=120)
    minutes: int = Field(view_as.DEFAULT_MINUTES, ge=view_as.MIN_MINUTES, le=view_as.MAX_MINUTES)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = v.strip()
        if not 3 <= len(v) <= 500:
            raise ValueError("Give a reason of 3 to 500 characters")
        return v

    @field_validator("ticket_ref")
    @classmethod
    def _ticket(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None


def _caller(principal: Any, request: Request) -> StaffPrincipal:
    if isinstance(principal, StaffPrincipal):
        return principal
    return principal_from_role(request.headers.get("x-admin-role"), request_staff_identity(request))


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else None


def session_view(r: Any) -> dict:
    return {
        "session_id": r["session_id"], "admin_user_id": r["admin_user_id"], "staff_name": r["staff_name"],
        "tenant_id": r["tenant_id"], "tenant_name": r["tenant_name"], "tenant_slug": r["tenant_slug"],
        "reason": r["reason"], "ticket_ref": r["ticket_ref"], "started_at": _iso(r["started_at"]),
        "expires_at": _iso(r["expires_at"]), "ended_at": _iso(r["ended_at"]), "end_reason": r["end_reason"],
        "open": bool(r["open"]), "read_only": True,
    }


def _unavailable(exc: Exception) -> HTTPException:
    logger.error("view_as_unavailable", reason=type(exc).__name__)
    return HTTPException(status_code=503, detail=UNAVAILABLE)


def _require_staff_id(request: Request) -> str:
    staff_id = staff_id_of(request)
    if staff_id is None:
        raise HTTPException(status_code=403, detail=NO_STAFF_ID)
    return staff_id


async def _refuse_e2e(request: Request, conn: Any, tenant_id: Any) -> None:
    """The Dev-only E2E CIS admin (A10b, read-only) may start / end a view-as session on a synthetic tenant only
    (``is_canary IS TRUE``); 403 ``e2e_read_only`` otherwise, before any row. Every other staff member is unaffected.
    AA-1035 A10d: any other Dev-only E2E identity (``e2e_read_only`` without ``e2e_allowlisted_writes``) is refused
    outright (the bearer middleware and staff_authz already refuse it; this is the route's own fail-closed check)."""
    identity = request_staff_identity(request)
    if not getattr(identity, "e2e_read_only", False):
        return
    if not e2e_staff.has_allowlisted_writes(identity):
        logger.warning("view_as_e2e_refused", tenant_id=str(tenant_id), reason="not_allowlisted")
        raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)
    if not await e2e_staff.is_synthetic_tenant(conn, tenant_id):
        logger.warning("view_as_e2e_refused", tenant_id=str(tenant_id), reason="tenant_not_synthetic")
        raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)


def _sees_every_session(principal: StaffPrincipal, request: Request) -> bool:
    """admin (tenant.read: every tenant) reads and ends every member's session; the others only their own. The Dev-only
    E2E CIS admin (A10b-2, read-only) only its own: its evidence run never lists, reads or ends a real member's
    session."""
    if getattr(request_staff_identity(request), "e2e_read_only", False):
        return False
    return TENANT_READ in principal.perms


@router.post("/tenants/{tenant_id}/view-as", status_code=201, dependencies=TENANT_SCOPED,
             summary="Start a read-only view-as session on this tenant (at most 60 minutes, logged)")
async def start_view_as(
    tenant_id: UUID, body: StartBody, request: Request,
    principal: StaffPrincipal = Depends(staff_policy),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    if request.headers.get(view_as.BFF_HEADER) != view_as.BFF_HEADER_VALUE:
        # Defence in depth: the token is answered only to the staff BFF that keeps it in an httpOnly cookie, never to
        # a page through a generic proxy (which would hand a JS-readable bearer to the browser).
        logger.warning("view_as_start_refused", reason="not_bff", tenant_id=str(tenant_id))
        raise HTTPException(status_code=403, detail=view_as.BFF_ONLY_DETAIL)
    staff_id = _require_staff_id(request)
    # The role this start is authorised with (staff_policy: the x-admin-role the bearer middleware rewrote from the
    # verified Cognito groups). It is signed into the token (srl): the gate evaluates scope and reads with it.
    role = _caller(principal, request).role
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        tenant = await conn.fetchrow(TENANT_SQL, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        # The same rule the gate re-checks on every request (an offboarded or suspended tenant ends an open session).
        refusal = view_as.tenant_refusal(
            tenant_id, cancelled_at=tenant["cancelled_at"], account_status=tenant["account_status"],
            kind=tenant["kind"], is_active=tenant["is_active"])
        if refusal is not None:
            raise HTTPException(status_code=409, detail=view_as.TENANT_REFUSAL_MESSAGES[refusal])
        await _refuse_e2e(request, conn, tenant_id)
        staff = await conn.fetchrow(STAFF_STATE_SQL, staff_id)
        if staff is None or not staff["is_active"]:
            raise HTTPException(status_code=403, detail=NO_STAFF_ID)
        if view_as.start_role_mismatch(staff["role"], role):
            logger.warning("view_as_start_refused", reason=view_as.ROLE_MISMATCH_DETAIL, tenant_id=str(tenant_id),
                           staff_id=staff_id, role=role, db_role=staff["role"])
            raise HTTPException(status_code=409, detail=view_as.ROLE_MISMATCH_MESSAGE)
        try:
            closed = await conn.fetch(END_OTHERS_SQL, staff_id)
            row = await conn.fetchrow(INSERT_SQL, staff_id, tenant_id, body.reason, body.ticket_ref, body.minutes)
        except asyncpg.UndefinedTableError as exc:
            raise _unavailable(exc) from exc
        ended = [r["session_id"] for r in closed]
        actor = f"admin:{staff_id}"
        # audit rows last (the chain trigger's lock): one tenant.view_as_ended per session this start closed, under
        # that session's own id and tenant, then the start
        for r in closed:
            await write_audit_log(conn, **view_as.ended_audit(
                tenant_id=r["tenant_id"], session_id=r["session_id"], actor=actor, end_reason=r["end_reason"],
                cause="replaced", session_staff=staff_id, replaced_by=row["session_id"]))
        await write_audit_log(
            conn, tenant_id=str(tenant_id), actor=actor, action=AdminTenantAuditAction.VIEW_AS_STARTED,
            resource_type=view_as.AUDIT_RESOURCE, resource_id=row["session_id"],
            details={"session_id": row["session_id"], "minutes": body.minutes, "ticket_ref": body.ticket_ref,
                     "expires_at": _iso(row["expires_at"]), "ended_sessions": ended})
    token = view_as.mint_token(
        session_id=row["session_id"], tenant_id=str(tenant_id), staff_id=staff_id, start_role=role,
        name=tenant["name"], plan_tier=tenant["plan_tier"], expires_at=row["expires_at"])
    logger.info("view_as_started", session_id=row["session_id"], tenant_id=str(tenant_id), staff_id=staff_id,
                minutes=body.minutes, role=role, ended_sessions=len(ended))
    return {
        "session_id": row["session_id"], "tenant_id": str(tenant_id), "tenant_name": tenant["name"],
        "tenant_slug": tenant["slug"], "started_at": _iso(row["started_at"]), "expires_at": _iso(row["expires_at"]),
        "read_only": True, "allowed_reads": view_as.reads_for_role(role), "token": token,
    }


@router.get("/view-as/sessions", summary="View-as sessions: the caller's own (admin: every member's)")
async def list_view_as_sessions(
    request: Request,
    tenant_id: Optional[UUID] = None,
    open_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    principal: StaffPrincipal = Depends(staff_policy),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    caller = _caller(principal, request)
    staff_filter = None if _sees_every_session(caller, request) else _require_staff_id(request)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(LIST_SQL, staff_filter, tenant_id, open_only, limit)
        except asyncpg.UndefinedTableError as exc:
            raise _unavailable(exc) from exc
    return {"sessions": [session_view(r) for r in rows], "scope": "all" if staff_filter is None else "own"}


async def _visible_session(conn: Any, request: Request, caller: StaffPrincipal, session_id: UUID) -> Any:
    try:
        row = await conn.fetchrow(ONE_SQL, session_id)
    except asyncpg.UndefinedTableError as exc:
        raise _unavailable(exc) from exc
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    if not _sees_every_session(caller, request) and row["admin_user_id"] != _require_staff_id(request):
        raise HTTPException(status_code=404, detail=NOT_FOUND)   # another member's session: same answer as unknown
    return row


@router.get("/view-as/sessions/{session_id}", summary="One view-as session (own; admin: any)")
async def get_view_as_session(
    session_id: UUID, request: Request,
    principal: StaffPrincipal = Depends(staff_policy),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    caller = _caller(principal, request)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await _visible_session(conn, request, caller, session_id)
    return session_view(row)


@router.post("/view-as/sessions/{session_id}/end", summary="End a view-as session (own: ended; admin: revoked)")
async def end_view_as_session(
    session_id: UUID, request: Request,
    principal: StaffPrincipal = Depends(staff_policy),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    caller = _caller(principal, request)
    staff_id = staff_id_of(request)
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        row = await _visible_session(conn, request, caller, session_id)
        await _refuse_e2e(request, conn, UUID(row["tenant_id"]))
        own = staff_id is not None and row["admin_user_id"] == staff_id
        done = await conn.fetchrow(END_SQL, session_id, "ended" if own else "revoked")
        if done is not None:
            await write_audit_log(conn, **view_as.ended_audit(
                tenant_id=row["tenant_id"], session_id=row["session_id"], actor=f"admin:{staff_id or 'unknown'}",
                end_reason=done["end_reason"], cause="ended_by_staff" if own else "revoked_by_admin",
                session_staff=row["admin_user_id"]))
        fresh = await conn.fetchrow(ONE_SQL, session_id)
    logger.info("view_as_ended", session_id=str(session_id), changed=done is not None,
                end_reason=done["end_reason"] if done else row["end_reason"])
    return {**session_view(fresh), "changed": done is not None}
