"""AA-900 (S271 W0) — CIS-08 content quality funnel and nightly watchdog (daily, generator cis).

The daily chain (kpi_snapshot_daily -> report_generate) renders CIS-08 for the closed ICT day: the funnel snapshot of
``services/quality/funnel.compute`` (8 ISO weeks ending with the day) and the watchdog findings of its trailing 7 days.
The run row's ``output`` IS the snapshot (GET /admin/quality/funnel serves it as stored); a scheduled run then queues
``quality_funnel_watch`` (services/jobs/quality_funnel_watch_job.py), which raises the NEW findings stored here as one
digest through the ops-check bell and e-mail. A finding the previous scheduled run (``PREVIOUS_SQL``) already had is
stored with ``repeat: true`` (``funnel.mark_repeats``): visible, not raised again. Stored rows only, no LLM, no paid
call.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Optional

from services.quality import funnel
from services.reports.generate import GENERATED_BY, Rendered
from services.reports.money import table
from services.reports.quality import DEV_NOTE
from services.reports.registry import Registry, Report

REPORT_ID = "CIS-08"
TITLE = "Content quality funnel"
# GET /admin/quality/funnel (api/routers/admin_quality_funnel.py): the latest succeeded SCHEDULED run (the one the
# watchdog alerted on) and the headline totals of the newest scheduled runs (one per day); manual runs ("Generate
# now", never alerted on) are listed apart (LATEST_MANUAL_SQL).
_FINDINGS = ("CASE WHEN jsonb_typeof(output -> 'watch' -> 'findings') = 'array' THEN output -> 'watch' -> 'findings' "
             "ELSE '[]'::jsonb END")
LATEST_SQL = """
    SELECT id, period_start, period_end, trigger, finished_at, summary, output
      FROM shared.report_run
     WHERE report_id = $1 AND status = 'succeeded' AND output IS NOT NULL AND trigger = 'schedule'
     ORDER BY period_end DESC, finished_at DESC NULLS LAST, id
     LIMIT 1
"""
LATEST_MANUAL_SQL = """
    SELECT id, period_start, period_end, trigger, finished_at, summary
      FROM shared.report_run
     WHERE report_id = $1 AND status = 'succeeded' AND output IS NOT NULL AND trigger <> 'schedule'
     ORDER BY finished_at DESC NULLS LAST, period_end DESC, id
     LIMIT 1
"""
HISTORY_SQL = f"""
    SELECT DISTINCT ON (period_end) id, period_end, finished_at, summary, output -> 'totals' AS totals,
           jsonb_array_length({_FINDINGS}) AS findings,
           (SELECT count(*) FROM jsonb_array_elements({_FINDINGS}) f
             WHERE coalesce(f ->> 'repeat', 'false') <> 'true') AS new_findings
      FROM shared.report_run
     WHERE report_id = $1 AND status = 'succeeded' AND output IS NOT NULL AND trigger = 'schedule'
     ORDER BY period_end DESC, finished_at DESC NULLS LAST, id
     LIMIT $2
"""
# The transition baseline: the findings of the newest succeeded scheduled run of an earlier day, at most
# funnel.WATCH_DAYS days back ($1 report, $2 this run's day, $3 the oldest day in reach). A same-day re-render (a
# retry) compares with the night before, never with itself.
PREVIOUS_SQL = f"""
    SELECT id, period_end, {_FINDINGS} AS findings
      FROM shared.report_run
     WHERE report_id = $1 AND status = 'succeeded' AND output IS NOT NULL AND trigger = 'schedule'
       AND period_end < $2 AND period_end >= $3
     ORDER BY period_end DESC, finished_at DESC NULLS LAST, id
     LIMIT 1
