"""AA-1018 — the bulk re-queue of `llm_access_denied` jobs (shared/jobs/queue.py `retry_by_reason`), the jobs-list
`reason` filter and the blocked-connection read, against a real Postgres (migrations applied by conftest)."""
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import queue  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
DENIED = ("llm_access_denied: An error occurred (AccessDeniedException) when calling the Converse operation: "
          "Error 002: Access to Bedrock models is not allowed for this account")


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.llm_provider_health")
    yield p
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.llm_provider_health")
    await p.close()


async def _failed(pool, kind, error, finished_ago=timedelta(hours=1), status="failed"):
    return await pool.fetchval(
        "INSERT INTO shared.job (kind, payload, status, attempt, max_attempts, error, finished_at) "
        "VALUES ($1, '{}'::jsonb, $2, 1, 2, $3, now() - $4::interval) RETURNING id::text",
        kind, status, error, finished_ago)


@pytest.mark.asyncio
async def test_the_reason_filter_is_a_literal_prefix(pool):
    a = await _failed(pool, "t9_write", DENIED)
    await _failed(pool, "t9_write", "RuntimeError: llm_access_denied appears later")
    await _failed(pool, "t9_write", "llm_access%denied: not the prefix")
    got = [j["id"] for j in await queue.list_jobs(pool, reason="llm_access_denied")]
    assert got == [a] and await queue.count_jobs(pool, reason="llm_access_denied") == 1
    assert await queue.count_jobs(pool, reason="llm_access%") == 1        # % is not a wildcard
    assert await queue.count_jobs(pool) == 3


@pytest.mark.asyncio
async def test_retry_by_reason_dry_run_then_apply(pool):
    since = datetime.now(timezone.utc) - timedelta(days=14)
    a = await _failed(pool, "t9_write", DENIED)
    b = await _failed(pool, "a3_atomize", DENIED)
    old = await _failed(pool, "t9_write", DENIED, finished_ago=timedelta(days=30))
    other = await _failed(pool, "t9_write", "RuntimeError: Read timed out")
    unknown = await _failed(pool, "no_such_kind_aa1018", DENIED)

    dry = await queue.retry_by_reason(pool, since=since)
    assert dry["matched"] == 3 and dry["eligible"] == 2 and dry["requeued"] == 0
    assert {h["id"]: h["held"] for h in dry["held"]} == {unknown: "kind is not registered"}
    assert await pool.fetchval("SELECT count(*) FROM shared.job WHERE status = 'failed'") == 5

    out = await queue.retry_by_reason(pool, since=since, kinds=["t9_write"], dry_run=False)
    assert out["requeued_ids"] == [a]
    row = await pool.fetchrow("SELECT status, attempt, error, finished_at, "
                              "run_after <= now() + interval '61 seconds' AS soon FROM shared.job "
                              "WHERE id = $1::uuid", a)
    assert row["status"] == "queued" and row["attempt"] == 0 and row["error"] is None
    assert row["finished_at"] is None and row["soon"] is True
    for untouched in (b, old, other, unknown):
        assert await pool.fetchval("SELECT status FROM shared.job WHERE id = $1::uuid", untouched) == "failed"
    again = await queue.retry_by_reason(pool, since=since, kinds=["t9_write"], dry_run=False)
    assert again["matched"] == 0 and again["requeued"] == 0


@pytest.mark.asyncio
async def test_a_refused_connection_holds_its_jobs(pool):
    models = [r["model_key"] for r in await pool.fetch(
        "SELECT model_key FROM shared.llm_model_catalog ORDER BY model_key LIMIT 2")]
    conn_key = await pool.fetchval(
        "SELECT connection_key FROM shared.llm_provider_connection ORDER BY connection_key LIMIT 1")
    if len(models) < 2 or conn_key is None:
        pytest.skip("registry seed rows missing in this database")
    for m in models:
        await pool.execute(
            "INSERT INTO shared.llm_provider_health (connection_key, model_key, last_error_at, last_error_class, "
            "consecutive_failures, circuit_open_until, open_streak, opened_at) "
            "VALUES ($1, $2, now(), 'access_denied', 1, now() + interval '5 minutes', 1, now())", conn_key, m)
    blocked, keys = await queue.blocked_connections(pool)
    assert blocked == {conn_key: "access_denied"} and conn_key in keys
    job = await _failed(pool, "t9_write", DENIED)
    out = await queue.retry_by_reason(pool, since=datetime.now(timezone.utc) - timedelta(days=1), dry_run=False)
    assert out["requeued"] == 0 and [h["id"] for h in out["held"]] == [job]
    assert await pool.fetchval("SELECT status FROM shared.job WHERE id = $1::uuid", job) == "failed"
