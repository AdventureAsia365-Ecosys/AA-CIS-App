"""AA-992 — GET /admin/data-coverage: the admin "Data coverage" page (numbers only, the whole system).

  GET /admin/data-coverage[?refresh=1]   every row of every stage in one call (cached: fast 60 s, slow 600 s,  ops.read
                                          CRM 600 s; refresh recomputes the fast tier when older than 15 s)

Admin only (OPS_READ, as /admin/overview; design D10). The CRM rows are read from crm.ops.v1 1.4 with the signed-in
staff member's own Cognito token (relayed like the CRM console); a password session gets those rows as
``na: cognito_session_required``. Sources and status rules: api/services/data_coverage/.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.schemas.data_coverage import DataCoverageResponse
from api.services import data_coverage
from api.services.platform_apps import relay_authorization

router = APIRouter(prefix="/admin/data-coverage", tags=["admin-data-coverage"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


@router.get("", response_model=DataCoverageResponse,
            summary="AA-992 — data coverage: live counts from raw upload to TripPlanner, Booking and CRM")
async def get_data_coverage(request: Request, refresh: bool = Query(False)) -> dict:
    state = request.app.state
    return await data_coverage.build_data_coverage(
        state.pool,
        getattr(state, "tenant_pool", None),
        getattr(state, "redis", None),
        authorization=relay_authorization(request),
        refresh=refresh,
    )
