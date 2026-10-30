"""AA-1003 R3 — the money reports: weekly spend and business pack, monthly cost review and its sections.

  EX-01  Weekly business pack (semi): draft of the business review (template business-review.md, weekly cadence);
         numbers generated, the narrative lines are left for the tech lead to write before posting to Jira.
  FI-02  Weekly spend and vendor balance digest (auto, e-mailed to the OPS-12 opt-in list).
  FI-01  Monthly cost review (semi; template cost-finops-review.md Part A), with the FI-03 / FI-04 / FI-05 / FI-06
         sections inside it.
  FI-03  LLM cost attribution (monthly).   FI-04  Vendor spend, balances and contracts (monthly, semi).
  FI-05  Unit economics (monthly).         FI-06  Budget vs actual and credit runway (monthly, semi).
  OPS-08 Monthly capacity, SLO and availability review (semi).

Data: services/reports/money_sources.py (stored rows only; days are UTC calendar days, see there). Each section
computes from the collected facts with pure functions (unit-tested with fixtures) and says "not wired" for a KPI
whose source the platform does not hold yet (Linear, incidents, free-plan usage, contracts, CRM ledger) or "not
readable" when a part failed: a missing source is a line in the report, never a failed run.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any, Iterable, Optional

from services.reports import money_sources as src
from services.reports import periods
from services.reports.generate import GENERATED_BY, Rendered, fmt
from services.reports.registry import Registry, Report

COST_PAGE = "/admin/cost-ops"
PLATFORM_COST_PAGE = "/admin/platform/cost"
WOW_FLAG_RATIO = 0.30            # K-COST-WOW: flag a change > 30 % ...
WOW_FLAG_USD = 5.0               # ... and > 5 USD
LLM_WEEK_FACTOR = 1.3            # K-LLM-COST-WEEK: flag > 1.3 x the 4-week average ...
LLM_WEEK_MIN_USD = 5.0           # ... and > 5 USD
MOVER_RATIO = 0.20               # FI-01 A4: a mover changed > 20 % ...
MOVER_USD = 2.0                  # ... and > 2 USD
RUNWAY_RED_DAYS = 14             # K-RUNWAY-DFS / -JEV: red under 14 days
CREDIT_UNUSED_FLAG_USD = 50.0    # K-RUNWAY-ACC3 / K-VEND-EXPIRY: flag > 50 USD expiring unused
CPU_BASELINE_PCT = 20.0          # K-CAP-CPU: hours with an hourly average above the burstable baseline
NOT_WIRED = "not wired"
TO_WRITE = "To write before approval"

DEV_NOTE = "> Dev account: spend reflects development and test runs, not customer load. Numbers show trends only."
DAYS_NOTE = ("Money is counted per UTC calendar day, like Cost Explorer and the LLM ledger; AWS figures are the "
             "stored Cost Explorer snapshots (no live call from a report).")

PROVIDERS = (  # FI-02 section 2 order: key, label, source
    ("aws_acc2", "AWS main account (Cost Explorer)", "shared.cost_explorer_snapshot"),
    ("aws_acc3", "AWS LLM account, gross (Cost Explorer)", "shared.cost_explorer_snapshot"),
    ("bedrock_acc3", "Bedrock on the LLM account (gateway estimate)", "LLM ledger"),
    ("bedrock_acc2", "Bedrock on the main account (gateway estimate)", "LLM ledger"),
    ("openai", "OpenAI API", "LLM ledger"),
    ("jev", "Jev (TypeSafe)", "LLM ledger"),
    ("dfs", "DataForSEO", "shared.dfs_call_log"),
    ("geocoder", "Geocoder (Geoapify and others)", "geo.geocode_call"),
)


# ── small helpers ───────────────────────────────────────────────────────────────────────────────────────────────


def usd(v: Optional[float]) -> str:
    return fmt(v, "usd")


def pct(v: Optional[float]) -> str:
    return fmt(v, "ratio")


def ratio(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    if part is None or whole is None or whole == 0:
        return None
    return round(part / whole, 4)


def change(this: Optional[float], prev: Optional[float]) -> Optional[float]:
    if this is None or prev is None or prev == 0:
        return None
    return round((this - prev) / prev, 4)


def signed(v: Optional[float]) -> str:
    return "—" if v is None else (("+" if v > 0 else "") + pct(v))


def avg(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(mean(vals), 4) if vals else None


def table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join("—" if c is None else str(c) for c in r) + " |" for r in rows]
    return out


def _count(v: Optional[float]) -> str:
    return f"{v or 0:,.0f}"


def _d(v: Any) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def failed(part: Any) -> Optional[str]:
    """The error of a part that could not be read, else None."""
    return part.get("error") if isinstance(part, dict) and "error" in part else None


def in_window(day: Any, first: date, last: date) -> bool:
    return first <= _d(day) <= last


def source_of(provider: Optional[str], account: Optional[str]) -> str:
    """LLM ledger row -> FI-02 provider key (bedrock_acc3 / bedrock_acc2 / bedrock_retired, openai, jev, other)."""
    from api.services.platform_cost import provider_family

    fam = provider_family(provider)
    if fam == "bedrock":
        acc = account or "acc2"          # an older Bedrock row without account ran on acc2 (platform_cost rule)
        return f"bedrock_{acc if acc in ('acc2', 'acc3') else 'retired'}"
    return fam


def ledger_sum(rows: Iterable[dict], first: date, last: date, key=lambda r: "all") -> dict[str, dict]:
    """{key: {calls, fallback_calls, tokens_in, tokens_out, cost_usd}} over UTC days [first, last]."""
    out: dict[str, dict] = {}
    for r in rows:
        if not in_window(r["day"], first, last):
            continue
        k = key(r)
        acc = out.setdefault(k, {"calls": 0, "fallback_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
        acc["calls"] += int(r.get("calls") or 0)
        acc["fallback_calls"] += int(r.get("fallback_calls") or 0)
        acc["tokens_in"] += int(r.get("tokens_in") or 0)
        acc["tokens_out"] += int(r.get("tokens_out") or 0)
        acc["cost_usd"] = round(acc["cost_usd"] + float(r.get("cost_usd") or 0.0), 6)
    return out


def daily_sum(rows: Any, first: date, last: date, field: str = "cost_usd", where=lambda r: True) -> Optional[float]:
    if failed(rows) or rows is None:
        return None
    return round(sum(float(r.get(field) or 0) for r in rows if in_window(r["day"], first, last) and where(r)), 6)


def _header(report: Report, first: date, last: date, generated_at: datetime, title: str) -> list[str]:
    period = first.isoformat() if first == last else f"{first.isoformat()} to {last.isoformat()}"
    return [f"# {title}", "", DEV_NOTE, "",
            f"Report {report.id} ({report.name_en}), period {period}. Generated "
            f"{generated_at.strftime('%Y-%m-%d %H:%M UTC')} by the {GENERATED_BY}. {DAYS_NOTE}", ""]


def _output(report: Report, first: date, last: date, generated_at: datetime, headline: str, **kw) -> dict:
    return {"report_id": report.id, "name": report.name_en,
            "period": {"start": first.isoformat(), "end": last.isoformat()},
            "generated_at": generated_at.isoformat(), "generated_by": GENERATED_BY, "headline": headline, **kw}


def _kpi_line(kid: str, value: Any, status: str, note: str = "") -> dict:
    return {"id": kid, "value": value, "status": status, "note": note}


def _kpi_rows(reg: Registry, kpis: list[dict], unit_of=None) -> list[list[Any]]:
    rows = []
    for k in kpis:
        kpi = reg.kpi(k["id"])
        unit = (unit_of or {}).get(k["id"]) or (kpi.unit if kpi else "")
        v = k["value"]
        shown = fmt(v, unit) if isinstance(v, (int, float)) else (v if v is not None else "—")
        rows.append([f"{k['id']}@{kpi.version if kpi else 1}", kpi.name if kpi else "", kpi.threshold if kpi else "",
                     shown, k["status"], k.get("note") or ""])
    return rows


KPI_HEADERS = ["KPI (ID@version)", "Name", "Target", "Actual", "Status", "Note"]


# ── weekly: spend per provider, runways, alerts ─────────────────────────────────────────────────────────────────


def provider_spend(facts: dict, first: date, last: date) -> dict[str, dict]:
    """{provider key: {"usd", "days_present"?}} for UTC days [first, last]; usd None when the source was not
    readable. AWS rows count only the days Cost Explorer data is stored for (days_present of 7)."""
    out: dict[str, dict] = {}
    aws = facts.get("aws")
    for acc in ("acc2", "acc3"):
        if failed(aws) or aws is None:
            out[f"aws_{acc}"] = {"usd": None}
            continue
        days = [r for r in aws if r["account"] == acc and in_window(r["day"], first, last)]
        out[f"aws_{acc}"] = {"usd": round(sum(float(r["amount_usd"]) for r in days), 4) if days else None,
                             "days_present": len({_d(r["day"]) for r in days})}
    llm = facts.get("llm")
    sums = {} if failed(llm) or llm is None else ledger_sum(llm, first, last,
                                                           key=lambda r: source_of(r["provider"], r["account"]))
    for key in ("bedrock_acc3", "bedrock_acc2", "openai", "jev"):
        out[key] = {"usd": None if failed(llm) else round(sums.get(key, {}).get("cost_usd", 0.0), 4)}
    out["dfs"] = {"usd": daily_sum(facts.get("dfs"), first, last)}
    out["geocoder"] = {"usd": daily_sum(facts.get("geo"), first, last)}
    return out


def wow_flag(this: Optional[float], prev: Optional[float]) -> bool:
    """K-COST-WOW: flagged when spend rose > 30 % and > 5 USD (a first week of spend above 5 USD counts)."""
    if this is None or prev is None:
        return False
    rise = this - prev
    if rise <= WOW_FLAG_USD:
        return False
    return prev == 0 or rise / prev > WOW_FLAG_RATIO


def daily_average(rows: Any, last: date, days: int = src.RUNWAY_DAYS, field: str = "cost_usd",
                  where=lambda r: True) -> Optional[float]:
    """Mean daily spend over the `days` UTC days ending with `last` (missing day = 0)."""
    total = daily_sum(rows, last - timedelta(days=days - 1), last, field, where)
    return None if total is None else round(total / days, 6)


def runway(balance: Optional[float], per_day: Optional[float]) -> Optional[float]:
    """Days the balance lasts at the daily average; None without a balance or without spend."""
    if balance is None or not per_day:
        return None
    return round(max(balance, 0.0) / per_day, 1)


def credit_outlook(remaining: Optional[float], per_day: Optional[float], expires_on: Optional[date],
                   today: date) -> dict:
    """acc3 credit: runway days, the amount left at expiry at this burn, and the flag (> 50 USD expiring unused, or
    exhausted before expiry). An unknown burn (per_day None: the spend source was not read) gives no amount and no
    flag, so a missing source never reads as "the whole credit expires unused"; a known zero burn does."""
    out = {"remaining_usd": remaining, "per_day_usd": per_day, "expires_on": expires_on.isoformat() if expires_on
           else None, "runway_days": runway(remaining, per_day), "left_at_expiry_usd": None, "flag": None}
    if remaining is None or expires_on is None or per_day is None:
        return out
    days_left = max(0, (expires_on - today).days)
    left = round(remaining - per_day * days_left, 2)
    out["left_at_expiry_usd"] = max(left, 0.0)
    if left > CREDIT_UNUSED_FLAG_USD:
        out["flag"] = f"about {usd(left)} would expire unused on {expires_on.isoformat()}"
    elif left < 0:
        out["flag"] = f"credit runs out about {out['runway_days']} days from now, before {expires_on.isoformat()}"
    return out


def balances_view(facts: dict, last: date) -> dict:
    """K-RUNWAY-DFS, K-RUNWAY-JEV, K-RUNWAY-ACC3 from the stored balances and the 14-day spend."""
    bal = facts.get("balances")
    if failed(bal) or bal is None:
        return {"error": failed(bal) or "not read"}
    dfs_bal = (bal.get("dfs") or {}).get("balance_usd")
    dfs_rate = daily_average(facts.get("dfs"), last)
    jev = bal.get("jev") or {}
    llm = facts.get("llm")
    jev_rate = None if failed(llm) or llm is None else round(sum(
        float(r.get("cost_usd") or 0) for r in llm
        if in_window(r["day"], last - timedelta(days=src.RUNWAY_DAYS - 1), last)
        and source_of(r["provider"], r["account"]) == "jev") / src.RUNWAY_DAYS, 6)
    acc3 = (bal.get("credits") or {}).get("acc3")
    acc3_rate = None
    aws = facts.get("aws")
    if not failed(aws) and aws:
        days = [r for r in aws if r["account"] == "acc3"
                and in_window(r["day"], last - timedelta(days=src.RUNWAY_DAYS - 1), last)]
        if days:
            acc3_rate = round(sum(float(r["amount_usd"]) for r in days) / len(days), 4)
    if acc3_rate is None and not failed(llm) and llm is not None:
        acc3_rate = round(sum(float(r.get("cost_usd") or 0) for r in llm if in_window(
            r["day"], last - timedelta(days=src.RUNWAY_DAYS - 1), last)
            and source_of(r["provider"], r["account"]) == "bedrock_acc3") / src.RUNWAY_DAYS, 4)
    credit = credit_outlook(acc3["remaining_usd"], acc3_rate, _d(acc3["expires_on"]) if acc3.get("expires_on")
                            else None, last + timedelta(days=1)) if acc3 else None
    return {
        "dfs": {"balance_usd": dfs_bal, "fetched_at": (bal.get("dfs") or {}).get("fetched_at"), "per_day_usd": dfs_rate,
                "runway_days": runway(dfs_bal, dfs_rate)},
        "jev": {"estimated_left_usd": jev.get("estimated_left_usd"), "per_day_usd": jev_rate,
                "runway_days": runway(jev.get("estimated_left_usd"), jev_rate)},
        "acc3_credit": credit,
    }


def runway_status(days: Optional[float], known: bool) -> str:
    if not known:
        return NOT_WIRED
    if days is None:
        return "no spend"
    return "red" if days < RUNWAY_RED_DAYS else "ok"


def alerts_view(alerts: Any) -> dict:
    if failed(alerts) or alerts is None:
        return {"error": failed(alerts) or "not read"}
    by_provider = Counter(a["provider"] or "unknown" for a in alerts)
    stops = [a for a in alerts if a["stopped"]]
    return {"bells": len(alerts), "stops": len(stops), "by_provider": dict(sorted(by_provider.items())),
            "items": [{"provider": a["provider"], "at": str(a["created_at"])[:16], "stopped": a["stopped"],
                       "message": (a.get("message") or "")[:200]} for a in alerts[:10]]}


def mtd_vs_budget(forecast: Any) -> dict:
    """K-COST-MTD: (acc2 + acc3 month to date) / the budgets prorated to the days Cost Explorer covers per account
    (its ~2-day lag would otherwise read as under budget). Accounts without stored data are left out (named)."""
    if failed(forecast) or forecast is None:
        return {"value": None, "error": failed(forecast) or "not read"}
    n = int(forecast["days_in_month"])
    start = _d(forecast["period_start"])
    spent = prorated = 0.0
    missing = []
    for a in forecast.get("aws_accounts") or []:
        if a["account"] not in ("acc2", "acc3"):
            continue
        if a.get("mtd_usd") is None or not a.get("covered_to") or not a.get("budget_usd"):
            missing.append(a["account"])
            continue
        days = (_d(a["covered_to"]) - start).days + 1
        spent += float(a["mtd_usd"])
        prorated += float(a["budget_usd"]) * days / n
    value = ratio(spent, prorated)
    status = "no_data" if value is None else "red" if value > 1.1 else "amber" if value > 1.0 else "ok"
    return {"value": value, "status": status, "spent_usd": round(spent, 2), "prorated_budget_usd": round(prorated, 2),
            "accounts_missing": missing}


def forecast_rows(forecast: Any) -> list[dict]:
    """K-COST-FCST per AWS account and AI provider: month to date, forecast, budget, forecast / budget, status."""
    if failed(forecast) or forecast is None:
        return []
    rows = []
    for a in forecast.get("aws_accounts") or []:
        rows.append({"key": f"aws_{a['account']}", "label": f"AWS {a.get('label') or a['account']}",
                     "mtd_usd": a.get("mtd_usd"), "forecast_usd": a.get("forecast_usd"),
                     "budget_usd": a.get("budget_usd"), "ratio": ratio(a.get("forecast_usd"), a.get("budget_usd")),
                     "status": a.get("status"), "covered_to": a.get("covered_to")})
    for p in forecast.get("providers") or []:
        rows.append({"key": p["key"], "label": p["label"], "mtd_usd": p.get("mtd_usd"),
                     "forecast_usd": p.get("forecast_usd"), "budget_usd": p.get("budget_usd"),
                     "ratio": ratio(p.get("forecast_usd"), p.get("budget_usd")), "status": p.get("status"),
                     "covered_to": None})
    return rows


def top_stages(llm: Any, first: date, last: date, n: int = 5) -> list[dict]:
    if failed(llm) or llm is None:
        return []
    sums = ledger_sum(llm, first, last, key=lambda r: f"{r['app']}/{r['stage']}")
    ranked = sorted(sums.items(), key=lambda kv: -kv[1]["cost_usd"])[:n]
    return [{"stage": k, "calls": v["calls"], "cost_usd": round(v["cost_usd"], 4)} for k, v in ranked]


def week_compute(facts: dict, first: date, last: date) -> dict:
    prev_first, prev_last = first - timedelta(days=7), first - timedelta(days=1)
    this = provider_spend(facts, first, last)
    prev = provider_spend(facts, prev_first, prev_last)
    lines = []
    for key, label, source in PROVIDERS:
        t, p = this[key]["usd"], prev[key]["usd"]
        lines.append({"key": key, "label": label, "source": source, "usd": t, "prev_usd": p, "change": change(t, p),
                      "flag": wow_flag(t, p), "days_present": this[key].get("days_present")})
    # One week total without counting acc3 Bedrock twice: AWS acc3 (gross bill) replaces the gateway's estimate for
    # the LLM account when Cost Explorer covers the whole week.
    acc3_full = (this["aws_acc3"].get("days_present") or 0) >= 7

    def total(spend: dict) -> Optional[float]:
        keys = ["aws_acc2", "openai", "jev", "dfs", "geocoder", "aws_acc3" if acc3_full else "bedrock_acc3"]
        vals = [spend[k]["usd"] for k in keys]
        return round(sum(v for v in vals if v is not None), 4) if any(v is not None for v in vals) else None
    return {"lines": lines, "total_usd": total(this), "prev_total_usd": total(prev),
            "flagged": [x["key"] for x in lines if x["flag"]],
            "forecast": forecast_rows(facts.get("forecast")), "mtd": mtd_vs_budget(facts.get("forecast")),
            "top_stages": top_stages(facts.get("llm"), first, last), "balances": balances_view(facts, last),
            "alerts": alerts_view(facts.get("alerts"))}


def _money_week_md(w: dict, first: date, last: date) -> list[str]:
    md = ["## 1. Month to date and month-end forecast", ""]
    mtd = w["mtd"]
    if mtd.get("value") is not None:
        md += [f"K-COST-MTD: {pct(mtd['value'])} of the budget prorated to the days Cost Explorer covers "
               f"({usd(mtd['spent_usd'])} of {usd(mtd['prorated_budget_usd'])}); status {mtd['status']}."
               + (f" No stored data for: {', '.join(mtd['accounts_missing'])}." if mtd['accounts_missing'] else ""),
               ""]
    else:
        md += [f"K-COST-MTD: no stored Cost Explorer data ({mtd.get('error') or 'none for this month'}).", ""]
    if w["forecast"]:
        md += table(["Line", "Month to date", "Forecast month end", "Budget", "Forecast / budget", "Status",
                     "AWS data to"],
                    [[r["label"], usd(r["mtd_usd"]), usd(r["forecast_usd"]), usd(r["budget_usd"]), pct(r["ratio"]),
                      r["status"], r["covered_to"]] for r in w["forecast"]]) + [""]
    md += ["## 2. This week vs the week before", ""]
    md += table(["Provider", "This week", "Week before", "Change", "Flag", "Source"],
                [[x["label"] + (f" ({x['days_present']} of 7 days stored)" if x["key"].startswith("aws_") and
                                x["days_present"] is not None else ""),
                  usd(x["usd"]), usd(x["prev_usd"]), signed(x["change"]), "flag" if x["flag"] else "",
                  x["source"]] for x in w["lines"]]) + [""]
    md += [f"Week total {usd(w['total_usd'])} (week before {usd(w['prev_total_usd'])}); the LLM account counts once "
           "(its Cost Explorer bill when stored for the whole week, else the gateway estimate). A flag = up more than "
           f"{int(WOW_FLAG_RATIO * 100)} % and more than {usd(WOW_FLAG_USD)}.", ""]
    md += ["## 3. Top LLM stages this week", ""]
    md += (table(["App / stage", "Calls", "Cost"], [[s["stage"], s["calls"], usd(s["cost_usd"])]
                                                   for s in w["top_stages"]]) if w["top_stages"]
           else ["- No LLM call this week."]) + [""]
    md += ["## 4. Balances and runway", ""]
    b = w["balances"]
    if "error" in b:
        md += [f"- Balances not readable: {b['error']}", ""]
    else:
        dfs = b["dfs"]
        dfs_state = runway_status(dfs["runway_days"], dfs["balance_usd"] is not None)
        md += [f"- DataForSEO: balance {usd(dfs['balance_usd'])} (stored {str(dfs['fetched_at'] or '—')[:16]}),"
               f" {usd(dfs['per_day_usd'])} a day over 14 days, runway {fmt(dfs['runway_days'], 'days')} "
               f"({dfs_state}).",
               f"- Jev: estimated {usd(b['jev']['estimated_left_usd'])} left (top-ups minus logged spend), "
               f"{usd(b['jev']['per_day_usd'])} a day, runway {fmt(b['jev']['runway_days'], 'days')} "
               f"({runway_status(b['jev']['runway_days'], b['jev']['estimated_left_usd'] is not None)})."]
        c = b["acc3_credit"]
        if c is None:
            md.append(f"- LLM account credit: {NOT_WIRED} (set COST_OPS_CREDITS_USD on the worker, e.g. "
                      "acc3=<remaining>@<expiry date>).")
        else:
            md.append(f"- LLM account credit: {usd(c['remaining_usd'])} as set on the worker, burn "
                      f"{usd(c['per_day_usd'])} a day, runway {fmt(c['runway_days'], 'days')}, expires "
                      f"{c['expires_on'] or '—'}" + (f"; {c['flag']}." if c["flag"] else "."))
        md.append("")
    md += ["## 5. Budget bells and hard stops", ""]
    a = w["alerts"]
    if "error" in a:
        md += [f"- Not readable: {a['error']}"]
    elif not a["bells"]:
        md += ["- No budget bell this period."]
    else:
        md += [f"- {a['bells']} budget bell(s), {a['stops']} for a stopped run; by provider: "
               + ", ".join(f"{k} {v}" for k, v in a["by_provider"].items())]
        md += [f"  - {i['at']} {i['provider']}{' (stopped)' if i['stopped'] else ''}: {i['message']}"
               for i in a["items"]]
    return md + [""]


def render_fi02(reg: Registry, report: Report, first: date, last: date, facts: dict,
                generated_at: datetime) -> Rendered:
    w = week_compute(facts, first, last)
    b = w["balances"]
    reds = [k for k in ("dfs", "jev") if "error" not in b and b[k]["runway_days"] is not None
            and b[k]["runway_days"] < RUNWAY_RED_DAYS]
    credit_flag = None if "error" in b or not b.get("acc3_credit") else b["acc3_credit"]["flag"]
    attention = [f"{x['label']} up {signed(x['change'])}" if x["change"] is not None else f"{x['label']} new spend"
                 for x in w["lines"] if x["flag"]]
    attention += [f"{k} runway under {RUNWAY_RED_DAYS} days" for k in reds]
    attention += [f"forecast over budget: {r['label']}" for r in w["forecast"] if r["status"] == "over"]
    attention += [credit_flag] if credit_flag else []
    attention += [f"{w['alerts']['stops']} run(s) stopped by a budget"] if w["alerts"].get("stops") else []
    headline = (f"Weekly spend {first.isoformat()} to {last.isoformat()}: {usd(w['total_usd'])}"
                f" ({signed(change(w['total_usd'], w['prev_total_usd']))} vs the week before), "
                + (f"{len(attention)} item(s) to look at" if attention else "nothing flagged"))
    kpis = [
        _kpi_line("K-COST-FCST", max((r["ratio"] for r in w["forecast"] if r["ratio"] is not None), default=None),
                  "red" if any(r["status"] == "over" for r in w["forecast"]) else
                  "amber" if any(r["status"] == "warn" for r in w["forecast"]) else "ok" if w["forecast"]
                  else "no_data", "highest forecast / budget of the lines in section 1"),
        _kpi_line("K-COST-WOW", len(w["flagged"]), "amber" if w["flagged"] else "ok",
                  "providers flagged (section 2)"),
        _kpi_line("K-RUNWAY-DFS", None if "error" in b else b["dfs"]["runway_days"],
                  "no_data" if "error" in b else runway_status(b["dfs"]["runway_days"],
                                                               b["dfs"]["balance_usd"] is not None)),
        _kpi_line("K-RUNWAY-JEV", None if "error" in b else b["jev"]["runway_days"],
                  "no_data" if "error" in b else runway_status(b["jev"]["runway_days"],
                                                               b["jev"]["estimated_left_usd"] is not None)),
        _kpi_line("K-RUNWAY-ACC3", None if "error" in b or not b.get("acc3_credit")
                  else b["acc3_credit"]["runway_days"],
                  NOT_WIRED if "error" in b or not b.get("acc3_credit") else
                  ("amber" if b["acc3_credit"]["flag"] else "ok"),
                  "credit set by hand (COST_OPS_CREDITS_USD)" if b.get("acc3_credit") else
                  "no credit balance is stored yet"),
    ]
    md = _header(report, first, last, generated_at, headline)
    md += ["## Needs a look", ""] + ([f"- {i}" for i in attention] or ["- Nothing flagged."]) + [""]
    md += ["## KPIs", ""] + table(KPI_HEADERS, _kpi_rows(reg, kpis, {"K-COST-FCST": "ratio", "K-COST-WOW": "count"}))
    md += [""] + _money_week_md(w, first, last)
    md += ["## 6. More", "", f"Platform › Cost operations ({COST_PAGE}) has the forecast, the budgets and the "
           f"DataForSEO runs; Platform › Cost per app ({PLATFORM_COST_PAGE}) refreshes Cost Explorer.", ""]
    out = _output(report, first, last, generated_at, headline, kpis=kpis, attention=attention, week=w,
                  sources_unreadable={k: failed(v) for k, v in facts.items() if failed(v)})
    return Rendered(headline[:1000], out, "\n".join(md))


# ── EX-01 weekly business pack ──────────────────────────────────────────────────────────────────────────────────


def llm_week(llm: Any, first: date, last: date) -> dict:
    """K-LLM-COST-WEEK: the week's LLM spend with the shadow split, vs the average of the 4 weeks before."""
    if failed(llm) or llm is None:
        return {"error": failed(llm) or "not read"}
    week = ledger_sum(llm, first, last, key=lambda r: "shadow" if r["is_shadow"] else "live")
    total = round(sum(v["cost_usd"] for v in week.values()), 4)
    prior = [round(sum(v["cost_usd"] for v in ledger_sum(llm, first - timedelta(days=7 * i),
                                                         last - timedelta(days=7 * i)).values()), 4)
             for i in range(1, 5)]
    avg4 = round(mean(prior), 4)
    flag = total > LLM_WEEK_MIN_USD and total > LLM_WEEK_FACTOR * avg4
    return {"usd": total, "shadow_usd": round(week.get("shadow", {}).get("cost_usd", 0.0), 4),
            "avg_4w_usd": avg4, "prior_weeks_usd": prior, "flag": flag}


