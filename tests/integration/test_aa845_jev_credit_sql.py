"""AA-845 — the Jev credit alert SQL against real Postgres (tables from migrations 181/209 + notifications + job).

- The 402 sweep (`_JEV_402_SWEEP_SQL`) counts every error text decide() writes for a credit outage:
  'credit_exhausted', 'credit_exhausted (breaker open)' and the raw "402 Payment Required" text, and nothing else.
  Only `n_open` raises the bell: real 402s (not breaker-open skips) after the latest top-up and the latest
  successful canary ($1). S246 review B1: a top-up the day before must not re-alert at the next 06:15 sweep.
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


async def _decision_error(c, error, hours_ago=0.0):
    await c.execute(
        """INSERT INTO shared.decision_log (stage, question_key, subject_key, mode, zone, error, created_at)
           VALUES ('a3_research', 'aa845_q', $1, 'enforce', 'error', $2, now() - make_interval(secs => $3))""",
        f"aa845:{uuid.uuid4().hex[:8]}", error, hours_ago * 3600.0)


# Inside the rolled-back transaction only: the sweep counts every recent error row in the shared test DB.
_CLEAR_RECENT_ERRORS = ("DELETE FROM shared.decision_log "
                        "WHERE zone = 'error' AND created_at >= now() - interval '25 hours'")


async def _sweep(c, canary_ok_at=None):
    return await c.fetchrow(admin_llm_ops._JEV_402_SWEEP_SQL, canary_ok_at)


@pytest.mark.asyncio
async def test_402_sweep_counts_every_credit_outage_text(conn):
    await conn.execute(_CLEAR_RECENT_ERRORS)
    for text in ("credit_exhausted", "credit_exhausted (breaker open)",
                 "Client error '402 Payment Required' for url 'https://example/decide'",
                 "ReadTimeout", "no answer returned"):
        await _decision_error(conn, text, hours_ago=1)
    row = await _sweep(conn)
    assert row["n"] == 3                     # no top-up, no canary: the two real 402s are open
    assert row["n_open"] == 2


@pytest.mark.asyncio
async def test_402_sweep_rows_before_a_topup_or_canary_are_not_open(conn):
    """B1: 402 at D 14:00 (16 h ago), breaker-open skips until 14:15, top-up recorded at 14:05. The next morning's
    sweep still counts the rows but raises nothing; a new real 402 after the top-up is open again."""
    await conn.execute(_CLEAR_RECENT_ERRORS)
    await _decision_error(conn, "credit_exhausted", hours_ago=16)
    await _decision_error(conn, "credit_exhausted (breaker open)", hours_ago=15.8)   # after the top-up
    await conn.execute("INSERT INTO shared.jev_credit_topup (topped_up_on, amount_usd, created_by, created_at) "
                       "VALUES (current_date, 10, 'test', now() - interval '15.9 hours')")
    row = await _sweep(conn)
    assert row["n"] == 2 and row["n_open"] == 0

    await _decision_error(conn, "credit_exhausted", hours_ago=2)      # credit ran out again after the top-up
    assert (await _sweep(conn))["n_open"] == 1
    # ... but a successful canary after it (this run's) proves the credit is back.
    canary_ok = await conn.fetchval("SELECT now() - interval '1 hour'")
    assert (await _sweep(conn, canary_ok))["n_open"] == 0
    # A canary older than the newest 402 does not hide it.
    old_canary = await conn.fetchval("SELECT now() - interval '3 hours'")
    assert (await _sweep(conn, old_canary))["n_open"] == 1


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

    roles = await conn.fetch("SELECT payload->>'test' AS test, target_roles FROM shared.notifications "
                             "WHERE event_type = $1 ORDER BY id", jc.JEV_CREDIT_LOW_EVENT)
    assert [(r["test"], list(r["target_roles"])) for r in roles] == [
        ("false", ["admin", "content"]), ("true", ["admin"])]

    # The throttle ignores the shared read flag (S246 review): a read real bell still blocks the next one.
    await conn.execute("UPDATE shared.notifications SET is_read = TRUE WHERE event_type = $1", jc.JEV_CREDIT_LOW_EVENT)
    assert (await jc.check_low_credit(pool, source="itest"))["alerted"] is False
    # A test bell never throttles a real one: without the real bell, only the test bell is left.
    await conn.execute("DELETE FROM shared.notifications WHERE event_type = $1 AND payload->>'test' = 'false'",
                       jc.JEV_CREDIT_LOW_EVENT)
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
