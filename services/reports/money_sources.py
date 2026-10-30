"""AA-1003 R3 — the stored rows the money reports read (EX-01, FI-01..FI-06, OPS-08), behind one interface.

Every reader takes the report job's connection and returns plain dicts; `collect_week` / `collect_month` run the
readers a report needs, each part on its own (a part that fails becomes {"error": ...} and the report says so,
never a failed run: an outside source that is down must not hide the rest of the month's numbers).

Days: the money reports count UTC calendar days, the day unit of every cost ledger (Cost Explorer, the
shared.llm_call_daily rollup, the /admin/platform/cost and /admin/cost-ops pages). The ISO week / month of the period
is read as those UTC dates; the KPI snapshots (shared.kpi_daily, ICT days) are read only for K-CAT-LIVE and the job
KPIs.

Sources (names: root docs/architecture/db-schema-reference.md). No LLM, no paid API call, no new IAM:
  llm ledger      shared.llm_call_daily (rolled-up days) + shared.llm_call_log (days not rolled up yet), the same
                  rolled/raw split as api.services.platform_cost.AI_SPEND_SQL, by day, app, tenant, stage, role,
                  model, provider, account, shadow (quality_signal shadow flag, as the rollup computes it).
  aws             STORED Cost Explorer snapshots only (shared.cost_explorer_snapshot per account and service,
                  shared.cost_explorer_app_snapshot per App tag), read through the cost modules the admin pages use
                  (platform_cost.cost_by_app with refresh_if_stale=False, cost_ops.cost_forecast,
                  cost_explorer.read_cost_range). A report never calls Cost Explorer (~0.01 USD a request): a month
                  whose stored data is missing, partial or fetched before the month settled is labelled so
                  (`aws_status`), with the way to refresh it (open Platform › Cost for that month, run again).
  vendors         shared.dfs_call_log, shared.dfs_balance_snapshot (latest), geo.geocode_call, shared.decision_log
                  (Jev decisions), shared.jev_credit_topup via shared.jev_credit.estimate.
  budgets         shared.spend_budget, the AWS / App budgets mirrored in api.services.cost_ops, budget bells
                  (shared.notifications event spend_budget_alert), credits from env COST_OPS_CREDITS_USD.
  demand / units  gold_aa_internal.published_tours (published_at = first publish, updated_at = last republish,
                  CIS 273), gold_aa_internal.v_tour_liveness (tours live now); the ops.overview.v1 contract views
                  tripplanner.v_ops_traffic_daily and booking.v_ops_funnel_daily (cis_app SELECT; unreadable =
                  "not readable", like TP-03).
  capacity        pg catalog sizes and connection limits (free), shared.kpi_daily job KPIs, OPS-12 runs (daily app
                  health), CloudWatch RDS metrics through the ops overview client (one GetMetricData call a month,
                  3 metrics, about 0.00003 USD; cloudwatch:GetMetricData is already granted for /admin/overview).
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional

import structlog

from services.reports import periods, store

logger = structlog.get_logger()

UTC = timezone.utc
CE_FINAL_AFTER = timedelta(days=3)        # Cost Explorer stops revising a day ~48 h after it ends (platform_cost)
CE_LAG_DAYS = 2                           # the last ~2 days of a window may be missing from a fetch: not a gap
BUDGET_ALERT_EVENT = "spend_budget_alert"  # shared/cost_guard.py BUDGET_ALERT_EVENT
RUNWAY_DAYS = 14                          # K-RUNWAY-*: average daily spend over 14 days


class ConnPool:
    """The job's one connection behind the pool interface the cost modules were written for (`acquire()` plus the
    fetch helpers). Calls stay sequential: one asyncpg connection runs one query at a time."""

    def __init__(self, conn: Any):
        self._conn = conn

    @asynccontextmanager
    async def _ctx(self):
        yield self._conn

    def acquire(self):
        return self._ctx()

    async def fetch(self, *a, **kw):
        return await self._conn.fetch(*a, **kw)

    async def fetchrow(self, *a, **kw):
        return await self._conn.fetchrow(*a, **kw)

    async def fetchval(self, *a, **kw):
        return await self._conn.fetchval(*a, **kw)


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:200]}"


def utc_start(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=UTC)


def utc_end_of(day: date) -> datetime:
    """The last second of a UTC day: the `now` that makes the cost modules treat `day` as today (complete up to it)."""
    return datetime.combine(day, time(23, 59, 59), tzinfo=UTC)


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def months_back(first: date, n: int) -> list[tuple[date, date]]:
    """The n calendar months ending with the one starting at `first`, oldest first."""
    out = [periods.month_bounds(first)]
    while len(out) < n:
        out.insert(0, periods.previous_month(out[0][0]))
    return out


# ── SQL ─────────────────────────────────────────────────────────────────────────────────────────────────────────

# $1/$2 = UTC dates [start, end). A day is either rolled (llm_call_daily) or raw, never both (services/retention).
LLM_LEDGER_SQL = """
    WITH rolled AS (
        SELECT d.day, d.app, d.tenant_id, d.stage, d.role, d.model, d.provider, d.account, d.is_shadow,
               d.calls, d.fallback_calls, d.tokens_in, d.tokens_out, d.cost_usd
          FROM shared.llm_call_daily d
         WHERE d.day >= $1 AND d.day < $2
    ), raw AS (
        SELECT (l.created_at AT TIME ZONE 'UTC')::date AS day, coalesce(l.quality_signal ->> 'app', 'cis') AS app,
               l.tenant_id, l.stage, l.role, l.model, l.provider, l.account,
               l.quality_signal @> '{"shadow": true}'::jsonb AS is_shadow,
               count(*)::bigint AS calls, count(*) FILTER (WHERE l.fallback_used)::bigint AS fallback_calls,
               coalesce(sum(l.tokens_in), 0)::bigint AS tokens_in, coalesce(sum(l.tokens_out), 0)::bigint AS tokens_out,
               coalesce(sum(l.cost_usd), 0) AS cost_usd
          FROM shared.llm_call_log l
         WHERE l.created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
           AND l.created_at < ($2::date::timestamp AT TIME ZONE 'UTC')
           AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                            WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
         GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
    )
    SELECT day, app, tenant_id::text AS tenant_id, stage, role, model, provider, account, is_shadow,
           sum(calls)::bigint AS calls, sum(fallback_calls)::bigint AS fallback_calls,
           sum(tokens_in)::bigint AS tokens_in, sum(tokens_out)::bigint AS tokens_out,
           sum(cost_usd)::float AS cost_usd
      FROM (SELECT * FROM rolled UNION ALL SELECT * FROM raw) u
     GROUP BY day, app, tenant_id, stage, role, model, provider, account, is_shadow
