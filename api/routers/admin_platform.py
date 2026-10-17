"""AA-883 (S241 GĐ2) — Platform › People & access: the read-only staff directory.

  GET /admin/platform/staff              every CIS staff account (shared.admin_users) + counts
  GET /admin/platform/staff/{staff_id}   one account

Read-only on purpose (S241 design §5.4, Nghiệp Q6): accounts are still created and reset with
scripts/create_staff_user.py until Cognito (ADR 0006); role / active changes (staff.write, owners
only, Q2) come with GĐ5 once the backend verifies a staff JWT instead of trusting x-admin-role.

Only the existing columns are read: id, username, role, is_active, created_at, updated_at
(docs/architecture/db-schema-reference.md). password_hash is never selected, so it can not leak
through a later `SELECT *` refactor of the response either. There is no e-mail / last login yet
(GĐ5 migration).

Access: admin only. staff_policy looks both GETs up in api/core/staff_authz.py POLICY
(staff.read): reviewer/content and the machine principal (secret, no role) get 403. The BFF has
the same rule (frontend/lib/rbac.ts API_READ_RULES "platform"), the page is behind PAGE_RULES
/admin/platform. The router is included without an include prefix (staff_policy reads the route
template from scope["route"].path, see staff_authz._route_template).
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from api.core.staff_authz import STAFF_ROLES, staff_policy
from api.routers.admin import verify_admin_secret

router = APIRouter(prefix="/admin/platform", tags=["admin-platform"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

# The only columns this router may read. Listed explicitly (never `*`): password_hash stays out.
STAFF_COLUMNS: tuple[str, ...] = ("id", "username", "role", "is_active", "created_at", "updated_at")

_SELECT = (
    "SELECT id::text AS id, username, role::text AS role, is_active, created_at, updated_at "
    "FROM shared.admin_users"
)


def _staff_row(row: Any) -> dict:
    d = dict(row)
    out = {k: d.get(k) for k in STAFF_COLUMNS}
    for key in ("created_at", "updated_at"):
        value = out[key]
        out[key] = value.isoformat() if value is not None else None
    out["is_active"] = bool(out["is_active"])
    return out


@router.get("/staff", summary="AA-883 — CIS staff accounts (read-only)")
async def list_staff(request: Request):
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT + " ORDER BY is_active DESC, role, lower(username)")
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
    }


@router.get("/staff/{staff_id}", summary="AA-883 — one CIS staff account (read-only)")
async def get_staff(staff_id: str, request: Request):
    try:
        sid = uuid.UUID(staff_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Staff account not found") from None
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SELECT + " WHERE id = $1", sid)
    if row is None:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return _staff_row(row)
