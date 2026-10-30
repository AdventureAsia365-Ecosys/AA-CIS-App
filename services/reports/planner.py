"""AA-1003 R4 part 2 — TP-01 weekly TripPlanner funnel.

  TP-01  weekly (ISO week): sessions, trips started, trips edited, inquiries sent per UTC day from TripPlanner's
         contract view tripplanner.v_ops_traffic_daily (tp-015, ops.overview.v1, granted to cis_app), with the four
         weeks before for the trend; step conversion (K-TP-S2T trip_started / sessions, K-TP-T2I inquiry_sent /
         trip_started); AI searches (shared.llm_call_log stage tp_search_embed: cache misses only, the ops overview's
         rule); the customer-facing LLM spend per UTC day (TripPlanner's online stages in shared.llm_call_log,
         quality_signal.app = tripplanner) against the mirrored default daily cap (K-TP-LLMCAP).

The keyword-fallback share and the top pinned components live in TripPlanner tables that are not granted to cis_app
(no contract view): the report names them "not readable from CIS". tripplanner.llm_spend_daily (the TripPlanner
gateway's own daily roll-up, the KPI's registry source) is not granted either; the CIS ledger rows TripPlanner writes
are the same calls (api/core/app_registry.py, the /admin/platform/apps card reads them the same way).

Days are UTC here, like the contract view and the ledger; the report period is the ISO week. Stored rows only; no
LLM, no paid API.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from api.core.app_registry import TP_DEFAULT_DAILY_CAP_USD, TP_ONLINE_STAGES
from api.services.ops_overview import activity
from services.reports import money_sources as src
from services.reports.generate import Rendered
from services.reports.money import _kpi_line, failed, pct, ratio, table, usd
from services.reports.registry import Registry, Report
from services.reports.reliability import _render, _unreadable

TREND_WEEKS = 4
CAP_SHARE_FLAG = 0.80            # K-TP-LLMCAP: < 80 % of the cap any day
METRICS = activity.TP_METRICS    # planner_session_started, trip_started, trip_edited, inquiry_sent
LABEL = {"planner_session_started": "Sessions", "trip_started": "Trips started", "trip_edited": "Trips edited",
         "inquiry_sent": "Inquiries sent", "ai_search": "AI searches"}
UTC_NOTE = ("Days are UTC calendar days (TripPlanner's contract view and the LLM ledger count per UTC day); the "
            "period is the ISO week.")

SPEND_SQL = """
    SELECT (created_at AT TIME ZONE 'UTC')::date AS day, stage, count(*)::int AS calls,
           coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM shared.llm_call_log
     WHERE created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
       AND created_at < (($2::date + 1)::timestamp AT TIME ZONE 'UTC')
       AND stage = ANY($3::text[])
       AND quality_signal ->> 'app' = $4
     GROUP BY 1, 2
