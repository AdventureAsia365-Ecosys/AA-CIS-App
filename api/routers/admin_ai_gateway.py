"""AA-804 (UI parts 1 to 3, root ADR 0009) — the read API of the admin "AI providers" page (/admin/ai-gateway).

  GET /admin/ai-gateway/providers          every paid source in one list: LLM connections (health, spend today and
                                           this month, monthly budget), Jev (estimated credit left, last 24 h of
                                           verdicts), DataForSEO (latest stored balance), Geoapify (credits today vs
                                           the daily limit); each with its provider-wide daily cap, and (AA-1025 C1)
                                           p50 / p95 latency and error rate over 24 h / 7 d; the CRM's AI and
                                           guardrail rows (crm.ops.v1 GET /llm/usage, the daily rollup, with the
                                           staff member's own token) and a static Mapbox row.                spend.read
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
                                           source / window, keyset paged; AA-1045: also a custom since / until
                                           and the usage explorer's tenant / model / account / role / error
                                           class / product (its drill-down).                                 spend.read

  GET /admin/ai-gateway/experiments        the Models & trials Experiments section: the prompt register
                                           (shared.prompt_version, AA-900) and the newest evaluation runs
                                           (shared.prompt_eval_runs).                                        spend.read
  GET /admin/ai-gateway/guide              (AA-1042, S270) the Guide tab: the prose source of the gateway (root
                                           docs/architecture/ai-gateway.md, catalog id ARCH-01-ai-gateway, and its
                                           Vietnamese copy, staff tier only, from the Docs sync bundle) plus the live
                                           tables: routes with the deployment that serves each stage first, the model x
                                           connection matrix, the connection register (secret names only), the
                                           connection caps and the stage coverage (api/services/ai_gateway_guide.py).
                                           This exposes that ONE staff-tier docs page to finance and ai_ops, who cannot
                                           open /admin/docs (ops.read).                                       spend.read
  GET /admin/ai-gateway/guide/asset?path=  (AA-1064) one image that guide page links (doc.assets: staff-tier SVG,
                                           PNG or WebP of the bundle), checked on read like /admin/docs/asset;
                                           &encoding=base64 answers JSON for the admin BFF. Other paths 404.  spend.read
  GET /admin/ai-gateway/access             (AA-1025 C1 follow-up) which of the page's writes the caller may use:
                                           each answer is staff_authz.denial_reason on that write's own route, so
                                           the page hides exactly what the API would refuse (a read-only session,
                                           such as the Dev E2E admin, sees no write button). UI hint only: every
                                           write keeps its own check.                                         spend.read
  GET    /admin/ai-gateway/tenant-budgets/{tenant_id}   one tenant's monthly AI budget and this month's spend
                                                        (Tenant 360 › Cost).                                 spend.read
  PUT    /admin/ai-gateway/tenant-budgets/{tenant_id}   create or replace it (audited tenant_ai_budget_set). spend.write
  DELETE /admin/ai-gateway/tenant-budgets/{tenant_id}   remove it: back to no budget (audited
                                                        tenant_ai_budget_cleared).                          spend.write

  GET    /admin/ai-gateway/credit                       AA-1025 B5: the credit ledger of every connection whose
                                                        balance_source is topups_minus_spend (the vendor APIs): the
                                                        entries, the estimate, the low-balance threshold.   spend.read
  POST   /admin/ai-gateway/credit/{connection_key}/entries   record a top-up or a balance read (audited
                                                        provider_credit.entry_added), then check that connection's
                                                        low-credit alert at once.                           spend.write
  DELETE /admin/ai-gateway/credit/entries/{entry_id}    delete one entry (audited provider_credit.entry_deleted).
                                                                                                             spend.write
  POST   /admin/ai-gateway/credit/check                 the low-credit check of every ledger connection now (the
                                                        sampler runs it every 30 min): bell + e-mail below the
                                                        threshold, throttled.                               spend.write

  GET    /admin/ai-gateway/app-profiles             AA-1025 H1a: the app profile of every app (AI on / off, bulk
                                                        paused, default daily budget, degrade mode) with the central
                                                        LLM cap llm/app:<app> next to it.                   spend.read
  PATCH  /admin/ai-gateway/app-profiles/{app}       edit one app's profile; switching AI off needs a reason (audited
                                                        llm_app_ai_switched_off / _on, llm_app_profile_updated).
                                                        AA-1025 H1c: for app crm the whole profile is first relayed to
                                                        the CRM (crm.ops.v1 PUT /llm/app-profile, the staff member's
                                                        own token, crm-platform-admin); a refusal there changes
                                                        nothing here.                                         ai.admin

  GET    /admin/ai-gateway/capacity                 AA-1025 H2: the TPM limits and prod guards
                                                        (shared.llm_capacity_limit), the claim hold now (prod guard
                                                        circuits, bulk pause, held lines and job kinds) and this
                                                        api task's TPM windows.                             spend.read
  PUT    /admin/ai-gateway/capacity/{connection_key}/{model_key}   create or replace one limit row (model_key * =
                                                        the whole connection; audited llm_capacity_limit_set).
                                                                                                              ai.admin
  DELETE /admin/ai-gateway/capacity/{connection_key}/{model_key}   remove it: no limit there any more (audited
                                                        llm_capacity_limit_cleared).                         ai.admin

$0: database reads (and one CRM read) and the tenant budget writes; no provider is called. The other caps are edited
through their existing write routes (PUT /admin/budgets/{provider}/{scope}, PATCH /admin/llm-connections/{key}).
AA-1016 (C4): the tenant budget writes answer 503 when migration 321 (the table) is not applied, 404 for an unknown
tenant; the budget is stored and shown only: enforcement is a later gateway slice.
Admin only (POLICY spend.read / spend.write; the page is in frontend/lib/rbac.ts PAGE_RULES and API_READ_RULES). The
sources and their limits: api/services/ai_gateway.py and api/services/ai_gateway_calls.py.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.core.staff_authz import StaffPrincipal, denial_reason, staff_policy
from api.core.tenant_scope import TENANT_SCOPED
from api.routers.admin import verify_admin_secret
from api.routers.admin_docs import asset_response
from api.services import (ai_gateway, ai_gateway_calls, ai_gateway_experiments, ai_gateway_guide, crm_console,
                          docs_reader, platform_apps, tenant_ai_budget)
from services.acp_shared.audit_log import write_audit_log
from shared import provider_credit
from shared.llm_client import app_profile, capacity

router = APIRouter(prefix="/admin/ai-gateway", tags=["admin-ai-gateway"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


@router.get("/providers", summary="Every paid AI and data provider: health, spend, budget, balance (read-only, $0)")
async def get_providers(request: Request) -> dict:
    authorization = platform_apps.relay_authorization(request)

    async def crm_usage_rows(start, end):
        # AA-1025 C1: the CRM rows (Partner CRM AI, Bedrock Guardrails) with the staff member's own WORKFORCE token.
        # Only the aggregate GET /llm/usage: the row ledger GET /llm/calls writes an ops_read event into every
        # agency's access log, so it stays on the Calls tab (a search someone runs), never on this page's refresh.
        return await platform_apps.crm_llm_usage_rows(authorization, start, end)

    return await ai_gateway.read_providers(request.app.state.pool, crm_usage_rows=crm_usage_rows)


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
    # AA-1045: the usage explorer's drill-down (a custom range and the explorer's dimensions)
    since: Optional[str] = Query(None, max_length=40),
    until: Optional[str] = Query(None, max_length=40),
    tenant: Optional[str] = Query(None, max_length=64),
    model: Optional[str] = Query(None, max_length=120),
    account: Optional[str] = Query(None, max_length=120),
    role: Optional[str] = Query(None, max_length=32),
    error_class: Optional[str] = Query(None, max_length=32),
    product: Optional[str] = Query(None, max_length=32),
) -> dict:
    try:
        filters = ai_gateway_calls.parse_filters(job_id=job_id, tour_id=tour_id, app=app, provider=provider,
                                                 status=status, stage=stage, source=source, window=window,
                                                 since=since, until=until, tenant=tenant, model=model,
                                                 account=account, role=role, error_class=error_class,
                                                 product=product)
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


# ── AA-1025 C1 follow-up: which of the page's writes the caller may use ───────────────────────────────────────────

# Each write control of the AI providers page and the route it calls (method, POLICY template). The answer for a
# control is denial_reason on that route, so it follows the API exactly: a role without the capability and a
# read-only session (the viewer role, the Dev E2E admin) both get False.
PAGE_WRITES: dict[str, tuple[str, str]] = {
    "provider_switch": ("PATCH", "/admin/llm-connections/{connection_key}"),   # Jev / DataForSEO / Geoapify switch
    "connections": ("PATCH", "/admin/llm-connections/{connection_key}"),       # Connections view: on/off, limits
    "connection_test": ("POST", "/admin/llm-connections/{connection_key}/test"),
    "requeue": ("POST", "/admin/job-runner/jobs/retry-llm-denied"),            # Re-queue refused jobs (dry run too)
    "routing": ("PATCH", "/admin/llm-config/{stage}"),
    "catalog": ("PATCH", "/admin/llm-catalog/{model_key}"),
    "budgets": ("PUT", "/admin/budgets/{provider}/{scope}"),
    "jev_credit": ("POST", "/admin/decisions/jev-credit/topups"),
    # AA-1025 B5 Credit view: add or delete an entry, Check now (all spend.write); its threshold save is `connections`
    "credit": ("POST", "/admin/ai-gateway/credit/{connection_key}/entries"),
    # AA-1025 H1a App profiles card: the AI switch, bulk pause, default budget and degrade mode of each app (ai.admin)
    "app_profiles": ("PATCH", "/admin/ai-gateway/app-profiles/{app}"),
    # AA-1042 Route control tab: save, presets, revert (ai.admin; the preview needs the same capability)
    "route_control": ("POST", "/admin/ai-gateway/route-control/save"),
    # AA-1042 Trace tab: the route alert policy / hold switch, the route test and the manual watch run (ai.admin)
    "route_policy": ("PUT", "/admin/ai-gateway/route-policy/{stage}"),
    "route_test": ("POST", "/admin/ai-gateway/route-trace/test"),
}


def page_access(principal: StaffPrincipal) -> dict:
    """{read_only, actions: {control: allowed}} for one caller (pure; tests/unit/test_aa1025_providers_access.py)."""
    actions = {key: denial_reason(principal, method, route) is None for key, (method, route) in PAGE_WRITES.items()}
    return {"read_only": not any(actions.values()), "actions": actions}


@router.get("/access", summary="Which write controls of the AI providers page the caller may use (UI hint, $0)")
async def get_access(principal: StaffPrincipal = Depends(staff_policy)) -> dict:
    return page_access(principal)


# AA-1042: the docs reader route whose POLICY decides whether the Guide's links may open /admin/docs for this caller.
DOCS_READER_ROUTE = ("GET", "/admin/docs/page")


@router.get("/guide", summary="The AI gateway guide: the prose page from the docs catalog plus the live route, "
                              "matrix, register, caps and coverage tables (read-only, $0)")
async def get_guide(request: Request, principal: StaffPrincipal = Depends(staff_policy)) -> dict:
    reader = denial_reason(principal, *DOCS_READER_ROUTE) is None
    return await ai_gateway_guide.read_guide(request.app.state.pool, reader=reader)


@router.get("/guide/asset", summary="One image the AI gateway guide page links (SVG, PNG or WebP; read-only, $0)")
async def get_guide_asset(
    path: str = Query(..., min_length=5, max_length=300,
                      description="repo-relative path of an image the guide page links (GET /guide doc.assets)"),
    encoding: Literal["raw", "base64"] = Query(
        "raw", description="raw: the image bytes; base64: JSON {repo, path, type, bytes, sha256, data} (the admin BFF "
                           "reads this form: the API Gateway in front is not binary safe)"),
    principal: StaffPrincipal = Depends(staff_policy),
):
    # AA-1064: spend.read like the tab; only an image the guide page itself links (never the rest of the docs bundle,
    # which stays ops.read under /admin/docs/asset).
    try:
        data = await ai_gateway_guide.read_asset(path)
    except docs_reader.DocsMissing as e:
        raise HTTPException(status_code=404, detail=str(e))
    except docs_reader.DocsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return asset_response(ai_gateway_guide.GUIDE_REPO, path, data, encoding)


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


@router.get("/tenant-budgets/{tenant_id}", summary="One tenant's monthly AI budget and this month's spend ($0)",
            dependencies=TENANT_SCOPED)
async def get_tenant_budget(request: Request, tenant_id: str = _TENANT_ID) -> dict:
    out = await tenant_ai_budget.read_one(request.app.state.pool, tenant_id.lower())
    if out is None:
        raise HTTPException(status_code=404, detail="no such tenant")
    return out


@router.put("/tenant-budgets/{tenant_id}", summary="Set one tenant's monthly AI budget (audited)",
            dependencies=TENANT_SCOPED)
async def put_tenant_budget(body: TenantBudgetBody, request: Request, tenant_id: str = _TENANT_ID,
                            x_admin_user_id: Optional[str] = Header(None)) -> dict:
    try:
        return await tenant_ai_budget.upsert(request.app.state.pool, tenant_id.lower(), body.model_dump(),
                                             actor=_actor(x_admin_user_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="no such tenant")
    except tenant_ai_budget.TenantBudgetUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.delete("/tenant-budgets/{tenant_id}", summary="Remove one tenant's monthly AI budget (audited)",
               dependencies=TENANT_SCOPED)
async def delete_tenant_budget(request: Request, tenant_id: str = _TENANT_ID,
                               x_admin_user_id: Optional[str] = Header(None)) -> dict:
    try:
        return await tenant_ai_budget.clear(request.app.state.pool, tenant_id.lower(), actor=_actor(x_admin_user_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="no such tenant")
    except tenant_ai_budget.TenantBudgetUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── AA-1025 B5: the credit ledger of the vendor API connections (top-ups and balance reads) ────────────────────────

_CONNECTION_KEY = Path(..., pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
_LEDGER_MISSING = "the credit ledger needs migration 342"


class CreditEntryBody(BaseModel):
    """One ledger entry. kind 'topup' = money added (amount above 0); 'opening_balance' = the balance read on the
    provider's console (amount 0 or more): the estimate restarts from the newest one. effective_at defaults to now."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["topup", "opening_balance"] = "topup"
    amount_usd: float = Field(..., ge=0, le=100_000)
    effective_at: Optional[datetime] = None
    note: Optional[str] = Field(None, max_length=500)

    @model_validator(mode="after")
    def _amount(self):
        if self.kind == "topup" and self.amount_usd <= 0:
            raise ValueError("a top-up must be above 0")
        self.amount_usd = round(self.amount_usd, 2)
        if self.note is not None:
            self.note = self.note.strip() or None
        return self


