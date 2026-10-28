"""AA-665 — Cost Operations reads: DataForSEO spend by job (C2) and the month-end forecast vs budget (C3).

Both are DB-only: no live Cost Explorer, DataForSEO or Jev call (the Cost Operations page loads them on every view).

    async def dfs_runs(pool, since, until, *, infer_unlinked=False, limit=200) -> dict
    async def cost_forecast(pool, month=None, *, now=None) -> dict

DFS runs (C2). ``shared.dfs_call_log.job_id`` (CIS 293) is the shared.job that was running when the call was made
(shared/dfs_client/call_log.py reads the worker's bound job id). ``dfs_runs`` groups the window's rows by job and joins
``shared.job`` for what triggered it: kind, created_by, a short scope from the payload, status, error, the cost the
handler reported (``shared.job.cost_usd``) and the job's LLM spend (``shared.llm_call_log`` by job_id). Rows with no
job (a request handler, a script, or any row written before 293) are one ``job_id = None`` group. With
``infer_unlinked`` those rows are matched, best effort, to the jobs whose run window [started_at, finished_at or
updated_at] contains the call: one candidate = "window" attribution, several = "ambiguous" (the candidate kinds are
listed), none = "none". started_at is the LAST attempt's start, and succeeded jobs are deleted after 30 days
(retention_job), so this only explains recent rows; it is a hint, never a fact.

Forecast (C3). Per provider family of the AI / third-party spend — Bedrock (with the acc3 / acc2 split), OpenAI,
Jev (TypeSafe), DataForSEO — and per AWS account bill (acc2, acc3). AA-998: the former LLM fallback account left the
ecosystem; a month that still holds its spend (llm_call_log rows or stored Cost Explorer history) shows it as one
``retired`` row (no account ID), after acc2 / acc3:

  * month to date = the month's spend so far (today's partial day included);
  * run rate = mean daily spend of the last ``RUN_RATE_DAYS`` complete days (may reach into the previous month);
  * forecast = month to date over complete days + run rate x the days left (today included); a closed month's forecast
    is its total.

Sources: LLM spend from ``shared.llm_call_daily`` (rolled-up days) + ``shared.llm_call_log`` (days not rolled up yet),
the same split as ``platform_cost.AI_SPEND_SQL``; DFS from ``shared.dfs_call_log``; AWS from the STORED Cost Explorer
snapshots (``shared.cost_explorer_snapshot`` per account and, for acc2, ``shared.cost_explorer_app_snapshot`` — per day
the newer fetch wins). Cost Explorer lags ~1-2 days and is stored only when someone or something fetched it, so an AWS
row projects from its last covered day and says how fresh it is (``covered_to``, ``fetched_at``).

Budgets: an AI provider's monthly budget = its ``shared.spend_budget`` ``global`` daily cap x the days in the month
(the Budgets tab); an AWS account's = ``AWS_MONTHLY_BUDGET_USD`` (mirrors the AWS Budgets set in AA-CIS-Infra until
AA-665 C5 reads them live; env ``COST_OPS_AWS_BUDGETS_USD`` = ``acc2=300,acc3=250`` overrides). Status: ``over`` when
the forecast exceeds the budget, ``warn`` from the row's alert percentage (default 80 %), ``ok``, ``no_budget``.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import structlog

from api.services.platform_cost import month_window, provider_family
from shared.aws_client import cost_explorer as ce
from shared.aws_client.accounts import account_label

logger = structlog.get_logger()

RUN_RATE_DAYS = 7
DEFAULT_ALERT_PCT = 80
INFER_MAX_DAYS = 31
# AWS Budgets of each account (AA-CIS-Infra accounts/aa365/observability.tf total budget, accounts/acc3-bedrock
# cost_guard.tf). Presentation only; C5 will read the live budgets.
AWS_MONTHLY_BUDGET_USD: dict[str, float] = {"acc2": 250.0, "acc3": 250.0}
AWS_BUDGET_ENV = "COST_OPS_AWS_BUDGETS_USD"

PROVIDER_LABELS = {
    "bedrock": "Amazon Bedrock (all accounts)",
    "openai": "OpenAI API",
    "jev": "Jev (TypeSafe)",
    "dfs": "DataForSEO",
    "other": "Other LLM providers",
}
# Display order of the Bedrock split and the AWS rows; the retired account only appears when it has data.
_BEDROCK_ACCOUNT_ORDER = ("acc3", "acc2", ce.RETIRED_ACCOUNT_KEY)
_AWS_ACCOUNTS = ("acc2", "acc3")

# ── C2: DataForSEO spend by job ─────────────────────────────────────────────────────────────────────────────────────

DFS_RUNS_SQL = """
    WITH d AS (
        SELECT job_id,
               count(*)                                  AS calls,
               count(*) FILTER (WHERE fetched_live)      AS live_calls,
               count(*) FILTER (WHERE cache_hit)         AS cache_hits,
               coalesce(sum(cost_usd), 0)::float         AS dfs_cost_usd,
               coalesce(sum(keyword_count), 0)::bigint   AS keywords,
               min(created_at)                           AS first_call_at,
               max(created_at)                           AS last_call_at,
               array_agg(DISTINCT endpoint ORDER BY endpoint) AS endpoints
          FROM shared.dfs_call_log
         WHERE created_at >= $1 AND created_at < $2
         GROUP BY job_id
    )
    SELECT d.*, (j.id IS NOT NULL) AS job_found, j.kind, j.status, j.created_by, j.payload,
           j.cost_usd::float AS job_reported_cost_usd, j.error, j.parent_job_id, j.attempt,
           j.created_at AS job_created_at, j.started_at, j.finished_at,
           CASE WHEN d.job_id IS NULL THEN 0::float
                ELSE (SELECT coalesce(sum(l.cost_usd), 0)::float FROM shared.llm_call_log l WHERE l.job_id = d.job_id)
           END AS llm_cost_usd
      FROM d
      LEFT JOIN shared.job j ON j.id = d.job_id
     ORDER BY d.dfs_cost_usd DESC, d.last_call_at DESC
     LIMIT $3