def _latest_in(by_day: dict[date, float], first: date, last: date) -> tuple[Optional[date], Optional[float]]:
    days = [d for d in by_day if first <= d <= last]
    return (max(days), by_day[max(days)]) if days else (None, None)


def tours_live_change(rows: Any, first: date, last: date, live_now: Any = None) -> dict:
    """K-CAT-LIVE at the end of the week vs the end of the week before (the newest snapshot inside each week, ICT
    days). A week with no snapshot (before the snapshots started, or the snapshot job failed every day) falls back to
    the live count at generation (`live_now`, money_sources.liveness_now), labelled so, with no week-over-week change:
    the two numbers are not the same point in time."""
    snap_err = failed(rows) if rows is not None else "not read"
    by_day = {} if snap_err else {_d(r["day"]): float(r["value"]) for r in rows if r["scope"] == "all"}
    day, now_v = _latest_in(by_day, first, last)
    _pday, prev_v = _latest_in(by_day, first - timedelta(days=7), first - timedelta(days=1))
    if now_v is not None:
        ch = change(now_v, prev_v)
        return {"value": now_v, "previous": prev_v, "change": ch, "flag": ch is not None and ch < -0.02,
                "source": "snapshot", "as_of": day.isoformat()}
    if isinstance(live_now, dict) and not failed(live_now):
        return {"value": float(live_now.get("served", 0)), "previous": prev_v, "change": None, "flag": False,
                "source": "live", "as_of": live_now.get("as_of"),
                "states": {k: live_now.get(k, 0) for k in ("served", "paused", "retired")}}
    return {"error": snap_err or failed(live_now) or "no snapshot in the week and no live count"}


