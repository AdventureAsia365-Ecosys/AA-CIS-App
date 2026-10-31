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

# AA-712: 'geocoder' = the external geocoder of the place pipeline (Mapbox Permanent / Amazon
# Location, services/places/geocoder.py); its spend is logged in geo.geocode_call (migration 226).
# AA-1002 (S10): 'anthropic' (the Anthropic Messages API connections) and 'azure_openai' (Azure OpenAI connections),
# the two vendor-API providers migration 304 added to spend_budget_provider_check.
PROVIDERS = ("dfs", "bedrock", "openai", "jev", "geocoder", "anthropic", "azure_openai")

# Used only when shared.spend_budget is missing (migration 168 not applied yet). Same values as
# the migration's seed rows; (per_run_usd, per_day_usd).
SAFE_DEFAULTS: dict[tuple[str, str], tuple[Optional[float], Optional[float]]] = {
    ("dfs", "global"): (None, 10.0),
    ("dfs", "job:segment_research"): (5.0, None),
    ("bedrock", "job:segment_research"): (2.0, None),
}

# Worst-case price of one call, used for the pre-call check when the exact price is not known yet.
# Observed: search_volume task $0.057-0.059 with <=13 keywords (25/09/2026) but $0.09 with ~150
# keywords (Bhutan pilot, 28/09/2026 — the task price rises with keyword count), keywords_for_keywords
# $0.09, serp advanced $0.002. Use the highest observed price.
DFS_CALL_ESTIMATE_USD = {
    "search_volume": 0.09,
    "search_volume_bulk": 0.09,
    "keywords_for_keywords": 0.09,
    "serp_advanced": 0.002,
    # AA-779 (B) — the admin SERP top-up's own log label: a live advanced SERP with the PAA box
    # expanded 2 levels (billed per extra click). Deliberately high so the $3 cap is never overshot.
    "serp_advanced_paa_deep": 0.01,
}

_DAY_SPEND_SQL = {
    "dfs": "SELECT coalesce(sum(cost_usd), 0) FROM shared.dfs_call_log "
           "WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
    "bedrock": "SELECT coalesce(sum(cost_usd), 0) FROM shared.llm_call_log "
               "WHERE provider LIKE 'bedrock%' "
               "AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
    "openai": "SELECT coalesce(sum(cost_usd), 0) FROM shared.llm_call_log WHERE provider = 'openai' "
              "AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
    "anthropic": "SELECT coalesce(sum(cost_usd), 0) FROM shared.llm_call_log WHERE provider = 'anthropic' "
                 "AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
    "azure_openai": "SELECT coalesce(sum(cost_usd), 0) FROM shared.llm_call_log WHERE provider = 'azure_openai' "
                    "AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
    "geocoder": "SELECT coalesce(sum(cost_usd), 0) FROM geo.geocode_call "
                "WHERE called_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
    # AA-642 (S0): Jev (TypeSafe) asks are logged in llm_call_log as provider 'typesafe' (decide.py). Same filter
    # as the admin Overview's Jev figure (api/routers/admin_overview.py _JEV_DAY_SPEND_SQL), so the two agree.
    "jev": "SELECT coalesce(sum(cost_usd), 0) FROM shared.llm_call_log WHERE lower(provider) IN ('typesafe', 'jev') "
           "AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
}

