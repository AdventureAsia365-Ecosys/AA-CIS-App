"""AA-1003 R4 — reliability and data-quality reports: OPS-13 weekly SLO and DB performance, OPS-14 job runner health,
DATA-02 weekly cross-app reconciliation, DATA-03 schema change report, DATA-04 ledger growth and retention.

  OPS-14  weekly (ISO week, ICT days): shared.job per kind (runs, failures, retries, p95 duration vs the registry's
          expected_seconds, queue age), overdue running rows now, scheduled kinds with no run in 30 days, cost per run.
          Booking, TripPlanner and CRM job tables are not granted to cis_app: named "not readable from CIS".
  OPS-13  weekly (semi): the C1.2 objectives CIS can measure (data standards 03-scaling.md), pg_stat_statements top
          statements per role (query text only for the report's own role: cis_app has no pg_read_all_stats), slow
          statements, connection saturation (CloudWatch DatabaseConnections, one GetMetricData call, already granted
          for OPS-08; pg_stat_activity per role now), two-week misses against the previous OPS-13 run, OPS-14 summary.
  DATA-02 weekly: inquiry handoff roll-up (TP-03's contract views), Booking inbox age (booking.v_ops_inquiry_inbox,
          bk-035), TripPlanner keys without an inbox row after 24 h (tripplanner.v_ops_inquiry_keys, tp-022), the LLM
          ledger vs the stored Cost Explorer bill of the LLM account; the CRM feed and Vercel parts are not wired.
  DATA-03 event (default the last 7 closed days): migrations registered in shared.schema_versions per owner (CIS,
          tp-, bk-, crm-) and the newest version of each owner; GitHub-side checks (bot PRs, contract checks, ERD
          drift) are not wired.
  DATA-04 monthly: rows past their retention window per ledger (the retention job's own count, services/retention),
          retention job outcomes of the month, rows per day over 30 days, the 20 largest relations and their growth
          since the previous DATA-04 run (its stored output).

Stored rows and catalog reads only (cis_app), plus the one CloudWatch call of OPS-13; no LLM, no paid API.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from statistics import quantiles
from typing import Any, Iterable, Optional

from services.reports import handoff, periods, store
from services.reports import money_sources as src
from services.reports.generate import GENERATED_BY, Rendered, fmt
from services.reports.money import (KPI_HEADERS, NOT_WIRED, _d, _kpi_line, _kpi_rows, _output, failed, pct,
                                    ratio, table, usd)
from services.reports.registry import Registry, Report

UTC = timezone.utc
DEV_NOTE = "> Dev environment: load comes from development and test runs; trends only."
ICT_NOTE = "Days are ICT (UTC+7) calendar days."
SCHEDULER_PREFIX = "scheduler:"
DEAD_AFTER_DAYS = 30             # K-JOB-DEAD: a scheduled kind with no run in 30 days
FAILRATE_FLAG = 0.02             # K-JOB-FAILRATE: < 2 %
OVERDUE_FACTOR = 2               # overdue running = current attempt older than 2 x expected (shared/jobs/backlog.py)
SLOW_MEAN_MS = 200.0             # K-DB-SLOWSTMT: calls >= 20 and mean > 200 ms
SLOW_MIN_CALLS = 20
SLOW_LOG_MS = 500.0              # OPS-13 "slow > 500 ms" (mean, from pg_stat_statements)
CONN_OBJECTIVE = 0.60            # C1.2: DatabaseConnections p95 < 60 % of max_connections
ROLE_OBJECTIVE = 0.80            # C1.2: each role < 80 % of its CONNECTION LIMIT
CLAIM_MEAN_MS = 20.0             # C1.2: job claim mean < 20 ms
INBOX_OBJECTIVE_S = 300.0        # K-REC-INBOX: < 5 min
UNMATCHED_AFTER = timedelta(hours=24)
RECON_FLAG = 0.10                # K-LLM-RECON: <= 10 %
TOP_N = 20


def _header(report: Report, first: date, last: date, generated_at: datetime, title: str, note: str) -> list[str]:
    period = first.isoformat() if first == last else f"{first.isoformat()} to {last.isoformat()}"
    return [f"# {title}", "", DEV_NOTE, "",
            f"Report {report.id} ({report.name_en}), period {period}. Generated "
            f"{generated_at.strftime('%Y-%m-%d %H:%M UTC')} by the {GENERATED_BY}. {note}", ""]


def _render(reg: Registry, report: Report, first: date, last: date, generated_at: datetime, title: str,
            headline: str, kpis: list[dict], body: list[str], note: str = ICT_NOTE, **data) -> Rendered:
    md = _header(report, first, last, generated_at, title, note)
    md += ["## KPIs", ""] + table(KPI_HEADERS, _kpi_rows(reg, kpis)) + [""] + body
    out = _output(report, first, last, generated_at, headline, kpis=kpis, **data)
    return Rendered(headline[:1000], out, "\n".join(md))


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (None if v is None else str(v))


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _unreadable(facts: dict) -> dict:
    return {k: failed(v) for k, v in facts.items() if failed(v)}


def _p95(values: Iterable[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    return round(quantiles(vals, n=20, method="inclusive")[18], 2)


# ═══ OPS-14 job runner health ═══════════════════════════════════════════════════════════════════════════════════

# One row per kind for the window [$1, $2): finished rows decide failures, retries, durations and cost; started rows
# the queue age (started_at keeps the first attempt's start, so a retried row's duration includes its retries).
JOB_KINDS_SQL = """
    SELECT kind,
           count(*) FILTER (WHERE created_at >= $1 AND created_at < $2)::int AS created,
           count(*) FILTER (WHERE finished_at >= $1 AND finished_at < $2)::int AS finished,
           count(*) FILTER (WHERE finished_at >= $1 AND finished_at < $2 AND status = 'failed')::int AS failed,
           count(*) FILTER (WHERE finished_at >= $1 AND finished_at < $2
                              AND status IN ('cancelled', 'stopped_budget'))::int AS stopped,
           count(*) FILTER (WHERE finished_at >= $1 AND finished_at < $2 AND attempt > 1)::int AS retried,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at))
               FILTER (WHERE finished_at >= $1 AND finished_at < $2 AND status = 'succeeded'
                         AND started_at IS NOT NULL) AS p95_s,
           percentile_cont(0.95) WITHIN GROUP (
               ORDER BY greatest(0, extract(epoch FROM started_at - greatest(created_at, run_after))))
               FILTER (WHERE started_at >= $1 AND started_at < $2) AS queue_p95_s,
           coalesce(sum(cost_usd) FILTER (WHERE finished_at >= $1 AND finished_at < $2), 0)::float AS cost_usd
      FROM shared.job
     WHERE (created_at >= $1 AND created_at < $2) OR (finished_at >= $1 AND finished_at < $2)
        OR (started_at >= $1 AND started_at < $2)
     GROUP BY kind ORDER BY kind
