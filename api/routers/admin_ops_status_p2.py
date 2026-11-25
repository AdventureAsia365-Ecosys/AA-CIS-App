"""AA-1142 (S292) — the P2 routes of the Ecosystem status page (/admin/overview): the incident record, the per-agency
drill-down and the Dev drill that proves the error budget burn alert. The SLO and external check sections ride in
``GET /admin/overview/status`` itself (api/services/ops_status/slo.py, external.py).

- ``GET  /admin/overview/incidents``                 ops.read   open incidents first, then the newest resolved ones
- ``GET  /admin/overview/incidents/{incident_id}``   ops.read   one incident, its timeline and a lesson draft
- ``POST /admin/overview/incidents``                 ops.write  declare (from a Needs-attention item, or by hand)
- ``POST /admin/overview/incidents/{incident_id}``   ops.write  status, SEV, take over, note
- ``GET  /admin/overview/status/tenants``            ops.read   per-agency rankings, or one agency (``tenant``)
- ``GET  /admin/overview/status/slo/drill``          ops.read   Dev only: answers 503 on purpose (synthetic error
                                                                burst, scripts/ops/slo_error_burst.py)

Writes refuse the business owner (tech lead AI10, the same rule as the ack route) and fail closed when that flag cannot
be read. Every write is audited (api/services/ops_status/incidents.py).
"""
from __future__ import annotations

from typing import Literal, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import ops_status

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/overview", tags=["admin-overview"], dependencies=[Depends(staff_policy)])

_ITEM_ID = r"^[A-Za-z0-9_.:@ -]{1,200}$"
DRILL_ENVIRONMENTS = ("dev",)


class DeclareBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    severity: Literal["sev1", "sev2", "sev3"]
    app: Optional[Literal["cis", "tripplanner", "booking", "crm", "shared"]] = None
    item_id: Optional[str] = Field(None, min_length=1, max_length=200, pattern=_ITEM_ID,
                                   description="the Needs-attention item it is declared from (it must be on the page)")
    summary: Optional[str] = Field(None, max_length=2000)
    range: Literal["1h", "6h", "24h", "7d"] = Field("24h", description="the page's range (the item ids depend on it)")


class UpdateBody(BaseModel):
    status: Optional[Literal["investigating", "identified", "monitoring", "resolved"]] = None
    severity: Optional[Literal["sev1", "sev2", "sev3"]] = None
    take_over: bool = Field(False, description="the caller becomes the owner")
    note: Optional[str] = Field(None, max_length=2000)


def _incident_error(e: Exception) -> HTTPException:
    from api.services.ops_status import incidents

    if isinstance(e, asyncpg.exceptions.UndefinedTableError):
        return HTTPException(status_code=503, detail={"code": "not_migrated", "message": incidents.NOT_MIGRATED})
    detail = {"code": e.code, "message": str(e)}
    if getattr(e, "incident_id", None):
        detail["incident_id"] = e.incident_id
    return HTTPException(status_code=e.status, detail=detail)


