"""AA-1024 G17 — the wave start gate on real PostgreSQL, as cis_app (the API's login): the newest wave's window and
expected set are read, the wave report's queries run against the real schema, and a queued job of one of its tours makes
every flag pending (409 at the wave start); with no A1 version and no active job the gate does not apply.
Every test runs in one transaction that is rolled back. Invented ids and names only.
"""
import asyncio
import datetime as dt
import json
import os
import sys

import asyncpg
import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_waves  # noqa: E402
from services.waves import quality as wq  # noqa: E402

TOUR = "00000000-0000-4000-8000-0000000c1024"


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
    await conn.execute("GRANT USAGE ON SCHEMA shared, silver_aa_internal, gold_aa_internal, acp_contract TO cis_app")
    await conn.execute("GRANT SELECT, INSERT ON shared.rerun_wave, shared.job TO cis_app")
    await conn.execute("GRANT SELECT ON silver_aa_internal.generated_content, silver_aa_internal.raw_tours, "
                       "silver_aa_internal.quality_scores, silver_aa_internal.review_queue, "
                       "gold_aa_internal.published_tours, acp_contract.atomize_day_fingerprint, "
                       "acp_contract.tour_atoms TO cis_app")
    await conn.execute("SET LOCAL SESSION AUTHORIZATION cis_app")


async def _wave(conn) -> dt.datetime:
    # newer than any wave already in the DB, so the gate reads this one
    created = await conn.fetchval("SELECT greatest(now(), coalesce(max(created_at), now())) + interval '1 hour' AS ok "
                                  "FROM shared.rerun_wave")
    await conn.execute("INSERT INTO shared.rerun_wave (name, expected_tour_ids, created_at) "
                       "VALUES ('zz invented gate wave', $1::uuid[], $2)", [TOUR], created)
    return created


def test_a_wave_without_versions_or_jobs_does_not_block():
    async def body(conn):
        await _as_cis_app(conn)
        created = await _wave(conn)
        out = await wq.previous_wave_gate(conn, now=created + dt.timedelta(hours=1))
        assert out["wave"]["name"] == "zz invented gate wave"
        assert out["applies"] is False and out["blocks"] is False
    _run(body)


def test_a_queued_job_of_the_waves_tour_makes_the_flags_pending_and_refuses_the_next_wave():
    async def body(conn):
        await _as_cis_app(conn)
        created = await _wave(conn)
        await conn.execute("INSERT INTO shared.job (kind, payload, run_after) "
                           "VALUES ('s1_rewrite', $1::jsonb, now() + interval '1 day')", json.dumps({"tour_id": TOUR}))
        out = await wq.previous_wave_gate(conn, now=created + dt.timedelta(hours=1))
        assert out["blocks"] is True and out["exit_code"] == wq.EXIT_PENDING
        assert {j["kind"] for j in out["active_jobs"]} == {"s1_rewrite"}
        with pytest.raises(HTTPException) as e:
            await admin_waves._stop_gate(conn, admin_waves.WaveCreate(name="zz next", tour_ids=[TOUR]))
        assert e.value.status_code == 409 and "pending" in e.value.detail["error"]
    _run(body)
