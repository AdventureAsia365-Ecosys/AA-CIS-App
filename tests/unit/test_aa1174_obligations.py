"""AA-1174 — the privacy obligations of Platform › Compliance without a database: the obligation state rule, laws out
of the percentage, the grading of the new facts against the settings, the cis_db and crm_ops sources (counts and dates
only; a 401 / 403 / no session from the CRM writes no evidence row), the Obligations answer, the route policy and the
read-only "view as" rule (a GET writes nothing, only Refresh collects the live sources). Invented ids (ex- law, EX-
controls)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz
from api.routers import admin
from api.services import compliance, compliance_crm, compliance_db, compliance_obligations
from api.services import compliance_score as cs
from api.services import compliance_settings as keys

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
SECRET = "the-real-secret"
BASE = "/admin/platform/compliance"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}


def settings(**over) -> cs.ScoreSettings:
    limits = {cid: (fact, float(over.get(key, {"cis.compliance.legal_docs_overdue_max": 0,
                                               "cis.compliance.erasure_drill_max_age_days": 180,
                                               "cis.compliance.retention_run_max_age_days": 3,
                                               "cis.compliance.retention_live_max_age_days": 7,
                                               "cis.compliance.consent_capture_max_age_days": 90,
                                               "cis.compliance.campaign_consent_violations_max": 0}.get(key, 90))),
                    kind) for cid, (fact, key, kind) in keys.FACT_LIMITS.items()}
    return cs.ScoreSettings(validity_hours={"root_daily": 72, "cis_db": 72, "crm_ops": 72}, heartbeat_hours=36,
                            partial_weight=0.5, fact_limits=limits)


def ev(check_id, detail, source="root_daily", status="pass", hours_ago=1):
    return {"source": source, "check_id": check_id, "status": status, "collected_at": NOW - timedelta(hours=hours_ago),
            "valid_until": None, "summary": "x", "detail": detail, "link": "https://github.com/ExampleOrg/x"}


# ── state rule ──────────────────────────────────────────────────────────────────────────────────────────────────────


def test_obligation_state_rule():
    assert cs.obligation_state("met", ["met", "no_evidence"]) == ("met", False)
    assert cs.obligation_state("unmapped", []) == ("not_checked", False)
    assert cs.obligation_state("gap", ["no_evidence", "no_evidence"]) == ("not_checked", False)
    assert cs.obligation_state("gap", ["gap", "no_evidence"]) == ("not_met", False)
    assert cs.obligation_state("partial", ["partial"]) == ("not_met", True)
    assert cs.obligation_state("stale", ["stale"]) == ("not_met", False)
    counts = cs.law_counts([{"state": "met"}, {"state": "not_met", "partly": True, "counsel_review": True},
                            {"state": "not_checked", "counsel_review": True}])
    assert counts == {"met": 1, "not_met": 1, "not_checked": 1, "partly": 1, "counsel_review": 2, "total": 3}


def test_laws_stay_out_of_the_percentage_unless_scored():
    reqs = [{"framework": "iso27001:2022", "ref": "A.5.1"}, {"framework": "ex-law:2025", "ref": "Art.1"}]
    ctl = [{"control_id": "EX-PRV-01", "checks": [{"check_id": "legal_ropa_reviewed", "required": True}]}]
    maps = [{"control_id": "EX-PRV-01", "framework": "ex-law:2025", "ref": "Art.1", "coverage": "full"}]
    latest = {"legal_ropa_reviewed": ev("legal_ropa_reviewed", {"overdue": 0})}
    r = cs.evaluate(reqs, ctl, maps, latest, {"root_daily": NOW}, settings(), NOW, scored=["iso27001:2022"])
    assert list(r["frameworks"]) == ["iso27001:2022"]
    assert r["requirements"][("ex-law:2025", "Art.1")] == "met"
    default = cs.evaluate(reqs, ctl, maps, latest, {"root_daily": NOW}, settings(), NOW)
    assert list(default["frameworks"]) == list(cs.FRAMEWORKS)     # unchanged default: the six of 466


@pytest.mark.parametrize("check_id,detail,expected", [
    ("legal_dpa_reviewed", {"overdue": 0, "pages": 5}, "pass"),
    ("legal_dpa_reviewed", {"overdue": 1, "pages": 5}, "fail"),      # one annex past its own review cycle
    ("legal_dpa_reviewed", {"pages": 5}, "error"),                   # the fact is missing
    ("last_erasure_drill", {"last_date": "2026-10-31"}, "pass"),
    ("last_erasure_drill", {"last_date": "2026-01-02"}, "fail"),     # older than 180 days
    ("retention_runs_recent", {"last_date": "2026-11-22"}, "pass"),
    ("retention_runs_recent", {"last_date": "2026-11-10"}, "fail"),
    ("retention_live_enabled", {"live_last_date": "2026-11-20"}, "pass"),
    ("retention_live_enabled", {"live_last_date": "2026-11-10"}, "fail"),   # newest live run 13 days old
    ("retention_live_enabled", {"never_live_kinds": []}, "error"),           # the fact is missing
    ("campaign_consent_enforced", {"violations": 0, "sent": 12}, "pass"),
    ("campaign_consent_enforced", {"violations": 1, "sent": 12}, "fail"),
])
def test_privacy_facts_are_graded_against_the_settings(check_id, detail, expected):
    source = {"retention_runs_recent": "cis_db", "retention_live_enabled": "cis_db",
              "campaign_consent_enforced": "crm_ops"}.get(check_id, "root_daily")
    st = cs.check_state(check_id, ev(check_id, detail, source=source), {source: NOW}, settings(), NOW)
    assert st.state == expected, st


def test_a_shorter_setting_turns_a_legal_check_red():
    row = ev("legal_ropa_reviewed", {"overdue": 2})
    assert cs.check_state("legal_ropa_reviewed", row, {"root_daily": NOW},
                          settings(**{"cis.compliance.legal_docs_overdue_max": 2}), NOW).state == "pass"
    assert cs.check_state("legal_ropa_reviewed", row, {"root_daily": NOW}, settings(), NOW).state == "fail"


# ── cis_db ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def _agg(kind, last_run, last_live, dry=0, live=0):
    return {"kind": kind, "last_run": last_run, "last_live": last_live, "dry_runs": dry, "live_or_held": live}


def test_cis_db_retention_rows():
    day = datetime(2026, 11, 22, 4, tzinfo=timezone.utc)
    rows = [_agg(k, day - timedelta(days=i), day if i else None, dry=3) for i, k in enumerate(compliance_db.KINDS)]
    runs, lives = compliance_db.evaluate(rows, NOW)
    assert runs["check_id"] == "retention_runs_recent" and runs["status"] == "pass"
    assert runs["detail"]["last_date"] == (day - timedelta(days=len(compliance_db.KINDS) - 1)).date().isoformat()
    assert lives["status"] == "fail" and lives["detail"]["never_live_kinds"] == [compliance_db.KINDS[0]]
    assert compliance_db.KINDS[0] in lives["summary"] and "live_last_date" not in lives["detail"]
    for r in (runs, lives):
        assert r["source"] == "cis_db" and r["link"].startswith("https://github.com/")
        assert set(r) >= {"collected_at", "collector", "collector_version", "summary"}
    runs, _ = compliance_db.evaluate(rows[1:], NOW)                  # one kind never ran
    assert runs["status"] == "error" and compliance_db.KINDS[0] in runs["summary"]


def test_cis_db_a_live_run_long_ago_then_dry_runs_only_fails():
    """One approved live run, then a retention window changed: every scheduled run since is a DRY RUN (decide_mode),
    so nothing is deleted. Recent runs and an old live run must not read as storage limitation met."""
    last_live = NOW - timedelta(days=40)
    recent = NOW - timedelta(hours=5)
    rows = [_agg(k, recent, last_live, dry=30, live=0) for k in compliance_db.KINDS]
    runs, lives = compliance_db.evaluate(rows, NOW)
    assert runs["status"] == "pass"                                   # the schedule runs ...
    assert lives["status"] == "pass" and lives["detail"]["live_last_date"] == last_live.date().isoformat()
    s = settings()
    assert cs.check_state("retention_runs_recent", runs, {"cis_db": NOW}, s, NOW).state == "pass"
    assert cs.check_state("retention_live_enabled", lives, {"cis_db": NOW}, s, NOW).state == "fail"   # ... deletes nothing
    fresh = [_agg(k, recent, NOW - timedelta(days=2), dry=0, live=30) for k in compliance_db.KINDS]
    _, ok = compliance_db.evaluate(fresh, NOW)
    assert cs.check_state("retention_live_enabled", ok, {"cis_db": NOW}, s, NOW).state == "pass"


def test_cis_db_live_baseline_matches_the_job_runner():
    """A manual_override live run is not a live baseline for the retention job runner; the evidence query must leave
    it out the same way (last_live and the live count), or the page would count a Dev proof run as retention on."""
    from services.jobs import retention_job

    assert compliance_db.NOT_OVERRIDE in retention_job._LAST_LIVE_SQL
    assert compliance_db.SQL_RETENTION.count(compliance_db.NOT_OVERRIDE) == 2


def test_cis_db_error_is_a_row_not_an_exception():
    class Broken:
        def acquire(self):
            raise RuntimeError("db down")
    rows = asyncio.run(compliance_db.collect(Broken(), NOW))
    # AA-1173: + the two catalogue checks and the monthly review check, each an error row too
    assert {r["status"] for r in rows} == {"error"} and len(rows) == 5


# ── crm_ops ─────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status,body", [(401, {"detail": "cognito_session_required"}),
                                         (403, {"detail": "forbidden"})])
def test_crm_no_session_or_forbidden_writes_no_evidence(status, body):
    with patch.object(compliance_crm.crm_console, "crm_ops_call", AsyncMock(return_value=(status, body))):
        rows, info = asyncio.run(compliance_crm.collect("Bearer x", 30, NOW))
    assert rows == [] and info["http"] == status


def test_crm_unavailable_is_an_error_row():
    with patch.object(compliance_crm.crm_console, "crm_ops_call",
                      AsyncMock(return_value=(404, {"detail": "not_found"}))):
        rows, _ = asyncio.run(compliance_crm.collect("Bearer x", 30, NOW))
    assert [r["status"] for r in rows] == ["error", "error"]


def test_crm_counts_only():
    body = {"as_of": "2026-11-23T09:00:00Z", "window_days": 30, "marketing_sent": 12, "violations": 0,
            "excluded_erased": 1, "tenants": 3, "consents_recorded": 4, "last_consent_at": "2026-11-20T10:00:00Z",
            "names": ["should not pass"]}
    call = AsyncMock(return_value=(200, body))
    with patch.object(compliance_crm.crm_console, "crm_ops_call", call):
        rows, _ = asyncio.run(compliance_crm.collect("Bearer x", 30, NOW))
    assert call.await_args.kwargs["params"] == {"days": 30}
    campaign, capture = rows
    assert campaign["status"] == "pass"
    assert campaign["detail"] == {"violations": 0, "sent": 12, "uncertain": None, "window_days": 30, "tenants": 3,
                                  "excluded_erased": 1}
    assert capture["detail"]["last_date"] == "2026-11-20" and "names" not in str(rows)
    none = compliance_crm.evaluate(dict(body, last_consent_at=None), NOW)[1]
    assert none["status"] == "error"


def test_crm_zero_marketing_sends_is_no_evidence_not_met():
    body = {"window_days": 30, "marketing_sent": 0, "marketing_uncertain": 0, "violations": 0, "tenants": 3,
            "excluded_erased": 0, "consents_recorded": 0, "last_consent_at": None}
    campaign = compliance_crm.evaluate(body, NOW)[0]
    assert campaign["status"] == "manual" and "nothing to check" in campaign["summary"]
    st = cs.check_state("campaign_consent_enforced", campaign, {"crm_ops": NOW}, settings(), NOW)
    assert st.state == "manual"                                    # never pass on 0 violations out of 0 sends
    ctl = cs.control_state({"checks": [{"check_id": "campaign_consent_enforced"}]}, {st.check_id: st})
    assert cs.obligation_state(cs.requirement_state([{"control_id": "C", "coverage": "full"}], {"C": ctl}),
                               [ctl]) == ("not_met", False)


def test_crm_send_uncertain_sends_are_reported():
    body = {"window_days": 30, "marketing_sent": 5, "marketing_uncertain": 2, "violations": 1, "tenants": 1,
            "excluded_erased": 0, "consents_recorded": 1, "last_consent_at": "2026-11-20"}
    campaign = compliance_crm.evaluate(body, NOW)[0]
    assert campaign["detail"]["uncertain"] == 2 and "2 possibly delivered" in campaign["summary"]
    st = cs.check_state("campaign_consent_enforced", campaign, {"crm_ops": NOW}, settings(), NOW)
    assert st.state == "fail"                                      # the CRM counts them as sent and as violations


def test_live_sources_wait_for_the_minimum_interval():
    class Conn:
        async def fetchval(self, sql, source):
            return NOW - timedelta(minutes=2)

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_inner):
                    return Conn()

                async def __aexit__(self_inner, *a):
                    return False
            return Ctx()
    with patch.object(compliance_db, "collect", AsyncMock()) as db:
        out = asyncio.run(compliance.collect_live_sources(Pool(), None, NOW))
    assert out["cis_db"]["state"] == "recent" and out["crm_ops"]["state"] == "recent" and db.await_count == 0


def test_refresh_right_after_a_page_load_still_collects_live_sources():
    """A GET fills the process cache; a Refresh pressed inside the 30 s refresh window must still collect cis_db and
    crm_ops (the cache bounds the file read only), and the cached state must not carry the live answer."""
    class Conn:
        async def fetchval(self, sql, source):
            return None

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_inner):
                    return Conn()

                async def __aexit__(self_inner, *a):
                    return False
            return Ctx()
    compliance.reset_cache()
    db = AsyncMock(return_value=[])
    crm = AsyncMock(return_value=([], {"http": 401}))
    with patch.object(compliance, "_read_published", lambda factory: None), \
            patch.object(compliance_db, "collect", db), patch.object(compliance_crm, "collect", crm), \
            patch.object(compliance.settings, "aget", AsyncMock(return_value=30)):
        page = asyncio.run(compliance.sync(Pool(), now=NOW))
        refreshed = asyncio.run(compliance.sync(Pool(), refresh=True, now=NOW, collect_live=True))
        again = asyncio.run(compliance.sync(Pool(), now=NOW))
    compliance.reset_cache()
    assert page["state"] == "missing" and "live" not in page
    assert set(refreshed["live"]) == {"cis_db", "crm_ops"} and db.await_count == 1 and crm.await_count == 1
    assert refreshed["live"]["crm_ops"] == {"state": "skipped", "http": 401}
    assert "live" not in again and again is page


# ── the Obligations answer ──────────────────────────────────────────────────────────────────────────────────────────


def test_build_obligations():
    data = {
        "frameworks": [{"framework": "iso27001:2022", "kind": "standard", "title": "ISO", "scored": True,
                        "active": True},
                       {"framework": "ex-law:2025", "kind": "law", "title": "Example law", "jurisdiction": "VN",
                        "citation": "Example 1/2025", "source_url": "https://vbpl.vn/x", "in_force_on":
                        date(2025, 7, 1), "applies_from": date(2026, 1, 1), "scored": False, "active": True}],
        "requirements": [
            {"framework": "ex-law:2025", "ref": "Art.20", "section": "Law", "title": "File the transfer dossier",
             "sort_order": 1, "applicable": True, "counsel_review": True, "applies_as": "both",
             "source_url": "https://vbpl.vn/x", "doc_url": "https://github.com/ExampleOrg/r/blob/master/d.md"},
            {"framework": "ex-law:2025", "ref": "Art.33", "section": "Law", "title": "Name a data protection lead",
             "sort_order": 2, "applicable": True, "counsel_review": False, "applies_as": "controller",
             "source_url": None, "doc_url": None}],
        "controls": [{"control_id": "EX-PRV-04", "checks": [{"check_id": "legal_transfer_dossier_vn"}],
                      "active": True},
                     {"control_id": "EX-PRV-11", "checks": [], "active": True}],
        "mappings": [{"control_id": "EX-PRV-04", "framework": "ex-law:2025", "ref": "Art.20", "coverage": "full"},
                     {"control_id": "EX-PRV-11", "framework": "ex-law:2025", "ref": "Art.33", "coverage": "full"}],
        "latest": {"legal_transfer_dossier_vn": ev("legal_transfer_dossier_vn", {"overdue": 0,
                                                                                 "doc_status": "draft"})},
    }
    result = cs.evaluate(data["requirements"], data["controls"], data["mappings"], data["latest"],
                         {"root_daily": NOW}, settings(), NOW, scored=["iso27001:2022"])
    matrix = [{"market": "VN", "segment": "staff", "framework": "ex-law:2025", "test": "Processes data in Vietnam",
               "threshold": None, "verdict": "applies", "source_url": "https://vbpl.vn/x", "source_title": "Law",
               "assessed_on": date(2026, 1, 2), "counsel_review": True, "note": None}]
    out = compliance_obligations.build(data, result, matrix, 180, NOW)
    items = {i["ref"]: i for i in out["items"]}
    assert items["Art.20"]["state"] == "met" and items["Art.20"]["evidence"][0]["doc_status"] == "draft"
    assert items["Art.33"]["state"] == "not_checked" and items["Art.33"]["evidence"] == []
    assert [law["framework"] for law in out["laws"]] == ["ex-law:2025"]
    assert out["laws"][0]["counts"]["met"] == 1 and out["laws"][0]["verdicts"]["applies"] == 1
    assert out["applicability"][0]["review_due"] is True and out["applicability"][0]["assessed_on"] == "2026-01-02"
    assert (out["laws"][0]["in_force_on"], out["laws"][0]["applies_from"]) == ("2025-07-01", "2026-01-01")
    # the root collector fails a legal check whose page is still a draft: the obligation is not met on a draft
    draft = dict(data, latest={"legal_transfer_dossier_vn": ev("legal_transfer_dossier_vn", {
        "overdue": 0, "drafts": 1, "doc_status": "draft"}, status="fail")})
    res2 = cs.evaluate(draft["requirements"], draft["controls"], draft["mappings"], draft["latest"],
                       {"root_daily": NOW}, settings(), NOW, scored=["iso27001:2022"])
    items2 = {i["ref"]: i for i in compliance_obligations.build(draft, res2, matrix, 180, NOW)["items"]}
    assert items2["Art.20"]["state"] == "not_met" and items2["Art.20"]["evidence"][0]["state"] == "fail"


# ── routes ──────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(main.app.state, "pool", object(), create=True):
        yield TestClient(main.app)


def test_obligations_policy_and_non_admin_403(client):
    assert staff_authz.POLICY[("GET", BASE + "/obligations")] == "compliance.read"
    with patch.object(compliance_obligations, "obligations", AsyncMock()) as ob:
        for role in ("viewer", "content", "finance", "ai_ops"):
            assert client.get(BASE + "/obligations",
                              headers={"X-Admin-Secret": SECRET, "x-admin-role": role}).status_code == 403
    assert ob.await_count == 0


def test_obligations_route_syncs_first_and_answers(client):
    with patch.object(compliance, "sync", AsyncMock(return_value={"state": "ok"})) as sync, \
            patch.object(compliance_obligations, "obligations", AsyncMock(return_value={"laws": [], "items": []})):
        r = client.get(BASE + "/obligations", headers=ADMIN)
    assert r.status_code == 200 and r.json()["ingest"] == {"state": "ok"} and sync.await_count == 1
    assert sync.await_args.kwargs.get("collect_live", False) is False      # a GET never collects the live sources
    assert r.headers["cache-control"] == "private, no-store"


def test_view_as_get_writes_nothing():
    from api.routers import admin_platform_compliance as router

    class State:
        staff_view_as = {"session": "x"}

    class Req:
        state = State()
        app = main.app
    with patch.object(main.app.state, "pool", object(), create=True), \
            patch.object(compliance, "sync", AsyncMock()) as sync, \
            patch.object(compliance, "overview", AsyncMock(return_value={})) as ov:
        asyncio.run(router.get_overview(Req()))
    assert sync.await_count == 0 and ov.await_args.kwargs["write"] is False


def test_heartbeat_applies_to_scheduled_sources_only():
    s = cs.ScoreSettings(validity_hours={"root_daily": 72, "cis_db": 72}, heartbeat_hours=36, partial_weight=0.5,
                         heartbeat_sources=frozenset({"root_daily"}))
    old = ev("retention_live_enabled", {"live_last_date": "2026-11-22"}, source="cis_db", hours_ago=48)
    assert cs.check_state("retention_live_enabled", old, {"cis_db": old["collected_at"]}, s, NOW).state == "pass"
    stale = ev("retention_live_enabled", {"live_last_date": "2026-11-22"}, source="cis_db", hours_ago=80)
    assert cs.check_state("retention_live_enabled", stale, {"cis_db": stale["collected_at"]}, s, NOW).state == "stale"
    root = ev("legal_ropa_reviewed", {"overdue": 0}, hours_ago=48)
    assert cs.check_state("legal_ropa_reviewed", root, {"root_daily": root["collected_at"]}, s, NOW).state == "error"