"""

DFS_DAILY_SQL = """
    SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*)::bigint AS calls,
           count(*) FILTER (WHERE fetched_live)::bigint AS live_calls,
           coalesce(sum(keyword_count), 0)::bigint AS keywords, coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM shared.dfs_call_log
     WHERE created_at >= $1 AND created_at < $2
     GROUP BY 1
"""

GEO_DAILY_SQL = """
    SELECT (called_at AT TIME ZONE 'UTC')::date AS day, provider, coalesce(sum(requests), 0)::bigint AS requests,
           coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM geo.geocode_call
     WHERE called_at >= $1 AND called_at < $2
     GROUP BY 1, 2
"""

# decision_log_created_id_idx (created_at, id) bounds the scan to the window.
JEV_DAILY_SQL = """
    SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*)::bigint AS decisions,
           count(*) FILTER (WHERE NOT cached)::bigint AS uncached, coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM shared.decision_log
     WHERE created_at >= $1 AND created_at < $2
     GROUP BY 1
"""

# Newest fetch per (account, service, day), summed per (account, day): api.services.cost_ops.CE_DAILY_SQL.
CE_DAILY_SQL = """
    SELECT account_id, period_start AS day, sum(amount_usd)::float AS amount_usd, max(fetched_at) AS fetched_at
      FROM (SELECT DISTINCT ON (account_id, service, period_start) account_id, period_start, amount_usd, fetched_at
              FROM shared.cost_explorer_snapshot
             WHERE period_start >= $1 AND period_start < $2
             ORDER BY account_id, service, period_start, fetched_at DESC) s
     GROUP BY 1, 2