"""

# The newest row of each kind in the last 90 days and whether a scheduler queued it (created_by scheduler:...).
JOB_LAST_SQL = """
    SELECT kind, max(created_at) AS last_created,
           bool_or(created_by LIKE 'scheduler:%') AS scheduled,
           max(created_at) FILTER (WHERE created_by LIKE 'scheduler:%') AS last_scheduled
      FROM shared.job
     WHERE created_at >= now() - interval '90 days'
     GROUP BY kind
"""

JOB_ACTIVE_SQL = """
    SELECT kind, status, started_at, progress ->> 'attempt_started_at' AS attempt_started_at, run_after, created_at
      FROM shared.job
     WHERE status IN ('running', 'queued')
     ORDER BY created_at
     LIMIT 2000
"""

OTHER_JOB_TABLES = ("booking.job", "tripplanner.job", "crm.job")


async def job_kinds(conn, first: date, last: date) -> list[dict]:
    start, end = periods.range_bounds(first, last)
    return [dict(r) for r in await conn.fetch(JOB_KINDS_SQL, start, end)]


async def job_last(conn) -> list[dict]:
    return [{**dict(r), "last_created": _iso(r["last_created"]), "last_scheduled": _iso(r["last_scheduled"])}
            for r in await conn.fetch(JOB_LAST_SQL)]


async def job_active(conn) -> list[dict]:
    return [dict(r) for r in await conn.fetch(JOB_ACTIVE_SQL)]


def registered_kinds() -> dict[str, int]:
    """name -> expected_seconds of every kind this process registered (the worker imports every handler)."""
    from shared.jobs import registry as jobs_registry

    return {name: int(k.expected_seconds or 600) for name, k in jobs_registry.kinds().items()}


def _ts(v: Any) -> Optional[datetime]:
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def job_health(facts: dict, expected: dict[str, int], now: datetime) -> dict:
    kinds = facts.get("job_kinds")
    if kinds is None or failed(kinds):
        return {"error": failed(kinds) or "not read"}
    rows = []
    for r in kinds:
        exp = expected.get(r["kind"])
        p95 = _f(r.get("p95_s"))
        rows.append({**r, "p95_s": None if p95 is None else round(p95, 1),
                     "queue_p95_s": None if r.get("queue_p95_s") is None else round(float(r["queue_p95_s"]), 1),
                     "expected_s": exp, "p95_ratio": ratio(p95, exp) if p95 is not None and exp else None,
                     "fail_rate": ratio(r["failed"], r["finished"]),
                     "cost_per_run": ratio(r["cost_usd"], r["finished"]) if r["cost_usd"] else None})
    finished = sum(r["finished"] for r in rows)
    failed_n = sum(r["failed"] for r in rows)
    active = facts.get("job_active")
    overdue, queued = [], {}
    if active is not None and not failed(active):
        for a in active:
            if a["status"] == "running":
                began = _ts(a.get("attempt_started_at")) or _ts(a.get("started_at"))
                exp = expected.get(a["kind"], 600)
                if began is not None and (now - began).total_seconds() > OVERDUE_FACTOR * exp:
                    overdue.append({"kind": a["kind"], "running_s": round((now - began).total_seconds()),
                                    "expected_s": exp})
            else:
                due = max(a["run_after"], a["created_at"])
                q = queued.setdefault(a["kind"], {"queued": 0, "oldest_due_s": 0})
                q["queued"] += 1
                if due <= now:
                    q["oldest_due_s"] = max(q["oldest_due_s"], round((now - due).total_seconds()))
    last = facts.get("job_last")
    dead, never = [], []
    if last is not None and not failed(last):
        cutoff = now - timedelta(days=DEAD_AFTER_DAYS)
        seen = {r["kind"] for r in last}
        for r in last:
            ls = _ts(r.get("last_scheduled"))
            if r.get("scheduled") and ls is not None and ls < cutoff:
                dead.append({"kind": r["kind"], "last_scheduled": r["last_scheduled"]})
        never = sorted(k for k in expected if k not in seen)
    return {"kinds": rows, "finished": finished, "failed": failed_n, "fail_rate": ratio(failed_n, finished),
            "over_fail": [r["kind"] for r in rows if r["fail_rate"] is not None and r["fail_rate"] >= FAILRATE_FLAG
                          and r["failed"] > 0],
            "slow": [r["kind"] for r in rows if r["p95_ratio"] is not None and r["p95_ratio"] > 1.0],
            "worst_p95_ratio": max((r["p95_ratio"] for r in rows if r["p95_ratio"] is not None), default=None),
            "overdue": overdue, "queued": queued, "dead": dead, "never_90d": never,
            "paid": [{"kind": r["kind"], "cost_usd": round(r["cost_usd"], 4), "runs": r["finished"],
                      "per_run": r["cost_per_run"]} for r in rows if r["cost_usd"]]}


def queue_age_week(rows: Any) -> Optional[float]:
    """The worst daily K-JOB-QUEUEAGE snapshot of the week (seconds)."""
    if rows is None or failed(rows):
        return None
    vals = [float(r["value"]) for r in rows if r["kpi_id"] == "K-JOB-QUEUEAGE"]
    return max(vals) if vals else None


def ops14_kpis(h: dict, queue_age: Optional[float]) -> list[dict]:
    if "error" in h:
        note = f"not readable: {h['error']}"
        return [_kpi_line(k, None, "no_data", note) for k in ("K-JOB-FAILRATE", "K-JOB-P95", "K-JOB-QUEUEAGE",
                                                               "K-JOB-DEAD")]
    return [
        _kpi_line("K-JOB-FAILRATE", h["fail_rate"], "no_data" if h["fail_rate"] is None else
                  "amber" if h["over_fail"] else "ok",
                  f"{h['failed']} of {h['finished']} finished CIS rows failed; kinds at or above 2 %: "
                  f"{', '.join(h['over_fail']) or 'none'}; Booking, TripPlanner and CRM tables not readable from CIS"),
        _kpi_line("K-JOB-P95", h["worst_p95_ratio"], "no_data" if h["worst_p95_ratio"] is None else
                  "amber" if h["slow"] else "ok",
                  f"worst p95 duration / expected; over 1.0: {', '.join(h['slow']) or 'none'}"),
        _kpi_line("K-JOB-QUEUEAGE", queue_age, "no_data" if queue_age is None else
                  "amber" if queue_age >= 300 else "ok", "worst daily snapshot of the week (all CIS kinds)"),
        _kpi_line("K-JOB-DEAD", float(len(h["dead"])), "red" if h["dead"] else "ok",
                  "scheduled kinds (queued by a scheduler in 90 days) with no scheduled run in 30 days"
                  + (f": {', '.join(d['kind'] for d in h['dead'])}" if h["dead"] else "")),
    ]


def ops14_md(h: dict) -> list[str]:
    if "error" in h:
        return [f"- Job table not readable: {h['error']}", ""]
    md = ["## Per kind (CIS shared.job)", ""]
    md += table(["Kind", "Created", "Finished", "Failed", "Stopped", "Retried", "p95 duration", "Expected",
                 "p95 / expected", "Queue age p95", "Cost per run"],
                [[r["kind"], r["created"], r["finished"], r["failed"], r["stopped"], r["retried"],
                  fmt(r["p95_s"], "seconds"), fmt(r["expected_s"], "seconds"), r["p95_ratio"],
                  fmt(r["queue_p95_s"], "seconds"), usd(r["cost_per_run"]) if r["cost_per_run"] else "—"]
                 for r in h["kinds"]]) if h["kinds"] else ["- No CIS job ran in the period."]
    md += ["", "## Overdue running rows and queue now", ""]
    md += [f"- {o['kind']}: running {o['running_s']} s (expected {o['expected_s']} s)" for o in h["overdue"]] \
        or ["- No running row past twice its expected duration."]
    md += [f"- {k}: {q['queued']} queued, oldest due {q['oldest_due_s']} s" for k, q in sorted(h["queued"].items())]
    md += ["", "## Kinds that never ran", ""]
    md += [f"- Scheduled kind with no scheduled run in {DEAD_AFTER_DAYS} days: {d['kind']} (last "
           f"{str(d['last_scheduled'])[:10]})" for d in h["dead"]] or [
        f"- Every scheduled kind ran in the last {DEAD_AFTER_DAYS} days."]
    if h["never_90d"]:
        md += [f"- Registered kinds with no row in 90 days (on demand, or unused): {', '.join(h['never_90d'])}."]
    md += ["", "## Paid-call kinds", ""]
    md += table(["Kind", "Runs", "Cost", "Cost per run"],
                [[p["kind"], p["runs"], usd(p["cost_usd"]), usd(p["per_run"])] for p in h["paid"]]) \
        if h["paid"] else ["- No job recorded a cost in the period."]
    md += ["", "## Not readable from CIS", "",
           f"- {', '.join(OTHER_JOB_TABLES)}: not granted to the CIS role (crm.job is behind the crm.ops.v1 feed). "
           "Deploy-gate blocks and overrides live in the GitHub Deploy Dev runs: not wired.", ""]
    return md


async def collect_jobs(conn, first: date, last: date) -> dict:
    return await src.run_parts({
        "job_kinds": lambda: job_kinds(conn, first, last),
        "job_last": lambda: job_last(conn),
        "job_active": lambda: job_active(conn),
        "queue_kpi": lambda: store.kpi_series(conn, ["K-JOB-QUEUEAGE"], first, last),
    })


def render_ops14(reg, report, first, last, facts, generated_at, expected: Optional[dict] = None) -> Rendered:
    h = job_health(facts, expected if expected is not None else registered_kinds(), generated_at)
    qa = queue_age_week(facts.get("queue_kpi"))
    kpis = ops14_kpis(h, qa)
    headline = (f"Job runner {first.isoformat()} to {last.isoformat()}: "
                + (f"not readable ({h['error']})" if "error" in h else
                   f"{h['finished']} CIS rows finished, failure rate {pct(h['fail_rate'])}, {len(h['overdue'])} "
                   f"overdue, {len(h['dead'])} dead kind(s)"))
    return _render(reg, report, first, last, generated_at, f"Job runner health {first.isoformat()} to "
                   f"{last.isoformat()}", headline, kpis, ops14_md(h), jobs=h, sources_unreadable=_unreadable(facts))


# ═══ OPS-13 weekly SLO and DB performance ═══════════════════════════════════════════════════════════════════════

_DB = "(SELECT oid FROM pg_database WHERE datname = current_database())"
STMT_COLS = f"""r.rolname AS role, s.calls::bigint AS calls, round(s.total_exec_time::numeric, 1)::float AS total_ms,
           round(s.mean_exec_time::numeric, 2)::float AS mean_ms, s.rows::bigint AS rows,
           CASE WHEN r.rolname = current_user THEN left(regexp_replace(s.query, '\\s+', ' ', 'g'), 240) END AS query
      FROM public.pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid
     WHERE s.dbid = {_DB}"""
TOP_TOTAL_SQL = f"SELECT {STMT_COLS} ORDER BY s.total_exec_time DESC LIMIT {TOP_N}"
TOP_MEAN_SQL = f"SELECT {STMT_COLS} AND s.calls >= {SLOW_MIN_CALLS} ORDER BY s.mean_exec_time DESC LIMIT {TOP_N}"
SLOW_BY_ROLE_SQL = f"""
    SELECT r.rolname AS role, count(*)::int AS statements,
           count(*) FILTER (WHERE s.calls >= {SLOW_MIN_CALLS} AND s.mean_exec_time > {SLOW_MEAN_MS})::int AS slow,
           count(*) FILTER (WHERE s.calls >= {SLOW_MIN_CALLS} AND s.mean_exec_time > {SLOW_LOG_MS})::int AS over_500ms
      FROM public.pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid
     WHERE s.dbid = {_DB}
     GROUP BY 1 ORDER BY 1
