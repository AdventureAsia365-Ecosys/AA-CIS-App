"""AA-1003 R3 — the money reports (no DB, no AWS, no paid call): weekly / monthly periods and the chain, the collector
math on fixture rows, each renderer's Markdown and JSON, and the isolation of an unreadable source.

Reports: EX-01, FI-02 (weekly); FI-01, FI-03, FI-04, FI-05, FI-06, OPS-08 (monthly). services/reports/money.py,
services/reports/money_sources.py.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.jobs import report_jobs, report_ops_jobs
from services.reports import generate, money, money_sources as src, periods, registry

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)
MON, SUN = date(2026, 10, 19), date(2026, 10, 25)


def _reg():
    return registry.load()


# ── periods and the chain ───────────────────────────────────────────────────────────────────────────────────────

def test_business_day_two_skips_the_weekend():
    assert periods.business_day(2026, 11) == date(2026, 11, 3)       # 1 Nov is a Sunday: Mon 2 = BD1, Tue 3 = BD2
    assert periods.business_day(2026, 12) == date(2026, 12, 2)
    assert periods.business_day(2026, 8) == date(2026, 8, 4)         # Sat 1, Sun 2, Mon 3, Tue 4


@pytest.mark.parametrize("schedule,day,period", [
    ("daily", date(2026, 10, 28), (date(2026, 10, 28), date(2026, 10, 28))),
    ("weekly", SUN, (MON, SUN)),
    ("weekly", date(2026, 10, 24), None),
    ("monthly", date(2026, 11, 2), (date(2026, 10, 1), date(2026, 10, 31))),   # next day = business day 2
    ("monthly", date(2026, 11, 3), None),
    ("quarterly", SUN, None),
])
def test_scheduled_period(schedule, day, period):
    assert periods.scheduled_period(schedule, day) == period


def test_default_period_is_the_last_closed_week_or_month():
    now = datetime(2026, 10, 30, 3, 0, tzinfo=UTC)                     # ICT 30/10: last closed day 29/10 (Thu)
    assert periods.default_period("weekly", now) == (MON, SUN)
    assert periods.default_period("monthly", now) == (date(2026, 9, 1), date(2026, 9, 30))
    sunday_evening = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)          # ICT Mon 26/10: Sunday 25 is closed
    assert periods.default_period("weekly", sunday_evening) == (MON, SUN)


def test_due_state_of_weekly_and_monthly_reports():
    reg = _reg()
    fi02, fi01 = reg.report("FI-02"), reg.report("FI-01")
    before_chain = datetime(2026, 10, 26, 0, 10, tzinfo=UTC)           # snapshot day still Sat 24
    assert generate.due_state(fi02, None, before_chain)["expected_period_end"] == "2026-10-18"
    after = datetime(2026, 10, 26, 1, 0, tzinfo=UTC)
    assert generate.due_state(fi02, None, after) == {"state": "due", "expected_period_end": "2026-10-25"}
    assert generate.due_state(fi02, None, after + timedelta(hours=3))["state"] == "overdue"
    assert generate.due_state(fi02, {"covered_to": SUN}, after)["state"] == "ok"
    assert generate.due_state(fi01, None, NOW) == {"state": "due", "expected_period_end": "2026-10-31"}
    mid_month = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    assert generate.due_state(fi01, {"covered_to": date(2026, 10, 31)}, mid_month)["state"] == "ok"


def test_chain_adds_weekly_on_sunday_and_monthly_before_business_day_two(monkeypatch):
    seen = []

    async def fake_enqueue(pool, report, first, last, **kw):
        seen.append((report.id, first, last))
        return {"run_id": "r"}
    monkeypatch.setattr(report_jobs, "enqueue_report_run", fake_enqueue)
    asyncio.run(report_jobs.chain_reports(MagicMock(), SUN, "job"))
    weekly = {s[0]: s[1:] for s in seen if s[1] != s[2]}
    assert weekly == {"EX-01": (MON, SUN), "FI-02": (MON, SUN)}
    seen.clear()
    asyncio.run(report_jobs.chain_reports(MagicMock(), date(2026, 11, 2), "job"))
    monthly = sorted(s[0] for s in seen if s[1] != s[2])
    assert monthly == ["FI-01", "FI-03", "FI-04", "FI-05", "FI-06", "OPS-08"]
    assert all(s[1:] == (date(2026, 10, 1), date(2026, 10, 31)) for s in seen if s[1] != s[2])


def test_fi02_is_emailed_to_the_digest_list():
    to, _ = report_ops_jobs.report_recipients("FI-02", {"OPS_DIGEST_EMAIL_TO": "lead@example.com"})
    assert to == ["lead@example.com"]
    assert report_ops_jobs.report_recipients("FI-01", {"OPS_DIGEST_EMAIL_TO": "lead@example.com"}) == ([], [])


# ── fixtures ────────────────────────────────────────────────────────────────────────────────────────────────────

def _llm(day, cost, provider="bedrock-converse", account="acc3", app="cis", stage="s1_generate", shadow=False,
         tenant=None, calls=10, fallback=0, model="m-sonnet", tokens=(1000, 500)):
    return {"day": day, "app": app, "tenant_id": tenant, "stage": stage, "role": "writer", "model": model,
            "provider": provider, "account": account, "is_shadow": shadow, "calls": calls, "fallback_calls": fallback,
            "tokens_in": tokens[0], "tokens_out": tokens[1], "cost_usd": cost}


def _forecast():
    return {"days_in_month": 31, "period_start": "2026-10-01",
            "aws_accounts": [
                {"account": "acc2", "label": "<account> (main)", "mtd_usd": 200.0, "forecast_usd": 260.0,
                 "budget_usd": 300.0, "status": "warn", "covered_to": "2026-10-23"},
                {"account": "acc3", "label": "<account> (LLM)", "mtd_usd": 100.0, "forecast_usd": 300.0,
                 "budget_usd": 250.0, "status": "over", "covered_to": "2026-10-24"}],
            "providers": [{"key": "bedrock", "label": "Amazon Bedrock", "mtd_usd": 90.0, "forecast_usd": 120.0,
                           "budget_usd": None, "status": "no_budget"},
                          {"key": "dfs", "label": "DataForSEO", "mtd_usd": 4.0, "forecast_usd": 6.0,
                           "budget_usd": 310.0, "status": "ok"}]}


def _week_facts():
    days = [MON + timedelta(days=i) for i in range(7)]
    prev = [d - timedelta(days=7) for d in days]
    llm = [_llm(d, 2.0) for d in days] + [_llm(d, 1.0) for d in prev]
    llm += [_llm(days[0], 3.0, stage="adhoc_aa1", shadow=True), _llm(days[1], 0.5, provider="typesafe", account=None,
                                                                    stage="jev_gate")]
    llm += [_llm(d - timedelta(days=7 * w), 1.0) for w in (2, 3, 4) for d in days]
    aws = [{"account": "acc2", "day": d, "amount_usd": 9.0, "fetched_at": NOW} for d in days[:5]]
    aws += [{"account": "acc2", "day": d, "amount_usd": 8.0, "fetched_at": NOW} for d in prev]
    aws += [{"account": "acc3", "day": d, "amount_usd": 2.0, "fetched_at": NOW} for d in days + prev]
    return {
        "forecast": _forecast(), "llm": llm, "aws": aws,
        "dfs": [{"day": days[2], "calls": 4, "live_calls": 4, "keywords": 40, "cost_usd": 7.0},
                {"day": prev[2], "calls": 1, "live_calls": 1, "keywords": 10, "cost_usd": 0.5}],
        "geo": [{"day": days[3], "provider": "geoapify", "requests": 1000, "cost_usd": 0.2}],
        "jev": [{"day": days[1], "decisions": 100, "uncached": 50, "cost_usd": 0.5}],
        "balances": {"dfs": {"balance_usd": 14.0, "fetched_at": "2026-10-25T00:00:00+00:00", "below_threshold": False},
                     "jev": {"estimated_left_usd": 20.0},
                     "credits": {"acc3": {"remaining_usd": 249.94, "expires_on": "2026-12-31"}}},
        "alerts": [{"provider": "dfs", "message": "dfs spend today $8.00 is 80% of the $10.00 daily budget.",
                    "hard_stop": "true", "created_at": NOW, "stopped": False},
                   {"provider": "bedrock", "message": "place_extract stopped by its per-run budget",
                    "hard_stop": "true", "created_at": NOW, "stopped": True}],
        "window": {"first": MON.isoformat(), "last": SUN.isoformat(), "days": "UTC"},
    }


# ── weekly math ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("this,prev,flag", [(10, 2, True), (10, 6, False), (20, 0, True), (3, 0, False),
                                            (None, 2, False), (40, 30, True), (36, 30, False)])
def test_week_over_week_flag_needs_both_the_ratio_and_the_floor(this, prev, flag):
    assert money.wow_flag(this, prev) is flag


def test_mtd_is_prorated_to_the_days_cost_explorer_covers():
    m = money.mtd_vs_budget(_forecast())
    # 300 spent / (300 x 23/31 + 250 x 24/31)
    assert m["value"] == pytest.approx(300 / (300 * 23 / 31 + 250 * 24 / 31), abs=1e-4)
    assert m["status"] == "ok" and m["accounts_missing"] == []
    assert money.mtd_vs_budget({"error": "boom"})["value"] is None


def test_provider_spend_and_the_week_total_count_the_llm_account_once():
    w = money.week_compute(_week_facts(), MON, SUN)
    lines = {x["key"]: x for x in w["lines"]}
    assert lines["aws_acc2"]["usd"] == 45.0 and lines["aws_acc2"]["days_present"] == 5
    assert lines["aws_acc2"]["prev_usd"] == 56.0
    assert lines["aws_acc3"]["usd"] == 14.0 and lines["aws_acc3"]["days_present"] == 7
    assert lines["bedrock_acc3"]["usd"] == 17.0 and lines["bedrock_acc3"]["prev_usd"] == 7.0
    assert lines["bedrock_acc3"]["flag"] is True                   # +10 USD, +143 %
    assert lines["jev"]["usd"] == 0.5 and lines["dfs"]["usd"] == 7.0 and lines["dfs"]["flag"] is True
    # acc3 counted from its full-week bill, not again from the gateway estimate
    assert w["total_usd"] == pytest.approx(45.0 + 14.0 + 0.5 + 7.0 + 0.2)
    assert w["top_stages"][0] == {"stage": "cis/s1_generate", "calls": 70, "cost_usd": 14.0}
    assert w["alerts"]["bells"] == 2 and w["alerts"]["stops"] == 1


def test_runways_and_the_credit_outlook():
    b = money.balances_view(_week_facts(), SUN)
    assert b["dfs"]["per_day_usd"] == pytest.approx(7.5 / 14, abs=1e-6)    # 7.0 + 0.5 over 14 days
    assert b["dfs"]["runway_days"] == pytest.approx(14.0 / (7.5 / 14), abs=0.1)
    assert b["jev"]["runway_days"] == pytest.approx(20.0 / (0.5 / 14), abs=0.1)
    c = b["acc3_credit"]
    assert c["per_day_usd"] == 2.0 and c["expires_on"] == "2026-12-31"
    assert c["left_at_expiry_usd"] == pytest.approx(249.94 - 2.0 * 66, abs=0.01)   # 26/10 -> 31/12
    assert "expire unused" in c["flag"]
    short = money.credit_outlook(100.0, 2.0, date(2026, 12, 31), date(2026, 10, 26))
    assert short["runway_days"] == 50.0 and "runs out" in short["flag"] and short["left_at_expiry_usd"] == 0.0
    assert money.credit_outlook(None, 2.0, None, date(2026, 10, 26))["flag"] is None
    assert money.runway(10.0, 0.0) is None and money.runway(None, 1.0) is None


def test_stop_bells_are_told_apart_from_threshold_bells():
    assert src.is_stop_message("place_extract stopped by its per-run budget")
    assert not src.is_stop_message("dfs spend today $8.00 is 80% of the $10.00 daily budget.")
    assert not src.is_stop_message(None)


def test_llm_week_flags_against_the_four_week_average():
    lw = money.llm_week(_week_facts()["llm"], MON, SUN)
    assert lw["usd"] == 17.5 and lw["shadow_usd"] == 3.0
    assert lw["prior_weeks_usd"] == [7.0, 7.0, 7.0, 7.0] and lw["flag"] is True


# ── weekly renderers ────────────────────────────────────────────────────────────────────────────────────────────

def test_fi02_renders_every_section_and_its_kpis():
    reg = _reg()
    out = money.render_fi02(reg, reg.report("FI-02"), MON, SUN, _week_facts(), NOW)
    md = out.markdown
    for heading in ("## Needs a look", "## KPIs", "## 1. Month to date", "## 2. This week vs the week before",
                    "## 3. Top LLM stages", "## 4. Balances and runway", "## 5. Budget bells", "## 6. More"):
        assert heading in md, heading
    kp = {k["id"]: k for k in out.output["kpis"]}
    assert set(kp) == set(reg.report("FI-02").kpis)
    assert kp["K-COST-FCST"]["status"] == "red" and kp["K-COST-FCST"]["value"] == 1.2      # acc3 300 / 250
    assert kp["K-RUNWAY-ACC3"]["status"] == "amber"
    assert out.output["headline"].startswith("Weekly spend 2026-10-19 to 2026-10-25")
    assert any("forecast over budget" in a for a in out.output["attention"])
    assert "<account>" not in md or "(main)" in md          # labels come from the forecast rows, never raw IDs


def test_fi02_with_unreadable_sources_still_renders():
    reg = _reg()
    facts = {k: {"error": "UndefinedTableError: x"} for k in ("forecast", "llm", "dfs", "geo", "jev", "aws",
                                                             "balances", "alerts")}
    out = money.render_fi02(reg, reg.report("FI-02"), MON, SUN, facts, NOW)
    assert "Balances not readable" in out.markdown and "## 5. Budget bells" in out.markdown
    assert set(out.output["sources_unreadable"]) == set(facts)


def test_ex01_is_a_draft_with_generated_numbers_and_narrative_slots():
    reg = _reg()
    facts = _week_facts()
    facts["tours_live"] = [{"kpi_id": "K-CAT-LIVE", "day": SUN, "scope": "all", "value": 860},
                           {"kpi_id": "K-CAT-LIVE", "day": MON - timedelta(days=1), "scope": "all", "value": 892}]
    facts["demand"] = {"tripplanner": {"trip_started": 12, "inquiry_sent": 3},
                       "booking": {"error": "InsufficientPrivilegeError: x"}}
    facts["demand_prev"] = {"tripplanner": {"trip_started": 10}, "booking": {"inquiry_received": 1}}
    facts["digests"] = [{"day": MON, "attention": [], "apps": []},
                        {"day": SUN, "attention": ["LLM route unknown"], "apps": []}]
    out = money.render_ex01(reg, reg.report("EX-01"), MON, SUN, facts, NOW)
    kp = {k["id"]: k for k in out.output["kpis"]}
    assert set(kp) == set(reg.report("EX-01").kpis)
    assert kp["K-CAT-LIVE"]["status"] == "amber"                 # 892 -> 860 = -3.6 %
    assert kp["K-DEL-SHIPPED"]["status"] == money.NOT_WIRED and kp["K-REL-SEV"]["status"] == money.NOT_WIRED
    assert kp["K-LLM-COST-WEEK"]["value"] == 17.5
    assert out.output["draft"] is True and out.output["platform"]["green_days"] == 1
    md = out.markdown
    assert md.count("NARRATIVE") >= 5 and "booking contract view not readable" in md
    assert "## 3. Scorecard" in md and "## 7. Incidents and risks" in md


# ── monthly math ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cfrom,cto,fetched,status", [
    (None, None, None, "missing"),
    ("2026-10-05", "2026-10-31", "2026-11-04T00:00:00+00:00", "partial"),
    ("2026-10-01", "2026-10-25", "2026-11-04T00:00:00+00:00", "partial"),
    ("2026-10-01", "2026-10-30", "2026-11-01T06:00:00+00:00", "provisional"),
    ("2026-10-01", "2026-10-31", "2026-11-03T23:59:59+00:00", "provisional"),
    ("2026-10-01", "2026-10-31", "2026-11-04T00:00:01+00:00", "final"),
])
def test_aws_status(cfrom, cto, fetched, status):
    assert money.aws_status(cfrom, cto, fetched, date(2026, 10, 1), date(2026, 10, 31)) == status


def _cba(aws_total, apps, untagged=0.0, sat=(10.0, 9.0), covered=("2026-10-01", "2026-10-31"),
         fetched="2026-11-04T00:00:00+00:00"):
    rows = [{"app": a, "kind": "app", "aws_usd": v, "total_usd": v + 1, "ai_outside_aws_usd": 1.0}
            for a, v in apps.items() if a != "shared"]
    if "shared" in apps:
        rows.append({"app": "tag:shared", "kind": "shared_tag", "aws_usd": apps["shared"], "total_usd": apps["shared"],
                     "ai_outside_aws_usd": 0.0})
    rows.append({"app": "untagged", "kind": "untagged", "aws_usd": untagged, "total_usd": untagged,
                 "ai_outside_aws_usd": 0.0})
    return {"aws": {"total_usd": aws_total, "untagged_usd": untagged, "covered_from": covered[0],
                    "covered_to": covered[1], "fetched_at": fetched},
            "apps": rows, "llm_satellites": {"acc3": {"ce_bedrock_usd": sat[0], "llm_log_estimate_usd": sat[1]}}}


def _month_facts():
    oct1, sep1, aug1 = date(2026, 10, 1), date(2026, 9, 1), date(2026, 8, 1)
    llm = [_llm(oct1 + timedelta(days=i), 1.0) for i in range(30)]                       # 31 Oct: no call
    llm += [_llm(oct1, 2.0, shadow=True, stage="s1_shadow"), _llm(oct1, 0.5, stage="adhoc_aa1003"),
            _llm(oct1, 4.0, tenant="11111111-2222-3333-4444-555555555555", stage="t2_write", fallback=1),
            _llm(oct1, 0.25, provider="typesafe", account=None, stage="jev_gate", calls=100)]
    llm += [_llm(sep1 + timedelta(days=i), 0.5) for i in range(30)] + [_llm(aug1, 3.0)]
    svc_this = {"accounts": [{"account_key": "acc2", "total_usd": 280.0}, {"account_key": "acc3", "total_usd": 12.0}],
                "services": [{"account_key": "acc2", "service": "Amazon Relational Database Service", "amount_usd": 60.0},
                             {"account_key": "acc2", "service": "Amazon Elastic Container Service", "amount_usd": 90.0},
                             {"account_key": "acc2", "service": "AWS WAF", "amount_usd": 3.0},
                             {"account_key": "acc3", "service": "Amazon Bedrock", "amount_usd": 12.0}],
                "covered_from": "2026-10-01", "covered_to": "2026-10-31", "fetched_at": "2026-11-04T00:00:00+00:00"}
    svc_prev = {"accounts": [{"account_key": "acc2", "total_usd": 250.0}],
                "services": [{"account_key": "acc2", "service": "Amazon Relational Database Service", "amount_usd": 61.0},
                             {"account_key": "acc2", "service": "Amazon Elastic Container Service", "amount_usd": 60.0},
                             {"account_key": "acc2", "service": "AWS WAF", "amount_usd": 2.0}],
                "covered_from": "2026-09-01", "covered_to": "2026-09-30", "fetched_at": "2026-10-05T00:00:00+00:00"}
    fe = _forecast()
    fe["aws_accounts"][0].update(mtd_usd=280.0, forecast_usd=280.0)
    fe["aws_accounts"][1].update(mtd_usd=200.0, forecast_usd=200.0)
    fm = _forecast()
    fm["aws_accounts"][0].update(forecast_usd=308.0)
    fm["aws_accounts"][1].update(forecast_usd=150.0)
    return {
        "months": [{"key": "2026-08", "first": "2026-08-01", "last": "2026-08-31"},
                   {"key": "2026-09", "first": "2026-09-01", "last": "2026-09-30"},
                   {"key": "2026-10", "first": "2026-10-01", "last": "2026-10-31"}],
        "window": {"first": "2026-10-01", "last": "2026-10-31", "days": "UTC"},
        "cost_by_app:2026-08": {"error": "TimeoutError: x"},
        "cost_by_app:2026-09": _cba(250.0, {"cis": 80.0, "shared": 100.0, "booking": 10.0}, untagged=20.0),
        "cost_by_app:2026-10": _cba(280.0, {"cis": 95.0, "shared": 100.0, "booking": 10.0}, untagged=28.0),
        "services": svc_this, "services_prev": svc_prev, "llm": llm,
        "dfs": [{"day": oct1, "calls": 5, "live_calls": 5, "keywords": 100, "cost_usd": 3.0}],
        "geo": [{"day": oct1, "provider": "geoapify", "requests": 2000, "cost_usd": 0.4}],
        "jev": [{"day": oct1, "decisions": 4000, "uncached": 1000, "cost_usd": 1.5},
                {"day": sep1, "decisions": 10, "uncached": 10, "cost_usd": 0.01}],
        "balances": {"dfs": {"balance_usd": 30.0, "fetched_at": "x"}, "jev": {"estimated_left_usd": 5.0},
                     "credits": {}},
        "published:2026-08": 0, "published:2026-09": 40, "published:2026-10": 50,
        "demand:2026-08": {"tripplanner": {}, "booking": {}},
        "demand:2026-09": {"tripplanner": {"trip_started": 4}, "booking": {"confirmed": 0}},
        "demand:2026-10": {"tripplanner": {"trip_started": 10}, "booking": {"error": "InsufficientPrivilegeError"}},
        "forecast_mid": fm, "forecast_end": fe,
        "ytd": {"accounts": [{"account_key": "acc2", "total_usd": 1000.0}, {"account_key": "acc3", "total_usd": 300.0},
                             {"account_key": "retired", "total_usd": 50.0}], "covered_from": "2026-06-01"},
        "spend_budgets": [{"provider": "dfs", "scope": "global", "per_day_usd": 10.0, "per_run_usd": None,
                           "hard_stop": True, "alert_pct": 80}],
        "alerts": [],
    }


def test_by_app_against_the_per_app_budgets():
    ab = money.by_app(_month_facts(), {"cis": 90.0, "booking": 25.0, "shared": 120.0})
    rows = {r["app"]: r for r in ab["rows"]}
    assert rows["cis"]["ratio"] == pytest.approx(95 / 90, abs=1e-4) and rows["cis"]["status"] == "red"
    assert rows["shared"]["status"] == "amber" and rows["booking"]["status"] == "ok"
    assert rows["cis"]["prev_usd"] == 80.0 and rows["cis"]["avg_3m_usd"] == 87.5    # August unreadable: 2 months
    assert ab["untagged_ratio"] == pytest.approx(28 / 280, abs=1e-4)
    assert ab["worst"]["app"] == "cis" and ab["aws_status"] == "final"
    assert ab["acc2_history_usd"] == [None, 250.0, 280.0]


def test_movers_need_twenty_percent_and_two_dollars():
    mv = money.movers(_month_facts())
    moved = {m["service"] for m in mv["movers"]}
    assert moved == {"Amazon Elastic Container Service", "Amazon Bedrock"}     # WAF +1 USD is not a mover
    assert mv["top"][0]["service"] == "Amazon Elastic Container Service"


def test_llm_month_attribution():
    lm = money.llm_month(_month_facts())
    assert lm["cost_usd"] == pytest.approx(30 + 2 + 0.5 + 4 + 0.25)
    assert lm["shadow_ratio"] == pytest.approx(2 / 36.75, abs=1e-4)
    assert lm["adhoc"] == {"adhoc_aa1003": 0.5}
    assert lm["zero_call_days"] == ["2026-10-31"]
    assert lm["tenants"] == [{"tenant": "11111111", "cost_usd": 4.0}]
    assert lm["platform_usd"] == pytest.approx(32.75)
    assert lm["recon"]["ratio"] == pytest.approx(0.1)
    assert lm["by_app_trend"]["2026-09"] == {"cis": 15.0} and lm["by_app_trend"]["2026-08"] == {"cis": 3.0}
    model = next(m for m in lm["models"] if m["source"] == "bedrock_acc3")
    assert model["usd_per_1k_calls"] == pytest.approx(36.5 / 330 * 1000, abs=1e-3)   # 30 x 10 + 3 x 10 calls
    k = {x["id"]: x for x in money.llm_kpis(lm)}
    assert k["K-LLM-RECON"]["status"] == "ok" and k["K-LLM-SHADOW"]["status"] == "ok"


def test_units_per_month():
    u = money.units(_month_facts())
    oct_, sep = u["months"][-1], u["months"][-2]
    assert oct_["tour"] == pytest.approx(96 / 50, abs=1e-4)          # cis total (aws 95 + AI 1) / 50 published
    assert sep["tour"] == pytest.approx(81 / 40, abs=1e-4)
    assert oct_["trip"] is None                                      # no tripplanner cost row in the fixture
    assert oct_["booking"] is None and oct_["confirmed"] is None    # booking view not readable
    assert oct_["jev_per_1k"] == 1.5 and oct_["dfs_per_keyword"] == 0.03 and oct_["geo_per_1k"] == 0.2
    assert u["months"][0]["tour"] is None                            # August: cost unreadable, 0 published
    k = {x["id"]: x for x in money.unit_kpis(u)}
    assert k["K-UNIT-AGENCY"]["status"] == money.NOT_WIRED and k["K-UNIT-BOOKING"]["note"].startswith("n/a")


def test_budgets_variance_forecast_accuracy_and_ytd():
    facts = _month_facts()
    ab = money.by_app(facts, {"cis": 90.0})
    b = money.budgets(facts, {"acc2": 300.0, "acc3": 250.0}, ab)
    lines = {x["line"]: x for x in b["lines"]}
    assert lines["AWS acc2"]["variance"] == pytest.approx(-20 / 300, abs=1e-4)
    assert lines["AWS acc2"]["forecast_accuracy"] == pytest.approx(0.1)          # |308 - 280| / 280
    assert lines["AWS acc3"]["forecast_accuracy"] == pytest.approx(0.25)
    assert lines["App cis"]["variance"] == pytest.approx(5 / 90, abs=1e-4)
    assert b["forecast_accuracy"] == pytest.approx(0.25)
    # June..October stored: 5 months x (300 + 250); the retired account is not part of the plan
    assert b["ytd"] == {"usd": 1300.0, "plan_usd": 2750.0, "ratio": pytest.approx(1300 / 2750, abs=1e-4),
                        "covered_from": "2026-06-01", "months": 5}
    k = {x["id"]: x for x in money.budget_kpis(b)}
    assert k["K-FIN-FCSTACC"]["status"] == "red" and k["K-FIN-HARDSTOP"]["value"] == 0


def test_capacity_from_rds_metrics_db_and_digests():
    t0 = datetime(2026, 10, 2, tzinfo=UTC)
    hours = [t0 + timedelta(hours=i) for i in range(30 * 24)]
    free = [(h, 50e9 - i * (0.5e9 / 24)) for i, h in enumerate(hours)]           # shrinks 0.5 GB a day
    facts = {
        "rds": {"cpu": [(h, 30.0 if i < 5 else 5.0) for i, h in enumerate(hours)],
                "connections": [(h, float(i % 40)) for i, h in enumerate(hours)], "free_storage": free},
        "db": {"database_bytes": 1_500_000_000, "top_relations": [{"schema": "shared", "name": "decision_log",
                                                                   "bytes": 443_700_000}],
               "connections": 30, "max_connections": 181, "role_limits_total": 150},
        "job_kpis": [{"kpi_id": "K-JOB-FAIL", "value": 2}, {"kpi_id": "K-JOB-FAIL", "value": 1},
                     {"kpi_id": "K-JOB-QUEUEAGE", "value": 12.5}],
        "digests": [{"day": date(2026, 10, d), "attention": [],
                     "apps": [{"id": "cis", "status": "ok"}, {"id": "booking", "status": "ok" if d % 2 else "down"},
                              {"id": "tripplanner", "status": "no_probe"}]} for d in range(1, 11)],
        "services": {"services": [{"service": "Amazon Relational Database Service", "amount_usd": 60.0}]},
    }
    c = money.capacity(facts)
    assert c["cpu_hours_above"] == 5 and c["conn_max"] == 39.0
    assert c["storage_gb_per_day_30d"] == pytest.approx(0.5, abs=0.01)
    assert c["conn_p95_ratio"] == pytest.approx(37 / 181, abs=0.01)
    assert c["availability"]["booking"] == {"ok_days": 5, "probed_days": 10, "ratio": 0.5}
    assert "tripplanner" not in c["availability"] and c["jobs"]["failed"] == 3 and c["rds_cost_usd"] == 60.0
    k = {x["id"]: x for x in money.capacity_kpis(c)}
    assert k["K-AVAIL"]["value"] == 0.5 and k["K-CAP-TRIGGERS"]["status"] == money.NOT_WIRED
    reg = _reg()
    out = money.render_ops08(reg, reg.report("OPS-08"), date(2026, 10, 1), date(2026, 10, 31),
                             {**facts, "months": _month_facts()["months"]}, NOW)
    assert "## Database" in out.markdown and "shared.decision_log" in out.markdown
    gone = money.capacity({"rds": {"unavailable": "not_permitted: AccessDenied"}})
    assert {x["id"]: x for x in money.capacity_kpis(gone)}["K-CAP-CPU"]["status"] == "no_data"


# ── monthly renderers ───────────────────────────────────────────────────────────────────────────────────────────

def test_fi01_follows_the_cost_template(monkeypatch):
    from api.services import cost_ops

    monkeypatch.delenv(cost_ops.APP_BUDGET_ENV, raising=False)
    monkeypatch.delenv(cost_ops.AWS_BUDGET_ENV, raising=False)
    reg = _reg()
    out = money.render_fi01(reg, reg.report("FI-01"), date(2026, 10, 1), date(2026, 10, 31), _month_facts(), NOW)
    md = out.markdown
    for h in ("## Summary for the business owner", "## A1. KPIs", "## A2. Totals by account", "## A3. By App tag",
              "## A4. Top services and movers", "## A5. LLM spend (FI-03)", "## A6. Vendors and credits (FI-04)",
              "## A7. Unit costs (FI-05)", "## A8. Budgets, alerts and hard stops (FI-06)", "## Decisions needed",
              "## Sources"):
        assert h in md, h
    kp = {k["id"]: k for k in out.output["kpis"]}
    assert set(kp) == set(reg.report("FI-01").kpis) | {"K-UNIT-TOUR"}
    assert kp["K-COST-ACC2"]["value"] == pytest.approx(280 / 300, abs=1e-4)
    assert kp["K-COST-EXPLAINED"]["status"] == "to_write"
    assert out.output["aws_status"] == "final" and "AWS data: **final**" in md
    assert "cost_by_app:2026-08" in out.output["sources_unreadable"]
    assert out.output["headline"].startswith("Cost review 2026-10")


def test_fi01_names_a_provisional_month_and_how_to_refresh(monkeypatch):
    reg = _reg()
    facts = _month_facts()
    facts["cost_by_app:2026-10"]["aws"]["fetched_at"] = "2026-11-01T02:00:00+00:00"
    out = money.render_fi01(reg, reg.report("FI-01"), date(2026, 10, 1), date(2026, 10, 31), facts, NOW)
    assert out.output["aws_status"] == "provisional"
    assert "/admin/platform/cost?month=2026-10" in out.markdown


@pytest.mark.parametrize("rid,fn,heading", [("FI-03", money.render_fi03, "## By app and stage"),
                                            ("FI-04", money.render_fi04, "## Contracts and DPAs"),
                                            ("FI-05", money.render_fi05, "## Unit cost table"),
                                            ("FI-06", money.render_fi06, "## Budget table")])
def test_section_reports_render_their_kpis(rid, fn, heading):
    reg = _reg()
    out = fn(reg, reg.report(rid), date(2026, 10, 1), date(2026, 10, 31), _month_facts(), NOW)
    assert heading in out.markdown and "## KPIs" in out.markdown
    ids = {k["id"] for k in out.output["kpis"]}
    assert set(reg.report(rid).kpis) <= ids
    assert out.output["report_id"] == rid and out.summary


# ── loaders and dispatch ────────────────────────────────────────────────────────────────────────────────────────

def test_dispatch_collects_the_parts_each_report_needs(monkeypatch):
    seen = {}

    async def fake_month(conn, first, last, now, needs):
        seen["month"] = (first, last, tuple(needs))
        return _month_facts()

    async def fake_week(conn, first, last, with_business=False):
        seen["week"] = (first, last, with_business)
        return _week_facts()
    monkeypatch.setattr(src, "collect_month", fake_month)
    monkeypatch.setattr(src, "collect_week", fake_week)
    reg = _reg()
    out = asyncio.run(generate.render(None, reg, reg.report("FI-05"), date(2026, 10, 1), date(2026, 10, 31), NOW))
    assert seen["month"] == (date(2026, 10, 1), date(2026, 10, 31), money.MONTH_NEEDS["FI-05"])
    assert out.output["report_id"] == "FI-05"
    asyncio.run(generate.render(None, reg, reg.report("FI-01"), date(2026, 10, 1), date(2026, 10, 17), NOW))
    assert seen["month"][:2] == (date(2026, 10, 1), date(2026, 10, 17))      # a manual month-to-date run
    asyncio.run(generate.render(None, reg, reg.report("EX-01"), MON, SUN, NOW))
    assert seen["week"] == (MON, SUN, True)
    asyncio.run(generate.render(None, reg, reg.report("FI-02"), MON, SUN, NOW))
    assert seen["week"] == (MON, SUN, False)


def test_run_parts_isolates_a_failing_source():
    async def ok():
        return [1]

    async def boom():
        raise RuntimeError("relation does not exist")
    out = asyncio.run(src.run_parts({"a": ok, "b": boom, "c": ok}))
    assert out["a"] == [1] and out["c"] == [1] and out["b"] == {"error": "RuntimeError: relation does not exist"}


def test_collect_month_stops_the_current_month_at_its_last_day(monkeypatch):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    facts = asyncio.run(src.collect_month(conn, date(2026, 10, 1), date(2026, 10, 17), NOW, ["llm"]))
    assert facts["months"][-1] == {"key": "2026-10", "first": "2026-10-01", "last": "2026-10-17"}
    assert facts["months"][0]["first"] == "2026-08-01" and facts["llm"] == []
    llm_call = next(c for c in conn.fetch.await_args_list if c.args[0] == src.LLM_LEDGER_SQL)
    assert llm_call.args[1:] == (date(2026, 8, 1), date(2026, 10, 18))


def test_collect_month_rejects_unknown_parts():
    with pytest.raises(ValueError):
        asyncio.run(src.collect_month(None, date(2026, 10, 1), date(2026, 10, 31), NOW, ["nope"]))


def test_collect_week_reads_utc_windows(monkeypatch):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(src, "forecast", AsyncMock(return_value={"ok": 1}))
    monkeypatch.setattr(src, "balances", AsyncMock(return_value={}))
    facts = asyncio.run(src.collect_week(conn, MON, SUN))
    assert facts["forecast"] == {"ok": 1} and facts["llm"] == [] and facts["alerts"] == []
    assert src.forecast.await_args.args[1:] == (date(2026, 10, 1), src.utc_end_of(SUN))
    llm_call = next(c for c in conn.fetch.await_args_list if c.args[0] == src.LLM_LEDGER_SQL)
    assert llm_call.args[1:] == (MON - timedelta(days=28), SUN + timedelta(days=1))
    dfs_call = next(c for c in conn.fetch.await_args_list if c.args[0] == src.DFS_DAILY_SQL)
    assert dfs_call.args[2] == datetime(2026, 10, 26, tzinfo=UTC)


def test_ce_daily_keeps_account_keys_never_ids(monkeypatch):
    from shared.aws_client import cost_explorer as ce

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"account_id": "<account>", "day": MON, "amount_usd": 1.0,
                                          "fetched_at": NOW}])
    monkeypatch.setattr(ce, "account_key", lambda a: "acc2")
    rows = asyncio.run(src.ce_daily(conn, MON, SUN))
    assert rows == [{"day": MON, "amount_usd": 1.0, "fetched_at": NOW, "account": "acc2"}]


def test_conn_pool_runs_the_pool_api_on_one_connection():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=["row"])
    pool = src.ConnPool(conn)

    async def go():
        async with pool.acquire() as c:
            assert c is conn
        return await pool.fetch("SELECT 1")
    assert asyncio.run(go()) == ["row"]


def test_money_sql_names_only_reference_columns():
    """Every table the money readers name is in the live schema reference (column names: see the reference)."""
    from pathlib import Path

    ref = Path(__file__).resolve().parents[2].parent.parent / "docs" / "architecture" / "db-schema-reference.md"
    if not ref.exists():
        pytest.skip("root repo not checked out next to this one")
    text = ref.read_text(encoding="utf-8")
    for table_name in ("shared.llm_call_daily", "shared.llm_call_log", "shared.dfs_call_log", "geo.geocode_call",
                       "shared.decision_log", "shared.cost_explorer_snapshot", "shared.notifications",
                       "shared.spend_budget", "gold_aa_internal.published_tours", "tripplanner.v_ops_traffic_daily",
                       "booking.v_ops_funnel_daily", "shared.report_run"):
        assert f"### `{table_name}`" in text, table_name


def test_cost_ops_budgets_and_credits_from_env(monkeypatch):
    from api.services import cost_ops

    monkeypatch.setenv(cost_ops.APP_BUDGET_ENV, "cis=100, bad")
    monkeypatch.setenv(cost_ops.CREDITS_ENV, "acc3=249.94@2026-12-31,x=oops")
    assert cost_ops.app_budgets()["cis"] == 100.0 and cost_ops.app_budgets()["shared"] == 120.0
    assert cost_ops.credits() == {"acc3": {"remaining_usd": 249.94, "expires_on": date(2026, 12, 31)}}
    monkeypatch.delenv(cost_ops.CREDITS_ENV)
    assert cost_ops.credits() == {}