"""

BUDGET_ALERTS_SQL = """
    SELECT entity_id AS provider, payload ->> 'message' AS message, coalesce(payload ->> 'hard_stop', '') AS hard_stop,
           created_at
      FROM shared.notifications
     WHERE event_type = $1 AND created_at >= $2 AND created_at < $3
     ORDER BY created_at
     LIMIT 200
"""

SPEND_BUDGETS_SQL = """
    SELECT provider, scope, per_run_usd::float AS per_run_usd, per_day_usd::float AS per_day_usd, hard_stop, alert_pct
      FROM shared.spend_budget ORDER BY provider, scope
"""

# K-UNIT-TOUR's denominator: tours published or republished in [$1, $2). Since CIS 273 (AA-645 d06) published_at is the
# FIRST publish and stays put on a rewrite, which records updated_at instead (status changes do not touch it,
# services/tour_lifecycle). Counting published_at alone gave 0 for October 2026, the month the whole catalogue was
# rewritten (S259). A tour republished again after the period counts in the later period only (no publish history).
PUBLISHED_TOURS_SQL = """
    SELECT count(*)::bigint AS total,
           count(*) FILTER (WHERE published_at >= $1 AND published_at < $2)::bigint AS first_published,
           count(*) FILTER (WHERE published_at < $1)::bigint AS republished
      FROM gold_aa_internal.published_tours
     WHERE (published_at >= $1 AND published_at < $2) OR (updated_at >= $1 AND updated_at < $2)
"""

# Tours by liveness state now (the K-CAT-LIVE source, kpis.TOURS_LIVE_SQL, split by state).
LIVENESS_NOW_SQL = "SELECT state, count(*)::bigint AS n FROM gold_aa_internal.v_tour_liveness GROUP BY state"

TP_METRICS_SQL = """
    SELECT metric, coalesce(sum(n), 0)::bigint AS n FROM tripplanner.v_ops_traffic_daily
     WHERE day >= $1 AND day <= $2 GROUP BY metric
"""

BK_METRICS_SQL = """
    SELECT metric, coalesce(sum(n), 0)::bigint AS n FROM booking.v_ops_funnel_daily
     WHERE day >= $1 AND day <= $2 GROUP BY metric
"""

# The OPS-12 digests of the period (one per ICT day; the newest succeeded run of a day wins).
DIGEST_RUNS_SQL = """
    SELECT DISTINCT ON (period_end) period_end, output -> 'attention' AS attention,
           output -> 'facts' -> 'apps' -> 'items' AS apps
      FROM shared.report_run
     WHERE report_id = 'OPS-12' AND status = 'succeeded' AND period_end >= $1 AND period_end <= $2
     ORDER BY period_end, created_at DESC
"""

DB_SIZE_SQL = "SELECT pg_database_size(current_database())::bigint"
TOP_RELATIONS_SQL = """
    SELECT n.nspname AS schema, c.relname AS name, pg_total_relation_size(c.oid)::bigint AS bytes
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'm', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
     ORDER BY 3 DESC LIMIT 10
