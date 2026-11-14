"""AA-1003 R5 — B2B go-live and Booking operations reports (root ADR 0019: the B2B-only model; AA end-customer reports
are dropped).

  FI-08  monthly: tenant entitlements and AI spend against the cap. For every tenant with a live subscription or AI
         spend in the period: its live subscriptions (shared.tenant_subscription: product, plan, status, trial end,
         paid through), the entitlements in force (shared.tenant_entitlement, the materialised feed of contract
         tenant.entitlements.v1: flags, limits and seats, plan / trial / override, the earliest expiry), and its AI
         spend in the CIS AI gateway ledger (shared.llm_call_daily rolled days + shared.llm_call_log raw days, the
         FI-03 rule; shadow rows out, the Tenant 360 rule) against the AI budget the gateway enforces now
         (api/services/tenant_ai_budget: the resolved ai.budget_usd_monthly, an override on Tenant 360 > the
         per-tenant budget row > the plan's included spend). The state per tenant (ok / alert / approval / over /
         unlimited) uses that tenant's own alert % and approval %, which staff edit on AI providers > Budgets and
         Tenant 360 (audited): this report adds no threshold of its own. The cap is the one in force when the report
         is generated (no history of past caps is stored); the spend is the period's.
  BK-03  weekly: agency receivables (SaaS fees and settlement invoices, issued by AA-Booking) from Booking's contract
         view booking.v_tenant_billing_status (bk-042, SELECT to cis_app; the hourly tenant_billing_tick reads the
         same view): per agency the billing status, overdue invoices, the age of the oldest overdue invoice (in the
         catalog's aging buckets), overdue amount per currency, paid through and the agency's warn / suspend days.
         Status colours come from each agency's own warn / suspend days (Booking billing_terms, set by finance), not
         from a number here. The view is a snapshot of now, so the report states the time it was read; the trend is
         against the previous BK-03 run's stored output. Per-invoice balances, payments awaiting verification and
         supplier bills (AP) are staff-only in Booking (no contract view): named "not published to CIS".
  FI-07  weekly flash, counts only: trip cases created, proposals sent, bookings confirmed and cancelled per source
         system (crm = agency bookings through the B2B API) per UTC day from Booking's contract view
         booking.v_ops_funnel_daily (bk-017, ops.overview.v1: first time a case reached the status), with the four
         weeks before, and the open pipeline by current status (booking.v_ops_case_status). GMV, margin, cash and FX
         live in Booking's staff-only finance views (v_case_profit, v_agency_profit_month): "not published to CIS".

R5 rows of the catalog without a live source here stay `generator: null` (OPS-17, SEC-10, SEC-09, DEV-04, OPS-03 are
event / checklist documents; BK-01 reads Booking tables with no contract view; EX-02 composes other reports).

Stored rows and contract views only (cis_app); no LLM, no paid API, $0.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from api.services import tenant_ai_budget
from api.services.ops_overview import activity
from services.reports import money_sources as src
from services.reports.generate import Rendered
from services.reports.money import _kpi_line, failed, pct, ratio, table, usd
from services.reports.registry import Registry, Report
from services.reports.reliability import _render, _unreadable, previous_output
from services.tenant_account import billing

UTC = timezone.utc
UTC_NOTE = ("Days are UTC calendar days here (the AI gateway's budget month and Booking's contract views count UTC "
            "days); the period is the report's.")
NOT_PUBLISHED = "not published to CIS"
LIVE_SUB = ("trialing", "active", "past_due")
# Tenant 360 / FI-03 rule: tenants are shown by id prefix and kind, never by name (the output is copied to audits).
ID_PREFIX = 8
TREND_WEEKS = 4

# ═══ FI-08 tenant entitlements and AI spend against the cap ═════════════════════════════════════════════════════

TENANTS_SQL = """
    SELECT tenant_id::text AS tenant_id, kind, account_status, is_pilot
      FROM shared.tenants
     ORDER BY tenant_id
"""
SUBSCRIPTIONS_SQL = """
    SELECT tenant_id::text AS tenant_id, product, plan_key, status, trial_ends_at, paid_through, seats
      FROM shared.tenant_subscription
     ORDER BY tenant_id, product
"""
ENTITLEMENTS_SQL = """
    SELECT tenant_id::text AS tenant_id, product, value_kind, source, count(*)::int AS n, min(expires_at) AS expires_at
      FROM shared.tenant_entitlement
     GROUP BY 1, 2, 3, 4