@router.get("/credit", summary="The credit ledger and estimated balance of the vendor API connections ($0)")
async def get_credit(request: Request) -> dict:
    try:
        async with request.app.state.pool.acquire() as conn:
            return {"connections": await provider_credit.read_all(conn)}
    except provider_credit.LedgerUnavailable:
        raise HTTPException(status_code=503, detail=_LEDGER_MISSING)


@router.post("/credit/check", summary="Run the vendor API low-credit check now (bell + e-mail below the threshold)")
async def post_credit_check(request: Request) -> dict:
    try:
        results = await provider_credit.check_low_credit(request.app.state.pool, source="admin_check")
    except provider_credit.LedgerUnavailable:
        raise HTTPException(status_code=503, detail=_LEDGER_MISSING)
    return {"results": results}


@router.post("/credit/{connection_key}/entries", summary="Record a top-up or a balance read of a vendor API "
                                                         "connection (audited)")
async def post_credit_entry(body: CreditEntryBody, request: Request, connection_key: str = _CONNECTION_KEY,
                            x_admin_user_id: Optional[str] = Header(None)) -> dict:
    actor = _actor(x_admin_user_id)
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                entry = await provider_credit.add_entry(
                    conn, connection_key, kind=body.kind, amount_usd=body.amount_usd, effective_at=body.effective_at,
                    note=body.note, actor=actor)
                await write_audit_log(
                    conn, tenant_id=provider_credit.PLATFORM_TENANT_ID, actor=actor,
                    action="provider_credit.entry_added", resource_type="provider_credit_topup",
                    resource_id=str(entry["id"]),
                    details={"connection_key": connection_key, "kind": body.kind, "amount_usd": body.amount_usd,
                             "effective_at": entry["effective_at"], "note": body.note})
        check = await provider_credit.check_low_credit(pool, source="entry_added", connection_key=connection_key)
    except LookupError:
        raise HTTPException(status_code=404, detail="no connection with a credit ledger by that key")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except provider_credit.LedgerUnavailable:
        raise HTTPException(status_code=503, detail=_LEDGER_MISSING)
    return {"entry": entry, "check": check[0] if check else None}


