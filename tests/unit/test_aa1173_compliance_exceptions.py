"""AA-1173 — exceptions and monthly reviews of Platform › Compliance without a database: the pure rules (effective
state, expiry window from the setting, the review summary), the route policy (compliance.read / compliance.write,
administrator only), and the write routes' sign-in rule (a verified company sign-in holding compliance.write; a
password, secret-only, read-only E2E or view-as session gets 403 before any database call). The database paths
(maker-checker, triggers, one review a month) are in tests/integration/test_aa1173_compliance_exceptions.py."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz
from api.routers import admin
from api.routers import admin_platform_compliance as router
from api.services import compliance, compliance_exceptions as ce

SECRET = "the-real-secret"
BASE = "/admin/platform/compliance"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
NOW = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
COGNITO = SimpleNamespace(auth="cognito", admin_id="00000000-0000-4000-8000-0000000000aa", role="admin",
                          e2e_read_only=False)
BODY = {"subject_kind": "control", "subject_ref": "EX-IAM-01", "title": "Invented exception",
        "reason": "An invented reason that is long enough to pass.", "risk_level": "low", "expires_on": "2026-12-31"}


@pytest.fixture
def client():
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(main.app.state, "pool", object(), create=True):
        yield TestClient(main.app)


def test_route_policy_is_compliance_read_and_write():
    P = staff_authz.POLICY
    for p in ("/exceptions", "/reviews"):
        assert P[("GET", BASE + p)] == staff_authz.COMPLIANCE_READ
    for p in ("/exceptions", "/exceptions/{exception_id}/approve", "/exceptions/{exception_id}/reject",
              "/exceptions/{exception_id}/revoke", "/reviews"):
        assert P[("POST", BASE + p)] == staff_authz.COMPLIANCE_WRITE


def test_non_admin_gets_403(client):
    for role in ("viewer", "content", "finance", "tenant"):
        h = {"X-Admin-Secret": SECRET, "x-admin-role": role}
        assert client.get(BASE + "/exceptions", headers=h).status_code == 403, role
        assert client.post(BASE + "/exceptions", headers=h, json=BODY).status_code == 403, role


def test_writes_need_a_verified_company_sign_in(client):
    with patch.object(ce, "propose", AsyncMock()) as propose:
        res = client.post(BASE + "/exceptions", headers=ADMIN, json=BODY)   # secret-only admin
        assert res.status_code == 403 and "Cognito" in res.json()["detail"] and not propose.await_count
        for ident in (SimpleNamespace(**{**COGNITO.__dict__, "e2e_read_only": True}),
                      SimpleNamespace(**{**COGNITO.__dict__, "role": "viewer"})):
            with patch.object(router, "request_staff_identity", lambda r, i=ident: i):
                assert client.post(BASE + "/exceptions", headers=ADMIN, json=BODY).status_code == 403
        assert not propose.await_count


def test_a_verified_admin_proposes_and_decides(client):
    row = {"exception_id": 1, "state": "proposed"}
    with patch.object(router, "request_staff_identity", lambda r: COGNITO), \
            patch.object(ce, "propose", AsyncMock(return_value=row)) as propose, \
            patch.object(ce, "decide", AsyncMock(return_value={**row, "state": "approved"})) as decide, \
            patch.object(ce, "revoke", AsyncMock(side_effect=ce.ComplianceRefused(409, "x", "not_revocable"))):
        assert client.post(BASE + "/exceptions", headers=ADMIN, json=BODY).json() == row
        assert propose.await_args.args[2] == f"admin:{COGNITO.admin_id}"
        assert client.post(BASE + "/exceptions/1/approve", headers=ADMIN, json={}).json()["state"] == "approved"
        assert decide.await_args.kwargs["approve"] is True
        res = client.post(BASE + "/exceptions/1/revoke", headers=ADMIN, json={"note": "Fixed now"})
        assert res.status_code == 409 and res.json()["detail"]["code"] == "not_revocable"
    # extra fields are refused by the body model
    with patch.object(router, "request_staff_identity", lambda r: COGNITO):
        assert client.post(BASE + "/exceptions", headers=ADMIN, json={**BODY, "status": "approved"}).status_code == 422


def test_list_carries_the_viewer(client):
    with patch.object(router, "request_staff_identity", lambda r: COGNITO), \
            patch.object(ce, "list_exceptions", AsyncMock(return_value={"exceptions": []})):
        assert client.get(BASE + "/exceptions", headers=ADMIN).json()["viewer"] == f"admin:{COGNITO.admin_id}"
    with patch.object(ce, "list_exceptions", AsyncMock(return_value={"exceptions": []})):
        assert client.get(BASE + "/exceptions", headers=ADMIN).json()["viewer"] is None


def test_review_route_records_the_page_numbers(client):
    overview = {"generated_at": "x", "frameworks": [{"framework": "iso27001:2022", "strict_pct": 2.4,
                                                     "weighted_pct": 9.3, "n_met": 1, "n_gap": 5, "n_stale": 0}],
                "controls": {"met": 8}, "checks": {"fail": 1}, "attention": [{"check_id": "ex_check"}],
                "heartbeat": [{"source": "aws_nightly", "scheduled": True, "ok": False}]}
    with patch.object(router, "request_staff_identity", lambda r: COGNITO), \
            patch.object(compliance, "overview", AsyncMock(return_value=overview)), \
            patch.object(ce, "list_exceptions", AsyncMock(return_value={"exceptions": [], "counts": {"approved": 0}})), \
            patch.object(ce, "record_review", AsyncMock(return_value={"review_id": 1})) as rec:
        res = client.post(BASE + "/reviews", headers=ADMIN, json={"notes": "Twenty characters or more here."})
    assert res.status_code == 200
    summary = rec.await_args.args[3]
    assert summary["frameworks"][0]["weighted_pct"] == 9.3 and summary["collectors_down"] == ["aws_nightly"]
    assert summary["attention"] == ["ex_check"]


def test_effective_state_and_expiry_window():
    assert ce.effective_status({"status": "approved", "expires_on": date(2026, 11, 30)}, TODAY) == "expired"
    assert ce.effective_status({"status": "approved", "expires_on": TODAY}, TODAY) == "approved"
    assert ce._validate_expiry("2026-12-31", TODAY, 90) == date(2026, 12, 31)
    for bad, code in (("2026-12-01", "expires_on_past"), ("2027-04-01", "expires_on_too_far"),
                      ("not a date", "expires_on_invalid")):
        with pytest.raises(ce.ComplianceRefused) as e:
            ce._validate_expiry(bad, TODAY, 90)
        assert e.value.code == code and e.value.status == 422


def test_propose_validates_before_the_database():
    with patch.object(ce, "max_days", AsyncMock(return_value=90)):
        for over, code in (({"subject_kind": "server"}, "subject_kind"), ({"subject_ref": "bad ref!"}, "subject_ref"),
                           ({"risk_level": "huge"}, "risk_level"), ({"reason": "short"}, "reason_length")):
            with pytest.raises(ce.ComplianceRefused) as e:
                asyncio.run(ce.propose(object(), {**BODY, **over}, "admin:x", now=NOW))
            assert e.value.code == code


def test_review_period_rules():
    with pytest.raises(ce.ComplianceRefused) as e:
        asyncio.run(ce.record_review(object(), {"notes": "x" * 25, "period": "2026-09-01"}, "admin:x", {}, now=NOW))
    assert e.value.code == "period_out_of_range"
    with pytest.raises(ce.ComplianceRefused) as e:
        asyncio.run(ce.record_review(object(), {"notes": "short"}, "admin:x", {}, now=NOW))
    assert e.value.code == "notes_length"