"""
# The tenant-attributed AI spend of UTC days [$1, $2): rolled days from shared.llm_call_daily, raw days from
# shared.llm_call_log (a day is one or the other, never both: services/retention; the FI-03 ledger rule), shadow rows
# out (Tenant 360 and the gateway's budget read the same way). tenant_id NULL = platform work.
TENANT_SPEND_SQL = """
    WITH rolled AS (
        SELECT d.tenant_id, d.app, d.calls, d.refused_calls, d.cost_usd
          FROM shared.llm_call_daily d
         WHERE d.day >= $1 AND d.day < $2 AND NOT d.is_shadow
    ), raw AS (
        SELECT l.tenant_id, coalesce(l.app, l.quality_signal ->> 'app', 'cis') AS app, count(*)::bigint AS calls,
               count(*) FILTER (WHERE l.status = 'refused')::bigint AS refused_calls,
               coalesce(sum(l.cost_usd), 0) AS cost_usd
          FROM shared.llm_call_log l
         WHERE l.created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
           AND l.created_at < ($2::date::timestamp AT TIME ZONE 'UTC')
           AND NOT coalesce(l.is_shadow, l.quality_signal @> '{"shadow": true}'::jsonb)
           AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                            WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
         GROUP BY 1, 2
    )
    SELECT tenant_id::text AS tenant_id, app, sum(calls)::bigint AS calls, sum(refused_calls)::bigint AS refused_calls,
           sum(cost_usd)::float AS cost_usd
      FROM (SELECT * FROM rolled UNION ALL SELECT * FROM raw) u
     GROUP BY 1, 2
