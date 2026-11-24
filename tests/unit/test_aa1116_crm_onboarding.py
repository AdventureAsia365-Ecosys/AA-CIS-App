"""AA-1116 (S282 gaps G5 / G11) — a tenant's CRM agency and its first-owner invitation with no hand step, no database
and no network: services/tenant_account/crm_onboarding.py against a fake crm.ops.v1 relay, and the provisioning routes
(api/routers/admin_tenant_provision.py) that run it in the staff request.

- onboard(): refuses before any CRM call (unknown / offboarded tenant, no live crm subscription, a bad address); creates
  the agency only when the CRM has none (a race's tenant_exists is "exists"), invites the owner once (pending / owner
  present are done), reports a CRM refusal without raising, nudges the account feed and audits only when something
  changed, and never puts the address in the audit row or a log line.
- the routes: a create / attach with a crm add-on runs the saga's transaction under the job's id and then the
  onboarding with the caller's token; the address never reaches the job payload; an address without a crm add-on or a
  malformed one is 422 before anything is enqueued; POST /admin/tenants/{id}/crm-onboarding.
Invented names and addresses only (example.test).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from api.core.staff_authz import POLICY, TENANT_WRITE
from services.tenant_account import crm_agency
from services.tenant_account import crm_onboarding as co
from services.tenant_account import provision as pv

TID = "1116c000-0000-4000-8000-000000000001"
JOB = "1116c000-0000-4000-8000-0000000000f1"
KEY = "aa1116-crm-onboard-1"
OWNER = "owner@marrowfen-tours.example.test"
ACTOR = "admin:u-admin"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, row):
        self.row = row
        self.executed: list[tuple] = []

    async def fetchrow(self, sql, *args):
        assert sql == co.TENANT_SQL and args == (TID,)
        return self.row

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    def transaction(self):
        return _Tx()


class FakePool:
    def __init__(self, row):
        self.conn = FakeConn(row)

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Acq()


def _tenant(crm_status="trialing", account_status="active", name="Marrowfen Tours synthetic"):
    return {"tenant_id": TID, "name": name, "account_status": account_status, "suspend_reason": None,
            "crm_status": crm_status}


class FakeCrm:
    """crm.ops.v1 answers by (method, path): (status, body)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple] = []

    async def __call__(self, method, path, authorization, *, json=None, params=None):
        self.calls.append((method, path, authorization, json))
        return self.answers[(method, path)]


AGENCY = f"/tenants/{TID}"
INVITE = f"/tenants/{TID}/owner-invite"


@pytest.fixture
def audit(monkeypatch):
    m = AsyncMock()
    monkeypatch.setattr(co, "write_audit_log", m)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    return m


def _run(pool, crm, owner=OWNER, ticket="AA-1116"):
    return asyncio.run(co.onboard(pool, TID, owner_email=owner, authorization="Bearer staff", actor=ACTOR,
                                  ticket=ticket, request_key=KEY, call=crm))


# ── pure parts ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,ok", [
    (OWNER, OWNER), ("  Owner@Marrowfen-Tours.Example.Test ", OWNER), ("a@b.co", "a@b.co"),
    ("", None), ("no-at.example.test", None), ("two@@example.test", None), ("x@example", None), ("x@.", None),
    ("sp ace@example.test", None), ("@example.test", None), ("x@" + "a" * 250 + ".test", None), (7, None),
])
def test_clean_email(raw, ok):
    if ok is None:
        with pytest.raises(co.OnboardingError) as e:
            co.clean_email(raw)
        assert e.value.code == "invalid_owner_email" and e.value.status == 422
        assert not isinstance(raw, str) or not raw or raw not in e.value.message
    else:
        assert co.clean_email(raw) == ok


def test_ticket_ref():
    assert co.ticket_ref("AA-1116") == "AA-1116"
    assert co.ticket_ref(None, now=1763340000) == "CISONB-1763340000"
    for bad in ("1116", "AA_1116", "ABCDEFGHIJKLMNOPQ-1", "AA-12345678901"):
        with pytest.raises(co.OnboardingError) as e:
            co.ticket_ref(bad)
        assert e.value.code == "invalid_ticket_ref"