"""

DFS_SUMMARY_SQL = """
    SELECT count(*) AS calls, coalesce(sum(cost_usd), 0)::float AS dfs_cost_usd,
           count(*) FILTER (WHERE job_id IS NOT NULL) AS linked_calls,
           coalesce(sum(cost_usd) FILTER (WHERE job_id IS NOT NULL), 0)::float AS linked_cost_usd,
           count(DISTINCT job_id) AS jobs
      FROM shared.dfs_call_log
     WHERE created_at >= $1 AND created_at < $2
"""

# Rows without a job, matched to the jobs whose run window contains the call (best effort, see the module docstring).
DFS_INFER_SQL = """
    WITH u AS (
        SELECT id, created_at, cost_usd, fetched_live
          FROM shared.dfs_call_log
         WHERE job_id IS NULL AND created_at >= $1 AND created_at < $2
    ), j AS (
        SELECT id, kind, started_at, coalesce(finished_at, updated_at) AS ended_at
          FROM shared.job
         WHERE started_at IS NOT NULL AND started_at < $2 AND coalesce(finished_at, updated_at) >= $1
    ), m AS (
        SELECT u.id, u.created_at, u.cost_usd, u.fetched_live,
               count(j.id) AS candidates,
               (array_agg(j.id ORDER BY j.started_at DESC) FILTER (WHERE j.id IS NOT NULL))[1] AS job_id,
               coalesce(array_agg(DISTINCT j.kind) FILTER (WHERE j.id IS NOT NULL), '{}') AS kinds
          FROM u LEFT JOIN j ON u.created_at >= j.started_at AND u.created_at <= j.ended_at
         GROUP BY u.id, u.created_at, u.cost_usd, u.fetched_live
    )
    SELECT CASE WHEN candidates = 1 THEN job_id END                       AS job_id,
           CASE WHEN candidates = 0 THEN 'none'
                WHEN candidates = 1 THEN 'window' ELSE 'ambiguous' END     AS attribution,
           CASE WHEN candidates > 1 THEN kinds ELSE '{}'::text[] END       AS candidate_kinds,
           count(*)                                                        AS calls,
           count(*) FILTER (WHERE fetched_live)                            AS live_calls,
           coalesce(sum(cost_usd), 0)::float                               AS dfs_cost_usd,
           min(created_at)                                                 AS first_call_at,
           max(created_at)                                                 AS last_call_at
      FROM m
     GROUP BY 1, 2, 3
     ORDER BY dfs_cost_usd DESC
"""

JOBS_BY_ID_SQL = """
    SELECT id, kind, status, created_by, payload, cost_usd::float AS job_reported_cost_usd, error, started_at,
           finished_at
      FROM shared.job WHERE id = ANY($1::uuid[])