"""


def short(tenant_id: Optional[str]) -> str:
    return (tenant_id or "")[:ID_PREFIX] or "platform"


async def tenant_spend(conn, first: date, last: date) -> list[dict]:
    return [dict(r) for r in await conn.fetch(TENANT_SPEND_SQL, first, last + timedelta(days=1))]


async def caps(conn, tenant_ids: list[str]) -> dict:
    """{tenant_id: the AI budget the gateway applies now (tenant_ai_budget._effective) or None (before CIS 353)}."""
    return {tid: await tenant_ai_budget._effective(conn, tid) for tid in tenant_ids}


async def collect_fi08(conn, first: date, last: date) -> dict:
    facts = await src.run_parts({
        "tenants": lambda: _rows(conn, TENANTS_SQL),
        "subscriptions": lambda: _rows(conn, SUBSCRIPTIONS_SQL),
        "entitlements": lambda: _rows(conn, ENTITLEMENTS_SQL),
        "spend": lambda: tenant_spend(conn, first, last),
    })
    facts["caps"] = (await src.run_parts({"caps": lambda: caps(conn, in_scope(facts))}))["caps"]
    return facts


async def _rows(conn, sql: str) -> list[dict]:
    return [dict(r) for r in await conn.fetch(sql)]


def in_scope(facts: dict) -> list[str]:
    """Tenants the report lists: a live subscription, or AI spend in the period. Sorted."""
    ids: set[str] = set()
    subs = facts.get("subscriptions")
    if isinstance(subs, list):
        ids |= {s["tenant_id"] for s in subs if s["status"] in LIVE_SUB}
    spend = facts.get("spend")
    if isinstance(spend, list):
        ids |= {r["tenant_id"] for r in spend if r["tenant_id"]}
    return sorted(ids)


def _day(v: Any) -> Optional[str]:
    if isinstance(v, datetime):
        return v.astimezone(UTC).date().isoformat()
    return v.isoformat() if isinstance(v, date) else None


def tenant_rows(facts: dict) -> dict:
    """Per tenant in scope: kind, account, live subscriptions, entitlements, spend, cap and state; plus the platform
    spend and totals. Pure. {"error"} when the tenant list or the ledger cannot be read."""
    for part in ("tenants", "spend"):
        if failed(facts.get(part)) or facts.get(part) is None:
            return {"error": f"{part}: {failed(facts.get(part)) or 'not read'}"}
    tenants = {t["tenant_id"]: t for t in facts["tenants"]}
    subs = facts.get("subscriptions") if isinstance(facts.get("subscriptions"), list) else []
    ents = facts.get("entitlements") if isinstance(facts.get("entitlements"), list) else []
    cap_map = facts.get("caps") if isinstance(facts.get("caps"), dict) and not failed(facts.get("caps")) else {}
    spend: dict[Optional[str], dict] = {}
    for r in facts["spend"]:
        s = spend.setdefault(r["tenant_id"], {"cost_usd": 0.0, "calls": 0, "refused_calls": 0, "by_app": {}})
        s["cost_usd"] = round(s["cost_usd"] + float(r["cost_usd"] or 0), 6)
        s["calls"] += int(r["calls"] or 0)
        s["refused_calls"] += int(r["refused_calls"] or 0)
        s["by_app"][r["app"]] = round(s["by_app"].get(r["app"], 0.0) + float(r["cost_usd"] or 0), 6)
    rows = []
    for tid in in_scope(facts):
        t = tenants.get(tid, {})
        live = [s for s in subs if s["tenant_id"] == tid and s["status"] in LIVE_SUB]
        mine = [e for e in ents if e["tenant_id"] == tid]
        kinds: dict[str, int] = {}
        for e in mine:
            kinds[e["value_kind"]] = kinds.get(e["value_kind"], 0) + int(e["n"])
        expiries = [e["expires_at"] for e in mine if e.get("expires_at") is not None]
        sp = spend.get(tid, {"cost_usd": 0.0, "calls": 0, "refused_calls": 0, "by_app": {}})
        cap = cap_map.get(tid)       # None: not read, or no resolver yet (before CIS 353)
        monthly = None if cap is None else cap.get("monthly_usd")
        if cap is not None and monthly is not None:
            used, state = tenant_ai_budget.budget_state(float(monthly), sp["cost_usd"], int(cap["alert_pct"]),
                                                        int(cap["approval_pct"]), bool(cap["hard_stop"]))
        else:
            used, state = None, ("unknown" if cap is None else "unlimited")
        rows.append({
            "tenant": short(tid), "tenant_id": tid, "kind": t.get("kind"), "account_status": t.get("account_status"),
            "pilot": bool(t.get("is_pilot")),
            "subscriptions": [{"product": s["product"], "plan_key": s["plan_key"], "status": s["status"],
                               "trial_ends_at": _day(s.get("trial_ends_at")),
                               "paid_through": _day(s.get("paid_through")), "seats": s.get("seats")} for s in live],
            "entitlements": {"total": sum(kinds.values()), **kinds,
                             "sources": sorted({e["source"] for e in mine}),
                             "next_expiry": _day(min(expiries)) if expiries else None},
            "spend_usd": round(sp["cost_usd"], 4), "calls": sp["calls"], "refused_calls": sp["refused_calls"],
            "by_app": sp["by_app"],
            "cap": None if cap is None else {"source": cap.get("source"), "monthly_usd": monthly,
                                             "alert_pct": cap.get("alert_pct"), "approval_pct": cap.get("approval_pct"),
                                             "hard_stop": cap.get("hard_stop")},
            "used_pct": used, "state": state,
        })
    platform = spend.get(None, {"cost_usd": 0.0, "calls": 0})
    return {"rows": rows, "platform_usd": round(platform["cost_usd"], 4), "platform_calls": platform["calls"],
            "tenant_usd": round(sum(r["spend_usd"] for r in rows), 4),
            "caps_unreadable": failed(facts.get("caps")),
            "subscriptions_unreadable": failed(facts.get("subscriptions")),
            "entitlements_unreadable": failed(facts.get("entitlements"))}


def fi08_kpis(tr: dict) -> list[dict]:
    if "error" in tr:
        why = f"not readable: {tr['error']}"
        return [_kpi_line(k, None, "no_data", why) for k in ("K-TEN-LIVE", "K-LLM-TENANT", "K-TEN-AICAP",
                                                              "K-TEN-AINOCAP")]
    rows = tr["rows"]
    live = [r for r in rows if r["subscriptions"]]
    alert = [r for r in rows if r["state"] == "alert"]
    stopped = [r for r in rows if r["state"] in ("approval", "over")]
    nocap = [r for r in rows if r["state"] == "unlimited" and r["spend_usd"] > 0]
    unknown = [r for r in rows if r["state"] == "unknown"]
    if tr["subscriptions_unreadable"]:
        live_line = _kpi_line("K-TEN-LIVE", None, "no_data", f"not readable: {tr['subscriptions_unreadable']}")
    else:
        live_line = _kpi_line("K-TEN-LIVE", float(len(live)), "info",
                              ", ".join(f"{k} {n}" for k, n in sorted(_count_by(live, "kind").items())) or "none")
    if tr["caps_unreadable"] or (unknown and len(unknown) == len(rows)):
        cap_line = _kpi_line("K-TEN-AICAP", None, "no_data",
                             f"caps not readable: {tr['caps_unreadable'] or 'the resolver is not in the database'}")
        nocap_line = _kpi_line("K-TEN-AINOCAP", None, "no_data", "caps not readable")
    else:
        cap_line = _kpi_line(
            "K-TEN-AICAP", float(len(alert) + len(stopped)),
            "red" if stopped else "amber" if alert else "ok",
            (f"{len(stopped)} at or past the approval threshold ({', '.join(r['tenant'] for r in stopped)}); "
             if stopped else "") + (f"{len(alert)} past the alert threshold ({', '.join(r['tenant'] for r in alert)})"
                                    if alert else "none past its alert threshold")
            + "; each tenant's own alert % and approval %")
        nocap_line = _kpi_line("K-TEN-AINOCAP", float(len(nocap)), "amber" if nocap else "ok",
                               ", ".join(f"{r['tenant']} {usd(r['spend_usd'])}" for r in nocap) or
                               "every tenant with AI spend has a budget")
    return [live_line,
            _kpi_line("K-LLM-TENANT", tr["tenant_usd"], "info",
                      f"{sum(1 for r in rows if r['spend_usd'] > 0)} tenant(s) with spend; platform work "
                      f"{usd(tr['platform_usd'])}"),
            cap_line, nocap_line]


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return out


def _subs_text(subs: list[dict]) -> str:
    if not subs:
        return "no live subscription"
    parts = []
    for s in subs:
        extra = f", trial to {s['trial_ends_at']}" if s["status"] == "trialing" and s["trial_ends_at"] else ""
        parts.append(f"{s['plan_key']} ({s['status']}{extra})")
    return "; ".join(parts)


def _cap_text(r: dict) -> str:
    c = r["cap"]
    if c is None:
        return "not readable" if r["state"] == "unknown" else "no budget"
    if c["monthly_usd"] is None:
        return f"unlimited ({c['source']})"
    stop = "stop" if c["hard_stop"] else "alert only"
    return f"{usd(c['monthly_usd'])} ({c['source']}; alert {c['alert_pct']} %, approval {c['approval_pct']} %, {stop})"


def render_fi08(reg: Registry, report: Report, first: date, last: date, facts: dict,
                generated_at: datetime) -> Rendered:
    tr = tenant_rows(facts)
    kpis = fi08_kpis(tr)
    title = f"Tenant entitlements and AI spend {first.isoformat()} to {last.isoformat()}"
    if "error" in tr:
        headline = f"{title}: not readable ({tr['error']})"
        body = [f"- Not readable: {tr['error']}", ""]
        return _render(reg, report, first, last, generated_at, title, headline, kpis, body, UTC_NOTE,
                       tenants=tr, sources_unreadable=_unreadable(facts))
    rows = tr["rows"]
    flagged = [r for r in rows if r["state"] in ("alert", "approval", "over")]
    headline = (f"{title}: {len(rows)} tenants, AI spend {usd(tr['tenant_usd'])} attributed to tenants "
                f"(platform {usd(tr['platform_usd'])}), {len(flagged)} at or past their alert threshold")
    body = ["## AI spend against the cap", "",
            "Spend is the period's (UTC days, shadow calls out); the cap and thresholds are the ones the AI gateway "
            "applies now (Tenant 360 override > tenant budget row > the plan's included spend). Staff change them on "
            "AI providers > Budgets and Tenant 360.", ""]
    if rows:
        body += table(["Tenant", "Kind", "Account", "Cap", "Spend", "Used", "State", "Calls", "Refused"],
                      [[r["tenant"] + (" (pilot)" if r["pilot"] else ""), r["kind"], r["account_status"],
                        _cap_text(r), usd(r["spend_usd"]),
                        "—" if r["used_pct"] is None else f"{r['used_pct']:.1f} %", r["state"], r["calls"],
                        r["refused_calls"]] for r in rows])
    else:
        body += ["- No tenant with a live subscription or AI spend in the period."]
    by_app: dict[str, float] = {}
    for r in rows:
        for a, v in r["by_app"].items():
            by_app[a] = round(by_app.get(a, 0.0) + v, 6)
    body += ["", "- Tenant-attributed spend by app: "
             + (", ".join(f"{a} {usd(v)}" for a, v in sorted(by_app.items())) or "none") + f". Platform work (no "
             f"tenant): {usd(tr['platform_usd'])} over {tr['platform_calls']} calls.", ""]
    body += ["## Entitlements in force (tenant.entitlements.v1)", ""]
    if tr["entitlements_unreadable"] or tr["subscriptions_unreadable"]:
        body += [f"- Not readable: {tr['entitlements_unreadable'] or tr['subscriptions_unreadable']}", ""]
    else:
        body += table(["Tenant", "Live subscriptions", "Entitlements", "Flags", "Limits", "Seats", "Sources",
                       "Earliest expiry"],
                      [[r["tenant"], _subs_text(r["subscriptions"]), r["entitlements"]["total"],
                        r["entitlements"].get("flag", 0), r["entitlements"].get("limit", 0),
                        r["entitlements"].get("seats", 0), ", ".join(r["entitlements"]["sources"]) or "—",
                        r["entitlements"]["next_expiry"] or "—"] for r in rows])
        trials = [(r["tenant"], s) for r in rows for s in r["subscriptions"] if s["status"] == "trialing"]
        body += ["", "### Trials", ""]
        body += [f"- {t}: {s['plan_key']} trial ends {s['trial_ends_at'] or '—'}" for t, s in trials] or \
            ["- No subscription is in trial."]
        body += [""]
    body += ["## Notes", "",
             "- Tenants are shown by the first characters of their id (the FI-03 rule); Tenant 360 opens one by id.",
             "- No history of past caps is stored: a closed month is read against today's cap.",
             "- CRM calls are counted here when the CRM's AI goes through the CIS gateway (app crm); the CRM's own "
             "ledger is read through crm.ops.v1, not by this report.", ""]
    return _render(reg, report, first, last, generated_at, title, headline, kpis, body, UTC_NOTE,
                   tenants=tr, sources_unreadable=_unreadable(facts))


# ═══ BK-03 agency receivables aging ═════════════════════════════════════════════════════════════════════════════

# The catalog's aging buckets (BK-03 section 1): current, 1-30, 31-60, 61-90, > 90 days past due. A presentation of
# the age, not a decision level: red / amber come from each agency's own warn / suspend days.
AGING_BUCKETS = (("current", 0, 0), ("1-30", 1, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("> 90", 91, None))
BILLING_STATUS_SQL = billing.STATUS_SQL
AGENCIES_SQL = "SELECT tenant_id::text AS tenant_id, kind, account_status FROM shared.tenants"


async def billing_status(conn) -> dict:
    """{"rows", "read_at"} or {"error"} when the contract view is missing or not granted."""
    try:
        rows = await conn.fetch(BILLING_STATUS_SQL)
    except billing.VIEW_UNREADABLE as e:
        return {"error": f"contract view booking.v_tenant_billing_status not readable ({type(e).__name__})"}
    return {"rows": [dict(r) for r in rows], "read_at": datetime.now(UTC).isoformat()}


async def collect_bk03(conn, first: date, last: date) -> dict:
    return await src.run_parts({
        "billing": lambda: billing_status(conn),
        "agencies": lambda: _rows(conn, AGENCIES_SQL),
        "previous": lambda: previous_output(conn, "BK-03", first),
    })


def bucket(days: Optional[int]) -> str:
    d = int(days or 0)
    for name, lo, hi in AGING_BUCKETS:
        if d >= lo and (hi is None or d <= hi):
            return name
    return AGING_BUCKETS[0][0]


def _money_add(total: dict[str, Decimal], amounts: dict[str, str]) -> None:
    for code, amount in amounts.items():
        total[code] = total.get(code, Decimal(0)) + Decimal(amount)


def _money_text(amounts: dict) -> str:
    return ", ".join(f"{amount} {code}" for code, amount in sorted(amounts.items())) or "—"


def receivables(facts: dict) -> dict:
    """Per agency row and the roll-up (by status, by bucket, overdue per currency). Pure."""
    b = facts.get("billing")
    if b is None or failed(b):
        return {"error": failed(b) or "not read"}
    agencies = {a["tenant_id"]: a for a in facts["agencies"]} if isinstance(facts.get("agencies"), list) else {}
    rows, by_status, by_bucket, total = [], {}, {name: 0 for name, _, _ in AGING_BUCKETS}, {}
    for r in sorted(b["rows"], key=lambda x: (-(x.get("oldest_overdue_days") or 0), str(x["tenant_id"]))):
        tid = str(r["tenant_id"])
        amounts = billing.overdue_amounts(r.get("overdue_amount"))
        overdue = int(r.get("overdue_invoices") or 0)
        age = r.get("oldest_overdue_days") if overdue else None
        name = bucket(age)
        a = agencies.get(tid, {})
        rows.append({"tenant": short(tid), "tenant_id": tid, "kind": a.get("kind"),
                     "account_status": a.get("account_status"), "status": r["status"], "overdue_invoices": overdue,
                     "oldest_overdue_days": age, "bucket": name, "overdue_amount": amounts,
                     "paid_through": _day(r.get("paid_through")), "warn_after_days": r.get("warn_after_days"),
                     "suspend_after_days": r.get("suspend_after_days")})
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_bucket[name] += 1
        _money_add(total, amounts)
    return {"rows": rows, "by_status": by_status, "by_bucket": by_bucket,
            "overdue_invoices": sum(r["overdue_invoices"] for r in rows),
            "overdue_amount": {k: format(v, "f") for k, v in sorted(total.items())}, "read_at": b.get("read_at")}


def bk03_kpis(rc: dict) -> list[dict]:
    hidden = ("Booking keeps agency invoices, payments and supplier bills staff-only (no contract view for CIS)")
    tail = [_kpi_line("K-AR-DSO", None, "no_data", f"{NOT_PUBLISHED}: needs the invoiced amount; {hidden}"),
            _kpi_line("K-AR-UNVERIFIED", None, "no_data", f"{NOT_PUBLISHED}: {hidden}"),
            _kpi_line("K-AP-OVERDUE", None, "no_data", f"{NOT_PUBLISHED}: {hidden}")]
    if "error" in rc:
        why = f"not readable: {rc['error']}"
        return [_kpi_line("K-AR-OVERDUE", None, "no_data", why), _kpi_line("K-AR-AGING", None, "no_data", why)] + tail
    past = [r for r in rc["rows"] if r["status"] == "past_due"]
    warn = [r for r in rc["rows"] if r["status"] == "warn"]
    status = "red" if past else "amber" if warn else "ok"
    worst = max((r["oldest_overdue_days"] or 0 for r in rc["rows"]), default=0)
    by_bucket = ", ".join(f"{k} {v}" for k, v in rc["by_bucket"].items() if v) or "no agency row"
    return [
        _kpi_line("K-AR-OVERDUE", float(rc["overdue_invoices"]), status,
                  f"{_money_text(rc['overdue_amount'])}; {len(past)} agency(ies) past their suspend days, "
                  f"{len(warn)} past their warn days"),
        _kpi_line("K-AR-AGING", f"oldest {worst} days ({bucket(worst) if worst else 'current'})", status,
                  f"agencies by oldest overdue invoice: {by_bucket}"),
    ] + tail


def render_bk03(reg: Registry, report: Report, first: date, last: date, facts: dict,
                generated_at: datetime) -> Rendered:
    rc = receivables(facts)
    kpis = bk03_kpis(rc)
    title = f"Agency receivables aging, week {first.isoformat()} to {last.isoformat()}"
    if "error" in rc:
        headline = f"{title}: not readable ({rc['error']})"
        return _render(reg, report, first, last, generated_at, title, headline, kpis,
                       [f"- Not readable: {rc['error']}", ""], UTC_NOTE, receivables=rc,
                       sources_unreadable=_unreadable(facts))
    headline = (f"{title}: {len(rc['rows'])} agencies with billing terms, {rc['overdue_invoices']} overdue "
                f"invoice(s) ({_money_text(rc['overdue_amount'])}), "
                f"{rc['by_status'].get('past_due', 0)} past their suspend days")
    body = [f"Booking's billing status read at {rc['read_at']} (the view is a snapshot of now, not of the week).", "",
            "## Aging buckets (agencies by their oldest overdue invoice)", ""]
    body += table(["Bucket", "Agencies"], [[k, v] for k, v in rc["by_bucket"].items()])
    body += ["", "## Agencies", ""]
    if rc["rows"]:
        body += table(["Agency", "Kind", "Account", "Billing status", "Overdue invoices", "Oldest overdue",
                       "Overdue amount", "Paid through", "Warn / suspend days"],
                      [[r["tenant"], r["kind"] or "not in CIS", r["account_status"] or "—", r["status"],
                        r["overdue_invoices"], "—" if r["oldest_overdue_days"] is None else
                        f"{r['oldest_overdue_days']} days ({r['bucket']})", _money_text(r["overdue_amount"]),
                        r["paid_through"] or "—", f"{r['warn_after_days']} / {r['suspend_after_days']}"]
                       for r in rc["rows"]])
    else:
        body += ["- Booking has no agency with billing terms yet."]
    body += ["", "## Collection trend", ""]
    prev = facts.get("previous")
    prc = (prev or {}).get("receivables") if isinstance(prev, dict) else None
    if isinstance(prc, dict) and "rows" in prc:
        body += [f"- Previous BK-03 run ({(prev.get('period') or {}).get('end')}): {prc.get('overdue_invoices')} "
                 f"overdue invoice(s), {_money_text(prc.get('overdue_amount') or {})}; now "
                 f"{rc['overdue_invoices']}, {_money_text(rc['overdue_amount'])}.", ""]
    else:
        body += ["- No earlier BK-03 run to compare with.", ""]
    body += ["## Not published to CIS", "",
             "- Per-invoice balances and their buckets, payments recorded but not verified, deposits due before "
             "departure, supplier bills due and the cash gap (AP vs AR): Booking keeps agency invoices, payments and "
             "supplier bills staff-only; the Booking billing pages show them. A Booking contract view would add "
             "them here.", ""]
    return _render(reg, report, first, last, generated_at, title, headline, kpis, body, UTC_NOTE, receivables=rc,
                   sources_unreadable=_unreadable(facts))


# ═══ FI-07 weekly flash (counts only) ═══════════════════════════════════════════════════════════════════════════

FLASH_METRICS = ("trip_case_created", "proposal_sent", "confirmed", "cancelled")
FLASH_LABEL = {"trip_case_created": "Trip cases created", "proposal_sent": "Proposals sent",
               "confirmed": "Bookings confirmed", "cancelled": "Bookings cancelled"}
FUNNEL_SQL = """
    SELECT day, metric, coalesce(source_system, 'unknown') AS source_system, sum(n)::bigint AS n
      FROM booking.v_ops_funnel_daily
     WHERE day >= $1 AND day <= $2 AND metric = ANY($3::text[])
     GROUP BY 1, 2, 3
