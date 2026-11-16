"""shared/research_budget.py — AA-1122: the caps shared by the budgets of one research run.

A research run pays three providers: DataForSEO (a dfs RunBudget), the keyword LLM (a bedrock RunBudget) and Jev (its
keyword questions). The monthly research cap (setting ``cis.spend.research_max_usd_per_month``) and the automatic run
cap (``cis.spend.research_auto_max_usd_per_run``, or ``cis.spend.tour_paa_backfill_max_usd_per_run`` for the tour PAA
backfill) are ONE counter over all three: a :class:`SharedCap` is attached to both run budgets (:func:`with_caps` turns
a shared.cost_guard.RunBudget into a :class:`CappedRunBudget`), so every check before a paid call asks it and every
charge after one adds to it; the Jev questions reserve their estimate through it too
(segment_research_batch._gate_keywords).

The month figure is read ONCE at the start of a job attempt from the ledger, BY JOB (critic C1 of the S282 design:
research rows carry the stages a3_search_demand / segment_research_batch / a3_research, never 'segment_research'):
every shared.dfs_call_log and shared.llm_call_log row (Jev rows included) whose job_id is a job of a research kind
(:data:`RESEARCH_JOB_KINDS`), created this UTC month. "Ledger at start + this attempt's own charges" holds only while
ONE paying research job runs at a time, and the job runner's concurrency 1 is per kind, not across the research kinds
(S282 review). So paying research is one lane over every research kind (:func:`active_paying_research_job`,
:func:`other_paying_research_job`): the run endpoints refuse with 409 while another paying research job is queued or
running, and a paying job that starts while another one runs goes back to the queue (Reschedule) before it reads the
month. A job is marked running when it is claimed, before its handler starts, so two attempts can never both see the
other as absent. It does not depend on the fire-and-forget ledger writes landing in time (critic C3). An unreadable
ledger raises: the caller refuses the run (fail closed), it never reads 0. A cap of 0 means no spend at all, never
"unlimited" (critic C9).
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Optional

from shared import settings as _settings
from shared.cost_guard import BudgetExceeded, RunBudget

# The job kinds whose spend counts against the research month cap.
RESEARCH_JOB_KINDS: tuple[str, ...] = ("segment_research", "tour_paa_backfill")
MONTH_CAP_SETTING = "cis.spend.research_max_usd_per_month"

# A research job that may pay: every segment_research job and a tour_paa_backfill whose payload asks for SERPs (a
# store-only backfill reads the store, $0, and runs beside a research run: the research run enqueues it). Any `paid`
# value other than false / null / 0 / "" counts as paying (the job reads it with bool()): over-counting only waits.
_PAYING_SQL = """(kind = 'segment_research' OR (kind = 'tour_paa_backfill'
                  AND coalesce(payload->'paid', 'false'::jsonb) NOT IN ('false'::jsonb, 'null'::jsonb, '0'::jsonb,
                                                                        '""'::jsonb)))"""
ACTIVE_PAYING_SQL = f"""
    SELECT id::text AS id, kind, status FROM shared.job
     WHERE status IN ('queued', 'running') AND {_PAYING_SQL}
     ORDER BY created_at LIMIT 1
"""
OTHER_PAYING_RUNNING_SQL = f"""
    SELECT id::text AS id, kind, status FROM shared.job
     WHERE status = 'running' AND id <> $1::uuid AND {_PAYING_SQL}
     ORDER BY created_at LIMIT 1
"""

_RESEARCH_MONTH_SPEND_SQL = """
    WITH jobs AS (SELECT id FROM shared.job WHERE kind = ANY($1::text[]))
    SELECT (SELECT coalesce(sum(d.cost_usd), 0) FROM shared.dfs_call_log d
             WHERE d.job_id IN (SELECT id FROM jobs) AND d.created_at >= $2)::float8 AS dfs_usd,
           (SELECT coalesce(sum(l.cost_usd), 0) FROM shared.llm_call_log l
             WHERE l.job_id IN (SELECT id FROM jobs) AND l.created_at >= $2)::float8 AS llm_usd
"""
_JOB_SPEND_SQL = """
    SELECT (SELECT coalesce(sum(cost_usd), 0) FROM shared.dfs_call_log WHERE job_id = $1::uuid)::float8 AS dfs_usd,
           (SELECT coalesce(sum(cost_usd), 0) FROM shared.llm_call_log WHERE job_id = $1::uuid)::float8 AS llm_usd
"""


def month_start(now: Optional[datetime] = None) -> datetime:
    """The start of the current UTC calendar month (an aware datetime)."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass
class SharedCap:
    """One USD cap over several budgets of a run. ``cap_usd`` None = no limit; 0 = no spend at all.
    ``spent_at_start`` = what the ledger says was already spent against it."""
    name: str
    cap_usd: Optional[float]
    spent_at_start: float = 0.0
    run_spent: float = 0.0
    limit: str = "per_month"

    @property
    def spent(self) -> float:
        return self.spent_at_start + self.run_spent

    def remaining(self) -> float:
        return float("inf") if self.cap_usd is None else max(self.cap_usd - self.spent, 0.0)

    def check(self, estimate_usd: float) -> None:
        if self.cap_usd is not None and self.spent + float(estimate_usd or 0.0) > self.cap_usd + 1e-12:
            raise BudgetExceeded(
                f"research {self.name} budget ${self.cap_usd:.2f} reached (spent ${self.spent:.4f}, next call "
                f"~${float(estimate_usd or 0.0):.4f})", "research", self.limit)

    def charge(self, usd: Optional[float]) -> None:
        self.run_spent += float(usd or 0.0)

    def summary(self) -> dict:
        return {"name": self.name, "cap_usd": self.cap_usd, "spent_at_start_usd": round(self.spent_at_start, 4),
                "run_spent_usd": round(self.run_spent, 4), "remaining_usd": None if self.cap_usd is None
                else round(self.remaining(), 4)}


@dataclass
class CappedRunBudget(RunBudget):
    """A RunBudget that also asks / charges its shared caps (the month cap, the automatic run cap)."""
    shared_caps: tuple = ()

    def remaining_usd(self) -> Optional[float]:
        own = super().remaining_usd()
        caps = [c.remaining() for c in self.shared_caps] + ([own] if own is not None else [])
        return max(min(caps), 0.0) if caps else None

    def check(self, estimate_usd: float) -> None:
        super().check(estimate_usd)
        for cap in self.shared_caps:
            cap.check(estimate_usd)

    def charge(self, usd: Optional[float]) -> None:
        super().charge(usd)
        for cap in self.shared_caps:
            cap.charge(usd)

    def summary(self) -> dict:
        return {**super().summary(), "shared_caps": [c.summary() for c in self.shared_caps]}


def with_caps(budget: RunBudget, *caps: Optional[SharedCap]) -> CappedRunBudget:
    """`budget` as a CappedRunBudget with `caps` (None entries left out); its fields and run spend are kept."""
    values = {f.name: getattr(budget, f.name) for f in fields(RunBudget)}
    return CappedRunBudget(**values, shared_caps=tuple(c for c in caps if c is not None))


async def research_month_spend(pool, now: Optional[datetime] = None) -> dict:
    """{dfs_usd, llm_usd, total_usd} of every research job this UTC month, from the ledger. Raises on a database
    error: the caller must refuse, never read 0."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_RESEARCH_MONTH_SPEND_SQL, list(RESEARCH_JOB_KINDS), month_start(now))
    dfs, llm = float(row["dfs_usd"] or 0.0), float(row["llm_usd"] or 0.0)
    return {"dfs_usd": round(dfs, 6), "llm_usd": round(llm, 6), "total_usd": round(dfs + llm, 6)}


async def active_paying_research_job(pool) -> Optional[dict]:
    """The oldest paying research job queued or running ({id, kind, status}), or None. Raises on a database error
    (the endpoint then refuses: fail closed)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(ACTIVE_PAYING_SQL)
    return dict(row) if row else None


async def other_paying_research_job(pool, job_id: str) -> Optional[dict]:
    """Another paying research job that is running now ({id, kind, status}), or None. Asked by a paying job at the
    start of its attempt, before it reads the month: when there is one, it waits (Reschedule). Raises on a database
    error (the attempt fails and retries: fail closed)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(OTHER_PAYING_RUNNING_SQL, str(job_id))
    return dict(row) if row else None


def lane_busy_detail(other: dict) -> str:
    return f"another paying research job is {other['status']} ({other['kind']} job {other['id']})"


async def wait_for_lane(pool, job_id: str) -> None:
    """Called by a paying research job at the start of each attempt, before it reads the month: while another paying
    research job runs, this job goes back to the queue (Reschedule: the attempt is not counted) and looks again after
    the runner's longest reschedule delay. A database error propagates (the attempt fails: fail closed)."""
    from shared.jobs.registry import RESCHEDULE_MAX_DELAY_SECONDS, Reschedule

    other = await other_paying_research_job(pool, job_id)
    if other is not None:
        raise Reschedule(RESCHEDULE_MAX_DELAY_SECONDS, {"phase": "waiting_research_lane", "waiting_for": other},
                         lane_busy_detail(other))


async def job_spend(pool, job_id: str) -> float:
    """What the ledger holds for one job (every attempt so far): DFS + LLM + Jev. Raises on a database error."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_JOB_SPEND_SQL, str(job_id))
    return float(row["dfs_usd"] or 0.0) + float(row["llm_usd"] or 0.0)


def research_month_cap_usd() -> float:
    """The monthly research cap in force (0 = no research spend)."""
    return float(_settings.get(MONTH_CAP_SETTING))


async def load_research_month_cap(pool, now: Optional[datetime] = None) -> SharedCap:
    """The month cap of research with the ledger's month spend at this moment (read once per job attempt)."""
    spent = await research_month_spend(pool, now)
    return SharedCap("month", research_month_cap_usd(), spent_at_start=spent["total_usd"], limit="per_month")


__all__ = ["RESEARCH_JOB_KINDS", "active_paying_research_job", "other_paying_research_job", "lane_busy_detail",
           "wait_for_lane",            "SharedCap", "CappedRunBudget", "with_caps", "month_start", "research_month_spend",
           "job_spend", "research_month_cap_usd", "load_research_month_cap"]