"""
CONNECTIONS_SQL = """
    SELECT (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend')::int AS connections,
           current_setting('max_connections')::int AS max_connections,
           (SELECT coalesce(sum(rolconnlimit), 0) FROM pg_roles WHERE rolcanlogin AND rolconnlimit > 0)::int
               AS role_limits_total
"""


# ── readers ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _rows(rows: Iterable) -> list[dict]:
    return [dict(r) for r in rows]


async def llm_ledger(conn, first: date, end: date) -> list[dict]:
    """LLM ledger rows for UTC days [first, end)."""
    return _rows(await conn.fetch(LLM_LEDGER_SQL, first, end))


async def dfs_daily(conn, first: date, end: date) -> list[dict]:
    return _rows(await conn.fetch(DFS_DAILY_SQL, utc_start(first), utc_start(end)))


async def geo_daily(conn, first: date, end: date) -> list[dict]:
    return _rows(await conn.fetch(GEO_DAILY_SQL, utc_start(first), utc_start(end)))


async def jev_daily(conn, first: date, end: date) -> list[dict]:
    return _rows(await conn.fetch(JEV_DAILY_SQL, utc_start(first), utc_start(end)))


async def ce_daily(conn, first: date, end: date) -> list[dict]:
    """Stored Cost Explorer spend per account key (acc2 / acc3 / retired) and UTC day."""
    from shared.aws_client import cost_explorer as ce

    rows = _rows(await conn.fetch(CE_DAILY_SQL, first, end))
    ids = {r["account_id"] for r in rows}
    keys = {a: await asyncio.to_thread(ce.account_key, a) for a in ids}      # STS once (cached), off the loop
    for r in rows:
        r["account"] = keys.get(r["account_id"]) or "unknown"
        r.pop("account_id", None)        # never carried into a report (public repo, AA-902)
    return rows


async def budget_alerts(conn, first: date, end: date) -> list[dict]:
    rows = _rows(await conn.fetch(BUDGET_ALERTS_SQL, BUDGET_ALERT_EVENT, utc_start(first), utc_start(end)))
    for r in rows:
        r["stopped"] = is_stop_message(r.get("message"))
    return rows


def is_stop_message(message: Optional[str]) -> bool:
    """A budget bell written because a run was stopped (cost_guard.maybe_alert with a reason) rather than the
    threshold bell, whose text is "<provider> spend today $x is n% of the $y daily budget." Best effort: the gateway
    keeps no refusal log, so K-FIN-HARDSTOP counts these bells."""
    text = (message or "").strip()
    return bool(text) and not (" spend today $" in text and text.endswith(" daily budget."))


async def spend_budgets(conn) -> list[dict]:
    return _rows(await conn.fetch(SPEND_BUDGETS_SQL))


async def published_tours(conn, first: date, end: date) -> dict:
    """{"total", "first_published", "republished"}: tours published or republished in UTC days [first, end)."""
    row = await conn.fetchrow(PUBLISHED_TOURS_SQL, utc_start(first), utc_start(end))
    return {k: int((row or {}).get(k) or 0) for k in ("total", "first_published", "republished")}


async def liveness_now(conn) -> dict:
    """{state: n} of gold_aa_internal.v_tour_liveness now (served / paused / retired) and `as_of`."""
    out: dict[str, Any] = {r["state"]: int(r["n"] or 0) for r in await conn.fetch(LIVENESS_NOW_SQL)}
    out["as_of"] = datetime.now(UTC).isoformat()
    return out


async def demand(conn, first: date, last: date) -> dict:
    """{"tripplanner": {metric: n}, "booking": {metric: n}} over UTC days [first, last] from the contract views; a
    view CIS cannot read as written gives {"error": ...} for that side (handoff.CONTRACT_ERRORS)."""
    from services.reports import handoff

    out: dict[str, Any] = {}
    for side, sql in (("tripplanner", TP_METRICS_SQL), ("booking", BK_METRICS_SQL)):
        try:
            out[side] = {r["metric"]: int(r["n"] or 0) for r in await conn.fetch(sql, first, last)}
        except handoff.CONTRACT_ERRORS as e:
            out[side] = {"error": _err(e)}
    return out


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


async def digest_runs(conn, first: date, last: date) -> list[dict]:
    rows = []
    for r in await conn.fetch(DIGEST_RUNS_SQL, first, last):
        rows.append({"day": r["period_end"], "attention": _json(r["attention"]) or [], "apps": _json(r["apps"]) or []})
    return rows


async def balances(conn) -> dict:
    """DataForSEO's latest stored balance, the Jev credit estimate and the hand-set credits (acc3)."""
    from api.services import cost_ops
    from shared import jev_credit
    from shared.dfs_client.balance import read_latest_balance

    dfs = await read_latest_balance(ConnPool(conn))
    return {"dfs": {k: dfs.get(k) for k in ("balance_usd", "fetched_at", "below_threshold")} if dfs else None,
            "jev": await jev_credit.estimate(conn),
            "credits": {k: {**v, "expires_on": v["expires_on"].isoformat() if v["expires_on"] else None}
                        for k, v in cost_ops.credits().items()}}


async def cost_by_app_month(conn, month_first: date, now: datetime) -> dict:
    """/admin/platform/cost for one month from STORED rows (no Cost Explorer refresh)."""
    from api.services import platform_cost

    return await platform_cost.cost_by_app(ConnPool(conn), month_key(month_first), redis=None,
                                           refresh_if_stale=False, now=now)


async def ce_services(conn, first: date, end: date) -> Optional[dict]:
    """Stored Cost Explorer spend per account and service, [first, end). Account IDs are dropped (keys only)."""
    from shared.aws_client import cost_explorer as ce

    data = await ce.read_cost_range(ConnPool(conn), first, end)
    if data is None:
        return None
    for a in data["accounts"]:
        a.pop("account_id", None)
    for s in data["services"]:
        s.pop("account_id", None)
    return data


async def forecast(conn, month_first: date, as_of: datetime) -> dict:
    """/admin/cost-ops/forecast for the month as it stood at `as_of` (stored rows only)."""
    from api.services import cost_ops

    out = await cost_ops.cost_forecast(ConnPool(conn), month_key(month_first), now=as_of)
    for a in out.get("aws_accounts") or []:
        a.pop("account_id", None)
    return out


async def db_capacity(conn) -> dict:
    size = await conn.fetchval(DB_SIZE_SQL)
    top = _rows(await conn.fetch(TOP_RELATIONS_SQL))
    c = dict(await conn.fetchrow(CONNECTIONS_SQL) or {})
    return {"database_bytes": int(size or 0), "top_relations": top, **c}


RDS_METRICS = (("cpu", "CPUUtilization", "Average"), ("connections", "DatabaseConnections", "Maximum"),
               ("free_storage", "FreeStorageSpace", "Minimum"))


def rds_instance_id() -> str:
    """The RDS instance the capacity review reads (env RDS_INSTANCE_ID, the same variable /admin/pipeline uses)."""
    return os.environ.get("RDS_INSTANCE_ID", "aa-cis-dev-db")


def _rds_metrics_sync(first: date, end: date) -> dict:
    from api.services.ops_overview import cloudwatch

    client = cloudwatch.aws_client("cloudwatch")
    queries = [{"Id": f"q_{key}", "ReturnData": True,
                "MetricStat": {"Metric": {"Namespace": "AWS/RDS", "MetricName": name,
                                          "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": rds_instance_id()}]},
                               "Period": 3600, "Stat": stat}} for key, name, stat in RDS_METRICS]
    results, calls = cloudwatch.get_metric_data(client, queries, utc_start(first), utc_start(end))
    out: dict[str, Any] = {"calls": calls}
    for r in results:
        key = str(r.get("Id", ""))[2:]
        out[key] = sorted(zip(r.get("Timestamps") or [], r.get("Values") or []))
    return out