def published_line(pub: Any) -> dict:
    n, new = published_count(pub)
    if n is None:
        return {"error": failed(pub) or "not read"}
    return {"total": n, "first_published": new, "republished": None if new is None else n - new}


def _tours_live_note(tl: dict) -> str:
    if "error" in tl:
        return ""
    if tl.get("source") == "live":
        return f"live count at generation ({str(tl.get('as_of') or '')[:16]} UTC): no snapshot in this week"
    return f"week over week {signed(tl['change'])} (snapshot {tl.get('as_of')})"


def demand_line(d: Any) -> dict:
    if failed(d) or d is None:
        return {"error": failed(d) or "not read"}
    tp, bk = d.get("tripplanner") or {}, d.get("booking") or {}
    out = {}
    for side, data, metrics in (("tripplanner", tp, ("trip_started", "inquiry_sent")),
                                ("booking", bk, ("inquiry_received", "confirmed"))):
        out.update({m: (None if "error" in data else int(data.get(m, 0))) for m in metrics})
        if "error" in data:
            out[f"{side}_error"] = data["error"]
    return out


def digest_rollup(runs: Any, first: date, last: date) -> dict:
    """OPS-12 over the week: digests found, days all green, the attention items seen most often."""
    if failed(runs) or runs is None:
        return {"error": failed(runs) or "not read"}
    expected = (last - first).days + 1
    items = Counter(i for r in runs for i in (r["attention"] or []))
    return {"digests": len(runs), "expected": expected, "green_days": sum(1 for r in runs if not r["attention"]),
            "top_items": [{"item": k[:160], "days": v} for k, v in items.most_common(5)]}


