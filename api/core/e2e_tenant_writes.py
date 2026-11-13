"""AA-1093 (S279, root ADR 0006a A10b-5) — the route-side half of the A10b writes of the tenant offboarding erasure
saga drill (api/core/e2e_staff.E2E_ERASURE_DRILL_WRITES).

The bearer middleware and the route policy let the A10b session (the Dev-only E2E CIS admin) reach five routes:
offboard and restore a tenant (api/routers/admin.py), start a drill, run the saga job now and stop a request
(api/routers/admin_tenant_erasure.py). Each of them then asks this module, before any write, job or audit row:

- :func:`refuse_unless_synthetic` — 403 ``e2e_read_only`` unless the tenant the write touches is synthetic
  (``is_canary IS TRUE`` and not the pilot: e2e_staff.is_synthetic_tenant). A no-op without a DB read for every
  identity that is not a Dev-only E2E session; any Dev-only E2E identity without the allow-list (A10d) is refused.
- :data:`SYNTHETIC_PATH_TENANT` — the same check as a route dependency on the ``{tenant_id}`` path parameter (offboard,
  restore). It reads ``request.path_params`` instead of declaring the parameter, so the OpenAPI document is unchanged.
- :func:`refuse_any_e2e` — 403 ``e2e_read_only`` for every Dev-only E2E identity: the approve and new-round routes,
  which are never E2E writes (defence in depth behind the middleware and the route policy).

Removed with the rest of A10b at the end of project code (root ADR 0006a A10b removal list).
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request

from api.core import e2e_staff
from api.core.staff_authz import request_staff_identity

logger = structlog.get_logger()


def _refused(event: str, reason: str, request: Any, **fields: Any) -> HTTPException:
    path = getattr(getattr(request, "url", None), "path", None)   # a direct call (tests) may pass a stand-in
    logger.warning(event, reason=reason, path=path, **fields)
    return HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)


async def refuse_unless_synthetic(request: Request, conn: Any, tenant_id: Any, *, event: str) -> None:
    """403 ``e2e_read_only`` (log ``event`` with ``reason``) when a Dev-only E2E session writes to a tenant that is not
    synthetic, or has no allow-list at all. ``conn`` is a connection or the pool. No-op for every other caller."""
    reason = await e2e_staff.synthetic_write_refusal(request_staff_identity(request), conn, tenant_id)
    if reason is not None:
        raise _refused(event, reason, request, tenant_id=str(tenant_id))


async def _synthetic_path_tenant(request: Request) -> None:
    if not getattr(request_staff_identity(request), "e2e_read_only", False):
        return
    async with request.app.state.pool.acquire() as conn:
        await refuse_unless_synthetic(request, conn, request.path_params.get("tenant_id"),
                                      event="tenant_offboarding_e2e_refused")


# ``@router.post("/tenants/{tenant_id}/offboard", dependencies=TENANT_SCOPED + SYNTHETIC_PATH_TENANT)``
SYNTHETIC_PATH_TENANT = [Depends(_synthetic_path_tenant)]


def refuse_any_e2e(request: Request, *, event: str) -> None:
    """403 ``e2e_read_only`` for every Dev-only E2E identity (a write that is never an E2E write)."""
    if getattr(request_staff_identity(request), "e2e_read_only", False):
        raise _refused(event, "never_e2e", request)