@router.delete("/credit/entries/{entry_id}", summary="Delete one credit ledger entry (audited)")
async def delete_credit_entry(request: Request, entry_id: int = Path(..., ge=1),
                              x_admin_user_id: Optional[str] = Header(None)) -> dict:
    actor = _actor(x_admin_user_id)
    try:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                gone = await provider_credit.delete_entry(conn, entry_id)
                if gone is None:
                    raise HTTPException(status_code=404, detail="no such entry")
                await write_audit_log(
                    conn, tenant_id=provider_credit.PLATFORM_TENANT_ID, actor=actor,
                    action="provider_credit.entry_deleted", resource_type="provider_credit_topup",
                    resource_id=str(entry_id), details=gone)
    except provider_credit.LedgerUnavailable:
        raise HTTPException(status_code=503, detail=_LEDGER_MISSING)
    return {"deleted": True, "entry": gone}


# ── AA-1025 H1a: the app profile of each app (root ADR 0009 decision 4) ─────────────────────────────────────────────

_APP = Path(..., pattern=r"^[a-z][a-z0-9_]{0,30}$")
_PROFILE_MISSING = "the app profile needs migration 345"
_CENTRAL_APP_CAPS_SQL = """
    SELECT to_jsonb(b) AS row
      FROM shared.spend_budget b
     WHERE b.provider = 'llm' AND b.scope LIKE 'app:%'
"""