def render_ex01(reg: Registry, report: Report, first: date, last: date, facts: dict,
                generated_at: datetime) -> Rendered:
    w = week_compute(facts, first, last)
    lw = llm_week(facts.get("llm"), first, last)
    tl = tours_live_change(facts.get("tours_live"), first, last, facts.get("liveness_now"))
    pb, pb_prev = published_line(facts.get("published")), published_line(facts.get("published_prev"))
    dm, dm_prev = demand_line(facts.get("demand")), demand_line(facts.get("demand_prev"))
    ops = digest_rollup(facts.get("digests"), first, last)
    mtd = w["mtd"]
    kpis = [
        _kpi_line("K-DEL-SHIPPED", None, NOT_WIRED, "Linear Done-with-live-verify issues: count them when writing"),
        _kpi_line("K-COST-MTD", mtd.get("value"), mtd.get("status", "no_data"),
                  f"{usd(mtd.get('spent_usd'))} of {usd(mtd.get('prorated_budget_usd'))} prorated"),
        _kpi_line("K-LLM-COST-WEEK", lw.get("usd"), "no_data" if "error" in lw else ("amber" if lw["flag"] else "ok"),
                  "" if "error" in lw else f"shadow {usd(lw['shadow_usd'])}; 4-week average {usd(lw['avg_4w_usd'])}"),
        _kpi_line("K-CAT-LIVE", tl.get("value"), "no_data" if tl.get("value") is None else
                  ("amber" if tl["flag"] else "ok"), _tours_live_note(tl)),
        _kpi_line("K-DEM-INQ", None if "error" in dm else dm.get("inquiry_sent"), "info",
                  "" if "error" in dm else (f"trips started {fmt(dm.get('trip_started'), 'count')}, inquiries sent "
                                            f"{fmt(dm.get('inquiry_sent'), 'count')}, received "
                                            f"{fmt(dm.get('inquiry_received'), 'count')}, confirmed "
                                            f"{fmt(dm.get('confirmed'), 'count')}")),
        _kpi_line("K-REL-SEV", None, NOT_WIRED, "SEV1-2 incidents: from the postmortem folder when writing"),
    ]
    headline = (f"Business review draft {first.isoformat()} to {last.isoformat()}: spend {usd(w['total_usd'])}, "
                f"LLM {usd(lw.get('usd'))}, tours live {fmt(tl.get('value'), 'count')}")
    md = _header(report, first, last, generated_at, headline)
    md += ["Draft: every number below is generated; the lines marked NARRATIVE are written by the tech lead before "
           "the weekly Jira comment (Vietnamese, no internal issue IDs). Commercial numbers stay here, not in git.", ""]
    md += ["## 1. Summary", "", f"NARRATIVE ({TO_WRITE}): three lines: went well, went wrong, money.", ""]
    week_change = signed(change(w["total_usd"], w["prev_total_usd"]))
    facts_for_narrative = [f"Spend this week {usd(w['total_usd'])} ({week_change} vs the week before)."]
    if mtd.get("value") is not None:
        facts_for_narrative.append(f"Month to date at {pct(mtd['value'])} of the prorated budget.")
    if tl.get("value") is not None:
        facts_for_narrative.append(f"Tours live {fmt(tl['value'], 'count')} ({_tours_live_note(tl)}).")
    if "error" not in pb:
        facts_for_narrative.append(f"Tours published or republished this week: {pb['total']:,}.")
    if "error" not in ops:
        facts_for_narrative.append(f"Platform digest: {ops['green_days']} of {ops['digests']} digests all green "
                                   f"({ops['expected']} days in the period).")
    md += ["Facts for the narrative:", ""] + [f"- {x}" for x in facts_for_narrative] + [""]
    md += ["## 2. How to read this report", "",
           "- Status: red = act now, amber = watch, ok = on target, info = trend only (Dev), not wired = the "
           "platform does not hold the number yet (fill it in when writing).",
           "- Week over week compares with the ISO week before; money is per UTC day, tours live per ICT day.", ""]
    md += ["## 3. Scorecard", ""] + table(KPI_HEADERS, _kpi_rows(reg, kpis, {"K-DEM-INQ": "count",
                                                                           "K-COST-MTD": "ratio"})) + [""]
    md += ["## 4. Delivered per app", "", f"NARRATIVE ({TO_WRITE}): versions released per app (release-please "
           "CHANGELOG) and the issues done with live verify; not read by the engine yet.", ""]
    md += ["## 5. Money", ""]
    md += table(["Line", "This week", "Week before", "Change"],
                [[x["label"], usd(x["usd"]), usd(x["prev_usd"]), signed(x["change"])] for x in w["lines"]])
    md += ["", f"Month-end forecast and budgets: see FI-02 for the same week ({len(w['forecast'])} lines; "
           + (", ".join(f"{r['label']} {r['status']}" for r in w["forecast"] if r["status"] in ("over", "warn"))
              or "none over or near budget") + ").", "",
           f"NARRATIVE ({TO_WRITE}): one line per mover above 20 %, with the reason.", ""]
    md += ["## 6. Demand and catalog", ""]
    if "error" in dm:
        md += [f"- Demand not readable: {dm['error']}"]
    else:
        md += table(["Metric", "This week", "Week before"],
                    [[m, dm.get(m), None if "error" in dm_prev else dm_prev.get(m)]
                     for m in ("trip_started", "inquiry_sent", "inquiry_received", "confirmed")])
        md.append("")
        for side in ("tripplanner_error", "booking_error"):
            if dm.get(side):
                md.append(f"- {side.split('_')[0]} contract view not readable: {dm[side]}")
    tours_line = (f"- Tours live: {fmt(tl.get('value'), 'count')} ({_tours_live_note(tl)}; end of the week "
                  f"before: {fmt(tl.get('previous'), 'count')})." if "error" not in tl
                  else f"- Tours live not readable: {tl['error']}")
    if "error" in pb:
        pub_line = f"- Tours published or republished: not readable ({pb['error']})."
    else:
        prev = "—" if "error" in pb_prev else f"{pb_prev['total']:,}"
        pub_line = (f"- Tours published or republished: {pb['total']:,} ({pb['first_published']:,} new, "
                    f"{pb['republished']:,} republished; week before: {prev}).")
    md += ["", tours_line, pub_line, ""]
    md += ["## 7. Incidents and risks", ""]
    if "error" in ops:
        md += [f"- Platform digest runs not readable: {ops['error']}"]
    else:
        md += [f"- Platform digest (OPS-12): {ops['digests']} of {ops['expected']} days generated, "
               f"{ops['green_days']} all green."]
        md += [f"  - {t['item']} ({t['days']} day(s))" for t in ops["top_items"]]
    md += [f"- NARRATIVE ({TO_WRITE}): SEV1-2 incidents with their postmortems, blocked items.", ""]
    md += ["## 8. Next week", "", f"NARRATIVE ({TO_WRITE}): top 3-5 items from the live backlog.", ""]
    md += ["## 9. Decisions needed", "", f"NARRATIVE ({TO_WRITE}): options and a recommendation for each.", ""]
    out = _output(report, first, last, generated_at, headline, kpis=kpis, draft=True, week=w, llm_week=lw,
                  tours_live=tl, published={"this": pb, "previous": pb_prev},
                  demand={"this": dm, "previous": dm_prev}, platform=ops,
                  sources_unreadable={k: failed(v) for k, v in facts.items() if failed(v)})
    return Rendered(headline[:1000], out, "\n".join(md))


# ── monthly: AWS data status, by account, by App tag, movers ────────────────────────────────────────────────────


def aws_status(covered_from: Any, covered_to: Any, fetched_at: Any, first: date, last: date) -> str:
    """missing / partial (days missing beyond the ~2-day lag) / provisional (fetched before the month settled,
    end + 3 days) / final."""
    if not covered_to:
        return "missing"
    if _d(covered_from) > first or _d(covered_to) < last - timedelta(days=src.CE_LAG_DAYS):
        return "partial"
    fetched = fetched_at if isinstance(fetched_at, datetime) else datetime.fromisoformat(str(fetched_at))
    if fetched < src.utc_start(last + timedelta(days=1)) + src.CE_FINAL_AFTER:
        return "provisional"
    return "final"


AWS_STATUS_NOTE = {
    "missing": "no Cost Explorer data is stored for this month",
    "partial": "the stored Cost Explorer data misses days of this month",
    "provisional": "fetched before the month settled (Cost Explorer revises the last ~48 h)",
    "final": "fetched after the month settled",
}


def month_cba(facts: dict, key: str) -> Optional[dict]:
    v = facts.get(f"cost_by_app:{key}")
    return None if v is None or failed(v) else v


def app_lines(cba: Optional[dict]) -> dict[str, dict]:
    """{budget key: row}: App rows by App id, shared tags by tag name, `untagged`.

    cost_by_app returns a row for every App with aws_usd 0.0 even when no Cost Explorer snapshot is stored for the
    month (aws.has_data false). Those zeros are not spend: the AWS amounts (and total_usd, which includes them)
    become None, so ratios, statuses, variances, averages and unit costs read no_data instead of "0 USD, ok". The AI
    amounts come from the LLM ledger and stay."""
    out: dict[str, dict] = {}
    aws_known = bool(((cba or {}).get("aws") or {}).get("has_data"))
    for r in (cba or {}).get("apps") or []:
        if not aws_known:
            r = {**r, "aws_usd": None, "aws_bedrock_usd": None, "total_usd": None}
        if r["kind"] == "app":
            out[r["app"]] = r
        elif r["kind"] in ("shared_tag", "other_tag"):
            out[r["app"][4:]] = r
        elif r["kind"] == "untagged":
            out["untagged"] = r
    return out


def by_app(facts: dict, budgets: dict[str, float]) -> dict:
    """FI-01 A3 / K-COST-APP / K-COST-UNTAGGED: AWS spend per App tag vs the per-App budgets, 3 months."""
    keys = [m["key"] for m in facts["months"]]
    lines = {k: app_lines(month_cba(facts, k)) for k in keys}
    this = keys[-1]
    names = list(budgets) + sorted(n for n in lines[this] if n not in budgets and n != "untagged") + ["untagged"]
    rows = []
    for name in names:
        hist = [(lines[k].get(name) or {}).get("aws_usd") for k in keys]
        actual = hist[-1]
        budget = budgets.get(name)
        r = ratio(actual, budget)
        rows.append({"app": name, "aws_usd": actual, "prev_usd": hist[-2], "avg_3m_usd": avg(hist),
                     "budget_usd": budget, "ratio": r, "variance_usd": None if actual is None or budget is None
                     else round(actual - budget, 2),
                     "status": "no_budget" if budget is None else "no_data" if r is None else
                     "red" if r > 1.0 else "amber" if r >= 0.8 else "ok",
                     "ai_outside_aws_usd": (lines[this].get(name) or {}).get("ai_outside_aws_usd"),
                     "total_usd": (lines[this].get(name) or {}).get("total_usd")})
    cba = month_cba(facts, this)
    aws_total = ((cba or {}).get("aws") or {}).get("total_usd")
    untagged = ((cba or {}).get("aws") or {}).get("untagged_usd")
    budgeted = [x for x in rows if x["ratio"] is not None]
    worst = max(budgeted, key=lambda x: x["ratio"]) if budgeted else None
    aws = (cba or {}).get("aws") or {}
    first, last = _d(facts["window"]["first"]), _d(facts["window"]["last"])
    status = aws_status(aws.get("covered_from"), aws.get("covered_to"), aws.get("fetched_at"), first, last) \
        if cba is not None else "missing"
    return {"rows": rows, "aws_total_usd": aws_total, "untagged_ratio": ratio(untagged, aws_total),
            "worst": worst, "aws_status": status, "covered_to": aws.get("covered_to"),
            "fetched_at": aws.get("fetched_at"), "error": failed(facts.get(f"cost_by_app:{this}")),
            "acc2_history_usd": [((month_cba(facts, k) or {}).get("aws") or {}).get("total_usd") for k in keys]}


def accounts(facts: dict, aws_budgets: dict[str, float]) -> dict:
    """FI-01 A2: stored Cost Explorer totals per account (this and last month) vs the AWS Budgets."""
    this, prev = facts.get("services"), facts.get("services_prev")

    def totals(d: Any) -> dict[str, float]:
        if d is None or failed(d):
            return {}
        return {a.get("account_key") or "unknown": a["total_usd"] for a in d["accounts"]}
    t, p = totals(this), totals(prev)
    rows = []
    for acc in ("acc2", "acc3") + tuple(k for k in t if k not in ("acc2", "acc3")):
        budget = aws_budgets.get(acc, 0.0 if acc == "retired" else None)
        actual = t.get(acc)
        rows.append({"account": acc, "usd": actual, "prev_usd": p.get(acc), "budget_usd": budget,
                     "ratio": ratio(actual, budget) if budget else None,
                     "variance_usd": None if actual is None or budget is None else round(actual - budget, 2)})
    first, last = _d(facts["window"]["first"]), _d(facts["window"]["last"])
    status = "missing" if this is None or failed(this) else aws_status(
        this.get("covered_from"), this.get("covered_to"), this.get("fetched_at"), first, last)
    return {"rows": rows, "aws_status": status, "error": failed(this)}


