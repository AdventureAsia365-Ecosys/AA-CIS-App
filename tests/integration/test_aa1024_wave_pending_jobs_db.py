"""AA-1024 G17 — the wave report's pending read on real PostgreSQL, as cis_app (the ECS exec login): a queued job of a
wave tour by a single `tour_id`, by a `tour_ids` list (s1_seo_prefetch runs before any s1_rewrite of the wave) or by a
t2_rewrite's `published_tour_id` (published_tours.id, covered by the unit test of the SQL) is found; a job of another
tour and a finished job are not.
Every test runs in one transaction that is rolled back. Invented ids and names only.
"""
import asyncio
import json
import os
import sys

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.waves import quality as wq  # noqa: E402

TOUR = "00000000-0000-4000-8000-0000000c1024"
OTHER = "00000000-0000-4000-8000-0000000c1025"


def _run(fn):
    async def go():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            tr = conn.transaction()
            await tr.start()
            try:
                return await fn(conn)
            finally:
                await tr.rollback()
        finally:
            await conn.close()
    return asyncio.run(go())


async def _as_cis_app(conn):
    if not await conn.fetchval("SELECT 1 AS ok FROM pg_roles WHERE rolname = 'cis_app'"):
        pytest.skip("role cis_app is not in this DB")
    await conn.execute("GRANT USAGE ON SCHEMA shared, gold_aa_internal TO cis_app")
    await conn.execute("GRANT SELECT, INSERT ON shared.job TO cis_app")
    await conn.execute("GRANT SELECT ON gold_aa_internal.published_tours TO cis_app")
    await conn.execute("SET LOCAL SESSION AUTHORIZATION cis_app")


async def _job(conn, kind, payload, status="queued"):
    await conn.execute("INSERT INTO shared.job (kind, payload, status, run_after) "
                       "VALUES ($1, $2::jsonb, $3, now() + interval '1 day')", kind, json.dumps(payload), status)


def test_jobs_by_tour_id_and_by_tour_ids_list_are_pending():
    async def body(conn):
        await _as_cis_app(conn)
        assert await wq.load_active_jobs(conn, [TOUR]) == []
        await _job(conn, "s1_seo_prefetch", {"tour_ids": [OTHER, TOUR], "tenant_id": "invented"})
        await _job(conn, "s1_rewrite", {"tour_id": TOUR})
        await _job(conn, "s1_rewrite", {"tour_id": OTHER})
        await _job(conn, "segment_research", {"tour_ids": "not a list"})
        await _job(conn, "a3_atomize", {"tour_id": TOUR}, status="succeeded")
        got = {(j["kind"], j["status"]): j["n"] for j in await wq.load_active_jobs(conn, [TOUR])}
        assert got == {("s1_seo_prefetch", "queued"): 1, ("s1_rewrite", "queued"): 1}
    _run(body)
