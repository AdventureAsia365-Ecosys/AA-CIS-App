"""AA-646 — admin-triggered search-demand research (the only path that buys DataForSEO demand data).

Before AA-646 `run_segment_research()` fired after every tenant T2 rewrite and, because AA-545 made
Segments platform-wide, swept every platform place for the tenant's markets (25/09/2026: 2,215
places x AU/US/UK, $49). Research is now an explicit admin action:

  - POST /admin/segment-research/preview — free: scope, stale count, selected count, and the spend
                                           ceiling from the budgets (AA-649). No LLM, no DFS.
  - POST /admin/segment-research/run     — starts one run in the background (admin secret required).

Both take explicit markets and a mandatory `max_places` cap. Only one run at a time. The run stays an
in-process background task until the durable job runner (AA-650) replaces it; places that fail or are
skipped by an abort stay stale and are picked up by the next run (AA-647).

AA-649 — every run is bounded by shared.spend_budget (DFS + Bedrock, job kind `segment_research`):
the ceiling is shown before the run, each paid call is checked before it is sent, and the run is
refused up front when the DFS balance is below the run's DFS budget.
"""
from __future__ import annotations

import asyncio
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from api.routers.admin import verify_admin_secret
from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP
from shared.cost_guard import load_run_budget, maybe_alert

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/segment-research", tags=["admin-segment-research"])

JOB_KIND = "segment_research"

# Hard ceiling on scope size: one run can never cover more places than this.
MAX_PLACES_PER_RUN = 200

_background_tasks: set = set()
_run_state: dict = {"running": False, "last_result": None}


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


async def _budgets(pool):
    return (await load_run_budget(pool, "dfs", JOB_KIND),
            await load_run_budget(pool, "bedrock", JOB_KIND))


@router.post("/preview", summary="AA-646 — scope + stale count + spend ceiling (no LLM/DFS spend)")
async def preview_research(scope: ResearchScope, request: Request):
    from services.acp_contract.segment_research import run_segment_research

    pool = request.app.state.pool
    result = await run_segment_research({"countries": scope.markets}, pool, dry_run=True, **_kwargs(scope))
    dfs_budget, llm_budget = await _budgets(pool)
    return {
        **result,
        "running": _run_state["running"],
        # AA-649 — the most this run can spend; the run stops before crossing either figure.
        "max_spend_usd": {"dfs": dfs_budget.remaining_usd(), "bedrock": llm_budget.remaining_usd()},
        "budgets": [dfs_budget.summary(), llm_budget.summary()],
    }


async def _dfs_balance() -> Optional[float]:
    from services.seo_intelligence.dataforseo_client import DataForSEOClient

    try:
        money = await DataForSEOClient().fetch_balance()  # free call
        return float(money["balance"]) if money.get("balance") is not None else None
    except Exception as exc:
        logger.warning("segment_research_balance_check_failed", error=str(exc))
        return None


@router.post("/run", status_code=202, summary="AA-646 — start one admin-triggered research run")
async def run_research(scope: ResearchScope, request: Request, x_admin_secret: str = Header(None),
                       x_admin_user_id: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    if _run_state["running"]:
        raise HTTPException(status_code=409, detail="A segment research run is already in progress")

    from services.acp_contract.segment_research import run_segment_research

    pool = request.app.state.pool
    preview = await run_segment_research({"countries": scope.markets}, pool, dry_run=True, **_kwargs(scope))
    if preview["places_selected"] == 0:
        return {**preview, "started": False, "reason": "nothing stale in scope"}

    dfs_budget, llm_budget = await _budgets(pool)
    for budget in (dfs_budget, llm_budget):
        remaining = budget.remaining_usd()
        if remaining is not None and remaining <= 0:
            raise HTTPException(status_code=409, detail=f"{budget.provider} budget exhausted: {budget.summary()}")

    # AA-649 — refuse up front when the DFS balance cannot cover this run's DFS budget, instead of
    # draining the account mid-run (the 25/09 failure mode). An unreadable balance also refuses.
    balance = await _dfs_balance()
    needed = dfs_budget.remaining_usd()
    if balance is None or (needed is not None and balance < needed):
        raise HTTPException(
            status_code=409,
            detail=f"DataForSEO balance {balance} is below this run's DFS budget {needed}; top up or lower the budget",
        )

    async def _run() -> None:
        _run_state["running"] = True
        try:
            result = await run_segment_research(
                {"countries": scope.markets}, pool, dfs_budget=dfs_budget, llm_budget=llm_budget,
                **_kwargs(scope),
            )
            _run_state["last_result"] = result
            logger.info("admin_segment_research_done", admin_user=x_admin_user_id, **result)
            reason = result.get("abort_reason") if result.get("aborted") else None
            for budget in (dfs_budget, llm_budget):
                await maybe_alert(pool, budget, reason if reason and budget.provider in reason else None)
        except Exception:
            logger.error("admin_segment_research_failed", admin_user=x_admin_user_id, exc_info=True)
        finally:
            _run_state["running"] = False

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    logger.info("admin_segment_research_started", admin_user=x_admin_user_id, markets=scope.markets,
                places_selected=preview["places_selected"], dfs_balance=balance)
    return {**preview, "started": True, "dfs_balance_usd": balance,
            "max_spend_usd": {"dfs": needed, "bedrock": llm_budget.remaining_usd()}}


@router.get("/status", summary="AA-646 — is a run in progress, and the last run's result")
async def research_status():
    return dict(_run_state)
