"""AA-1003 R1 — report rendering (job kind report_generate) and the due/overdue rule of the register.

Generators (catalog.yaml `generator: cis`):
  EX-04  Executive KPI scorecard: the latest snapshot values of shared.kpi_daily for the period's last day, delta vs
         the day before and the median of the 7 days before, coloured by the kpis.yaml `alert` bounds. The KPIs EX-04
         lists come first, then every other KPI the snapshot measures; KPIs nothing measures yet are named.
  EX-06  Report register: every catalog report with its schedule, priority, last run and due state.
  OPS-12 Daily platform health digest (R2, services/reports/digest.py; e-mailed to its opt-in list).
  TP-03  Inquiry handoff reconciliation, TripPlanner to Booking (R2, services/reports/handoff.py).
  R3 money reports (services/reports/money.py): EX-01 weekly business pack (draft), FI-02 weekly spend digest
         (e-mailed), FI-01 monthly cost review with FI-03 LLM attribution, FI-04 vendors, FI-05 unit costs, FI-06
         budgets, and OPS-08 monthly capacity review.
  R4 (services/reports/quality.py, services/reports/reliability.py): CIS-01 weekly catalog health, CIS-06 eval and
         release report, CIS-07 rerun waves, OPS-13 weekly SLO and DB performance, OPS-14 job runner health, DATA-02
         cross-app reconciliation, DATA-03 schema changes, DATA-04 ledger growth and retention.
  R4 part 2: TP-01 weekly TripPlanner funnel (services/reports/planner.py); SEC-03 monthly security hygiene, SEC-08
         privileged actions and audit log (services/reports/security.py); DEV-01 monthly delivery and DORA, DEV-03
         weekly engineering flow, OPS-15 session-close state snapshot (services/reports/delivery.py).

Each renderer is pure (data in, (summary, output JSON, Markdown) out); the loaders read the DB. Reports read stored
rows only: no LLM, no paid API, no recomputation of history (automation-plan.md §5).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Awaitable, Callable, Optional

from services.reports import periods, store
from services.reports.registry import Kpi, Registry, Report

GENERATED_BY = "AA-CIS report engine (report_generate)"
MEDIAN_DAYS = 7


@dataclass(frozen=True)
class Rendered:
    summary: str
    output: dict
    markdown: str


# ── value helpers ───────────────────────────────────────────────────────────────────────────────────────────────


def _num(v) -> Optional[float]:
    if v is None:
        return None
    return float(v) if isinstance(v, (Decimal, int, float)) else float(Decimal(str(v)))


def fmt(value: Optional[float], unit: str) -> str:
    if value is None:
        return "—"
    if unit == "usd":
        return f"{value:,.2f} USD"
    if unit == "ratio":
        return f"{value * 100:.1f} %"
    if unit == "seconds":
        return f"{value:,.0f} s"
    if unit == "multiple":       # a value / reference ratio read against 1.0 (K-JOB-P95), not a percentage
        return f"{value:,.2f}×"
    if unit in ("count", "days", "hours"):
        text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"
        return text if unit == "count" else f"{text} {unit}"
    return f"{value:,.2f}"


def alert_status(kpi: Kpi, value: Optional[float]) -> str:
    """red / amber / ok from the kpis.yaml alert bounds; info when the KPI has none (a trend KPI)."""
    if value is None:
        return "no_data"
    bounds = kpi.alert or {}
    if not bounds:
        return "info"
    if "red_gte" in bounds and value >= bounds["red_gte"]:
        return "red"
    if "amber_gte" in bounds and value >= bounds["amber_gte"]:
        return "amber"
    return "ok"


def _scope_order(scope: str) -> tuple:
    return (0 if scope == "all" else 1 if scope.startswith("app:") else 2, scope)


# ── EX-04 KPI scorecard ─────────────────────────────────────────────────────────────────────────────────────────


def render_scorecard(reg: Registry, report: Report, day: date, rows: list[dict],
                     generated_at: datetime) -> Rendered:
    """`rows`: shared.kpi_daily rows of the snapshot KPIs from day - MEDIAN_DAYS to day."""
    series: dict[tuple[str, str], dict[date, float]] = {}
    sources: dict[str, set] = {}
    for r in rows:
        series.setdefault((r["kpi_id"], r["scope"]), {})[r["day"]] = _num(r["value"])
        sources.setdefault(r["kpi_id"], set()).add(r["source"])
    measured = [k.id for k in reg.snapshot_kpis()]
    ordered = [kid for kid in report.kpis if kid in measured] + sorted(k for k in measured if k not in report.kpis)
    not_measured = [kid for kid in report.kpis if kid not in measured]
    kpis_out, counts = [], {"red": 0, "amber": 0, "ok": 0, "info": 0, "no_data": 0}
    for kid in ordered:
        kpi = reg.kpi(kid)
        scopes = sorted({s for (k, s) in series if k == kid}, key=_scope_order) or ["all"]
        scope_rows = []
        for s in scopes:
            points = series.get((kid, s), {})
            value = points.get(day)
            previous = points.get(day - timedelta(days=1))
            history = [points[d] for d in (day - timedelta(days=i) for i in range(1, MEDIAN_DAYS + 1)) if d in points]
            status = alert_status(kpi, value)
            scope_rows.append({
                "scope": s, "value": value, "previous": previous,
                "delta": (value - previous) if value is not None and previous is not None else None,
                "median_7d": median(history) if history else None, "status": status,
            })
        head = scope_rows[0]["status"] if scope_rows else "no_data"
        counts[head] = counts.get(head, 0) + 1
        kpis_out.append({"id": kid, "name": kpi.name, "unit": kpi.unit, "listed": kid in report.kpis,
                         "threshold": kpi.threshold_for(report.id), "alert": kpi.alert,
                         "sources": sorted(sources.get(kid, set())) or list(kpi.sources), "scopes": scope_rows})
    output = {
        "report_id": report.id, "name": report.name_en, "period": {"start": day.isoformat(), "end": day.isoformat()},
        "generated_at": generated_at.isoformat(), "generated_by": GENERATED_BY, "source": "shared.kpi_daily",
        "kpis": kpis_out, "status_counts": counts,
        "not_measured": [{"id": kid, "name": reg.kpi(kid).name} for kid in not_measured],
    }
    flagged = counts.get("red", 0) + counts.get("amber", 0)
    summary = (f"{report.id} {day.isoformat()}: {len(kpis_out)} KPIs measured, {flagged} flagged "
               f"({counts.get('red', 0)} red), {len(not_measured)} listed KPIs not measured yet")

    md = [f"# {report.id} {report.name_en} — {day.isoformat()} (ICT)", "",
          f"Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')} by the {GENERATED_BY} from shared.kpi_daily. "
          f"Delta is against the day before; the median covers the {MEDIAN_DAYS} days before.", ""]

    def table(items: list[dict]) -> list[str]:
        lines = ["| KPI | Scope | Value | Change vs day before | 7-day median | Status |", "|---|---|---|---|---|---|"]
        for k in items:
            for s in k["scopes"]:
                delta = s["delta"]
                change = "—" if delta is None else (("+" if delta > 0 else "") + fmt(delta, k["unit"]))
                lines.append(f"| {k['id']} {k['name']} | {s['scope']} | {fmt(s['value'], k['unit'])} | {change} | "
                             f"{fmt(s['median_7d'], k['unit'])} | {s['status']} |")
        return lines

    listed = [k for k in kpis_out if k["listed"]]
    other = [k for k in kpis_out if not k["listed"]]
    if listed:
        md += [f"## KPIs of {report.id}", ""] + table(listed) + [""]
    if other:
        md += ["## Other measured KPIs", ""] + table(other) + [""]
    if not_measured:
        md += ["## Not measured yet", ""]
        md += [f"- {kid} {reg.kpi(kid).name}" for kid in not_measured] + [""]
    md += ["## Thresholds", ""]
    md += [f"- {k['id']}: {k['threshold']}" for k in kpis_out] + [""]
    return Rendered(summary, output, "\n".join(md))


# ── EX-06 register and due state ────────────────────────────────────────────────────────────────────────────────


def scheduled_expectation(report: Report, now: datetime) -> Optional[tuple[date, date]]:
    """(the last day the newest scheduled period covers, the snapshot day whose chain creates it) for a CIS-generated
    report the daily chain schedules (daily, live, weekly, monthly; R3 adds weekly and monthly); None otherwise."""
    if not report.runnable:
        return None
    found = periods.latest_scheduled(report.schedule, now)
    if found is None:
        return None
    (_first, last), chain_day = found
    return last, chain_day


def expected_period_end(report: Report, now: datetime) -> Optional[date]:
    """The last day a scheduled CIS-generated report should cover by now; None when nothing is scheduled here."""
    exp = scheduled_expectation(report, now)
    return exp[0] if exp else None


def due_state(report: Report, last: Optional[dict], now: datetime) -> dict:
    """{state, expected_period_end}; state: not_automated (no CIS generator), manual (generated on request only),
    pending_first (scheduled, but the newest scheduled period was chained before the report's automated_from day, so
    the chain never made it: nothing is expected yet), ok (the expected period is covered), due (the chain for it may
    still be running: within OVERDUE_GRACE of its start), overdue (past the grace)."""
    if not report.runnable:
        return {"state": "not_automated", "expected_period_end": None}
    exp = scheduled_expectation(report, now)
    if exp is None:
        return {"state": "manual", "expected_period_end": None}
    expected, chain_day = exp
    if report.automated_from is not None and chain_day < report.automated_from:
        return {"state": "pending_first", "expected_period_end": None}
    covered = (last or {}).get("covered_to")
    if covered is not None and covered >= expected:
        return {"state": "ok", "expected_period_end": expected.isoformat()}
    deadline = periods.day_bounds(chain_day)[1] + periods.SETTLE + periods.OVERDUE_GRACE
    return {"state": "overdue" if now >= deadline else "due", "expected_period_end": expected.isoformat()}


def _iso(v) -> Optional[str]:
    return v.isoformat() if v is not None else None


def register_rows(reg: Registry, last: dict[str, dict], now: datetime) -> list[dict]:
    out = []
    for r in reg.reports:
        run = last.get(r.id)
        out.append({
            **r.summary(),
            "last_run": None if run is None else {
                "id": str(run["id"]), "status": run["status"], "trigger": run["trigger"],
                "period_start": _iso(run["period_start"]), "period_end": _iso(run["period_end"]),
                "finished_at": _iso(run["finished_at"]), "created_at": _iso(run["created_at"]),
                "summary": run.get("summary"), "error": run.get("error"),
            },
            "covered_to": _iso((run or {}).get("covered_to")),
            "failed_7d": int((run or {}).get("failed_7d") or 0),
            "due": due_state(r, run, now),
        })
    return out


_REGISTER_KEYS = ("id", "name_en", "priority", "schedule", "automation", "status", "generator", "covered_to",
                  "failed_7d")


def _register_entry(row: dict, self_id: str) -> dict:
    # the register's own last run is the one rendering it (status running): name it instead of showing "running"
    status = "this_run" if row["id"] == self_id else (row["last_run"] or {}).get("status")
    return {**{k: row[k] for k in _REGISTER_KEYS}, "due": row["due"]["state"], "last_status": status}


def render_register(reg: Registry, report: Report, day: date, rows: list[dict], generated_at: datetime) -> Rendered:
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["due"]["state"]] = by_state.get(r["due"]["state"], 0) + 1
    automated = [r for r in rows if r["runnable"]]
    attention = [r for r in rows if r["due"]["state"] in ("due", "overdue") or r["failed_7d"]]
    output = {
        "report_id": report.id, "name": report.name_en, "period": {"start": day.isoformat(), "end": day.isoformat()},
        "generated_at": generated_at.isoformat(), "generated_by": GENERATED_BY,
        "registry": {"reports": len(rows), "automated": len(automated), "imported_from": reg.imported_from},
        "states": by_state, "attention": [r["id"] for r in attention],
        "reports": [_register_entry(r, report.id) for r in rows],
    }
    summary = (f"{report.id} {day.isoformat()}: {len(rows)} reports, {len(automated)} generated here, "
               f"{by_state.get('overdue', 0)} overdue, {sum(r['failed_7d'] for r in rows)} failed runs in 7 days")
    md = [f"# {report.id} {report.name_en} — {day.isoformat()} (ICT)", "",
          f"Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')} by the {GENERATED_BY} from the report registry "
          f"and shared.report_run.", "",
          "## Generated here", "",
          "| Report | Schedule | Last run | Covered to | Due | Failed 7 days |", "|---|---|---|---|---|---|"]
    for r in automated:
        last = r["last_run"] or {}
        status = "this run" if r["id"] == report.id else (last.get("status") or "never")
        md.append(f"| {r['id']} {r['name_en']} | {r['schedule']} | {status} | "
                  f"{r['covered_to'] or '—'} | {r['due']['state']} | {r['failed_7d']} |")
    md += ["", "## Needs attention", ""]
    md += [f"- {r['id']} {r['name_en']}: {r['due']['state']}, {r['failed_7d']} failed in 7 days" for r in attention] \
        or ["- Nothing overdue or failed."]
    md += ["", "## Catalogue by priority", "", "| Priority | Reports | Generated here |", "|---|---|---|"]
    for p in ("P0", "P1", "P2", "P3"):
        group = [r for r in rows if r["priority"] == p]
        md.append(f"| {p} | {len(group)} | {sum(1 for r in group if r['runnable'])} |")
    md.append("")
    return Rendered(summary, output, "\n".join(md))


# ── loaders + dispatch ──────────────────────────────────────────────────────────────────────────────────────────


async def _scorecard(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    kpi_ids = [k.id for k in reg.snapshot_kpis()]
    rows = await store.kpi_series(conn, kpi_ids, min(first, last - timedelta(days=MEDIAN_DAYS)), last)
    return render_scorecard(reg, report, last, rows, now)


async def _register(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    rows = register_rows(reg, await store.last_runs(conn), now)
    return render_register(reg, report, last, rows, now)


Renderer = Callable[..., Awaitable[Rendered]]


async def _digest(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    from services.reports import digest      # lazy: digest imports this module

    return await digest.render(conn, reg, report, first, last, now)


async def _handoff(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    from services.reports import handoff

    return await handoff.render(conn, reg, report, first, last, now)


async def _money_week(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    from services.reports import money

    return await money.render_week(conn, reg, report, first, last, now)


async def _money_month(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    from services.reports import money

    return await money.render_month(conn, reg, report, first, last, now)


async def _quality(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    from services.reports import quality

    return await quality.render(conn, reg, report, first, last, now)


async def _reliability(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    from services.reports import reliability

    return await reliability.render(conn, reg, report, first, last, now)


async def _planner(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    from services.reports import planner

    return await planner.render(conn, reg, report, first, last, now)


async def _security(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    from services.reports import security

    return await security.render(conn, reg, report, first, last, now)


async def _delivery(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    from services.reports import delivery

    return await delivery.render(conn, reg, report, first, last, now)


# AA-1003 R2: OPS-12 daily digest (services/reports/digest.py), TP-03 inquiry handoff (services/reports/handoff.py).
# R3 (services/reports/money.py): weekly EX-01, FI-02; monthly FI-01, FI-03, FI-04, FI-05, FI-06, OPS-08.
# R4: catalog and AI quality (services/reports/quality.py): weekly CIS-01, event CIS-06, CIS-07; reliability and data
# quality (services/reports/reliability.py): weekly OPS-13, OPS-14, DATA-02, event DATA-03, monthly DATA-04.
# R4 part 2: weekly TP-01 (planner.py); monthly SEC-03, SEC-08 (security.py); monthly DEV-01, weekly DEV-03, event
# OPS-15 (delivery.py).
RENDERERS: dict[str, Renderer] = {"EX-04": _scorecard, "EX-06": _register, "OPS-12": _digest, "TP-03": _handoff,
                                  "EX-01": _money_week, "FI-02": _money_week,
                                  **{rid: _money_month for rid in ("FI-01", "FI-03", "FI-04", "FI-05", "FI-06",
                                                                   "OPS-08")},
                                  **{rid: _quality for rid in ("CIS-01", "CIS-06", "CIS-07")},
                                  **{rid: _reliability for rid in ("OPS-13", "OPS-14", "DATA-02", "DATA-03",
                                                                   "DATA-04")},
                                  "TP-01": _planner, "SEC-03": _security, "SEC-08": _security,
                                  **{rid: _delivery for rid in ("DEV-01", "DEV-03", "OPS-15")}}


def default_period(report: Report, now: Optional[datetime] = None) -> tuple[date, date]:
    """The period of a manual run without dates (R3: the last closed ISO week / month of a weekly / monthly report)."""
    return periods.default_period(report.schedule, now)


async def render(conn, reg: Registry, report: Report, first: date, last: date,
                 now: Optional[datetime] = None) -> Rendered:
    fn = RENDERERS.get(report.id)
    if fn is None:
        raise LookupError(f"no generator for report {report.id}")
    return await fn(conn, reg, report, first, last, now or periods.now_utc())
