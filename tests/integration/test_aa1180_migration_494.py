"""AA-1180 — CIS 494 on a real Postgres (conftest replays every migration): shared.ai_usage_hourly.cost_usd is
numeric(20,8), at least the widest scale of the source ledgers, a sub-micro-dollar cost is stored exactly, and a
re-run of 494 changes nothing. Runs in a transaction that is rolled back; invented values only."""
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M494 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "494_aa1180_usage_rollup_cost_scale.sql")
TYPE_SQL = """
    SELECT numeric_precision AS p, numeric_scale AS s, is_nullable AS nullable, column_default AS dflt
      FROM information_schema.columns
     WHERE table_schema = $1 AND table_name = $2 AND column_name = 'cost_usd'
"""
LEDGERS = (("shared", "llm_call_log"), ("shared", "decision_log"), ("shared", "dfs_call_log"),
           ("geo", "geocode_call"))
HOUR = datetime(2024, 3, 9, 4, tzinfo=timezone.utc)   # far outside every sliding window; rolled back anyway


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


@pytest.mark.asyncio
async def test_494_registered(conn):
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version IN ('494')") == 1


@pytest.mark.asyncio
async def test_cost_is_numeric_20_8_not_null_default_0(conn):
    row = await conn.fetchrow(TYPE_SQL, "shared", "ai_usage_hourly")
    assert (row["p"], row["s"], row["nullable"]) == (20, 8, "NO")
    assert row["dflt"].startswith("0")


@pytest.mark.asyncio
async def test_scale_covers_every_source_ledger(conn):
    rolled = await conn.fetchrow(TYPE_SQL, "shared", "ai_usage_hourly")
    for schema, table in LEDGERS:
        src = await conn.fetchrow(TYPE_SQL, schema, table)
        assert src is not None, f"{schema}.{table}"
        assert rolled["s"] >= src["s"], f"{schema}.{table} has scale {src['s']}"
        assert rolled["p"] - rolled["s"] >= src["p"] - src["s"], f"{schema}.{table} integer digits"


@pytest.mark.asyncio
async def test_a_sub_micro_dollar_cost_is_stored_exactly(conn):
    await conn.execute(
        """INSERT INTO shared.ai_usage_hourly (hour, source, app, provider, status, traffic, cost_usd, latency_hist)
           VALUES ($1, 'jev', 'cis', 'probe-provider', 'ok', 'live', $2, array_fill(0, ARRAY[20]))""",
        HOUR, Decimal("0.00000078"))
    got = await conn.fetchval("SELECT cost_usd FROM shared.ai_usage_hourly WHERE hour = $1 AND source = 'jev' "
                              "AND provider = 'probe-provider'", HOUR)
    assert got == Decimal("0.00000078")


@pytest.mark.asyncio
async def test_rerun_of_494_changes_nothing(conn):
    with open(M494, encoding="utf-8") as f:
        sql = f.read()
    # The file has its own BEGIN/COMMIT: strip them so the re-run stays inside the test's rolled-back transaction.
    body = sql.replace("BEGIN;", "").replace("COMMIT;", "")
    await conn.execute(body)
    row = await conn.fetchrow(TYPE_SQL, "shared", "ai_usage_hourly")
    assert (row["p"], row["s"]) == (20, 8)
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version IN ('494')") == 1
