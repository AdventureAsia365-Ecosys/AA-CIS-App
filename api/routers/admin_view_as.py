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

* ``POST /admin/view-as/roles``                         — AA-1157: an owner starts a read-only view as a lower staff
  role (``{role, reason 3-500, minutes 5-60}``; capability ``staff.view_as_role``: owner + admin + Cognito; downward
  only; BFF only). One ``staff.view_as_started`` row with the target role's full capability list. The session read
  and the end above serve this kind too (``kind`` "role", ``view_role``; the BFF calls them without the view-as
  header, as the owner); the list stays tenant sessions only. api/core/staff_view_as.py has the per-request part.

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

from api.core import e2e_staff, staff_view_as, view_as
from api.core.staff_authz import (
    TENANT_READ, VIEW_AS_ROLE_NEEDS_OWNER, StaffPrincipal, principal_from_role, request_staff_identity, staff_policy,
)
from api.core.tenant_scope import TENANT_SCOPED, staff_id_of
from api.routers.admin import verify_admin_secret
from services.acp_shared.audit_log import AdminTenantAuditAction, StaffViewAsAuditAction, write_audit_log

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
# AA-1157: a start takes a per-member transaction lock first, so two concurrent starts (two tabs) cannot both end the
# other sessions and both insert: the second waits, then ends the first one's row.
START_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('staff_view_as:' || $1::text))"
# The caller's other open sessions end when a new one starts (an expired one as 'expired'); each gets its own
# tenant.view_as_ended row (cause 'replaced').
END_OTHERS_SQL = (
    "UPDATE shared.staff_view_as_session SET ended_at = greatest(now(), started_at), "
    "end_reason = CASE WHEN expires_at <= now() THEN 'expired' ELSE 'ended' END "
    "WHERE admin_user_id = $1::uuid AND ended_at IS NULL "
    "RETURNING session_id::text AS session_id, tenant_id::text AS tenant_id, end_reason, view_role"
)
INSERT_SQL = (
    "INSERT INTO shared.staff_view_as_session (admin_user_id, tenant_id, reason, ticket_ref, started_at, expires_at) "
    "VALUES ($1::uuid, $2::uuid, $3, $4, now(), now() + make_interval(mins => $5::int)) "
    "RETURNING session_id::text AS session_id, started_at, expires_at"
)
_SESSION_COLUMNS = (
    "s.session_id::text AS session_id, s.admin_user_id::text AS admin_user_id, s.tenant_id::text AS tenant_id, "
    "t.name AS tenant_name, t.slug AS tenant_slug, u.display_name AS staff_name, s.reason, s.ticket_ref, "
    "s.started_at, s.expires_at, s.ended_at, s.end_reason, (s.ended_at IS NULL AND s.expires_at > now()) AS open, "
    "s.view_role"
)
# AA-1157 (CIS 470): a row is a tenant session (tenant_id) or a "view as a lower role" session (view_role, no tenant):
# LEFT JOIN so one session read serves both kinds (the BFF's banner reads its own role session by id).
_SESSION_FROM = (
    "FROM shared.staff_view_as_session s LEFT JOIN shared.tenants t ON t.tenant_id = s.tenant_id "
    "JOIN shared.admin_users u ON u.id = s.admin_user_id"
)
ONE_SQL = f"SELECT {_SESSION_COLUMNS} {_SESSION_FROM} WHERE s.session_id = $1::uuid"
# $1 staff filter (NULL = every session: admin), $2 tenant filter, $3 open only, $4 limit
LIST_SQL = (
    f"SELECT {_SESSION_COLUMNS} {_SESSION_FROM} "
    # the tenant view-as list (Tenant 360, the staff BFF's sign-out): tenant sessions only (AA-1157)
    "WHERE s.tenant_id IS NOT NULL "
    "AND ($1::uuid IS NULL OR s.admin_user_id = $1::uuid) AND ($2::uuid IS NULL OR s.tenant_id = $2::uuid) "
    "AND (NOT $3::boolean OR (s.ended_at IS NULL AND s.expires_at > now())) "
    "ORDER BY s.started_at DESC LIMIT $4"
)
# AA-1157 (CIS 470): a "view as a lower role" session: no tenant, the role code viewed.
INSERT_ROLE_SQL = (
    "INSERT INTO shared.staff_view_as_session (admin_user_id, view_role, reason, started_at, expires_at) "
    "VALUES ($1::uuid, $2, $3, now(), now() + make_interval(mins => $4::int)) "
    "RETURNING session_id::text AS session_id, started_at, expires_at"
)
OWNER_STATE_SQL = "SELECT is_active, is_owner FROM shared.admin_users WHERE id = $1::uuid"
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
    view_role = r.get("view_role")
    return {
        "kind": "role" if view_role else "tenant", "view_role": view_role,
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
    if tenant_id is None:
        # AA-1157: a "view as a lower role" session (no tenant) is never an E2E identity's to start or end.
        logger.warning("view_as_e2e_refused", reason="role_session")
        raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)
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
            await conn.execute(START_LOCK_SQL, staff_id)   # one start at a time per member (two tabs)
            closed = await conn.fetch(END_OTHERS_SQL, staff_id)
            row = await conn.fetchrow(INSERT_SQL, staff_id, tenant_id, body.reason, body.ticket_ref, body.minutes)
        except asyncpg.UndefinedTableError as exc:
            raise _unavailable(exc) from exc
        ended = [r["session_id"] for r in closed]
        actor = f"admin:{staff_id}"
        # audit rows last (the chain trigger's lock): one tenant.view_as_ended per session this start closed, under
        # that session's own id and tenant, then the start
        for r in closed:   # AA-1157: either kind (a role session ends as staff.view_as_ended)
            await write_audit_log(conn, **view_as.ended_audit(
                tenant_id=r["tenant_id"], session_id=r["session_id"], actor=actor, end_reason=r["end_reason"],
                cause="replaced", session_staff=staff_id, view_role=r.get("view_role"),
                replaced_by=row["session_id"]))
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
    own = staff_id_of(request)
    async with pool.acquire() as conn:
        if own is not None:
            # AA-1157: the member's own role sessions that ran out without an end (the cookie is gone) end here, with
            # their staff.view_as_ended rows; the BFF's sign-out reads this list first.
            await staff_view_as.close_expired_own(conn, own)
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
        if _expired_own_role_session(row, staff_id_of(request)):
            # AA-1157: the owner's next read of an expired role session (the banner's check, the BFF's view read)
            # closes it as 'expired' with its staff.view_as_ended row, so a view left to time out still gets its end
            # row without waiting for the next start or sign-out. Only the member's own row; nothing else changes.
            await staff_view_as.close(conn, session_id=row["session_id"], staff_id=row["admin_user_id"],
                                      view_role=row["view_role"], cause="expired",
                                      actor=f"admin:{row['admin_user_id']}")
            row = await conn.fetchrow(ONE_SQL, session_id)
    return session_view(row)


