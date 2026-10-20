"""AA-913 — the alert recipient code against real Postgres (asyncpg, the driver the app uses).

The unit tests use an in-memory fake that matches the SQL by text; here every statement of shared/alert_recipients.py
and api/services/alert_recipients.py runs for real (lesson "a green unit test does not prove the query runs") on
shared.alert_recipient (migration 251), acp_shared.audit_log (hash-chain trigger, CIS 248) and shared.job: add (advisory
lock, insert, audit), reconcile (FOR UPDATE, DISTINCT ON, updates, audit), the page reads (joins to the staff
directory), remove, the rate limits over the audit log, the test e-mail job, and the ops recipients. SNS is the stub of
tests/unit/aa913_fakes.py.
"""
import os
import sys
import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.services import alert_recipients as svc  # noqa: E402
from shared import alert_recipients as ar  # noqa: E402
from tests.unit.aa913_fakes import BOOTSTRAP, TOPIC_ARN, StubSns, sub_arn  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
DOMAIN = "zz913sql.example.com"


def _email(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:6]}@{DOMAIN}"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALERTS_TOPIC_ARN", TOPIC_ARN)
    monkeypatch.setenv("ALERT_BOOTSTRAP_EMAIL", BOOTSTRAP)
    monkeypatch.setenv("ALERT_EMAIL_TO", f"fallback@{DOMAIN}")
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@lumiguides.it.com")
    ar.clear_cache()


@pytest_asyncio.fixture
async def env():
    """A pool and the actor ('admin:<id>') of a throwaway admin row (asyncpg's Pool takes no attributes)."""
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    admin_id = await p.fetchval(
        "INSERT INTO shared.admin_users (username, role, is_active, email, display_name) "
        "VALUES ($1, 'admin', true, $2, 'AA913 Admin') RETURNING id::text",
        f"zz913-{uuid.uuid4().hex[:6]}", f"zz913-{uuid.uuid4().hex[:6]}@adventure.asia")
    actor = f"admin:{admin_id}"
    try:
        yield SimpleNamespace(pool=p, actor=actor)
    finally:
        await p.execute("DELETE FROM shared.alert_recipient WHERE email LIKE $1", f"%@{DOMAIN}")
        await p.execute("DELETE FROM shared.job WHERE kind = 'ops_alert_email' AND created_by = $1", actor)
        await p.execute("DELETE FROM shared.admin_users WHERE id = $1::uuid", admin_id)
        await p.close()


async def _audit(pool, rid):
    rows = await pool.fetch("SELECT action, actor, details FROM acp_shared.audit_log WHERE resource_type = "
                            "'alert_recipient' AND resource_id = $1 ORDER BY chain_seq NULLS LAST, created_at", rid)
    return [r["action"] for r in rows], rows


async def test_add_confirm_reconcile_page_remove(env):
    pool, actor = env.pool, env.actor
    sns = StubSns(subs={BOOTSTRAP: sub_arn("boot")})
    email = _email("cycle")
    added = await svc.add(pool, sns, actor=actor, email=email.upper(), note=" on-call ")
    rec = added["recipient"]
    assert rec["status"] == "pending" and rec["email"] == email and rec["created_by"] == "AA913 Admin"
    actions, _ = await _audit(pool, rec["id"])
    assert actions == ["alert_recipient.added", "alert_recipient.confirmation_sent"]

    # duplicate refused by the real SQL (and by the partial unique index behind it)
    with pytest.raises(svc.AlertRecipientError) as err:
        await svc.add(pool, sns, actor=actor, email=email, note=None)
    assert err.value.status == 409

    # resend right away: the add's confirmation counts (rate limit read from the real audit log)
    with pytest.raises(svc.AlertRecipientError) as err:
        await svc.resend(pool, sns, actor=actor, recipient_id=rec["id"])
    assert err.value.status == 429 and int(err.value.headers["Retry-After"]) > 0

    sns.confirm(email)
    result = await ar.reconcile(pool, sns, ar.topic_config(), actor=actor)
    assert any(c["email"] == email and c["to"] == "confirmed" for c in result.changes)

    body = await svc.page(pool, sns, refresh=True)
    mine = [r for r in body["recipients"] if r["email"] == email]
    assert mine and mine[0]["status"] == "confirmed" and mine[0]["has_subscription"] is True
    assert body["bootstrap"]["state"] == "confirmed"
    assert "arn:" not in repr(body)

    to, error = await ar.ops_recipients(pool)
    assert error is None and email in to and to[-1] == f"fallback@{DOMAIN}"

    removed = await svc.remove(pool, sns, actor=actor, recipient_id=rec["id"])
    assert removed["sns"] == "unsubscribed" and removed["recipient"]["status"] == "removed"
    assert removed["recipient"]["removed_by"] == "AA913 Admin"
    assert email not in sns.subs
    actions, _ = await _audit(pool, rec["id"])
    assert actions[-1] == "alert_recipient.removed"
    body = await svc.page(pool, sns, refresh=True)
    assert any(h["email"] == email for h in body["history"])
    assert email not in (await ar.ops_recipients(pool))[0]

    # the address can be added again (a new row next to the removed one)
    again = await svc.add(pool, sns, actor=actor, email=email, note=None)
    assert again["recipient"]["id"] != rec["id"]