def movers(facts: dict) -> dict:
    """FI-01 A4: top 5 services this month and every service that moved > 20 % and > 2 USD vs last month."""
    this, prev = facts.get("services"), facts.get("services_prev")
    if this is None or failed(this):
        return {"top": [], "movers": [], "error": failed(this) or "no stored Cost Explorer data"}

    def per_service(d: Any) -> dict[tuple, float]:
        if d is None or failed(d):
            return {}
        out: dict[tuple, float] = {}
        for s in d["services"]:
            k = (s.get("account_key") or "unknown", s["service"])
            out[k] = out.get(k, 0.0) + float(s["amount_usd"])
        return out
    t, p = per_service(this), per_service(prev)
    top = sorted(t.items(), key=lambda kv: -kv[1])[:5]
    moved = []
    for k in sorted(set(t) | set(p)):
        a, b = t.get(k, 0.0), p.get(k, 0.0)
        if abs(a - b) > MOVER_USD and (b == 0 or abs(a - b) / b > MOVER_RATIO):
            moved.append({"account": k[0], "service": k[1], "usd": round(a, 2), "prev_usd": round(b, 2),
                          "change": change(a, b)})
    moved.sort(key=lambda m: -abs(m["usd"] - m["prev_usd"]))
    return {"top": [{"account": k[0], "service": k[1], "usd": round(v, 2), "prev_usd": round(p.get(k, 0.0), 2)}
                    for k, v in top], "movers": moved, "previous_missing": prev is None or bool(failed(prev))}


# ── monthly: LLM (FI-03) ────────────────────────────────────────────────────────────────────────────────────────


def llm_month(facts: dict) -> dict:
    llm = facts.get("llm")
    if llm is None or failed(llm):
        return {"error": failed(llm) or "not read"}
    months = [(m["key"], _d(m["first"]), _d(m["last"])) for m in facts["months"]]
    key, first, last = months[-1]
    total = ledger_sum(llm, first, last).get("all", {"calls": 0, "fallback_calls": 0, "cost_usd": 0.0,
                                                    "tokens_in": 0, "tokens_out": 0})
    cost = total["cost_usd"]
    by_app_trend = {k: {a: round(v["cost_usd"], 4) for a, v in ledger_sum(llm, f, lst, key=lambda r: r["app"]).items()}
                    for k, f, lst in months}
    stages = sorted(ledger_sum(llm, first, last, key=lambda r: f"{r['app']}/{r['stage']}").items(),
                    key=lambda kv: -kv[1]["cost_usd"])
    models = sorted(ledger_sum(llm, first, last, key=lambda r: f"{r['model']}|{source_of(r['provider'], r['account'])}"
                               ).items(), key=lambda kv: -kv[1]["cost_usd"])
    shadow = ledger_sum(llm, first, last, key=lambda r: "shadow" if r["is_shadow"] else "live")
    adhoc = {k: round(v["cost_usd"], 4) for k, v in ledger_sum(
        [r for r in llm if str(r["stage"]).startswith("adhoc_")], first, last, key=lambda r: r["stage"]).items()}
    tenants = ledger_sum([r for r in llm if r.get("tenant_id")], first, last, key=lambda r: r["tenant_id"])
    days_with_calls = {_d(r["day"]) for r in llm if in_window(r["day"], first, last) and int(r["calls"] or 0) > 0}
    zero_days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)
                 if first + timedelta(days=i) not in days_with_calls]
    cba = month_cba(facts, key)
    sat = ((cba or {}).get("llm_satellites") or {}).get("acc3") or {}
    ce_bedrock, est = sat.get("ce_bedrock_usd"), sat.get("llm_log_estimate_usd")
    recon = None if not ce_bedrock else round(abs(ce_bedrock - (est or 0.0)) / ce_bedrock, 4)
    return {
        "cost_usd": round(cost, 4), "calls": total["calls"], "by_app_trend": by_app_trend,
        "stages": [{"stage": k, "calls": v["calls"], "cost_usd": round(v["cost_usd"], 4)} for k, v in stages[:15]],
        "models": [{"model": k.split("|")[0], "source": k.split("|")[1], "calls": v["calls"],
                    "tokens": v["tokens_in"] + v["tokens_out"], "cost_usd": round(v["cost_usd"], 4),
                    "usd_per_1k_calls": round(v["cost_usd"] / v["calls"] * 1000, 4) if v["calls"] else None,
                    "usd_per_1m_tokens": round(v["cost_usd"] / (v["tokens_in"] + v["tokens_out"]) * 1e6, 4)
                    if v["tokens_in"] + v["tokens_out"] else None} for k, v in models[:15]],
        "shadow_ratio": ratio(shadow.get("shadow", {}).get("cost_usd", 0.0), cost),
        "adhoc": adhoc, "fallback_ratio": ratio(total["fallback_calls"], total["calls"]),
        "zero_call_days": zero_days,
        "tenants": [{"tenant": k[:8], "cost_usd": round(v["cost_usd"], 4)}
                    for k, v in sorted(tenants.items(), key=lambda kv: -kv[1]["cost_usd"])[:10]],
        "platform_usd": round(cost - sum(v["cost_usd"] for v in tenants.values()), 4),
        "recon": {"ce_bedrock_usd": ce_bedrock, "estimate_usd": est, "ratio": recon},
    }


def llm_kpis(lm: dict) -> list[dict]:
    if "error" in lm:
        return [_kpi_line(k, None, "no_data", lm["error"]) for k in
                ("K-LLM-APP", "K-LLM-TENANT", "K-LLM-SHADOW", "K-LLM-FALLBACK", "K-LLM-ADHOC", "K-LLM-RECON")]
    this = list(lm["by_app_trend"].values())[-1]
    sh, fb, rc = lm["shadow_ratio"], lm["fallback_ratio"], lm["recon"]["ratio"]
    return [
        _kpi_line("K-LLM-APP", lm["cost_usd"], "info", ", ".join(f"{a} {usd(v)}" for a, v in sorted(this.items()))
                  + "; the CRM gateway's own ledger is not read here"),
        _kpi_line("K-LLM-TENANT", round(sum(t["cost_usd"] for t in lm["tenants"]), 4), "info",
                  f"{len(lm['tenants'])} tenant(s); platform work {usd(lm['platform_usd'])}"),
        _kpi_line("K-LLM-SHADOW", sh, "no_data" if sh is None else "amber" if sh > 0.15 else "ok"),
        _kpi_line("K-LLM-FALLBACK", fb, "no_data" if fb is None else "red" if fb > 0.05 else "ok"),
        _kpi_line("K-LLM-ADHOC", round(sum(lm["adhoc"].values()), 4), "info", f"{len(lm['adhoc'])} ad hoc stage(s)"),
        _kpi_line("K-LLM-RECON", rc, "no_data" if rc is None else "red" if rc > 0.10 else "ok",
                  f"Cost Explorer Bedrock {usd(lm['recon']['ce_bedrock_usd'])} vs gateway "
                  f"{usd(lm['recon']['estimate_usd'])}"),
    ]


def llm_md(lm: dict, heading: str = "##") -> list[str]:
    if "error" in lm:
        return [f"- LLM ledger not readable: {lm['error']}", ""]
    keys = list(lm["by_app_trend"])
    apps = sorted({a for m in lm["by_app_trend"].values() for a in m})
    md = [f"{heading} By app (3 months)", ""]
    md += table(["App"] + keys, [[a] + [usd(lm["by_app_trend"][k].get(a)) for k in keys] for a in apps]) + [""]
    md += [f"{heading} By app and stage (top 15)", ""]
    md += table(["App / stage", "Calls", "Cost"], [[s["stage"], s["calls"], usd(s["cost_usd"])] for s in lm["stages"]])
    md += ["", f"{heading} By model and provider", ""]
    md += table(["Model", "Provider / account", "Calls", "Tokens", "Cost", "USD per 1,000 calls", "USD per 1M tokens"],
                [[m["model"], m["source"], m["calls"], m["tokens"], usd(m["cost_usd"]), usd(m["usd_per_1k_calls"]),
                  usd(m["usd_per_1m_tokens"])] for m in lm["models"]])
    md += ["", f"{heading} Shadow, ad hoc, fallback, outages", "",
           f"- Shadow share {pct(lm['shadow_ratio'])}; fallback rate {pct(lm['fallback_ratio'])}.",
           "- Ad hoc stages: " + (", ".join(f"{k} {usd(v)}" for k, v in lm["adhoc"].items()) or "none") + ".",
           "- Days without any LLM call (outage or idle, never a saving): "
           + (", ".join(lm["zero_call_days"]) if lm["zero_call_days"] else "none") + ".", ""]
    md += [f"{heading} Cost per tenant (internal view)", ""]
    md += (table(["Tenant (id prefix)", "AI cost"], [[t["tenant"], usd(t["cost_usd"])] for t in lm["tenants"]])
           if lm["tenants"] else ["- No tenant-attributed call."])
    md += ["", f"Platform work (no tenant): {usd(lm['platform_usd'])}. Plan prices are not stored: the share of the "
           "plan price (K-LLM-TENANT target) is for the tech lead to add.", ""]
    md += [f"{heading} Ledger vs Cost Explorer (LLM account)", "",
           f"- Cost Explorer Bedrock {usd(lm['recon']['ce_bedrock_usd'])}, gateway estimate "
           f"{usd(lm['recon']['estimate_usd'])}, gap {pct(lm['recon']['ratio'])} (target <= 10 %).",
           "- TripPlanner's calls are in the shared ledger (app tripplanner); the CRM's gateway ledger is read only "
           "through the crm.ops.v1 feed of the cost page, not by this report.", ""]
    return md


# ── monthly: vendors (FI-04), units (FI-05) ─────────────────────────────────────────────────────────────────────


def vendors(facts: dict) -> dict:
    months = [(m["key"], _d(m["first"]), _d(m["last"])) for m in facts["months"]]
    (_, pf, pl), (_, first, last) = months[-2], months[-1]
    llm = facts.get("llm")
    llm_ok = llm is not None and not failed(llm)

    def llm_src(f, lst, key):
        if not llm_ok:
            return None
        return round(ledger_sum(llm, f, lst, key=lambda r: source_of(r["provider"], r["account"])).get(
            key, {}).get("cost_usd", 0.0), 4)
    geo = facts.get("geo")
    geo_providers = sorted({r["provider"] for r in geo}) if geo and not failed(geo) else []
    rows = [
        {"vendor": "Amazon Bedrock (LLM account, gateway estimate)", "usd": llm_src(first, last, "bedrock_acc3"),
         "prev_usd": llm_src(pf, pl, "bedrock_acc3"), "usage": None},
        {"vendor": "OpenAI API", "usd": llm_src(first, last, "openai"), "prev_usd": llm_src(pf, pl, "openai"),
         "usage": None},
        {"vendor": "Jev (TypeSafe), decision log", "usd": daily_sum(facts.get("jev"), first, last),
         "prev_usd": daily_sum(facts.get("jev"), pf, pl),
         "usage": f"{daily_sum(facts.get('jev'), first, last, 'uncached') or 0:,.0f} uncached decisions"},
        {"vendor": "DataForSEO", "usd": daily_sum(facts.get("dfs"), first, last),
         "prev_usd": daily_sum(facts.get("dfs"), pf, pl),
         "usage": f"{daily_sum(facts.get('dfs'), first, last, 'calls') or 0:,.0f} calls, "
                  f"{daily_sum(facts.get('dfs'), first, last, 'keywords') or 0:,.0f} keywords"},
    ]
    for p in geo_providers or ["geocoder"]:
        rows.append({"vendor": f"Geocoder: {p}",
                     "usd": daily_sum(geo, first, last, where=lambda r, p=p: r["provider"] == p),
                     "prev_usd": daily_sum(geo, pf, pl, where=lambda r, p=p: r["provider"] == p),
                     "usage": _count(daily_sum(geo, first, last, 'requests', where=lambda r, p=p: r['provider'] == p))
                              + " requests"})
    for r in rows:
        r["change"] = change(r["usd"], r["prev_usd"])
    total = round(sum(r["usd"] for r in rows if r["usd"] is not None), 4)
    last_day = last
    bal = balances_view(facts, last_day)
    return {"rows": rows, "total_usd": total, "balances": bal}