def _expired_own_role_session(row: Any, staff_id: Optional[str]) -> bool:
    """Is ``row`` the caller's own role session, past its expiry but not yet ended?"""
    expires = row["expires_at"]
    return bool(row.get("view_role") and staff_id is not None and row["admin_user_id"] == staff_id
                and row["ended_at"] is None and not row["open"] and isinstance(expires, datetime))


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
        await _refuse_e2e(request, conn, UUID(row["tenant_id"]) if row["tenant_id"] else None)
        own = staff_id is not None and row["admin_user_id"] == staff_id
        done = await conn.fetchrow(END_SQL, session_id, "ended" if own else "revoked")
        if done is not None:
            await write_audit_log(conn, **view_as.ended_audit(
                tenant_id=row["tenant_id"], session_id=row["session_id"], actor=f"admin:{staff_id or 'unknown'}",
                end_reason=done["end_reason"], cause="ended_by_staff" if own else "revoked_by_admin",
                session_staff=row["admin_user_id"], view_role=row.get("view_role")))
        fresh = await conn.fetchrow(ONE_SQL, session_id)
    logger.info("view_as_ended", session_id=str(session_id), changed=done is not None,
                end_reason=done["end_reason"] if done else row["end_reason"])
    return {**session_view(fresh), "changed": done is not None}


# ── AA-1157: view as a lower staff role (owners only) ────────────────────────────────────────────────────────────
class RoleStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(..., min_length=2, max_length=63, pattern=r"^[a-z][a-z0-9_]{1,62}$")
    reason: str = Field(..., min_length=3, max_length=500)
    minutes: int = Field(view_as.DEFAULT_MINUTES, ge=view_as.MIN_MINUTES, le=view_as.MAX_MINUTES)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = v.strip()
        if not 3 <= len(v) <= 500:
            raise ValueError("Give a reason of 3 to 500 characters")
        return v


@router.post("/view-as/roles", status_code=201,
             summary="Owner only: start a read-only view as a lower staff role (at most 60 minutes, logged)")
