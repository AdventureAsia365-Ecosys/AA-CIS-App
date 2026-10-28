"""
/v1/account — AA-761, the tenant portal's Account pages (Activity, Integrations › API key).

  GET /v1/account/activity          the tenant's own activity trail (acp_shared.audit_log), newest first, paged
  GET /v1/account/activity/export   the same filtered set as CSV (text/csv)
  GET /v1/account/api-key           masked key, issue date, last sign-in / API call with it

Tenant-JWT-gated (``get_tenant``, the dependency every /v1 tenant route shares; a staff token is 403). The tenant id
comes ONLY from the verified token; no route takes one from the query string. The reads run on the main pool with
that id bound in every query: ``acp_shared.audit_log`` has no RLS and no tenant-role grant, so the tenant pool
(``acquire_scoped_conn``) cannot read it — the /v1/billing pattern (AA-496). What a tenant may see of the trail is
an allowlist of actions (``api.services.tenant_account.TENANT_VISIBLE_ACTIONS``); staff-only rows never leave.
Billing (plan, usage, overage, reset date) stays on GET /v1/billing and GET /v1/quota (v1_tours.py).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from api.routers.v1_tours import get_tenant
from api.schemas.public_api import AccountActivityPage, AccountApiKey
from api.services import tenant_account

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/account", tags=["tenant-account"])


def _tenant_id(tenant: dict) -> str:
    try:
        return tenant_account.normalise_tenant_id(tenant.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="Tenant token required")


def _filters(category: Optional[str], action: Optional[str], since: Optional[date],
             until: Optional[date]) -> tenant_account.ActivityFilters:
    f = tenant_account.ActivityFilters(category=category or None, action=action or None, since=since, until=until)
    try:
        tenant_account.validate_filters(f)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return f


# Literal /activity/export before any /activity/{...} route (none today; keep the order if one is added).
class CsvResponse(Response):
    media_type = "text/csv"


@router.get("/activity/export", summary="The tenant's activity trail as CSV (current filters)",
            response_class=CsvResponse, responses={200: {"content": {"text/csv": {}}}})
async def export_activity(
    request: Request,
    category: Optional[str] = Query(None, max_length=32),
    action: Optional[str] = Query(None, max_length=64),
    since: Optional[date] = Query(None, description="first UTC day, inclusive (YYYY-MM-DD)"),
    until: Optional[date] = Query(None, description="last UTC day, inclusive (YYYY-MM-DD)"),
    tenant=Depends(get_tenant),
):
    tenant_id = _tenant_id(tenant)
    f = _filters(category, action, since, until)
    body = await tenant_account.export_activity_csv(request.app.state.pool, tenant_id, f)
    logger.info("tenant_activity_exported", tenant_id=tenant_id, category=f.category, action=f.action,
                bytes=len(body))
    return CsvResponse(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="activity.csv"', "Cache-Control": "no-store"},
    )


@router.get("/activity", summary="The tenant's activity trail, newest first, server paging",
            response_model=AccountActivityPage)
async def list_activity(
    request: Request,
    category: Optional[str] = Query(None, max_length=32),
    action: Optional[str] = Query(None, max_length=64),
    since: Optional[date] = Query(None, description="first UTC day, inclusive (YYYY-MM-DD)"),
    until: Optional[date] = Query(None, description="last UTC day, inclusive (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(25, ge=1, le=tenant_account.MAX_PAGE_SIZE),
    tenant=Depends(get_tenant),
):
    tenant_id = _tenant_id(tenant)
    f = _filters(category, action, since, until)
    return await tenant_account.list_activity(request.app.state.pool, tenant_id, f, page=page, page_size=page_size)


@router.get("/api-key", summary="Masked API key, issue date and last use", response_model=AccountApiKey)
async def get_api_key(request: Request, tenant=Depends(get_tenant)):
    return await tenant_account.api_key_facts(request.app.state.pool, _tenant_id(tenant))
