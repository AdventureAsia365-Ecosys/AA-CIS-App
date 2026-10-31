"""AA-804 (UI parts 1 to 3, root ADR 0009) — the read API of the admin "AI providers" page (/admin/ai-gateway).

  GET /admin/ai-gateway/providers          every paid source in one list: LLM connections (health, spend today and
                                           this month, monthly budget), Jev (estimated credit left, last 24 h of
                                           verdicts), DataForSEO (latest stored balance), Geoapify (credits today vs
                                           the daily limit); each with its provider-wide daily cap.         spend.read
  GET /admin/ai-gateway/trace?job_id=      every logged paid call of one job in time order (LLM, Jev, DataForSEO,
      /admin/ai-gateway/trace?tour_id=     geocoder ledgers); or of one tour (its jobs, the pieces they wrote, the
                                           tour_id columns).                                                 spend.read
  GET /admin/ai-gateway/budgets            every spending cap by tier: provider daily caps and job caps
                                           (shared.spend_budget), LLM connection monthly budgets, the tenant tier
                                           (empty until AA-1016), today's spend and a coverage list.     spend.read
  GET /admin/ai-gateway/calls              the Calls tab: every logged paid call newest first across the CIS ledgers
                                           and the CRM ledger (crm.ops.v1 GET /llm/calls with the staff member's
                                           own token), filtered by job / tour / app / provider / status / stage /
                                           source / window, keyset paged.                                    spend.read

Read-only and $0: database reads (and one CRM read) only, no provider is called. The caps are edited through the
existing write routes (PUT /admin/budgets/{provider}/{scope}, PATCH /admin/llm-connections/{key}); this router never
writes. Admin only (POLICY spend.read; the page is in frontend/lib/rbac.ts PAGE_RULES and API_READ_RULES). The
sources and their limits: api/services/ai_gateway.py and api/services/ai_gateway_calls.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import ai_gateway, ai_gateway_calls, platform_apps

router = APIRouter(prefix="/admin/ai-gateway", tags=["admin-ai-gateway"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


@router.get("/providers", summary="Every paid AI and data provider: health, spend, budget, balance (read-only, $0)")
async def get_providers(request: Request) -> dict:
    return await ai_gateway.read_providers(request.app.state.pool)


@router.get("/trace", summary="Every logged paid call of one job or one tour in time order (LLM, Jev, DataForSEO, "
                              "geocoder)")
async def get_trace(request: Request, job_id: Optional[str] = Query(None, min_length=1, max_length=64),
                    tour_id: Optional[str] = Query(None, min_length=1, max_length=64)) -> dict:
    if (job_id is None) == (tour_id is None):
        raise HTTPException(status_code=422, detail="give exactly one of job_id or tour_id")
    if tour_id is not None:
        try:
            tid = ai_gateway.parse_job_id(tour_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="tour_id must be a tour UUID")
        return await ai_gateway.trace_tour(request.app.state.pool, tid)
    try:
        jid = ai_gateway.parse_job_id(job_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="job_id must be a job UUID")
    out = await ai_gateway.trace_job(request.app.state.pool, jid)
    if out is None:
        raise HTTPException(status_code=404, detail="no such job")
    return out


@router.get("/budgets", summary="Every spending cap by tier with today's spend (read-only, $0)")
async def get_budgets(request: Request) -> dict:
    return await ai_gateway.read_budgets(request.app.state.pool)


@router.get("/calls", summary="Every logged paid call, newest first, across the CIS and CRM ledgers (read-only, $0)")
async def get_calls(
    request: Request,
    job_id: Optional[str] = Query(None, max_length=64),
    tour_id: Optional[str] = Query(None, max_length=64),
    app: Optional[str] = Query(None, max_length=32),
    provider: Optional[str] = Query(None, max_length=32),
    status: Optional[str] = Query(None, max_length=32),
    stage: Optional[str] = Query(None, max_length=64),
    source: Optional[str] = Query(None, max_length=16),
    window: Optional[str] = Query(None, max_length=8),
    cursor: Optional[str] = Query(None, max_length=200),
    limit: int = Query(ai_gateway_calls.DEFAULT_LIMIT, ge=1, le=ai_gateway_calls.MAX_LIMIT),
) -> dict:
    try:
        filters = ai_gateway_calls.parse_filters(job_id=job_id, tour_id=tour_id, app=app, provider=provider,
                                                 status=status, stage=stage, source=source, window=window)
        ai_gateway_calls.parse_cursor(cursor)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    authorization = platform_apps.relay_authorization(request)

    async def crm_calls(params: dict):
        # AA-964 / crm.ops.v1 1.6: the CRM rows, with the staff member's own WORKFORCE token.
        return await platform_apps.crm_llm_calls(authorization, params)

    async def crm_usage(start, end_exclusive):
        # The CRM aggregate when its rows cannot be read (crm.ops.v1 GET /llm/usage).
        return await platform_apps.crm_llm_usage(authorization, start, end_exclusive)

    return await ai_gateway_calls.list_calls(request.app.state.pool, filters, cursor=cursor, limit=limit,
                                             crm_calls=crm_calls, crm_usage=crm_usage)
