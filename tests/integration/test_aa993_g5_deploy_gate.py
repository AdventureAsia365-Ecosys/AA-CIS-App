"""AA-993 G5 (design D1.4) — the deploy gate's blocker query against a real shared.job / shared.rerun_wave (CI
Postgres). The unit tests (tests/unit/test_aa725_deploy_gate.py) check the gate logic with faked pools; here the SQL
of queue.deploy_blockers runs: only an OPEN wave and RUNNING rows of the given kinds count.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import queue  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
WAVE_NAME = "aa993-g5-test-wave"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.rerun_wave WHERE name = $1", WAVE_NAME)
    yield p
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.rerun_wave WHERE name = $1", WAVE_NAME)
    await p.close()


async def _job(pool, kind: str, status: str) -> None:
    job_id, _ = await queue.enqueue(pool, kind, {})
    if status == "running":
        await pool.execute("UPDATE shared.job SET status = 'running', started_at = now(), locked_by = 'w-g5', "
                           "locked_until = now() + interval '5 min' WHERE id = $1::uuid", job_id)


async def _wave(pool, status: str = "open") -> None:
    await pool.execute("INSERT INTO shared.rerun_wave (name, expected_tour_ids, status) VALUES ($1, $2::uuid[], $3)",
                       WAVE_NAME, [str(uuid.uuid4())], status)


async def _open_waves_elsewhere(pool) -> int:
    """Open waves another test module may have left behind (the shared CI database); counted relative to it."""
    return await pool.fetchval("SELECT count(*)::int FROM shared.rerun_wave WHERE status = 'open'")


@pytest.mark.asyncio
async def test_queued_and_running_ordinary_jobs_do_not_block(pool):
    waves = await _open_waves_elsewhere(pool)
    await _job(pool, "aa993_plain", "queued")
    await _job(pool, "aa993_plain", "running")
    await _job(pool, "aa993_blocking", "queued")  # a queued row of a blocking kind has not started: no block
    out = await queue.deploy_blockers(pool, blocking_kinds=["aa993_blocking"])
    assert out == {"open_waves": waves, "blocking_running": {}}
    assert (await queue.active_count(pool))["active"] == 3  # still reported as information


@pytest.mark.asyncio
async def test_a_running_blocking_kind_blocks(pool):
    waves = await _open_waves_elsewhere(pool)
    await _job(pool, "aa993_blocking", "running")
    await _job(pool, "aa993_plain", "running")
    out = await queue.deploy_blockers(pool, blocking_kinds=["aa993_blocking"])
    assert out == {"open_waves": waves, "blocking_running": {"aa993_blocking": 1}}
    assert await queue.deploy_blockers(pool, blocking_kinds=[]) == {"open_waves": waves, "blocking_running": {}}


@pytest.mark.asyncio
async def test_only_an_open_wave_blocks(pool):
    waves = await _open_waves_elsewhere(pool)
    await _wave(pool, "closed")
    await _wave(pool, "complete")
    assert (await queue.deploy_blockers(pool))["open_waves"] == waves
    await _wave(pool, "open")
    assert await queue.deploy_blockers(pool) == {"open_waves": waves + 1, "blocking_running": {}}