# AA-712: today's LLM spend of ONE job kind (its jobs' llm_call_log rows), for a kind whose daily
# cap is about its own spend — `job:place_extract` $40/day must not trip on a busy S1 wave day,
# which is what the provider-wide figure would do.
_KIND_DAY_SPEND_SQL = (
    "SELECT coalesce(sum(l.cost_usd), 0) FROM shared.llm_call_log l JOIN shared.job j ON j.id = l.job_id "
    "WHERE j.kind = $1 AND l.created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'"
)

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
    # KAN-90 (29/09/2026): the daily cap can be alert-only while the per-run cap still stops.
    # `hard_stop` governs the per-run cap; `hard_stop_day` the daily cap (None = same as hard_stop).
    hard_stop_day: Optional[bool] = None
    # AA-712: a kind-scoped budget (load_run_budget day_scope="kind") keeps the provider-wide daily
    # cap as a second axis — the kind's own spend against its row, the provider's against `global`.
    global_day_usd: Optional[float] = None
    global_day_spent_at_start: float = 0.0
    global_day_hard: bool = True

    @property
    def day_is_hard(self) -> bool:
        return self.hard_stop if self.hard_stop_day is None else self.hard_stop_day

    @property
    def day_spent(self) -> float:
        return self.day_spent_at_start + self.run_spent

    @property
    def global_day_spent(self) -> float:
        return self.global_day_spent_at_start + self.run_spent

    def remaining_usd(self) -> Optional[float]:
        """The most this run may still spend, or None when both axes are unlimited."""
        caps = []
        if self.per_run_usd is not None and self.hard_stop:
            caps.append(self.per_run_usd - self.run_spent)
        if self.per_day_usd is not None and self.day_is_hard:  # an alert-only cap never limits
            caps.append(self.per_day_usd - self.day_spent)
        if self.global_day_usd is not None and self.global_day_hard:
            caps.append(self.global_day_usd - self.global_day_spent)
        return max(min(caps), 0.0) if caps else None

    def check(self, estimate_usd: float) -> None:
        """Raise BudgetExceeded when spending `estimate_usd` more would breach a hard limit."""
        if self.hard_stop and self.per_run_usd is not None and self.run_spent + estimate_usd > self.per_run_usd:
            raise BudgetExceeded(
                f"{self.provider} per-run budget ${self.per_run_usd:.2f} reached "
                f"(spent ${self.run_spent:.4f}, next call ~${estimate_usd:.4f})",
                self.provider, "per_run")
        if self.day_is_hard and self.per_day_usd is not None and self.day_spent + estimate_usd > self.per_day_usd:
            raise BudgetExceeded(
                f"{self.provider} daily budget ${self.per_day_usd:.2f} reached "
                f"(spent today ${self.day_spent:.4f}, next call ~${estimate_usd:.4f})",
                self.provider, "per_day")
        if (self.global_day_hard and self.global_day_usd is not None
                and self.global_day_spent + estimate_usd > self.global_day_usd):
            raise BudgetExceeded(
                f"{self.provider} provider-wide daily budget ${self.global_day_usd:.2f} reached "
                f"(spent today ${self.global_day_spent:.4f}, next call ~${estimate_usd:.4f})",
                self.provider, "per_day_global")

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
            "hard_stop": self.hard_stop, "hard_stop_day": self.day_is_hard,
            "run_spent_usd": round(self.run_spent, 4),
            "day_spent_usd": round(self.day_spent, 4), "remaining_usd": _round(self.remaining_usd()),
            **({"global_day_usd": self.global_day_usd, "global_day_spent_usd": round(self.global_day_spent, 4)}
               if self.global_day_usd is not None else {}),
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
    if sql is None:  # a provider with no ledger query
        return 0.0
    if provider == "geocoder":  # geo.geocode_call exists only once migration 226 is applied
        try:
            return float(await conn.fetchval(sql) or 0.0)
        except Exception as exc:
            logger.warning("geocoder_day_spend_unavailable", error=str(exc)[:200])
            return 0.0
    return float(await conn.fetchval(sql) or 0.0)


async def kind_day_spend(conn, job_kind: str) -> float:
    """Today's (UTC) LLM spend of one job kind's jobs."""
    return float(await conn.fetchval(_KIND_DAY_SPEND_SQL, job_kind) or 0.0)


