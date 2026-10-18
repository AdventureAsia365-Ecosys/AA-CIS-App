"""AA-816 (S242) — the staff invitation e-mail: api/services/staff_invite.py, the create / resend writes in
api/services/staff_directory.py, POST /admin/platform/staff/{id}/invite, and the staff_invite_email job.

Covers:
- the template: subject, the only link is the sign-in page, roles in plain words, no secret / code / token /
  password, AA colours, HTML escaping and control-character stripping of display names;
- the rate limit (1 per 10 minutes, 5 per 24 h) as a pure function and through the service / route (429 +
  Retry-After);
- the create enqueues exactly one job (ids-only payload, idempotency key per staff id + seq) in the SAME
  transaction as the row when the checkbox is on, none when off or when SES is not configured, none on a
  re-POST; a failure after the enqueue rolls both back;
- resend: seq from the highest key (survives job retention), the key backstop, guards (inactive 409, no email
  409, break-glass 403, stale owner 403, not configured 503), policy through the real app (legacy / non-owner /
  reviewer 403);
- the job handler with a stubbed SES client: From / configuration set / recipient, audit rows, send-once guard,
  skips, permanent vs transient SES errors (no address in the job error), on_terminal.
No AWS call: SES is a stub, Cognito a botocore Stubber.
"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from api.core import identity_config, staff_identity
from api.main import app
from api.routers import admin, admin_platform, admin_settings
from api.services import staff_directory as sd
from api.services import staff_invite as si
from services.jobs import staff_invite_email_job as job_mod
from shared.jobs import registry
from shared.jobs.queue import Job
from tests.unit import test_aa816_staff_directory_writes as c6
from tests.unit.aa854_cognito_staff import ISS, install_issuer

NEW_EMAIL = c6.NEW_EMAIL
SENDER = "no-reply@lumiguides.it.com"
CONFIG_SET = "aa-email-dev-transactional"
T0 = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)


# ── Fakes ────────────────────────────────────────────────────────────────────────────────────────

class InviteDB(c6.StaffDB):
    """The C6 fake plus shared.job (queue.enqueue), timestamped audit rows on a fake clock, and the invite
    SQL (seq, rate window, worker reads, invite columns on the staff list)."""

    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[dict[str, Any]] = []
        self.now = T0
        self.fail_on_action: Optional[str] = None

    @asynccontextmanager
    async def transaction(self):
        jobs = [dict(j) for j in self.jobs]
        try:
            async with super().transaction():
                yield
        except BaseException:
            self.jobs = jobs
            raise

    def _listed(self, r: dict[str, Any]) -> dict[str, Any]:
        out = super()._listed(r)

        def latest(action: str) -> Optional[datetime]:
            ts = [a["created_at"] for a in self.audit if a["action"] == action and a["resource_id"] == r["id"]]
            return max(ts) if ts else None

        out.update(last_invited_at=latest(si.InviteAudit.SENT),
                   last_invite_requested_at=latest(si.InviteAudit.REQUESTED),
                   last_invite_not_sent_at=latest(si.InviteAudit.NOT_SENT))
        return out

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO acp_shared.audit_log" in sql:
            if self.fail_on_action and args[2] == self.fail_on_action:
                raise OSError("connection reset during the audit write")
            result = await super().execute(sql, *args)
            self.audit[-1]["created_at"] = self.now
            return result
        return await super().execute(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        if "INSERT INTO shared.job" in sql:
            self.sql.append(sql)
            kind, payload, key, max_attempts, _parent, created_by, _run_after = args
            if key is not None and any(j["idempotency_key"] == key for j in self.jobs):
                return None
            jid = str(uuid.uuid4())
            self.jobs.append({"id": jid, "kind": kind, "payload": json.loads(payload), "idempotency_key": key,
                              "max_attempts": max_attempts, "created_by": created_by})
            return {"id": jid}
        if sql == si.SQL_RECENT_REQUESTS:
            self.sql.append(sql)
            action, rtype, rid, window = args
            ts = [a["created_at"] for a in self.audit if a["action"] == action and a["resource_type"] == rtype
                  and a["resource_id"] == rid and a["created_at"] > self.now - timedelta(seconds=window)]
            return {"n": len(ts), "first_at": min(ts) if ts else None, "last_at": max(ts) if ts else None,
                    "now": self.now}
        if sql == si.SQL_INVITE_TARGET:
            self.sql.append(sql)
            r = self._find(args[0])
            if r is None:
                return None
            return {k: r.get(k) for k in ("id", "username", "email", "display_name", "role", "is_active")} | {
                "booking_role": self.grants.get((r["id"], "booking"))}
        if sql == si.SQL_ACTOR_NAME:
            self.sql.append(sql)
            r = self._find(args[0])
            return None if r is None else {"display_name": r.get("display_name"), "username": r["username"]}
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if sql == si.SQL_MAX_SEQ:
            self.sql.append(sql)
            prefix = args[0].rstrip("%")
            seqs = [int(j["idempotency_key"].rsplit(":", 1)[1]) for j in self.jobs
                    if (j["idempotency_key"] or "").startswith(prefix)]
            return max(seqs, default=0)
        if sql.startswith("SELECT id FROM shared.job WHERE idempotency_key"):
            self.sql.append(sql)
            return next((j["id"] for j in self.jobs if j["idempotency_key"] == args[0]), None)
        if sql == si.SQL_ALREADY_SENT:
            self.sql.append(sql)
            action, rtype, rid, job_id = args
            return 1 if any(a["action"] == action and a["resource_id"] == rid and a["details"].get("job_id") == job_id
                            for a in self.audit) else None
        return await super().fetchval(sql, *args)

    def invite_jobs(self) -> list[dict[str, Any]]:
        return [j for j in self.jobs if j["kind"] == si.KIND]


@pytest.fixture
def mail_env(monkeypatch):
    monkeypatch.setenv("SES_FROM_ADDRESS", SENDER)
    monkeypatch.setenv("SES_CONFIGURATION_SET", CONFIG_SET)
    monkeypatch.delenv("ADMIN_BASE_URL", raising=False)


@pytest.fixture
def no_mail_env(monkeypatch):
    for k in ("SES_FROM_ADDRESS", "SES_CONFIGURATION_SET"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def db():
    staff_identity.clear_cache()
    d = InviteDB()
    d.owner = d.add(owner=True, email="owner@adventure.asia", username="owner", role="admin",
                    display_name="Nghiep Pham")
    yield d
    staff_identity.clear_cache()


@pytest.fixture
def stubbed():
    client = c6._client()
    with Stubber(client) as stub:
        yield sd.CognitoDirectory(client=client, pool_id=c6.POOL), stub
        stub.assert_no_pending_responses()


def _expect_create(stub: Stubber, email: str = NEW_EMAIL, group: str = "cis-content") -> None:
    c6._expect(stub, "admin_create_user", c6._create(email), {"User": {"Username": email}})
    c6._expect(stub, "admin_add_user_to_group", c6._group(email, group))


# ── Template ─────────────────────────────────────────────────────────────────────────────────────

def _render(**kw: Any):
    args = dict(to=NEW_EMAIL, display_name="Thu Le", actor_name="Nghiep Pham", role="admin", booking_role="admin")
    args.update(kw)
    return si.render_invite(**args)


def test_template_subject_link_steps_and_roles(mail_env):
    m = _render()
    assert m.subject == "You've been added to Adventure Asia staff admin" and m.to == NEW_EMAIL
    login = "https://aa-cis.lumiguides.it.com/login"
    for part in (m.text, m.html):
        assert login in part
        assert "Sign in with company email" in part and NEW_EMAIL in part
        assert "one-time code" in part and "valid for a few minutes" in part
        assert "Nghiep Pham" in part and "Thu Le" in part
        assert si.CIS_ROLE_TEXT["admin"] in part and "Booking administrator" in part
        assert "There is no password" in part
    # Every link in the HTML is the sign-in page (no deep link, no query string, no code); the only image is the
    # public logo.
    assert set(re.findall(r'href="([^"]+)"', m.html)) == {login}
    assert re.findall(r'src="([^"]+)"', m.html) == ["https://aa-cis.lumiguides.it.com/assets/adventure-asia-logo.png"]
    assert "?" not in login and "#" not in login
    assert "#f4f1ec" in m.html and "#db9628" in m.html


def test_template_carries_no_secret_or_code(mail_env):
    m = _render()
    for part in (m.text.lower(), m.html.lower()):
        for banned in ("token", "temporary password", "password:", "code=", "secret", "verification code:"):
            assert banned not in part
    assert len(re.findall(r"password", m.text.lower())) == 1   # only "There is no password"


def test_template_plain_words_for_every_role(mail_env):
    for role, text in si.CIS_ROLE_TEXT.items():
        assert text in _render(role=role).text
    assert "Booking: No access to Booking" in _render(booking_role=None).text
    assert "Booking: Sales" in _render(booking_role="sales").text
    assert "Booking: Content creator" in _render(booking_role="content_creator").text


def test_template_escapes_and_cleans_display_names(mail_env):
    m = _render(display_name='<script>alert(1)</script> & "x"', actor_name="<b>Evil</b>\r\nBcc: x@evil.com")
    assert "<script>" not in m.html and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;" in m.html
    assert "<b>Evil</b>" not in m.html and "&lt;b&gt;Evil&lt;/b&gt;" in m.html
    assert "\r" not in m.text and "<b>Evil</b> Bcc: x@evil.com added you" in m.text
    assert m.subject == si.SUBJECT   # never built from a name


def test_template_fallbacks_and_base_url_override(monkeypatch):
    m = _render(display_name=None, actor_name="  ")
    assert m.text.startswith("Hello,\n") and si.UNKNOWN_ACTOR in m.text
    monkeypatch.setenv("ADMIN_BASE_URL", "https://admin.example.test/")
    assert "https://admin.example.test/login" in _render().text


# ── Rate limit (pure) ────────────────────────────────────────────────────────────────────────────

def test_rate_limit_rules():
    assert si.rate_limit(0, None, None, T0) is None
    hit = si.rate_limit(1, T0, T0, T0 + timedelta(minutes=3))
    assert hit is not None and hit.retry_after_s == 7 * 60 and "7 minute(s)" in hit.message
    assert si.rate_limit(1, T0, T0, T0 + timedelta(minutes=10)) is None
    first = T0 - timedelta(hours=20)
    day = si.rate_limit(5, first, T0 - timedelta(hours=1), T0)
    assert day is not None and day.retry_after_s == 4 * 3600 and "5 invitations" in day.message
    assert "09:00 UTC" not in day.message and "13:00 UTC" in day.message
    assert si.rate_limit(4, first, T0 - timedelta(hours=1), T0) is None


def test_invite_status_derivation():
    t1, t2 = T0, T0 + timedelta(minutes=1)
    assert si.invite_status(None, None, None) is None
    assert si.invite_status(None, t1, None) == "queued"
    assert si.invite_status(t2, t1, None) == "sent"
    assert si.invite_status(None, t1, t2) == "not_sent"
    assert si.invite_status(t1, t2, None) == "queued"          # a resend after a sent one
    assert si.invite_status(t2, t1, t1) == "sent"


# ── Service: create ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_with_invite_enqueues_exactly_one_job_in_the_same_transaction(db, stubbed, mail_env):
    cognito, stub = stubbed
    _expect_create(stub)
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="New Person",
                                   role="content", send_invite=True)
    assert result.invite == sd.INVITE_QUEUED and result.created
    jobs = db.invite_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["idempotency_key"] == f"staff_invite_email:{result.staff_id}:1" and job["max_attempts"] == 5
    assert job["payload"] == {"staff_id": result.staff_id, "requested_by": db.owner, "reason": "created", "seq": 1}
    assert NEW_EMAIL not in json.dumps(job["payload"]) and job["created_by"] == f"admin:{db.owner}"
    assert result.invite_job_id == job["id"]
    assert db.actions() == [sd.StaffAudit.CREATED, sd.StaffAudit.INVITE_REQUESTED]
    assert db.audit[1]["details"]["job_id"] == job["id"] and db.audit[0]["details"]["send_invite"] is True


@pytest.mark.asyncio
async def test_create_without_invite_enqueues_nothing(db, stubbed, mail_env):
    cognito, stub = stubbed
    _expect_create(stub)
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N",
                                   role="content", send_invite=False)
    assert result.invite == sd.INVITE_NOT_REQUESTED and db.jobs == []
    assert db.actions() == [sd.StaffAudit.CREATED]


@pytest.mark.asyncio
async def test_create_with_invite_but_no_ses_sender_creates_without_a_job(db, stubbed, no_mail_env):
    cognito, stub = stubbed
    _expect_create(stub)
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N",
                                   role="content", send_invite=True)
    assert result.created and result.invite == sd.INVITE_NOT_CONFIGURED and db.jobs == []


@pytest.mark.asyncio
async def test_a_failure_after_the_enqueue_rolls_back_the_row_and_the_job(db, stubbed, mail_env):
    cognito, _ = stubbed   # no Cognito call may happen: the transaction fails first
    db.fail_on_action = si.InviteAudit.REQUESTED
    with pytest.raises(OSError):
        await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N",
                              role="content", send_invite=True)
    assert db.jobs == [] and len(db.rows) == 1 and db.audit == []


@pytest.mark.asyncio
async def test_re_post_of_an_existing_create_queues_no_second_invite(db, stubbed, mail_env):
    cognito, stub = stubbed
    db.add(email=NEW_EMAIL, role="content", username="new.person")
    c6._expect_error(stub, "admin_create_user", "UsernameExistsException", c6._create())
    c6._expect(stub, "admin_add_user_to_group", c6._group(NEW_EMAIL, "cis-content"))
    c6._expect(stub, "admin_enable_user", c6._user(NEW_EMAIL))
    for g in ("cis-admin", "cis-reviewer") + sd.BOOKING_GROUPS:
        c6._expect(stub, "admin_remove_user_from_group", c6._group(NEW_EMAIL, g))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N",
                                   role="content", send_invite=True)
    assert not result.created and result.invite == sd.INVITE_EXISTING and db.jobs == []


# ── Service: resend ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resend_queues_with_the_next_seq_and_is_rate_limited(db, mail_env):
    uid = db.add(email=NEW_EMAIL, role="content")
    r1 = await sd.resend_invite(db, actor_id=db.owner, staff_id=uid)
    assert r1.invite == sd.INVITE_QUEUED and r1.operation == "invite" and r1.cognito == sd.COGNITO_NOT_APPLICABLE
    assert [j["idempotency_key"] for j in db.invite_jobs()] == [f"staff_invite_email:{uid}:1"]
    assert db.invite_jobs()[0]["payload"]["reason"] == "resend"

    db.now = T0 + timedelta(minutes=4)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.resend_invite(db, actor_id=db.owner, staff_id=uid)
    assert err.value.status == 429 and err.value.headers == {"Retry-After": "360"}
    assert "6 minute(s)" in err.value.detail and len(db.invite_jobs()) == 1

    db.now = T0 + timedelta(minutes=11)
    await sd.resend_invite(db, actor_id=db.owner, staff_id=uid)
    assert db.invite_jobs()[-1]["idempotency_key"] == f"staff_invite_email:{uid}:2"


@pytest.mark.asyncio
async def test_resend_daily_cap_counts_the_create_invite(db, stubbed, mail_env):
    cognito, stub = stubbed
    _expect_create(stub)
    created = await sd.create_staff(db, cognito, actor_id=db.owner, email=NEW_EMAIL, display_name="N",
                                    role="content", send_invite=True)
    for i in range(1, 5):
        db.now = T0 + timedelta(minutes=11 * i)
        await sd.resend_invite(db, actor_id=db.owner, staff_id=created.staff_id)
    assert len(db.invite_jobs()) == 5
    db.now = T0 + timedelta(hours=2)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.resend_invite(db, actor_id=db.owner, staff_id=created.staff_id)
    assert err.value.status == 429 and "5 invitations" in err.value.detail
    assert int(err.value.headers["Retry-After"]) == 22 * 3600
    db.now = T0 + timedelta(hours=24, seconds=1)
    await sd.resend_invite(db, actor_id=db.owner, staff_id=created.staff_id)
    assert len(db.invite_jobs()) == 6


@pytest.mark.asyncio
async def test_seq_comes_from_the_highest_kept_key_so_retention_never_causes_a_collision(db, mail_env):
    uid = db.add(email=NEW_EMAIL, role="content")
    # Jobs 1 and 2 were deleted by the 30-day retention; job 3 is still there.
    db.jobs.append({"id": "kept", "kind": si.KIND, "payload": {}, "idempotency_key": si.invite_key(uid, 3),
                    "max_attempts": 5, "created_by": "admin:x"})
    await sd.resend_invite(db, actor_id=db.owner, staff_id=uid)
    assert db.invite_jobs()[-1]["idempotency_key"] == si.invite_key(uid, 4) and len(db.invite_jobs()) == 2


@pytest.mark.asyncio
async def test_the_idempotency_key_is_the_backstop_for_a_duplicate_request(db, mail_env, monkeypatch):
    uid = db.add(email=NEW_EMAIL, role="content")
    first = await si.enqueue_invite(db, staff_id=uid, actor_id=db.owner, reason="resend")
    real = db.fetchval

    async def stale_seq(sql: str, *args: Any) -> Any:   # a second request that read the seq before the first wrote
        return 0 if sql == si.SQL_MAX_SEQ else await real(sql, *args)

    monkeypatch.setattr(db, "fetchval", stale_seq)
    second = await si.enqueue_invite(db, staff_id=uid, actor_id=db.owner, reason="resend")
    assert second == first and len(db.invite_jobs()) == 1
    assert db.audit[-1]["details"]["new_job"] is False


@pytest.mark.asyncio
async def test_resend_guards(db, mail_env, monkeypatch):
    inactive = db.add(email="gone@adventure.asia", role="content", active=False)
    no_email = db.add(email=None, role="content")
    bg = db.add(username="admin", email="admin@adventure.asia", role="admin")
    active = db.add(email=NEW_EMAIL, role="content")
    for sid, status, code in ((inactive, 409, "inactive"), (no_email, 409, "no_email"), (bg, 403, "breakglass"),
                              (str(uuid.uuid4()), 404, "not_found")):
        with pytest.raises(sd.StaffDirectoryError) as err:
            await sd.resend_invite(db, actor_id=db.owner, staff_id=sid)
        assert (err.value.status, err.value.code) == (status, code)
    monkeypatch.delenv("SES_FROM_ADDRESS")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.resend_invite(db, actor_id=db.owner, staff_id=active)
    assert err.value.status == 503
    monkeypatch.setenv("SES_FROM_ADDRESS", SENDER)
    db.row(db.owner)["is_owner"] = False
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.resend_invite(db, actor_id=db.owner, staff_id=active)
    assert (err.value.status, err.value.code) == (403, "not_owner")
    assert db.jobs == [] and db.audit == []


# ── Routes through the real app ──────────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch, db, mail_env):
    iss, _ = install_issuer()
    cognito = sd.CognitoDirectory(client=c6._client(), pool_id=c6.POOL)
    stub = Stubber(cognito.client())
    stub.activate()
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", c6.SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", c6.SECRET)
    monkeypatch.setattr(app.state, "pool", db, raising=False)
    app.dependency_overrides[sd.get_cognito] = lambda: cognito
    owner_sub = "aaaaaaaa-0000-4000-8000-00000000000a"
    db.row(db.owner).update(cognito_iss=ISS, cognito_sub=owner_sub)
    try:
        yield TestClient(app), stub, iss, iss.token(sub=owner_sub, email="owner@adventure.asia",
                                                    **{"cognito:groups": ["cis-admin"]})
    finally:
        app.dependency_overrides.pop(sd.get_cognito, None)
        stub.deactivate()
        identity_config.reset_cis_staff_verifier()
        staff_identity.clear_cache()


def test_route_create_queues_one_invite_by_default_and_none_when_unticked(api, db):
    client, stub, _, token = api
    _expect_create(stub)
    resp = client.post("/admin/platform/staff", headers=c6._h(token),
                       json={"email": NEW_EMAIL, "display_name": "New Person", "role": "content"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["invite"] == "queued" and body["invite_job_id"] == db.invite_jobs()[0]["id"]
    assert body["staff"]["invite_status"] == "queued" and body["staff"]["last_invite_requested_at"]
    assert len(db.invite_jobs()) == 1

    other = "other.person@adventure.asia"
    _expect_create(stub, email=other)
    resp = client.post("/admin/platform/staff", headers=c6._h(token),
                       json={"email": other, "display_name": "Other", "role": "content", "send_invite": False})
    assert resp.status_code == 201 and resp.json()["invite"] == "not_requested"
    assert len(db.invite_jobs()) == 1
    stub.assert_no_pending_responses()


def test_route_resend_202_then_429_with_retry_after(api, db):
    client, _, _, token = api
    uid = db.add(email=NEW_EMAIL, role="content")
    resp = client.post(f"/admin/platform/staff/{uid}/invite", headers=c6._h(token))
    assert resp.status_code == 202, resp.text
    assert resp.json()["invite"] == "queued" and resp.json()["operation"] == "invite"
    assert resp.json()["staff"]["invite_status"] == "queued"
    resp = client.post(f"/admin/platform/staff/{uid}/invite", headers=c6._h(token))
    assert resp.status_code == 429 and resp.headers["Retry-After"] == "600"
    assert "10 minutes" in resp.json()["detail"] and len(db.invite_jobs()) == 1


def test_route_resend_status_codes(api, db):
    client, _, _, token = api
    inactive = db.add(email="gone@adventure.asia", role="content", active=False)
    bg = db.add(username="admin", email="admin@adventure.asia", role="admin")
    assert client.post(f"/admin/platform/staff/{inactive}/invite", headers=c6._h(token)).status_code == 409
    assert client.post(f"/admin/platform/staff/{bg}/invite", headers=c6._h(token)).status_code == 403
    assert client.post("/admin/platform/staff/not-a-uuid/invite", headers=c6._h(token)).status_code == 404
    assert db.jobs == []


def test_route_resend_policy_refuses_legacy_non_owner_and_reviewer(api, db):
    client, _, iss, _ = api
    uid = db.add(email=NEW_EMAIL, role="content")
    before = len(db.sql)
    resp = client.post(f"/admin/platform/staff/{uid}/invite", headers=c6._h(None, role="admin"))
    assert resp.status_code == 403 and "Cognito" in resp.json()["detail"]
    assert len(db.sql) == before   # refused before the DB
    sub = "bbbbbbbb-0000-4000-8000-00000000000b"
    db.add(email="plain.admin@adventure.asia", role="admin", iss=ISS, sub=sub)
    token = iss.token(sub=sub, email="plain.admin@adventure.asia", **{"cognito:groups": ["cis-admin"]})
    resp = client.post(f"/admin/platform/staff/{uid}/invite", headers=c6._h(token))
    assert resp.status_code == 403 and resp.json()["detail"] == "Only a platform owner can change staff accounts"
    sub2 = "cccccccc-0000-4000-8000-00000000000c"
    db.add(owner=True, email="rev@adventure.asia", role="reviewer", iss=ISS, sub=sub2)
    token = iss.token(sub=sub2, email="rev@adventure.asia", **{"cognito:groups": ["cis-reviewer"]})
    assert client.post(f"/admin/platform/staff/{uid}/invite", headers=c6._h(token, role="admin")).status_code == 403
    assert db.jobs == []


def test_staff_list_reports_last_invited(api, db):
    client, _, _, token = api
    uid = db.add(email=NEW_EMAIL, role="content")
    db.audit.append({"tenant_id": None, "actor": "x", "action": si.InviteAudit.REQUESTED, "resource_type": "admin_user",
                     "resource_id": uid, "details": {}, "created_at": T0})
    db.audit.append({"tenant_id": None, "actor": si.WORKER_ACTOR, "action": si.InviteAudit.SENT,
                     "resource_type": "admin_user", "resource_id": uid, "details": {},
                     "created_at": T0 + timedelta(seconds=20)})
    row = next(s for s in client.get("/admin/platform/staff", headers=c6._h(token)).json()["staff"] if s["id"] == uid)
    assert row["invite_status"] == "sent" and row["last_invited_at"] == (T0 + timedelta(seconds=20)).isoformat()
    owner = client.get(f"/admin/platform/staff/{db.owner}", headers=c6._h(token)).json()
    assert owner["invite_status"] is None and owner["last_invited_at"] is None


def test_staff_select_reads_the_invite_columns_from_the_audit_log():
    for alias in ("last_invited_at", "last_invite_requested_at", "last_invite_not_sent_at"):
        assert f"AS {alias}" in admin_platform._SELECT
    assert "acp_shared.audit_log" in admin_platform._SELECT and "password_hash" not in admin_platform._SELECT


# ── Job handler (stubbed SES) ────────────────────────────────────────────────────────────────────

class StubSes:
    def __init__(self, error: Optional[Exception] = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"MessageId": "ses-msg-1"}


class SesClientError(Exception):  # botocore-shaped: .response["Error"]["Code"]
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.response = {"Error": {"Code": code, "Message": message}}


def _ctx(db: InviteDB, staff_id: str, ses: StubSes, job_id: Optional[str] = None) -> registry.JobContext:
    job = Job(id=job_id or str(uuid.uuid4()), kind=si.KIND,
              payload={"staff_id": staff_id, "requested_by": db.owner, "reason": "created", "seq": 1},
              status="running", attempt=1, max_attempts=5, progress={}, cost_usd=0.0)
    return registry.JobContext(db, job, resources={"ses": ses})


def test_kind_is_registered():
    kind = registry.get_kind("staff_invite_email")
    assert kind is not None and kind.max_attempts == 5 and kind.on_terminal is job_mod.mark_not_sent
    assert not kind.background


@pytest.mark.asyncio
async def test_handler_sends_through_ses_as_the_terraform_sender(db, mail_env):
    uid = db.add(email=NEW_EMAIL, role="reviewer", booking="sales", display_name="Thu <Le>")
    ses = StubSes()
    ctx = _ctx(db, uid, ses)
    result = await job_mod.run(ctx)
    assert len(ses.calls) == 1
    call = ses.calls[0]
    assert call["FromEmailAddress"] == SENDER and call["ConfigurationSetName"] == CONFIG_SET
    assert call["Destination"] == {"ToAddresses": [NEW_EMAIL]}
    simple = call["Content"]["Simple"]
    assert simple["Subject"]["Data"] == si.SUBJECT
    assert "Nghiep Pham" in simple["Body"]["Text"]["Data"] and "Booking: Sales" in simple["Body"]["Text"]["Data"]
    assert "Thu &lt;Le&gt;" in simple["Body"]["Html"]["Data"]
    assert result["status"] == "sent" and result["message_id"] == "ses-msg-1"
    assert NEW_EMAIL not in json.dumps(result)
    sent = [a for a in db.audit if a["action"] == si.InviteAudit.SENT]
    assert len(sent) == 1 and sent[0]["details"]["job_id"] == ctx.job_id
    assert sent[0]["details"]["message_id"] == "ses-msg-1" and sent[0]["actor"] == si.WORKER_ACTOR


@pytest.mark.asyncio
async def test_handler_does_not_send_twice_for_the_same_job(db, mail_env):
    uid = db.add(email=NEW_EMAIL, role="content")
    ses = StubSes()
    job_id = str(uuid.uuid4())
    await job_mod.run(_ctx(db, uid, ses, job_id))
    again = await job_mod.run(_ctx(db, uid, ses, job_id))   # the reaper re-ran a job that had already sent
    assert again["status"] == "already_sent" and len(ses.calls) == 1


@pytest.mark.parametrize("setup,reason", [
    (lambda d: d.add(email=NEW_EMAIL, role="content", active=False), "account_disabled"),
    (lambda d: d.add(email=None, role="content"), "no_email"),
    (lambda d: d.add(username="admin", email="admin@adventure.asia", role="admin"), "breakglass"),
    (lambda d: d.add(email="someone@gmail.com", role="content"), "not_a_staff_address"),
    (lambda d: str(uuid.uuid4()), "account_not_found"),
])
@pytest.mark.asyncio
async def test_handler_skips_without_sending(db, mail_env, setup, reason):
    uid = setup(db)
    ses = StubSes()
    result = await job_mod.run(_ctx(db, uid, ses))
    assert ses.calls == [] and result["status"] == "skipped" and result["skip_reason"] == reason
    assert [a["details"]["reason"] for a in db.audit if a["action"] == si.InviteAudit.NOT_SENT] == [reason]


@pytest.mark.asyncio
async def test_handler_permanent_ses_error_is_non_retryable_without_the_address(db, mail_env):
    uid = db.add(email=NEW_EMAIL, role="content")
    ses = StubSes(SesClientError("MessageRejected", f"Email address is not verified: {NEW_EMAIL}"))
    with pytest.raises(registry.NonRetryable) as err:
        await job_mod.run(_ctx(db, uid, ses))
    assert "MessageRejected" in str(err.value) and NEW_EMAIL not in str(err.value)
    assert not [a for a in db.audit if a["action"] == si.InviteAudit.SENT]


@pytest.mark.asyncio
async def test_handler_transient_ses_error_is_retried(db, mail_env):
    uid = db.add(email=NEW_EMAIL, role="content")
    ses = StubSes(SesClientError("TooManyRequestsException", f"slow down {NEW_EMAIL}"))
    with pytest.raises(RuntimeError) as err:
        await job_mod.run(_ctx(db, uid, ses))
    assert not isinstance(err.value, registry.NonRetryable) and NEW_EMAIL not in str(err.value)


@pytest.mark.asyncio
async def test_handler_without_a_sender_fails_and_bad_payload_is_non_retryable(db, no_mail_env):
    uid = db.add(email=NEW_EMAIL, role="content")
    ses = StubSes()
    with pytest.raises(registry.NonRetryable):
        await job_mod.run(_ctx(db, uid, ses))
    with pytest.raises(registry.NonRetryable):
        await job_mod.run(_ctx(db, "not-a-uuid", ses))
    assert ses.calls == []


@pytest.mark.asyncio
async def test_on_terminal_records_not_sent(db):
    uid = db.add(email=NEW_EMAIL, role="content")
    await job_mod.mark_not_sent(db, {"id": "job-1", "status": "failed", "error": "SES refused the invitation (X)",
                                     "payload": {"staff_id": uid}})
    rows = [a for a in db.audit if a["action"] == si.InviteAudit.NOT_SENT]
    assert len(rows) == 1 and rows[0]["details"]["reason"] == "job_failed" and rows[0]["resource_id"] == uid
    await job_mod.mark_not_sent(db, {"id": "job-2", "status": "failed", "payload": {}})
    assert len([a for a in db.audit if a["action"] == si.InviteAudit.NOT_SENT]) == 1