"""
# The CIS job claim (shared/jobs/queue.py claim): its UPDATE ... FOR UPDATE SKIP LOCKED statement, own role only.
CLAIM_SQL = f"""
    SELECT max(s.mean_exec_time)::float AS mean_ms, sum(s.calls)::bigint AS calls
      FROM public.pg_stat_statements s
     WHERE s.dbid = {_DB} AND s.userid = (SELECT oid FROM pg_roles WHERE rolname = current_user)
       AND s.query LIKE 'UPDATE shared.job SET status = %running%' AND s.query LIKE '%SKIP LOCKED%'
"""
STATS_RESET_SQL = "SELECT stats_reset FROM public.pg_stat_statements_info"
ROLE_CONN_SQL = """
    SELECT r.rolname AS role, r.rolconnlimit AS conn_limit,
           (SELECT count(*) FROM pg_stat_activity a WHERE a.usename = r.rolname)::int AS connections
      FROM pg_roles r
     WHERE r.rolcanlogin AND r.rolconnlimit > 0
     ORDER BY 1
"""
PREVIOUS_RUN_SQL = """
    SELECT output FROM shared.report_run
     WHERE report_id = $1 AND status = 'succeeded' AND period_end < $2
     ORDER BY period_end DESC, created_at DESC
     LIMIT 1