async def load_run_budget(pool, provider: str, job_kind: str, *, day_scope: str = "provider") -> RunBudget:
    """Effective budget for one run of `job_kind`: per-run from the job row (else global), daily
    cap = the smaller of the job and global daily caps. Each cap stops or only alerts according to
    the row that sets it (KAN-90): the per-run cap follows the hard_stop of the row its value comes
    from; the daily cap is hard if any row that sets a daily value is hard. So `global` with
    hard_stop=false makes the daily DFS cap alert-only while `job:segment_research`'s $5 per run
    still stops the run.

    AA-712 — `day_scope="kind"`: the job row's daily cap is checked against this kind's own LLM
    spend today (kind_day_spend), and the global row's daily cap — the provider-wide hard stop —
    stays a second axis checked against the provider's spend (RunBudget.global_day_usd)."""
    job_scope = f"job:{job_kind}"
    async with pool.acquire() as conn:
        rows = await _read_rows(conn, provider, job_scope)
        spent = await (kind_day_spend(conn, job_kind) if day_scope == "kind" else day_spend(conn, provider))
        provider_spent = await day_spend(conn, provider) if day_scope == "kind" else spent
    global_axis = {}
    if day_scope == "kind":
        g = rows.pop("global", None) or {}
        if g.get("per_day_usd") is not None:
            global_axis = {"global_day_usd": float(g["per_day_usd"]), "global_day_spent_at_start": provider_spent,
                           "global_day_hard": bool(g.get("hard_stop", True))}
    job, glob = rows.get(job_scope, {}), rows.get("global", {})
    run_row = job if job.get("per_run_usd") is not None else glob
    per_run = run_row.get("per_run_usd")
    hard_stop = bool(run_row.get("hard_stop", True)) if run_row else True
    day_rows = [r for r in (job, glob) if r and r.get("per_day_usd") is not None]
    hard_stop_day = any(r.get("hard_stop", True) for r in day_rows) if day_rows else True
    return RunBudget(
        provider=provider, scope=job_scope,
        per_run_usd=_min_cap(per_run),
        per_day_usd=_min_cap(job.get("per_day_usd"), glob.get("per_day_usd")),
        hard_stop=hard_stop, hard_stop_day=hard_stop_day,
        alert_pct=int(job.get("alert_pct") or glob.get("alert_pct") or 80),
        day_spent_at_start=spent,
        **global_axis,
    )


async def maybe_alert(pool, budget: RunBudget, reason: Optional[str] = None) -> bool:
    """Admin notification when today's spend reached alert_pct of the daily cap, or when a run was
    stopped by the budget. At most one unread alert per provider per 24h. Best-effort.
    AA-828: each new bell row also queues one operator e-mail (shared/ops_alerts.py)."""
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
            await _queue_alert_email(conn, budget, message, pct, stopped=reason is not None)
        return True
    except Exception as exc:
        logger.warning("spend_budget_alert_failed", provider=budget.provider, error=str(exc))
        return False


async def _queue_alert_email(conn, budget: RunBudget, message: str, pct: Optional[float], *,
                             stopped: bool) -> None:
    """AA-828: queue the operator e-mail for the bell row just written (job ops_alert_email, sent by
    the worker; never SES from here). Best-effort: a failure only logs, the bell row stays."""
    try:
        from shared import ops_alerts  # lazy: keeps this module import-light for the LLM client

        await ops_alerts.enqueue_spend_budget_email(conn, summary=budget.summary(), alert_pct=budget.alert_pct,
                                                    stopped=stopped, message=message, used_pct=pct)
    except Exception as exc:
        logger.warning("spend_budget_alert_email_enqueue_failed", provider=budget.provider,
                       scope=budget.scope, error=str(exc)[:300])


# ── AA-1002 (gateway slice S10): a monthly budget per LLM connection ────────────────────────────────────────────
#
# shared.llm_provider_connection.monthly_budget_usd (304) is the cap; shared.llm_connection_cost_policy (317) says how
# it acts: alert_pct (soft warning, one bell + e-mail per 24 h) and hard_stop (at 100 % the gateway leaves the
# connection out of every route; false = alert only). No policy row = DEFAULT_CONNECTION_ALERT_PCT / hard stop. The
# month-to-date spend is the connection's own llm_call_log rows (account = connection_key, UTC calendar month).
#
# This axis is independent of the job budgets above (shared.spend_budget, load_run_budget) and of the wave caps,
# which are job budgets too: a job still stops on its own per-run / per-day cap whatever the connection's state, and
# a connection over its budget is left out for every job. The gateway reads the state through
# shared/llm_client/connection_budget.py (cached, never raises); the admin reads it through connection_budgets().

