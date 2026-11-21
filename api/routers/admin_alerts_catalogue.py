"""AA-1171 (S287 alerts catalogue P0, decision D1) — Platform › Alerts › Catalogue and its summary.

  GET /admin/platform/alerts/catalogue   every alert condition of the ecosystem, one row each, with counts, the
                                         freshness of each source and the coverage gaps
                                         (api/services/alerts_catalogue.py)
  GET /admin/platform/alerts/summary     the counts and the firing rows only: the Ecosystem status card calls this
                                         (one source, no second list)

Both read only (ops.read, admin in the BFF). The AWS reads are cached 5 minutes in Redis (no Redis = the AWS section
says ``cache_offline`` and no AWS call is made); Booking and the CRM registries are read with the staff member's own
Cognito token (a legacy session sees those two sources as unavailable). Nothing returns an ARN or a dimension value.
The router is included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import alerts_catalogue as svc
from api.services.platform_apps import relay_authorization

router = APIRouter(prefix="/admin/platform", tags=["admin-alerts-catalogue"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


def _redis(request: Request) -> Any:
    return getattr(request.app.state, "redis", None)


def _pool(request: Request) -> Any:
    return getattr(request.app.state, "pool", None)


@router.get("/alerts/catalogue", summary="Every alert condition (alarms, rules, budgets, in-app checks)")
async def alerts_catalogue(request: Request):
    return await svc.build(_pool(request), _redis(request), relay_authorization(request))


@router.get("/alerts/summary", summary="Alert counts and firing alerts (the Ecosystem status card)")
async def alerts_summary(request: Request):
    doc = await svc.build(_pool(request), _redis(request), relay_authorization(request))
    return svc.summary_view(doc)
