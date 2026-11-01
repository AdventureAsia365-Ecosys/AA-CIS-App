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
                                           (shared.tenant_ai_budget, AA-1016), today's spend and a coverage
                                           list.                                                             spend.read
  GET /admin/ai-gateway/calls              the Calls tab: every logged paid call newest first across the CIS ledgers
                                           and the CRM ledger (crm.ops.v1 GET /llm/calls with the staff member's
                                           own token), filtered by job / tour / app / provider / status / stage /
                                           source / window, keyset paged.                                    spend.read

  GET /admin/ai-gateway/experiments        the Models & trials Experiments section: the prompt register
                                           (shared.prompt_version, AA-900) and the newest evaluation runs
                                           (shared.prompt_eval_runs).                                        spend.read
  GET    /admin/ai-gateway/tenant-budgets/{tenant_id}   one tenant's monthly AI budget and this month's spend
                                                        (Tenant 360 › Cost).                                 spend.read
  PUT    /admin/ai-gateway/tenant-budgets/{tenant_id}   create or replace it (audited tenant_ai_budget_set). spend.write
  DELETE /admin/ai-gateway/tenant-budgets/{tenant_id}   remove it: back to no budget (audited
                                                        tenant_ai_budget_cleared).                          spend.write

$0: database reads (and one CRM read) and the tenant budget writes; no provider is called. The other caps are edited
through their existing write routes (PUT /admin/budgets/{provider}/{scope}, PATCH /admin/llm-connections/{key}).
AA-1016 (C4): the tenant budget writes answer 503 when migration 321 (the table) is not applied, 404 for an unknown
tenant; the budget is stored and shown only: enforcement is a later gateway slice.
Admin only (POLICY spend.read / spend.write; the page is in frontend/lib/rbac.ts PAGE_RULES and API_READ_RULES). The
sources and their limits: api/services/ai_gateway.py and api/services/ai_gateway_calls.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import ai_gateway, ai_gateway_calls, ai_gateway_experiments, platform_apps, tenant_ai_budget

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


# ── AA-900 UI (C4): the Experiments section of Models & trials ─────────────────────────────────────────────────────

@router.get("/experiments", summary="The prompt register and the newest evaluation runs (read-only, $0)")
async def get_experiments(request: Request) -> dict:
    return await ai_gateway_experiments.read(request.app.state.pool)


# ── AA-1016 UI (C4): one tenant's monthly AI budget (Tenant 360 › Cost) ────────────────────────────────────────────

_TENANT_ID = Path(..., pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class TenantBudgetBody(BaseModel):
    """The whole budget row (a PUT replaces it). monthly_usd null = no amount (unlimited); alert_pct is where the bell
    rings, approval_pct (never below alert_pct) where new T-stage jobs wait for staff approval when hard_stop is on.
    The limits are migration 321's CHECKs."""
    model_config = ConfigDict(extra="forbid")
    monthly_usd: Optional[float] = Field(None, ge=0, le=1_000_000)
    alert_pct: int = Field(100, ge=1, le=1000)
    approval_pct: int = Field(150, ge=1, le=1000)
    hard_stop: bool = True          # AA-642 A8: a new budget holds T-stage jobs at approval_pct unless told otherwise
    notes: Optional[str] = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _approval_after_alert(self):
        if self.approval_pct < self.alert_pct:
            raise ValueError("approval_pct must be at least alert_pct")
        if self.monthly_usd is not None:
            self.monthly_usd = round(self.monthly_usd, 2)
        if self.notes is not None:
            self.notes = self.notes.strip() or None
        return self


def _actor(x_admin_user_id: Optional[str]) -> str:
    return f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"


@router.get("/tenant-budgets/{tenant_id}", summary="One tenant's monthly AI budget and this month's spend ($0)")
async def get_tenant_budget(request: Request, tenant_id: str = _TENANT_ID) -> dict:
    out = await tenant_ai_budget.read_one(request.app.state.pool, tenant_id.lower())
    if out is None:
        raise HTTPException(status_code=404, detail="no such tenant")
    return out


@router.put("/tenant-budgets/{tenant_id}", summary="Set one tenant's monthly AI budget (audited)")
async def put_tenant_budget(body: TenantBudgetBody, request: Request, tenant_id: str = _TENANT_ID,
                            x_admin_user_id: Optional[str] = Header(None)) -> dict:
    try:
        return await tenant_ai_budget.upsert(request.app.state.pool, tenant_id.lower(), body.model_dump(),
                                             actor=_actor(x_admin_user_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="no such tenant")
    except tenant_ai_budget.TenantBudgetUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.delete("/tenant-budgets/{tenant_id}", summary="Remove one tenant's monthly AI budget (audited)")
async def delete_tenant_budget(request: Request, tenant_id: str = _TENANT_ID,
                               x_admin_user_id: Optional[str] = Header(None)) -> dict:
    try:
        return await tenant_ai_budget.clear(request.app.state.pool, tenant_id.lower(), actor=_actor(x_admin_user_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="no such tenant")
    except tenant_ai_budget.TenantBudgetUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
