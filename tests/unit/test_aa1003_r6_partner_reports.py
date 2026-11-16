"""AA-1003 R6 — partner and agency reports (services/reports/partners.py, crm_ai_report.py) and their crm.ops.v1 inputs
(api/services/report_crm_inputs.py). No DB, no AWS, no network, no paid call.

- EX-03, EX-07 and CRM-04 are generated here and take the run's captured inputs; each renderer lists every KPI its
  registry entry names.
- Every decision level comes from the settings (fixture values here, never the defaults): a missing key leaves its
  check out and names the key.
- A scheduled run (no inputs) says crm.ops.v1 was not read; a manual run shows the CRM part.
- Tenants and agencies appear by id prefix only: no full id and no agency name reaches the output.
- The capture pages through /tenants and /llm/calls, reads usage per agency, drops the agency name, reduces the
  ledger rows to numbers (shadow rows out) and turns a failed page into an unreadable part.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

import httpx
import pytest

from api.services import crm_console, report_crm_inputs
from services.reports import crm_ai_report, generate, partners, registry
from services.reports import settings_keys as sk

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
OCT1, OCT31 = date(2026, 10, 1), date(2026, 10, 31)
MON, SUN = date(2026, 11, 9), date(2026, 11, 15)
R6 = ("EX-03", "EX-07", "CRM-04")

T_A = "aaaaaaaa-1111-4111-8111-000000000001"   # agency: content paying, heavy user, CRM trial with a full team
T_B = "bbbbbbbb-2222-4222-8222-000000000002"   # agency: content paying, low use two months, CRM agency locked
T_C = "cccccccc-3333-4333-8333-000000000003"   # agency: cancelled in October
T_D = "dddddddd-4444-4444-8444-000000000004"   # demo tenant: not a partner
T_E = "eeeeeeee-5555-4555-8555-000000000005"   # agency created in October, stuck after its brand brief
TH = {"activation_days": 10, "stuck_days": 5, "upsell_pct": 75, "low_use_pct": 20, "low_use_months": 2, "missing": []}


def _t(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=UTC)


def _reg():
    return registry.load()


def _kpis(out) -> dict:
    return {k["id"]: k for k in out.output["kpis"]}


def _assert_shape(out, rid):
    assert [k["id"] for k in out.output["kpis"]] == list(_reg().report(rid).kpis), rid
    assert out.output["report_id"] == rid and out.markdown.startswith("# ")
    dumped = json.dumps(out.output, default=str) + out.markdown
    for full in (T_A, T_B, T_C, T_D, T_E):
        assert full not in dumped, f"{rid}: full tenant id in the output"
    assert "Zz Invented Travel" not in dumped


TENANTS = [
    {"tenant_id": T_A, "kind": "agency", "account_status": "active", "is_canary": False, "is_pilot": False,
     "created_at": _t(2026, 8, 1), "cancelled_at": None, "has_reason": False},
    {"tenant_id": T_B, "kind": "agency", "account_status": "past_due", "is_canary": False, "is_pilot": False,
     "created_at": _t(2026, 8, 5), "cancelled_at": None, "has_reason": False},
    {"tenant_id": T_C, "kind": "agency", "account_status": "closed", "is_canary": False, "is_pilot": False,
     "created_at": _t(2026, 7, 1), "cancelled_at": _t(2026, 10, 20), "has_reason": True},
    {"tenant_id": T_D, "kind": "demo", "account_status": "trial", "is_canary": False, "is_pilot": False,
     "created_at": _t(2026, 9, 1), "cancelled_at": None, "has_reason": False},
    {"tenant_id": T_E, "kind": "agency", "account_status": "trial", "is_canary": False, "is_pilot": False,
     "created_at": _t(2026, 10, 3), "cancelled_at": None, "has_reason": False},
]
SUBS = [
    {"tenant_id": T_A, "product": "content", "plan_key": "content.growth", "status": "active", "seats": None,
     "started_at": _t(2026, 8, 1), "canceled_at": None, "trial_ends_at": None},
    {"tenant_id": T_A, "product": "crm", "plan_key": "crm.core", "status": "trialing", "seats": 3,
     "started_at": _t(2026, 10, 10), "canceled_at": None, "trial_ends_at": _t(2026, 11, 10)},
    {"tenant_id": T_B, "product": "content", "plan_key": "content.starter", "status": "past_due", "seats": None,
     "started_at": _t(2026, 8, 5), "canceled_at": None, "trial_ends_at": None},
    {"tenant_id": T_C, "product": "content", "plan_key": "content.starter", "status": "canceled", "seats": None,
     "started_at": _t(2026, 7, 1), "canceled_at": _t(2026, 10, 20), "trial_ends_at": None},
    {"tenant_id": T_D, "product": "content", "plan_key": "content.growth", "status": "active", "seats": None,
     "started_at": _t(2026, 9, 1), "canceled_at": None, "trial_ends_at": None},
    {"tenant_id": T_E, "product": "content", "plan_key": "content.starter", "status": "active", "seats": None,
     "started_at": _t(2026, 10, 3), "canceled_at": None, "trial_ends_at": None},
]
PLANS = [
    {"plan_key": "content.growth", "product": "content", "price": 200.0, "price_unit": "tenant", "seats_included": None,
     "extra_seat": None},
    {"plan_key": "content.starter", "product": "content", "price": 50.0, "price_unit": "tenant",
     "seats_included": None, "extra_seat": None},
    {"plan_key": "crm.core", "product": "crm", "price": 30.0, "price_unit": "seat", "seats_included": None,
     "extra_seat": None},
]
STEPS = [
    {"tenant_id": T_A, "account_status": "active", "created_at": _t(2026, 8, 1), "member_at": _t(2026, 8, 2),
     "brief_at": _t(2026, 8, 3), "approved_at": _t(2026, 8, 6), "publish_at": _t(2026, 8, 8), "has_seo_config": True},
    {"tenant_id": T_B, "account_status": "past_due", "created_at": _t(2026, 8, 5), "member_at": _t(2026, 8, 6),
     "brief_at": None, "approved_at": None, "publish_at": None, "has_seo_config": False},
    {"tenant_id": T_C, "account_status": "closed", "created_at": _t(2026, 7, 1), "member_at": None, "brief_at": None,
     "approved_at": None, "publish_at": None, "has_seo_config": False},
    {"tenant_id": T_E, "account_status": "trial", "created_at": _t(2026, 10, 3), "member_at": _t(2026, 10, 4),
     "brief_at": _t(2026, 10, 6), "approved_at": None, "publish_at": None, "has_seo_config": True},
]


def _ex03_facts(**over):
    facts = {"tenants": TENANTS, "subscriptions": SUBS, "plans": PLANS,
             "rewrites": [{"tenant_id": T_A, "year_month": "2026-10", "rewrite_count": 180},
                          {"tenant_id": T_A, "year_month": "2026-09", "rewrite_count": 120},
                          {"tenant_id": T_B, "year_month": "2026-10", "rewrite_count": 5},
                          {"tenant_id": T_B, "year_month": "2026-09", "rewrite_count": 3}],
             "steps": STEPS, "quotas": {T_A: 200, T_B: 50, T_C: 50, T_E: 50}, "months": ["2026-09", "2026-10"]}
    facts.update(over)
    return facts


def _inputs(**parts):
    base = {
        "coverage": {"as_of": "2026-11-16T00:59:00+00:00",
                     "agencies": {"active": 1, "locked": 1, "offboarding": 0, "offboarded": 0, "synthetic": 2},
                     "members": {"active": 4, "invited": 1, "disabled": 0}},
        "agencies": {"rows": [
            {"tenant_id": T_A, "state": "active", "is_synthetic": True, "has_owner": True,
             "created_at": "2026-10-10T00:00:00+00:00", "lock_source": None},
            {"tenant_id": T_B, "state": "locked", "is_synthetic": True, "has_owner": False,
             "created_at": "2026-10-01T00:00:00+00:00", "lock_source": "aa_ops"},
            {"tenant_id": T_E, "state": "active", "is_synthetic": True, "has_owner": False,
             "created_at": "2026-11-01T00:00:00+00:00", "lock_source": None}]},
        "usage": {"by_agency": {
            T_A: {"seats_used": 3, "seats_allowed": 3, "active_members_30d": 3, "ai_cost_usd_month": "1.2000",
                  "ai_budget_usd_month": "2.0000", "failed_jobs_24h": 0, "last_activity_at": "2026-11-15T10:00:00Z",
                  "period_start": "2026-11-01"},
            T_B: {"seats_used": 1, "seats_allowed": 5, "active_members_30d": 0, "ai_cost_usd_month": "0",
                  "ai_budget_usd_month": "0.5000", "failed_jobs_24h": 1, "last_activity_at": None,
                  "period_start": "2026-11-01"}}, "unreadable": {}},
        "llm_usage": {"rows": [
            {"day": "2026-10-02", "stage": "crm_assist", "model": "m-one", "calls": 40, "failed_calls": 2,
             "cost_usd": "0.400000"},
            {"day": "2026-10-03", "stage": "crm_scrub", "model": "m-two", "calls": 10, "failed_calls": 0,
             "cost_usd": "0.010000"}]},
        "llm_calls": report_crm_inputs.reduce_calls(
            [{"tenant_id": T_A, "status": "ok", "cost_usd": "0.05", "latency_ms": 900 + i, "stage": "crm_assist"}
             for i in range(30)]
            + [{"tenant_id": T_B, "status": "refused", "refusal_code": "policy", "cost_usd": "0.60",
                "latency_ms": 2000, "stage": "crm_assist"}]
            + [{"tenant_id": T_B, "status": "ok", "cost_usd": "9", "latency_ms": 1, "stage": "crm_assist",
                "is_shadow": True}]),
    }
    base.update(parts)
    return {"contract": "crm.ops.v1", "read_at": "2026-11-16T01:00:00+00:00", "parts": base}


# ── registry and dispatch ───────────────────────────────────────────────────────────────────────────────────────

def test_r6_reports_are_generated_here_and_take_inputs():
    reg = _reg()
    for rid in R6:
        r = reg.report(rid)
        assert r.runnable and rid in generate.RENDERERS and rid in generate.TAKES_INPUTS, rid
        assert r.automated_from == date(2026, 11, 17) and r.status == "partial", rid
        assert report_crm_inputs.needs_inputs(rid)
    assert reg.report("EX-03").schedule == reg.report("CRM-04").schedule == "monthly"
    assert reg.report("EX-07").schedule == "weekly"
    assert not report_crm_inputs.needs_inputs("FI-08")


def test_thresholds_read_the_settings_and_name_a_missing_key(monkeypatch):
    seen = []

    async def aget(key, tenant_id=None):
        seen.append(key)
        if key == sk.STUCK_DAYS:
            raise KeyError(key)
        return 3

    monkeypatch.setattr(partners.settings, "aget", aget)
    th = asyncio.run(partners.thresholds())
    assert set(seen) == set(sk.ALL)
    assert th["stuck_days"] is None and th["missing"] == [sk.STUCK_DAYS] and th["upsell_pct"] == 3


# ── EX-03 ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_monthly_price_per_unit():
    plans = {p["plan_key"]: p for p in PLANS}
    assert partners.monthly_price({"plan_key": "crm.core", "seats": 3}, plans) == 90.0
    extra = {"plan_key": "x", "seats": 5}
    assert partners.monthly_price(extra, {"x": {"price": 100.0, "price_unit": "tenant", "seats_included": 3,
                                                 "extra_seat": 10.0}}) == 120.0
    assert partners.monthly_price({"plan_key": "unknown"}, plans) == 0.0


def test_growth_counts_partners_only_and_bridges_mrr():
    gr = partners.growth(_ex03_facts(), OCT1, OCT31)
    assert gr["partners"] == 4 and gr["not_partners"] == {"demo": 1}
    content = gr["by_product"]["content"]
    assert (content["start"], content["new"], content["cancelled"], content["end"]) == (3, 1, 1, 3)
    assert gr["by_product"]["crm"]["new"] == 1 and gr["by_product"]["crm"]["trialing"] == 1
    assert gr["paying"] == 3                       # A, B (past_due), E; D is a demo, C cancelled
    m = gr["mrr"]
    assert m["end"] == 300.0                        # 200 + 50 + 50; the CRM trial counts 0
    assert m["start"] + m["new"] - m["cancelled"] + m["other"] == pytest.approx(m["end"])
    assert gr["churned"] == [T_C[:8]] and gr["churn_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert gr["churn_reasons_recorded"] == 1


def test_paying_counts_only_subscriptions_live_at_the_period_end():
    # an October report generated in November: T_E's content and T_A's tripplanner both started after 31 October
    late = [dict(s, started_at=_t(2026, 11, 2)) if s["tenant_id"] == T_E else s for s in SUBS]
    late.append({"tenant_id": T_A, "product": "tripplanner", "plan_key": "content.growth", "status": "active",
                 "seats": None, "started_at": _t(2026, 11, 3), "canceled_at": None, "trial_ends_at": None})
    gr = partners.growth(_ex03_facts(subscriptions=late), OCT1, OCT31)
    assert gr["paying"] == 2                       # A, B; E only pays from November
    assert gr["by_product"]["content"]["paying"] == 2 and gr["by_product"]["tripplanner"]["paying"] == 0
    assert gr["mrr"]["end"] == 250.0                # the headline's paying count and MRR agree on the same end


def test_activation_counts_only_closed_windows():
    act = partners.activation(STEPS, OCT1, OCT31, 10, NOW)
    assert act == {"cohort": 1, "complete": 1, "activated": 0, "pending": 0, "rate": 0.0}
    early = [dict(STEPS[3], created_at=_t(2026, 11, 10), publish_at=_t(2026, 11, 12))]
    act = partners.activation(early, date(2026, 11, 1), date(2026, 11, 30), 10, NOW)
    assert (act["complete"], act["activated"], act["pending"], act["rate"]) == (0, 1, 0, None)
    assert partners.activation(STEPS, OCT1, OCT31, None, NOW)["rate"] is None


def test_health_flags_come_from_the_settings_and_own_limits():
    inputs = _inputs()
    facts = _ex03_facts()
    qu = partners.quota_use(facts, TH, inputs["parts"]["usage"])
    assert qu["content"][T_A]["use_pct"] == 90.0 and qu["crm_seats"][T_A] == 100.0
    rows = {r["tenant"]: r for r in partners.health(facts, TH, qu, inputs["parts"]["agencies"],
                                                    inputs["parts"]["usage"])}
    assert any("rewrites 90 %" in u for u in rows[T_A[:8]]["upsell"])
    assert any("CRM seats" in u for u in rows[T_A[:8]]["upsell"]) and not rows[T_A[:8]]["risk"]
    risk_b = " ".join(rows[T_B[:8]]["risk"])
    assert "content subscription past_due" in risk_b and "account past_due" in risk_b
    assert "under 20 % of quota for 2 month(s)" in risk_b and "CRM agency locked" in risk_b
    # a higher upsell level from the settings removes the upsell flag: the level is the setting, not a constant
    rows = {r["tenant"]: r for r in partners.health(facts, dict(TH, upsell_pct=100), qu, {"rows": []}, {"error": "x"})}
    assert not any("rewrites" in u for u in rows.get(T_A[:8], {"upsell": []})["upsell"])


def test_ex03_manual_run_shows_the_crm_part():
    out = partners.render_ex03(_reg(), _reg().report("EX-03"), OCT1, OCT31, _ex03_facts(), TH, _inputs(), NOW)
    _assert_shape(out, "EX-03")
    k = _kpis(out)
    assert k["K-PART-PAYING"]["value"] == 3.0 and k["K-PART-MRR"]["value"] == 300.0
    assert k["K-PART-QUOTA"]["status"] == "amber" and "75 %" in k["K-PART-QUOTA"]["note"]
    assert "Booking B2B partners not published to CIS" in k["K-PART-PAYING"]["note"]
    assert "## CRM agencies (crm.ops.v1)" in out.markdown and "locked 1" in out.markdown
    assert "3 / 3" in out.markdown and partners.NOT_READ not in out.markdown


def test_ex03_scheduled_run_says_crm_not_read():
    out = partners.render_ex03(_reg(), _reg().report("EX-03"), OCT1, OCT31, _ex03_facts(), TH, None, NOW)
    _assert_shape(out, "EX-03")
    assert partners.NOT_READ in out.markdown and "crm.ops.v1 not read" in out.summary


def test_ex03_unreadable_source_is_a_line_not_an_exception():
    out = partners.render_ex03(_reg(), _reg().report("EX-03"), OCT1, OCT31,
                               _ex03_facts(subscriptions={"error": "UndefinedTable"}), TH, None, NOW)
    _assert_shape(out, "EX-03")
    assert all(k["status"] == "no_data" for k in out.output["kpis"]) and "UndefinedTable" in out.markdown


def test_ex03_missing_setting_leaves_its_check_out():
    th = dict(TH, activation_days=None, missing=[sk.ACTIVATION_DAYS])
    out = partners.render_ex03(_reg(), _reg().report("EX-03"), OCT1, OCT31, _ex03_facts(), th, None, NOW)
    k = _kpis(out)["K-PART-ACT"]
    assert k["status"] == "no_data" and sk.ACTIVATION_DAYS in k["note"]


# ── EX-07 ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_funnel_stuck_list_and_time_to_value():
    fn = partners.funnel(STEPS, MON, SUN, 5, NOW)
    assert fn["reached"]["created"] == 4 and fn["reached"]["publish"] == 1
    assert fn["ttv_median_days"] == 7.0
    stuck = {r["partner"]: r for r in fn["stuck"]}
    assert set(stuck) == {T_B[:8], T_E[:8]}                    # C is closed, A published
    assert stuck[T_E[:8]]["at"] == "Brand brief" and stuck[T_E[:8]]["waiting_for"] == "First approved piece"
    assert fn["no_seo_config"] == [T_B[:8], T_C[:8]]
    longer = partners.funnel(STEPS, MON, SUN, 60, NOW)
    assert [r["partner"] for r in longer["stuck"]] == [T_B[:8]]   # the level is the setting


def test_ex07_counts_crm_stuck_when_read():
    out = partners.render_ex07(_reg(), _reg().report("EX-07"), MON, SUN, {"steps": STEPS}, TH, _inputs(), NOW)
    _assert_shape(out, "EX-07")
    k = _kpis(out)
    assert k["K-ONB-STUCK"]["value"] == 3.0 and k["K-ONB-STUCK"]["status"] == "amber"   # 2 content + E (B is locked)
    assert k["K-ONB-CRM"]["status"] == "no_data" and "not published to CIS" in k["K-ONB-CRM"]["note"]
    assert "(synthetic)" in out.markdown


def test_ex07_scheduled_and_missing_setting():
    th = dict(TH, stuck_days=None, missing=[sk.STUCK_DAYS])
    out = partners.render_ex07(_reg(), _reg().report("EX-07"), MON, SUN, {"steps": STEPS}, th, None, NOW)
    _assert_shape(out, "EX-07")
    assert _kpis(out)["K-ONB-STUCK"]["status"] == "no_data" and partners.NOT_READ in out.markdown


# ── CRM-04 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_crm04_roll_up_against_each_agency_budget():
    out = crm_ai_report.render_crm04(_reg(), _reg().report("CRM-04"), OCT1, OCT31, _inputs(), NOW)
    _assert_shape(out, "CRM-04")
    k = _kpis(out)
    assert k["K-CRM04-FAILURE-RATE"]["value"] == pytest.approx(0.04)
    assert k["K-CRM04-REFUSAL-RATE"]["value"] == pytest.approx(1 / 31, abs=1e-4)
    assert k["K-CRM04-COST-PER-AGENCY"]["status"] == "amber" and T_B[:8] in k["K-CRM04-COST-PER-AGENCY"]["note"]
    assert "shadow calls left out: 1" in k["K-CRM04-LATENCY-P95"]["note"]
    assert k["K-CRM04-AI-CALLS-PER-ACTIVE-MEMBER"]["value"] == pytest.approx(31 / 3, abs=1e-3)


def test_crm04_scheduled_run_is_not_read():
    out = crm_ai_report.render_crm04(_reg(), _reg().report("CRM-04"), OCT1, OCT31, None, NOW)
    _assert_shape(out, "CRM-04")
    assert all(k["status"] == "no_data" for k in out.output["kpis"])
    assert "not read" in out.summary


def test_render_dispatch_passes_inputs(monkeypatch):
    out = asyncio.run(generate.render(None, _reg(), _reg().report("CRM-04"), OCT1, OCT31, NOW, inputs=_inputs()))
    assert "calls" in out.summary and "not read" not in out.summary


# ── capture (api/services/report_crm_inputs.py) ─────────────────────────────────────────────────────────────────

def _token():
    import base64
    body = base64.urlsafe_b64encode(json.dumps({"iss": "https://issuer.example"}).encode()).decode().rstrip("=")
    return f"Bearer h.{body}.s"


def _transport(fail_calls_page=False):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        seen.append((path, q))
        if path.endswith("/data-coverage"):
            return httpx.Response(200, json={"as_of": "x", "agencies": {"active": 2}, "members": {"active": 1}})
        if path.endswith("/tenants"):
            if not q.get("cursor"):
                return httpx.Response(200, json={"items": [
                    {"tenant_id": T_A, "agency_name": "Zz Invented Travel", "state": "active", "is_synthetic": True,
                     "has_owner": True, "holds_travellers": False, "created_at": "2026-10-01T00:00:00Z"}],
                    "next_cursor": T_A})
            return httpx.Response(200, json={"items": [
                {"tenant_id": T_B, "agency_name": "Zz Invented Tours", "state": "offboarded", "is_synthetic": True,
                 "has_owner": False, "holds_travellers": False, "created_at": "2026-10-02T00:00:00Z"}],
                "next_cursor": None})
        if path.endswith("/usage") and "/tenants/" in path:
            return httpx.Response(200, json={"seats_used": 1, "seats_allowed": 3, "active_members_30d": 1,
                                             "ai_cost_usd_month": "0.1", "ai_budget_usd_month": "1",
                                             "failed_jobs_24h": 0, "last_activity_at": None,
                                             "period_start": "2026-11-01"})
        if path.endswith("/llm/usage"):
            return httpx.Response(200, json={"from": q["from"], "to": q["to"], "rows": [
                {"day": "2026-10-02", "stage": "crm_assist", "model": "m-one", "calls": 2, "failed_calls": 0,
                 "input_tokens": 1, "output_tokens": 1, "cost_usd": "0.02"}]})
        if path.endswith("/llm/calls"):
            if q.get("cursor"):
                if fail_calls_page:
                    return httpx.Response(500, json={"detail": "boom"})
                return httpx.Response(200, json={"rows": [
                    {"tenant_id": T_A, "status": "ok", "cost_usd": "0.01", "latency_ms": 100, "is_shadow": False}],
                    "next_cursor": None})
            return httpx.Response(200, json={"rows": [
                {"tenant_id": T_A, "status": "refused", "refusal_code": "policy", "cost_usd": "0.01",
                 "latency_ms": 300, "is_shadow": False}], "next_cursor": "c1"})
        return httpx.Response(404, json={"detail": "nope"})

    return httpx.MockTransport(handler), seen


def test_capture_reads_every_page_and_drops_names(monkeypatch):
    transport, seen = _transport()
    monkeypatch.setattr(crm_console, "_transport", transport)
    out = asyncio.run(report_crm_inputs.capture("CRM-04", OCT1, OCT31, _token()))
    parts = out["parts"]
    assert [r["tenant_id"] for r in parts["agencies"]["rows"]] == [T_A, T_B]
    assert "Zz Invented" not in json.dumps(out)
    assert list(parts["usage"]["by_agency"]) == [T_A]                 # the offboarded agency is not read
    assert parts["llm_calls"]["total"]["calls"] == 2 and parts["llm_calls"]["total"]["refused"] == 1
    assert parts["llm_calls"]["by_agency"][T_A]["latency_p95_ms"] is not None
    assert parts["llm_usage"]["rows"][0]["calls"] == 2
    assert any(p.endswith("/llm/calls") and q.get("cursor") == "c1" for p, q in seen)
    assert asyncio.run(report_crm_inputs.capture("FI-08", OCT1, OCT31, _token())) is None


def test_capture_failed_page_makes_the_part_unreadable(monkeypatch):
    transport, _seen = _transport(fail_calls_page=True)
    monkeypatch.setattr(crm_console, "_transport", transport)
    out = asyncio.run(report_crm_inputs.capture("CRM-04", OCT1, OCT31, _token()))
    assert "error" in out["parts"]["llm_calls"] and "rows" in out["parts"]["llm_usage"]


def test_capture_without_a_cognito_session_marks_every_part():
    out = asyncio.run(report_crm_inputs.capture("EX-03", OCT1, OCT31, None))
    assert set(out["parts"]) == {"coverage", "agencies", "usage"}
    assert all("error" in p for p in out["parts"].values())