"""


async def statements(conn) -> dict:
    return {"top_total": [dict(r) for r in await conn.fetch(TOP_TOTAL_SQL)],
            "top_mean": [dict(r) for r in await conn.fetch(TOP_MEAN_SQL)],
            "by_role": [dict(r) for r in await conn.fetch(SLOW_BY_ROLE_SQL)],
            "claim": dict(await conn.fetchrow(CLAIM_SQL) or {}),
            "stats_reset": _iso(await conn.fetchval(STATS_RESET_SQL))}


async def role_connections(conn) -> dict:
    rows = [dict(r) for r in await conn.fetch(ROLE_CONN_SQL)]
    return {"roles": rows, "max_connections": int(await conn.fetchval("SELECT current_setting('max_connections')"))}


async def previous_output(conn, report_id: str, before: date) -> Optional[dict]:
    v = await conn.fetchval(PREVIOUS_RUN_SQL, report_id, before)
    return json.loads(v) if isinstance(v, str) else v


async def collect_ops13(conn, first: date, last: date) -> dict:
    facts = await src.run_parts({
        "statements": lambda: statements(conn),
        "roles": lambda: role_connections(conn),
        "rds": lambda: src.rds_metrics(first, last + timedelta(days=1)),
        "previous": lambda: previous_output(conn, "OPS-13", first),
    })
    facts.update(await collect_jobs(conn, first, last))
    return facts


def objectives(facts: dict) -> list[dict]:
    """The C1.2 objectives with a value this week (measured) and the ones CIS cannot measure yet."""
    out: list[dict] = []
    rds, roles, st = facts.get("rds") or {}, facts.get("roles"), facts.get("statements")
    max_conn = None if roles is None or failed(roles) else roles.get("max_connections")
    conn_series = [float(v) for _t, v in (rds.get("connections") or [])] if not failed(rds) else []
    p95 = _p95(conn_series)
    if p95 is not None and max_conn:
        value = round(p95 / max_conn, 4)
        out.append({"id": "conn_saturation", "name": "DatabaseConnections p95 / max_connections",
                    "objective": "< 60 %", "value": value, "met": value < CONN_OBJECTIVE})
    else:
        out.append({"id": "conn_saturation", "name": "DatabaseConnections p95 / max_connections",
                    "objective": "< 60 %", "value": None, "met": None,
                    "why": rds.get("unavailable") or failed(rds) or "no CloudWatch datapoint"})
    if roles is not None and not failed(roles):
        worst = max((ratio(r["connections"], r["conn_limit"]) or 0.0 for r in roles["roles"]), default=None)
        out.append({"id": "role_limits", "name": "Connections per role / its CONNECTION LIMIT (now)",
                    "objective": "< 80 % each", "value": worst,
                    "met": None if worst is None else worst < ROLE_OBJECTIVE})
    claim = (st or {}).get("claim") if st is not None and not failed(st) else None
    if claim and claim.get("mean_ms") is not None:
        out.append({"id": "job_claim", "name": "CIS job claim mean", "objective": "< 20 ms",
                    "value": round(float(claim["mean_ms"]), 2), "met": float(claim["mean_ms"]) < CLAIM_MEAN_MS})
    else:
        out.append({"id": "job_claim", "name": "CIS job claim mean", "objective": "< 20 ms", "value": None,
                    "met": None, "why": failed(st) or "claim statement not in pg_stat_statements"})
    for oid, name, obj in (("tenant_reads", "CIS tenant API reads DB time p95", "< 150 ms"),
                           ("admin_pages", "CIS admin list and cost pages p95", "< 1 s"),
                           ("tp_browse", "TripPlanner browse reads p95", "< 300 ms"),
                           ("bk_api", "Booking API p95", "< 100 ms"),
                           ("db_errors", "DB error rate / too many connections", "< 0.5 %; 0")):
        out.append({"id": oid, "name": name, "objective": obj, "value": None, "met": None,
                    "why": "needs Performance Insights or the postgresql log export (not on Dev)"})
    return out


def two_week_misses(objs: list[dict], previous: Any) -> list[str]:
    if not isinstance(previous, dict):
        return []
    prev_missed = {o.get("id") for o in previous.get("objectives") or [] if o.get("met") is False}
    return [o["id"] for o in objs if o.get("met") is False and o["id"] in prev_missed]


def ops13_kpis(objs: list[dict], misses: list[str], st: Any, roles: Any) -> list[dict]:
    measured = [o for o in objs if o["met"] is not None]
    met = sum(1 for o in measured if o["met"])
    slow = None if st is None or failed(st) else sum(r["slow"] for r in st["by_role"])
    conn = next((o for o in objs if o["id"] == "conn_saturation"), {})
    role_note = ""
    if roles is not None and not failed(roles):
        role_note = "; now: " + ", ".join(f"{r['role']} {r['connections']}/{r['conn_limit']}" for r in roles["roles"])
    return [
        _kpi_line("K-SLI-MET", float(met) if measured else None, "no_data" if not measured else
                  "ok" if met == len(measured) else "amber",
                  f"{met} of {len(measured)} measured objectives met; {len(objs) - len(measured)} not measurable yet"),
        _kpi_line("K-SLI-2WEEK", float(len(misses)), "red" if misses else "ok",
                  ("raise to the tech lead with the query and a fix: " + ", ".join(misses)) if misses else
                  "no objective missed two weeks running"),
        _kpi_line("K-DB-SLOWSTMT", None if slow is None else float(slow), "no_data" if slow is None else "info",
                  f"statements with >= {SLOW_MIN_CALLS} calls and mean > {SLOW_MEAN_MS:.0f} ms, all roles, since the "
                  "last statistics reset"),
        _kpi_line("K-DB-CONN", conn.get("value"), "no_data" if conn.get("value") is None else
                  "ok" if conn.get("met") else "amber", (conn.get("why") or "hourly maximums of the week") + role_note),
    ]


def statements_md(st: Any) -> list[str]:
    if st is None or failed(st):
        return [f"- pg_stat_statements not readable: {failed(st) or 'not read'}", ""]

    def rows(items):
        return [[r["role"], r["calls"], f"{r['total_ms']:,.0f} ms", f"{r['mean_ms']:,.1f} ms",
                 (r.get("query") or "(another role's statement: text hidden)").replace("|", "/")[:160]]
                for r in items]
    md = [f"Cumulative since the last statistics reset ({st.get('stats_reset') or 'unknown'}); the text of other "
          "roles' statements is hidden from the report's role.", "", "### By total time", ""]
    md += table(["Role", "Calls", "Total", "Mean", "Statement"], rows(st["top_total"]))
    md += ["", f"### By mean time (>= {SLOW_MIN_CALLS} calls)", ""]
    md += table(["Role", "Calls", "Total", "Mean", "Statement"], rows(st["top_mean"]))
    md += ["", "### Slow statements per role", ""]
    md += table(["Role", "Statements", f"Mean > {SLOW_MEAN_MS:.0f} ms", f"Mean > {SLOW_LOG_MS:.0f} ms"],
                [[r["role"], r["statements"], r["slow"], r["over_500ms"]] for r in st["by_role"]])
    return md + ["", "Lock waits and the > 500 ms slow log need the postgresql log export: not wired.", ""]


def render_ops13(reg, report, first, last, facts, generated_at, expected: Optional[dict] = None) -> Rendered:
    objs = objectives(facts)
    misses = two_week_misses(objs, facts.get("previous") if not failed(facts.get("previous")) else None)
    kpis = ops13_kpis(objs, misses, facts.get("statements"), facts.get("roles"))
    h = job_health(facts, expected if expected is not None else registered_kinds(), generated_at)
    measured = [o for o in objs if o["met"] is not None]
    headline = (f"SLO and DB {first.isoformat()} to {last.isoformat()}: {sum(1 for o in measured if o['met'])} of "
                f"{len(measured)} measured objectives met, {len(misses)} two-week miss(es)")
    body = ["## SLI table (C1.2 objectives)", ""]
    body += table(["Objective", "Target", "Measured", "Met"],
                  [[o["name"], o["objective"], "—" if o["value"] is None else
                    (pct(o["value"]) if o["id"] in ("conn_saturation", "role_limits") else f"{o['value']} ms"),
                    "yes" if o["met"] else "no" if o["met"] is False else f"not measured ({o.get('why')})"]
                   for o in objs])
    body += ["", "## Two-week misses", ""]
    body += [f"- {m}: missed this week and in the previous OPS-13 run. NARRATIVE (to write before approval): the "
             "offending query and a proposed fix." for m in misses] or ["- None."]
    body += ["", "## Top statements (pg_stat_statements)", ""] + statements_md(facts.get("statements"))
    body += ["## Job runner health (OPS-14 summary)", ""]
    body += [f"- Not readable: {h['error']}"] if "error" in h else [
        f"- {h['finished']} CIS rows finished, failure rate {pct(h['fail_rate'])}; kinds at or above 2 %: "
        f"{', '.join(h['over_fail']) or 'none'}; slower than expected: {', '.join(h['slow']) or 'none'}; "
        f"{len(h['overdue'])} overdue running row(s) now. Full table: OPS-14."]
    body.append("")
    return _render(reg, report, first, last, generated_at, f"SLO and DB performance {first.isoformat()} to "
                   f"{last.isoformat()} (OPS-13 draft)", headline, kpis, body, draft=True, objectives=objs,
                   two_week_misses=misses, statements=facts.get("statements"), roles=facts.get("roles"),
                   sources_unreadable=_unreadable(facts))


# ═══ DATA-02 weekly cross-app reconciliation ════════════════════════════════════════════════════════════════════

INBOX_SQL = """
    SELECT status, count(*)::int AS n,
           count(*) FILTER (WHERE processed_at IS NULL)::int AS unprocessed,
           count(*) FILTER (WHERE processed_at IS NOT NULL AND NOT has_trip_case
                              AND received_at < now() - interval '24 hours')::int AS no_case_after_24h
      FROM booking.v_ops_inquiry_inbox
     WHERE received_at >= $1 AND received_at < $2
     GROUP BY status ORDER BY status