def vendor_kpis(v: dict) -> list[dict]:
    credit = None if "error" in v["balances"] else v["balances"].get("acc3_credit")
    return [
        _kpi_line("K-VEND-SPEND", v["total_usd"], "info", "Bedrock (LLM account), OpenAI, Jev, DataForSEO, geocoder"),
        _kpi_line("K-VEND-EXPIRY", None if not credit else credit["left_at_expiry_usd"],
                  NOT_WIRED if not credit else ("amber" if credit["flag"] else "ok"),
                  "LLM account credit (COST_OPS_CREDITS_USD)" if credit else "no credit balance stored"),
        _kpi_line("K-VEND-HEADROOM", None, NOT_WIRED, "free-plan usage (Vercel, Sentry, GitHub, Geoapify, Linear) is "
                  "not read by the platform: fill in by hand"),
        _kpi_line("K-VEND-DPA", None, NOT_WIRED, "contract and DPA status live in the legal register (root repo)"),
    ]


def credits_md(b: dict) -> list[str]:
    if "error" in b:
        return [f"- Balances not readable: {b['error']}", ""]
    c = b["acc3_credit"]
    line = (f"- LLM account credit: {NOT_WIRED} (set COST_OPS_CREDITS_USD on the worker)." if c is None else
            f"- LLM account credit {usd(c['remaining_usd'])} as set on the worker, burn {usd(c['per_day_usd'])} a day, "
            f"runway {fmt(c['runway_days'], 'days')}, expires {c['expires_on'] or '—'}, left at expiry "
            f"{usd(c['left_at_expiry_usd'])}" + (f" ({c['flag']})." if c["flag"] else "."))
    return [f"- DataForSEO balance {usd(b['dfs']['balance_usd'])}, runway {fmt(b['dfs']['runway_days'], 'days')}.",
            f"- Jev estimated left {usd(b['jev']['estimated_left_usd'])}, runway "
            f"{fmt(b['jev']['runway_days'], 'days')}.", line, ""]


def vendors_md(v: dict) -> list[str]:
    md = table(["Vendor", "This month", "Last month", "Change", "Usage"],
               [[r["vendor"], usd(r["usd"]), usd(r["prev_usd"]), signed(r["change"]), r["usage"]] for r in v["rows"]])
    md += ["", f"Vendor total {usd(v['total_usd'])} (AWS bills are in the account totals).", ""]
    return md + credits_md(v["balances"])


def published_count(pub: Any) -> tuple[Optional[int], Optional[int]]:
    """(published or republished, first published) from money_sources.published_tours; None when not read."""
    if pub is None or failed(pub):
        return None, None
    if isinstance(pub, dict):
        return int(pub.get("total") or 0), int(pub.get("first_published") or 0)
    return int(pub), None            # an older stored shape: a single count


def units(facts: dict) -> dict:
    """FI-05 per month: USD per published tour / trip plan / confirmed booking / Jev 1,000 decisions; vendor units."""
    out = []
    for m in facts["months"]:
        key, first, last = m["key"], _d(m["first"]), _d(m["last"])
        lines = app_lines(month_cba(facts, key))
        pub = facts.get(f"published:{key}")
        dem = facts.get(f"demand:{key}")
        dem = None if dem is None or failed(dem) else dem
        tp = (dem or {}).get("tripplanner") or {}
        bk = (dem or {}).get("booking") or {}
        trips = None if dem is None or "error" in tp else tp.get("trip_started", 0)
        confirmed = None if dem is None or "error" in bk else bk.get("confirmed", 0)
        jev_cost = daily_sum(facts.get("jev"), first, last)
        jev_n = daily_sum(facts.get("jev"), first, last, "uncached")
        dfs_cost = daily_sum(facts.get("dfs"), first, last)
        dfs_kw = daily_sum(facts.get("dfs"), first, last, "keywords")
        geo_cost = daily_sum(facts.get("geo"), first, last)
        geo_req = daily_sum(facts.get("geo"), first, last, "requests")
        pub_n, pub_new = published_count(pub)
        out.append({
            "month": key, "published_tours": pub_n, "first_published": pub_new, "trip_started": trips,
            "confirmed": confirmed,
            "tour": ratio((lines.get("cis") or {}).get("total_usd"), pub_n),
            "trip": ratio((lines.get("tripplanner") or {}).get("total_usd"), trips),
            "booking": ratio((lines.get("booking") or {}).get("total_usd"), confirmed),
            "jev_per_1k": None if not jev_n or jev_cost is None else round(jev_cost / jev_n * 1000, 4),
            "dfs_per_keyword": ratio(dfs_cost, dfs_kw), "geo_per_1k": None if not geo_req or geo_cost is None
            else round(geo_cost / geo_req * 1000, 4),
            "numerators": {a: (lines.get(a) or {}).get("total_usd") for a in ("cis", "tripplanner", "booking", "crm")},
        })
    return {"months": out}


UNIT_LINES = (("tour", "USD per published tour", "K-UNIT-TOUR"), ("trip", "USD per trip plan", "K-UNIT-TRIP"),
              ("booking", "USD per confirmed booking", "K-UNIT-BOOKING"),
              ("jev_per_1k", "Jev USD per 1,000 decisions", "K-UNIT-JEV"),
              ("dfs_per_keyword", "DataForSEO USD per keyword", None), ("geo_per_1k", "Geocoder USD per 1,000 requests",
                                                                       None))


def unit_kpis(u: dict) -> list[dict]:
    this, prev = u["months"][-1], u["months"][-2]
    out = []
    for key, _label, kid in UNIT_LINES:
        if kid is None:
            continue
        v, p = this[key], prev[key]
        ch = change(v, p)
        status = "no_data" if v is None else "amber" if kid == "K-UNIT-TOUR" and ch is not None and ch > 0.20 else \
            "info"
        note = "n/a until the first booking" if kid == "K-UNIT-BOOKING" and not this["confirmed"] else \
            f"vs last month {signed(ch)}"
        out.append(_kpi_line(kid, v, status, note))
    out.append(_kpi_line("K-UNIT-AGENCY", None, NOT_WIRED, "active CRM tenants come from the crm.ops.v1 feed"))
    return out


def units_md(u: dict) -> list[str]:
    ms = u["months"]
    md = table(["Unit"] + [m["month"] for m in ms] + ["3-month average"],
               [[label] + [usd(m[key]) for m in ms] + [usd(avg(m[key] for m in ms))]
                for key, label, _ in UNIT_LINES])
    md += ["", "Numerators (cost per app incl. its AI outside the AWS bill) and denominators:", ""]
    md += table(["Month", "CIS", "TripPlanner", "Booking", "Tours published or republished", "Trips started",
                 "Confirmed"],
                [[m["month"], usd(m["numerators"]["cis"]), usd(m["numerators"]["tripplanner"]),
                  usd(m["numerators"]["booking"]), _published_cell(m), m["trip_started"], m["confirmed"]]
                 for m in ms])
    return md + ["", "Tours published or republished: gold_aa_internal.published_tours rows whose first publish "
                     "(published_at) or last republish (updated_at) falls in the month; a rewrite counts as a "
                     "republish.", ""]


def _published_cell(m: dict) -> Optional[str]:
    n, new = m.get("published_tours"), m.get("first_published")
    if n is None:
        return None
    return f"{n:,}" if new is None else f"{n:,} ({new:,} new)"


# ── monthly: budgets (FI-06) ────────────────────────────────────────────────────────────────────────────────────


def budgets(facts: dict, aws_budgets: dict[str, float], app: dict) -> dict:
    first, last = _d(facts["window"]["first"]), _d(facts["window"]["last"])
    end, mid = facts.get("forecast_end"), facts.get("forecast_mid")
    lines = []
    acc = []
    for a in (end or {}).get("aws_accounts") or [] if end is not None and not failed(end) else []:
        if a["account"] not in ("acc2", "acc3"):
            continue
        mid_row = next((m for m in (mid or {}).get("aws_accounts") or [] if m["account"] == a["account"]), None) \
            if mid is not None and not failed(mid) else None
        actual = a.get("mtd_usd")
        f15 = (mid_row or {}).get("forecast_usd")
        acc.append({"line": f"AWS {a['account']}", "budget_usd": a.get("budget_usd"), "actual_usd": actual,
                    "variance": change(actual, a.get("budget_usd")), "forecast_day15_usd": f15,
                    "forecast_accuracy": None if not actual or f15 is None else round(abs(f15 - actual) / actual, 4)})
    lines += acc
    for r in app["rows"]:
        if r["budget_usd"] is None:
            continue
        lines.append({"line": f"App {r['app']}", "budget_usd": r["budget_usd"], "actual_usd": r["aws_usd"],
                      "variance": change(r["aws_usd"], r["budget_usd"]), "forecast_day15_usd": None,
                      "forecast_accuracy": None})
    for p in (end or {}).get("providers") or [] if end is not None and not failed(end) else []:
        if p.get("budget_usd") is None:
            continue
        lines.append({"line": f"Vendor {p['label']}", "budget_usd": p["budget_usd"], "actual_usd": p.get("mtd_usd"),
                      "variance": change(p.get("mtd_usd"), p["budget_usd"]), "forecast_day15_usd": None,
                      "forecast_accuracy": None})
    ytd = facts.get("ytd")
    ytd_view = None
    if ytd is not None and not failed(ytd):
        spent = sum(a["total_usd"] for a in ytd["accounts"] if a.get("account_key") in ("acc2", "acc3"))
        # the plan covers the months the stored data covers (Cost Explorer is stored only when fetched)
        cf = _d(ytd["covered_from"]) if ytd.get("covered_from") else first.replace(month=1, day=1)
        months_elapsed = first.month - cf.month + 1 if cf.year == first.year else first.month
        plan = (aws_budgets.get("acc2", 0) + aws_budgets.get("acc3", 0)) * months_elapsed
        ytd_view = {"usd": round(spent, 2), "plan_usd": round(plan, 2), "ratio": ratio(spent, plan),
                    "covered_from": ytd.get("covered_from"), "months": months_elapsed}
    worst_var = max((x["variance"] for x in lines if x["variance"] is not None), default=None)
    accs = [x["forecast_accuracy"] for x in acc if x["forecast_accuracy"] is not None]
    al = alerts_view(facts.get("alerts"))
    sb = facts.get("spend_budgets")
    return {"lines": lines, "worst_variance": worst_var, "forecast_accuracy": max(accs) if accs else None,
            "ytd": ytd_view, "alerts": al, "spend_budgets": [] if sb is None or failed(sb) else sb,
            "balances": balances_view(facts, last),
            "error": failed(end)}


