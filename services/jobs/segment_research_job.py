"""AA-650 — `segment_research` job kind (admin-triggered DFS + Haiku research, AA-646/648/649).

Before AA-650 this ran as an asyncio task inside the API with its state in process memory, so a
deploy mid-run lost both the run and its result. Re-running after an interruption is cheap: DFS
results are cached for 7 days (AA-625) and places already researched are no longer stale.

Payload: {"markets": [...], "max_usd"?: float, "max_places"?: int, "places"?, "tour_ids"?, "country"?,
          "strategy": "batch"|"loop", "use_suggestions": bool, "tiers"?: ["volume", "serp"],
          "dfs_queue"?: "live"|"standard", "recompute"?: bool, "reatomized"?: bool}

AA-1055 — the selection is bounded by `max_usd` (default: what the DFS budget still allows this run, and never more
than MAX_USD_PER_RUN, also when the budget row sets no limit), not by a place count; the DataForSEO balance is checked
against the selection's capped estimate (it used to be checked against the whole remaining budget, which refused small
runs whenever the balance was low). `max_usd` is also the run's hard DFS spend limit: the run budget's per-run cap is
narrowed to it (`narrow_to`), so the check before every paid post stops the run at the figure that was approved and
balance-checked, whatever the estimate missed. A run that stored new demand enqueues one debounced platform recompute
(segment_research.enqueue_recompute_for_places). `strategy: "loop"` is refused (retired, AA-1055).
"""
from __future__ import annotations

from typing import Optional

import structlog

from shared.cost_guard import BudgetExceeded, load_run_budget, maybe_alert
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "segment_research"
# AA-1055 — the USD cap replaced the 200-place cap. The most one run may spend on DataForSEO: the route refuses a
# larger `max_usd`, and a run whose budget row sets no limit is capped here.
MAX_USD_PER_RUN = 50.0
_SCOPE_KEYS = ("places", "tour_ids", "country", "max_places", "max_usd", "strategy", "use_suggestions", "tiers",
               "dfs_queue", "recompute", "reatomized")


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


def max_usd_for(payload: dict, dfs_budget) -> float:
    """The run's USD cap: the payload's (default MAX_USD_PER_RUN), never above what the DFS budget still allows nor
    above MAX_USD_PER_RUN. Never None: a budget row with no limit does not mean an unbounded selection."""
    remaining = dfs_budget.remaining_usd()
    asked = payload.get("max_usd")
    caps = [MAX_USD_PER_RUN, float(asked) if asked is not None else MAX_USD_PER_RUN]
    if remaining is not None:
        caps.append(remaining)
    return min(caps)


def narrow_to(dfs_budget, max_usd: float) -> None:
    """Make `max_usd` the run's hard per-run DFS limit (the same narrowing as the question top-up): the per-run cap
    becomes min(max_usd, the row's per-run cap) and always stops the run. The daily axes keep the row's own hard /
    alert-only setting."""
    if dfs_budget.hard_stop_day is None:
        dfs_budget.hard_stop_day = dfs_budget.hard_stop     # the day cap keeps what hard_stop meant for it
    current = dfs_budget.per_run_usd if dfs_budget.hard_stop else None
    dfs_budget.per_run_usd = max_usd if current is None else min(max_usd, current)
    dfs_budget.hard_stop = True


def balance_gate(balance: Optional[float]):
    """AA-1055 — the before_buy check: refuse when the balance can not cover the selection's capped DFS estimate."""
    async def _check(summary: dict) -> None:
        need = (summary.get("estimate") or {}).get("dfs_usd_cap") or 0.0
        if balance is None or balance < need:
            raise NonRetryable(f"DataForSEO balance {balance} is below this run's capped estimate {need}")
    return _check


# Research runs its selection in phases (AA-648); a run is bounded by its USD cap (AA-1055).
@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=3600)
async def run(ctx: JobContext) -> dict:
    from services.acp_contract.segment_research import run_segment_research

    markets = ctx.payload.get("markets")
    if not markets:
        raise NonRetryable("payload has no markets")
    if (ctx.payload.get("strategy") or "batch") != "batch":
        raise NonRetryable(f"strategy {ctx.payload.get('strategy')!r} is not supported (only 'batch', AA-1055)")

    # Budgets and balance are re-checked here, not only at enqueue: a retry can start much later.
    dfs_budget, llm_budget = await load_budgets(ctx.pool)
    for budget in (dfs_budget, llm_budget):
        remaining = budget.remaining_usd()
        if remaining is not None and remaining <= 0:
            raise BudgetExceeded(f"{budget.provider} budget exhausted before start",
                                 budget.provider, "per_day")
    balance = await dfs_balance()
    if balance is None:
        raise NonRetryable("DataForSEO balance could not be read")
    kwargs = scope_kwargs(ctx.payload)
    kwargs["max_usd"] = max_usd_for(ctx.payload, dfs_budget)
    narrow_to(dfs_budget, kwargs["max_usd"])

    await ctx.progress(phase="researching", markets=markets, dfs_balance_usd=balance,
                       max_spend_usd={"dfs": kwargs["max_usd"], "bedrock": llm_budget.remaining_usd()})
    result = await run_segment_research({"countries": markets}, ctx.pool, dfs_budget=dfs_budget,
                                        llm_budget=llm_budget, before_buy=balance_gate(balance), **kwargs)
    # LLM spend is already attributed through llm_call_log.job_id (read-side sum, queue.py);
    # only the non-LLM DataForSEO spend is reported here, so nothing is counted twice.
    ctx.add_cost(dfs_budget.run_spent)
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
