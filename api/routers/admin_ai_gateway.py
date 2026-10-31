"""AA-804 (UI part 1, root ADR 0009) — the read API of the admin "AI providers" page (/admin/ai-gateway).

  GET /admin/ai-gateway/providers          every paid source in one list: LLM connections (health, spend today and
                                           this month, monthly budget), Jev (estimated credit left, last 24 h of
                                           verdicts), DataForSEO (latest stored balance), Geoapify (credits today vs
                                           the daily limit); each with its provider-wide daily cap.         spend.read
  GET /admin/ai-gateway/trace?job_id=      every logged paid call of one job in time order (LLM, Jev, DataForSEO,
                                           geocoder ledgers).                                                spend.read

Read-only and $0: database reads only, no provider is called. Admin only (POLICY spend.read; the page is in
frontend/lib/rbac.ts PAGE_RULES and API_READ_RULES). The sources and their limits: api/services/ai_gateway.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import ai_gateway

router = APIRouter(prefix="/admin/ai-gateway", tags=["admin-ai-gateway"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


@router.get("/providers", summary="Every paid AI and data provider: health, spend, budget, balance (read-only, $0)")
async def get_providers(request: Request) -> dict:
    return await ai_gateway.read_providers(request.app.state.pool)


@router.get("/trace", summary="Every logged paid call of one job in time order (LLM, Jev, DataForSEO, geocoder)")
async def get_trace(request: Request, job_id: str = Query(..., min_length=1, max_length=64)) -> dict:
    try:
        jid = ai_gateway.parse_job_id(job_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="job_id must be a job UUID")
    out = await ai_gateway.trace_job(request.app.state.pool, jid)
    if out is None:
        raise HTTPException(status_code=404, detail="no such job")
    return out
