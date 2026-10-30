"""AA-1002 (gateway slice S6) — migration 312 (llm_call_log cache split + pricing_mode, llm_call_daily sums) against
real Postgres.

conftest applies every migration; these tests run the file again directly (errors NOT swallowed, lesson S231): it is
idempotent, the CHECKs refuse a bad pricing_mode / negative cache tokens, the gateway's S6 INSERT lands, and the
rollup's S6 statement sums the cache and counts estimate rows. Every test removes its own rows.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared.llm_client import call_log  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "312_llm_call_log_cache_pricing_mode.sql")
STAGE = "zz_aa1002_s6"
DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.llm_call_log WHERE stage = %s", (STAGE,))
        c.execute("DELETE FROM shared.llm_call_daily WHERE stage = %s", (STAGE,))
        conn.close()


def test_idempotent_and_registered(cur):
    cur.execute(open(MIGRATION, encoding="utf-8").read())
    cur.execute(open(MIGRATION, encoding="utf-8").read())
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '312'")
    assert cur.fetchone()
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name IN ('llm_call_log', 'llm_call_daily') AND column_name IN "
                "('cache_read_tokens', 'cache_write_tokens', 'pricing_mode', 'estimate_calls')")
    assert len(cur.fetchall()) == 6


@pytest.mark.parametrize("column, value", [("pricing_mode", "free"), ("pricing_mode", "batch+catalog"),
                                           ("cache_read_tokens", -1), ("cache_write_tokens", -5)])
def test_checks_refuse_bad_values(cur, column, value):
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(f"INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, {column}) "
                    "VALUES (%s, 'judge', 'm', '{}', %s)", (STAGE, value))


def test_the_gateway_s6_insert_and_the_rollup(cur):
    async def write():
        conn = await asyncpg.connect(DSN)
        try:
            base = (None, STAGE, "writer", "sonnet-5-5", 5000, 100, 0.01, "{}", None, None, "end_turn", "acc3",
                    False, "bedrock", None)
            await call_log._execute_rows(conn, call_log._INSERT_SQL, [base, base],
                                         [(4000, 0, "catalog"), (None, None, "estimate")], many=True)
        finally:
            await conn.close()
    asyncio.run(write())
    old = datetime.now(timezone.utc) - timedelta(days=400)
    cur.execute("UPDATE shared.llm_call_log SET created_at = %s WHERE stage = %s", (old, STAGE))
    cur.execute("SELECT cache_read_tokens, pricing_mode FROM shared.llm_call_log WHERE stage = %s ORDER BY 2", (STAGE,))
    assert cur.fetchall() == [(4000, "catalog"), (None, "estimate")]

    async def roll():
        from services.retention import rollups
        conn = await asyncpg.connect(DSN)
        try:
            # only our (backdated) day is unrolled before this cutoff in a fresh CI database
            return await rollups.write(conn, "llm_call_daily", old + timedelta(days=1))
        finally:
            await conn.close()
    asyncio.run(roll())
    cur.execute("SELECT calls, cache_read_tokens, cache_write_tokens, estimate_calls FROM shared.llm_call_daily "
                "WHERE stage = %s", (STAGE,))
    assert cur.fetchall() == [(2, 4000, 0, 1)]
