"""AA-646 — admin-triggered search-demand research (the only path that buys DataForSEO demand data).

Before AA-646 `run_segment_research()` fired after every tenant T2 rewrite and, because AA-545 made
Segments platform-wide, swept every platform place for the tenant's markets (25/09/2026: 2,215
places x AU/US/UK, $49). Research is now an explicit admin action:

  - POST /admin/segment-research/preview — free: scope, stale count, selected count, and the spend
                                           ceiling from the budgets (AA-649). No LLM, no DFS.
  - POST /admin/segment-research/run     — enqueues one `segment_research` job (admin secret required).

Both take explicit markets. AA-1055: a run is bounded by `max_usd` (the selection's capped DFS estimate, default and
ceiling: what the DFS budget still allows) instead of the old 200-place cap; `max_places` is an optional extra limit.
`tiers` picks volume and / or the one SERP per place, `use_suggestions` is off by default, `dfs_queue` picks the live
endpoint or the Standard queue. Only one run at a time. AA-650: a run is
a durable job (shared.job, services/jobs/segment_research_job.py), so a deploy mid-run re-queues it
instead of losing it; places that fail or are skipped by an abort stay stale and are picked up by the
next run (AA-647).

AA-649 — every run is bounded by shared.spend_budget (DFS + Bedrock, job kind `segment_research`):
the ceiling is shown before the run, each paid call is checked before it is sent, and the run is
refused up front when the DFS balance is below the selection's capped estimate (AA-1055; it used to be the whole
remaining budget, which refused small runs whenever the balance was low).
"""
from __future__ import annotations

from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from api.core.staff_authz import staff_policy
from pydantic import BaseModel, Field, field_validator

from api.routers.admin import verify_admin_secret
from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP
from services.jobs.segment_research_job import KIND as JOB_KIND, dfs_balance, load_budgets
from shared.jobs import queue as job_queue
from shared.jobs.registry import enqueue

logger = structlog.get_logger()

# S241 (GĐ −1): every route of this router needs the admin secret. Router-level so a new GET can not
# ship open again (the reads here were public until S241). The inline verify_admin_secret() calls in
# the write routes are kept as-is (same check, run twice).
# AA-882 (GĐ3): staff_policy narrows every route listed in api/core/staff_authz.py POLICY by role.
router = APIRouter(prefix="/admin/segment-research", tags=["admin-segment-research"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

# AA-1055 — the USD cap replaced the 200-place cap; this only bounds a typo (one run's capped estimate).
MAX_USD_PER_RUN = 50.0

class ResearchScope(BaseModel):
    markets: list[str] = Field(..., min_length=1, description="Buyer market codes, e.g. ['US','UK']")
    max_usd: Optional[float] = Field(None, gt=0, le=MAX_USD_PER_RUN,
                                     description="Cap on the selection's capped DFS estimate (default: DFS budget)")
    max_places: Optional[int] = Field(None, ge=1)
    places: Optional[list[str]] = None
    tour_ids: Optional[list[str]] = None
    country: Optional[str] = None
    # AA-648 — "batch" (default): few large paid tasks. "loop": the original per-place ReAct loop.
    strategy: Literal["batch", "loop"] = "batch"
    # AA-1055 — keyword suggestions (keywords_for_keywords) are off by default.
    use_suggestions: bool = False
    tiers: list[Literal["volume", "serp"]] = Field(default_factory=lambda: ["volume", "serp"], min_length=1)
    # None = the env setting DFS_RESEARCH_QUEUE (live by default).
    dfs_queue: Optional[Literal["live", "standard"]] = None
    recompute: bool = True

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
        "max_usd": scope.max_usd,
        "strategy": scope.strategy,
        "use_suggestions": scope.use_suggestions,
        "tiers": list(scope.tiers),
        "dfs_queue": scope.dfs_queue,
        "recompute": scope.recompute,
    }


def _capped(kwargs: dict, dfs_budget) -> dict:
    """The kwargs with max_usd bounded by what the DFS budget still allows (the job applies the same rule)."""
    from services.jobs.segment_research_job import max_usd_for
    return {**kwargs, "max_usd": max_usd_for(kwargs, dfs_budget)}


async def _active_job(pool) -> Optional[dict]:
    return await job_queue.latest(pool, JOB_KIND, ("queued", "running"))


@router.post("/preview", summary="Scope + stale count + spend ceiling (no LLM/DFS spend)")
async def preview_research(scope: ResearchScope, request: Request):
    from services.acp_contract.segment_research import run_segment_research

    pool = request.app.state.pool
    dfs_budget, llm_budget = await load_budgets(pool)
    result = await run_segment_research({"countries": scope.markets}, pool, dry_run=True,
                                        **_capped(_kwargs(scope), dfs_budget))
    active = await _active_job(pool)
    return {
        **result,
        "running": active is not None,
        "active_job_id": active["id"] if active else None,
        # AA-649 — the most this run can spend; the run stops before crossing either figure.
        "max_spend_usd": {"dfs": dfs_budget.remaining_usd(), "bedrock": llm_budget.remaining_usd()},
        "budgets": [dfs_budget.summary(), llm_budget.summary()],
    }


@router.post("/run", status_code=202, summary="Enqueue one admin-triggered research run")
async def run_research(scope: ResearchScope, request: Request, x_admin_secret: str = Header(None),
                       x_admin_user_id: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.acp_contract.segment_research import run_segment_research

    pool = request.app.state.pool
    active = await _active_job(pool)
    if active is not None:
        raise HTTPException(status_code=409,
                            detail=f"A segment research run is already {active['status']} (job {active['id']})")

    dfs_budget, llm_budget = await load_budgets(pool)
    kwargs = _capped(_kwargs(scope), dfs_budget)
    preview = await run_segment_research({"countries": scope.markets}, pool, dry_run=True, **kwargs)
    if preview["places_selected"] == 0:
        return {**preview, "started": False, "reason": "nothing stale in scope (or nothing fits max_usd)"}

    for budget in (dfs_budget, llm_budget):
        remaining = budget.remaining_usd()
        if remaining is not None and remaining <= 0:
            raise HTTPException(status_code=409, detail=f"{budget.provider} budget exhausted: {budget.summary()}")

    # AA-649 / AA-1055 — refuse up front when the DFS balance cannot cover the selection's capped estimate, instead of
    # draining the account mid-run (the 25/09 failure mode). An unreadable balance also refuses.
    # The job re-checks both when it starts (a retry can start much later).
    balance = await dfs_balance()
    needed = preview["estimate"]["dfs_usd_cap"]
    if balance is None or balance < needed:
        raise HTTPException(
            status_code=409,
            detail=f"DataForSEO balance {balance} is below this run's capped estimate {needed}; top up or lower max_usd",
        )

    job_id, _ = await enqueue(pool, JOB_KIND, {"markets": scope.markets, **_kwargs(scope)},
                              created_by=f"admin:{x_admin_user_id or 'unknown'}")
    logger.info("admin_segment_research_enqueued", admin_user=x_admin_user_id, markets=scope.markets,
                places_selected=preview["places_selected"], dfs_balance=balance, job_id=job_id)
    return {**preview, "started": True, "job_id": job_id, "dfs_balance_usd": balance,
            "max_spend_usd": {"dfs": kwargs["max_usd"], "bedrock": llm_budget.remaining_usd()}}


@router.get("/status", summary="The active run (if any) and the last finished run")
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