async def rds_metrics(first: date, end: date) -> dict:
    """Hourly RDS CPU (average), connections (maximum) and free storage (minimum) for [first, end): one
    GetMetricData call. {"unavailable": reason} when CloudWatch refuses or fails (never a failed report)."""
    from api.services.ops_overview import cloudwatch

    try:
        return await asyncio.to_thread(_rds_metrics_sync, first, end)
    except Exception as e:      # noqa: BLE001 — an outside read; the report names it instead of failing
        reason = "not_permitted" if cloudwatch.is_denied(e) else "error"
        return {"unavailable": f"{reason}: {_err(e)}"}


# ── part runner ─────────────────────────────────────────────────────────────────────────────────────────────────

Part = Callable[[], Awaitable[Any]]


async def run_parts(parts: dict[str, Part]) -> dict:
    """Each part in order (one connection: never concurrent); a failing part becomes {"error": ...}."""
    out: dict[str, Any] = {}
    for name, fn in parts.items():
        try:
            out[name] = await fn()
        except Exception as e:      # noqa: BLE001 — one unreadable source must not hide the rest
            out[name] = {"error": _err(e)}
            logger.warning("money_report_part_failed", part=name, error=out[name]["error"])
    return out


async def collect_week(conn, first: date, last: date, *, with_business: bool = False) -> dict:
    """Facts of a weekly money report for UTC days [first, last] (the ISO week), with the four weeks before it for
    the averages and a 14-day tail for the runways. `with_business` adds what only EX-01 needs."""
    end = last + timedelta(days=1)
    back = first - timedelta(days=28)
    parts: dict[str, Part] = {
        "forecast": lambda: forecast(conn, last.replace(day=1), utc_end_of(last)),
        "llm": lambda: llm_ledger(conn, back, end),
        "dfs": lambda: dfs_daily(conn, back, end),
        "geo": lambda: geo_daily(conn, back, end),
        "jev": lambda: jev_daily(conn, back, end),
        "aws": lambda: ce_daily(conn, back, end),
        "balances": lambda: balances(conn),
        "alerts": lambda: budget_alerts(conn, first, end),
    }
    if with_business:
        parts.update({
            "tours_live": lambda: store.kpi_series(conn, ["K-CAT-LIVE"], first - timedelta(days=7), last),
            "liveness_now": lambda: liveness_now(conn),
            "published": lambda: published_tours(conn, first, end),
            "published_prev": lambda: published_tours(conn, first - timedelta(days=7), first),
            "demand": lambda: demand(conn, first, last),
            "demand_prev": lambda: demand(conn, first - timedelta(days=7), first - timedelta(days=1)),
            "digests": lambda: digest_runs(conn, first, last),
        })
    facts = await run_parts(parts)
    facts["window"] = {"first": first.isoformat(), "last": last.isoformat(), "days": "UTC"}
    return facts


