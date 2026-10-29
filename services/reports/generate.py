"""AA-1003 R1 — report rendering (job kind report_generate) and the due/overdue rule of the register.

Generators (catalog.yaml `generator: cis`):
  EX-04  Executive KPI scorecard: the latest snapshot values of shared.kpi_daily for the period's last day, delta vs
         the day before and the median of the 7 days before, coloured by the kpis.yaml `alert` bounds. The KPIs EX-04
         lists come first, then every other KPI the snapshot measures; KPIs nothing measures yet are named.
  EX-06  Report register: every catalog report with its schedule, priority, last run and due state.

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


def expected_period_end(report: Report, now: datetime) -> Optional[date]:
    """The last day a daily/live CIS-generated report should cover by now (the day the daily chain snapshots);
    None when nothing is scheduled here."""
    if not report.runnable or report.schedule not in ("daily", "live"):
        return None
    return periods.snapshot_day(now)


def due_state(report: Report, last: Optional[dict], now: datetime) -> dict:
    """{state, expected_period_end}; state: not_automated (no CIS generator), manual (generated on request only),
    ok (the expected period is covered), due (the chain for it may still be running: within OVERDUE_GRACE of its
    start), overdue (past the grace)."""
    if not report.runnable:
        return {"state": "not_automated", "expected_period_end": None}
    expected = expected_period_end(report, now)
    if expected is None:
        return {"state": "manual", "expected_period_end": None}
    covered = (last or {}).get("covered_to")
    if covered is not None and covered >= expected:
        return {"state": "ok", "expected_period_end": expected.isoformat()}
    deadline = periods.day_bounds(expected)[1] + periods.SETTLE + periods.OVERDUE_GRACE
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


def _register_entry(row: dict) -> dict:
    return {**{k: row[k] for k in _REGISTER_KEYS}, "due": row["due"]["state"],
            "last_status": (row["last_run"] or {}).get("status")}


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
        "reports": [_register_entry(r) for r in rows],
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
        md.append(f"| {r['id']} {r['name_en']} | {r['schedule']} | {last.get('status') or 'never'} | "
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
RENDERERS: dict[str, Renderer] = {"EX-04": _scorecard, "EX-06": _register}


def default_period(report: Report, now: Optional[datetime] = None) -> tuple[date, date]:
    day = periods.snapshot_day(now)
    return day, day


async def render(conn, reg: Registry, report: Report, first: date, last: date,
                 now: Optional[datetime] = None) -> Rendered:
    fn = RENDERERS.get(report.id)
    if fn is None:
        raise LookupError(f"no generator for report {report.id}")
    return await fn(conn, reg, report, first, last, now or periods.now_utc())