# ── onboard() ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("row,code,status", [
    (None, "tenant_not_found", 404),
    (_tenant(account_status="offboarding"), "tenant_offboarded", 409),
    (_tenant(crm_status=None), "no_crm_subscription", 409),
    (_tenant(crm_status="canceled"), "no_crm_subscription", 409),
    (_tenant(crm_status="paused"), "no_crm_subscription", 409),
])
def test_refused_before_any_crm_call(audit, row, code, status):
    crm = FakeCrm({})
    with pytest.raises(co.OnboardingError) as e:
        _run(FakePool(row), crm)
    assert (e.value.code, e.value.status) == (code, status) and crm.calls == [] and not audit.await_count


def test_a_bad_address_or_tenant_id_is_refused_before_the_database(audit):
    crm = FakeCrm({})
    with pytest.raises(co.OnboardingError) as e:
        _run(FakePool(_tenant()), crm, owner="not-an-address")
    assert e.value.code == "invalid_owner_email" and crm.calls == []
    with pytest.raises(co.OnboardingError) as e:
        asyncio.run(co.onboard(FakePool(_tenant()), "x", owner_email=None, authorization=None, actor=ACTOR, call=crm))
    assert e.value.code == "tenant_not_found"


def test_creates_the_agency_invites_the_owner_nudges_the_feed_and_audits(audit, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    pool = FakePool(_tenant(name="  " + "M" * 230 + "  "))
    crm = FakeCrm({("GET", AGENCY): (404, {"detail": "not_found"}), ("POST", "/tenants"): (201, {"tenant_id": TID}),
                   ("POST", INVITE): (202, {"status": "pending"})})
    with structlog.testing.capture_logs() as logs:
        out = _run(pool, crm)
    assert out == {"tenant_id": TID, "status": "done", "steps": ["crm_agency_created", "crm_owner_invited"],
                   "error": None}
    assert [c[:2] for c in crm.calls] == [("GET", AGENCY), ("POST", "/tenants"), ("POST", INVITE)]
    assert all(c[2] == "Bearer staff" for c in crm.calls)
    assert crm.calls[1][3] == {"tenant_id": TID, "agency_name": "M" * 200, "is_synthetic": True,
                               "ticket_ref": "AA-1116"}
    assert crm.calls[2][3] == {"owner_email": OWNER, "ticket_ref": "AA-1116"}
    sqls = [s for s, _ in pool.conn.executed]
    assert sqls == [crm_agency.UPSERT_SQL, co.ACTOR_SQL, co.EMIT_SQL]          # AA-1160: the marker first
    assert pool.conn.executed[0][1] == (TID, "crm_onboarding", float(crm_agency.REFRESH_AFTER_S))
    assert pool.conn.executed[2][1] == (TID, "active", None)
    kw = audit.await_args.kwargs
    assert kw["action"] == "tenant.crm_onboarded" and kw["actor"] == ACTOR and kw["tenant_id"] == TID
    assert kw["details"]["owner_invite"] == "sent" and kw["details"]["provision_request_key"] == KEY
    assert OWNER not in json.dumps(kw["details"]) and OWNER not in json.dumps(logs)


def test_outside_dev_the_agency_is_real(audit, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    crm = FakeCrm({("GET", AGENCY): (404, {}), ("POST", "/tenants"): (201, {}), ("POST", INVITE): (202, {})})
    _run(FakePool(_tenant()), crm)
    assert crm.calls[1][3]["is_synthetic"] is False


def test_a_second_run_sends_nothing_new_and_writes_nothing(audit):
    pool = FakePool(_tenant())
    crm = FakeCrm({("GET", AGENCY): (200, {"tenant_id": TID}),
                   ("POST", INVITE): (409, {"detail": "owner_invite_pending"})})
    out = _run(pool, crm)
    assert out["status"] == "done" and out["steps"] == ["crm_agency_exists", "crm_owner_invite_pending"]
    assert [s for s, _ in pool.conn.executed] == [crm_agency.UPSERT_SQL]      # only the AA-1160 marker (throttled)
    assert not audit.await_count


def test_a_create_race_and_an_owner_already_there_are_done(audit):
    pool = FakePool(_tenant())
    crm = FakeCrm({("GET", AGENCY): (404, {}), ("POST", "/tenants"): (409, {"detail": "tenant_exists"}),
                   ("POST", INVITE): (409, {"detail": "owner_exists"})})
    out = _run(pool, crm)
    assert out["status"] == "done" and out["steps"] == ["crm_agency_exists", "crm_owner_present"]
    assert [s for s, _ in pool.conn.executed] == [crm_agency.UPSERT_SQL]
    assert not audit.await_count


@pytest.mark.parametrize("step,answers,status,code", [
    ("agency_create", {("GET", AGENCY): (404, {}), ("POST", "/tenants"): (403, {"detail": "forbidden"})},
     "failed", "forbidden"),
    ("agency_create", {("GET", AGENCY): (404, {}), ("POST", "/tenants"): (409, {"detail": "tenant_deleted"})},
     "failed", "tenant_deleted"),
    ("agency_read", {("GET", AGENCY): (401, {"detail": "cognito_session_required"})},
     "failed", "cognito_session_required"),
    ("agency_read", {("GET", AGENCY): (503, {"detail": "crm_unavailable"})}, "failed", "crm_unavailable"),
    ("owner_invite", {("GET", AGENCY): (200, {}), ("POST", INVITE): (409, {"detail": "has_travellers"})},
     "partial", "has_travellers"),
    ("owner_invite", {("GET", AGENCY): (200, {}), ("POST", INVITE): (502, {"message": "no detail"})},
     "partial", "crm_error"),
])
def test_a_crm_refusal_is_reported_never_raised(audit, step, answers, status, code):
    pool = FakePool(_tenant())
    out = _run(pool, FakeCrm(answers))
    assert out["status"] == status and out["error"]["step"] == step and out["error"]["code"] == code
    assert not audit.await_count
    # AA-1160: the CRM agency marker only once the agency is known to exist
    marked = [s for s, _ in pool.conn.executed] == [crm_agency.UPSERT_SQL]
    assert marked is (step == "owner_invite")


def test_a_created_agency_without_an_address_is_audited_and_nudged(audit):
    pool = FakePool(_tenant())
    crm = FakeCrm({("GET", AGENCY): (404, {}), ("POST", "/tenants"): (201, {})})
    out = _run(pool, crm, owner=None, ticket=None)
    assert out["steps"] == ["crm_agency_created", "crm_owner_not_invited"] and out["status"] == "done"
    assert [c[:2] for c in crm.calls] == [("GET", AGENCY), ("POST", "/tenants")]
    assert crm.calls[1][3]["ticket_ref"].startswith("CISONB-")
    details = audit.await_args.kwargs["details"]
    assert details["owner_invite"] == "not_sent" and details["owner_email_given"] is False


# ── the routes ──────────────────────────────────────────────────────────────────────────────────────────────────
_SECRET = "aa1116-crm-onboarding-test-secret"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    pool = MagicMock()
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=True), pool


def _h(role="admin"):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "u-admin"}
    if role is not None:
        h["x-admin-role"] = role
    return h


