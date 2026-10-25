"""AA-917 WM-12 scope 5 — the learning-loop budget guard (app ADR 0010 decision 9, E4).

Learning-loop LLM spend on Dev is capped at $25 per calendar month (UTC), summed from `shared.llm_call_log.cost_usd`
over every writing-memory stage (`config.MEMORY_STAGES`: the miner now; eval / shadow with AA-919; the WM-13 one-off).
`shared.spend_budget` has a per-run and a per-day axis only, so the month is checked here; the per-run $1 and per-day
$2 of `job:writing_memory_mine` stay in `shared.spend_budget` (CIS 280) through `shared.cost_guard`.

One helper shared by the mine / eval / shadow stages: `MonthBudget.check(estimate)` before each paid call,
`charge(actual)` after it; `check_run(estimate_total)` refuses a whole run up front.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from shared.cost_guard import BudgetExceeded
from services.writing_memory import config as cfg

_MONTH_SPEND_SQL = """
    SELECT coalesce(sum(cost_usd), 0)
      FROM shared.llm_call_log
     WHERE stage = ANY($1::text[])
       AND created_at >= date_trunc('month', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
"""


async def month_spend(conn, stages: Iterable[str] = cfg.MEMORY_STAGES) -> float:
    """This calendar month's (UTC) learning-loop spend in USD."""
    return float(await conn.fetchval(_MONTH_SPEND_SQL, list(stages)) or 0.0)


@dataclass
class MonthBudget:
    spent_at_start: float
    cap_usd: float = cfg.MONTH_CAP_USD
    run_spent: float = 0.0

    @property
    def spent(self) -> float:
        return self.spent_at_start + self.run_spent

    def remaining(self) -> float:
        return max(self.cap_usd - self.spent, 0.0)

    def check(self, estimate_usd: float) -> None:
        """Raise BudgetExceeded when spending `estimate_usd` more would pass the month cap."""
        if self.spent + estimate_usd > self.cap_usd:
            raise BudgetExceeded(
                f"writing-memory month budget ${self.cap_usd:.2f} reached (spent this month ${self.spent:.4f}, "
                f"next ~${estimate_usd:.4f}; ADR 0010 E4)", "bedrock", "per_month")

    check_run = check                     # a whole run's estimate up front: the same rule

    def charge(self, usd: Optional[float]) -> None:
        self.run_spent += float(usd or 0.0)

    def summary(self) -> dict:
        return {"cap_usd": self.cap_usd, "spent_usd": round(self.spent, 4), "remaining_usd": round(self.remaining(), 4)}


async def load_month_budget(conn, cap_usd: float = cfg.MONTH_CAP_USD) -> MonthBudget:
    return MonthBudget(spent_at_start=await month_spend(conn), cap_usd=cap_usd)


__all__ = ["MonthBudget", "load_month_budget", "month_spend"]
