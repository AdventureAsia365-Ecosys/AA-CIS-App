"""AA-900 — api/services/llm_availability.read() on real Postgres (one transaction, rolled back).

The unit tests build the response from dict rows; here the two SELECTs run against real shared.llm_call_log and
shared.job rows. The window sits in 2099 so rows of other tests never fall inside it.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.services import llm_availability as av  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
NOW = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _call(conn, *, provider, account, role, at, signal="{}"):
    await conn.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, created_at, provider, account) "
        "VALUES ('zz900_stage', $1, 'zz900-model', 0.001, $2::jsonb, $3, $4, $5)",
        role, signal, at, provider, account)


async def _job(conn, *, kind, status, error, at):
    return await conn.fetchval(
        "INSERT INTO shared.job (kind, payload, status, attempt, max_attempts, error, created_at, updated_at, "
        "finished_at) VALUES ($1, '{}'::jsonb, $2, 1, 1, $3, $4::timestamptz, $4::timestamptz, "
        "CASE WHEN $2::text = 'failed' THEN $4::timestamptz END) "
        "RETURNING id::text",
        kind, status, error, at)


@pytest.mark.asyncio
async def test_read_reports_blocked_from_real_rows(conn):
    await _call(conn, provider="bedrock-satellite", account="acc3", role="writer", at=NOW - timedelta(hours=3))
    await _call(conn, provider="bedrock-satellite", account="acc3", role="judge", at=NOW - timedelta(hours=2),
                signal='{"billed_unusable": true}')
    await _call(conn, provider="bedrock", account="acc2", role="embed", at=NOW - timedelta(minutes=5))
    await _call(conn, provider="typesafe", account=None, role="validate", at=NOW - timedelta(minutes=1))
    job_id = await _job(conn, kind="zz900_writer", status="failed", at=NOW - timedelta(minutes=30),
                        error="llm_access_denied: All models in the route failed: Error 002: Access to Bedrock "
                              "models is not allowed for this account")
    await _job(conn, kind="zz900_writer", status="failed", at=NOW - timedelta(minutes=20),
               error="NonRetryable: payload needs tour_id")
    await _job(conn, kind="zz900_writer", status="succeeded", at=NOW - timedelta(minutes=10), error=None)

    out = await av.read(conn, hours=24, now=NOW)

    assert out["status"] == "blocked"
    assert out["cause"]["job_id"] == job_id and out["cause"]["error_class"] == "access_denied"
    assert out["last_ok_at"] == (NOW - timedelta(hours=3)).isoformat()   # the billed_unusable row is not an answer
    rows = {(p["provider"], p["account"], p["embed"]): p for p in out["providers"]}
    assert ("typesafe", None, False) not in rows
    text = rows[("bedrock-satellite", "acc3", False)]
    assert (text["calls"], text["ok_calls"]) == (2, 1)
    assert rows[("bedrock", "acc2", True)]["ok_calls"] == 1
    assert [f["error_class"] for f in out["failures"]] == ["access_denied"]


@pytest.mark.asyncio
async def test_read_is_ok_once_a_call_is_answered_after_the_refusal(conn):
    await _job(conn, kind="zz900_writer", status="failed", at=NOW - timedelta(hours=1),
               error="llm_access_denied: Error 002: Access to Bedrock models is not allowed for this account")
    await _call(conn, provider="bedrock-satellite", account="acc3", role="writer", at=NOW - timedelta(minutes=2))
    out = await av.read(conn, hours=24, now=NOW)
    assert out["status"] == "ok" and out["cause"] is None
