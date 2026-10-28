"""AA-884 (S241 GĐ7 / GĐ8 / GĐ10) — Platform › Apps, Audit log and Cost per app (admin only, read only).

  GET /admin/platform/apps             every app's health / version card (cached 60 s; ?refresh=1)   ops.read
  GET /admin/platform/apps/{app_id}    one card (cis | booking | tripplanner | crm)                   ops.read
  GET /admin/platform/apps/crm/jobs    AA-936: the CRM's crm.job per kind (crm.ops.v1), staff token     ops.read
  GET /admin/platform/audit            acp_shared.audit_log rows, newest first, keyset cursor        audit.read
  GET /admin/platform/audit/verify     recompute the CIS 248 hash chain (full chain cached 60 s)     audit.read
  GET /admin/platform/cost             cost per app for a month (CE App-tag snapshot + LLM spend)    spend.read

No write route, no deploy / scale button, no AWS / Vercel / GitHub write token (S241). Separate from
admin_platform.py (the staff directory, AA-816 lane) so the two never collide. Included in main.py WITHOUT an
include prefix: staff_policy reads the route template from scope["route"].path (staff_authz._route_template).

GET /cost may refresh a stale month itself (one Cost Explorer request, ~$0.01, at most about once a day per open
month, behind a Redis lock — api/services/platform_cost.py); ?refresh_if_stale=false reads stored data only. The
explicit "Refresh from AWS" stays POST /admin/cost-explorer/check (spend.write), which also fetches the App tag.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import platform_apps, platform_audit, platform_cost

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/platform", tags=["admin-platform-ops"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


def _redis(request: Request):
    return getattr(request.app.state, "redis", None)


@router.get("/apps", summary="Health and version of every app (cached 60 s)")
async def get_apps(request: Request, refresh: bool = Query(False)):
    return await platform_apps.collect_apps(request.app.state.pool, _redis(request), refresh=refresh)


# Literal /apps/crm/jobs before /apps/{app_id}/… (FastAPI matches in order; {app_id} takes one segment anyway).
@router.get("/apps/crm/jobs", summary="The partner CRM's job backlog per kind (crm.ops.v1)")
async def get_crm_jobs(request: Request):
    # The CRM accepts only a WORKFORCE cis-admin token on /crm/ops/v1: the staff member's own Bearer is relayed.
    return await platform_apps.crm_jobs(platform_apps.relay_authorization(request))


@router.get("/apps/{app_id}", summary="One app's health and version card")
async def get_app(request: Request, app_id: str, refresh: bool = Query(False)):
    card = await platform_apps.collect_app(request.app.state.pool, _redis(request), app_id, refresh=refresh)
    if card is None:
        raise HTTPException(status_code=404, detail=f"Unknown app '{app_id}'")
    return card


# Literal /audit/verify before any /audit/{...} route (none today; keep the order if one is added).
@router.get("/audit/verify", summary="Verify the audit_log hash chain (CIS 248)")
async def verify_audit(request: Request, from_seq: Optional[int] = Query(None, ge=1)):
    return await platform_audit.verify_chain_cached(request.app.state.pool, _redis(request), from_seq=from_seq)


@router.get("/audit", summary="Staff action trail (acp_shared.audit_log)")
async def list_audit(
    request: Request,
    scope: str = Query("staff", pattern="^(staff|all)$"),
    actor: Optional[str] = Query(None, max_length=64),
    action: Optional[str] = Query(None, max_length=64, description="prefix match"),
    resource_type: Optional[str] = Query(None, max_length=32),
    resource_id: Optional[str] = Query(None, max_length=512),
    route: Optional[str] = Query(None, max_length=512),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    cursor: Optional[str] = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=platform_audit.MAX_LIMIT),
):
    try:
        return await platform_audit.list_audit(
            request.app.state.pool, scope=scope, actor=actor, action=action, resource_type=resource_type,
            resource_id=resource_id, route=route, since=since, until=until, cursor=cursor, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/cost", summary="Cost per app for a month (AWS by App tag + LLM spend by app)")
async def get_cost(request: Request, month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
                   refresh_if_stale: bool = Query(True)):
    try:
        authorization = platform_apps.relay_authorization(request)

        async def crm_llm(start, end_exclusive):
            # AA-936: the CRM row's LLM spend from crm.ops.v1, with the staff member's own WORKFORCE token.
            return await platform_apps.crm_llm_usage(authorization, start, end_exclusive)

        return await platform_cost.cost_by_app(request.app.state.pool, month, redis=_redis(request),
                                               refresh_if_stale=refresh_if_stale, crm_llm=crm_llm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