"""


async def funnel(conn, first: date, last: date) -> dict:
    try:
        rows = await conn.fetch(FUNNEL_SQL, first, last, list(FLASH_METRICS))
    except activity._CONTRACT_ERRORS as e:
        return {"error": f"contract view booking.v_ops_funnel_daily not readable ({type(e).__name__})"}
    return {"rows": [{"day": r["day"], "metric": r["metric"], "source_system": r["source_system"], "n": int(r["n"])}
                     for r in rows]}


async def case_status(conn) -> dict:
    try:
        rows = await conn.fetch(activity.BK_STATUS_SQL)
    except activity._CONTRACT_ERRORS as e:
        return {"error": f"contract view booking.v_ops_case_status not readable ({type(e).__name__})"}
    return {"rows": [{"status": r["status"], "n": int(r["n"])} for r in rows]}


async def collect_fi07(conn, first: date, last: date) -> dict:
    return await src.run_parts({
        "funnel": lambda: funnel(conn, first - timedelta(weeks=TREND_WEEKS), last),
        "cases": lambda: case_status(conn),
    })


def flash(facts: dict, first: date, last: date) -> dict:
    """{"weeks": [{start, end, <metric>: n}], "this": {...}, "by_source": {source: {metric: n}}} or {"error"}. Pure."""
    fn = facts.get("funnel")
    if fn is None or failed(fn):
        return {"error": failed(fn) or "not read"}
    weeks = []
    for i in range(TREND_WEEKS, -1, -1):
        a = first - timedelta(weeks=i)
        b = a + timedelta(days=6)
        w: dict[str, Any] = {"start": a.isoformat(), "end": b.isoformat(), **{m: 0 for m in FLASH_METRICS}}
        for r in fn["rows"]:
            if a <= r["day"] <= b:
                w[r["metric"]] += r["n"]
        weeks.append(w)
    by_source: dict[str, dict[str, int]] = {}
    for r in fn["rows"]:
        if first <= r["day"] <= last:
            s = by_source.setdefault(r["source_system"], {m: 0 for m in FLASH_METRICS})
            s[r["metric"]] += r["n"]
    return {"weeks": weeks, "this": weeks[-1], "previous": weeks[-2], "by_source": dict(sorted(by_source.items()))}


def cancel_rate(w: dict) -> Optional[float]:
    return ratio(w["cancelled"], w["confirmed"] + w["cancelled"])


def fi07_kpis(fl: dict) -> list[dict]:
    """In the registry order: K-BK-CONFIRMED, the four money KPIs (not published to CIS), K-BK-CANCEL."""
    hidden = f"{NOT_PUBLISHED}: Booking's finance views (case and agency profit) are staff-only"
    money = [_kpi_line(k, None, "no_data", hidden) for k in ("K-BK-GMV", "K-BK-MARGIN", "K-BK-CASH", "K-BK-INVOICED")]
    if "error" in fl:
        why = f"not readable: {fl['error']}"
        return [_kpi_line("K-BK-CONFIRMED", None, "no_data", why)] + money \
            + [_kpi_line("K-BK-CANCEL", None, "no_data", why)]
    t, p = fl["this"], fl["previous"]
    rate = cancel_rate(t)
    return [_kpi_line("K-BK-CONFIRMED", float(t["confirmed"]), "info", f"week before {p['confirmed']}")] + money + [
        _kpi_line("K-BK-CANCEL", rate, "no_data" if rate is None else "info",
                  "no booking confirmed or cancelled this week" if rate is None else
                  f"{t['cancelled']} cancelled / {t['confirmed'] + t['cancelled']} confirmed or cancelled; "
                  "trend on Dev")]


def render_fi07(reg: Registry, report: Report, first: date, last: date, facts: dict,
                generated_at: datetime) -> Rendered:
    fl = flash(facts, first, last)
    kpis = fi07_kpis(fl)
    title = f"Bookings weekly flash (counts) {first.isoformat()} to {last.isoformat()}"
    if "error" in fl:
        headline = f"{title}: not readable ({fl['error']})"
        body = [f"- Not readable: {fl['error']}", ""]
    else:
        t = fl["this"]
        headline = (f"{title}: {t['trip_case_created']} trip cases created, {t['proposal_sent']} proposals sent, "
                    f"{t['confirmed']} confirmed, {t['cancelled']} cancelled")
        body = ["## This week and the four before", ""]
        body += table(["Week"] + [FLASH_LABEL[m] for m in FLASH_METRICS] + ["Cancellation rate"],
                      [[f"{w['start']} to {w['end']}"] + [w[m] for m in FLASH_METRICS] + [pct(cancel_rate(w))]
                       for w in fl["weeks"]])
        body += ["", "## By source system (this week)", "",
                 "Source system crm = agency bookings through the Booking B2B API (the CRM).", ""]
        body += (table(["Source system"] + [FLASH_LABEL[m] for m in FLASH_METRICS],
                       [[s] + [v[m] for m in FLASH_METRICS] for s, v in fl["by_source"].items()])
                 if fl["by_source"] else ["- No trip case moved this week."])
        body += [""]
    cs = facts.get("cases")
    body += ["## Open pipeline now (trip cases by current status)", ""]
    if cs is None or failed(cs):
        body += [f"- Not readable: {failed(cs) or 'not read'}", ""]
    else:
        order = {s: i for i, s in enumerate(activity.BK_STATUS_ORDER)}
        rows = sorted(cs["rows"], key=lambda r: (order.get(r["status"], len(order)), r["status"]))
        body += table(["Status", "Trip cases"], [[r["status"], r["n"]] for r in rows]) if rows else \
            ["- No trip case."]
        body += [""]
    body += ["## Not published to CIS", "",
             "- GMV, average booking value, gross margin, cash (invoiced, collected, refunds, supplier payouts), FX "
             "exposure and the monthly close checklist: Booking's finance views are staff-only (the Booking finance "
             "pages show them). Only counts reach CIS, through ops.overview.v1.", ""]
    return _render(reg, report, first, last, generated_at, title, headline, kpis, body, UTC_NOTE, flash=fl,
                   sources_unreadable=_unreadable(facts))


# ── dispatch (generate.RENDERERS) ────────────────────────────────────────────────────────────────────────────────


async def render(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    if report.id == "FI-08":
        return render_fi08(reg, report, first, last, await collect_fi08(conn, first, last), now)
    if report.id == "BK-03":
        return render_bk03(reg, report, first, last, await collect_bk03(conn, first, last), now)
    if report.id == "FI-07":
        return render_fi07(reg, report, first, last, await collect_fi07(conn, first, last), now)
    raise LookupError(f"b2b_ops.render has no report {report.id}")
