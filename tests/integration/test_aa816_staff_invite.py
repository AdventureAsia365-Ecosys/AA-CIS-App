"""AA-816 (S242) — the staff invitation e-mail against real Postgres.

Proves the SQL, not SES (a stub client stands in): the create queues its staff_invite_email job in shared.job
(idempotency key per staff id + seq) together with the row and the audit rows; the seq query reads the highest
kept key; the rate window query counts staff.invite_requested rows; the resend 429s; the worker's reads (target,
actor name, send-once guard) and its staff.invite_sent row; the staff list's invite columns.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_platform  # noqa: E402
from api.services import staff_directory as sd  # noqa: E402
from api.services import staff_invite as si  # noqa: E402
from services.jobs import staff_invite_email_job as job_mod  # noqa: E402
from shared.jobs import registry  # noqa: E402
from shared.jobs.queue import Job  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class RecordingCognito:
    def __init__(self):
        self.calls = []

    def create_user(self, email):
        self.calls.append(("create_user", email))
        return True

    def add_to_group(self, email, group):
        self.calls.append(("add", email, group))


class StubSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "it-msg-1"}


@pytest.fixture
def mail_env(monkeypatch):
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@lumiguides.it.com")
    monkeypatch.setenv("SES_CONFIGURATION_SET", "aa-email-dev-transactional")


@pytest_asyncio.fixture
async def env(mail_env):
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    tag = uuid.uuid4().hex[:8]
    owner = uuid.uuid4()
    await pool.execute(
        "INSERT INTO shared.admin_users (id, username, password_hash, role, is_active, email, is_owner, display_name) "
        "VALUES ($1, $2, NULL, 'admin', true, $3, true, 'Invite Owner')", owner, f"aa816inv-owner-{tag}",
        f"aa816inv-owner-{tag}@adventure.asia")
    emails = []
    yield pool, str(owner), tag, emails
    ids = [r["id"] for r in await pool.fetch(
        "SELECT id FROM shared.admin_users WHERE id = $1 OR lower(email) = ANY($2::text[])", owner, emails)]
    str_ids = [str(i) for i in ids]
    await pool.execute("DELETE FROM shared.job WHERE kind = $1 AND payload->>'staff_id' = ANY($2::text[])",
                       si.KIND, str_ids)
    await pool.execute("DELETE FROM acp_shared.audit_log WHERE resource_type = 'admin_user' "
                       "AND resource_id = ANY($1::text[])", str_ids)
    await pool.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", ids)
    await pool.close()


@pytest.mark.asyncio
async def test_create_queues_the_invite_and_the_worker_sends_it(env):
    pool, owner, tag, emails = env
    email = f"aa816inv.{tag}@adventure.asia"
    emails.append(email)
    res = await sd.create_staff(pool, RecordingCognito(), actor_id=owner, email=email, display_name="Invited One",
                                role="reviewer", booking_role="sales", send_invite=True)
    assert res.created and res.invite == sd.INVITE_QUEUED
    jobs = await pool.fetch("SELECT id::text AS id, kind, payload, idempotency_key, max_attempts, status, created_by "
                            "FROM shared.job WHERE kind = $1 AND payload->>'staff_id' = $2", si.KIND, res.staff_id)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["idempotency_key"] == f"staff_invite_email:{res.staff_id}:1" and job["status"] == "queued"
    assert job["max_attempts"] == 5 and job["created_by"] == f"admin:{owner}" and job["id"] == res.invite_job_id
    assert json.loads(job["payload"])["seq"] == 1
    # The create's invite waits for the Cognito steps (run_after = commit + CREATE_DELAY_S).
    delay = await pool.fetchval("SELECT extract(epoch FROM run_after - now()) FROM shared.job WHERE id = $1::uuid",
                                job["id"])
    assert si.CREATE_DELAY_S - 30 < float(delay) <= si.CREATE_DELAY_S
    actions = [r["action"] for r in await pool.fetch(
        "SELECT action FROM acp_shared.audit_log WHERE resource_type = 'admin_user' AND resource_id = $1 "
        "ORDER BY created_at, action", res.staff_id)]
    assert sorted(actions) == ["staff.created", "staff.invite_requested"]

    # The staff list: queued, then sent after the worker ran.
    async with pool.acquire() as conn:
        listed = admin_platform._staff_row(await conn.fetchrow(admin_platform._SELECT + " WHERE u.id = $1",
                                                               uuid.UUID(res.staff_id)))
    assert listed["invite_status"] == "queued" and listed["last_invite_requested_at"] is not None

    ses = StubSes()
    payload = json.loads(job["payload"])
    ctx = registry.JobContext(pool, Job(id=job["id"], kind=si.KIND, payload=payload, status="running", attempt=1,
                                        max_attempts=5, progress={}, cost_usd=0.0), resources={"ses": ses})
    # A pending Cognito row (the create's Cognito half failed): the worker waits (retryable), sends nothing.
    await pool.execute("INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
                       "VALUES ('test', $1, 'admin_user', $2, '{}'::jsonb)", sd.StaffAudit.COGNITO_PENDING,
                       res.staff_id)
    with pytest.raises(job_mod.CognitoNotReady):
        await job_mod.run(ctx)
    assert ses.calls == []
    await pool.execute("INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details, "
                       "created_at) VALUES ('test', $1, 'admin_user', $2, '{}'::jsonb, now() + interval '1 second')",
                       sd.StaffAudit.COGNITO_SYNCED, res.staff_id)
    result = await job_mod.run(ctx)
    assert result["status"] == "sent" and len(ses.calls) == 1
    assert ses.calls[0]["Destination"] == {"ToAddresses": [email]}
    assert "Invite Owner" in ses.calls[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert "Booking: Sales" in ses.calls[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
    again = await job_mod.run(ctx)
    assert again["status"] == "already_sent" and len(ses.calls) == 1
    async with pool.acquire() as conn:
        listed = admin_platform._staff_row(await conn.fetchrow(admin_platform._SELECT + " WHERE u.id = $1",
                                                               uuid.UUID(res.staff_id)))
    assert listed["invite_status"] == "sent" and listed["last_invited_at"] is not None


@pytest.mark.asyncio
async def test_resend_seq_and_rate_limit(env):
    pool, owner, tag, emails = env
    email = f"aa816inv.resend.{tag}@adventure.asia"
    emails.append(email)
    res = await sd.create_staff(pool, RecordingCognito(), actor_id=owner, email=email, display_name="R",
                                role="content", send_invite=False)
    assert res.invite == sd.INVITE_NOT_REQUESTED
    assert await pool.fetchval("SELECT count(*) FROM shared.job WHERE kind = $1 AND payload->>'staff_id' = $2",
                               si.KIND, res.staff_id) == 0
    # A kept job with seq 7 (older ones pruned): the next key is 8.
    await pool.execute("INSERT INTO shared.job (kind, payload, idempotency_key, status, finished_at) "
                       "VALUES ($1, $2::jsonb, $3, 'succeeded', now())", si.KIND,
                       json.dumps({"staff_id": res.staff_id}), si.invite_key(res.staff_id, 7))
    out = await sd.resend_invite(pool, actor_id=owner, staff_id=res.staff_id)
    key = await pool.fetchval("SELECT idempotency_key FROM shared.job WHERE id = $1::uuid", out.invite_job_id)
    assert key == si.invite_key(res.staff_id, 8)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.resend_invite(pool, actor_id=owner, staff_id=res.staff_id)
    assert err.value.status == 429 and 590 <= int(err.value.headers["Retry-After"]) <= 600
    async with pool.acquire() as conn:
        window = await conn.fetchrow(si.SQL_RECENT_REQUESTS, si.InviteAudit.REQUESTED, si.RESOURCE_TYPE,
                                     res.staff_id, float(si.DAY_S))
    assert window["n"] == 1 and window["last_at"] is not None
