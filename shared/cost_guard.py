"""AA-649 — cost guard: check a spend budget BEFORE every paid external call.

Limits live in `shared.spend_budget` (migration 168). A job loads one `RunBudget` per provider at
start, calls `check(estimate)` before each paid call and `charge(actual)` after it:

    dfs_budget = await load_run_budget(pool, "dfs", "segment_research")
    dfs_budget.check(0.06)          # raises BudgetExceeded when the call would breach a hard limit
    ... call DataForSEO ...
    dfs_budget.charge(response_cost)

The daily figure is today's (UTC) spend from shared.dfs_call_log / shared.llm_call_log at load
time plus what this run has charged since, so two runs started the same day see each other's
spend as of their own start. That is enough for one-run-at-a-time jobs; the durable job runner
(AA-650) will serialise runs per provider.

If the table does not exist yet (the window between deploy and applying migration 168) the guard
falls back to SAFE_DEFAULTS instead of failing open.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import structlog

logger = structlog.get_logger()

PROVIDERS = ("dfs", "bedrock", "openai", "jev")

# Used only when shared.spend_budget is missing (migration 168 not applied yet). Same values as
# the migration's seed rows; (per_run_usd, per_day_usd).
SAFE_DEFAULTS: dict[tuple[str, str], tuple[Optional[float], Optional[float]]] = {
    ("dfs", "global"): (None, 10.0),
    ("dfs", "job:segment_research"): (5.0, None),
    ("bedrock", "job:segment_research"): (2.0, None),
}

# Worst-case price of one call, used for the pre-call check when the exact price is not known yet.
# Observed on 25/09/2026: search_volume task ~$0.057-0.059, keywords_for_keywords $0.09,
# serp advanced $0.002. Rounded up.
DFS_CALL_ESTIMATE_USD = {
    "search_volume": 0.075,
    "search_volume_bulk": 0.075,
    "keywords_for_keywords": 0.09,
    "serp_advanced": 0.002,
}

_DAY_SPEND_SQL = {
    "dfs": "SELECT coalesce(sum(cost_usd), 0) FROM shared.dfs_call_log "
           "WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
    "bedrock": "SELECT coalesce(sum(cost_usd), 0) FROM shared.llm_call_log "
               "WHERE provider LIKE 'bedrock%' "
               "AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
    "openai": "SELECT coalesce(sum(cost_usd), 0) FROM shared.llm_call_log WHERE provider = 'openai' "
              "AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
}

_RECENT_ALERT_SQL = """
    SELECT 1 FROM shared.notifications
     WHERE event_type = $1 AND entity_id = $2 AND is_read = FALSE
       AND created_at >= now() - interval '24 hours'
     LIMIT 1
"""
_INSERT_ALERT_SQL = """
    INSERT INTO shared.notifications
        (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, 'spend_budget', $3, $4::jsonb, ARRAY['admin'])
