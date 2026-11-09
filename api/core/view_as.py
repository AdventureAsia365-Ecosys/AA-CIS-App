"""AA-1035 R2 slice 2 (root ADR 0019 d5) — read-only "view as tenant" for AA staff.

d5: "View as tenant is read-only, lasts at most 60 minutes, requires a reason or ticket, shows a banner and is logged in
shared.staff_view_as_session." Matrix (clarifications.md section 3): admin, customer_success (own book), support
(own book + pool); nobody else.

How it works
------------
1. ``POST /admin/tenants/{tenant_id}/view-as`` (api/routers/admin_view_as.py; capability ``tenant.view_as`` and
   ``require_tenant_scope``) writes one ``shared.staff_view_as_session`` row (reason, optional ticket, at most 60
   minutes) and returns a short-lived tenant JWT minted here (:func:`mint_token`): the usual tenant shape (``role``
   tenant, ``sub`` the tenant, ``client`` portal so it is never metered as the tenant's API calls, ``csr`` viewer) plus
   ``vas`` (the session id), ``stf`` (the staff member), ``srl`` (the staff role the start was authorised with: the
   verified Cognito group role, never ``shared.admin_users.role``) and ``amr`` ``staff_view_as``. It expires with the
   session. The start refuses a staff member whose directory role differs from that role (409,
   :data:`ROLE_MISMATCH_MESSAGE`): a session is only opened when the two agree.
   The start answers the token only to the staff BFF that asked for it (request header :data:`BFF_HEADER`, never
   set by the generic admin proxy, which also refuses this path): the token never reaches a browser.
2. Every request that carries such a token passes :class:`api.middleware.view_as.ViewAsGateMiddleware` first:
   - only GET / HEAD on :data:`READ_ROUTES` (an exact allow-list of side-effect-free tenant reads) is let through; any
     other method or path answers 403 ``view_as_read_only`` — writes are refused on the server whatever the UI shows,
     and so are reads that start work (``GET /v1/slate`` enqueues a paid proposal job), exports and the brand branch;
   - the session must still be open (not ended, not expired), the staff account active, its role still holding
     ``tenant.view_as`` and still the role the session was started with (``srl``; the DB row and, when the staff BFF
     forwards it, the verified group role of the reader, :data:`ROLE_HINT_HEADER`), the tenant still viewable
     (:func:`tenant_refusal`, the same rule as the start: not the AA internal tenant, not offboarded, live) and still
     in the START role's scope (assignment removed = view ends; a directory row that drifted above the group never
     widens it): else 401 ``view_as_ended``, and the session is ended for good (:func:`close_session`, one
     ``tenant.view_as_ended`` row);
   - the read must be one the session's start role may see (:func:`role_may_read`, :data:`ROUTE_CAPS`): a read
     that carries the plan or usage (``/v1/quota``) or the add-on subscriptions and request decisions
     (``/v1/account/add-ons``) needs an account capability of its own on top of ``tenant.view_as``, so support never
     sees it; else 403 ``view_as_not_for_role``. View-as never widens a role's own reads;
   - every request, refused or served, writes one ``acp_shared.audit_log`` row (:func:`audit_row`, action
     ``tenant.view_as_request``: actor the staff member, the session, method, route, status). A request is served only
     after its session check; its row is written once the response status is known.
3. Defence in depth: ``api/middleware/tenant_role.enforce`` (called by the two dependencies that accept a tenant JWT)
   refuses a view-as payload on anything but an allowed read too (:func:`refusal`), so a header shape the middleware
   did not foresee still cannot write.
4. ``POST /admin/view-as/sessions/{session_id}/end`` ends it (``ended``; an admin ending someone else's: ``revoked``);
   an expired session reads as ended and is closed as ``expired`` the next time it is touched; a new start ends the
   caller's other open sessions; offboarding the tenant and disabling or signing out the staff member (the staff
   member's own sign-out included, through the staff BFF) end every open session at once (:func:`close_open_sessions`).
   Every path that ends a session writes one ``tenant.view_as_ended`` audit row under that session's id with its end
   reason (:func:`ended_audit`).
5. A view-as token is not a tenant session: ``POST /auth/verify-tenant`` refuses it (403, :data:`NOT_A_TENANT_SESSION`).

Only staff paths and the tenant read routes are involved; no tenant member ever sees a view-as row (the tenant
activity trail is an allow-list of actions, api/services/tenant_account.TENANT_VISIBLE_ACTIONS).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import asyncpg
import jwt
import structlog

from api.core.tenant_scope import AA_INTERNAL_TENANT

logger = structlog.get_logger()

VIEW_AS_CLAIM = "vas"
STAFF_CLAIM = "stf"
# The staff role the start was authorised with (the verified group role, the x-admin-role the bearer middleware
# rewrote). The gate evaluates scope and ROUTE_CAPS with it, and ends the session when the role changes.
START_ROLE_CLAIM = "srl"
# The staff BFF forwards the verified group role of the member making each read (frontend app/api/view-as/v1/...).
# Not a credential: it can only END a session (a role other than the start role), never widen one.
ROLE_HINT_HEADER = "x-view-as-staff-role"
VIEW_AS_AMR = "staff_view_as"

MAX_MINUTES = 60          # root ADR 0019 d5; CIS 364 staff_view_as_session_window_chk enforces it in the table too
DEFAULT_MINUTES = 30
MIN_MINUTES = 5

READ_ONLY_DETAIL = "view_as_read_only"
NOT_FOR_ROLE_DETAIL = "view_as_not_for_role"
NOT_FOR_ROLE_MESSAGE = "Your staff role cannot see this part of the partner's account in a view-as session."
NOT_FOR_ROLE_REASON = "route_not_for_role"
BFF_ONLY_DETAIL = "view_as_bff_only"
# The staff BFF (frontend app/api/view-as/route.ts) sets this header on the start call; the generic /api/admin proxy
# builds its upstream headers from a fixed list and never sets it. Without it the start answers 403 before any row.
BFF_HEADER = "x-view-as-client"
BFF_HEADER_VALUE = "bff"
ENDED_DETAIL = "view_as_ended"
NOT_A_TENANT_SESSION = "A view-as token is not a tenant session"
ROLE_MISMATCH_DETAIL = "view_as_role_mismatch"
ROLE_MISMATCH_MESSAGE = ("Your staff role in the directory differs from your sign-in group; ask an admin to align them "
                         "before viewing as a partner.")
READ_ONLY_MESSAGE = "View as tenant is read-only: this request is not allowed."
ENDED_MESSAGE = "This view-as session has ended. Start a new one from Tenant 360."

AUDIT_ACTION_REQUEST = "tenant.view_as_request"
AUDIT_RESOURCE = "staff_view_as_session"

READ_METHODS = frozenset({"GET", "HEAD"})

# The tenant reads a view-as session may call (route templates, matched in full). Each is a plain read of the tenant's
# own rows with no side effect: no job, no paid call, no export, no write. Never add a route that starts work (GET
# /v1/slate enqueues t7_slate), streams a file (export-docx, activity/export), or shows channel credentials
# (/v1/integrations/*). tests/unit/test_aa1035_s2_view_as.py walks the app: every other route is refused.
# /v1/billing (plan price, overage, quotas, daily usage) is NOT here: no view-as page reads it, and support may never
# see billing (S265 role table). A read that carries plan or usage data is listed in ROUTE_CAPS too.
READ_ROUTES: tuple[str, ...] = (
    "/v1/quota",
    "/v1/marketplace",
    "/v1/tours/my-versions",
    "/v1/tours/versions/{version_id}",
    "/v1/content-writing/reviews",
    "/v1/content-writing/pieces/{piece_id}",
    "/v1/publish-log",
    "/v1/publish-log/pending",
    "/v1/notifications",
    "/v1/facts",
    "/v1/account/members",
    "/v1/account/activity",
    "/v1/account/add-ons",          # AA-1036 R3: the agency's add-ons (read only; the request POST stays refused)
)


# Reads that need a capability of their own on top of tenant.view_as (any one of the listed): the role x read matrix.
# View-as must never show a role more than its own staff pages do. /v1/quota carries the plan tier and the rewrites
# used: the Tenant 360 account / usage reads (tenant.read: admin; tenant.book.account.read: customer_success), never
# support. /v1/account/add-ons carries each add-on's subscription status and the add-on requests with the staff
# decision notes: the Tenant 360 › Subscription reads (GET /admin/tenants/{id}/subscription and …/addon-requests need
# tenant.read | tenant.book.account.read | billing.read), never support (AA-1036 review). A read not listed here needs
# tenant.view_as only.
ROUTE_CAPS: dict[str, tuple[str, ...]] = {
    "/v1/quota": ("tenant.read", "tenant.book.account.read"),
    "/v1/account/add-ons": ("tenant.read", "tenant.book.account.read"),
}


def role_may_read(role: Optional[str], template: str) -> bool:
    """May a staff member of ``role`` (the session's start role, which the gate keeps equal to the current one) make
    the view-as read ``template``?
    Needs ``tenant.view_as`` and, for a :data:`ROUTE_CAPS` read, one of its capabilities."""
    from api.core.staff_authz import ROLE_PERMS, TENANT_VIEW_AS

    perms = ROLE_PERMS.get(role or "", frozenset())
    if TENANT_VIEW_AS not in perms or template not in READ_ROUTES:
        return False
    needed = ROUTE_CAPS.get(template)
    return needed is None or any(cap in perms for cap in needed)


def reads_for_role(role: Optional[str]) -> list[str]:
    """The :data:`READ_ROUTES` templates ``role`` may read in a view-as session (start answer, BFF mirror)."""
    return [t for t in READ_ROUTES if role_may_read(role, t)]


def _template_regex(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", template)
    return re.compile("".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts) + r"\Z")


_READ_PATTERNS: tuple[re.Pattern[str], ...] = tuple(_template_regex(t) for t in READ_ROUTES)


def allowed_route(method: str, path: str) -> Optional[str]:
    """The :data:`READ_ROUTES` template a view-as token may call with this request, or None: GET / HEAD only, the path
    matched in full (no prefix, no trailing slash, no extra segment)."""
    if (method or "").upper() not in READ_METHODS:
        return None
    for template, pattern in zip(READ_ROUTES, _READ_PATTERNS):
        if pattern.match(path or ""):
            return template
    return None


def route_allowed(method: str, path: str) -> bool:
    return allowed_route(method, path) is not None


@dataclass(frozen=True)
class ViewAsClaims:
    session_id: str
    tenant_id: str
    staff_id: str
    start_role: str


def _uuid(value: Any) -> Optional[str]:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def is_view_as(payload: Any) -> bool:
    """True when a verified JWT payload is a view-as token (carries the ``vas`` claim at all, valid or not)."""
    return isinstance(payload, dict) and VIEW_AS_CLAIM in payload


def claims_of(payload: dict) -> Optional[ViewAsClaims]:
    """The session, tenant and staff ids and the start role of a view-as payload; None when an id is not a UUID or the
    start role is missing / not a staff role (then the request is refused like an ended session)."""
    from api.core.staff_authz import ROLE_PERMS

    session_id, tenant_id, staff_id = (_uuid(payload.get(VIEW_AS_CLAIM)), _uuid(payload.get("sub")),
                                       _uuid(payload.get(STAFF_CLAIM)))
    start_role = payload.get(START_ROLE_CLAIM)
    if session_id is None or tenant_id is None or staff_id is None or payload.get("role") != "tenant":
        return None
    if not isinstance(start_role, str) or start_role not in ROLE_PERMS:
        return None
    return ViewAsClaims(session_id=session_id, tenant_id=tenant_id, staff_id=staff_id, start_role=start_role)


def refusal(method: str, path: str, payload: dict) -> bool:
    """True when a view-as ``payload`` may not make this request (the dependency-side check of tenant_role.enforce)."""
    return is_view_as(payload) and not route_allowed(method, path)


def mint_token(*, session_id: str, tenant_id: str, staff_id: str, start_role: str, name: str, plan_tier: str,
               expires_at: datetime, now: Optional[datetime] = None) -> str:
    """The tenant JWT of one view-as session (see the module docstring). Same secret and algorithm as every tenant
    token, so the tenant read routes accept it unchanged; the gate decides what it may do."""
    from api.routers.auth import JWT_ALG, JWT_SECRET, PORTAL_CLIENT

    payload = {
        "sub": tenant_id,
        "name": name,
        "plan_tier": plan_tier,
        "role": "tenant",
        "client": PORTAL_CLIENT,
        "amr": VIEW_AS_AMR,
        "csr": "viewer",
        VIEW_AS_CLAIM: session_id,
        STAFF_CLAIM: staff_id,
        START_ROLE_CLAIM: start_role,
        "iat": now or datetime.now(timezone.utc),
        "exp": expires_at,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def window(minutes: int, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    start = now or datetime.now(timezone.utc)
    return start, start + timedelta(minutes=minutes)


# ── The tenant a session may view (the start and the per-request gate apply the same rule) ─────────────────────────
# CIS 354: is_active is true exactly for these statuses (the account-status trigger keeps them in step).
TENANT_LIVE_STATUSES = frozenset({"trial", "active", "past_due"})
TENANT_CLOSED_STATUSES = frozenset({"offboarding", "closed"})
TENANT_REFUSAL_MESSAGES: dict[str, str] = {
    "internal": "The AA internal tenant is not a partner; nothing to view as.",
    "offboarded": "This tenant is offboarded; view as is not available.",
    "inactive": "This tenant is not active (suspended or not live yet); view as is not available.",
}


def tenant_refusal(tenant_id: Any, *, cancelled_at: Any, account_status: Optional[str], kind: Optional[str],
                   is_active: Optional[bool]) -> Optional[str]:
    """Why this tenant may not be viewed as, or None. One rule for the start (409) and every request (the session
    ends): the AA internal tenant (the sentinel id, or kind ``internal``) is not a partner; an offboarded one
    (``cancelled_at`` set, status offboarding / closed) is gone; a tenant that is not live (suspended, lead, applied:
    ``is_active`` false) would only answer 403 to its own reads (the tenant rate limiter's revocation)."""
    if str(tenant_id) == AA_INTERNAL_TENANT or kind == "internal":
        return "internal"
    if cancelled_at is not None or account_status in TENANT_CLOSED_STATUSES:
        return "offboarded"
    if is_active is False or (account_status is not None and account_status not in TENANT_LIVE_STATUSES):
        return "inactive"
    return None


# ── Session state (the gate re-checks it on every request) ──────────────────────────────────────────────────────────
# $1 session, $2 tenant, $3 staff. Open = not ended and not past its expiry; the staff row must still be active and the
# tenant still viewable (tenant_refusal).
SESSION_STATE_SQL = (
    "SELECT s.ended_at IS NULL AND s.expires_at > now() AS open, s.ended_at IS NULL AS not_ended, "
    "u.is_active AS staff_active, u.role::text AS staff_role, "
    "t.cancelled_at AS tenant_cancelled_at, t.account_status AS tenant_status, t.kind AS tenant_kind, "
    "t.is_active AS tenant_active "
    "FROM shared.staff_view_as_session s JOIN shared.admin_users u ON u.id = s.admin_user_id "
    "JOIN shared.tenants t ON t.tenant_id = s.tenant_id "
    "WHERE s.session_id = $1::uuid AND s.tenant_id = $2::uuid AND s.admin_user_id = $3::uuid"
)
# End one open session ($2 = the end_reason when it is not past its expiry; one past it always ends 'expired'). The
# guard lets an UPDATE only end an open session, and the WHERE makes it end once: two requests racing on the same
# session close it once, and only the one that closed it writes the tenant.view_as_ended row.
CLOSE_SQL = (
    "UPDATE shared.staff_view_as_session SET ended_at = greatest(now(), started_at), "
    "end_reason = CASE WHEN expires_at <= now() THEN 'expired' ELSE $2 END "
    "WHERE session_id = $1::uuid AND ended_at IS NULL RETURNING end_reason"
)
# Why the gate ends a session (details.cause of its tenant.view_as_ended row). The end_reason column takes only
# ended / expired / revoked (CIS 364 CHECK; no migration in this slice): a lost right ends the session 'revoked'.
GATE_END_CAUSES = frozenset({"expired", "staff_inactive", "role_lost_view_as", "role_changed", "tenant_inactive",
                             "out_of_scope"})
GATE_ACTOR = "system:view_as_gate"


def ended_audit(*, tenant_id: str, session_id: str, actor: str, end_reason: str, cause: str,
                session_staff: Optional[str], **extra: Any) -> dict:
    """The keyword arguments of write_audit_log for one tenant.view_as_ended row: one row per closed session, under
    that session's own resource id, whatever path ended it (the end route, a new start, the gate)."""
    from services.acp_shared.audit_log import AdminTenantAuditAction

    return {
        "tenant_id": tenant_id, "actor": actor[:64], "action": AdminTenantAuditAction.VIEW_AS_ENDED,
        "resource_type": AUDIT_RESOURCE, "resource_id": session_id,
        "details": {"session_id": session_id, "end_reason": end_reason, "cause": cause,
                    "session_staff": session_staff, **extra},
    }


async def close_session(conn: Any, claims: ViewAsClaims, cause: str) -> Optional[str]:
    """End the session of ``claims`` for ``cause`` (:data:`GATE_END_CAUSES`) and write its tenant.view_as_ended row,
    in one transaction (the audit row last: the chain trigger's lock). The end_reason it got, or None when it was
    already closed (another request ended it first: no second row). Best effort for the caller: the request is refused
    whatever this does; a failure leaves the session open and the next request tries again."""
    from services.acp_shared.audit_log import write_audit_log

    try:
        async with conn.transaction():
            end_reason = await conn.fetchval(CLOSE_SQL, claims.session_id,
                                             "expired" if cause == "expired" else "revoked")
            if end_reason is None:
                return None
            await write_audit_log(conn, **ended_audit(
                tenant_id=claims.tenant_id, session_id=claims.session_id, actor=GATE_ACTOR, end_reason=end_reason,
                cause=cause, session_staff=claims.staff_id))
    except Exception as e:  # noqa: BLE001 - the refusal stands; the next request retries the close
        logger.warning("view_as_close_failed", error=repr(e)[:300], session_id=claims.session_id,
                                       cause=cause)
        return None
    return end_reason


# AA-1035 follow-up (S268): the paths outside the gate that end every open session of a tenant or of a staff member
# at once, so nothing waits for the next request or the expiry. $1 = the tenant / the staff id, $2 = the end_reason of
# a session not yet past its expiry (one past it ends 'expired'). Each closed row comes back for its own ended row.
_CLOSE_ALL_SQL = (
    "UPDATE shared.staff_view_as_session SET ended_at = greatest(now(), started_at), "
    "end_reason = CASE WHEN expires_at <= now() THEN 'expired' ELSE $2 END "
    "WHERE {column} = $1::uuid AND ended_at IS NULL "
    "RETURNING session_id::text AS session_id, tenant_id::text AS tenant_id, admin_user_id::text AS session_staff, "
    "end_reason"
)
CLOSE_TENANT_SESSIONS_SQL = _CLOSE_ALL_SQL.format(column="tenant_id")
CLOSE_STAFF_SESSIONS_SQL = _CLOSE_ALL_SQL.format(column="admin_user_id")
# details.cause of the ended rows these paths write (the end route and a new start have their own, see ended_audit)
BULK_END_CAUSES = frozenset({"tenant_offboarded", "staff_disabled", "staff_signed_out"})


async def close_open_sessions(conn: Any, *, actor: str, cause: str, tenant_id: Optional[str] = None,
                              staff_id: Optional[str] = None, end_reason: str = "revoked") -> list[dict]:
    """End every open view-as session of ``tenant_id`` (offboarding) or of ``staff_id`` (the staff member disabled or
    signed out) in the caller's transaction, and write one ``tenant.view_as_ended`` row per closed session (cause
    ``cause``, actor ``actor``). Exactly one of the two ids. Returns the closed sessions ({session_id, tenant_id,
    session_staff, end_reason}).

    The UPDATE runs in a savepoint: before CIS 364 (no table) nothing is closed and the caller's transaction goes on.
    The ended rows are audit rows: the caller writes its own audit rows around them, every UPDATE of the transaction
    first (the audit chain trigger's lock)."""
    from services.acp_shared.audit_log import write_audit_log

    if (tenant_id is None) == (staff_id is None):
        raise ValueError("close_open_sessions needs exactly one of tenant_id / staff_id")
    sql, key = (CLOSE_TENANT_SESSIONS_SQL, tenant_id) if tenant_id is not None else (CLOSE_STAFF_SESSIONS_SQL,
                                                                                      staff_id)
    try:
        async with conn.transaction():
            rows = [dict(r) for r in await conn.fetch(sql, str(key), end_reason)]
    except asyncpg.UndefinedTableError:                 # before CIS 364: nothing to close
        logger.warning("view_as_close_all_unavailable", cause=cause)
        return []
    for r in rows:
        await write_audit_log(conn, **ended_audit(
            tenant_id=r["tenant_id"], session_id=r["session_id"], actor=actor, end_reason=r["end_reason"],
            cause=cause, session_staff=r["session_staff"]))
    if rows:
        logger.info("view_as_sessions_closed", cause=cause, sessions=len(rows))
    return rows


def start_role_mismatch(db_role: Optional[str], start_role: Optional[str]) -> bool:
    """True when the directory role (``shared.admin_users.role``) is not the
    role a view-as start is authorised with (the verified group role). The start refuses such a member (409) and the
    gate ends a session whose member's row moved away from it: scope and reads are evaluated with the start role
    only, and only while the two agree, so a row that drifted ABOVE the group never widens a view."""
    return db_role != start_role


async def session_open(conn: Any, claims: ViewAsClaims, template: Optional[str] = None,
                       role_hint: Optional[str] = None) -> tuple[bool, str]:
    """(open, reason) for one request: the session row is open, the staff account active, the role still holds
    tenant.view_as and is still the role the session was started with (``claims.start_role``, the signed ``srl``
    claim): the CURRENT DB role must equal it and, when the staff BFF forwarded the reader's verified group role
    (``role_hint``, :data:`ROLE_HINT_HEADER`), so must that one. The tenant is still viewable (:func:`tenant_refusal`,
    the rule of the start) and still in the START role's scope. With ``template`` (the matched READ_ROUTES entry)
    the start role must also be allowed that read (:func:`role_may_read`); else (False, :data:`NOT_FOR_ROLE_REASON`),
    which the gate answers 403, not "ended", and the session stays open.

    Every other refusal ends the session for good (:func:`close_session`: 'expired' past its expiry, else 'revoked',
    with a tenant.view_as_ended row naming the cause): a right lost during the session never comes back to it.
    ``role_hint`` can only end a session (a role other than the start role); scope and reads never come from it."""
    from api.core.staff_authz import ROLE_PERMS, TENANT_VIEW_AS
    from api.core.tenant_scope import in_scope, scope_for_role

    row = await conn.fetchrow(SESSION_STATE_SQL, claims.session_id, claims.tenant_id, claims.staff_id)
    if row is None:
        return False, "unknown_session"
    if not row["open"]:
        if row["not_ended"]:
            await close_session(conn, claims, "expired")
            return False, "expired"
        return False, "ended"
    start_role = claims.start_role
    current = [row["staff_role"]]
    hint = (role_hint or "").strip()
    if hint:
        current.append(hint)
    reason: Optional[str] = None
    if not row["staff_active"]:
        reason = "staff_inactive"
    elif any(TENANT_VIEW_AS not in ROLE_PERMS.get(r or "", frozenset()) for r in [start_role, *current]):
        reason = "role_lost_view_as"
    elif any(r != start_role for r in current):
        reason = "role_changed"
    elif tenant_refusal(claims.tenant_id, cancelled_at=row["tenant_cancelled_at"], account_status=row["tenant_status"],
                        kind=row["tenant_kind"], is_active=row["tenant_active"]) is not None:
        reason = "tenant_inactive"
    else:
        scope = scope_for_role(start_role)
        if scope is None or not await in_scope(conn, scope, claims.staff_id, claims.tenant_id):
            reason = "out_of_scope"
    if reason is not None:
        await close_session(conn, claims, reason)
        return False, reason
    if template is not None and not role_may_read(start_role, template):
        return False, NOT_FOR_ROLE_REASON
    return True, "open"


def audit_row(claims: Optional[ViewAsClaims], *, method: str, path: str, route: Optional[str], status: Optional[int],
              outcome: str, query_keys: list[str]) -> dict:
    """The acp_shared.audit_log row of one view-as request (the log writer; no body, no query values).

    ``http_method`` stays NULL: the column's CHECK (CIS 248) only takes write methods; the method is in ``details``.
    ``status`` is the refusal's status; a served read is logged BEFORE it runs (no unlogged read), so its status is
    NULL and ``details.outcome`` is ``served``."""
    return {
        "tenant_id": claims.tenant_id if claims else None,
        "actor": (f"admin:{claims.staff_id}" if claims else "staff:unknown")[:64],
        "actor_type": "staff",
        "action": AUDIT_ACTION_REQUEST,
        "resource_type": AUDIT_RESOURCE,
        "resource_id": claims.session_id if claims else "unknown",
        "route": route,
        "status": status,
        "details": {"method": (method or "").upper(), "path": (path or "")[:512], "outcome": outcome,
                    "query_keys": query_keys},
    }