"""

_SCOPE_MAX_KEYS = 8
_SCOPE_MAX_CHARS = 80
_ERROR_MAX_CHARS = 300


def payload_scope(payload: Any) -> dict[str, str]:
    """A short, display-only view of a job payload: scalar values (cut to 80 chars), lists and objects as their size,
    at most 8 keys in the payload's order. Not a security boundary: the page is admin-only (spend.read)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {"payload": payload[:_SCOPE_MAX_CHARS]}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in payload.items():
        if len(out) >= _SCOPE_MAX_KEYS:
            break
        if isinstance(v, list):
            out[str(k)] = f"{len(v)} item{'s' if len(v) != 1 else ''}"
        elif isinstance(v, dict):
            out[str(k)] = f"{len(v)} field{'s' if len(v) != 1 else ''}"
        elif v is None:
            continue
        else:
            out[str(k)] = str(v)[:_SCOPE_MAX_CHARS]
    return out


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _job_view(r: Any) -> dict[str, Any]:
    error = r["error"]
    return {
        "kind": r["kind"], "status": r["status"], "created_by": r["created_by"],
        "scope": payload_scope(r["payload"]),
        "job_reported_cost_usd": round(float(r["job_reported_cost_usd"] or 0.0), 6),
        "error": error[:_ERROR_MAX_CHARS] if error else None,
        "started_at": _iso(r["started_at"]), "finished_at": _iso(r["finished_at"]),
    }


async def dfs_runs(pool: Any, since: datetime, until: datetime, *, infer_unlinked: bool = False,
                   limit: int = 200) -> dict[str, Any]:
    """DataForSEO spend in [since, until) grouped by the job that made the calls. See the module docstring."""
    if infer_unlinked and (until - since) > timedelta(days=INFER_MAX_DAYS):
        raise ValueError(f"infer_unlinked needs a window of at most {INFER_MAX_DAYS} days")
    rows = await pool.fetch(DFS_RUNS_SQL, since, until, limit)
    summary = await pool.fetchrow(DFS_SUMMARY_SQL, since, until)
    runs = []
    for r in rows:
        job_id = str(r["job_id"]) if r["job_id"] is not None else None
        run: dict[str, Any] = {
            "job_id": job_id,
            "attribution": "job" if job_id else "none",
            "job_found": bool(r["job_found"]),
            "calls": int(r["calls"]), "live_calls": int(r["live_calls"]), "cache_hits": int(r["cache_hits"]),
            "keywords": int(r["keywords"]), "dfs_cost_usd": round(float(r["dfs_cost_usd"]), 6),
            "llm_cost_usd": round(float(r["llm_cost_usd"] or 0.0), 6),
            "endpoints": list(r["endpoints"] or []),
            "first_call_at": _iso(r["first_call_at"]), "last_call_at": _iso(r["last_call_at"]),
            "job": None,
        }
        if r["job_found"]:
            run["job"] = _job_view(r)
            run["job"]["attempt"] = r["attempt"]
            run["job"]["parent_job_id"] = str(r["parent_job_id"]) if r["parent_job_id"] else None
        runs.append(run)
    s = dict(summary) if summary else {}
    out: dict[str, Any] = {
        "since": since.isoformat(), "until": until.isoformat(),
        "summary": {
            "calls": int(s.get("calls") or 0), "dfs_cost_usd": round(float(s.get("dfs_cost_usd") or 0.0), 6),
            "linked_calls": int(s.get("linked_calls") or 0),
            "linked_cost_usd": round(float(s.get("linked_cost_usd") or 0.0), 6),
            "jobs": int(s.get("jobs") or 0),
        },
        "runs": runs,
        "truncated": len(runs) >= limit,
        "inferred": None,
    }
    if infer_unlinked:
        out["inferred"] = await _infer_unlinked(pool, since, until)
    return out


async def _infer_unlinked(pool: Any, since: datetime, until: datetime) -> list[dict[str, Any]]:
    rows = await pool.fetch(DFS_INFER_SQL, since, until)
    ids = [r["job_id"] for r in rows if r["job_id"] is not None]
    jobs = {}
    if ids:
        jobs = {jr["id"]: _job_view(jr) for jr in await pool.fetch(JOBS_BY_ID_SQL, ids)}
    out = []
    for r in rows:
        job_id = r["job_id"]
        out.append({
            "job_id": str(job_id) if job_id is not None else None,
            "attribution": r["attribution"],
            "candidate_kinds": sorted(r["candidate_kinds"] or []),
            "calls": int(r["calls"]), "live_calls": int(r["live_calls"]),
            "dfs_cost_usd": round(float(r["dfs_cost_usd"]), 6),
            "first_call_at": _iso(r["first_call_at"]), "last_call_at": _iso(r["last_call_at"]),
            "job": jobs.get(job_id) if job_id is not None else None,
        })
    return out


