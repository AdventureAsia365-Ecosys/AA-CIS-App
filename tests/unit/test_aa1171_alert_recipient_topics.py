"""AA-1171 (S287 decision D5) — alert recipients on two topics: the alerts topic (every alert) and the page topic
(page-severity alerts only, PAGE_TOPIC_ARN). Through the real app with the AA-913 fakes: a recipient chooses its
topic, one address may be on both topics, remove / resend act on the row's own topic, reconcile runs per topic, the
bootstrap is refused on both, the CIS ops e-mails go to the alerts recipients only, and without PAGE_TOPIC_ARN the
page choice answers 409. Invented addresses only."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from api.core import identity_config, staff_identity
from api.main import app
from api.routers import admin, admin_alert_recipients, admin_settings
from shared import alert_recipients as ar
from tests.unit.aa854_cognito_staff import ISS, install_issuer
from tests.unit.aa913_fakes import BOOTSTRAP, PAGE_TOPIC_ARN, TOPIC_ARN, AlertDB, MultiSns, StubSns, sub_arn

SECRET = "aa1171-test-admin-secret"
ADMIN_SUB = "aaaaaaaa-0000-4000-8000-000000117101"
BASE = "/admin/platform/alert-recipients"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALERTS_TOPIC_ARN", TOPIC_ARN)
    monkeypatch.setenv("PAGE_TOPIC_ARN", PAGE_TOPIC_ARN)
    monkeypatch.setenv("ALERT_BOOTSTRAP_EMAIL", BOOTSTRAP)
    monkeypatch.setenv("ALERT_EMAIL_TO", BOOTSTRAP)
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    ar.clear_cache()
    yield
    ar.clear_cache()


@pytest.fixture
def token():
    iss, _ = install_issuer()
    yield iss.token(sub=ADMIN_SUB, email="lead@example.invalid", **{"cognito:groups": ["cis-admin"]})
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()


@pytest.fixture
def db(monkeypatch):
    d = AlertDB()
    d.admin_id = d.add(role="admin", email="lead@example.invalid", iss=ISS, sub=ADMIN_SUB, username="lead")
    monkeypatch.setattr(app.state, "pool", d, raising=False)
    return d


@pytest.fixture
def stubs():
    alerts = StubSns(subs={BOOTSTRAP: sub_arn("boot")})
    page = StubSns(topic_arn=PAGE_TOPIC_ARN)
    multi = MultiSns(alerts, page)
    app.dependency_overrides[admin_alert_recipients.get_sns] = lambda: multi
    yield alerts, page
    app.dependency_overrides.pop(admin_alert_recipients.get_sns, None)


def _h(token):
    return {"X-Admin-Secret": SECRET, "x-admin-role": "admin", "Authorization": f"Bearer {token}"}


def test_page_lists_both_topics_and_each_row_names_its_topic(db, stubs, token):
    client = TestClient(app)
    r = client.post(BASE, headers=_h(token), json={"email": "pager@example.invalid", "topic": "page"})
    assert r.status_code == 201, r.text
    assert r.json()["recipient"]["topic"] == "page"
    alerts, page = stubs
    assert page.subscribed == ["pager@example.invalid"] and alerts.subscribed == []
    added = next(a for a in db.audit if a["action"] == "alert_recipient.added")
    assert added["details"]["topic"] == "aa-ecosys-dev-page-alerts"
    body = client.get(BASE, headers=_h(token)).json()
    assert body["topics"] == ["alerts", "page"] and body["page_topic_name"] == "aa-ecosys-dev-page-alerts"
    assert [(x["email"], x["topic"]) for x in body["recipients"]] == [("pager@example.invalid", "page")]
    assert "arn:" not in r.text and "arn:" not in client.get(BASE, headers=_h(token)).text


def test_one_address_may_be_on_both_topics_but_not_twice_on_one(db, stubs, token):
    client = TestClient(app)
    assert client.post(BASE, headers=_h(token), json={"email": "both@example.invalid"}).status_code == 201
    assert client.post(BASE, headers=_h(token), json={"email": "both@example.invalid", "topic": "page"}).status_code \
        == 201
    dup = client.post(BASE, headers=_h(token), json={"email": "both@example.invalid", "topic": "page"})
    assert dup.status_code == 409 and dup.json()["code"] == "duplicate"
    assert sorted(r["topic"] for r in db.recipients) == ["alerts", "page"]


def test_bootstrap_refused_on_the_page_topic_too(db, stubs, token):
    r = TestClient(app).post(BASE, headers=_h(token), json={"email": BOOTSTRAP, "topic": "page"})
    assert r.status_code == 409 and r.json()["code"] == "bootstrap"


def test_unknown_topic_is_422_and_missing_page_topic_is_409(db, stubs, token, monkeypatch):
    client = TestClient(app)
    assert client.post(BASE, headers=_h(token), json={"email": "x@example.invalid", "topic": "sms"}).status_code == 422
    monkeypatch.delenv("PAGE_TOPIC_ARN")
    r = client.post(BASE, headers=_h(token), json={"email": "x@example.invalid", "topic": "page"})
    assert r.status_code == 409 and r.json()["code"] == "page_not_configured"
    assert client.get(BASE, headers=_h(token)).json()["topics"] == ["alerts"]


def test_remove_unsubscribes_on_the_rows_topic(db, stubs, token):
    client = TestClient(app)
    alerts, page = stubs
    rid = client.post(BASE, headers=_h(token), json={"email": "p2@example.invalid", "topic": "page"}).json()[
        "recipient"]["id"]
    page.confirm("p2@example.invalid")
    r = client.delete(f"{BASE}/{rid}", headers=_h(token))
    assert r.status_code == 200 and r.json()["sns"] == "unsubscribed"
    assert page.unsubscribed and not alerts.unsubscribed


def test_reconcile_runs_per_topic_and_ops_mail_goes_to_alerts_only(db, stubs):
    alerts, page = stubs
    db.insert("a@example.invalid", "pending", topic="alerts")
    db.insert("p@example.invalid", "pending", topic="page")
    alerts.subs["a@example.invalid"] = sub_arn("a")
    page.subs["p@example.invalid"] = f"{PAGE_TOPIC_ARN}:p-confirmed"
    page.subs["stray@example.invalid"] = f"{PAGE_TOPIC_ARN}:stray"
    result = asyncio.run(ar.reconcile(db, MultiSns(alerts, page), ar.topic_config()))
    assert {(r["email"], r["status"]) for r in db.recipients} == {("a@example.invalid", "confirmed"),
                                                                 ("p@example.invalid", "confirmed")}
    assert result.unmanaged == [{"email": "stray@example.invalid", "state": "confirmed", "topic": "page"}]
    assert result.locked and result.locked["email"] == BOOTSTRAP
    recipients, _ = asyncio.run(ar.ops_recipients(db))
    assert recipients == ["a@example.invalid", BOOTSTRAP]