MONTH_PARTS = ("cost_by_app", "services", "llm", "vendors", "units", "budgets", "capacity")


async def collect_month(conn, first: date, last: date, now: datetime, needs: Iterable[str]) -> dict:
    """Facts of a monthly money report for the calendar month [first, last] (UTC days) and the two months before
    it (trend, 3-month average). `needs` picks the parts (MONTH_PARTS) the report renders."""
    needs = set(needs)
    unknown = needs - set(MONTH_PARTS)
    if unknown:
        raise ValueError(f"unknown month parts {sorted(unknown)}")
    months = months_back(first, 3)
    months[-1] = (first, last)          # a manual month-to-date run stops at its last day
    end = last + timedelta(days=1)
    parts: dict[str, Part] = {}
    if needs & {"cost_by_app", "units", "budgets"}:
        for f, _l in months:
            parts[f"cost_by_app:{month_key(f)}"] = (lambda f=f: cost_by_app_month(conn, f, now))
    if needs & {"services", "capacity"}:
        prev = months[1][0]
        parts["services"] = lambda: ce_services(conn, first, end)
        parts["services_prev"] = lambda: ce_services(conn, prev, first)
    if needs & {"llm", "units"}:
        parts["llm"] = lambda: llm_ledger(conn, months[0][0], end)
    if needs & {"vendors", "units"}:
        parts["dfs"] = lambda: dfs_daily(conn, months[0][0], end)
        parts["geo"] = lambda: geo_daily(conn, months[0][0], end)
        parts["jev"] = lambda: jev_daily(conn, months[0][0], end)
        parts["balances"] = lambda: balances(conn)
    if "units" in needs:
        for f, lst in months:
            parts[f"published:{month_key(f)}"] = (
                lambda f=f, lst=lst: published_tours(conn, f, lst + timedelta(days=1)))
            parts[f"demand:{month_key(f)}"] = (lambda f=f, lst=lst: demand(conn, f, lst))
    if "budgets" in needs:
        mid = min(first + timedelta(days=14), last)
        parts["forecast_mid"] = lambda: forecast(conn, first, utc_end_of(mid))
        parts["forecast_end"] = lambda: forecast(conn, first, utc_end_of(last))
        parts["ytd"] = lambda: ce_services(conn, first.replace(month=1, day=1), end)
        parts["spend_budgets"] = lambda: spend_budgets(conn)
        parts["alerts"] = lambda: budget_alerts(conn, first, end)
        if "balances" not in parts:
            parts["balances"] = lambda: balances(conn)
    if "capacity" in needs:
        parts["db"] = lambda: db_capacity(conn)
        parts["job_kpis"] = lambda: store.kpi_series(conn, ["K-JOB-FAIL", "K-JOB-QUEUEAGE"], first, last)
        parts["digests"] = lambda: digest_runs(conn, first, last)
        parts["rds"] = lambda: rds_metrics(last - timedelta(days=29), end)
    facts = await run_parts(parts)
    facts["months"] = [{"key": month_key(f), "first": f.isoformat(), "last": lst.isoformat()} for f, lst in months]
    facts["window"] = {"first": first.isoformat(), "last": last.isoformat(), "days": "UTC"}
    return facts