# ── C3: month-end forecast vs budget ────────────────────────────────────────────────────────────────────────────────

# Same rolled/raw split as platform_cost.AI_SPEND_SQL, per UTC day. $1/$2 = UTC dates [start, end).
AI_DAILY_SQL = """
    WITH rolled AS (
        SELECT d.day, d.provider, d.account, sum(d.cost_usd)::float AS cost_usd
          FROM shared.llm_call_daily d
         WHERE d.day >= $1 AND d.day < $2
         GROUP BY 1, 2, 3
    ), raw AS (
        SELECT (l.created_at AT TIME ZONE 'UTC')::date AS day, l.provider, l.account,
               coalesce(sum(l.cost_usd), 0)::float AS cost_usd
          FROM shared.llm_call_log l
         WHERE l.created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
           AND l.created_at < ($2::date::timestamp AT TIME ZONE 'UTC')
           AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                            WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
         GROUP BY 1, 2, 3
    )
    SELECT day, provider, account, sum(cost_usd)::float AS cost_usd
      FROM (SELECT * FROM rolled UNION ALL SELECT * FROM raw) u
     GROUP BY day, provider, account
"""

DFS_DAILY_SQL = """
    SELECT (created_at AT TIME ZONE 'UTC')::date AS day, coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM shared.dfs_call_log
     WHERE created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
       AND created_at < ($2::date::timestamp AT TIME ZONE 'UTC')
     GROUP BY 1
"""

# Newest fetch per (account, service, day), summed per (account, day) — the External Spend / AWS bill data.
CE_DAILY_SQL = """
    SELECT account_id, period_start AS day, sum(amount_usd)::float AS amount_usd, max(fetched_at) AS fetched_at
      FROM (SELECT DISTINCT ON (account_id, service, period_start) account_id, period_start, amount_usd, fetched_at
              FROM shared.cost_explorer_snapshot
             WHERE period_start >= $1 AND period_start < $2
             ORDER BY account_id, service, period_start, fetched_at DESC) s
     GROUP BY 1, 2
"""

# acc2 by App tag (Cost per app, CIS 248): refreshed lazily by that page, often newer than the SERVICE snapshot.
CE_APP_DAILY_SQL = """
    SELECT account_id, period_start AS day, sum(amount_usd)::float AS amount_usd, max(fetched_at) AS fetched_at
      FROM (SELECT DISTINCT ON (account_id, app_tag, service, period_start)
                   account_id, period_start, amount_usd, fetched_at
              FROM shared.cost_explorer_app_snapshot
             WHERE period_start >= $1 AND period_start < $2
             ORDER BY account_id, app_tag, service, period_start, fetched_at DESC) s
     GROUP BY 1, 2
"""

BUDGETS_SQL = """
    SELECT provider, per_day_usd::float AS per_day_usd, alert_pct
      FROM shared.spend_budget WHERE scope = 'global'
"""


def aws_budgets() -> dict[str, float]:
    """AWS_MONTHLY_BUDGET_USD, overridden per account by env COST_OPS_AWS_BUDGETS_USD (``acc2=300,acc3=250``)."""
    out = dict(AWS_MONTHLY_BUDGET_USD)
    raw = os.environ.get(AWS_BUDGET_ENV, "").strip()
    for part in filter(None, (p.strip() for p in raw.split(","))):
        key, _, value = part.partition("=")
        try:
            out[key.strip()] = float(value)
        except ValueError:
            logger.warning("cost_ops_aws_budget_env_ignored", part=part[:40])
    return out


def days_in_month(month_start: date) -> int:
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (next_month - month_start).days