async def test_reconcile_expires_unsubscribes_and_reunsubscribes(env):
    pool, actor = env.pool, env.actor
    old = datetime.now(timezone.utc) - timedelta(days=4)
    expired = _email("expired")
    unsub = _email("unsub")
    late = _email("late")
    async with pool.acquire() as conn:
        exp_id = await conn.fetchval(
            "INSERT INTO shared.alert_recipient (email, status, created_by, last_confirmation_sent_at, "
            "subscription_arn) VALUES ($1, 'pending', $2, $3, $4) RETURNING id::text",
            expired, actor, old, sub_arn(expired))
        uns_id = await conn.fetchval(
            "INSERT INTO shared.alert_recipient (email, status, created_by, confirmed_at, subscription_arn) "
            "VALUES ($1, 'confirmed', $2, now(), $3) RETURNING id::text", unsub, actor, sub_arn(unsub))
        late_id = await conn.fetchval(
            "INSERT INTO shared.alert_recipient (email, status, created_by, removed_reason, removed_at, removed_by) "
            "VALUES ($1, 'removed', $2, 'admin', now(), $2) RETURNING id::text", late, actor)
    sns = StubSns(subs={BOOTSTRAP: sub_arn("boot"), late: sub_arn(late)})
    result = await ar.reconcile(pool, sns, ar.topic_config())
    rows = {r["id"]: r for r in await pool.fetch(
        "SELECT id::text AS id, status, removed_reason, removed_by, last_seen_state FROM shared.alert_recipient "
        "WHERE id = ANY($1::uuid[])", [exp_id, uns_id, late_id])}
    assert (rows[exp_id]["status"], rows[exp_id]["removed_reason"]) == ("removed", "expired")
    assert (rows[uns_id]["status"], rows[uns_id]["removed_reason"]) == ("removed", "unsubscribed")
    assert rows[uns_id]["removed_by"] == "reconcile" and rows[uns_id]["last_seen_state"] == "missing"
    assert late in result.unsubscribed and late not in sns.subs
    actions, _ = await _audit(pool, late_id)
    assert actions == ["alert_recipient.reconciled"]


async def test_test_email_job_and_rate_limit(env):
    pool, actor = env.pool, env.actor
    out = await svc.send_test(pool, actor=actor, requested_by="AA913 Admin")
    assert out["recipients_count"] >= 1
    job = await pool.fetchrow(
        "SELECT kind, payload::text AS payload, idempotency_key FROM shared.job WHERE id = $1::uuid", out["job_id"])
    assert job["kind"] == "ops_alert_email" and '"template": "test"' in job["payload"]
    assert job["idempotency_key"].startswith("ops_alert_email:test:")
    with pytest.raises(svc.AlertRecipientError) as err:
        await svc.send_test(pool, actor=actor, requested_by=None)
    assert err.value.status == 429


async def test_adds_today_counts_real_audit_rows(env):
    pool, actor = env.pool, env.actor
    async with pool.acquire() as conn:
        before = await svc.adds_today(conn)
    await svc.add(pool, StubSns(subs={}), actor=actor, email=_email("count"), note=None)
    async with pool.acquire() as conn:
        assert await svc.adds_today(conn) == before + 1