DEFAULT_CONNECTION_ALERT_PCT = 80
# Same bell type as the job budgets; entity "llm_connection:<key>".
CONNECTION_BUDGET_ALERT_EVENT = BUDGET_ALERT_EVENT

_CONNECTION_MONTH_SPEND_SQL = """
    SELECT account, coalesce(sum(cost_usd), 0) AS spent, count(*)::int AS calls
      FROM shared.llm_call_log
     WHERE account = ANY($1::text[])
       AND created_at >= date_trunc('month', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
     GROUP BY account
"""
_COST_POLICY_SQL = """
    SELECT connection_key, alert_pct, hard_stop, usage_secret_name, usage_scope_id, notes, updated_at, updated_by
      FROM shared.llm_connection_cost_policy
"""


@dataclass(frozen=True)
class ConnectionCostPolicy:
    """The 317 policy row of a connection (or the defaults when it has none)."""
    connection_key: str
    alert_pct: int = DEFAULT_CONNECTION_ALERT_PCT
    hard_stop: bool = True
    usage_secret_name: Optional[str] = None
    usage_scope_id: Optional[str] = None
    notes: Optional[str] = None
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    stored: bool = False          # False = no row (defaults)


@dataclass(frozen=True)
class ConnectionBudget:
    connection_key: str
    monthly_budget_usd: float
    month_spent_usd: float
    alert_pct: int = DEFAULT_CONNECTION_ALERT_PCT
    hard_stop: bool = True
    month_calls: int = 0

    @property
    def used_pct(self) -> Optional[float]:
        if self.monthly_budget_usd <= 0:
            return None if self.month_spent_usd <= 0 else 100.0
        return 100.0 * self.month_spent_usd / self.monthly_budget_usd

    @property
    def reached(self) -> bool:
        """At or above 100 % of the monthly budget (a $0 budget is reached at once: "no spend allowed")."""
        return self.month_spent_usd >= self.monthly_budget_usd

    @property
    def stops(self) -> bool:
        """The gateway must leave the connection out: the budget is reached and the policy is a hard stop."""
        return self.hard_stop and self.reached

    @property
    def alerting(self) -> bool:
        pct = self.used_pct
        return self.reached or (pct is not None and pct >= self.alert_pct)

    @property
    def state(self) -> str:
        """ok | alert (at or above alert_pct, or reached without a hard stop) | stopped (left out of routes)."""
        if self.stops:
            return "stopped"
        return "alert" if self.alerting else "ok"

    def summary(self) -> dict:
        return {
            "provider": "llm_connection", "scope": f"connection:{self.connection_key}",
            "connection_key": self.connection_key, "monthly_budget_usd": round(self.monthly_budget_usd, 2),
            "month_spent_usd": round(self.month_spent_usd, 4), "month_calls": self.month_calls,
            "used_pct": _round(self.used_pct), "alert_pct": self.alert_pct, "hard_stop": self.hard_stop,
            "state": self.state,
        }


def _iso(v) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)


async def read_cost_policies(conn, *, missing=None) -> Optional[dict[str, ConnectionCostPolicy]]:
    """Every stored policy row by connection_key. Before migration 317 (the table is missing): ``missing`` when given,
    else {} (the defaults apply to every connection)."""
    try:
        rows = await conn.fetch(_COST_POLICY_SQL)
    except Exception as exc:  # noqa: BLE001 — UndefinedTableError before 317: defaults for every connection
        if type(exc).__name__ != "UndefinedTableError":
            raise
        return {} if missing is None else missing
    return {r["connection_key"]: ConnectionCostPolicy(
        connection_key=r["connection_key"], alert_pct=int(r["alert_pct"]), hard_stop=bool(r["hard_stop"]),
        usage_secret_name=r["usage_secret_name"], usage_scope_id=r["usage_scope_id"], notes=r["notes"],
        updated_at=_iso(r["updated_at"]), updated_by=r["updated_by"], stored=True) for r in rows}