def project(daily: dict[date, float], month_start: date, month_end: date, complete_until: date,
            rate_window_end: Optional[date] = None, rate_days: int = RUN_RATE_DAYS,
            mean_over_present: bool = False) -> dict[str, Any]:
    """One series' projection. ``daily`` = spend per UTC day.

    ``complete_until``: the first day NOT complete (today for the current month, ``month_end`` for a closed month).
    ``rate_window_end``: exclusive end of the run-rate window (default ``complete_until``).
    ``mean_over_present``: a log source has a row for every day it spent (missing day = 0, divide by ``rate_days``);
    a Cost Explorer snapshot only has the days someone fetched (missing day = unknown, divide by the days present).
    forecast = spend of the month's complete days + rate x the days in [complete_until, month_end)."""
    rw_end = rate_window_end or complete_until
    rw_start = rw_end - timedelta(days=rate_days)
    in_window = [v for d, v in daily.items() if rw_start <= d < rw_end]
    divisor = len(in_window) if mean_over_present else rate_days
    rate = sum(in_window) / divisor if divisor else 0.0
    mtd = sum(v for d, v in daily.items() if month_start <= d < month_end)
    mtd_complete = sum(v for d, v in daily.items() if month_start <= d < min(complete_until, month_end))
    days_left = max(0, (month_end - max(complete_until, month_start)).days)
    forecast = mtd_complete + rate * days_left
    return {"mtd_usd": round(mtd, 4), "run_rate_usd_per_day": round(rate, 4), "days_left": days_left,
            "forecast_usd": round(max(forecast, mtd), 4)}


def budget_status(forecast: Optional[float], budget: Optional[float], alert_pct: int = DEFAULT_ALERT_PCT) -> str:
    if budget is None or budget <= 0:
        return "no_budget"
    if forecast is None:
        return "no_data"
    if forecast > budget:
        return "over"
    if forecast >= budget * alert_pct / 100.0:
        return "warn"
    return "ok"


def _with_budget(row: dict[str, Any], budget: Optional[float], alert_pct: Optional[int], source: str) -> dict:
    pct = alert_pct or DEFAULT_ALERT_PCT
    row["budget_usd"] = round(budget, 2) if budget is not None else None
    row["budget_source"] = source if budget is not None else None
    row["alert_pct"] = pct if budget is not None else None
    row["status"] = budget_status(row.get("forecast_usd"), budget, pct)
    row["forecast_pct_of_budget"] = (round(100.0 * row["forecast_usd"] / budget, 1)
                                     if budget and row.get("forecast_usd") is not None else None)
    return row


def _account_of(provider: Optional[str], account: Optional[str]) -> str:
    if (provider or "").startswith("bedrock"):
        acc = account or "acc2"           # an older Bedrock row without account ran on acc2 (platform_cost rule)
        # AA-998: any other account is the retired fallback account's history.
        return acc if acc in ("acc2", "acc3") else ce.RETIRED_ACCOUNT_KEY
    return account or "none"


def _add(series: dict[str, dict[date, float]], key: str, day: date, amount: float) -> None:
    s = series.setdefault(key, {})
    s[day] = s.get(day, 0.0) + amount


def _ce_series(rows: Iterable[Any], app_rows: Iterable[Any]) -> dict[str, dict[date, tuple[float, datetime]]]:
    """{account_key: {day: (amount, fetched_at)}}; for acc2 the App-tag snapshot's day wins when fetched later."""
    out: dict[str, dict[date, tuple[float, datetime]]] = {}
    for src in (rows, app_rows):
        for r in src:
            key = ce.account_key(r["account_id"]) or r["account_id"]
            cur = out.setdefault(key, {}).get(r["day"])
            if cur is None or r["fetched_at"] > cur[1]:
                out[key][r["day"]] = (float(r["amount_usd"]), r["fetched_at"])
    return out


