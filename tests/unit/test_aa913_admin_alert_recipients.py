"""AA-913 — api/routers/admin_alert_recipients.py through the real app.

Callers: an admin with a Cognito Bearer (staff bearer middleware -> verified identity), a legacy admin session
(secret + x-admin-role, no identity), other roles, the machine. SNS is the stub of tests/unit/aa913_fakes.py
(injected through the router's get_sns dependency); the DB is the in-memory AlertDB.

Covers the authorization rule (ops.read to read; alerts.write + Cognito to change, except the status refresh), every
route's statuses (201/200/202, 404, 409, 422, 429 + Retry-After, 502, 503), the audit rows, that no response carries
an ARN, and the end-to-end flows: add -> confirm -> confirmed; remove -> unsubscribed; a confirmation clicked after
the removal -> unsubscribed again.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from api.core import identity_config, staff_identity
from api.core.staff_authz import ALERTS_WRITE, OPS_READ, POLICY, ROLE_PERMS
from api.main import app
from api.routers import admin, admin_alert_recipients, admin_settings
from shared import alert_recipients as ar
from tests.unit.aa854_cognito_staff import ISS, install_issuer
from tests.unit.aa913_fakes import BOOTSTRAP, TOPIC_ARN, AlertDB, StubSns, sub_arn

SECRET = "aa913-test-admin-secret"
ADMIN_SUB = "aaaaaaaa-0000-4000-8000-0000000009a1"
REVIEWER_SUB = "bbbbbbbb-0000-4000-8000-0000000009b2"
BASE = "/admin/platform/alert-recipients"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALERTS_TOPIC_ARN", TOPIC_ARN)
    monkeypatch.setenv("ALERT_BOOTSTRAP_EMAIL", BOOTSTRAP)
    monkeypatch.setenv("ALERT_EMAIL_TO", BOOTSTRAP)
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@lumiguides.it.com")
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    ar.clear_cache()
    yield
    ar.clear_cache()


@pytest.fixture
def issuer():
    iss, _ = install_issuer()
    yield iss
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()


@pytest.fixture
def db(monkeypatch):
    d = AlertDB()
    d.admin_id = d.add(role="admin", email="nghiep@adventure.asia", iss=ISS, sub=ADMIN_SUB, username="nghiep",
                       display_name="Nghiep Pham")
    d.add(role="reviewer", email="rev@adventure.asia", iss=ISS, sub=REVIEWER_SUB, username="rev")
    monkeypatch.setattr(app.state, "pool", d, raising=False)
    return d


@pytest.fixture
def sns():
    s = StubSns(subs={BOOTSTRAP: sub_arn("boot")})
    app.dependency_overrides[admin_alert_recipients.get_sns] = lambda: s
    yield s
    app.dependency_overrides.pop(admin_alert_recipients.get_sns, None)


@pytest.fixture
def token(issuer):
    return issuer.token(sub=ADMIN_SUB, email="nghiep@adventure.asia", **{"cognito:groups": ["cis-admin"]})


@pytest.fixture
def client(db, sns):
    return TestClient(app)


def _h(token: Optional[str] = None, role: Optional[str] = "admin") -> dict:
    h = {"X-Admin-Secret": SECRET}
    if role is not None:
        h["x-admin-role"] = role
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _add(client, token, email="new@adventure.asia", note=None):
    body = {"email": email}
    if note is not None:
        body["note"] = note
    return client.post(BASE, headers=_h(token), json=body)


# ── policy ───────────────────────────────────────────────────────────────────────────────────────

def test_policy_entries():
    assert POLICY[("GET", BASE)] == OPS_READ
    for key in (("POST", BASE), ("POST", BASE + "/refresh"), ("POST", BASE + "/test-email"),
                ("DELETE", BASE + "/{recipient_id}"), ("POST", BASE + "/{recipient_id}/resend")):
        assert POLICY[key] == ALERTS_WRITE
    assert [r for r, perms in ROLE_PERMS.items() if ALERTS_WRITE in perms] == ["admin"]


# ── read ─────────────────────────────────────────────────────────────────────────────────────────

def test_admin_reads_the_page(client, token, db, sns):
    sns.subs["console@x.com"] = sub_arn("console")
    db.insert("ok@adventure.asia", "confirmed", arn=sub_arn("ok"))
    sns.subs["ok@adventure.asia"] = sub_arn("ok")
    resp = client.get(BASE, headers=_h(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True and body["topic_name"] == "aa-cis-dev-acp-alerts"
    assert body["bootstrap"] == {"email": BOOTSTRAP, "locked": True, "state": "confirmed"}
    assert body["ops_fallback_email"] is None            # same address as the bootstrap
    assert body["unmanaged"] == [{"email": "console@x.com", "state": "confirmed"}]
    assert [r["email"] for r in body["recipients"]] == ["ok@adventure.asia"]
    assert body["recipients"][0]["has_subscription"] is True
    assert body["limits"] == {"max_active": 10, "active": 1, "adds_left_today": 10, "max_note": 80}
    assert body["viewer"] == {"auth": "cognito", "can_write": True}
    assert body["reconcile_error"] is None and body["reconciled_at"]
    assert "arn:" not in resp.text and "111122223333" not in resp.text


def test_legacy_admin_reads_but_may_not_write(client, sns):
    resp = client.get(BASE, headers=_h())
    assert resp.status_code == 200
    assert resp.json()["viewer"] == {"auth": "legacy", "can_write": False}


def test_fallback_mailbox_shown_when_it_differs(client, token, monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@adventure.asia")
    assert client.get(BASE, headers=_h(token)).json()["ops_fallback_email"] == "ops@adventure.asia"


def test_reconcile_error_still_answers_200(client, token, db, sns):
    db.insert("ok@adventure.asia", "confirmed", arn=sub_arn("ok"))
    sns.list_error = "AuthorizationError"
    resp = client.get(BASE + "?refresh=true", headers=_h(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["reconcile_error"] == "AuthorizationError" and len(body["recipients"]) == 1


def test_not_configured(client, token, monkeypatch):
    monkeypatch.delenv("ALERTS_TOPIC_ARN")
    body = client.get(BASE, headers=_h(token)).json()
    assert body["configured"] is False and body["topic_name"] is None
    resp = _add(client, token)
    assert resp.status_code == 503 and resp.json()["code"] == "not_configured"


# ── who may call ─────────────────────────────────────────────────────────────────────────────────

WRITES = [("post", BASE, {"email": "x@adventure.asia"}), ("post", BASE + "/test-email", None),
          ("delete", BASE + "/11111111-1111-1111-1111-111111111111", None),
          ("post", BASE + "/11111111-1111-1111-1111-111111111111/resend", None)]


@pytest.mark.parametrize("method,path,body", WRITES)
def test_legacy_admin_session_cannot_change(client, db, sns, method, path, body):
    resp = getattr(client, method)(path, headers=_h(), **({"json": body} if body else {}))
    assert resp.status_code == 403 and "Cognito" in resp.json()["detail"]
    assert sns.subscribed == [] and sns.list_calls == 0 and db.recipients == []
    assert db.actions() == []


@pytest.mark.parametrize("role", ["reviewer", "content", "viewer", "tenant", ""])
@pytest.mark.parametrize("method,path,body", [("get", BASE, None), *WRITES, ("post", BASE + "/refresh", None)])
def test_other_roles_are_refused(client, sns, role, method, path, body):
    resp = getattr(client, method)(path, headers=_h(role=role), **({"json": body} if body else {}))
    assert resp.status_code == 403
    assert sns.list_calls == 0 and sns.subscribed == []


@pytest.mark.parametrize("method,path,body", [("get", BASE, None), *WRITES])
def test_machine_is_refused(client, method, path, body):
    resp = getattr(client, method)(path, headers=_h(role=None), **({"json": body} if body else {}))
    assert resp.status_code == 403


def test_cognito_reviewer_is_refused(client, issuer, sns):
    tok = issuer.token(sub=REVIEWER_SUB, email="rev@adventure.asia", **{"cognito:groups": ["cis-reviewer"]})
    assert client.post(BASE, headers=_h(tok, role="admin"), json={"email": "x@adventure.asia"}).status_code == 403
    assert sns.subscribed == []


def test_refresh_needs_no_cognito(client, sns):
    resp = client.post(BASE + "/refresh", headers=_h())
    assert resp.status_code == 200 and sns.list_calls == 1


def test_no_secret_is_refused(client):
    assert client.get(BASE, headers={"x-admin-role": "admin"}).status_code == 403


# ── add ──────────────────────────────────────────────────────────────────────────────────────────

def test_add_subscribes_and_audits(client, token, db, sns):
    resp = _add(client, token, "  New.Person@Adventure.Asia ", note="on-call")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["sns_state"] == "pending"
    rec = body["recipient"]
    assert rec["email"] == "new.person@adventure.asia" and rec["status"] == "pending" and rec["note"] == "on-call"
    assert rec["created_by"] == "Nghiep Pham" and rec["resend_available_at"]
    assert sns.subscribed == ["new.person@adventure.asia"] and db.locks == 1
    assert db.actions() == ["alert_recipient.added", "alert_recipient.confirmation_sent"]
    added = next(a for a in db.audit if a["action"] == "alert_recipient.added")
    assert added["actor"] == f"admin:{db.admin_id}" and added["details"]["email"] == "new.person@adventure.asia"
    assert added["details"]["topic"] == "aa-cis-dev-acp-alerts"
    assert "arn:" not in resp.text and "arn:" not in repr(added["details"])
    assert db.recipients[0]["subscription_arn"].startswith(TOPIC_ARN)   # stored, never returned


def test_add_of_an_address_sns_already_has_confirmed(client, token, db, sns):
    sns.subs["known@adventure.asia"] = sub_arn("known")
    resp = _add(client, token, "known@adventure.asia")
    assert resp.status_code == 201 and resp.json()["recipient"]["status"] == "confirmed"
    assert db.actions() == ["alert_recipient.added"]


def test_add_refuses_the_bootstrap(client, token, sns):
    resp = _add(client, token, BOOTSTRAP.upper())
    assert resp.status_code == 409 and resp.json()["code"] == "bootstrap" and sns.subscribed == []


def test_add_refuses_a_duplicate(client, token, db, sns):
    db.insert("dup@adventure.asia", "pending", arn=sub_arn("dup"))
    resp = _add(client, token, "dup@adventure.asia")
    assert resp.status_code == 409 and resp.json()["code"] == "duplicate" and sns.subscribed == []


def test_add_refuses_the_eleventh(client, token, db, sns):
    for i in range(10):
        db.insert(f"r{i}@adventure.asia", "confirmed", arn=sub_arn(f"r{i}"))
        sns.subs[f"r{i}@adventure.asia"] = sub_arn(f"r{i}")
    resp = _add(client, token, "eleven@adventure.asia")
    assert resp.status_code == 409 and resp.json()["code"] == "full" and sns.subscribed == []


@pytest.mark.parametrize("email", ["a@x.com,b@y.com", "nope", "a@b@c.com", "<a@x.com>"])
def test_add_refuses_a_bad_address(client, token, sns, email):
    resp = _add(client, token, email)
    assert resp.status_code == 422 and resp.json()["code"] == "invalid" and sns.subscribed == []


def test_add_refuses_unknown_fields_and_long_notes(client, token):
    assert client.post(BASE, headers=_h(token), json={"email": "a@x.com", "role": "x"}).status_code == 422
    assert _add(client, token, note="n" * 81).status_code == 422


def test_add_sns_failure_keeps_nothing(client, token, db, sns):
    sns.subscribe_error = "AuthorizationError"
    resp = _add(client, token)
    assert resp.status_code == 502
    assert resp.json()["code"] == "AuthorizationError" and "arn:" not in resp.text
    assert db.recipients == [] and db.actions() == []


def test_add_rate_limit_per_day(client, token, db, sns):
    for _ in range(10):
        db.audit_at("alert_recipient.added", ago=timedelta(hours=2))
    resp = _add(client, token)
    assert resp.status_code == 429 and int(resp.headers["Retry-After"]) > 0
    assert sns.subscribed == [] and db.recipients == []
    assert client.get(BASE, headers=_h(token)).json()["limits"]["adds_left_today"] == 0


# ── remove ───────────────────────────────────────────────────────────────────────────────────────

def test_remove_confirmed_unsubscribes_the_stored_arn(client, token, db, sns):
    rid = db.insert("c@adventure.asia", "confirmed", arn=sub_arn("c"))
    sns.subs["c@adventure.asia"] = sub_arn("c")
    resp = client.delete(f"{BASE}/{rid}", headers=_h(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["sns"] == "unsubscribed" and resp.json()["recipient"]["status"] == "removed"
    assert sns.unsubscribed == [sub_arn("c")]
    row = db.row(rid)
    assert (row["removed_reason"], row["removed_by"]) == ("admin", f"admin:{db.admin_id}")
    removed = next(a for a in db.audit if a["action"] == "alert_recipient.removed")
    assert removed["details"] == {"email": "c@adventure.asia", "was": "confirmed", "sns": "unsubscribed",
                                  "topic": "aa-cis-dev-acp-alerts"}


def test_remove_when_sns_no_longer_has_it(client, token, db, sns):
    rid = db.insert("gone@adventure.asia", "confirmed", arn=sub_arn("gone"))
    resp = client.delete(f"{BASE}/{rid}", headers=_h(token))
    assert resp.status_code == 200 and resp.json()["sns"] == "not_found"


def test_remove_pending_that_sns_refuses_to_delete_is_left_to_expire(client, token, db, sns):
    _add(client, token, "p@adventure.asia")
    rid = db.recipients[0]["id"]
    sns.unsubscribe_error = "InvalidParameter"
    resp = client.delete(f"{BASE}/{rid}", headers=_h(token))
    assert resp.status_code == 200 and resp.json()["sns"] == "pending_left_to_expire"
    assert db.row(rid)["status"] == "removed"


def test_remove_other_sns_error_changes_nothing(client, token, db, sns):
    rid = db.insert("c@adventure.asia", "confirmed", arn=sub_arn("c"))
    sns.subs["c@adventure.asia"] = sub_arn("c")
    sns.unsubscribe_error = "Throttled"
    resp = client.delete(f"{BASE}/{rid}", headers=_h(token))
    assert resp.status_code == 502 and resp.json()["code"] == "Throttled"
    assert db.row(rid)["status"] == "confirmed" and db.actions() == []


@pytest.mark.parametrize("rid", ["not-a-uuid", "33333333-3333-3333-3333-333333333333"])
def test_remove_unknown_is_404(client, token, rid):
    assert client.delete(f"{BASE}/{rid}", headers=_h(token)).status_code == 404


def test_remove_twice_is_404(client, token, db, sns):
    rid = db.insert("c@adventure.asia", "removed", reason="admin")
    assert client.delete(f"{BASE}/{rid}", headers=_h(token)).status_code == 404


# ── resend ───────────────────────────────────────────────────────────────────────────────────────

def test_resend_confirmed_is_409(client, token, db):
    rid = db.insert("c@adventure.asia", "confirmed", arn=sub_arn("c"))
    resp = client.post(f"{BASE}/{rid}/resend", headers=_h(token))
    assert resp.status_code == 409 and resp.json()["code"] == "not_pending"


def test_resend_right_after_the_add_is_429(client, token, db, sns):
    _add(client, token, "p@adventure.asia")
    rid = db.recipients[0]["id"]
    resp = client.post(f"{BASE}/{rid}/resend", headers=_h(token))
    assert resp.status_code == 429 and 0 < int(resp.headers["Retry-After"]) <= ar.RESEND_MIN_INTERVAL_S
    assert sns.subscribed == ["p@adventure.asia"]


def test_resend_later_subscribes_again(client, token, db, sns):
    rid = db.insert("p@adventure.asia", "pending", arn=sub_arn("p"))
    db.audit_at("alert_recipient.confirmation_sent", resource_id=rid, ago=timedelta(minutes=30))
    resp = client.post(f"{BASE}/{rid}/resend", headers=_h(token))
    assert resp.status_code == 200, resp.text
    assert sns.subscribed == ["p@adventure.asia"]
    assert db.actions()[-1] == "alert_recipient.confirmation_sent"


def test_resend_daily_cap(client, token, db, sns):
    rid = db.insert("p@adventure.asia", "pending", arn=sub_arn("p"))
    for h in (20, 10, 5):
        db.audit_at("alert_recipient.confirmation_sent", resource_id=rid, ago=timedelta(hours=h))
    resp = client.post(f"{BASE}/{rid}/resend", headers=_h(token))
    assert resp.status_code == 429 and sns.subscribed == []


# ── test e-mail ──────────────────────────────────────────────────────────────────────────────────

def test_test_email_queues_one_job_then_429(client, token, db):
    resp = client.post(BASE + "/test-email", headers=_h(token))
    assert resp.status_code == 202, resp.text
    assert resp.json()["recipients_count"] == 1 and resp.json()["created"] is True
    job = db.jobs[0]
    assert job["kind"] == "ops_alert_email" and job["payload"] == {"template": "test", "requested_by": "Nghiep Pham"}
    assert job["idempotency_key"].startswith("ops_alert_email:test:")
    assert db.actions() == ["alert_recipient.test_queued"]
    again = client.post(BASE + "/test-email", headers=_h(token))
    assert again.status_code == 429 and len(db.jobs) == 1


def test_test_email_needs_ses_and_a_recipient(client, token, monkeypatch):
    monkeypatch.delenv("SES_FROM_ADDRESS")
    assert client.post(BASE + "/test-email", headers=_h(token)).json()["code"] == "ses_not_configured"
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@lumiguides.it.com")
    monkeypatch.delenv("ALERT_EMAIL_TO")
    resp = client.post(BASE + "/test-email", headers=_h(token))
    assert resp.status_code == 503 and resp.json()["code"] == "no_recipient"


# ── flows ────────────────────────────────────────────────────────────────────────────────────────

def test_add_confirm_refresh_remove(client, token, db, sns):
    _add(client, token, "flow@adventure.asia")
    sns.confirm("flow@adventure.asia")
    body = client.post(BASE + "/refresh", headers=_h(token)).json()
    rec = body["recipients"][0]
    assert rec["status"] == "confirmed" and rec["confirmed_at"] and rec["last_seen_state"] == "confirmed"
    assert "alert_recipient.reconciled" in db.actions()
    client.delete(f"{BASE}/{rec['id']}", headers=_h(token))
    assert "flow@adventure.asia" not in sns.subs
    body = client.post(BASE + "/refresh", headers=_h(token)).json()
    assert body["recipients"] == [] and body["history"][0]["email"] == "flow@adventure.asia"
    assert body["history"][0]["removed_by"] == "Nghiep Pham"


def test_confirmation_clicked_after_removal_is_unsubscribed_again(client, token, db, sns):
    _add(client, token, "late@adventure.asia")
    rid = db.recipients[0]["id"]
    sns.unsubscribe_error = "InvalidParameter"          # SNS keeps the pending subscription
    client.delete(f"{BASE}/{rid}", headers=_h(token))
    sns.unsubscribe_error = None
    sns.confirm("late@adventure.asia")                  # the link is clicked afterwards
    body = client.post(BASE + "/refresh", headers=_h(token)).json()
    assert "late@adventure.asia" not in sns.subs
    assert body["unmanaged"] == [] and body["recipients"] == []


def test_unsubscribed_through_the_link_shows_as_removed(client, token, db, sns):
    rid = db.insert("u@adventure.asia", "confirmed", arn=sub_arn("u"))
    body = client.post(BASE + "/refresh", headers=_h(token)).json()
    assert body["recipients"] == []
    assert body["history"][0]["removed_reason"] == "unsubscribed"
    assert db.row(rid)["removed_by"] == "reconcile" and body["history"][0]["removed_by"] == "Status check (SNS)"
    audit = next(a for a in db.audit if a["action"] == "alert_recipient.reconciled")
    assert audit["actor"] == f"admin:{db.admin_id}" and audit["details"]["reason"] == "unsubscribed"
