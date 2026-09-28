"""AA-646 — admin-triggered search-demand research (the only path that buys DataForSEO demand data).

Before AA-646 `run_segment_research()` fired after every tenant T2 rewrite and, because AA-545 made
Segments platform-wide, swept every platform place for the tenant's markets (25/09/2026: 2,215
places x AU/US/UK, $49). Research is now an explicit admin action:

  - POST /admin/segment-research/preview — free: scope, stale count, selected count, and the spend
                                           ceiling from the budgets (AA-649). No LLM, no DFS.
  - POST /admin/segment-research/run     — enqueues one `segment_research` job (admin secret required).

Both take explicit markets and a mandatory `max_places` cap. Only one run at a time. AA-650: a run is
a durable job (shared.job, services/jobs/segment_research_job.py), so a deploy mid-run re-queues it
instead of losing it; places that fail or are skipped by an abort stay stale and are picked up by the
next run (AA-647).

AA-649 — every run is bounded by shared.spend_budget (DFS + Bedrock, job kind `segment_research`):
the ceiling is shown before the run, each paid call is checked before it is sent, and the run is
refused up front when the DFS balance is below the run's DFS budget.
"""
from __future__ import annotations

from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from api.routers.admin import verify_admin_secret
from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP
from services.jobs.segment_research_job import KIND as JOB_KIND, dfs_balance, load_budgets
from shared.jobs import queue as job_queue
from shared.jobs.registry import enqueue

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/segment-research", tags=["admin-segment-research"])

# Hard ceiling on scope size: one run can never cover more places than this.
MAX_PLACES_PER_RUN = 200

class ResearchScope(BaseModel):
    markets: list[str] = Field(..., min_length=1, description="Buyer market codes, e.g. ['US','UK']")
    max_places: int = Field(..., ge=1, le=MAX_PLACES_PER_RUN)
    places: Optional[list[str]] = None
    tour_ids: Optional[list[str]] = None
    country: Optional[str] = None
    # AA-648 — "batch" (default): few large paid tasks. "loop": the original per-place ReAct loop.
    strategy: Literal["batch", "loop"] = "batch"
    use_suggestions: bool = True

    @field_validator("markets")
    @classmethod
    def _known_markets(cls, v: list[str]) -> list[str]:
        codes = [m.strip().upper() for m in v]
        unknown = [m for m in codes if m not in DFS_LOCATION_MAP]
        if unknown:
            raise ValueError(f"unknown market(s) {unknown}; supported: {sorted(DFS_LOCATION_MAP)}")
        return codes


def _kwargs(scope: ResearchScope) -> dict:
    return {
        "places": scope.places,
        "tour_ids": scope.tour_ids,
        "country": scope.country,
        "max_places": scope.max_places,
        "strategy": scope.strategy,
        "use_suggestions": scope.use_suggestions,
    }


async def _active_job(pool) -> Optional[dict]:
    return await job_queue.latest(pool, JOB_KIND, ("queued", "running"))


@router.post("/preview", summary="AA-646 — scope + stale count + spend ceiling (no LLM/DFS spend)")
async def preview_research(scope: ResearchScope, request: Request):
    from services.acp_contract.segment_research import run_segment_research

    pool = request.app.state.pool
    result = await run_segment_research({"countries": scope.markets}, pool, dry_run=True, **_kwargs(scope))
    dfs_budget, llm_budget = await load_budgets(pool)
    active = await _active_job(pool)
    return {
        **result,
        "running": active is not None,
        "active_job_id": active["id"] if active else None,
        # AA-649 — the most this run can spend; the run stops before crossing either figure.
        "max_spend_usd": {"dfs": dfs_budget.remaining_usd(), "bedrock": llm_budget.remaining_usd()},
        "budgets": [dfs_budget.summary(), llm_budget.summary()],
    }


@router.post("/run", status_code=202, summary="AA-646/650 — enqueue one admin-triggered research run")
async def run_research(scope: ResearchScope, request: Request, x_admin_secret: str = Header(None),
                       x_admin_user_id: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.acp_contract.segment_research import run_segment_research

    pool = request.app.state.pool
    active = await _active_job(pool)
    if active is not None:
        raise HTTPException(status_code=409,
                            detail=f"A segment research run is already {active['status']} (job {active['id']})")

    preview = await run_segment_research({"countries": scope.markets}, pool, dry_run=True, **_kwargs(scope))
    if preview["places_selected"] == 0:
        return {**preview, "started": False, "reason": "nothing stale in scope"}

    dfs_budget, llm_budget = await load_budgets(pool)
    for budget in (dfs_budget, llm_budget):
        remaining = budget.remaining_usd()
        if remaining is not None and remaining <= 0:
            raise HTTPException(status_code=409, detail=f"{budget.provider} budget exhausted: {budget.summary()}")

    # AA-649 — refuse up front when the DFS balance cannot cover this run's DFS budget, instead of
    # draining the account mid-run (the 25/09 failure mode). An unreadable balance also refuses.
    # The job re-checks both when it starts (a retry can start much later).
    balance = await dfs_balance()
    needed = dfs_budget.remaining_usd()
    if balance is None or (needed is not None and balance < needed):
        raise HTTPException(
            status_code=409,
            detail=f"DataForSEO balance {balance} is below this run's DFS budget {needed}; top up or lower the budget",
        )

    job_id, _ = await enqueue(pool, JOB_KIND, {"markets": scope.markets, **_kwargs(scope)},
                              created_by=f"admin:{x_admin_user_id or 'unknown'}")
    logger.info("admin_segment_research_enqueued", admin_user=x_admin_user_id, markets=scope.markets,
                places_selected=preview["places_selected"], dfs_balance=balance, job_id=job_id)
    return {**preview, "started": True, "job_id": job_id, "dfs_balance_usd": balance,
            "max_spend_usd": {"dfs": needed, "bedrock": llm_budget.remaining_usd()}}


@router.get("/status", summary="AA-646/650 — the active run (if any) and the last finished run")
async def research_status(request: Request):
    pool = request.app.state.pool
    active = await _active_job(pool)
    last = await job_queue.latest(pool, JOB_KIND, ("succeeded", "failed", "stopped_budget", "cancelled"))
    return {
        "running": active is not None,
        "active_job": active,
        "last_result": last["result"] if last else None,
        "last_job": last,
    }