"""
INBOX_AGE_SQL = """
    SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM now() - received_at))
               FILTER (WHERE processed_at IS NULL) AS unprocessed_age_p95_s,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - received_at))
               FILTER (WHERE processed_at IS NOT NULL AND received_at >= $1 AND received_at < $2) AS lag_p95_s
      FROM booking.v_ops_inquiry_inbox
"""
# TripPlanner inquiries of the window older than 24 h with no Booking inbox row of the same key (both views keep 30
# days; keys only, no payload).
UNMATCHED_SQL = """
    SELECT count(*)::int AS unmatched, min(k.created_at) AS oldest
      FROM tripplanner.v_ops_inquiry_keys k
     WHERE k.created_at >= $1 AND k.created_at < $2 AND k.created_at < now() - interval '24 hours'
       AND NOT EXISTS (SELECT 1 FROM booking.v_ops_inquiry_inbox b WHERE b.idempotency_key = k.idempotency_key)
"""
KEYS_SQL = """
    SELECT count(*)::int AS n FROM tripplanner.v_ops_inquiry_keys WHERE created_at >= $1 AND created_at < $2
"""


async def inbox(conn, first: date, last: date) -> dict:
    start, end = periods.range_bounds(first, last)
    try:
        status = [dict(r) for r in await conn.fetch(INBOX_SQL, start, end)]
        age = dict(await conn.fetchrow(INBOX_AGE_SQL, start, end) or {})
    except handoff.CONTRACT_ERRORS as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"status": status, **{k: _f(v) for k, v in age.items()}}


async def unmatched(conn, first: date, last: date) -> dict:
    start, end = periods.range_bounds(first, last)
    try:
        row = dict(await conn.fetchrow(UNMATCHED_SQL, start, end) or {})
        keys = await conn.fetchval(KEYS_SQL, start, end)
    except handoff.CONTRACT_ERRORS as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"unmatched": int(row.get("unmatched") or 0), "keys": int(keys or 0), "oldest": _iso(row.get("oldest"))}


async def handoff_week(conn, first: date, last: date) -> dict:
    try:
        days = await handoff.read_days(conn, first, last)
    except handoff.CONTRACT_ERRORS as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"totals": handoff.totals(days), "days": [d.as_dict() for d in days]}


async def collect_data02(conn, first: date, last: date) -> dict:
    end = last + timedelta(days=1)
    facts = await src.run_parts({
        "handoff": lambda: handoff_week(conn, first, last),
        "inbox": lambda: inbox(conn, first, last),
        "unmatched": lambda: unmatched(conn, first, last),
        "aws": lambda: src.ce_daily(conn, first, end),
        "llm": lambda: src.llm_ledger(conn, first, end),
    })
    facts["window"] = {"first": first.isoformat(), "last": last.isoformat()}
    return facts


def llm_recon(facts: dict, first: date, last: date) -> dict:
    """Stored Cost Explorer bill of the LLM account (acc3, an LLM-only account) vs the gateway ledger rows of acc3,
    over the UTC days Cost Explorer has data for (shadow calls are billed, so they count)."""
    aws, llm = facts.get("aws"), facts.get("llm")
    if aws is None or failed(aws) or llm is None or failed(llm):
        return {"error": failed(aws) or failed(llm) or "not read"}
    bill = {}
    for r in aws:
        if r["account"] == "acc3" and first <= _d(r["day"]) <= last:
            bill[_d(r["day"])] = bill.get(_d(r["day"]), 0.0) + float(r["amount_usd"] or 0)
    days = set(bill)
    est = sum(float(r["cost_usd"] or 0) for r in llm if r.get("account") == "acc3" and _d(r["day"]) in days)
    ce = round(sum(bill.values()), 4)
    return {"days": len(days), "ce_usd": ce, "ledger_usd": round(est, 4),
            "ratio": None if not ce else round(abs(ce - est) / ce, 4)}


def data02_kpis(ho: dict, ib: Any, um: Any, rc: dict) -> list[dict]:
    out = []
    if ib is None or failed(ib):
        out.append(_kpi_line("K-REC-INBOX", None, "no_data", f"not readable: {failed(ib) or 'not read'}"))
    else:
        worst = max((v for v in (ib.get("unprocessed_age_p95_s"), ib.get("lag_p95_s")) if v is not None),
                    default=None)
        out.append(_kpi_line("K-REC-INBOX", None if worst is None else round(worst, 1),
                             "no_data" if worst is None else "amber" if worst >= INBOX_OBJECTIVE_S else "ok",
                             f"unprocessed age p95 {fmt(ib.get('unprocessed_age_p95_s'), 'seconds')}, processing lag "
                             f"p95 {fmt(ib.get('lag_p95_s'), 'seconds')} (booking.v_ops_inquiry_inbox)"))
    if um is None or failed(um):
        out.append(_kpi_line("K-REC-UNMATCHED", None, "no_data", f"not readable: {failed(um) or 'not read'}"))
    else:
        out.append(_kpi_line("K-REC-UNMATCHED", float(um["unmatched"]), "red" if um["unmatched"] else "ok",
                             f"of {um['keys']} TripPlanner inquiries this week, older than 24 h with no Booking inbox "
                             "row"))
    out.append(_kpi_line("K-REC-FEEDLAG", None, NOT_WIRED,
                         "booking.outbox / crm.booking_feed_state are not granted to the CIS role"))
    out.append(_kpi_line("K-LLM-RECON", rc.get("ratio"), "no_data" if rc.get("ratio") is None else
                         "amber" if rc["ratio"] > RECON_FLAG else "ok",
                         f"not readable: {rc['error']}" if "error" in rc else
                         f"Cost Explorer {usd(rc['ce_usd'])} vs gateway {usd(rc['ledger_usd'])} over {rc['days']} "
                         "day(s) with a stored bill"))
    return out


def render_data02(reg, report, first, last, facts, generated_at) -> Rendered:
    ho = facts.get("handoff") or {"error": "not read"}
    ib, um = facts.get("inbox"), facts.get("unmatched")
    rc = llm_recon(facts, first, last)
    kpis = data02_kpis(ho, ib, um, rc)
    flagged = [k["id"] for k in kpis if k["status"] in ("red", "amber")]
    headline = (f"Reconciliation {first.isoformat()} to {last.isoformat()}: "
                + (f"flagged {', '.join(flagged)}" if flagged else "nothing flagged"))
    body = ["## Inquiry handoff, TripPlanner to Booking (weekly roll-up of TP-03)", ""]
    if failed(ho):
        body += [f"- Contract views not readable: {failed(ho)}"]
    else:
        t = ho["totals"]
        body += [f"- Sent {t['sent']}, received {t['received']}, gap {t['gap']}, parked {t['parked']}, failed "
                 f"{t['failed']}, trip cases {t['cases']} (UTC days)."]
    body += ["", "## Booking inbox backlog and age", ""]
    if ib is None or failed(ib):
        body += [f"- Not readable: {failed(ib) or 'not read'}"]
    else:
        body += table(["Status", "Received this week", "Unprocessed", "Processed, no trip case after 24 h"],
                      [[r["status"], r["n"], r["unprocessed"], r["no_case_after_24h"]] for r in ib["status"]]) \
            if ib["status"] else ["- No inbox row received this week."]
    body += ["", "## Unmatched inquiries", ""]
    body += [f"- Not readable: {failed(um) or 'not read'}"] if um is None or failed(um) else [
        f"- {um['unmatched']} of {um['keys']} TripPlanner inquiries older than 24 h have no Booking inbox row"
        + (f" (oldest {str(um['oldest'])[:16]})." if um["unmatched"] else ".")]
    body += ["", "## LLM ledger vs the bill", ""]
    body += [f"- Not readable: {rc['error']}"] if "error" in rc else [
        f"- LLM account: Cost Explorer {usd(rc['ce_usd'])}, gateway ledger {usd(rc['ledger_usd'])}, gap "
        f"{pct(rc['ratio'])} over {rc['days']} UTC day(s) with a stored bill (target <= 10 %)."]
    body += ["", "## Not wired", "",
             "- Tour lifecycle pull lag (bk_tour_lifecycle_pull) and the Booking supply feed to the CRM: Booking and "
             "CRM tables are not granted to the CIS role.",
             "- crm.ops.v1 feed freshness: the feed is read per admin request with the staff token, not by the worker.",
             "- Vercel production commit vs main: no Vercel read in the worker.", ""]
    return _render(reg, report, first, last, generated_at, f"Cross-app reconciliation {first.isoformat()} to "
                   f"{last.isoformat()}", headline, kpis, body, ICT_NOTE + " Handoff and money are per UTC day.",
                   handoff=ho, inbox=ib, unmatched=um, llm_recon=rc, sources_unreadable=_unreadable(facts))


# ═══ DATA-03 schema and contract change report ══════════════════════════════════════════════════════════════════

SCHEMA_VERSIONS_SQL = "SELECT version, description, applied_at FROM shared.schema_versions"
_OWNER = (("tp-", "tripplanner"), ("bk-", "booking"), ("crm-", "crm"))


def owner_of(version: str) -> str:
    for prefix, owner in _OWNER:
        if version.startswith(prefix):
            return owner
    return "cis" if re.match(r"^\d", version or "") else "other"


def _vkey(version: str) -> tuple:
    m = re.search(r"(\d+)", version or "")
    return (int(m.group(1)) if m else -1, version)


async def schema_versions(conn) -> list[dict]:
    return [{"version": r["version"], "description": (r["description"] or "")[:160],
             "applied_at": r["applied_at"]} for r in await conn.fetch(SCHEMA_VERSIONS_SQL)]


def schema_changes(rows: Any, first: date, last: date) -> dict:
    if rows is None or failed(rows):
        return {"error": failed(rows) or "not read"}
    start, end = periods.range_bounds(first, last)
    applied = sorted((r for r in rows if r["applied_at"] is not None and start <= r["applied_at"] < end),
                     key=lambda r: r["applied_at"])
    owners: dict[str, dict] = {}
    for r in rows:
        o = owners.setdefault(owner_of(r["version"]), {"latest": None, "applied": 0, "registered": 0})
        o["registered"] += 1
        if o["latest"] is None or _vkey(r["version"]) > _vkey(o["latest"]):
            o["latest"] = r["version"]
    for r in applied:
        owners[owner_of(r["version"])]["applied"] += 1
    return {"applied": [{**r, "owner": owner_of(r["version"]), "applied_at": _iso(r["applied_at"])}
                        for r in applied], "owners": owners}


def render_data03(reg, report, first, last, facts, generated_at) -> Rendered:
    ch = schema_changes(facts.get("versions"), first, last)
    n = None if "error" in ch else len(ch["applied"])
    kpis = [
        _kpi_line("K-SCH-APPLIED", None if n is None else float(n), "no_data" if n is None else "info",
                  f"not readable: {ch['error']}" if n is None else
                  ", ".join(f"{o} {v['applied']}" for o, v in sorted(ch["owners"].items()))),
        _kpi_line("K-SCH-BREAKING", None, NOT_WIRED, "contract check results are GitHub workflow runs (api-contract, "
                                                     "contracts): not read by the worker"),
        _kpi_line("K-SCH-REFLAG", None, NOT_WIRED, "the db-schema-reference bot PRs are on GitHub: not read by the "
                                                   "worker"),
    ]
    headline = (f"Schema changes {first.isoformat()} to {last.isoformat()}: "
                + (f"not readable ({ch['error']})" if n is None else f"{n} migration(s) registered"))
    body = ["## Migrations registered per owner", ""]
    if n is None:
        body += [f"- Not readable: {ch['error']}", ""]
    else:
        body += table(["Owner", "Registered in the period", "Newest version", "Versions registered"],
                      [[o, v["applied"], v["latest"], v["registered"]] for o, v in sorted(ch["owners"].items())])
        body += ["", "## Applied in the period", ""]
        body += table(["Applied (UTC)", "Owner", "Version", "Description"],
                      [[str(r["applied_at"])[:16], r["owner"], r["version"], r["description"].replace("|", "/")]
                       for r in ch["applied"]]) if ch["applied"] else ["- No migration registered in the period."]
        body.append("")
    body += ["## Not wired", "",
             "- bot/db-schema-reference PRs and drift status, contract checks (docs/contracts, api-contract) and ERD "
             "drift live in GitHub (root repo and AA-CIS-Infra workflow runs); the worker does not read GitHub.", ""]
    return _render(reg, report, first, last, generated_at, f"Schema and contract changes {first.isoformat()} to "
                   f"{last.isoformat()}", headline, kpis, body, ICT_NOTE, changes=ch,
                   sources_unreadable=_unreadable(facts))


# ═══ DATA-04 ledger growth and retention ════════════════════════════════════════════════════════════════════════

# Ledgers named by DATA-04 (sizes from the catalog, readable for every schema); row counts for growth only where the
# CIS role may read the table.
LEDGERS = ("shared.llm_call_log", "shared.llm_call_daily", "shared.job", "shared.decision_log",
           "shared.tenant_api_usage", "shared.tenant_api_usage_monthly", "shared.cost_explorer_snapshot",
           "booking.job", "booking.inbox_events")
GROWTH_COLUMN = {"shared.llm_call_log": "created_at", "shared.job": "created_at", "shared.decision_log": "created_at",
                 "shared.tenant_api_usage": "called_at", "shared.cost_explorer_snapshot": "fetched_at"}
SIZES_SQL = """
    SELECT format('%s.%s', n.nspname, c.relname) AS name, greatest(c.reltuples, 0)::bigint AS rows_estimate,
           pg_total_relation_size(c.oid)::bigint AS bytes
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE format('%s.%s', n.nspname, c.relname) = ANY($1::text[])
"""
TOP_SQL = f"""
    SELECT format('%s.%s', n.nspname, c.relname) AS name, pg_total_relation_size(c.oid)::bigint AS bytes,
           greatest(c.reltuples, 0)::bigint AS rows_estimate
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'm', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
     ORDER BY 2 DESC LIMIT {TOP_N}
