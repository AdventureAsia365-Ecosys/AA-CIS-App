"""AA-1170 — the Evidenced % (api/services/compliance_score.py): check, control, requirement and framework states, the
validity window read at score time (a shorter setting turns fresh evidence stale and lowers the score), the collector
heartbeat, the facts graded against the settings, strict and weighted scores. Invented controls (EX- prefix) and
check ids; no DB."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from api.services import compliance_score as cs

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
S = cs.ScoreSettings(validity_hours={"root_daily": 72}, heartbeat_hours=36, partial_weight=0.5,
                     fact_limits={"last_restore_drill": ("last_date", 90.0, "age_days"),
                                  "docs_review_overdue": ("overdue", 0.0, "max"),
                                  "ci_image_scan_sbom": ("last_date", 8.0, "age_days")})


def ev(status="pass", hours_ago=2, source="root_daily", **over):
    row = {"source": source, "status": status, "collected_at": NOW - timedelta(hours=hours_ago), "valid_until": None,
           "summary": "invented", "detail": {}}
    row.update(over)
    return row


def seen(hours_ago=2):
    return {"root_daily": NOW - timedelta(hours=hours_ago)}


def test_check_state_basics():
    assert cs.check_state("a", None, {}, S, NOW).state == "missing"
    assert cs.check_state("a", ev(), seen(), S, NOW).state == "pass"
    assert cs.check_state("a", ev("fail"), seen(), S, NOW).state == "fail"
    assert cs.check_state("a", ev("manual"), seen(), S, NOW).state == "manual"
    st = cs.check_state("a", ev(), seen(), S, NOW)
    assert st.expires_at == NOW - timedelta(hours=2) + timedelta(hours=72)


def test_shorter_validity_setting_turns_evidence_stale_at_read_time():
    row = ev(hours_ago=10)
    assert cs.check_state("a", row, seen(1), S, NOW).state == "pass"
    short = cs.ScoreSettings(validity_hours={"root_daily": 6}, heartbeat_hours=36, partial_weight=0.5)
    assert cs.check_state("a", row, seen(1), short, NOW).state == "stale"
    # the row's own valid_until also ends it, whichever comes first
    assert cs.check_state("a", ev(hours_ago=10, valid_until=NOW - timedelta(hours=1)), seen(1), S, NOW).state == "stale"


def test_silent_collector_turns_its_checks_to_error():
    st = cs.check_state("a", ev(hours_ago=40), seen(40), S, NOW)
    assert st.state == "error" and "silent" in st.reason


def test_unknown_source_without_valid_until_is_an_error():
    assert cs.check_state("a", ev(source="other_src"), {"other_src": NOW}, S, NOW).state == "error"
    row = ev(source="other_src", valid_until=NOW + timedelta(hours=5))
    assert cs.check_state("a", row, {"other_src": NOW}, S, NOW).state == "pass"


@pytest.mark.parametrize("fact,expected", [("2026-11-01", "pass"), ("2026-06-01", "fail"), ("not a date", "error"),
                                           (None, "error")])
def test_date_fact_is_graded_against_the_setting(fact, expected):
    detail = {"last_date": fact} if fact is not None else {}
    assert cs.check_state("last_restore_drill", ev(detail=detail), seen(), S, NOW).state == expected


def test_count_fact_and_a_collector_error_wins():
    assert cs.check_state("docs_review_overdue", ev(detail={"overdue": 0}), seen(), S, NOW).state == "pass"
    assert cs.check_state("docs_review_overdue", ev(detail={"overdue": 3}), seen(), S, NOW).state == "fail"
    assert cs.check_state("docs_review_overdue", ev("error"), seen(), S, NOW).state == "error"


def _checks(**states):
    return {k: cs.CheckState(k, v, "") for k, v in states.items()}


def test_control_state():
    ctl = {"control_id": "EX-A-01", "checks": [{"check_id": "a", "required": True}, {"check_id": "b"},
                                               {"check_id": "c", "required": False}]}
    assert cs.control_state(ctl, _checks(a="pass", b="pass", c="fail")) == "met"
    assert cs.control_state(ctl, _checks(a="pass", b="fail")) == "partial"
    assert cs.control_state(ctl, _checks(a="stale", b="fail")) == "stale"
    assert cs.control_state(ctl, _checks(a="fail")) == "gap"
    assert cs.control_state({"control_id": "EX-A-02", "checks": []}, {}) == "no_evidence"


def test_requirement_state():
    full = [{"control_id": "EX-A-01", "coverage": "full"}]
    part = [{"control_id": "EX-A-01", "coverage": "partial"}]
    assert cs.requirement_state([], {"EX-A-01": "met"}) == "unmapped"
    assert cs.requirement_state(full, {}) == "unmapped"   # an inactive control covers nothing
    assert cs.requirement_state(full, {"EX-A-01": "met"}) == "met"
    assert cs.requirement_state(part, {"EX-A-01": "met"}) == "partial"
    assert cs.requirement_state(full, {"EX-A-01": "partial"}) == "partial"
    assert cs.requirement_state(full, {"EX-A-01": "stale"}) == "stale"
    assert cs.requirement_state(full, {"EX-A-01": "no_evidence"}) == "gap"


def test_framework_score_strict_and_weighted():
    sc = cs.framework_score(["met", "partial", "gap", "unmapped"], 0.5)
    assert sc == {"n_requirements": 4, "n_met": 1, "n_partial": 1, "n_gap": 1, "n_stale": 0, "n_unmapped": 1,
                  "strict_pct": 25.0, "weighted_pct": 37.5}
    assert cs.framework_score([], 0.5)["strict_pct"] == 0.0


def test_evaluate_end_to_end_and_stale_lowers_the_score():
    reqs = [{"framework": "iso27001:2022", "ref": "A.5.1", "applicable": True},
            {"framework": "iso27001:2022", "ref": "A.5.2", "applicable": True},
            {"framework": "iso27001:2022", "ref": "A.5.3", "applicable": False}]
    ctls = [{"control_id": "EX-GOV-01", "checks": [{"check_id": "docs_review_overdue"}], "active": True},
            {"control_id": "EX-GOV-09", "checks": [{"check_id": "x_check"}], "active": False}]
    maps = [{"control_id": "EX-GOV-01", "framework": "iso27001:2022", "ref": "A.5.1", "coverage": "full"},
            {"control_id": "EX-GOV-09", "framework": "iso27001:2022", "ref": "A.5.2", "coverage": "full"}]
    latest = {"docs_review_overdue": ev(hours_ago=10, detail={"overdue": 0})}
    out = cs.evaluate(reqs, ctls, maps, latest, seen(1), S, NOW)
    iso = out["frameworks"]["iso27001:2022"]
    assert (iso["n_requirements"], iso["n_met"], iso["n_unmapped"], iso["strict_pct"]) == (2, 1, 1, 50.0)
    assert set(out["frameworks"]) == set(cs.FRAMEWORKS)
    short = cs.ScoreSettings(validity_hours={"root_daily": 4}, heartbeat_hours=36, partial_weight=0.5,
                             fact_limits=S.fact_limits)
    iso = cs.evaluate(reqs, ctls, maps, latest, seen(1), short, NOW)["frameworks"]["iso27001:2022"]
    assert (iso["n_met"], iso["n_stale"], iso["strict_pct"]) == (0, 1, 0.0)


def test_an_old_image_scan_fails_on_its_date_even_when_the_collector_said_pass():
    # the collector reads the newest CI run that scanned; if CI stopped scanning, that run is old but still "pass"
    fresh = ev(detail={"last_date": "2026-11-20", "run_dates": {"AppA": "2026-11-20"}})
    assert cs.check_state("ci_image_scan_sbom", fresh, seen(), S, NOW).state == "pass"
    old = ev(detail={"last_date": "2026-11-06"})
    st = cs.check_state("ci_image_scan_sbom", old, seen(), S, NOW)
    assert st.state == "fail" and "15 days old" in st.reason
    assert cs.check_state("ci_image_scan_sbom", ev(detail={"ok": 3}), seen(), S, NOW).state == "error"
    assert cs.check_state("ci_image_scan_sbom", ev("fail", detail={}), seen(), S, NOW).state == "fail"


def test_settings_changed_on():
    rows = [{"snapshot_date": date(2026, 11, 1), "settings": {"w": 0.5}},
            {"snapshot_date": date(2026, 11, 3), "settings": {"w": 0.4}},
            {"snapshot_date": date(2026, 11, 2), "settings": {"w": 0.5}},
            {"snapshot_date": date(2026, 11, 4), "settings": {"w": 0.4}}]
    assert cs.settings_changed_on(rows) == "2026-11-03"
    assert cs.settings_changed_on(rows[:1]) is None
    assert S.as_json()["fact_limits"]["last_restore_drill"] == {"fact": "last_date", "limit": 90.0, "kind": "age_days"}
