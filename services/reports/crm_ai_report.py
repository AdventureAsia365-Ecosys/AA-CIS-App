"""AA-1003 R6 — CRM-04 CRM AI assistant usage, the AA roll-up (agencies see their own on the CRM Reports page).

Monthly. From the crm.ops.v1 numbers the API captured at a manual run (api/services/report_crm_inputs.py): the CRM
gateway's calls, failures and cost per stage (/llm/usage daily rows, every agency), and per agency its calls, refusals,
errors, cost and latency p95 (/llm/calls rows reduced to numbers, shadow calls out), each agency's period cost against
the AI budget its usage meter shows today (its own limit: no number of the report's own), and calls per member active
in 30 days. A scheduled run has no staff session: every KPI reads "not read". No DB read, no LLM, $0.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from services.reports.generate import Rendered
from services.reports.money import _kpi_line, failed, ratio, table, usd
from services.reports.partners import UTC_NOTE, _crm_read_line, _f, crm_part, short
from services.reports.registry import Registry, Report
from services.reports.reliability import _render


# ═══ CRM-04 CRM AI assistant usage (AA roll-up) ═════════════════════════════════════════════════════════════════


def crm_ai(inputs: Optional[dict]) -> dict:
    """Totals per stage (usage rows) and per agency (calls), each agency's period cost against its budget now. Pure."""
    us, calls = crm_part(inputs, "llm_usage"), crm_part(inputs, "llm_calls")
    if failed(us) and failed(calls):
        return {"error": failed(us)}
    stages: dict[str, dict] = {}
    for r in (us.get("rows") or []) if not failed(us) else []:
        s = stages.setdefault(str(r.get("stage")), {"calls": 0, "failed": 0, "cost_usd": 0.0})
        s["calls"] += int(r.get("calls") or 0)
        s["failed"] += int(r.get("failed_calls") or 0)
        s["cost_usd"] = round(s["cost_usd"] + (_f(r.get("cost_usd")) or 0.0), 6)
    meters = crm_part(inputs, "usage")
    meters = (meters.get("by_agency") or {}) if not failed(meters) else {}
    agencies = []
    for tid, a in sorted(((calls.get("by_agency") or {}) if not failed(calls) else {}).items()):
        m = meters.get(tid, {})
        budget = _f(m.get("ai_budget_usd_month"))
        active = m.get("active_members_30d")
        agencies.append({"agency": short(tid), "calls": a["calls"], "refused": a["refused"], "errors": a["errors"],
                         "cost_usd": round(a["cost_usd"], 4), "budget_usd": budget,
                         "over_budget": bool(budget) and a["cost_usd"] >= budget,
                         "latency_p95_ms": a.get("latency_p95_ms"), "active_members": active,
                         "calls_per_member": ratio(float(a["calls"]), float(active)) if active else None})
    total_calls = sum(s["calls"] for s in stages.values())
    return {"stages": stages, "calls": total_calls, "failed": sum(s["failed"] for s in stages.values()),
            "cost_usd": round(sum(s["cost_usd"] for s in stages.values()), 4), "agencies": agencies,
            "ledger": None if failed(calls) else calls.get("total"), "usage_error": failed(us),
            "calls_error": failed(calls), "meters_read": bool(meters)}


