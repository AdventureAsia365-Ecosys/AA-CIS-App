"""AA-845 — the Jev credit alert SQL against real Postgres (tables from migrations 181/209 + notifications + job).

- The 402 sweep (`_JEV_402_SWEEP_SQL`) counts every error text decide() writes for a credit outage:
  'credit_exhausted', 'credit_exhausted (breaker open)' and the raw "402 Payment Required" text, and nothing else.
- check_low_credit: estimate = top-ups − typesafe spend since the first top-up; below $3 writes one bell and queues
  one ops_alert_email job; a second check is throttled; a test check is marked and never throttles a real one.

Each test runs inside one transaction that is rolled back, so the shared test DB keeps no rows.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers import admin_llm_ops  # noqa: E402
from shared import jev_credit as jc  # noqa: E402
from shared.llm_client import decide as d  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class _OneConn:
    """A pool stand-in that hands out one connection already inside a rolled-back transaction."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *exc):
                return False

        return _Ctx()

    def __getattr__(self, name):
        return getattr(self.conn, name)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tx = c.transaction()
    await tx.start()
    # Empty the tables the estimate and the throttle read, inside the rolled-back transaction only.
    await c.execute("DELETE FROM shared.jev_credit_topup")
    await c.execute("DELETE FROM shared.notifications WHERE event_type LIKE 'platform.jev_credit.%'")
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _decision_error(c, error):
    await c.execute(
        """INSERT INTO shared.decision_log (stage, question_key, subject_key, mode, zone, error)
           VALUES ('a3_research', 'aa845_q', $1, 'enforce', 'error', $2)""",
        f"aa845:{uuid.uuid4().hex[:8]}", error)


@pytest.mark.asyncio
async def test_402_sweep_counts_every_credit_outage_text(conn):
    before = (await conn.fetchrow(admin_llm_ops._JEV_402_SWEEP_SQL))["n"]
    for text in ("credit_exhausted", "credit_exhausted (breaker open)",
                 "Client error '402 Payment Required' for url 'https://example/decide'",
                 "ReadTimeout", "no answer returned"):
        await _decision_error(conn, text)
    after = (await conn.fetchrow(admin_llm_ops._JEV_402_SWEEP_SQL))["n"]
    assert after - before == 3


async def _seed_credit(c, *, spent):
    await c.execute("INSERT INTO shared.jev_credit_topup (topped_up_on, amount_usd, created_by) "
                    "VALUES (current_date - 7, 10, 'test'), (current_date - 1, 10, 'test')")
    await c.execute(
        """INSERT INTO shared.llm_call_log (id, stage, role, model, cost_usd, quality_signal, provider, created_at)
           VALUES (gen_random_uuid(), 'a3_research', 'judge', 'jev', $1, '{}'::jsonb, 'typesafe', now()),
                  (gen_random_uuid(), 'a3_research', 'judge', 'jev', 99, '{}'::jsonb, 'typesafe',
                   now() - interval '30 days'),
                  (gen_random_uuid(), 's1_rewrite', 'writer', 'haiku', 5, '{}'::jsonb, 'bedrock', now())""",
        spent)


@pytest.mark.asyncio
async def test_low_credit_bell_email_throttle_and_test_marker(conn, monkeypatch):
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@example.com")
    pool = _OneConn(conn)
    baseline = await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'ops_alert_email'")
    await _seed_credit(conn, spent=17.5)      # 20 − 17.5 = 2.5 (the 30-day-old row predates the first top-up)

    out = await jc.check_low_credit(pool, source="itest")
    assert out["estimated_left_usd"] == pytest.approx(2.5) and out["below_threshold"] is True
    assert out["alerted"] is True and out["email_job_id"] is not None

    again = await jc.check_low_credit(pool, source="itest")
    assert again["below_threshold"] is True and again["alerted"] is False and again["email_job_id"] is None

    test = await jc.check_low_credit(pool, source="itest", threshold_usd=1000)
    assert test["alerted"] is True and test["test"] is True and test["email_job_id"] is not None

    bells = await conn.fetch("SELECT payload->>'test' AS test FROM shared.notifications "
                             "WHERE event_type = $1 ORDER BY id", jc.JEV_CREDIT_LOW_EVENT)
    assert [b["test"] for b in bells] == ["false", "true"]
    keys = await conn.fetch("SELECT idempotency_key FROM shared.job WHERE kind = 'ops_alert_email' "
                            "AND idempotency_key LIKE 'ops_alert_email:jev_credit_low:%'")
    # now() is the transaction start, so created_at ties: compare sorted (real < test).
    assert sorted(k["idempotency_key"].rsplit(":", 1)[0] for k in keys) == [
        "ops_alert_email:jev_credit_low:real", "ops_alert_email:jev_credit_low:test"]
    assert await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'ops_alert_email'") == baseline + 2

    # A test bell never throttles a real one: mark the real bell read, keep the unread test bell.
    await conn.execute("UPDATE shared.notifications SET is_read = TRUE "
                       "WHERE event_type = $1 AND payload->>'test' = 'false'", jc.JEV_CREDIT_LOW_EVENT)
    assert await conn.fetchval(jc.LOW_ALERT_RECENT_SQL, jc.JEV_CREDIT_LOW_EVENT) is None


@pytest.mark.asyncio
async def test_exhausted_alert_queues_the_email_once(conn, monkeypatch):
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@example.com")
    assert await d.maybe_alert_jev_credit(conn, source="itest") is True
    assert await d.maybe_alert_jev_credit(conn, source="itest") is False      # unread bell in 24 h
    assert await d.maybe_alert_jev_credit(conn, source="itest", test=True) is True
    rows = await conn.fetch("SELECT idempotency_key FROM shared.job WHERE idempotency_key LIKE "
                            "'ops_alert_email:jev_credit_exhausted:%'")
    assert sorted(r["idempotency_key"].rsplit(":", 1)[0] for r in rows) == [
        "ops_alert_email:jev_credit_exhausted:real", "ops_alert_email:jev_credit_exhausted:test"]