async def cost_forecast(pool: Any, month: Optional[str] = None, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Month-end forecast vs budget per provider and AWS account. DB only. ValueError on a bad / future month."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    start, _ = month_window(month, today)
    n_days = days_in_month(start)
    month_end = start + timedelta(days=n_days)
    current = start <= today < month_end
    complete_until = today if current else month_end
    q_start = min(start, complete_until - timedelta(days=RUN_RATE_DAYS))
    q_end = min(month_end, today + timedelta(days=1))

    ai_rows = await pool.fetch(AI_DAILY_SQL, q_start, q_end)
    dfs_rows = await pool.fetch(DFS_DAILY_SQL, q_start, q_end)
    ce_rows = await pool.fetch(CE_DAILY_SQL, q_start, q_end)
    ce_app_rows = await pool.fetch(CE_APP_DAILY_SQL, q_start, q_end)
    budget_rows = await pool.fetch(BUDGETS_SQL)

    families: dict[str, dict[date, float]] = {}
    accounts: dict[str, dict[date, float]] = {}
    for r in ai_rows:
        amount = float(r["cost_usd"] or 0.0)
        fam = provider_family(r["provider"])
        _add(families, fam, r["day"], amount)
        if fam == "bedrock":
            _add(accounts, _account_of(r["provider"], r["account"]), r["day"], amount)
    for r in dfs_rows:
        _add(families, "dfs", r["day"], float(r["cost_usd"] or 0.0))

    budgets = {r["provider"]: (r["per_day_usd"], r["alert_pct"]) for r in budget_rows}
    providers = []
    for key in ("bedrock", "openai", "jev", "dfs", "other"):
        series = families.get(key, {})
        if key == "other" and not series:
            continue
        row = {"key": key, "label": PROVIDER_LABELS[key],
               **project(series, start, month_end, complete_until)}
        per_day, alert_pct = budgets.get(key, (None, None))
        monthly = per_day * n_days if per_day is not None else None
        _with_budget(row, monthly, alert_pct, f"Budgets tab: daily cap {per_day} USD x {n_days} days"
                     if per_day is not None else "")
        if key == "bedrock":
            row["accounts"] = [{"account": acc, "label": account_label(acc) or acc,
                                **project(accounts[acc], start, month_end, complete_until)}
                               for acc in sorted(accounts, key=lambda a: _BEDROCK_ACCOUNT_ORDER.index(a)
                                                 if a in _BEDROCK_ACCOUNT_ORDER else 9)]
        providers.append(row)

    aws_budget = aws_budgets()
    ce_series = _ce_series(ce_rows, ce_app_rows)
    aws = []
    retired = ce.RETIRED_ACCOUNT_KEY
    shown = _AWS_ACCOUNTS + ((retired,) if any(start <= d < month_end for d in ce_series.get(retired, {})) else ())
    for acc in shown:
        days = {d: v for d, v in ce_series.get(acc, {}).items() if d < q_end}
        row: dict[str, Any] = {"account": acc, "label": account_label(acc), "covered_from": None,
                               "covered_to": None, "fetched_at": None, "days_missing": None,
                               "days_present_in_rate_window": 0}
        in_month = [d for d in days if start <= d < month_end]
        if not days or not in_month:
            row.update(mtd_usd=None, run_rate_usd_per_day=None, days_left=None, forecast_usd=None)
        else:
            covered_to = max(in_month)
            # CE days are complete only up to its lag: project from the day after the last stored day. A fetch made
            # today stores today's partial day too: it counts in month to date but not in the run rate.
            ce_complete_until = min(covered_to + timedelta(days=1), complete_until)
            proj = project({d: v[0] for d, v in days.items()}, start, month_end,
                           complete_until=ce_complete_until, rate_window_end=ce_complete_until,
                           mean_over_present=True)
            covered_from = min(in_month)
            row.update(proj, covered_from=covered_from.isoformat(), covered_to=covered_to.isoformat(),
                       fetched_at=max(v[1] for v in days.values()).isoformat(),
                       # days of the month before covered_to with no stored figure: month to date is then too low
                       days_missing=(covered_to - start).days + 1 - len(in_month),
                       days_present_in_rate_window=sum(
                           1 for d in days
                           if ce_complete_until - timedelta(days=RUN_RATE_DAYS) <= d < ce_complete_until))
        _with_budget(row, aws_budget.get(acc), DEFAULT_ALERT_PCT, "AWS Budgets (mirrored in code until AA-665 C5)")
        aws.append(row)

    totals = {k: round(sum(p[k] for p in providers), 4) for k in ("mtd_usd", "forecast_usd")}
    logger.info("cost_ops_forecast_computed", month=start.strftime("%Y-%m"), providers=len(providers))
    return {
        "month": start.strftime("%Y-%m"),
        "period_start": start.isoformat(), "period_end_exclusive": month_end.isoformat(),
        "days_in_month": n_days, "is_current_month": current,
        "complete_days": max(0, (min(complete_until, month_end) - start).days),
        "run_rate_days": RUN_RATE_DAYS,
        "providers": providers,
        "providers_total": totals,
        "aws_accounts": aws,
        "generated_at": now.isoformat(),
        "sources": {
            "providers": "LLM gateway estimates (shared.llm_call_daily + shared.llm_call_log, as Cost per app) and "
                         "shared.dfs_call_log; the CRM's own gateway is not included",
            "aws_accounts": "stored Cost Explorer snapshots (no live call): shared.cost_explorer_snapshot, and for "
                            "the main account shared.cost_explorer_app_snapshot when fetched later",
            "budgets": "shared.spend_budget global daily cap x days in month; AWS from AWS_MONTHLY_BUDGET_USD",
            "forecast": f"month to date over complete days + mean of the last {RUN_RATE_DAYS} complete days x days "
                        "left",
        },
    }
