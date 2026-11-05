"""AA-1035 (R2 slice 1, root ADR 0019 d4) — which tenants a staff member may reach: ``require_tenant_scope``.

ADR 0019 d4: "Every {tenant_id} route uses the require_tenant_scope dependency. A CI test checks that every such route
uses it." The capability check (api/core/staff_authz.py POLICY) says WHAT a role may do; this module says ON WHICH
tenant. Both run on every staff route with a ``{tenant_id}`` path parameter: staff_policy first (router-level), then
this dependency (route-level), before the endpoint reads anything.

Scope per role (:data:`TENANT_SCOPE`, deny by default: a role not listed reaches no tenant):

- ``all``      admin (bypasses), finance (billing columns of every tenant), ai_ops (AI budgets of every tenant),
               content_lead and tenant_qa (Tenant QA and the Writing Lab across tenants). Their capabilities still
               decide which {tenant_id} routes they may call at all.
- ``book``     customer_success: the tenants in ``shared.staff_tenant_assignment`` for this staff member (any kind:
               owner, backup or support).
- ``book_pool`` support: the same, plus the pool: agency and demo tenants with no ``owner`` assignment or in the
               ``tech_touch`` segment (clarifications.md section 3: "Tenants of a small plan or with no account manager
               are in the Pool view every support member sees").
- ``internal`` content and viewer: only the AA internal tenant, whose catalog Master Content reads
               through ``GET /admin/tenants/{tenant_id}/details``.

Who the staff member is: the Cognito identity the staff bearer middleware verified (``admin_id``). A request with the
MAIN admin secret and no verified identity (the machine, CI, an operator script) may name the staff row in
``x-admin-user-id``: the main secret already lets its holder claim any role, admin included, so trusting the id under
it grants nothing new. The Preview credential (capped to viewer: ``internal``) and any other request without an id
reach no ``book`` / ``book_pool`` tenant (fail closed, 403).

A request without a staff credential is left to the router's own guard (routers on staff_policy_if_staff also take a
tenant token); staff_policy has already refused it on the others. The machine (main secret, no role) is not scoped:
POLICY already limits it to MACHINE_ROUTES and unlisted GETs.

Refusals are 403 ``Tenant outside your scope`` whether the tenant exists or not, so a scoped caller can not probe ids.
``tenant_scope_filter`` gives list routes (the Tenants directory) the same answer as a set of tenant ids.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from uuid import UUID

import asyncpg
import structlog
from fastapi import Depends, HTTPException, Request

from api.core.staff_authz import (
    ROLE_PERMS,
    StaffPrincipal,
    _is_valid_secret,
    has_staff_credential,
    principal_from_role,
    request_staff_identity,
)

logger = structlog.get_logger()

AA_INTERNAL_TENANT = "00000000-0000-0000-0000-000000000001"

SCOPE_ALL = "all"
SCOPE_BOOK = "book"
SCOPE_BOOK_POOL = "book_pool"
SCOPE_INTERNAL = "internal"

TENANT_SCOPE: dict[str, str] = {
    "admin": SCOPE_ALL,
    "content_lead": SCOPE_ALL,
    "tenant_qa": SCOPE_ALL,
    "finance": SCOPE_ALL,
    "ai_ops": SCOPE_ALL,
    "customer_success": SCOPE_BOOK,
    "support": SCOPE_BOOK_POOL,
    "content": SCOPE_INTERNAL,
    "viewer": SCOPE_INTERNAL,
}

OUT_OF_SCOPE = "Tenant outside your scope"
NO_STAFF_ID = "Tenant scope needs a verified staff identity"
SCOPE_UNAVAILABLE = "Tenant scope is unavailable (migration 364 not applied)"

ASSIGNMENT_KINDS: tuple[str, ...] = ("owner", "backup", "support")

# The tenants a staff member is assigned to, and (pool) every agency / demo tenant without an owner or in the
# tech_touch segment. $1 = the staff member's admin_users.id.
SQL_BOOK = (
    "SELECT a.tenant_id::text AS tenant_id FROM shared.staff_tenant_assignment a WHERE a.admin_user_id = $1::uuid"
)
SQL_POOL = (
    "SELECT t.tenant_id::text AS tenant_id FROM shared.tenants t "
    "WHERE t.kind IN ('agency', 'demo') AND (t.segment = 'tech_touch' OR NOT EXISTS ("
    "SELECT 1 FROM shared.staff_tenant_assignment o WHERE o.tenant_id = t.tenant_id AND o.kind = 'owner'))"
)
SQL_IN_BOOK = (
    "SELECT EXISTS (SELECT 1 FROM shared.staff_tenant_assignment "
    "WHERE admin_user_id = $1::uuid AND tenant_id = $2::uuid) AS ok"
)
SQL_IN_POOL = (
    "SELECT EXISTS (SELECT 1 FROM shared.tenants t WHERE t.tenant_id = $1::uuid AND t.kind IN ('agency', 'demo') "
    "AND (t.segment = 'tech_touch' OR NOT EXISTS (SELECT 1 FROM shared.staff_tenant_assignment o "
    "WHERE o.tenant_id = t.tenant_id AND o.kind = 'owner'))) AS ok"
)


def scope_for_role(role: Optional[str]) -> Optional[str]:
    """The scope kind of a staff role, or None (unknown role: no tenant)."""
    return TENANT_SCOPE.get(role or "") if (role or "") in ROLE_PERMS else None


def _uuid_text(value: Any) -> Optional[str]:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def staff_id_of(request: Request) -> Optional[str]:
    """The staff member behind the request: the verified Cognito identity, else (main admin secret only) the
    ``x-admin-user-id`` header; None when neither holds a UUID."""
    identity = request_staff_identity(request)
    if identity is not None and getattr(identity, "auth", None) == "cognito":
        return _uuid_text(getattr(identity, "admin_id", None))
    if _is_valid_secret(request.headers.get("x-admin-secret")):
        return _uuid_text(request.headers.get("x-admin-user-id"))
    return None


def principal_of(request: Request) -> StaffPrincipal:
    return principal_from_role(request.headers.get("x-admin-role"), request_staff_identity(request))


async def in_scope(conn: Any, scope: str, staff_id: Optional[str], tenant_id: str) -> bool:
    """Pure decision for one tenant (``tenant_id`` already a canonical UUID string)."""
    if scope == SCOPE_ALL:
        return True
    if scope == SCOPE_INTERNAL:
        return tenant_id == AA_INTERNAL_TENANT
    if scope not in (SCOPE_BOOK, SCOPE_BOOK_POOL) or staff_id is None:
        return False
    if await conn.fetchval(SQL_IN_BOOK, staff_id, tenant_id):
        return True
    return scope == SCOPE_BOOK_POOL and bool(await conn.fetchval(SQL_IN_POOL, tenant_id))


def _refuse(request: Request, principal: StaffPrincipal, tenant_id: Any, reason: str, detail: str) -> HTTPException:
    logger.warning("tenant_scope_denied", method=request.method, path_tenant=str(tenant_id)[:64],
                   role=principal.role, reason=reason)
    return HTTPException(status_code=403, detail=detail)


async def require_tenant_scope(request: Request) -> None:
    """Route dependency of every staff route with a ``{tenant_id}`` path parameter (see the module docstring)."""
    if not has_staff_credential(request.headers.get("x-admin-secret")):
        return   # not a staff call: staff_policy refused it already, or the router's own guard decides
    principal = principal_of(request)
    if principal.is_machine:
        return
    raw = request.path_params.get("tenant_id")
    scope = scope_for_role(principal.role)
    if scope is None:
        raise _refuse(request, principal, raw, "unknown_role", OUT_OF_SCOPE)
    if scope == SCOPE_ALL:
        return
    tenant_id = _uuid_text(raw)
    if tenant_id is None:
        raise _refuse(request, principal, raw, "bad_tenant_id", OUT_OF_SCOPE)
    if scope == SCOPE_INTERNAL:
        if tenant_id != AA_INTERNAL_TENANT:
            raise _refuse(request, principal, raw, "internal_only", OUT_OF_SCOPE)
        return
    staff_id = staff_id_of(request)
    if staff_id is None:
        raise _refuse(request, principal, raw, "no_staff_id", NO_STAFF_ID)
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            ok = await in_scope(conn, scope, staff_id, tenant_id)
    except asyncpg.UndefinedTableError as exc:
        logger.error("tenant_scope_unavailable", error=type(exc).__name__)
        raise HTTPException(status_code=503, detail=SCOPE_UNAVAILABLE) from exc
    if not ok:
        raise _refuse(request, principal, raw, "not_in_book", OUT_OF_SCOPE)


async def tenant_scope_filter(request: Request, conn: Any) -> Optional[frozenset[str]]:
    """For list routes: None = every tenant (machine, a non-staff call or an ``all`` role), else the tenant ids
    (canonical UUID strings) the caller may see. An unknown role or a book role without a staff id sees none."""
    if not has_staff_credential(request.headers.get("x-admin-secret")):
        return None
    principal = principal_of(request)
    if principal.is_machine:
        return None
    scope = scope_for_role(principal.role)
    if scope == SCOPE_ALL:
        return None
    if scope == SCOPE_INTERNAL:
        return frozenset({AA_INTERNAL_TENANT})
    staff_id = staff_id_of(request) if scope in (SCOPE_BOOK, SCOPE_BOOK_POOL) else None
    if staff_id is None:
        return frozenset()
    try:
        ids = {r["tenant_id"] for r in await conn.fetch(SQL_BOOK, staff_id)}
        if scope == SCOPE_BOOK_POOL:
            ids |= {r["tenant_id"] for r in await conn.fetch(SQL_POOL)}
    except asyncpg.UndefinedTableError as exc:
        logger.error("tenant_scope_unavailable", error=type(exc).__name__)
        raise HTTPException(status_code=503, detail=SCOPE_UNAVAILABLE) from exc
    return frozenset(ids)



# AA-1035 R2 close-out (root ADR 0019 d4: "the crm.ops.v1 relay forwards the staff member's scope"): the scope of a
# staff member the partner CRM names by WORKFORCE identity (tenant.membership.v1 1.3 getStaffTenantScope). Only the two
# desk roles have a book to forward: every other role reaching the CRM console is cis-admin (scope all, the CRM's
# own rule) or reaches none.
DESK_ROLES: tuple[str, ...] = ("customer_success", "support")
SQL_ACTIVE_STAFF_BY_IDENTITY = (
    "SELECT id::text AS id FROM shared.admin_users WHERE cognito_iss = $1 AND cognito_sub = $2 AND is_active"
)


async def desk_scope_of_identity(conn: Any, iss: str, sub: str, role: str) -> tuple[str, list[str]]:
    """(scope, tenant ids sorted) of the ACTIVE staff row bound to ``(iss, sub)`` for the desk ``role`` the CRM read
    from the token's groups (the role decides the scope kind, as require_tenant_scope does with the group role; the
    directory row only says who the person is). An unknown or inactive identity: ("none", [])."""
    if role not in DESK_ROLES:
        raise ValueError(f"not a desk role: {role!r}")
    staff_id = await conn.fetchval(SQL_ACTIVE_STAFF_BY_IDENTITY, iss, sub)
    if staff_id is None:
        return "none", []
    scope = TENANT_SCOPE[role]
    ids = {r["tenant_id"] for r in await conn.fetch(SQL_BOOK, staff_id)}
    if scope == SCOPE_BOOK_POOL:
        ids |= {r["tenant_id"] for r in await conn.fetch(SQL_POOL)}
    return scope, sorted(ids)


_SQL_COLUMN = re.compile(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?\Z")


def tenant_scope_sql(scope: Optional[str], staff_id: Optional[str], column: str) -> str:
    """A SQL predicate: the uuid ``column`` names a tenant ``scope`` reaches for this staff member — the rule of
    :func:`in_scope` / :func:`tenant_scope_filter` (same book and pool queries as :data:`SQL_IN_BOOK` /
    :data:`SQL_IN_POOL`) for a query over many rows (the desk roles' bell, api/routers/admin.py notification_scope).

    Built from constants only: ``column`` must be a plain (optionally table-qualified) identifier, and ``staff_id`` is
    re-validated as a UUID before it is inlined as a literal; a missing or invalid staff id reaches no book (FALSE),
    an unknown scope no tenant."""
    if not _SQL_COLUMN.match(column or ""):
        raise ValueError(f"not a column identifier: {column!r}")
    if scope == SCOPE_ALL:
        return "TRUE"
    if scope == SCOPE_INTERNAL:
        return f"({column} = '{AA_INTERNAL_TENANT}'::uuid)"
    staff = _uuid_text(staff_id) if staff_id is not None else None
    if scope not in (SCOPE_BOOK, SCOPE_BOOK_POOL) or staff is None:
        return "FALSE"
    book = (f"EXISTS (SELECT 1 FROM shared.staff_tenant_assignment sa WHERE sa.admin_user_id = '{staff}'::uuid "
            f"AND sa.tenant_id = {column})")
    if scope == SCOPE_BOOK:
        return f"({book})"
    pool = (f"EXISTS (SELECT 1 FROM shared.tenants st WHERE st.tenant_id = {column} AND st.kind IN ('agency', 'demo') "
            "AND (st.segment = 'tech_touch' OR NOT EXISTS (SELECT 1 FROM shared.staff_tenant_assignment so "
            "WHERE so.tenant_id = st.tenant_id AND so.kind = 'owner')))")
    return f"({book} OR {pool})"


# The route-level dependency list every staff route with a {tenant_id} path parameter declares:
# ``@router.get("/tenants/{tenant_id}/x", dependencies=TENANT_SCOPED)`` (tests/unit/test_aa1035_tenant_scope_routes.py).
TENANT_SCOPED = [Depends(require_tenant_scope)]
