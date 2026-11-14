"""AA-1003 R5 — B2B reports (services/reports/b2b_ops.py): FI-08 tenant entitlements and AI spend against the cap,
BK-03 agency receivables aging, FI-07 weekly bookings flash (counts only). No DB, no AWS, no paid call.

Each renderer on fixture facts lists every KPI its registry entry names, a part that could not be read is a line in
the report (never an exception), the Booking sources without a contract view are named "not published to CIS", the
colours come from each tenant's / agency's own thresholds (no number of the report's own), and tenants are shown by
id prefix, never by name.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from services.reports import b2b_ops, generate, registry

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
MON, SUN = date(2026, 11, 9), date(2026, 11, 15)
OCT1, OCT31 = date(2026, 10, 1), date(2026, 10, 31)
R5 = ("FI-08", "BK-03", "FI-07")

T_A = "aaaaaaaa-1111-4111-8111-000000000001"      # agency on a plan with a budget, past its alert %
T_B = "bbbbbbbb-2222-4222-8222-000000000002"      # demo tenant on trial, past its approval % (hard stop)
T_C = "cccccccc-3333-4333-8333-000000000003"      # agency with spend and no budget at all
T_D = "dddddddd-4444-4444-8444-000000000004"      # canceled tenant, no spend: out of scope


def _reg():
    return registry.load()


def _kpis(out) -> dict:
    return {k["id"]: k for k in out.output["kpis"]}


def _assert_lists_registry_kpis(out, rid: str):
    assert [k["id"] for k in out.output["kpis"]] == list(_reg().report(rid).kpis), rid
    assert out.output["report_id"] == rid and out.markdown.startswith("# ")
    json.dumps(out.output, default=str)       # stored as JSON by store.finish_ok


# ── registry and dispatch ───────────────────────────────────────────────────────────────────────────────────────

def test_r5_reports_are_generated_here_with_their_schedules():
    reg = _reg()
    for rid in R5:
        r = reg.report(rid)
        assert r.runnable and rid in generate.RENDERERS, rid
        assert r.automated_from == date(2026, 11, 16), rid
        assert r.status == "partial", rid
    assert reg.report("FI-08").schedule == "monthly"
    assert {reg.report(r).schedule for r in ("BK-03", "FI-07")} == {"weekly"}
    # the week chained on Sunday 15/11 predates the generator: expected nothing yet, never overdue
    assert generate.due_state(reg.report("BK-03"), None, datetime(2026, 11, 16, 3, tzinfo=UTC))["state"] \
        == "pending_first"


def test_r5_checklist_reports_stay_out_of_the_engine():
    reg = _reg()
    for rid in ("OPS-17", "SEC-10", "SEC-09", "DEV-04", "OPS-03", "BK-01", "EX-02"):
        assert not reg.report(rid).runnable and rid not in generate.RENDERERS, rid


@pytest.mark.parametrize("rid", R5)
def test_generate_dispatches_to_b2b_ops(monkeypatch, rid):
    seen = []

    async def fake(conn, reg, report, first, last, now):
        seen.append(report.id)
        return generate.Rendered("s", {}, "m")
    monkeypatch.setattr(b2b_ops, "render", fake)
    reg = _reg()
    asyncio.run(generate.render(None, reg, reg.report(rid), MON, SUN, NOW))
    assert seen == [rid]


def test_b2b_ops_refuses_a_report_it_does_not_own():
    with pytest.raises(LookupError):
        asyncio.run(b2b_ops.render(None, _reg(), _reg().report("CIS-01"), MON, SUN, NOW))


# ── FI-08 tenant entitlements and AI spend against the cap ─────────────────────────────────────────────────────

def _cap(monthly, alert=80, approval=100, stop=True, source="plan"):
    return {"source": source, "monthly_usd": monthly, "alert_pct": alert, "approval_pct": approval,
            "hard_stop": stop, "used_pct": None, "state": "ok"}


def _fi08_facts(**over) -> dict:
    facts = {
        "tenants": [{"tenant_id": T_A, "kind": "agency", "account_status": "active", "is_pilot": True},
                    {"tenant_id": T_B, "kind": "demo", "account_status": "trial", "is_pilot": False},
                    {"tenant_id": T_C, "kind": "agency", "account_status": "active", "is_pilot": False},
                    {"tenant_id": T_D, "kind": "demo", "account_status": "closed", "is_pilot": False}],
        "subscriptions": [
            {"tenant_id": T_A, "product": "content", "plan_key": "content.pro", "status": "active",
             "trial_ends_at": None, "paid_through": date(2026, 10, 31), "seats": None},
            {"tenant_id": T_A, "product": "crm", "plan_key": "crm.core", "status": "active",
             "trial_ends_at": None, "paid_through": None, "seats": 3},
            {"tenant_id": T_B, "product": "content", "plan_key": "content.starter", "status": "trialing",
             "trial_ends_at": datetime(2026, 11, 20, 0, 0, tzinfo=UTC), "paid_through": None, "seats": None},
            {"tenant_id": T_C, "product": "content", "plan_key": "content.pro", "status": "active",
             "trial_ends_at": None, "paid_through": None, "seats": None},
            {"tenant_id": T_D, "product": "content", "plan_key": "content.pro", "status": "canceled",
             "trial_ends_at": None, "paid_through": None, "seats": None}],
        "entitlements": [
            {"tenant_id": T_A, "product": "content", "value_kind": "flag", "source": "plan", "n": 5,
             "expires_at": None},
            {"tenant_id": T_A, "product": "platform", "value_kind": "limit", "source": "override", "n": 1,
             "expires_at": None},
            {"tenant_id": T_A, "product": "crm", "value_kind": "seats", "source": "plan", "n": 1, "expires_at": None},
            {"tenant_id": T_B, "product": "content", "value_kind": "limit", "source": "trial", "n": 3,
             "expires_at": datetime(2026, 11, 20, 0, 0, tzinfo=UTC)}],
        "spend": [{"tenant_id": T_A, "app": "cis", "calls": 40, "refused_calls": 0, "cost_usd": 7.0},
                  {"tenant_id": T_A, "app": "crm", "calls": 10, "refused_calls": 0, "cost_usd": 1.5},
                  {"tenant_id": T_B, "app": "cis", "calls": 30, "refused_calls": 4, "cost_usd": 5.0},
                  {"tenant_id": T_C, "app": "cis", "calls": 5, "refused_calls": 0, "cost_usd": 0.75},
                  {"tenant_id": None, "app": "cis", "calls": 900, "refused_calls": 2, "cost_usd": 30.0}],
        "caps": {T_A: _cap(10.0, source="budget_row"), T_B: _cap(5.0, alert=50, approval=90, source="plan"),
                 T_C: _cap(None, source="none")},
    }
    facts.update(over)
    return facts


def _fi08(facts=None):
    reg = _reg()
    return b2b_ops.render_fi08(reg, reg.report("FI-08"), OCT1, OCT31, facts or _fi08_facts(), NOW)


def test_fi08_spend_against_each_tenants_own_cap():
    out = _fi08()
    _assert_lists_registry_kpis(out, "FI-08")
    rows = {r["tenant"]: r for r in out.output["tenants"]["rows"]}
    assert set(rows) == {T_A[:8], T_B[:8], T_C[:8]}                    # T_D: canceled and no spend
    a, b, c = rows[T_A[:8]], rows[T_B[:8]], rows[T_C[:8]]
    assert a["spend_usd"] == 8.5 and a["used_pct"] == 85.0 and a["state"] == "alert"
    assert a["by_app"] == {"cis": 7.0, "crm": 1.5}
    assert b["state"] == "approval" and b["refused_calls"] == 4   # 100 % >= its own 90 %
    assert c["state"] == "unlimited"
    assert out.output["tenants"]["platform_usd"] == 30.0 and out.output["tenants"]["tenant_usd"] == 14.25
    k = _kpis(out)
    assert k["K-TEN-AICAP"]["value"] == 2 and k["K-TEN-AICAP"]["status"] == "red"
    assert k["K-TEN-AINOCAP"]["value"] == 1 and k["K-TEN-AINOCAP"]["status"] == "amber"
    assert k["K-TEN-LIVE"]["value"] == 3 and k["K-TEN-LIVE"]["note"] == "agency 2, demo 1"
    assert k["K-LLM-TENANT"]["value"] == 14.25 and k["K-LLM-TENANT"]["status"] == "info"


def test_fi08_entitlements_and_trials_are_listed():
    out = _fi08()
    rows = {r["tenant"]: r for r in out.output["tenants"]["rows"]}
    assert rows[T_A[:8]]["entitlements"] == {"total": 7, "flag": 5, "limit": 1, "seats": 1,
                                         "sources": ["override", "plan"], "next_expiry": None}
    assert rows[T_B[:8]]["entitlements"]["next_expiry"] == "2026-11-20"
    assert [s["plan_key"] for s in rows[T_A[:8]]["subscriptions"]] == ["content.pro", "crm.core"]
    assert "content.starter trial ends 2026-11-20" in out.markdown
    assert "## Entitlements in force (tenant.entitlements.v1)" in out.markdown


def test_fi08_shows_tenants_by_id_prefix_never_by_name():
    out = _fi08()
    assert "aaaaaaaa (pilot)" in out.markdown and T_A not in out.markdown
    assert all(len(r["tenant"]) == b2b_ops.ID_PREFIX for r in out.output["tenants"]["rows"])


def test_fi08_and_bk03_store_no_full_tenant_id():
    # The stored output (shared.report_run.output) is copied to the public root repo: id prefix only, like FI-03.
    for out in (_fi08(), _bk03()):
        dumped = json.dumps(out.output, default=str)
        assert not any(t in dumped for t in (T_A, T_B, T_C, T_D)), out.output["report_id"]
        assert '"tenant_id"' not in dumped


def test_fi08_all_tenants_ok_is_green():
    facts = _fi08_facts(spend=[{"tenant_id": T_A, "app": "cis", "calls": 1, "refused_calls": 0, "cost_usd": 1.0}],
                        caps={T_A: _cap(10.0), T_B: _cap(5.0), T_C: _cap(10.0)})
    k = _kpis(_fi08(facts))
    assert k["K-TEN-AICAP"]["status"] == "ok" and k["K-TEN-AINOCAP"]["status"] == "ok"


def test_fi08_a_zero_budget_means_no_ai_and_stops():
    facts = _fi08_facts(caps={T_A: _cap(0.0), T_B: _cap(5.0), T_C: _cap(10.0)})
    rows = {r["tenant"]: r for r in _fi08(facts).output["tenants"]["rows"]}
    assert rows[T_A[:8]]["state"] == "approval"


def test_fi08_unreadable_caps_are_no_data_not_green():
    facts = _fi08_facts(caps={"error": "UndefinedTableError: shared.v_tenant_entitlement_resolved"})
    out = _fi08(facts)
    k = _kpis(out)
    assert k["K-TEN-AICAP"]["status"] == "no_data" and k["K-TEN-AINOCAP"]["status"] == "no_data"
    assert {r["state"] for r in out.output["tenants"]["rows"]} == {"unknown"}
    assert "not readable" in out.markdown and out.output["sources_unreadable"]["caps"]


def test_fi08_resolver_missing_reads_unknown():
    facts = _fi08_facts(caps={T_A: None, T_B: None, T_C: None})   # _effective before CIS 353
    assert _kpis(_fi08(facts))["K-TEN-AICAP"]["status"] == "no_data"


def test_fi08_unreadable_ledger_is_a_line():
    out = _fi08(_fi08_facts(spend={"error": "boom"}))
    _assert_lists_registry_kpis(out, "FI-08")
    assert {k["status"] for k in out.output["kpis"]} == {"no_data"}
    assert "not readable (spend: boom)" in out.summary


def test_fi08_in_scope_is_live_or_spending():
    assert b2b_ops.in_scope(_fi08_facts()) == sorted([T_A, T_B, T_C])
    assert b2b_ops.in_scope({"subscriptions": {"error": "x"}, "spend": {"error": "y"}}) == []


def test_fi08_collect_reads_caps_for_the_tenants_in_scope(monkeypatch):
    asked = []

    async def fake_effective(conn, tid, *, savepoint=False):
        asked.append(tid)
        return _cap(10.0)
    monkeypatch.setattr(b2b_ops.tenant_ai_budget, "_effective", fake_effective)

    class Conn:
        async def fetch(self, sql, *args):
            if "FROM shared.tenants" in sql:
                return [{"tenant_id": T_A, "kind": "agency", "account_status": "active", "is_pilot": False}]
            if "FROM shared.tenant_subscription" in sql:
                return [{"tenant_id": T_A, "product": "content", "plan_key": "content.pro", "status": "active",
                         "trial_ends_at": None, "paid_through": None, "seats": None}]
            if "FROM shared.tenant_entitlement" in sql:
                return []
            assert args == (OCT1, date(2026, 11, 1))           # UTC days [first, last + 1)
            return [{"tenant_id": T_C, "app": "cis", "calls": 1, "refused_calls": 0, "cost_usd": 0.1}]
    facts = asyncio.run(b2b_ops.collect_fi08(Conn(), OCT1, OCT31))
    assert asked == sorted([T_A, T_C]) and set(facts["caps"]) == {T_A, T_C}


def test_fi08_spend_sql_leaves_shadow_out_and_never_double_counts_a_day():
    sql = b2b_ops.TENANT_SPEND_SQL
    assert "NOT d.is_shadow" in sql and "is_shadow, l.quality_signal" in sql
    assert "NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d" in sql


# ── BK-03 agency receivables ────────────────────────────────────────────────────────────────────────────────────

def _bk_row(tid, status, overdue, days, amount, warn=7, suspend=30):
    return {"tenant_id": tid, "status": status, "oldest_overdue_days": days, "overdue_invoices": overdue,
            "overdue_amount": amount if isinstance(amount, str) else json.dumps(amount),
            "paid_through": date(2026, 10, 31), "warn_after_days": warn, "suspend_after_days": suspend}


def _bk_facts(**over) -> dict:
    facts = {
        "billing": {"rows": [_bk_row(T_A, "warn", 2, 12, '{"USD": 240.50}'),
                             _bk_row(T_B, "past_due", 1, 45, {"USD": 100, "EUR": 80}),
                             _bk_row(T_C, "ok", 0, None, {})],
                    "read_at": "2026-11-16T01:00:00+00:00"},
        "agencies": [{"tenant_id": T_A, "kind": "agency", "account_status": "past_due"},
                     {"tenant_id": T_B, "kind": "demo", "account_status": "suspended"},
                     {"tenant_id": T_C, "kind": "agency", "account_status": "active"}],
        "previous": None,
    }
    facts.update(over)
    return facts


def _bk03(facts=None):
    reg = _reg()
    return b2b_ops.render_bk03(reg, reg.report("BK-03"), MON, SUN, facts or _bk_facts(), NOW)


def test_bk03_buckets_amounts_and_agency_thresholds():
    out = _bk03()
    _assert_lists_registry_kpis(out, "BK-03")
    rc = out.output["receivables"]
    assert rc["by_bucket"] == {"current": 1, "1-30": 1, "31-60": 1, "61-90": 0, "> 90": 0}
    assert rc["overdue_amount"] == {"EUR": "80", "USD": "340.50"} and rc["overdue_invoices"] == 3
    assert [r["tenant"] for r in rc["rows"]] == ["bbbbbbbb", "aaaaaaaa", "cccccccc"]   # oldest overdue first
    k = _kpis(out)
    assert k["K-AR-OVERDUE"]["value"] == 3 and k["K-AR-OVERDUE"]["status"] == "red"
    assert k["K-AR-AGING"]["value"] == "oldest 45 days (31-60)" and k["K-AR-AGING"]["status"] == "red"
    for kid in ("K-AR-DSO", "K-AR-UNVERIFIED", "K-AP-OVERDUE"):
        assert k[kid]["status"] == "no_data" and b2b_ops.NOT_PUBLISHED in k[kid]["note"]
    assert "## Not published to CIS" in out.markdown and T_A not in out.markdown


def test_bk03_colour_follows_the_agency_terms_not_the_age():
    # 45 days overdue but the agency's terms say warn only (suspend at 60): amber, not red
    facts = _bk_facts(billing={"rows": [_bk_row(T_A, "warn", 1, 45, {"USD": 10}, warn=30, suspend=60)],
                               "read_at": "x"})
    k = _kpis(_bk03(facts))
    assert k["K-AR-OVERDUE"]["status"] == "amber" and k["K-AR-AGING"]["status"] == "amber"
    facts = _bk_facts(billing={"rows": [_bk_row(T_C, "ok", 0, None, {})], "read_at": "x"})
    assert _kpis(_bk03(facts))["K-AR-OVERDUE"]["status"] == "ok"


@pytest.mark.parametrize("days,name", [(None, "current"), (0, "current"), (1, "1-30"), (30, "1-30"),
                                       (31, "31-60"), (60, "31-60"), (61, "61-90"), (90, "61-90"), (91, "> 90")])
def test_bk03_bucket_edges(days, name):
    assert b2b_ops.bucket(days) == name


def test_bk03_trend_against_the_previous_run():
    prev = {"period": {"end": "2026-11-08"}, "receivables": {"rows": [], "overdue_invoices": 5,
                                                             "overdue_amount": {"USD": "900.00"}}}
    out = _bk03(_bk_facts(previous=prev))
    assert "Previous BK-03 run (2026-11-08): 5 overdue invoice(s), 900.00 USD; now 3" in out.markdown
    assert "No earlier BK-03 run" in _bk03().markdown


def test_bk03_view_missing_is_a_line(monkeypatch):
    out = _bk03(_bk_facts(billing={"error": "contract view booking.v_tenant_billing_status not readable"}))
    _assert_lists_registry_kpis(out, "BK-03")
    assert _kpis(out)["K-AR-OVERDUE"]["status"] == "no_data" and "not readable" in out.markdown


def test_bk03_reader_maps_a_refused_view_to_an_error():
    class Conn:
        async def fetch(self, sql, *args):
            raise b2b_ops.billing.VIEW_UNREADABLE[1]("permission denied for view v_tenant_billing_status")
    got = asyncio.run(b2b_ops.billing_status(Conn()))
    assert "not readable (InsufficientPrivilegeError)" in got["error"]


# ── FI-07 weekly flash (counts only) ────────────────────────────────────────────────────────────────────────────

def _fn(day, metric, source, n):
    return {"day": day, "metric": metric, "source_system": source, "n": n}


def _fi07_facts(**over) -> dict:
    facts = {
        "funnel": {"rows": [_fn(date(2026, 11, 10), "trip_case_created", "crm", 4),
                            _fn(date(2026, 11, 11), "proposal_sent", "crm", 3),
                            _fn(date(2026, 11, 12), "confirmed", "crm", 3),
                            _fn(date(2026, 11, 13), "cancelled", "staff", 1),
                            _fn(date(2026, 11, 3), "confirmed", "crm", 2),          # week before
                            _fn(date(2026, 10, 12), "trip_case_created", "crm", 9)]},  # 4 weeks before
        "cases": {"rows": [{"status": "confirmed", "n": 5}, {"status": "proposal_sent", "n": 2},
                           {"status": "new_inquiry", "n": 1}]},
    }
    facts.update(over)
    return facts


def _fi07(facts=None):
    reg = _reg()
    return b2b_ops.render_fi07(reg, reg.report("FI-07"), MON, SUN, facts or _fi07_facts(), NOW)


def test_fi07_counts_by_week_and_source():
    out = _fi07()
    _assert_lists_registry_kpis(out, "FI-07")
    fl = out.output["flash"]
    assert len(fl["weeks"]) == b2b_ops.TREND_WEEKS + 1 and fl["weeks"][0]["start"] == "2026-10-12"
    assert fl["weeks"][0]["trip_case_created"] == 9
    assert fl["this"] == {"start": "2026-11-09", "end": "2026-11-15", "trip_case_created": 4, "proposal_sent": 3,
                          "confirmed": 3, "cancelled": 1}
    assert fl["by_source"]["crm"]["confirmed"] == 3 and fl["by_source"]["staff"]["cancelled"] == 1
    k = _kpis(out)
    assert k["K-BK-CONFIRMED"]["value"] == 3 and k["K-BK-CONFIRMED"]["note"] == "week before 2"
    assert k["K-BK-CANCEL"]["value"] == 0.25 and k["K-BK-CANCEL"]["status"] == "info"   # no flag level yet
    for kid in ("K-BK-GMV", "K-BK-MARGIN", "K-BK-CASH", "K-BK-INVOICED"):
        assert k[kid]["status"] == "no_data" and b2b_ops.NOT_PUBLISHED in k[kid]["note"]


def test_fi07_open_pipeline_follows_the_booking_status_order():
    md = _fi07().markdown
    assert md.index("| new_inquiry | 1 |") < md.index("| proposal_sent | 2 |") < md.index("| confirmed | 5 |")


def test_fi07_quiet_week_and_unreadable_views():
    out = _fi07(_fi07_facts(funnel={"rows": []}))
    assert _kpis(out)["K-BK-CANCEL"]["status"] == "no_data" and "No trip case moved this week." in out.markdown
    out = _fi07(_fi07_facts(funnel={"error": "contract view booking.v_ops_funnel_daily not readable"},
                            cases={"error": "contract view booking.v_ops_case_status not readable"}))
    _assert_lists_registry_kpis(out, "FI-07")
    assert out.markdown.count("Not readable") == 2


def test_fi07_reader_maps_a_missing_view_to_an_error():
    class Conn:
        async def fetch(self, sql, *args):
            assert args[2] == list(b2b_ops.FLASH_METRICS)
            raise b2b_ops.activity._CONTRACT_ERRORS[0]("relation booking.v_ops_funnel_daily does not exist")
    got = asyncio.run(b2b_ops.funnel(Conn(), MON, SUN))
    assert "not readable (UndefinedTableError)" in got["error"]
