"""AA-1035 R2 slice 2 — the logged Writing Lab read of tenant text (root ADR 0019, settled by the tech lead S265,
item 1).

"ai_ops may read tenant text in Writing Lab: permission tenant_content.read, Lab pages only, every view logged."

ai_ops holds ``lab.tenant_text.read`` (api/core/staff_authz.py), not ``tenant_content.read``: the capability appears
only on the tenant-text GETs of ``/admin/writing-memory/*`` (POLICY ``_LAB_TEXT_READ``), so Tenant QA, Content Trace,
the A4 logs and Tenant 360 stay closed to it. Each of those routes declares :data:`LAB_TEXT_LOGGED`; for a caller that
reaches it ONLY through ``lab.tenant_text.read`` the dependency writes one ``acp_shared.audit_log`` row (action
``lab.tenant_text_read``: the staff member, role, route, concrete path, the query keys and the tenant filter) BEFORE the
route reads anything. When the row cannot be written the read is refused (503): no unlogged view. Roles that hold
``tenant_content.read`` in their own right (admin, content_lead, tenant_qa) are not logged here, as before.

tests/unit/test_aa1035_s2_lab_read.py checks that every POLICY route carrying ``lab.tenant_text.read`` declares the
dependency and lives under /admin/writing-memory/.
"""
from __future__ import annotations

import asyncio
from typing import Optional
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Request

from api.core.staff_authz import (
    LAB_TENANT_TEXT_READ, TENANT_CONTENT_READ, has_staff_credential, principal_from_role, request_staff_identity,
)

logger = structlog.get_logger()

AUDIT_ACTION = "lab.tenant_text_read"
RESOURCE_TYPE = "http_route"
WRITE_TIMEOUT_S = 3.0
UNLOGGED = "This read of tenant text could not be logged; try again."


def _uuid(value: Optional[str]) -> Optional[str]:
    try:
        return str(UUID(str(value))) if value else None
    except (TypeError, ValueError):
        return None


def needs_log(perms: frozenset[str]) -> bool:
    """A caller that reads tenant text here only through the Lab capability (ai_ops)."""
    return LAB_TENANT_TEXT_READ in perms and TENANT_CONTENT_READ not in perms


def audit_row(request: Request, role: Optional[str]) -> dict:
    from api.core.tenant_scope import staff_id_of

    staff_id = staff_id_of(request)
    route = getattr(request.scope.get("route"), "path", None)
    tenant = _uuid(request.path_params.get("tenant_id")) or _uuid(request.query_params.get("tenant_id"))
    return {
        "tenant_id": tenant,
        "actor": (f"admin:{staff_id}" if staff_id else "staff:unknown")[:64],
        "actor_type": "staff",
        "action": AUDIT_ACTION,
        "resource_type": RESOURCE_TYPE,
        "resource_id": request.url.path[:512],
        "route": route if isinstance(route, str) else None,
        "status": None,
        "details": {"role": role, "method": request.method, "query_keys": sorted(request.query_params.keys())},
    }


async def log_lab_tenant_text_read(request: Request) -> None:
    """Route dependency of every Writing Lab tenant-text GET (see the module docstring). Runs after staff_policy (a
    router-level dependency), so the caller already passed the route's capability check."""
    if not has_staff_credential(request.headers.get("x-admin-secret")):
        return
    principal = principal_from_role(request.headers.get("x-admin-role"), request_staff_identity(request))
    if principal.is_machine or not needs_log(principal.perms):
        return
    from services.acp_shared.audit_log import write_audit_log

    row = audit_row(request, principal.role)
    try:
        await asyncio.wait_for(write_audit_log(request.app.state.pool, http_method=None, **row),
                               timeout=WRITE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - every view logged: refuse the read when the row is not written
        logger.error("lab_tenant_text_read_unlogged", error=repr(e)[:300], route=row["route"], role=principal.role)
        raise HTTPException(status_code=503, detail=UNLOGGED) from e
    logger.info("lab_tenant_text_read", route=row["route"], role=principal.role, actor=row["actor"])


LAB_TEXT_LOGGED = [Depends(log_lab_tenant_text_read)]
