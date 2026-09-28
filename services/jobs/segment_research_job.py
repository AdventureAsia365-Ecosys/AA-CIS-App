"""AA-650 — `segment_research` job kind (admin-triggered DFS + Haiku research, AA-646/648/649).

Before AA-650 this ran as an asyncio task inside the API with its state in process memory, so a
deploy mid-run lost both the run and its result. Re-running after an interruption is cheap: DFS
results are cached for 7 days (AA-625) and places already researched are no longer stale.

Payload: {"markets": [...], "max_places": int, "places"?, "tour_ids"?, "country"?,
          "strategy": "batch"|"loop", "use_suggestions": bool}
"""
from __future__ import annotations

from typing import Optional

import structlog

from shared.cost_guard import BudgetExceeded, load_run_budget, maybe_alert
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "segment_research"
_SCOPE_KEYS = ("places", "tour_ids", "country", "max_places", "strategy", "use_suggestions")


async def load_budgets(pool):
    return (await load_run_budget(pool, "dfs", KIND), await load_run_budget(pool, "bedrock", KIND))


async def dfs_balance() -> Optional[float]:
    """DataForSEO account balance (free call), or None when it cannot be read."""
    from services.seo_intelligence.dataforseo_client import DataForSEOClient
    try:
        money = await DataForSEOClient().fetch_balance()
        return float(money["balance"]) if money.get("balance") is not None else None
    except Exception as exc:
        logger.warning("segment_research_balance_check_failed", error=str(exc))
        return None


def scope_kwargs(payload: dict) -> dict:
    return {k: payload.get(k) for k in _SCOPE_KEYS if k in payload}


@job_kind(KIND, concurrency=1, max_attempts=2)
async def run(ctx: JobContext) -> dict:
    from services.acp_contract.segment_research import run_segment_research

    markets = ctx.payload.get("markets")
    if not markets:
        raise NonRetryable("payload has no markets")

    # Budgets and balance are re-checked here, not only at enqueue: a retry can start much later.
    dfs_budget, llm_budget = await load_budgets(ctx.pool)
    for budget in (dfs_budget, llm_budget):
        remaining = budget.remaining_usd()
        if remaining is not None and remaining <= 0:
            raise BudgetExceeded(f"{budget.provider} budget exhausted before start",
                                 budget.provider, "per_day")
    balance = await dfs_balance()
    needed = dfs_budget.remaining_usd()
    if balance is None or (needed is not None and balance < needed):
        raise NonRetryable(f"DataForSEO balance {balance} is below this run's DFS budget {needed}")

    await ctx.progress(phase="researching", markets=markets, dfs_balance_usd=balance,
                       max_spend_usd={"dfs": needed, "bedrock": llm_budget.remaining_usd()})
    result = await run_segment_research({"countries": markets}, ctx.pool, dfs_budget=dfs_budget,
                                        llm_budget=llm_budget, **scope_kwargs(ctx.payload))
    ctx.add_cost(dfs_budget.run_spent + llm_budget.run_spent)
    ctx.set_result(result)
    await ctx.progress(phase="done")

    reason = result.get("abort_reason") if result.get("aborted") else None
    for budget in (dfs_budget, llm_budget):
        await maybe_alert(ctx.pool, budget, reason if reason and budget.provider in reason else None)
    if reason:
        if "budget" in reason:
            raise BudgetExceeded(reason)
        raise NonRetryable(f"aborted: {reason}")  # fatal DFS error (auth/payment): retry won't help
    return result
