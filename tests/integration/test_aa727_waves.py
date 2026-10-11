"""AA-727 — wave report SQL + check against real Postgres (migrations applied by conftest).

Builds one wave of 6 tours, one per outcome, and asserts the report counts and the
open → incomplete → (no second bell) transition.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.waves import report as wr  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = wr.MASTER_TENANT_ID


async def _no_fk(conn):
    # setup (per acquire): the fixture rows skip their FK parents (content, batches, tenants).
    await conn.execute("SET session_replication_role = replica")


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3, setup=_no_fk)
    yield p
    await p.close()


async def _raw(conn, name, status="active", itin="Day 1: " + "x" * 800):
    tid = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO silver_aa_internal.raw_tours
               (tour_id, tenant_id, batch_id, src_name, country, src_itineraries, source_status)
           VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'Laos', $5, $6::silver_aa_internal.source_status_enum)""",
        tid, MASTER, str(uuid.uuid4()), name, itin, status)
    return tid


async def _job(conn, kind, tour_id, status, created_at="now()"):
    return await conn.fetchval(
        f"""INSERT INTO shared.job (kind, payload, status, created_at, finished_at)
            VALUES ($1, $2::jsonb, $3, {created_at}, CASE WHEN $3 IN ('queued','running') THEN NULL ELSE now() END)
            RETURNING id::text""",
        kind, json.dumps({"tour_id": tour_id}), status)


@pytest.mark.asyncio
async def test_wave_report_and_check(pool):
    async with pool.acquire() as conn:
        t_master = await _raw(conn, "A master")
        t_review = await _raw(conn, "B review", itin="short")
        t_trash = await _raw(conn, "C trash", status="trashed")
        t_failed = await _raw(conn, "D failed")
        t_cancel = await _raw(conn, "E interrupted")
        t_none = await _raw(conn, "F not started")
        t_outside = await _raw(conn, "G not in wave")
        ids = [t_master, t_review, t_trash, t_failed, t_cancel, t_none]

        # a job of an expected tour created BEFORE the wave must not count
        await _job(conn, "s1_rewrite", t_none, "succeeded", "now() - interval '2 days'")
        wave_id = await conn.fetchval(
            "INSERT INTO shared.rerun_wave (name, country, expected_tour_ids) VALUES ('Laos', 'Laos', $1::uuid[]) "
            "RETURNING id::text", ids)

        j_master = await _job(conn, "s1_rewrite", t_master, "succeeded")
        await _job(conn, "a3_atomize", t_master, "succeeded")
        j_review = await _job(conn, "s1_rewrite", t_review, "succeeded")
        await _job(conn, "s1_rewrite", t_failed, "failed")
        await _job(conn, "s1_rewrite", t_cancel, "cancelled")
        await _job(conn, "s1_rewrite", t_outside, "succeeded")       # not in the expected set

        await conn.execute(
            """INSERT INTO gold_aa_internal.published_tours
                   (tour_id, generated_content_id, tenant_id, aa_name, master_status, published_at)
               VALUES ($1::uuid, $2::uuid, $3::uuid, 'A', 'active', now())""",
            t_master, str(uuid.uuid4()), MASTER)
        await conn.execute(
            """INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id,
                                                            failure_summary, score_overall, review_status)
               VALUES ($1::uuid, $2::uuid, $3::uuid, 'Low quality', 6.2, 'pending')""",
            t_review, str(uuid.uuid4()), MASTER)
        # Jev: a real verdict on the master tour's job, only an error on the review tour's job
        for job_id, err in ((j_master, None), (j_review, "timeout")):
            await conn.execute(
                """INSERT INTO shared.decision_log (stage, question_key, subject_key, job_id, mode, zone, error)
                   VALUES ('s1_grounding', 'a1_claim_supported', 's', $1::uuid, 'enforce',
                           CASE WHEN $2::text IS NULL THEN 'accept' ELSE 'error' END, $2)""", job_id, err)
        await conn.execute(
            """INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, job_id, provider)
               VALUES ('s1_generate', 'writer', 'haiku', 0.02, '{}', $1::uuid, 'bedrock')""", j_master)
        await conn.execute(
            """INSERT INTO shared.dfs_call_log (tour_id, endpoint, cost_usd, cache_hit, fetched_live)
               VALUES ($1::uuid, 'keywords', 0.01, false, true)""", t_master)

        rep = await wr.report(conn, wave_id)
        assert rep["expected"] == 6
        assert rep["columns"] == {"s1_ok": 2, "to_master": 1, "to_review": 1, "atomized": 1}
        states = {r["name"]: r["state"] for r in rep["tours"]}
        assert states == {"A master": "master", "B review": "review_pending", "C trash": "trashed",
                          "D failed": "failed", "E interrupted": "interrupted", "F not started": "not_started"}
        assert rep["review"][0]["review"]["failure_class"] == "raw_insufficient"
        assert rep["jev_coverage"]["tours"] == 1
        assert rep["cost"]["llm_usd"] == 0.02 and rep["cost"]["dfs_usd"] == 0.01
        assert rep["jobs_finished"] is True and rep["complete"] is False

        before = await conn.fetchval("SELECT count(*) FROM shared.notifications WHERE entity_id = $1", wave_id)
        first = await wr.check(conn, wave_id)
        assert first["status"] == "incomplete"
        second = await wr.check(conn, wave_id)              # already incomplete: no second bell
        assert second["status"] == "incomplete"
        after = await conn.fetchval("SELECT count(*) FROM shared.notifications WHERE entity_id = $1", wave_id)
        assert after - before == 1


@pytest.mark.asyncio
async def test_wave_completes_when_every_tour_is_terminal(pool):
    async with pool.acquire() as conn:
        t = await _raw(conn, "Only one")
        wave_id = await conn.fetchval(
            "INSERT INTO shared.rerun_wave (name, expected_tour_ids) VALUES ('one', $1::uuid[]) "
            "RETURNING id::text", [t])
        job = await _job(conn, "s1_rewrite", t, "running")
        assert (await wr.check(conn, wave_id))["status"] == "open"          # still running
        await conn.execute("UPDATE shared.job SET status = 'succeeded' WHERE id = $1::uuid", job)
        await conn.execute(
            """INSERT INTO gold_aa_internal.published_tours
                   (tour_id, generated_content_id, tenant_id, aa_name, master_status, published_at)
               VALUES ($1::uuid, $2::uuid, $3::uuid, 'X', 'active', now())""", t, str(uuid.uuid4()), MASTER)
        assert (await wr.check(conn, wave_id))["status"] == "complete"