def budget_kpis(b: dict) -> list[dict]:
    wv, fa = b["worst_variance"], b["forecast_accuracy"]
    y = (b["ytd"] or {}).get("ratio")
    stops = b["alerts"].get("stops") if "error" not in b["alerts"] else None
    return [
        _kpi_line("K-FIN-VAR", wv, "no_data" if wv is None else "red" if wv > 0.10 else "amber" if wv > 0 else "ok",
                  "the worst line of the budget table"),
        _kpi_line("K-FIN-FCSTACC", fa, "no_data" if fa is None else "red" if fa > 0.15 else "ok",
                  "forecast as of day 15, re-computed from the stored rows, vs the month"),
        _kpi_line("K-FIN-YTD", y, "no_data" if y is None else "red" if y > 1.0 else "ok",
                  "" if not b["ytd"] else f"from {b['ytd']['covered_from']} (stored Cost Explorer data only)"),
        _kpi_line("K-FIN-HARDSTOP", stops, "no_data" if stops is None else "amber" if stops else "ok",
                  "budget bells for a stopped run (the gateway keeps no refusal log)"),
    ]


def budgets_md(b: dict) -> list[str]:
    md = table(["Line", "Budget", "Actual", "Variance", "Forecast at day 15", "Forecast error"],
               [[x["line"], usd(x["budget_usd"]), usd(x["actual_usd"]), signed(x["variance"]),
                 usd(x["forecast_day15_usd"]), pct(x["forecast_accuracy"])] for x in b["lines"]]) + [""]
    if b["error"]:
        md += [f"- Forecast not readable: {b['error']}", ""]
    y = b["ytd"]
    md += [f"- Year to date: {usd(y['usd'])} of {usd(y['plan_usd'])} planned for {y['months']} month(s) "
           f"({pct(y['ratio'])}); stored data from {y['covered_from']}." if y
           else "- Year to date: no stored Cost Explorer data."]
    md += ["- Daily caps (shared.spend_budget): " + (", ".join(
        f"{s['provider']}/{s['scope']} {usd(s['per_day_usd'])} a day{' (hard stop)' if s['hard_stop'] else ''}"
        for s in b["spend_budgets"]) or "none") + "."]
    a = b["alerts"]
    md += [f"- Budget bells: {a['bells']}, runs stopped: {a['stops']}." if "error" not in a
           else f"- Budget bells not readable: {a['error']}"]
    md += ["", f"Proposed budget changes (Terraform per-App budgets) with reasons: {TO_WRITE}.", ""]
    return md


# ── OPS-08 capacity ─────────────────────────────────────────────────────────────────────────────────────────────


def _percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def capacity(facts: dict) -> dict:
    rds = facts.get("rds") or {}
    db = facts.get("db")
    db = None if db is None or failed(db) else db
    out: dict[str, Any] = {"rds_unavailable": rds.get("unavailable") or failed(rds)}
    cpu = [float(v) for _t, v in rds.get("cpu") or []]
    conn = [float(v) for _t, v in rds.get("connections") or []]
    free = rds.get("free_storage") or []
    out["cpu_hours_above"] = sum(1 for v in cpu if v > CPU_BASELINE_PCT) if cpu else None
    out["cpu_max_pct"] = round(max(cpu), 1) if cpu else None
    out["conn_max"] = max(conn) if conn else None
    out["conn_p95"] = _percentile(conn, 0.95)
    max_conn = (db or {}).get("max_connections")
    out["conn_p95_ratio"] = ratio(out["conn_p95"], max_conn)

    def growth(points: list, days: int) -> Optional[float]:
        if len(points) < 2:
            return None
        last_t, last_v = points[-1]
        start = [p for p in points if (last_t - p[0]).total_seconds() <= days * 86400]
        if len(start) < 2:
            return None
        t0, v0 = start[0]
        span = (last_t - t0).total_seconds() / 86400
        return round((v0 - last_v) / 1e9 / span, 3) if span > 0 else None
    out["storage_gb_per_day_30d"] = growth(free, 30)
    out["storage_gb_per_day_7d"] = growth(free, 7)
    out["free_storage_gb"] = round(free[-1][1] / 1e9, 1) if free else None
    out["db"] = db
    kp = facts.get("job_kpis")
    if kp is not None and not failed(kp):
        fails = [float(r["value"]) for r in kp if r["kpi_id"] == "K-JOB-FAIL"]
        ages = [float(r["value"]) for r in kp if r["kpi_id"] == "K-JOB-QUEUEAGE"]
        out["jobs"] = {"failed": int(sum(fails)), "days": len(fails),
                       "queue_age_p95_max_s": max(ages) if ages else None}
    runs = facts.get("digests")
    if runs is not None and not failed(runs):
        per_app: dict[str, list[int]] = {}
        for r in runs:
            for a in r["apps"] or []:
                if a.get("status") == "no_probe":
                    continue
                per_app.setdefault(a.get("id") or "?", []).append(1 if a.get("status") == "ok" else 0)
        out["availability"] = {k: {"ok_days": sum(v), "probed_days": len(v), "ratio": ratio(sum(v), len(v))}
                               for k, v in sorted(per_app.items())}
        out["digest_days"] = len(runs)
    services = facts.get("services")
    if services is not None and not failed(services):
        out["rds_cost_usd"] = round(sum(s["amount_usd"] for s in services["services"]
                                        if "Relational Database" in s["service"]), 2)
    return out


def capacity_kpis(c: dict) -> list[dict]:
    av = c.get("availability") or {}
    worst = min((v["ratio"] for v in av.values() if v["ratio"] is not None), default=None)
    rds_note = f"CloudWatch not readable: {c['rds_unavailable']}" if c.get("rds_unavailable") else ""
    return [
        _kpi_line("K-CAP-CPU", c.get("cpu_hours_above"), "no_data" if c.get("cpu_hours_above") is None else "info",
                  rds_note or f"hours above {CPU_BASELINE_PCT:.0f} % in 30 days; max {c.get('cpu_max_pct')} %"),
        _kpi_line("K-CAP-STORAGE", c.get("storage_gb_per_day_30d"),
                  "no_data" if c.get("storage_gb_per_day_30d") is None else
                  "amber" if (c.get("storage_gb_per_day_7d") or 0) > 1 else "ok",
                  rds_note or f"GB a day over 30 days; 7 days {c.get('storage_gb_per_day_7d')}"),
        _kpi_line("K-CAP-CONN", c.get("conn_p95_ratio"), "no_data" if c.get("conn_p95_ratio") is None else
                  "amber" if c["conn_p95_ratio"] >= 0.6 else "ok",
                  rds_note or f"p95 {c.get('conn_p95')} / max {c.get('conn_max')} of hourly maximums"),
        _kpi_line("K-AVAIL", worst, "no_data" if worst is None else "info",
                  "lowest app share of OPS-12 days all healthy (one probe a day, Dev)"),
        _kpi_line("K-CAP-TRIGGERS", None, NOT_WIRED, "C3 triggers and their decisions are written by the tech lead"),
    ]


def capacity_md(c: dict) -> list[str]:
    db = c.get("db") or {}
    md = ["## Database", ""]
    if db:
        md += [f"- Size {db['database_bytes'] / 1e9:.2f} GB; free storage {fmt(c.get('free_storage_gb'), 'count')} GB.",
               f"- Connections now {db.get('connections')} of max_connections {db.get('max_connections')}; login "
               f"roles' connection limits add up to {db.get('role_limits_total')} (connection budget C2.2).", "",
               "Largest relations:", ""]
        md += table(["Relation", "Size (MB)"], [[f"{r['schema']}.{r['name']}", f"{r['bytes'] / 1e6:,.1f}"]
                                               for r in db.get("top_relations") or []])
    else:
        md += ["- Database catalog not readable."]
    md += ["", "## RDS metrics (30 days, hourly)", ""]
    if c.get("rds_unavailable"):
        md += [f"- CloudWatch not readable: {c['rds_unavailable']}."]
    else:
        md += [f"- CPU: {c.get('cpu_hours_above')} hours above {CPU_BASELINE_PCT:.0f} %, max {c.get('cpu_max_pct')} %.",
               f"- Connections: max {c.get('conn_max')}, p95 {c.get('conn_p95')}.",
               f"- Storage growth: {c.get('storage_gb_per_day_30d')} GB a day (30 days), "
               f"{c.get('storage_gb_per_day_7d')} (7 days)."]
    md += ["", "## Jobs and availability", ""]
    j = c.get("jobs")
    md += [f"- CIS jobs failed in the month: {j['failed']} over {j['days']} snapshot days; worst daily queue-age p95 "
           f"{fmt(j['queue_age_p95_max_s'], 'seconds')}." if j else "- Job KPIs not readable."]
    av = c.get("availability")
    if av:
        md += table(["App", "Days healthy", "Days probed", "Share"],
                    [[k, v["ok_days"], v["probed_days"], pct(v["ratio"])] for k, v in av.items()])
    else:
        md += ["- No OPS-12 digest in the month (availability not measured)."]
    md += ["", f"- RDS line of the bill this month: {usd(c.get('rds_cost_usd'))} (stored Cost Explorer data).", "",
           "## Monitoring gaps and decisions", "", f"{TO_WRITE}: C3 triggers fired, gaps vs C4.1, resize / tune / "
           "accept.", ""]
    return md


# ── monthly renderers ───────────────────────────────────────────────────────────────────────────────────────────


def _aws_line(status: str, covered_to: Any, fetched_at: Any, month: str) -> str:
    note = AWS_STATUS_NOTE[status]
    tail = "" if status == "final" else (f" Open Platform › Cost per app ({PLATFORM_COST_PAGE}?month={month}) once to "
                                         "refresh it (one Cost Explorer request, about 0.01 USD), then generate this "
                                         "report again.")
    return (f"AWS data: **{status}** ({note}; covered to {covered_to or '—'}, fetched "
            f"{str(fetched_at or '—')[:16]}).{tail}")


def _month_context(facts: dict) -> tuple[str, date, date]:
    m = facts["months"][-1]
    return m["key"], _d(m["first"]), _d(m["last"])