"""
_PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"
BUDGET_ALERT_EVENT = "spend_budget_alert"


class BudgetExceeded(Exception):
    """A paid call would breach a hard spend limit. Jobs stop cleanly when they see this."""

    def __init__(self, message: str, provider: str = "", limit: str = ""):
        super().__init__(message, provider, limit)
        self.message = message
        self.provider = provider
        self.limit = limit

    def __str__(self) -> str:
        return self.message


@dataclass
class RunBudget:
    provider: str
    scope: str
    per_run_usd: Optional[float]
    per_day_usd: Optional[float]
    hard_stop: bool = True
    alert_pct: int = 80
    day_spent_at_start: float = 0.0
    run_spent: float = 0.0

    @property
    def day_spent(self) -> float:
        return self.day_spent_at_start + self.run_spent

    def remaining_usd(self) -> Optional[float]:
        """The most this run may still spend, or None when both axes are unlimited."""
        caps = []
        if self.per_run_usd is not None:
            caps.append(self.per_run_usd - self.run_spent)
        if self.per_day_usd is not None:
            caps.append(self.per_day_usd - self.day_spent)
        return max(min(caps), 0.0) if caps else None

    def check(self, estimate_usd: float) -> None:
        """Raise BudgetExceeded when spending `estimate_usd` more would breach a hard limit."""
        if not self.hard_stop:
            return
        if self.per_run_usd is not None and self.run_spent + estimate_usd > self.per_run_usd:
            raise BudgetExceeded(
                f"{self.provider} per-run budget ${self.per_run_usd:.2f} reached "
                f"(spent ${self.run_spent:.4f}, next call ~${estimate_usd:.4f})",
                self.provider, "per_run")
        if self.per_day_usd is not None and self.day_spent + estimate_usd > self.per_day_usd:
            raise BudgetExceeded(
                f"{self.provider} daily budget ${self.per_day_usd:.2f} reached "
                f"(spent today ${self.day_spent:.4f}, next call ~${estimate_usd:.4f})",
                self.provider, "per_day")

    def charge(self, usd: Optional[float]) -> None:
        self.run_spent += float(usd or 0.0)

    def day_used_pct(self) -> Optional[float]:
        if not self.per_day_usd:
            return None
        return 100.0 * self.day_spent / self.per_day_usd

    def summary(self) -> dict:
        return {
            "provider": self.provider, "scope": self.scope,
            "per_run_usd": self.per_run_usd, "per_day_usd": self.per_day_usd,
            "hard_stop": self.hard_stop, "run_spent_usd": round(self.run_spent, 4),
            "day_spent_usd": round(self.day_spent, 4), "remaining_usd": _round(self.remaining_usd()),
        }


def _round(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 4)


def _min_cap(*values: Optional[float]) -> Optional[float]:
    present = [float(v) for v in values if v is not None]
    return min(present) if present else None


async def _read_rows(conn, provider: str, job_scope: str) -> dict[str, dict]:
    try:
        rows = await conn.fetch(
            "SELECT scope, per_run_usd, per_day_usd, hard_stop, alert_pct FROM shared.spend_budget "
            "WHERE provider = $1 AND scope = ANY($2::text[])",
            provider, ["global", job_scope],
        )
        return {r["scope"]: dict(r) for r in rows}
    except Exception as exc:  # table missing (migration 168 not applied yet) -> safe defaults
        logger.warning("spend_budget_read_failed_using_defaults", provider=provider, error=str(exc))
        out = {}
        for scope in ("global", job_scope):
            if (provider, scope) in SAFE_DEFAULTS:
                per_run, per_day = SAFE_DEFAULTS[(provider, scope)]
                out[scope] = {"scope": scope, "per_run_usd": per_run, "per_day_usd": per_day,
                              "hard_stop": True, "alert_pct": 80}
        return out


async def day_spend(conn, provider: str) -> float:
    sql = _DAY_SPEND_SQL.get(provider)
    if sql is None:  # jev has no call log yet (AA-660)
        return 0.0
    return float(await conn.fetchval(sql) or 0.0)


async def load_run_budget(pool, provider: str, job_kind: str) -> RunBudget:
    """Effective budget for one run of `job_kind`: per-run from the job row (else global), daily
    cap = the smaller of the job and global daily caps, hard_stop if either row asks for it."""
    job_scope = f"job:{job_kind}"
    async with pool.acquire() as conn:
        rows = await _read_rows(conn, provider, job_scope)
        spent = await day_spend(conn, provider)
    job, glob = rows.get(job_scope, {}), rows.get("global", {})
    per_run = job.get("per_run_usd") if job.get("per_run_usd") is not None else glob.get("per_run_usd")
    hard_stop = any(r.get("hard_stop", True) for r in (job, glob) if r) if (job or glob) else True
    return RunBudget(
        provider=provider, scope=job_scope,
        per_run_usd=_min_cap(per_run),
        per_day_usd=_min_cap(job.get("per_day_usd"), glob.get("per_day_usd")),
        hard_stop=hard_stop,
        alert_pct=int(job.get("alert_pct") or glob.get("alert_pct") or 80),
        day_spent_at_start=spent,
    )


async def maybe_alert(pool, budget: RunBudget, reason: Optional[str] = None) -> bool:
    """Admin notification when today's spend reached alert_pct of the daily cap, or when a run was
    stopped by the budget. At most one unread alert per provider per 24h. Best-effort."""
    pct = budget.day_used_pct()
    if reason is None and (pct is None or pct < budget.alert_pct):
        return False
    try:
        async with pool.acquire() as conn:
            if await conn.fetchval(_RECENT_ALERT_SQL, BUDGET_ALERT_EVENT, budget.provider):
                return False
            message = reason or (f"{budget.provider} spend today ${budget.day_spent:.2f} is "
                                 f"{pct:.0f}% of the ${budget.per_day_usd:.2f} daily budget.")
            await conn.execute(_INSERT_ALERT_SQL, _PLATFORM_TENANT_ID, BUDGET_ALERT_EVENT,
                               budget.provider, json.dumps({"message": message, **budget.summary()}))
        return True
    except Exception as exc:
        logger.warning("spend_budget_alert_failed", provider=budget.provider, error=str(exc))
        return False