@router.get("/incidents", summary="Incidents of the Ecosystem status page (open first)")
async def get_incidents(request: Request, open_only: bool = Query(False), x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from api.services.ops_status import incidents

    try:
        return await incidents.list_incidents(request.app.state.pool, open_only=open_only)
    except asyncpg.exceptions.UndefinedTableError as e:
        raise _incident_error(e) from e


@router.get("/incidents/{incident_id}", summary="One incident with its timeline and a lesson draft")
async def get_incident(request: Request, incident_id: int = Path(..., ge=1, le=2**62),
                       x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from api.services.ops_status import incidents

    try:
        out = await incidents.detail(request.app.state.pool, incident_id)
    except asyncpg.exceptions.UndefinedTableError as e:
        raise _incident_error(e) from e
    if out is None:
        raise HTTPException(status_code=404, detail={"code": "no_such_incident", "message": "No such incident"})
    return out


@router.post("/incidents", summary="Declare an incident (from a Needs-attention item or by hand; audited)")
async def post_incident(body: DeclareBody, request: Request, x_admin_secret: str = Header(None)):
    """ops.write, never the business owner. With ``item_id`` the item must be on the status page now (404 otherwise)
    and must not have an open incident already (409 with its id)."""
    verify_admin_secret(x_admin_secret)
    from api.routers.admin_overview import _actor, _refuse_business_owner
    from api.services.ops_status import incidents

    await _refuse_business_owner(request)
    pool = request.app.state.pool
    app = body.app
    if body.item_id:
        from api.services.ops_assist.evidence import find_item
        from api.services.platform_apps import relay_authorization

        status = await ops_status.build_status(pool, getattr(request.app.state, "redis", None), range_key=body.range,
                                               authorization=relay_authorization(request))
        item = find_item(status, body.item_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "no_such_item",
                                                         "message": "No item with this id on the status page now"})
        app = app or item.get("app")
    try:
        out = await incidents.declare(pool, title=body.title, severity=body.severity, app=app, item_id=body.item_id,
                                      summary=body.summary, actor=_actor(request))
    except (incidents.IncidentError, asyncpg.exceptions.UndefinedTableError) as e:
        raise _incident_error(e) from e
    logger.info("ops_incident_declared", incident_id=out["incident_id"], severity=out["severity"],
                item_id=body.item_id)
    return out


@router.post("/incidents/{incident_id}", summary="Update an incident: status, SEV, take over, note (audited)")
async def post_incident_update(body: UpdateBody, request: Request, incident_id: int = Path(..., ge=1, le=2**62),
                               x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from api.routers.admin_overview import _actor, _refuse_business_owner
    from api.services.ops_status import incidents

    await _refuse_business_owner(request)
    try:
        out = await incidents.update(request.app.state.pool, incident_id, status=body.status,
                                     severity=body.severity, take_over=body.take_over, note=body.note,
                                     actor=_actor(request))
    except (incidents.IncidentError, asyncpg.exceptions.UndefinedTableError) as e:
        raise _incident_error(e) from e
    logger.info("ops_incident_updated", incident_id=incident_id, status=out["status"])
    return out


@router.get("/status/tenants", summary="Per-agency drill-down: top agencies by AI spend, failed jobs, AI errors")
async def get_status_tenants(
    request: Request,
    range_: str = Query("24h", alias="range", pattern="^(1h|6h|24h|7d|custom)$"),
    from_: str | None = Query(None, alias="from", max_length=40),
    to: str | None = Query(None, max_length=40),
    tenant: str | None = Query(None, max_length=40, description="one agency's id: its breakdown instead of rankings"),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    from api.services.ops_status import tenants

    try:
        rng = ops_status.range_of(range_, from_, to)
        tid = tenants.check_tenant(tenant)
    except ops_status.RangeError as e:
        raise HTTPException(status_code=422, detail={"code": "bad_range", "message": str(e)}) from e
    except tenants.TenantError as e:
        raise HTTPException(status_code=422, detail={"code": "bad_tenant", "message": str(e)}) from e
    return await tenants.collect(request.app.state.pool, rng, tid)


@router.get("/status/slo/drill", summary="Dev only: answer 503 on purpose (proves the error budget burn alert)")
async def get_slo_drill(request: Request, x_admin_secret: str = Header(None)):
    """A synthetic server error for the burn rate drill (scripts/ops/slo_error_burst.py). Dev only (404 elsewhere);
    signed-in staff with ops.read; nothing is read or written. Each call is one 5xx in the CIS API's CloudWatch
    metrics, so a burst raises the fast burn of CIS above cis.ops.status_burn_fast_factor within one 5-minute period."""
    verify_admin_secret(x_admin_secret)
    from api.services.ops_overview import cloudwatch

    if cloudwatch.environment() not in DRILL_ENVIRONMENTS:
        raise HTTPException(status_code=404, detail={"code": "not_here", "message": "The drill runs on Dev only"})
    logger.warning("ops_status_slo_drill")
    return JSONResponse(status_code=503, content={"code": "slo_drill",
                                                  "message": "A synthetic server error for the burn rate drill"})