class AppProfilePatch(BaseModel):
    """The fields to change (the others are kept). ``default_daily_budget_usd`` null clears it; switching AI off needs
    ``reason`` (3 to 500 characters), switching it on clears the stored reason. The limits are migration 345's
    CHECKs."""
    model_config = ConfigDict(extra="forbid")
    ai_enabled: Optional[bool] = None
    bulk_paused: Optional[bool] = None
    default_daily_budget_usd: Optional[float] = Field(None, ge=0, le=app_profile.BUDGET_MAX_USD)
    degrade_mode: Optional[Literal["queue_retry", "basic_narration", "last_saved_text", "manual_reply_needed",
                                   "none"]] = None
    reason: Optional[str] = Field(None, max_length=app_profile.REASON_MAX)

    @model_validator(mode="after")
    def _no_null_switches(self):
        for name in ("ai_enabled", "bulk_paused", "degrade_mode"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        if not (self.model_fields_set - {"reason"}):
            raise ValueError("no field to change")
        return self

    def fields(self) -> dict:
        """The profile fields of this PATCH (``reason`` becomes ``disabled_reason``)."""
        out = {k: getattr(self, k) for k in self.model_fields_set if k != "reason"}
        if "reason" in self.model_fields_set:
            out["disabled_reason"] = self.reason
        return out


def central_app_caps(rows: list) -> dict:
    """{app: {per_day_usd, per_month_usd, hard_stop}} of the central LLM caps llm/app:<app> (A7), from rows of
    ``to_jsonb`` text or dicts under ``row``. Pure."""
    out = {}
    for r in rows:
        raw = r["row"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        scope = str(raw.get("scope") or "")
        if not scope.startswith("app:"):
            continue

        def num(key, raw=raw):
            v = raw.get(key)
            return None if v is None else round(float(v), 4)

        out[scope[4:]] = {"per_day_usd": num("per_day_usd"), "per_month_usd": num("per_month_usd"),
                          "hard_stop": bool(raw.get("hard_stop", False))}
    return out


@router.get("/app-profiles", summary="The app profile of every app: AI on / off, bulk paused, default budget, "
                                     "degrade mode ($0)")
async def get_app_profiles(request: Request) -> dict:
    try:
        async with request.app.state.pool.acquire() as conn:
            profiles = await app_profile.read_all(conn)
            caps = central_app_caps(await conn.fetch(_CENTRAL_APP_CAPS_SQL))
    except app_profile.AppProfileUnavailable:
        raise HTTPException(status_code=503, detail=_PROFILE_MISSING)
    rows = []
    for app in app_profile.APPS:
        p = profiles.get(app) or app_profile.AppProfile(app=app)
        rows.append({**p.as_dict(), "central_cap": caps.get(app),
                     "enforced_here": app in ENFORCED_APPS})
    return {"profiles": rows, "degrade_modes": list(app_profile.DEGRADE_MODES)}


# The apps whose gateway acts on their profile: CIS (H1a), TripPlanner (H1b: catalog.v_llm_app_profile_v1, CIS 347,
# within its 20 s cache), the CRM (H1c: relayed at once to crm.llm_app_profile, crm-041). AA-Booking has no AI.
ENFORCED_APPS = frozenset({"cis", "tripplanner", "crm"})
CRM_PROFILE_PATH = "/llm/app-profile"


def crm_profile_body(p: app_profile.AppProfile) -> dict:
    """The crm.ops.v1 1.10 PUT body of a CIS profile (the whole profile; the budget as a 4-decimal string). Pure."""
    budget = p.default_daily_budget_usd
    return {"ai_enabled": p.ai_enabled, "bulk_paused": p.bulk_paused,
            "default_daily_budget_usd": None if budget is None else f"{budget:.4f}",
            "degrade_mode": p.degrade_mode, "disabled_reason": p.disabled_reason}


async def _relay_crm_profile(request: Request, fields: dict) -> None:
    """AA-1025 H1c: the CRM's copy first (root ADR 0008a §6: the CRM reads no shared.llm_* table). The CIS row after the
    edit is relayed whole; anything but 200 is raised with the CRM's code, so the CIS row is not changed."""
    try:
        async with request.app.state.pool.acquire() as conn:
            before = (await app_profile.read_all(conn)).get("crm") or app_profile.AppProfile(app="crm")
    except app_profile.AppProfileUnavailable:
        raise HTTPException(status_code=503, detail=_PROFILE_MISSING)
    try:
        after = app_profile.merged(before, fields)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    status, body = await crm_console.crm_ops_call("PUT", CRM_PROFILE_PATH, platform_apps.relay_authorization(request),
                                                  json=crm_profile_body(after))
    if status != 200:
        detail = body.get("detail") if isinstance(body.get("detail"), str) else "crm_app_profile_refused"
        raise HTTPException(status_code=status if status in (401, 403, 422, 503) else 502,
                            detail=f"the partner CRM did not take the profile: {detail}")


@router.patch("/app-profiles/{app}", summary="Edit one app's profile: AI on / off, bulk paused, default budget, "
                                            "degrade mode (audited)")
async def patch_app_profile(body: AppProfilePatch, request: Request, app: str = _APP,
                            x_admin_user_id: Optional[str] = Header(None)) -> dict:
    if app == "crm":
        await _relay_crm_profile(request, body.fields())
    try:
        return await app_profile.update(request.app.state.pool, app, body.fields(), actor=_actor(x_admin_user_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="no such app")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except app_profile.AppProfileUnavailable:
        raise HTTPException(status_code=503, detail=_PROFILE_MISSING)


# ── AA-1025 H2: capacity limits (TPM per connection / model, line shares, prod guards) ──────────────────────────────

_CONNECTION_KEY = Path(..., pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
_MODEL_KEY = Path(..., pattern=r"^(\*|[a-z0-9][a-z0-9._-]{0,99})$")
_CAPACITY_MISSING = "the capacity limits need migration 348"


class CapacityLimitBody(BaseModel):
    """The whole limit row (a PUT replaces it). tpm_limit null = no token limit at this level (then prod_guard must be
    true); bulk_pct / dev_pct = the share of tpm_limit the bulk / dev line may hold. The limits are migration 348's
    CHECKs."""
    model_config = ConfigDict(extra="forbid")
    tpm_limit: Optional[int] = Field(None, ge=1, le=capacity.TPM_MAX)
    bulk_pct: int = Field(50, ge=1, le=100)
    dev_pct: Optional[int] = Field(None, ge=1, le=100)
    prod_guard: bool = False
    notes: Optional[str] = Field(None, max_length=capacity.NOTES_MAX)


def _kinds_registered() -> list[str]:
    from shared.jobs.registry import kinds
    from shared.jobs.worker import load_kinds
    load_kinds()
    return sorted(kinds())


@router.get("/capacity", summary="TPM limits, line shares and prod guards; the job claim hold now ($0)")
async def get_capacity(request: Request) -> dict:
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            rows = await capacity.read_all(conn)
    except capacity.CapacityUnavailable:
        raise HTTPException(status_code=503, detail=_CAPACITY_MISSING)
    capacity.invalidate()
    hold = await capacity.claim_hold(pool)
    registered = _kinds_registered()
    return {
        "limits": [r.as_dict() for r in rows],
        "hold": hold.as_dict(),
        "held_kinds": sorted(capacity.held_kinds(hold.lines(), registered)),
        "kind_lines": {k: capacity.line_of_kind(k) for k in registered},
        "wait_s": dict(capacity.WAIT_S),
        "window_s": capacity.WINDOW_S,
        # The limiter is per process: these are this api task's windows; each worker task keeps its own.
        "windows_this_task": capacity.windows_snapshot(),
    }


@router.put("/capacity/{connection_key}/{model_key}", summary="Set one TPM limit / prod guard (audited)")
async def put_capacity(body: CapacityLimitBody, request: Request, connection_key: str = _CONNECTION_KEY,
                       model_key: str = _MODEL_KEY, x_admin_user_id: Optional[str] = Header(None)) -> dict:
    try:
        return await capacity.put(request.app.state.pool, connection_key, model_key, body.model_dump(),
                                  actor=_actor(x_admin_user_id))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except capacity.CapacityUnavailable:
        raise HTTPException(status_code=503, detail=_CAPACITY_MISSING)


@router.delete("/capacity/{connection_key}/{model_key}", summary="Remove one TPM limit / prod guard (audited)")
async def delete_capacity(request: Request, connection_key: str = _CONNECTION_KEY, model_key: str = _MODEL_KEY,
                          x_admin_user_id: Optional[str] = Header(None)) -> dict:
    try:
        gone = await capacity.delete(request.app.state.pool, connection_key, model_key,
                                     actor=_actor(x_admin_user_id))
    except capacity.CapacityUnavailable:
        raise HTTPException(status_code=503, detail=_CAPACITY_MISSING)
    if not gone:
        raise HTTPException(status_code=404, detail="no such limit")
    return {"deleted": True, "connection_key": connection_key, "model_key": model_key}