def policy_of(policies: dict, connection_key: str) -> ConnectionCostPolicy:
    return policies.get(connection_key) or ConnectionCostPolicy(connection_key=connection_key)


async def connection_month_spend(conn, connection_keys: list[str]) -> dict[str, tuple[float, int]]:
    """{connection_key: (USD, calls)} logged this UTC month (account = the key); keys with no row are absent."""
    if not connection_keys:
        return {}
    rows = await conn.fetch(_CONNECTION_MONTH_SPEND_SQL, list(connection_keys))
    return {r["account"]: (float(r["spent"] or 0.0), int(r["calls"] or 0)) for r in rows}


async def connection_budgets(conn, budgets_usd: dict[str, Optional[float]],
                             policies: Optional[dict] = None) -> dict[str, ConnectionBudget]:
    """The budget state of every connection in ``budgets_usd`` ({key: monthly_budget_usd}) that has a budget (None =
    no budget: left out, never enforced). One query for the spend, one for the policies (when not given)."""
    capped = {k: float(v) for k, v in budgets_usd.items() if v is not None}
    if not capped:
        return {}
    if policies is None:
        policies = await read_cost_policies(conn)
    spent = await connection_month_spend(conn, sorted(capped))
    out: dict[str, ConnectionBudget] = {}
    for key, cap in capped.items():
        p = policy_of(policies, key)
        usd, calls = spent.get(key, (0.0, 0))
        out[key] = ConnectionBudget(connection_key=key, monthly_budget_usd=cap, month_spent_usd=usd,
                                    alert_pct=p.alert_pct, hard_stop=p.hard_stop, month_calls=calls)
    return out


def connection_budget_message(b: ConnectionBudget) -> str:
    pct = b.used_pct
    pct_text = f"{pct:.0f}%" if pct is not None else "all"
    if b.stops:
        return (f"LLM connection {b.connection_key}: spend this month ${b.month_spent_usd:.2f} reached the "
                f"${b.monthly_budget_usd:.2f} monthly budget; the gateway leaves it out of every route until the "
                "budget is raised or the month ends.")
    return (f"LLM connection {b.connection_key}: spend this month ${b.month_spent_usd:.2f} is {pct_text} of the "
            f"${b.monthly_budget_usd:.2f} monthly budget"
            + (" (alert only: calls go on)." if b.reached else "."))


async def maybe_alert_connection(conn, budget: ConnectionBudget) -> bool:
    """One admin bell (and the ops e-mail, AA-828) when a connection is at or above its alert threshold, at most one
    unread bell per connection per 24 h. Best effort: never raises. ``conn`` is an asyncpg connection."""
    if not budget.alerting:
        return False
    entity = f"llm_connection:{budget.connection_key}"
    try:
        if await conn.fetchval(_RECENT_ALERT_SQL, CONNECTION_BUDGET_ALERT_EVENT, entity):
            return False
        message = connection_budget_message(budget)
        await conn.execute(_INSERT_ALERT_SQL, _PLATFORM_TENANT_ID, CONNECTION_BUDGET_ALERT_EVENT, entity,
                           json.dumps({"message": message, **budget.summary()}))
        try:
            from shared import ops_alerts  # lazy: keeps this module import-light for the LLM client

            await ops_alerts.enqueue_spend_budget_email(conn, summary=budget.summary(), alert_pct=budget.alert_pct,
                                                        stopped=budget.stops, message=message,
                                                        used_pct=budget.used_pct)
        except Exception as exc:  # noqa: BLE001 — the bell row stays
            logger.warning("llm_connection_budget_email_enqueue_failed", connection=budget.connection_key,
                           error=str(exc)[:300])
        logger.warning("llm_connection_budget_alert", **budget.summary())
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_connection_budget_alert_failed", connection=budget.connection_key, error=str(exc)[:300])
        return False
