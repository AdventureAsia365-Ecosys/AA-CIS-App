"""AA-1045 — the read API of the admin "Usage explorer" page (AI & spend › Usage explorer, /admin/usage).

  GET    /admin/usage-explorer/query            the explorer: a window (presets or custom ICT days), a granularity,
                                                one or two dimensions, filters on every dimension, the usage metrics
                                                per group, the time series and the totals (api/services/
                                                usage_explorer.py), from the hourly rollup plus the CRM's daily
                                                totals (crm.ops.v1 GET /llm/usage, the staff member's own token).
                                                                                                    spend.read
  GET    /admin/usage-explorer/options          the values each dimension takes in the window (filter lists), tenant
                                                names.                                              spend.read
  GET    /admin/usage-explorer/invoices         one UTC month per provider and account: the ledger total next to the
                                                provider's stored figure (nightly reconciliation) and the invoice
                                                (none recorded yet).                                spend.read
  GET    /admin/usage-explorer/views            the caller's saved views.                           spend.read
  POST   /admin/usage-explorer/views            save (create or replace by name) one of the caller's views.
                                                                                                    spend.read
  DELETE /admin/usage-explorer/views/{view_id}  delete one of the caller's views.                   spend.read
  GET    /admin/usage-explorer/rollup/check     the rollup's totals next to the ledgers' for whole hours (default: the
                                                7 days before the last rollup run started): ``ok`` when equal.
                                                                                                    spend.read
  POST   /admin/usage-explorer/rollup/run       queue a re-roll of [from, to) (job ai_usage_rollup, $0; audited
                                                usage_rollup.run_requested).                        ops.write

$0: database reads and writes, one CRM read. A saved view is the caller's own (the verified staff id): the writes need
only spend.read, like the notification bell's own state; a read-only role is refused on them anyway (READ_ONLY_ROLES).
Admin, finance and ai_ops (spend.read; frontend/lib/rbac.ts PAGE_RULES /admin/usage and API_READ_RULES
usage-explorer); never a tenant. Before CIS 372 is applied every route answers 503.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import staff_policy
from api.core.tenant_scope import staff_id_of
from api.routers.admin import verify_admin_secret
from api.services import platform_apps, usage_explorer, usage_rollup
from services.acp_shared.audit_log import write_audit_log
from shared import provider_credit

router = APIRouter(prefix="/admin/usage-explorer", tags=["admin-usage-explorer"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

_MISSING = "the usage rollup needs migration 372"
_VIEW_ID = Path(..., pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _query(request: Request) -> usage_explorer.ExplorerQuery:
    try:
        return usage_explorer.parse_query(dict(request.query_params))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/query", summary="Usage of every paid AI and data provider by one or two dimensions ($0)")
async def get_query(request: Request) -> dict:
    q = _query(request)
    authorization = platform_apps.relay_authorization(request)

    async def crm_usage_rows(start, end):
        # The CRM's daily totals (crm.ops.v1 GET /llm/usage: no agency, no personal data) with the caller's token.
        return await platform_apps.crm_llm_usage_rows(authorization, start, end)

    try:
        return await usage_explorer.explore(request.app.state.pool, q, crm_usage_rows=crm_usage_rows)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except usage_rollup.RollupUnavailable:
        raise HTTPException(status_code=503, detail=_MISSING)


@router.get("/options", summary="The values of every explorer dimension in the window ($0)")
async def get_options(request: Request) -> dict:
    q = _query(request)
    try:
        return await usage_explorer.options(request.app.state.pool, q)
    except usage_rollup.RollupUnavailable:
        raise HTTPException(status_code=503, detail=_MISSING)


@router.get("/invoices", summary="Ledger month totals per provider and account next to the provider figures ($0)")
async def get_invoices(request: Request, month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$")) -> dict:
    try:
        return await usage_explorer.invoices(request.app.state.pool, month)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except usage_rollup.RollupUnavailable:
        raise HTTPException(status_code=503, detail=_MISSING)


# ── saved views (the caller's own) ──────────────────────────────────────────────────────────────────────────────────

def _staff(request: Request) -> str:
    staff_id = staff_id_of(request)
    if staff_id is None:
        raise HTTPException(status_code=403, detail="saved views need a signed-in staff member")
    return staff_id


class UsageViewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=80)
    query: dict = Field(default_factory=dict)


@router.get("/views", summary="The caller's saved explorer views ($0)")
async def get_views(request: Request) -> dict:
    staff_id = _staff(request)
    try:
        return {"views": await usage_explorer.list_views(request.app.state.pool, staff_id),
                "limit": usage_explorer.VIEW_LIMIT}
    except usage_explorer.ViewsUnavailable:
        raise HTTPException(status_code=503, detail=_MISSING)


@router.post("/views", summary="Save one of the caller's explorer views (create or replace by name)")
async def post_view(body: UsageViewBody, request: Request) -> dict:
    staff_id = _staff(request)
    try:
        name, query = usage_explorer.check_view(body.name, body.query)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        return await usage_explorer.save_view(request.app.state.pool, staff_id, name, query)
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except usage_explorer.ViewsUnavailable:
        raise HTTPException(status_code=503, detail=_MISSING)


@router.delete("/views/{view_id}", summary="Delete one of the caller's explorer views")
async def delete_view(request: Request, view_id: str = _VIEW_ID) -> dict:
    staff_id = _staff(request)
    try:
        gone = await usage_explorer.delete_view(request.app.state.pool, staff_id, view_id.lower())
    except usage_explorer.ViewsUnavailable:
        raise HTTPException(status_code=503, detail=_MISSING)
    if not gone:
        raise HTTPException(status_code=404, detail="no such view")
    return {"deleted": True}


# ── rollup ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def _instant(value: Optional[str], name: str) -> Optional[datetime]:
    if value is None or value.strip() == "":
        return None
    try:
        t = datetime.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be an ISO date-time")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


@router.get("/rollup/check", summary="The hourly rollup's totals next to the ledgers' for whole hours ($0)")
async def get_rollup_check(request: Request, since: Optional[str] = Query(None, alias="from", max_length=40),
                           until: Optional[str] = Query(None, alias="to", max_length=40)) -> dict:
    pool = request.app.state.pool
    now = datetime.now(timezone.utc)
    last = await usage_rollup.last_run(pool)
    a, b = _instant(since, "from"), _instant(until, "to")
    if b is None:
        # the hours the last run had fully seen: before the hour it started in
        started = datetime.fromisoformat(last["started_at"]) if last and last.get("started_at") else now
        b = usage_rollup.floor_hour(min(started, now))
    if a is None:
        a = b - timedelta(days=7)
    try:
        a, b = usage_rollup.clamp_range(a, b, now=now)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        out = await usage_rollup.check_totals(pool, a, b)
    except usage_rollup.RollupUnavailable:
        raise HTTPException(status_code=503, detail=_MISSING)
    return {**out, "rollup": last}


class UsageRollupRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: datetime = Field(..., alias="from")
    until: datetime = Field(..., alias="to")


@router.post("/rollup/run", summary="Queue a re-roll of the hourly usage rollup for [from, to) ($0, audited)")
async def post_rollup_run(body: UsageRollupRunBody, request: Request,
                          x_admin_user_id: Optional[str] = Header(None)) -> dict:
    from services.jobs import ai_usage_rollup_job as job

    since = body.since if body.since.tzinfo else body.since.replace(tzinfo=timezone.utc)
    until = body.until if body.until.tzinfo else body.until.replace(tzinfo=timezone.utc)
    try:
        a, b = usage_rollup.clamp_range(since, until)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    actor = f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"
    pool = request.app.state.pool
    job_id, created = await job.enqueue_manual(pool, since=a, until=b, actor=actor)
    async with pool.acquire() as conn:
        await write_audit_log(conn, tenant_id=provider_credit.PLATFORM_TENANT_ID, actor=actor,
                              action="usage_rollup.run_requested", resource_type="job", resource_id=str(job_id),
                              details={"from": a.isoformat(), "to": b.isoformat()})
    return {"job_id": job_id, "created": created, "from": a.isoformat(), "to": b.isoformat()}