def render_fi01(reg, report, first, last, facts, generated_at) -> Rendered:
    from api.services import cost_ops

    key, _f, _l = _month_context(facts)
    ab = by_app(facts, cost_ops.app_budgets())
    ac = accounts(facts, cost_ops.aws_budgets())
    mv = movers(facts)
    lm = llm_month(facts)
    vd = vendors(facts)
    un = units(facts)
    bd = budgets(facts, cost_ops.aws_budgets(), ab)
    acc2_hist = ab["acc2_history_usd"]
    acc2_budget = cost_ops.aws_budgets().get("acc2")
    tour = [m["tour"] for m in un["months"]]
    worst = ab["worst"]
    kpis = [
        _kpi_line("K-COST-ACC2", ratio(acc2_hist[-1], acc2_budget),
                  "no_data" if acc2_hist[-1] is None else "red" if acc2_hist[-1] > acc2_budget else "ok",
                  f"{usd(acc2_hist[-1])} of {usd(acc2_budget)}; last month {usd(acc2_hist[-2])}, 3-month average "
                  f"{usd(avg(acc2_hist))}"),
        _kpi_line("K-COST-APP", worst["ratio"] if worst else None, worst["status"] if worst else "no_data",
                  f"worst: {worst['app']}" if worst else ""),
        _kpi_line("K-COST-UNTAGGED", ab["untagged_ratio"], "no_data" if ab["untagged_ratio"] is None else
                  "amber" if ab["untagged_ratio"] >= 0.05 else "ok"),
        _kpi_line("K-COST-EXPLAINED", None, "to_write" if mv["movers"] else "ok",
                  f"{len(mv['movers'])} mover(s); reasons are written in section A4"),
        next(k for k in llm_kpis(lm) if k["id"] == "K-LLM-RECON"),
        _kpi_line("K-UNIT-TOUR", tour[-1], "no_data" if tour[-1] is None else
                  "amber" if (change(tour[-1], tour[-2]) or 0) > 0.20 else "ok",
                  f"last month {usd(tour[-2])}, 3-month average {usd(avg(tour))}"),
    ]
    status = ab["aws_status"]
    total = sum(r["usd"] for r in ac["rows"] if r["usd"] is not None) if ac["rows"] else None
    headline = (f"Cost review {key}: AWS {usd(total)} across accounts (data {status}), LLM "
                f"{usd(lm.get('cost_usd'))}, vendors {usd(vd['total_usd'])}")
    md = _header(report, first, last, generated_at, f"Cost review {key} (FI-01 draft)")
    md += [_aws_line(status, ab["covered_to"], ab["fetched_at"], key), ""]
    md += ["## Summary for the business owner", "", f"NARRATIVE ({TO_WRITE}): five Vietnamese lines: total, vs "
           "budget, biggest mover, unit cost, decision.", ""]
    md += ["## How to read this report", "",
           "- Budgets are monthly per account and per App tag; red = actual above budget.",
           "- The LLM account is shown as Cost Explorer stores it (gross unless credits were applied in the fetch).",
           f"- A mover changed more than {int(MOVER_RATIO * 100)} % and more than {usd(MOVER_USD)}; every mover needs "
           "a reason.", ""]
    md += ["## A1. KPIs", ""] + table(KPI_HEADERS, _kpi_rows(reg, kpis, {"K-COST-APP": "ratio",
                                                                         "K-UNIT-TOUR": "usd"})) + [""]
    md += ["## A2. Totals by account", ""]
    md += table(["Account", "This month", "Last month", "Budget", "Variance"],
                [[r["account"], usd(r["usd"]), usd(r["prev_usd"]), usd(r["budget_usd"]), usd(r["variance_usd"])]
                 for r in ac["rows"]]) + ["", f"Per-account data: {ac['aws_status']}." + (
                     f" Not readable: {ac['error']}" if ac["error"] else ""), ""]
    md += ["## A3. By App tag (AWS main account)", ""]
    md += table(["App", "This month", "Last month", "3-month average", "Budget", "Actual / budget", "Status",
                 "AI outside the AWS bill"],
                [[r["app"], usd(r["aws_usd"]), usd(r["prev_usd"]), usd(r["avg_3m_usd"]), usd(r["budget_usd"]),
                  pct(r["ratio"]), r["status"], usd(r["ai_outside_aws_usd"])] for r in ab["rows"]]) + [""]
    md += ["## A4. Top services and movers", ""]
    if mv.get("error"):
        md += [f"- {mv['error']}", ""]
    else:
        md += table(["Account", "Service", "This month", "Last month"],
                    [[t["account"], t["service"], usd(t["usd"]), usd(t["prev_usd"])] for t in mv["top"]]) + [""]
        md += (table(["Account", "Service", "This month", "Last month", "Change", "Reason"],
                     [[m["account"], m["service"], usd(m["usd"]), usd(m["prev_usd"]), signed(m["change"]), TO_WRITE]
                      for m in mv["movers"]]) if mv["movers"] else ["- No mover."]) + [""]
    md += ["## A5. LLM spend (FI-03)", ""] + llm_md(lm, "###")
    md += ["## A6. Vendors and credits (FI-04)", ""] + vendors_md(vd)
    md += ["## A7. Unit costs (FI-05)", ""] + units_md(un)
    md += ["## A8. Budgets, alerts and hard stops (FI-06)", ""] + budgets_md(bd)
    unreadable = sorted(k for k, v in facts.items() if failed(v))
    md += ["## Decisions needed", "", f"{TO_WRITE}.", "", "## Actions", "", f"{TO_WRITE}.", "",
           "## Sources", "", "Stored rows only: the Cost Explorer snapshots, the LLM ledger, the vendor ledgers, "
           "shared.spend_budget and the budget bells (services/reports/money_sources.py). "
           + (f"Not readable this run: {', '.join(unreadable)}. " if unreadable else "")
           + f"Pages: {PLATFORM_COST_PAGE}?month={key}, {COST_PAGE}.", ""]
    out = _output(report, first, last, generated_at, headline, draft=True, kpis=kpis, aws_status=status,
                  accounts=ac, by_app=ab, movers=mv, llm=lm, vendors=vd, units=un, budgets=bd,
                  sources_unreadable={k: failed(v) for k, v in facts.items() if failed(v)})
    return Rendered(headline[:1000], out, "\n".join(md))


def _simple(reg, report, first, last, generated_at, title, headline, kpis, body, **data) -> Rendered:
    md = _header(report, first, last, generated_at, title)
    md += ["## KPIs", ""] + table(KPI_HEADERS, _kpi_rows(reg, kpis)) + [""] + body
    out = _output(report, first, last, generated_at, headline, kpis=kpis, **data)
    return Rendered(headline[:1000], out, "\n".join(md))


def render_fi03(reg, report, first, last, facts, generated_at) -> Rendered:
    key, _f, _l = _month_context(facts)
    lm = llm_month(facts)
    headline = f"LLM cost {key}: {usd(lm.get('cost_usd'))} over {lm.get('calls', 0):,} calls"
    return _simple(reg, report, first, last, generated_at, f"LLM cost attribution {key}", headline, llm_kpis(lm),
                   llm_md(lm), llm=lm, sources_unreadable={k: failed(v) for k, v in facts.items() if failed(v)})


def render_fi04(reg, report, first, last, facts, generated_at) -> Rendered:
    key, _f, _l = _month_context(facts)
    vd = vendors(facts)
    headline = f"Vendors {key}: {usd(vd['total_usd'])} usage spend"
    body = ["## Vendor spend and usage", ""] + vendors_md(vd) + [
        "## Free-plan headroom", "", f"{TO_WRITE}: Vercel Hobby, Sentry free, GitHub Actions minutes, Geoapify free "
        "tier, Linear Free (250 unarchived issues); the platform does not read their usage.", "",
        "## Contracts and DPAs", "", f"{TO_WRITE}: start, renewal, notice period, DPA / zero-retention status, "
        "renewals in the next 60 days (legal register).", ""]
    return _simple(reg, report, first, last, generated_at, f"Vendor register {key} (FI-04 draft)", headline,
                   vendor_kpis(vd), body, draft=True, vendors=vd,
                   sources_unreadable={k: failed(v) for k, v in facts.items() if failed(v)})


def render_fi05(reg, report, first, last, facts, generated_at) -> Rendered:
    key, _f, _l = _month_context(facts)
    un = units(facts)
    lm = llm_month(facts)
    this = un["months"][-1]
    pub = this["published_tours"]
    headline = (f"Unit costs {key}: {usd(this['tour'])} per published tour "
                f"({'tour count not readable' if pub is None else f'{pub:,} tours published or republished'})")
    body = ["## Unit cost table", ""] + units_md(un)
    if "error" not in lm:
        body += ["## LLM units per model", ""] + table(
            ["Model", "Provider / account", "USD per 1,000 calls", "USD per 1M tokens"],
            [[m["model"], m["source"], usd(m["usd_per_1k_calls"]), usd(m["usd_per_1m_tokens"])]
             for m in lm["models"]]) + [""]
    body += ["## Commentary on movers", "", f"NARRATIVE ({TO_WRITE}).", ""]
    return _simple(reg, report, first, last, generated_at, f"Unit economics {key}", headline, unit_kpis(un), body,
                   units=un, sources_unreadable={k: failed(v) for k, v in facts.items() if failed(v)})


def render_fi06(reg, report, first, last, facts, generated_at) -> Rendered:
    from api.services import cost_ops

    key, _f, _l = _month_context(facts)
    ab = by_app(facts, cost_ops.app_budgets())
    bd = budgets(facts, cost_ops.aws_budgets(), ab)
    headline = f"Budget vs actual {key}: worst variance {signed(bd['worst_variance'])}"
    body = ["## Budget table", "", _aws_line(ab["aws_status"], ab["covered_to"], ab["fetched_at"], key), ""]
    body += budgets_md(bd) + ["## Credits", ""] + credits_md(bd["balances"])
    return _simple(reg, report, first, last, generated_at, f"Budget vs actual {key} (FI-06 draft)", headline,
                   budget_kpis(bd), body, draft=True, budgets=bd, aws_status=ab["aws_status"],
                   sources_unreadable={k: failed(v) for k, v in facts.items() if failed(v)})


def render_ops08(reg, report, first, last, facts, generated_at) -> Rendered:
    key, _f, _l = _month_context(facts)
    cap = capacity(facts)
    kpis = capacity_kpis(cap)
    headline = (f"Capacity review {key}: CPU {cap.get('cpu_hours_above')} h above baseline, connections p95 "
                f"{pct(cap.get('conn_p95_ratio'))} of max")
    return _simple(reg, report, first, last, generated_at, f"Capacity, SLO and availability {key} (OPS-08 draft)",
                   headline, kpis, capacity_md(cap), draft=True, capacity=cap,
                   sources_unreadable={k: failed(v) for k, v in facts.items() if failed(v)})


# ── loaders + dispatch (generate.RENDERERS) ─────────────────────────────────────────────────────────────────────

MONTH_NEEDS = {
    "FI-01": ("cost_by_app", "services", "llm", "vendors", "units", "budgets"),
    "FI-03": ("cost_by_app", "llm"),
    "FI-04": ("vendors", "llm"),
    "FI-05": ("cost_by_app", "units", "llm"),
    "FI-06": ("cost_by_app", "budgets", "vendors", "llm"),     # llm: acc3 burn and Jev rate of the Credits section
    "OPS-08": ("capacity",),
}
MONTH_RENDER = {"FI-01": render_fi01, "FI-03": render_fi03, "FI-04": render_fi04, "FI-05": render_fi05,
                "FI-06": render_fi06, "OPS-08": render_ops08}


def month_of(first: date, last: date) -> tuple[date, date]:
    """A monthly report covers the calendar month of its period's last day, from the 1st to that day (a scheduled
    run covers a whole closed month; a manual run may stop earlier: month to date)."""
    return periods.month_bounds(last)[0], last


async def render_week(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    facts = await src.collect_week(conn, first, last, with_business=report.id == "EX-01")
    fn = render_ex01 if report.id == "EX-01" else render_fi02
    return fn(reg, report, first, last, facts, now)


async def render_month(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    mfirst, mlast = month_of(first, last)
    facts = await src.collect_month(conn, mfirst, mlast, now, MONTH_NEEDS[report.id])
    return MONTH_RENDER[report.id](reg, report, mfirst, mlast, facts, now)