def _job_row(payload):
    return {"id": JOB, "status": "queued", "payload": json.dumps(payload), "progress": "{}", "result": None,
            "error": None}


CREATE = {"request_key": KEY, "name": "Marrowfen Tours", "slug": "marrowfen-tours", "plan_tier": "starter",
          "posts_per_week": 3, "add_ons": [{"product": "crm", "plan_key": "crm.core"}]}


@pytest.fixture
def saga(client, monkeypatch):
    from services.jobs import tenant_provision_job as job

    _, pool = client
    enq = AsyncMock(return_value=(JOB, True))
    monkeypatch.setattr(job, "enqueue_request", enq)
    pool.fetchrow = AsyncMock(return_value=_job_row({**CREATE, "mode": "create"}))
    pool.fetchval = AsyncMock(return_value=CREATE["slug"])   # the provisioned tenant's slug (the create check)
    provision = AsyncMock(return_value={"tenant_id": TID, "steps": ["tenant_created", "content_active",
                                                                    "crm_subscribed"]})
    monkeypatch.setattr(pv, "provision", provision)
    onboard = AsyncMock(return_value={"tenant_id": TID, "status": "done",
                                      "steps": ["crm_agency_created", "crm_owner_invited"], "error": None})
    monkeypatch.setattr(co, "onboard", onboard)
    return enq, provision, onboard


def test_policy_entry():
    assert POLICY[("POST", "/admin/tenants/{tenant_id}/crm-onboarding")] == TENANT_WRITE