async def start_role_view(
    body: RoleStartBody, request: Request,
    principal: StaffPrincipal = Depends(staff_policy),
    x_admin_secret: str = Header(None),
):
    """staff_policy already required staff.view_as_role (an owner on a verified Cognito session holding the admin
    role). Then: only the staff BFF may start one (``x-view-as-client: bff``, the token never reaches a page); the
    target is a built-in CIS role or an active CIS custom role (404 ``view_as_unknown_role``); it is downward, i.e. its
    read rights are a subset of the caller's (403 ``view_as_not_downward``, naming no capability); the caller's row
    is re-read in the transaction (still active and an owner). The caller's other open sessions (either kind) end, one
    ``shared.staff_view_as_session`` row is written (no tenant, ``view_role``) and one ``staff.view_as_started`` row
    with the target role's full capability list. Answers the signed token to the BFF only."""
    verify_admin_secret(x_admin_secret)
    if request.headers.get(view_as.BFF_HEADER) != view_as.BFF_HEADER_VALUE:
        logger.warning("staff_view_as_start_refused", reason="not_bff")
        raise HTTPException(status_code=403, detail=view_as.BFF_ONLY_DETAIL)
    identity = request_staff_identity(request)
    if not staff_view_as.may_start(identity):
        logger.warning("staff_view_as_start_refused", reason="not_owner")
        raise HTTPException(status_code=403, detail=VIEW_AS_ROLE_NEEDS_OWNER)
    staff_id = identity.admin_id
    viewer_caps = frozenset(identity.capabilities)
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        try:
            target = await staff_view_as.target_role(conn, body.role)
        except staff_view_as.StaffViewAsRefused as exc:   # the custom role cannot be read right now
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        if target is None:
            raise HTTPException(status_code=404, detail=staff_view_as.UNKNOWN_ROLE_DETAIL)
        ok, missing = staff_view_as.downward(viewer_caps, target.capabilities)
        if not ok:
            logger.warning("staff_view_as_start_refused", reason=staff_view_as.NOT_DOWNWARD_DETAIL, role=target.code,
                           missing=missing[:20])
            raise HTTPException(status_code=403, detail=staff_view_as.NOT_DOWNWARD_DETAIL)
        owner = await conn.fetchrow(OWNER_STATE_SQL, staff_id)
        if owner is None or not owner["is_active"] or not owner["is_owner"]:
            logger.warning("staff_view_as_start_refused", reason="owner_row")
            raise HTTPException(status_code=403, detail=VIEW_AS_ROLE_NEEDS_OWNER)
        try:
            await conn.execute(START_LOCK_SQL, staff_id)   # one start at a time per member (two tabs)
            closed = await conn.fetch(END_OTHERS_SQL, staff_id)
            row = await conn.fetchrow(INSERT_ROLE_SQL, staff_id, target.code, body.reason, body.minutes)
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as exc:
            raise _unavailable(exc) from exc
        ended = [r["session_id"] for r in closed]
        actor = f"admin:{staff_id}"
        for r in closed:   # audit rows last (the chain trigger's lock)
            await write_audit_log(conn, **view_as.ended_audit(
                tenant_id=r["tenant_id"], session_id=r["session_id"], actor=actor, end_reason=r["end_reason"],
                cause="replaced", session_staff=staff_id, view_role=r.get("view_role"),
                replaced_by=row["session_id"]))
        await write_audit_log(
            conn, tenant_id=None, actor=actor, action=StaffViewAsAuditAction.STARTED,
            resource_type=staff_view_as.AUDIT_RESOURCE, resource_id=row["session_id"],
            details=staff_view_as.started_details(
                session_id=row["session_id"], viewer_role=identity.role, target=target, minutes=body.minutes,
                expires_at=row["expires_at"], ended_sessions=ended))
    effective = sorted(target.capabilities & viewer_caps)
    token = staff_view_as.mint_token(session_id=row["session_id"], staff_id=staff_id, view_role=target.code,
                                     capabilities=effective, expires_at=row["expires_at"])
    logger.info("staff_view_as_started", session_id=row["session_id"], staff_id=staff_id, view_role=target.code,
                minutes=body.minutes, ended_sessions=len(ended))
    return {
        "session_id": row["session_id"], "kind": "role", "view_role": target.code, "view_label": target.label,
        "started_at": _iso(row["started_at"]), "expires_at": _iso(row["expires_at"]), "read_only": True,
        "capabilities": effective, "token": token,
    }
