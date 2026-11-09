"""AA-1042 (S268, AI gateway route control center: Route trace, Alerts, Quality guard) — the API of AI gateway > Trace.

  GET    /admin/ai-gateway/route-trace/requests            the newest requests of a window, one line per gateway
                                                           request (route trace): attempts, who answered, cost,
                                                           latency; ?stage= ?window= ?fallback_only=          spend.read
  GET    /admin/ai-gateway/route-trace/requests/{trace_id} one request: every attempt in order (outcome, error class
                                                           and text, latency, cost); ?at= the time of one of its rows
                                                           (the Calls row), else the last 48 h                spend.read
  GET    /admin/ai-gateway/route-trace/rungs               per stage over a window: the share of answered requests
                                                           per rung (model @ connection, primary / fallback), the
                                                           fallback share, requests with no answer            spend.read
  GET    /admin/ai-gateway/route-trace/fallback-content    the newest A1 tour versions a fallback model wrote,
                                                           marked or held (catalog, no tenant data)           spend.read
  GET    /admin/content-quality/fallback-writes            the same for tenant content: T2 tour versions and T9
                                                           pieces, with the tenant and the admin-only held / marked
                                                           label. Tenant QA data, so it sits with the Tenant QA reads
                                                           (quality_router), not under /admin/ai-gateway
                                                                                                      tenant_content.read
  POST   /admin/ai-gateway/route-trace/test                the live check: 1-6 tiny real requests on one chat stage,
                                                           optionally with its first model refused for those
                                                           requests only (no stored switch; audited)            ai.admin
  GET    /admin/ai-gateway/route-policy                    the route alert thresholds and the hold switch per stage
                                                           (own row, the * row, the effective values)         spend.read
  PUT    /admin/ai-gateway/route-policy/{stage}            create or replace one row (* = the defaults; audited
                                                           llm_route_policy_set)                                ai.admin
  DELETE /admin/ai-gateway/route-policy/{stage}            remove it (audited llm_route_policy_cleared)        ai.admin
  POST   /admin/ai-gateway/route-watch/run                 enqueue the llm_route_watch job now (mode watch | digest,
                                                           dry_run): the alerts without waiting for the 15-min tick
                                                                                                                ai.admin

$0 except the route test (16 output tokens per request). The policy writes answer 503 before the llm_route_policy
migration.
Admin only (POLICY in api/core/staff_authz.py; the page in frontend/lib/rbac.ts PAGE_RULES / API_READ_RULES under
/admin/ai-gateway). Rules: api/services/llm_route_trace.py, shared/llm_client/route_watch.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import llm_route_trace as svc
from services.acp_shared.audit_log import write_audit_log
from services.quality import edit_gate
from shared.llm_client import route_evidence

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/ai-gateway", tags=["admin-ai-gateway"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])
# The tenant content list: every tenant's T2 versions / T9 pieces and their admin-only QA mark, gated like the other
# Tenant QA reads (/admin/content-quality/*: tenant_content.read). The AI & spend roles never reach it.
quality_router = APIRouter(prefix="/admin/content-quality", tags=["admin-content-quality"],
                           dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

_STAGE = Path(..., pattern=r"^(\*|[a-z0-9][a-z0-9_.-]{0,63})$")
_TRACE_ID = Path(..., pattern=r"^[0-9a-fA-F-]{32,36}$")
_WINDOW = Query(None, pattern=r"^(1h|24h|7d)$")


def _actor(x_admin_user_id: Optional[str]) -> str:
    return f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"


def _stage_or_422(stage: Optional[str]) -> Optional[str]:
    try:
        return svc.parse_stage(stage)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/route-trace/requests", summary="The newest gateway requests, one line per request (read-only, $0)")
async def get_route_requests(request: Request, stage: Optional[str] = Query(None, max_length=64),
                             window: Optional[str] = _WINDOW, fallback_only: bool = Query(False),
                             limit: int = Query(svc.REQUESTS_LIMIT, ge=1, le=svc.REQUESTS_LIMIT)) -> dict:
    return await svc.read_requests(request.app.state.pool, stage=_stage_or_422(stage), window=window,
                                   fallback_only=fallback_only, limit=limit)


@router.get("/route-trace/requests/{trace_id}", summary="Every attempt of one gateway request (read-only, $0)")
async def get_route_trace(request: Request, trace_id: str = _TRACE_ID,
                          at: Optional[datetime] = Query(None, description="the time of one row of the request")
                          ) -> dict:
    try:
        tid = svc.parse_trace_id(trace_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    out = await svc.read_trace(request.app.state.pool, tid, at=at)
    if out is None:
        raise HTTPException(status_code=404, detail="no request with that trace id in the time window")
    return out


@router.get("/route-trace/rungs", summary="Per stage, the share of requests answered by each rung (read-only, $0)")
async def get_route_rungs(request: Request, window: Optional[str] = _WINDOW,
                          stage: Optional[str] = Query(None, max_length=64)) -> dict:
    return await svc.read_rungs(request.app.state.pool, window=window, stage=_stage_or_422(stage))


@router.get("/route-trace/fallback-content",
            summary="The newest A1 tour versions a fallback model wrote (read-only, $0)")
async def get_fallback_content(request: Request, window: Optional[str] = _WINDOW) -> dict:
    return await svc.read_fallback_content(request.app.state.pool, window=window)


@quality_router.get("/fallback-writes",
                    summary="The newest tenant tour versions and pieces a fallback model wrote (read-only, $0)")
async def get_fallback_tenant_content(request: Request, window: Optional[str] = _WINDOW) -> dict:
    return await svc.read_fallback_tenant_content(request.app.state.pool, window=window)


class RouteTestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    refuse_primary: bool = False
    count: int = Field(1, ge=1, le=svc.TEST_MAX_REQUESTS)


@router.post("/route-trace/test", summary="Run 1-6 tiny real requests on one stage, optionally with its first "
                                          "model refused for those requests only (audited, about 0.0001 USD each)")
async def post_route_test(body: RouteTestBody, request: Request,
                          x_admin_user_id: Optional[str] = Header(None)) -> dict:
    actor = _actor(x_admin_user_id)
    pool = request.app.state.pool
    try:
        out = await svc.run_route_test(pool, stage=body.stage, refuse_primary=body.refuse_primary, count=body.count,
                                       actor=actor)
    except svc.RouteTestRefused as e:
        raise HTTPException(status_code=422, detail=str(e))
    await write_audit_log(pool, tenant_id=svc.PLATFORM_TENANT_ID, actor=actor, action="llm_route_test_run",
                          resource_type="llm_stage", resource_id=body.stage,
                          details={"refuse_primary": body.refuse_primary, "count": body.count,
                                   "ok": sum(1 for r in out["requests"] if r["ok"]), "cost_usd": out["cost_usd"]})
    return out


@router.get("/route-policy", summary="The route alert thresholds and the fallback hold switch per stage ($0)")
async def get_route_policy(request: Request) -> dict:
    return await svc.read_policy(request.app.state.pool)


class RoutePolicyBody(BaseModel):
    """One policy row; null = inherit (the * row, then the code default)."""
    model_config = ConfigDict(extra="forbid")
    fallback_alert_pct: Optional[int] = Field(None, ge=1, le=100)
    min_requests: Optional[int] = Field(None, ge=1, le=10000)
    alert_last_rung: Optional[bool] = None
    cost_shift_usd: Optional[float] = Field(None, ge=0.01, le=10000)
    hold_fallback_writes: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=500)
    # AA-1052 W6: what backs a hold switch change of a gated stage (or of the * defaults)
    eval_run_id: Optional[str] = Field(None, max_length=64)
    break_glass_reason: Optional[str] = Field(None, max_length=route_evidence.BREAK_GLASS_MAX_CHARS)


_EVIDENCE_FIELDS = {"eval_run_id", "break_glass_reason"}


async def _policy_gate(pool, stage: str, hold_after: Optional[bool], *, clearing: bool,
                       eval_run_id: Optional[str], break_glass_reason: Optional[str]) -> Optional[dict]:
    """AA-1052 W6 (services/quality/edit_gate.py): a change of the hold switch (what a writer stores when a fallback
    model wrote it) of a gated stage or of the * defaults is frozen during a wave (409) and needs an eval run or a
    break-glass reason (422). An alert-threshold-only edit changes no output and passes. The evidence, or None."""
    if not edit_gate.is_gated_stage(stage):
        return None
    exists, hold_before = await svc.read_hold(pool, stage)
    if clearing:
        if not exists:
            return None          # the 404 follows in clear_policy
        hold_after = None
    if hold_before == hold_after:
        return None
    try:
        await edit_gate.refuse_if_frozen(f"the route policy of {stage}", [stage], pool=pool)
        return await edit_gate.require_evidence(
            stage, None, eval_run_id=eval_run_id, break_glass_reason=break_glass_reason,
            what=f"hold switch change of the route policy {stage}")
    except edit_gate.EditFrozen as e:
        raise HTTPException(status_code=409, detail=str(e))
    except route_evidence.EvidenceRefused as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # noqa: BLE001 — the wave / eval run table could not be read: refuse, never wave through
        logger.warning("route_policy_gate_unreadable", stage=stage, error=str(e)[:200])
        raise HTTPException(status_code=503, detail="the edit gate could not be checked; try again")


@router.put("/route-policy/{stage}", summary="Set the route alert thresholds / hold switch of one stage (audited)")
async def put_route_policy(body: RoutePolicyBody, request: Request, stage: str = _STAGE,
                           x_admin_user_id: Optional[str] = Header(None)) -> dict:
    pool = request.app.state.pool
    fields = body.model_dump(exclude=_EVIDENCE_FIELDS)
    try:
        evidence = await _policy_gate(pool, stage, fields.get("hold_fallback_writes"), clearing=False,
                                      eval_run_id=body.eval_run_id, break_glass_reason=body.break_glass_reason)
        return await svc.write_policy(pool, stage, fields, actor=_actor(x_admin_user_id), evidence=evidence)
    except svc.PolicyUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/route-policy/{stage}", summary="Remove one stage's route policy row (audited)")
async def delete_route_policy(request: Request, stage: str = _STAGE,
                              x_admin_user_id: Optional[str] = Header(None),
                              eval_run_id: Optional[str] = Query(None, max_length=64),
                              break_glass_reason: Optional[str] = Query(
                                  None, max_length=route_evidence.BREAK_GLASS_MAX_CHARS)) -> dict:
    pool = request.app.state.pool
    try:
        evidence = await _policy_gate(pool, stage, None, clearing=True, eval_run_id=eval_run_id,
                                      break_glass_reason=break_glass_reason)
        return await svc.clear_policy(pool, stage, actor=_actor(x_admin_user_id), evidence=evidence)
    except svc.PolicyUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LookupError:
        raise HTTPException(status_code=404, detail="no policy row for that stage")


class RouteWatchRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["watch", "digest"] = "watch"
    dry_run: bool = False


@router.post("/route-watch/run", summary="Enqueue the route watch (alerts) or the route digest now ($0)")
async def post_route_watch_run(body: RouteWatchRunBody, request: Request,
                               x_admin_user_id: Optional[str] = Header(None)) -> dict:
    from services.jobs import llm_route_watch_job

    actor = _actor(x_admin_user_id)
    job_id, _ = await llm_route_watch_job.enqueue_manual(request.app.state.pool, mode=body.mode,
                                                         dry_run=body.dry_run, actor=actor)
    logger.info("admin_llm_route_watch_run", job_id=job_id, mode=body.mode, dry_run=body.dry_run, actor=actor)
    return {"job_id": job_id, "kind": llm_route_watch_job.KIND, "mode": body.mode, "dry_run": body.dry_run,
            "switch_on": llm_route_watch_job.schedule_enabled()}