"""


async def traffic(conn, first: date, last: date) -> dict:
    """{"rows": [{day, metric, n}]} or {"error"} when the contract view is missing / not granted."""
    try:
        rows = await conn.fetch(activity.TP_VIEW_SQL, first, last)
    except activity._CONTRACT_ERRORS as e:
        return {"error": f"contract view tripplanner.v_ops_traffic_daily not readable ({type(e).__name__})"}
    return {"rows": [{"day": r["day"], "metric": r["metric"], "n": int(r["n"])} for r in rows]}


async def searches(conn, first: date, last: date) -> list[dict]:
    return [{"day": r["day"], "n": int(r["n"])}
            for r in await conn.fetch(activity.TP_SEARCH_SQL, first, last, activity.TP_SEARCH_STAGE)]


async def spend(conn, first: date, last: date) -> list[dict]:
    return [dict(r) for r in await conn.fetch(SPEND_SQL, first, last, list(TP_ONLINE_STAGES), "tripplanner")]


async def collect(conn, first: date, last: date) -> dict:
    back = first - timedelta(weeks=TREND_WEEKS)
    return await src.run_parts({
        "traffic": lambda: traffic(conn, back, last),
        "searches": lambda: searches(conn, back, last),
        "spend": lambda: spend(conn, first, last),
    })


# ── computations (pure) ──────────────────────────────────────────────────────────────────────────────────────────


def weeks(first: date) -> list[tuple[date, date]]:
    """The report week and the TREND_WEEKS weeks before it, oldest first."""
    return [(first - timedelta(weeks=i), first - timedelta(weeks=i) + timedelta(days=6))
            for i in range(TREND_WEEKS, -1, -1)]


def funnel(facts: dict, first: date) -> dict:
    """{"weeks": [{start, end, <metric>: n, ai_search}], "this": {...}, "previous": {...}} or {"error"}."""
    tr = facts.get("traffic")
    if tr is None or failed(tr):
        return {"error": failed(tr) or "not read"}
    se = facts.get("searches")
    out = []
    for a, b in weeks(first):
        w: dict[str, Any] = {"start": a.isoformat(), "end": b.isoformat(), **{m: 0 for m in METRICS}}
        for r in tr["rows"]:
            if r["metric"] in METRICS and a <= r["day"] <= b:
                w[r["metric"]] += r["n"]
        w["ai_search"] = None if se is None or failed(se) else sum(r["n"] for r in se if a <= r["day"] <= b)
        out.append(w)
    return {"weeks": out, "this": out[-1], "previous": out[-2]}


def spend_days(rows: Any, first: date, last: date, cap: float = TP_DEFAULT_DAILY_CAP_USD) -> dict:
    """Per UTC day of the week: USD, calls, share of the cap; the worst day."""
    if rows is None or failed(rows):
        return {"error": failed(rows) or "not read"}
    by_day: dict[date, dict] = {first + timedelta(days=i): {"cost_usd": 0.0, "calls": 0}
                                for i in range((last - first).days + 1)}
    by_stage: dict[str, float] = {}
    for r in rows:
        d = by_day.setdefault(r["day"], {"cost_usd": 0.0, "calls": 0})
        d["cost_usd"] = round(d["cost_usd"] + float(r["cost_usd"] or 0), 6)
        d["calls"] += int(r["calls"] or 0)
        by_stage[r["stage"]] = round(by_stage.get(r["stage"], 0.0) + float(r["cost_usd"] or 0), 6)
    days = [{"day": d.isoformat(), **v, "cap_share": ratio(v["cost_usd"], cap)} for d, v in sorted(by_day.items())]
    worst = max(days, key=lambda d: d["cost_usd"]) if days else None
    return {"days": days, "by_stage": by_stage, "worst": worst, "cap_usd": cap,
            "total_usd": round(sum(d["cost_usd"] for d in days), 6)}


def tp01_kpis(f: dict, sp: dict) -> list[dict]:
    if "error" in f:
        why = f"not readable: {f['error']}"
        head = [_kpi_line(k, None, "no_data", why) for k in ("K-TP-SESSIONS", "K-TP-S2T", "K-TP-T2I")]
    else:
        t, p = f["this"], f["previous"]
        s2t = ratio(t["trip_started"], t["planner_session_started"])
        t2i = ratio(t["inquiry_sent"], t["trip_started"])
        head = [
            _kpi_line("K-TP-SESSIONS", float(t["planner_session_started"]), "info",
                      f"week before {p['planner_session_started']}"),
            _kpi_line("K-TP-S2T", s2t, "no_data" if s2t is None else "info",
                      "no session this week" if s2t is None else
                      f"{t['trip_started']} trips / {t['planner_session_started']} sessions; target set after the "
                      "first prod month"),
            _kpi_line("K-TP-T2I", t2i, "no_data" if t2i is None else "info",
                      "no trip started this week" if t2i is None else
                      f"{t['inquiry_sent']} inquiries / {t['trip_started']} trips; target set after the first prod "
                      "month"),
        ]
    if "error" in sp:
        cap_line = _kpi_line("K-TP-LLMCAP", None, "no_data", f"not readable: {sp['error']}")
    else:
        w = sp["worst"] or {}
        share = w.get("cap_share")
        cap_line = _kpi_line("K-TP-LLMCAP", w.get("cost_usd"), "no_data" if share is None else
                             "red" if share >= CAP_SHARE_FLAG else "ok",
                             f"worst UTC day {w.get('day')}: {pct(share)} of the default {usd(sp['cap_usd'])} cap "
                             "(the live cap is a TripPlanner Lambda setting CIS cannot read)")
    return head + [cap_line]


def render_tp01(reg: Registry, report: Report, first: date, last: date, facts: dict,
                generated_at: datetime) -> Rendered:
    f = funnel(facts, first)
    sp = spend_days(facts.get("spend"), first, last)
    kpis = tp01_kpis(f, sp)
    if "error" in f:
        headline = f"TripPlanner funnel {first.isoformat()} to {last.isoformat()}: not readable ({f['error']})"
    else:
        t = f["this"]
        headline = (f"TripPlanner funnel {first.isoformat()} to {last.isoformat()}: {t['planner_session_started']} "
                    f"sessions, {t['trip_started']} trips started, {t['inquiry_sent']} inquiries sent")
    body = ["## Sessions, trips and inquiries (this week and the four before)", ""]
    if "error" in f:
        body += [f"- Not readable: {f['error']}", ""]
    else:
        cols = list(METRICS) + ["ai_search"]
        body += table(["Week"] + [LABEL[c] for c in cols],
                      [[f"{w['start']} to {w['end']}"] + [w[c] for c in cols] for w in f["weeks"]])
        body += ["", "## Step conversion", ""]
        t, p = f["this"], f["previous"]
        body += table(["Step", "This week", "Week before"], [
            ["Session to trip", pct(ratio(t["trip_started"], t["planner_session_started"])),
             pct(ratio(p["trip_started"], p["planner_session_started"]))],
            ["Trip to inquiry", pct(ratio(t["inquiry_sent"], t["trip_started"])),
             pct(ratio(p["inquiry_sent"], p["trip_started"]))],
            ["Trips edited per trip started", pct(ratio(t["trip_edited"], t["trip_started"])),
             pct(ratio(p["trip_edited"], p["trip_started"]))]])
        body += ["", "## AI searches", "",
                 "- AI searches = embedding calls of stage tp_search_embed in the CIS LLM ledger: repeat searches "
                 "answered from TripPlanner's cache are not counted."
                 if f["this"]["ai_search"] is not None else
                 f"- AI searches not readable: {failed(facts.get('searches')) or 'not read'}",
                 "- Keyword-fallback share: not readable from CIS (TripPlanner does not publish it in a contract "
                 "view).", ""]
    body += ["## LLM spend vs the daily cap", ""]
    if "error" in sp:
        body += [f"- Not readable: {sp['error']}", ""]
    else:
        body += table(["UTC day", "Calls", "Spend", "Share of the default cap"],
                      [[d["day"], d["calls"], usd(d["cost_usd"]), pct(d["cap_share"])] for d in sp["days"]])
        body += ["", f"- Week total {usd(sp['total_usd'])}; by stage: "
                 + (", ".join(f"{k} {usd(v)}" for k, v in sorted(sp["by_stage"].items())) or "no call") + ".", ""]
    body += ["## Not readable from CIS", "",
             "- Top pinned components and the keyword-fallback share: TripPlanner tables without a contract view for "
             "the CIS role (a tp- contract view would add them).",
             "- tripplanner.llm_spend_daily (TripPlanner's own spend roll-up): not granted; the ledger rows above are "
             "the same calls.", ""]
    return _render(reg, report, first, last, generated_at, f"TripPlanner funnel {first.isoformat()} to "
                   f"{last.isoformat()}", headline, kpis, body, UTC_NOTE, funnel=f, llm_spend=sp,
                   sources_unreadable=_unreadable(facts))


# ── dispatch (generate.RENDERERS) ────────────────────────────────────────────────────────────────────────────────


async def render(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    if report.id == "TP-01":
        return render_tp01(reg, report, first, last, await collect(conn, first, last), now)
    raise LookupError(f"planner.render has no report {report.id}")
