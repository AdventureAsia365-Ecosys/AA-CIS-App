"""AA-1184 — the Calendar tab of Platform › Compliance readiness (api/services/compliance_calendar.py, GET
/admin/platform/compliance/calendar) without a database: the row format the root collector publishes, the shaped
answer (order, watched lines, the never-published state), the policy (compliance.read, administrator only) and the
proof that a deliberately past-due calendar line turns the check red on the page. Rows and titles are invented."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz
from api.routers import admin
from api.services import compliance_calendar as cc
from api.services import compliance_score as cs

SECRET = "the-real-secret"
BASE = "/admin/platform/compliance"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
NOW = datetime(2026, 12, 2, 4, 0, tzinfo=timezone.utc)
SETTINGS = cs.ScoreSettings(validity_hours={"root_daily": 36}, heartbeat_hours=48, partial_weight=0.5)

ROWS = [
    "OC-101|D|ok|2026-12-20||TL|Renew the widget licence",
    "OC-102|R|overdue|2026-12-01|2026-11-24|AG|Weekly widget review|||Do it this week",
    "OC-103|R|watched|||TL|Monthly gadget review|gadget_review_recent",
    "OC-104|D|open|||BO|Sign the gadget appendix|||open since 2026-11-26",
    "OC-105|A|automated|||AG|Nightly widget probe|widget_heartbeat",
    "OC-106|X|ok|||TL|Unknown kind is dropped",
    "not a row",
]


def evidence_row(status: str = "fail", rows=None, **detail_over) -> dict:
    detail = {"calendar_updated": "2026-11-28", "today": "2026-12-02", "lines": 5, "overdue": 1, "due_soon": 0,
              "open": 1, "counts": {"ok": 1, "overdue": 1, "watched": 1, "open": 1, "automated": 1, "bogus": 3},
              "rows": ROWS if rows is None else rows, "drill": False}
    detail.update(detail_over)
    return {"check_id": cc.CHECK_ID, "source": "root_daily", "status": status, "summary": "1 overdue: OC-102",
            "collected_at": NOW - timedelta(hours=2), "valid_until": None, "detail": detail,
            "link": "https://github.com/ExampleOrg/Root/blob/master/docs/ops/calendar.md",
            "object_key": "evidence/root_daily/bundles/x.json"}


def test_parse_row_round_trip_and_rejects():
    assert cc.parse_row("OC-102|R|overdue|2026-12-01|2026-11-24|AG|Weekly widget review|||Do it") == {
        "id": "OC-102", "kind": "recurring", "state": "overdue", "due": "2026-12-01", "last": "2026-11-24",
        "owner": "agent", "title": "Weekly widget review", "check": None, "extra": None, "note": "Do it"}
    assert cc.parse_row("OC-1|D|ok||||") is None                       # no owner code, no title
    assert cc.parse_row("OC-1|D|late|||TL|x") is None                  # unknown state
    assert cc.parse_row("OC-1|D|ok|not-a-date||TL|x")["due"] is None  # a bad date is dropped, the line kept
    assert cc.parse_row(None) is None and cc.parse_row(["OC-1"]) is None


def test_shape_orders_worst_first_and_adds_the_watched_checks_state():
    states = {"gadget_review_recent": cs.CheckState("gadget_review_recent", "stale", "old")}
    check = cs.CheckState(cc.CHECK_ID, "fail", "1 overdue: OC-102", "root_daily", NOW, NOW + timedelta(hours=34))
    body = cc.shape(evidence_row(), check, states, NOW)
    assert body["available"] is True
    assert [ln["id"] for ln in body["lines"]] == ["OC-102", "OC-104", "OC-101", "OC-103", "OC-105"]
    watched = next(ln for ln in body["lines"] if ln["id"] == "OC-103")
    assert watched["check_state"] == "stale"
    probe = next(ln for ln in body["lines"] if ln["id"] == "OC-105")
    assert probe["check_state"] == "missing"                           # no evidence for that check yet
    assert body["check"]["state"] == "fail" and body["check"]["sealed"] is True
    assert body["calendar"]["overdue"] == 1 and body["calendar"]["attention"] == 2
    assert body["calendar"]["counts"] == {"ok": 1, "overdue": 1, "watched": 1, "open": 1, "automated": 1}
    assert body["calendar"]["today"] == "2026-12-02"


def test_never_published_is_an_empty_state_not_an_error():
    body = cc.shape(None, None, {}, NOW)
    assert body["available"] is False and body["lines"] == [] and body["check"] is None


def test_a_deliberately_past_due_line_turns_the_check_red_and_its_removal_green():
    red = evidence_row("fail")
    state = cs.check_state(cc.CHECK_ID, red, {"root_daily": NOW - timedelta(hours=2)}, SETTINGS, NOW)
    assert state.state == "fail"                                       # red on Overview > attention
    result = cs.evaluate([], [], [], {cc.CHECK_ID: red}, {"root_daily": NOW}, SETTINGS, NOW, scored=())
    assert result["checks"][cc.CHECK_ID].state == "fail"
    green = evidence_row("pass", rows=[r for r in ROWS if not r.startswith("OC-102")], overdue=0)
    state = cs.check_state(cc.CHECK_ID, green, {"root_daily": NOW}, SETTINGS, NOW)
    assert state.state == "pass"
    body = cc.shape(green, state, {}, NOW)
    assert body["calendar"]["overdue"] == 0 and body["check"]["state"] == "pass"


def test_a_silent_collector_shows_the_check_stale_with_the_old_lines():
    old = evidence_row("pass")
    old["collected_at"] = NOW - timedelta(hours=40)
    state = cs.check_state(cc.CHECK_ID, old, {"root_daily": NOW - timedelta(hours=40)}, SETTINGS, NOW)
    assert state.state == "stale"
    assert cc.shape(old, state, {}, NOW)["check"]["state"] == "stale"


# ── route and policy ────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(main.app.state, "pool", object(), create=True):
        yield TestClient(main.app)


def test_policy_is_compliance_read():
    assert staff_authz.POLICY[("GET", BASE + "/calendar")] == staff_authz.COMPLIANCE_READ


def test_non_admin_gets_403_before_any_read(client):
    with patch.object(cc, "calendar", AsyncMock()) as cal:
        for role in ("viewer", "content", "finance", "ai_ops", "tenant"):
            assert client.get(BASE + "/calendar", headers={"X-Admin-Secret": SECRET, "x-admin-role": role}
                              ).status_code == 403, role
    assert cal.await_count == 0


def test_route_answers_with_the_service_and_the_ingest_state(client):
    state = {"state": "ok"}
    body = cc.shape(evidence_row(), None, {}, NOW)
    from api.services import compliance
    with patch.object(compliance, "sync", AsyncMock(return_value=state)), \
            patch.object(cc, "calendar", AsyncMock(return_value=body)):
        res = client.get(BASE + "/calendar", headers=ADMIN)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "private, no-store"
    out = res.json()
    assert out["ingest"] == state and out["lines"][0]["id"] == "OC-102"