"""
RETENTION_RUNS_SQL = """
    SELECT kind, status, coalesce(result ->> 'mode', '') AS mode, created_by, finished_at
      FROM shared.job
     WHERE kind = ANY($1::text[]) AND created_at >= $2 AND created_at < $3
     ORDER BY finished_at NULLS LAST
"""


async def overdue(conn) -> list[dict]:
    """Rows past each retention window, counted the way the retention job counts its candidates (rows a foreign
    key still references are kept by design and not counted)."""
    from services.retention import runner
    from services.retention.policy import SPECS

    out = []
    for spec in SPECS.values():
        refs = await runner.fk_refs(conn, spec.table)
        for part in spec.parts:
            cut = await runner.cutoff(conn, part.window_days)
            res = await runner.count_part(conn, spec, part, refs, cut)
            out.append({"kind": spec.kind, "table": spec.table, "part": part.name, "window_days": part.window_days,
                        "overdue": res.candidates, "oldest": res.oldest})
    return out


async def growth(conn) -> dict:
    out = {}
    for table_name, col in GROWTH_COLUMN.items():
        n = await conn.fetchval(f"SELECT count(*) FROM {table_name} WHERE {col} >= now() - interval '30 days'")
        out[table_name] = round(int(n or 0) / 30, 1)
    return out


async def sizes(conn) -> dict:
    return {"ledgers": [dict(r) for r in await conn.fetch(SIZES_SQL, list(LEDGERS))],
            "top": [dict(r) for r in await conn.fetch(TOP_SQL)]}


async def retention_runs(conn, first: date, last: date) -> list[dict]:
    from services.retention.policy import SPECS

    start, end = periods.range_bounds(first, last)
    return [{**dict(r), "finished_at": _iso(r["finished_at"])}
            for r in await conn.fetch(RETENTION_RUNS_SQL, list(SPECS), start, end)]


async def collect_data04(conn, first: date, last: date) -> dict:
    facts = await src.run_parts({
        "overdue": lambda: overdue(conn),
        "growth": lambda: growth(conn),
        "sizes": lambda: sizes(conn),
        "runs": lambda: retention_runs(conn, first, last),
        "previous": lambda: previous_output(conn, "DATA-04", first),
    })
    facts["window"] = {"first": first.isoformat(), "last": last.isoformat()}
    return facts


def retention_view(facts: dict) -> dict:
    from services.retention.policy import SPECS

    runs = facts.get("runs")
    by_kind: dict[str, dict] = {k: {"runs": 0, "succeeded": 0, "failed": 0, "live": 0, "dry_run": 0, "held": 0}
                                for k in SPECS}
    if runs is not None and not failed(runs):
        for r in runs:
            k = by_kind.setdefault(r["kind"], {"runs": 0, "succeeded": 0, "failed": 0, "live": 0, "dry_run": 0,
                                               "held": 0})
            k["runs"] += 1
            if r["status"] == "succeeded":
                k["succeeded"] += 1
                if r["mode"] in ("live", "dry_run", "held"):
                    k[r["mode"]] += 1
            elif r["status"] in ("failed", "cancelled", "stopped_budget"):
                k["failed"] += 1
    total = sum(v["runs"] for v in by_kind.values())
    ok = sum(v["succeeded"] for v in by_kind.values())
    return {"by_kind": by_kind, "runs": total, "succeeded": ok, "ratio": ratio(ok, total),
            "error": failed(runs) if runs is not None and failed(runs) else None}


def size_growth(facts: dict) -> list[dict]:
    sz = facts.get("sizes")
    if sz is None or failed(sz):
        return []
    prev = facts.get("previous")
    before = {}
    if isinstance(prev, dict):
        before = {t["name"]: t.get("bytes") for t in (prev.get("sizes") or {}).get("top") or []}
    return [{**t, "previous_bytes": before.get(t["name"]),
             "growth": ratio(t["bytes"] - before[t["name"]], before[t["name"]]) if before.get(t["name"]) else None}
            for t in sz["top"]]


def _mb(b: Any) -> str:
    return "—" if b is None else f"{int(b) / 1_048_576:,.1f} MB"


def render_data04(reg, report, first, last, facts, generated_at) -> Rendered:
    od, gr, sz = facts.get("overdue"), facts.get("growth"), facts.get("sizes")
    rv = retention_view(facts)
    top = size_growth(facts)
    od_total = None if od is None or failed(od) else sum(r["overdue"] for r in od)
    kpis = [
        _kpi_line("K-RET-OVERDUE", None if od_total is None else float(od_total),
                  "no_data" if od_total is None else "amber" if od_total else "ok",
                  f"not readable: {failed(od)}" if od_total is None else
                  "rows a retention run would remove now (rows still referenced are kept by design)"),
        _kpi_line("K-RET-RUNS", rv["ratio"], "no_data" if rv["ratio"] is None else
                  "ok" if rv["ratio"] >= 1.0 else "amber",
                  f"{rv['succeeded']} of {rv['runs']} CIS retention runs succeeded; Booking retention kinds "
                  "(booking.job) not readable from CIS"),
        _kpi_line("K-RET-GROWTH", None if gr is None or failed(gr) else float(sum(gr.values())),
                  "no_data" if gr is None or failed(gr) else "info", "rows a day over 30 days, CIS ledgers summed"),
    ]
    headline = (f"Ledgers and retention {first.isoformat()} to {last.isoformat()}: "
                f"{'—' if od_total is None else f'{od_total:,}'} row(s) past their window, retention runs "
                f"{pct(rv['ratio'])} succeeded")
    body = ["## Ledgers: size and growth", ""]
    if sz is None or failed(sz):
        body += [f"- Sizes not readable: {failed(sz) or 'not read'}"]
    else:
        g = gr if isinstance(gr, dict) and not failed(gr) else {}
        body += table(["Ledger", "Rows (estimate)", "Size", "Rows a day (30 days)"],
                      [[r["name"], f"{r['rows_estimate']:,}", _mb(r["bytes"]),
                        g.get(r["name"], "not readable from CIS" if r["name"].startswith("booking.") else "—")]
                       for r in sorted(sz["ledgers"], key=lambda r: r["name"])])
    body += ["", "## Rows older than their window", ""]
    body += [f"- Not readable: {failed(od) or 'not read'}"] if od_total is None else table(
        ["Ledger", "Part", "Window", "Past the window", "Oldest"],
        [[r["table"], r["part"], f"{r['window_days']} days", f"{r['overdue']:,}", str(r["oldest"] or "—")[:10]]
         for r in od])
    body += ["", "## Retention job outcomes this month", ""]
    body += [f"- Not readable: {rv['error']}"] if rv["error"] else table(
        ["Kind", "Runs", "Succeeded", "Live", "Dry run", "Held", "Failed"],
        [[k, v["runs"], v["succeeded"], v["live"], v["dry_run"], v["held"], v["failed"]]
         for k, v in sorted(rv["by_kind"].items())])
    body += ["", "Booking retention kinds (bk_retention_job, bk_retention_inbox_redact) run on booking.job: not "
                 "readable from CIS.", "", "## Largest relations", ""]
    body += table(["Relation", "Size", "Rows (estimate)", "Size at the previous DATA-04 run", "Growth"],
                  [[t["name"], _mb(t["bytes"]), f"{t['rows_estimate']:,}", _mb(t["previous_bytes"]), pct(t["growth"])]
                   for t in top]) if top else ["- Not readable."]
    body.append("")
    return _render(reg, report, first, last, generated_at, f"Ledger growth and retention {first.isoformat()} to "
                   f"{last.isoformat()}", headline, kpis, body,
                   "Sizes and windows are as of generation; runs are counted over the period (ICT days).",
                   overdue=od, growth=gr, sizes=sz, retention=rv, top_growth=top,
                   sources_unreadable=_unreadable(facts))


# ── dispatch (generate.RENDERERS) ────────────────────────────────────────────────────────────────────────────────


async def render(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    if report.id == "OPS-14":
        return render_ops14(reg, report, first, last, await collect_jobs(conn, first, last), now)
    if report.id == "OPS-13":
        return render_ops13(reg, report, first, last, await collect_ops13(conn, first, last), now)
    if report.id == "DATA-02":
        return render_data02(reg, report, first, last, await collect_data02(conn, first, last), now)
    if report.id == "DATA-03":
        facts = await src.run_parts({"versions": lambda: schema_versions(conn)})
        return render_data03(reg, report, first, last, facts, now)
    if report.id == "DATA-04":
        return render_data04(reg, report, first, last, await collect_data04(conn, first, last), now)
    raise LookupError(f"reliability.render has no report {report.id}")