"""

WORKFLOW_NAMES = {"t9": "T9 social content", "s1": "S1 Master rewrite", "t2": "T2 tenant tour rewrite"}


def _p(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.1f} %"


def _n(v: Any) -> str:
    return "—" if v is None else (f"{v:,.2f}" if isinstance(v, float) and not v.is_integer() else f"{int(v):,}")


def _usd(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.4f} USD"


def headline(snap: dict) -> str:
    t9 = (snap.get("totals") or {}).get("t9", {}).get("window", {})
    findings = (snap.get("watch") or {}).get("findings") or []
    n_new = sum(1 for f in findings if not f.get("repeat"))
    return (f"T9 {t9.get('delivered', 0)} delivered of {t9.get('created', 0)} created, first-pass yield "
            f"{_p(t9.get('first_pass_yield'))}, {_usd(t9.get('usd_per_delivered'))} per delivered; "
            f"{n_new} new watchdog alert(s), {len(findings) - n_new} still open")


def markdown(report: Report, snap: dict, generated_at: datetime) -> str:
    w = snap["window"]
    lines = [f"# {TITLE}", "", DEV_NOTE, "",
             f"Report {report.id} ({report.name_en}), day {snap['day']}, series {w['first_day']} to {w['last_day']} "
             f"({w['weeks']} ISO weeks, ICT). Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')} by the "
             f"{GENERATED_BY}. Delivered = content the agency or the Master received (held is an admin-only label); "
             "first-pass yield = clean on attempt 1 / finished.", ""]
    lines += ["## Totals over the series", ""]
    rows = []
    for wf in funnel.WORKFLOWS:
        t = (snap.get("totals") or {}).get(wf, {}).get("window", {})
        rows.append([WORKFLOW_NAMES[wf], _n(t.get("created")), _n(t.get("delivered")), _n(t.get("held")),
                     _n(t.get("failed")), _p(t.get("first_pass_yield")), _n(t.get("attempts_per_delivered")),
                     _usd(t.get("usd_per_delivered"))])
    lines += table(["Workflow", "Created", "Delivered", "Held", "Failed", "First-pass yield", "Attempts / delivered",
                    "LLM USD / delivered"], rows) + [""]

    lines += ["## T9 per week and channel", ""]
    rows = [[r["week"], r["channel"], _n(r["created"]), _n(r["delivered"]), _n(r["held"]), _n(r["failed"]),
             _p(r["first_pass_yield"]), _n(r["attempts_per_delivered"]), _usd(r["usd_per_delivered"])]
            for r in snap["workflows"]["t9"]["by_week_channel"]]
    lines += (table(["Week", "Channel", "Created", "Delivered", "Held", "Failed", "First-pass yield",
                     "Attempts / delivered", "Attributed USD / delivered"], rows)
              if rows else ["No piece in the series."]) + [""]
    t9_total = (snap.get("totals") or {}).get("t9", {}).get("window", {})
    unattributed = snap["workflows"]["t9"].get("unattributed_cost_usd") or {}
    lines += ["Attributed = T9 ledger rows that carry the piece or its angle-gate request. The rest of the T9 stage "
              "cost (judges, angle generation before a piece exists) is unattributed; the totals above include it "
              f"(attributed {_usd(t9_total.get('attributed_cost_usd'))}, unattributed "
              f"{_usd(t9_total.get('unattributed_cost_usd'))}).", ""]
    rows = [[week, _usd(float(v))] for week, v in sorted(unattributed.items())]
    lines += (table(["Week", "Unattributed T9 cost"], rows) if rows else []) + ([""] if rows else [])
    for wf in ("s1", "t2"):
        lines += [f"## {WORKFLOW_NAMES[wf]} per week", ""]
        rows = [[r["week"], _n(r["created"]), _n(r["delivered"]), _n(r["held"]), _n(r["failed"]),
                 _p(r["first_pass_yield"]), _n(r["attempts_per_delivered"]), _usd(r["usd_per_delivered"])]
                for r in snap["workflows"][wf]["by_week"]]
        lines += (table(["Week", "Created", "Delivered", "Held", "Failed", "First-pass yield", "Attempts / delivered",
                         "USD / delivered"], rows) if rows else ["No object in the series."]) + [""]

    lines += ["## T9 failing gates, every attempt (blocking)", ""]
    agg: dict[tuple[str, Any], int] = {}
    for g in snap["workflows"]["t9"]["gate_fails"]:
        if g["blocking"]:
            agg[(g["gate"], g["attempt"])] = agg.get((g["gate"], g["attempt"]), 0) + int(g["fires"])
    rows = [[gate, att if att is not None else "—", n] for (gate, att), n in
            sorted(agg.items(), key=lambda kv: (-kv[1], kv[0][0], str(kv[0][1])))[:40]]
    lines += (table(["Gate", "Attempt", "Fires"], rows) if rows else ["No failing gate."]) + [""]

    lines += ["## Stages over the watch window", ""]
    watch = snap.get("watch") or {}
    rows = [[s["stage"], s["primary_model"] or "—", _n(s["recent"]["answered"]),
             "—" if s["fallback_pct"] is None else f"{s['fallback_pct']} %",
             "—" if s["cut_pct"] is None else f"{s['cut_pct']} %",
             "—" if s["drift_pct"] is None else f"{s['drift_pct']} %"] for s in watch.get("stages") or []]
    lines += (table(["Stage", "Route primary", "Answered", "Fallback", "Cut", "Drift"], rows)
              if rows else ["No ledger row in the window."]) + [""]
    findings = watch.get("findings") or []
    lines += ["## Watchdog alerts", ""]
    lines += [f"- `{f['check']}`: {f['summary']}" + (" (still open from an earlier night, not raised again)"
                                                     if f.get("repeat") else " (new)")
              for f in findings] or ["None."]
    if snap.get("unavailable"):
        lines += ["", "## Sources not readable here", ""]
        lines += [f"- {k}: {v}" for k, v in sorted(snap["unavailable"].items())]
    return "\n".join(lines) + "\n"


def build(report: Report, snap: dict, generated_at: datetime, previous: Optional[list[dict]] = None) -> Rendered:
    """Pure: a computed snapshot -> the run's summary, output (the snapshot + its findings, each flagged ``repeat``
    against ``previous``: the findings of the previous scheduled run, None = none in reach) and Markdown."""
    findings = funnel.watch_findings(snap.get("watch") or {})
    stored = funnel.mark_repeats([{"check": f.check, "summary": f.summary, "details": f.details} for f in findings],
                                 previous)
    snap = {**snap, "report_id": report.id, "generated_by": GENERATED_BY,
            "watch": {**(snap.get("watch") or {}), "findings": stored}}
    snap["headline"] = headline(snap)
    return Rendered(summary=snap["headline"][:1000], output=snap, markdown=markdown(report, snap, generated_at))


async def previous_findings(conn, day: date) -> tuple[Optional[list[dict]], Optional[dict]]:
    """(findings, {run_id, day}) of the previous scheduled CIS-08 run in reach (PREVIOUS_SQL), or (None, None)."""
    row = await conn.fetchrow(PREVIOUS_SQL, REPORT_ID, day, day - timedelta(days=funnel.WATCH_DAYS))
    if row is None:
        return None, None
    found = row["findings"]
    found = json.loads(found) if isinstance(found, str) else (found or [])
    return list(found), {"run_id": str(row["id"]), "day": row["period_end"].isoformat()}


async def render(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    snap = await funnel.compute(conn, last, now=now)
    previous, ref = await previous_findings(conn, last)
    snap = {**snap, "watch": {**(snap.get("watch") or {}), "compared_with": ref}}
    return build(report, snap, now, previous)