def test_create_with_crm_sets_up_the_agency_in_the_same_request(client, saga):
    c, _ = client
    enq, provision, onboard = saga
    resp = c.post("/admin/tenants/provision", json={**CREATE, "crm_owner_email": " Owner@Marrowfen-Tours.Example.Test",
                                                     "crm_ticket_ref": "AA-1116"}, headers=_h())
    assert resp.status_code == 202, resp.text
    out = resp.json()
    assert out["tenant_id"] == TID and out["steps"][-1] == "crm_subscribed"
    assert out["crm_onboarding"] == {"tenant_id": TID, "status": "done",
                                     "steps": ["crm_agency_created", "crm_owner_invited"], "error": None}
    req = enq.await_args.args[1]
    assert OWNER not in json.dumps(req.to_payload()) and "crm_owner_email" not in req.to_payload()
    assert provision.await_args.args[1] == req and provision.await_args.kwargs == {"job_id": JOB}
    kw = onboard.await_args.kwargs
    assert onboard.await_args.args[1] == TID
    assert kw["owner_email"] == OWNER and kw["ticket"] == "AA-1116" and kw["actor"] == "admin:u-admin"
    assert kw["request_key"] == KEY and "authorization" in kw   # the caller's own token (relay_authorization)


def test_attach_with_crm_and_without_an_address_still_creates_the_agency(client, saga):
    c, _ = client
    _, _, onboard = saga
    resp = c.post(f"/admin/tenants/{TID}/provision", headers=_h(),
                  json={"request_key": KEY, "add_ons": [{"product": "crm", "plan_key": "crm.core", "seats": 3}]})
    assert resp.status_code == 202 and resp.json()["crm_onboarding"]["status"] == "done"
    assert onboard.await_args.kwargs["owner_email"] is None


def test_no_crm_add_on_no_onboarding(client, saga):
    c, pool = client
    _, provision, onboard = saga
    body = {**CREATE, "add_ons": [{"product": "tripplanner", "plan_key": "tripplanner.hosted"}]}
    resp = c.post("/admin/tenants/provision", json=body, headers=_h())
    assert resp.status_code == 202 and resp.json()["crm_onboarding"] is None
    assert not provision.await_count and not onboard.await_count


@pytest.mark.parametrize("body,code", [
    ({**CREATE, "add_ons": [], "crm_owner_email": OWNER}, "crm_owner_email_without_crm"),
    ({**CREATE, "crm_owner_email": "not-an-address"}, "invalid_owner_email"),
])
def test_an_address_problem_is_refused_before_anything_is_enqueued(client, saga, body, code):
    c, _ = client
    enq, _, _ = saga
    resp = c.post("/admin/tenants/provision", json=body, headers=_h())
    assert resp.status_code == 422 and resp.json()["detail"]["detail"] == code and not enq.await_count
    assert "not-an-address" not in resp.text


def test_a_refused_saga_skips_the_crm(client, saga, monkeypatch):
    c, _ = client
    _, provision, onboard = saga
    provision.side_effect = pv.ProvisionError("slug_taken", "exists")
    resp = c.post("/admin/tenants/provision", json={**CREATE, "crm_owner_email": OWNER}, headers=_h())
    assert resp.status_code == 202
    assert resp.json()["crm_onboarding"] == {"tenant_id": None, "status": "skipped", "steps": [],
                                             "error": {"step": "provision", "http_status": None, "code": "slug_taken"}}
    assert not onboard.await_count
    provision.side_effect = ConnectionError("lost")
    resp = c.post("/admin/tenants/provision", json={**CREATE, "crm_owner_email": OWNER}, headers=_h())
    assert resp.json()["crm_onboarding"]["error"]["code"] == "provision_pending" and not onboard.await_count


def test_an_onboarding_check_refusal_is_reported(client, saga):
    c, _ = client
    _, _, onboard = saga
    onboard.side_effect = co.OnboardingError("no_crm_subscription", "none")
    resp = c.post("/admin/tenants/provision", json={**CREATE, "crm_owner_email": OWNER}, headers=_h())
    got = resp.json()["crm_onboarding"]
    assert got["status"] == "skipped" and got["error"] == {"step": "check", "http_status": 409,
                                                           "code": "no_crm_subscription"}


def test_the_onboarding_route(client, saga):
    c, _ = client
    _, _, onboard = saga
    path = f"/admin/tenants/{TID}/crm-onboarding"
    resp = c.post(path, json={"owner_email": OWNER}, headers=_h())
    assert resp.status_code == 200 and resp.json()["status"] == "done"
    assert onboard.await_args.args[1] == TID and onboard.await_args.kwargs["owner_email"] == OWNER
    onboard.side_effect = co.OnboardingError("tenant_not_found", "No tenant with this id.", 404)
    resp = c.post(path, json={}, headers=_h())
    assert resp.status_code == 404 and resp.headers["X-Error-Code"] == "tenant_not_found"
    assert c.post(path, json={"ticket_ref": "bad ref"}, headers=_h()).status_code == 422