def crm04_kpis(ai: dict) -> list[dict]:
    ids = ("K-CRM04-AI-CALLS-PER-ACTIVE-MEMBER", "K-CRM04-FAILURE-RATE", "K-CRM04-REFUSAL-RATE",
           "K-CRM04-COST-PER-AGENCY", "K-CRM04-LATENCY-P95")
    if "error" in ai:
        return [_kpi_line(k, None, "no_data", ai["error"]) for k in ids]
    led = ai["ledger"]
    members = sum(int(a["active_members"] or 0) for a in ai["agencies"])
    over = [a for a in ai["agencies"] if a["over_budget"]]
    calls_err = f"not readable: {ai['calls_error']}"
    return [
        (_kpi_line(ids[0], ratio(float(led["calls"]), float(members)), "info",
                   f"{led['calls']} calls / {members} member(s) active in 30 days (agencies with calls)")
         if led and members else _kpi_line(ids[0], None, "no_data", calls_err if not led else "no active member read")),
        (_kpi_line(ids[1], ratio(float(ai["failed"]), float(ai["calls"])), "info",
                   f"{ai['failed']} of {ai['calls']} calls failed (gateway daily rows); trend on Dev, "
                   "no flag level yet")
         if not ai["usage_error"] else _kpi_line(ids[1], None, "no_data", f"not readable: {ai['usage_error']}")),
        (_kpi_line(ids[2], ratio(float(led["refused"]), float(led["calls"])), "info",
                   f"{led['refused']} of {led['calls']} calls refused; no flag level yet")
         if led else _kpi_line(ids[2], None, "no_data", calls_err)),
        (_kpi_line(ids[3], round(sum(a["cost_usd"] for a in ai["agencies"]), 4), "amber" if over else "ok",
                   f"{len(ai['agencies'])} agency(ies) with calls; at or past their own AI budget: "
                   + (", ".join(a["agency"] for a in over) or "none"))
         if led else _kpi_line(ids[3], None, "no_data", calls_err)),
        (_kpi_line(ids[4], None if led["latency_p95_ms"] is None else round(led["latency_p95_ms"] / 1000.0, 2),
                   "info", f"over {led['calls']} calls (shadow calls left out: {led['shadow_excluded']})")
         if led else _kpi_line(ids[4], None, "no_data", calls_err)),
    ]


def render_crm04(reg: Registry, report: Report, first: date, last: date, inputs: Optional[dict],
                 generated_at: datetime) -> Rendered:
    ai = crm_ai(inputs)
    kpis = crm04_kpis(ai)
    title = f"CRM AI assistant usage (AA roll-up) {first.isoformat()} to {last.isoformat()}"
    body = ["## Source", "", _crm_read_line(inputs), ""]
    if "error" in ai:
        headline = f"{title}: {'crm.ops.v1 not read (scheduled run)' if not isinstance(inputs, dict) else ai['error']}"
        body += [f"- {ai['error']}", ""] if isinstance(inputs, dict) else []
        return _render(reg, report, first, last, generated_at, title, headline, kpis, body, UTC_NOTE, ai=ai)
    headline = (f"{title}: {ai['calls']} calls, {usd(ai['cost_usd'])}, {len(ai['agencies'])} agency(ies) with calls, "
                f"{sum(1 for a in ai['agencies'] if a['over_budget'])} at or past their AI budget")
    body += ["## Calls by stage (gateway daily rows, every agency)", ""]
    if ai["usage_error"]:
        body += [f"- Not readable: {ai['usage_error']}"]
    else:
        body += table(["Stage", "Calls", "Failed", "Cost"],
                      [[s, v["calls"], v["failed"], usd(v["cost_usd"])] for s, v in sorted(ai["stages"].items())]) \
            if ai["stages"] else ["- No CRM AI call in the period."]
    body += ["", "## Per agency (ledger rows)", "",
             "Cost of the period against the AI budget the agency's usage meter shows today (no budget history is "
             "kept); calls per member counts the members active in the last 30 days when read.", ""]
    if ai["calls_error"]:
        body += [f"- Not readable: {ai['calls_error']}"]
    else:
        body += table(["Agency", "Calls", "Refused", "Errors", "Cost", "AI budget", "Latency p95", "Calls per member"],
                      [[a["agency"], a["calls"], a["refused"], a["errors"], usd(a["cost_usd"]),
                        "—" if a["budget_usd"] is None else usd(a["budget_usd"]) + (" (reached)" if a["over_budget"]
                                                                                     else ""),
                        "—" if a["latency_p95_ms"] is None else f"{a['latency_p95_ms'] / 1000:.2f} s",
                        "—" if a["calls_per_member"] is None else f"{a['calls_per_member']:.1f}"]
                       for a in ai["agencies"]]) if ai["agencies"] else ["- No agency made a call."]
    body += ["", "## Not published to CIS", "",
             "- Scrubber receipts per kind and the text of any prompt or answer: crm.ops.v1 serves the scrub's entity "
             "total per call only, and never text.", ""]
    return _render(reg, report, first, last, generated_at, title, headline, kpis, body, UTC_NOTE, ai=ai)
