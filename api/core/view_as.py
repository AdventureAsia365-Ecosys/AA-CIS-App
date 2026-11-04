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
   ``vas`` (the session id), ``stf`` (the staff member) and ``amr`` ``staff_view_as``. It expires with the session.
   The start answers the token only to the staff BFF that asked for it (request header :data:`BFF_HEADER`, never
   set by the generic admin proxy, which also refuses this path): the token never reaches a browser.
2. Every request that carries such a token passes :class:`api.middleware.view_as.ViewAsGateMiddleware` first:
   - only GET / HEAD on :data:`READ_ROUTES` (an exact allow-list of side-effect-free tenant reads) is let through; any
     other method or path answers 403 ``view_as_read_only`` — writes are refused on the server whatever the UI shows,
     and so are reads that start work (``GET /v1/slate`` enqueues a paid proposal job), exports and the brand branch;
   - the session must still be open (not ended, not expired), the staff account active, its role still holding
     ``tenant.view_as`` and the tenant still in its scope (assignment removed = view ends): else 401 ``view_as_ended``;
   - the read must be one the staff member's CURRENT role may see (:func:`role_may_read`, :data:`ROUTE_CAPS`): a read
     that carries the plan or usage (``/v1/quota``) needs a money capability of its own on top of ``tenant.view_as``,
     so support never sees it; else 403 ``view_as_not_for_role``. View-as never widens a role's own reads;
   - every request, refused or served, writes one ``acp_shared.audit_log`` row (:func:`audit_row`, action
     ``tenant.view_as_request``: actor the staff member, the session, method, route, status). A request is served only
     after its session check; its row is written once the response status is known.
3. Defence in depth: ``api/middleware/tenant_role.enforce`` (called by the two dependencies that accept a tenant JWT)
   refuses a view-as payload on anything but an allowed read too (:func:`refusal`), so a header shape the middleware
   did not foresee still cannot write.
4. ``POST /admin/view-as/sessions/{session_id}/end`` ends it (``ended``; an admin ending someone else's: ``revoked``);
   an expired session reads as ended and is closed as ``expired`` the next time it is touched.

Only staff paths and the tenant read routes are involved; no tenant member ever sees a view-as row (the tenant
activity trail is an allow-list of actions, api/services/tenant_account.TENANT_VISIBLE_ACTIONS).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import jwt

VIEW_AS_CLAIM = "vas"
STAFF_CLAIM = "stf"
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
)


# Reads that need a capability of their own on top of tenant.view_as (any one of the listed): the role x read matrix.
# View-as must never show a role more than its own staff pages do. /v1/quota carries the plan tier and the rewrites
# used: the Tenant 360 account / usage reads (tenant.read: admin; tenant.book.account.read: customer_success), never
# support. A read not listed here needs tenant.view_as only.
ROUTE_CAPS: dict[str, tuple[str, ...]] = {
    "/v1/quota": ("tenant.read", "tenant.book.account.read"),
}


def role_may_read(role: Optional[str], template: str) -> bool:
    """May a staff member of ``role`` (its CURRENT role, read from the DB) make the view-as read ``template``?
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


def _uuid(value: Any) -> Optional[str]:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def is_view_as(payload: Any) -> bool:
    """True when a verified JWT payload is a view-as token (carries the ``vas`` claim at all, valid or not)."""
    return isinstance(payload, dict) and VIEW_AS_CLAIM in payload


def claims_of(payload: dict) -> Optional[ViewAsClaims]:
    """The session, tenant and staff ids of a view-as payload; None when one of them is not a UUID (then the request
    is refused like an ended session)."""
    session_id, tenant_id, staff_id = (_uuid(payload.get(VIEW_AS_CLAIM)), _uuid(payload.get("sub")),
                                       _uuid(payload.get(STAFF_CLAIM)))
    if session_id is None or tenant_id is None or staff_id is None or payload.get("role") != "tenant":
        return None
    return ViewAsClaims(session_id=session_id, tenant_id=tenant_id, staff_id=staff_id)


def refusal(method: str, path: str, payload: dict) -> bool:
    """True when a view-as ``payload`` may not make this request (the dependency-side check of tenant_role.enforce)."""
    return is_view_as(payload) and not route_allowed(method, path)


def mint_token(*, session_id: str, tenant_id: str, staff_id: str, name: str, plan_tier: str,
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
        "iat": now or datetime.now(timezone.utc),
        "exp": expires_at,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def window(minutes: int, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    start = now or datetime.now(timezone.utc)
    return start, start + timedelta(minutes=minutes)


# ── Session state (the gate re-checks it on every request) ──────────────────────────────────────────────────────────
# $1 session, $2 tenant, $3 staff. Open = not ended and not past its expiry; the staff row must still be active.
SESSION_STATE_SQL = (
    "SELECT s.ended_at IS NULL AND s.expires_at > now() AS open, s.ended_at IS NULL AS not_ended, "
    "u.is_active AS staff_active, u.role::text AS staff_role "
    "FROM shared.staff_view_as_session s JOIN shared.admin_users u ON u.id = s.admin_user_id "
    "WHERE s.session_id = $1::uuid AND s.tenant_id = $2::uuid AND s.admin_user_id = $3::uuid"
)
# Close an expired session the first time it is touched after its expiry (guard: UPDATE only ends an open session).
EXPIRE_SQL = (
    "UPDATE shared.staff_view_as_session SET ended_at = greatest(now(), started_at), end_reason = 'expired' "
    "WHERE session_id = $1::uuid AND ended_at IS NULL AND expires_at <= now()"
)


async def session_open(conn: Any, claims: ViewAsClaims, template: Optional[str] = None) -> tuple[bool, str]:
    """(open, reason) for one request: the session row is open, the staff account active, its CURRENT role (the DB
    row, not the token) still holds tenant.view_as and the tenant is still in that role's scope. With ``template``
    (the matched READ_ROUTES entry) that role must also be allowed that read (:func:`role_may_read`); else
    (False, :data:`NOT_FOR_ROLE_REASON`), which the gate answers 403, not "ended"."""
    from api.core.staff_authz import ROLE_PERMS, TENANT_VIEW_AS
    from api.core.tenant_scope import in_scope, scope_for_role

    row = await conn.fetchrow(SESSION_STATE_SQL, claims.session_id, claims.tenant_id, claims.staff_id)
    if row is None:
        return False, "unknown_session"
    if not row["open"]:
        if row["not_ended"]:
            await conn.execute(EXPIRE_SQL, claims.session_id)
            return False, "expired"
        return False, "ended"
    if not row["staff_active"]:
        return False, "staff_inactive"
    role = row["staff_role"]
    if TENANT_VIEW_AS not in ROLE_PERMS.get(role or "", frozenset()):
        return False, "role_lost_view_as"
    scope = scope_for_role(role)
    if scope is None or not await in_scope(conn, scope, claims.staff_id, claims.tenant_id):
        return False, "out_of_scope"
    if template is not None and not role_may_read(role, template):
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