@pytest.mark.parametrize("role", ["content", "customer_success", None])
def test_the_onboarding_route_is_admin_only(client, saga, role):
    c, _ = client
    _, _, onboard = saga
    assert c.post(f"/admin/tenants/{TID}/crm-onboarding", json={}, headers=_h(role)).status_code == 403
    assert not onboard.await_count


# ── a reused request key never sends an owner invitation to another tenant's CRM agency (review fix) ───────────
OTHER = "1116c000-0000-4000-8000-0000000000b2"
ATTACH = {"request_key": KEY, "add_ons": [{"product": "crm", "plan_key": "crm.core"}], "crm_owner_email": OWNER}


def _skipped_reused(resp):
    assert resp.status_code == 202, resp.text
    assert resp.json()["crm_onboarding"] == {"tenant_id": None, "status": "skipped", "steps": [],
                                             "error": {"step": "check", "http_status": None,
                                                       "code": "request_key_reused"}}


def test_a_key_first_used_for_another_tenant_sends_nothing_to_the_crm(client, saga):
    c, pool = client
    enq, provision, onboard = saga
    enq.return_value = (JOB, False)    # the key's first job: an attach for tenant TID
    pool.fetchrow.return_value = _job_row({"request_key": KEY, "mode": "attach", "tenant_id": TID,
                                           "add_ons": [{"product": "crm", "plan_key": "crm.core", "seats": None}]})
    _skipped_reused(c.post(f"/admin/tenants/{OTHER}/provision", json=ATTACH, headers=_h()))
    assert not provision.await_count and not onboard.await_count
    # the same key on the create route (another mode) is refused the same way
    _skipped_reused(c.post("/admin/tenants/provision", json={**CREATE, "crm_owner_email": OWNER}, headers=_h()))
    assert not provision.await_count and not onboard.await_count


def test_a_create_key_reused_with_another_slug_sends_nothing(client, saga):
    c, _ = client
    enq, provision, onboard = saga
    enq.return_value = (JOB, False)    # stored payload: the create of marrowfen-tours
    body = {**CREATE, "slug": "quillmere-trails", "crm_owner_email": OWNER}
    _skipped_reused(c.post("/admin/tenants/provision", json=body, headers=_h()))
    assert not provision.await_count and not onboard.await_count


def test_the_same_request_again_still_onboards(client, saga):
    c, pool = client
    enq, _, onboard = saga
    enq.return_value = (JOB, False)    # a retry of the same attach (same key, same tenant)
    pool.fetchrow.return_value = _job_row({"request_key": KEY, "mode": "attach", "tenant_id": TID,
                                           "add_ons": [{"product": "crm", "plan_key": "crm.core", "seats": None}]})
    resp = c.post(f"/admin/tenants/{TID}/provision", json=ATTACH, headers=_h())
    assert resp.status_code == 202 and resp.json()["crm_onboarding"]["status"] == "done"
    assert onboard.await_args.args[1] == TID


def test_a_stored_outcome_of_another_tenant_sends_nothing(client, saga):
    """A new job (the first one's row is gone) but the saga's outcome, found by the key alone, is another tenant's."""
    c, pool = client
    _, provision, onboard = saga
    _skipped_reused(c.post(f"/admin/tenants/{OTHER}/provision", json=ATTACH, headers=_h()))   # provision: TID
    assert provision.await_count == 1 and not onboard.await_count
    pool.fetchval.return_value = "another-slug"   # a create whose stored outcome is a tenant with another slug
    _skipped_reused(c.post("/admin/tenants/provision", json={**CREATE, "crm_owner_email": OWNER}, headers=_h()))
    assert not onboard.await_count


def test_a_failing_marker_write_never_fails_the_onboarding(audit):
    """AA-1160: the marker is best effort (a database error is logged, the onboarding goes on)."""
    pool = FakePool(_tenant())

    async def broken(sql, *args):
        if sql == crm_agency.UPSERT_SQL:
            raise RuntimeError("database gone")
        pool.conn.executed.append((sql, args))
    pool.conn.execute = broken
    crm = FakeCrm({("GET", AGENCY): (200, {}), ("POST", INVITE): (409, {"detail": "owner_exists"})})
    with structlog.testing.capture_logs() as logs:
        out = _run(pool, crm)
    assert out["status"] == "done"
    assert any(e["event"] == "crm_agency_marker_failed" and e["error"] == "RuntimeError" for e in logs)
