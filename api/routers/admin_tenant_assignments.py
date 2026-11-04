"""AA-1035 R2 slice 2 (root ADR 0019 d4) — the Assignments page (Tenants › Assignments): who looks after which tenant.

* ``GET /admin/tenant-assignments``        — every partner tenant of the caller's scope (``tenant_scope_filter``: admin
  all, customer_success its book, support its book and the pool) with its owner / backup / support assignments and
  whether it is in the support pool. Capability ``tenant.read`` or ``tenant.book.read``.
* ``GET /admin/tenant-assignments/staff``  — the staff members that may be assigned (active, a role that works a tenant
  book: admin, customer_success, support). Capability ``tenant.assign`` (admin): the picker of the page's assign
  drawer; names and e-mail only.

The writes stay the per-tenant routes of slice 1 (``PUT`` / ``DELETE /admin/tenants/{tenant_id}/assignments/
{admin_user_id}``, ``tenant.assign``, audited); since slice 2 the PUT also refuses a staff member whose role works no
tenant book (an assignment would give them nothing: api/routers/admin_tenant360.py ``ASSIGNABLE_ROLES``).
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from api.core.staff_authz import (
    TENANT_ASSIGN, StaffPrincipal, principal_from_role, request_staff_identity, staff_policy,
)
from api.core.tenant_scope import AA_INTERNAL_TENANT, tenant_scope_filter
from api.routers.admin import tenant_status, verify_admin_secret
from api.routers.admin_tenant360 import ASSIGNABLE_ROLES

logger = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin-tenant-assignments"], dependencies=[Depends(staff_policy)])

KIND_ORDER = {"owner": 0, "backup": 1, "support": 2}

TENANTS_SQL = (
    "SELECT tenant_id::text AS tenant_id, name, slug, kind, segment, is_active, cancelled_at FROM shared.tenants "
    "WHERE tenant_id <> $1::uuid ORDER BY lower(name), tenant_id"
)
ASSIGNMENTS_SQL = (
    "SELECT a.tenant_id::text AS tenant_id, a.admin_user_id::text AS admin_user_id, a.kind, a.created_at, "
    "a.created_by, "
    "u.display_name, u.role::text AS staff_role, u.is_active "
    "FROM shared.staff_tenant_assignment a JOIN shared.admin_users u ON u.id = a.admin_user_id "
    "ORDER BY a.tenant_id, a.kind, u.display_name NULLS LAST"
)
STAFF_SQL = (
    "SELECT id::text AS admin_user_id, display_name, email, role::text AS role FROM shared.admin_users "
    "WHERE is_active AND role::text = ANY($1::text[]) "
    "ORDER BY lower(coalesce(display_name, email, username)), id"
)


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None else None


def in_pool(tenant: Any, assignments: list[dict]) -> bool:
    """The support pool rule of api/core/tenant_scope.py SQL_POOL: an agency or demo tenant with no owner, or in the
    tech_touch segment."""
    if tenant["kind"] not in ("agency", "demo"):
        return False
    return tenant["segment"] == "tech_touch" or not any(a["kind"] == "owner" for a in assignments)


def _caller(principal: Any, request: Request) -> StaffPrincipal:
    if isinstance(principal, StaffPrincipal):
        return principal
    return principal_from_role(request.headers.get("x-admin-role"), request_staff_identity(request))


@router.get("/tenant-assignments", summary="Every tenant of the caller's scope with its staff assignments")
async def list_tenant_assignments(
    request: Request, principal: StaffPrincipal = Depends(staff_policy), x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    caller = _caller(principal, request)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        scope = await tenant_scope_filter(request, conn)
        try:
            tenants = await conn.fetch(TENANTS_SQL, AA_INTERNAL_TENANT)
            rows = await conn.fetch(ASSIGNMENTS_SQL)
        except asyncpg.UndefinedTableError as exc:
            logger.error("tenant_assignments_unavailable", reason=type(exc).__name__)
            raise HTTPException(status_code=503, detail=(
                "Assignments need migration 364 (shared.staff_tenant_assignment).")) from exc
    by_tenant: dict[str, list[dict]] = {}
    for r in rows:
        by_tenant.setdefault(r["tenant_id"], []).append({
            "admin_user_id": r["admin_user_id"], "kind": r["kind"], "display_name": r["display_name"],
            "staff_role": r["staff_role"], "is_active": r["is_active"], "created_at": _iso(r["created_at"]),
            "created_by": r["created_by"]})
    out = []
    for t in tenants:
        if scope is not None and t["tenant_id"] not in scope:
            continue
        assignments = sorted(by_tenant.get(t["tenant_id"], []), key=lambda a: KIND_ORDER.get(a["kind"], 9))
        out.append({
            "tenant_id": t["tenant_id"], "name": t["name"], "slug": t["slug"], "kind": t["kind"],
            "segment": t["segment"], "status": tenant_status(t["is_active"], t["cancelled_at"]),
            "in_pool": in_pool(t, assignments), "assignments": assignments,
        })
    return {"tenants": out, "can_assign": TENANT_ASSIGN in caller.perms,
            "scope": "all" if scope is None else "scoped"}


@router.get("/tenant-assignments/staff", summary="Staff members that may be assigned to a tenant (admin)")
async def list_assignable_staff(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(STAFF_SQL, list(ASSIGNABLE_ROLES))
    return {"staff": [{"admin_user_id": r["admin_user_id"], "display_name": r["display_name"], "email": r["email"],
                       "role": r["role"]} for r in rows], "roles": list(ASSIGNABLE_ROLES)}
