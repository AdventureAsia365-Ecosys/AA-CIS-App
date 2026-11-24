"""AA-838 (S291) CIS 480 on real Postgres: the raw_tours index on the catalog key expression exists after the replay,
and a one-key read of gold_aa_internal.v_tour_liveness (the filter pushed into the view's GROUP BY, as the LATERAL
lookup of shared/tour_liveness.py REQUEST_TOURS_SQL does) can use it. The test DB has a few rows, so the planner
would scan anyway: seq scans are switched off inside the rolled-back transaction to prove the expression matches."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


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
async def test_480_index_is_on_the_key_expression_and_registered(conn):
    idx = await conn.fetchval(
        "SELECT pg_get_indexdef('silver_aa_internal.raw_tours_key_expr_idx'::regclass) AS ok")
    assert "COALESCE(source_group_id, tour_id)" in idx and "::text" in idx and "UNIQUE" not in idx
    assert await conn.fetchval("SELECT count(*) AS ok FROM shared.schema_versions WHERE version = '480'") == 1


@pytest.mark.asyncio
async def test_a_one_key_liveness_read_can_probe_the_index(conn):
    await conn.execute("SET LOCAL enable_seqscan = off")
    rows = await conn.fetch(
        "EXPLAIN SELECT lv.state FROM (SELECT 'k-480'::text AS key) k "
        "LEFT JOIN LATERAL (SELECT tl.state FROM gold_aa_internal.v_tour_liveness tl "
        "WHERE tl.source_id = k.key) lv ON true")
    plan = "\n".join(r[0] for r in rows)
    assert "raw_tours_key_expr_idx" in plan, plan
