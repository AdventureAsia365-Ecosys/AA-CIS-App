"""AA-687 — Jobs page backend against a real Postgres (migrations 174/177/178 applied by conftest):
worker liveness rows (shared.job_worker), reaper totals, queue depth, and a job's LLM calls."""
import asyncio
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs import registry  # noqa: E402
from shared.jobs.worker import Worker  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.job_worker")
    yield p
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.job_worker")
    await p.execute("DELETE FROM shared.llm_call_log WHERE stage = 'adhoc_aa687'")
    await p.close()


@pytest.fixture
def slow_kind(monkeypatch):
    monkeypatch.setattr(registry, "_KINDS", {})
    started = asyncio.Event()

    @registry.job_kind("slow_job", concurrency=2, expected_seconds=5)
    async def slow_job(ctx):
        started.set()
        await asyncio.sleep(30)

    return started


@pytest.mark.asyncio
async def test_worker_registers_beats_and_marks_itself_stopped(pool, slow_kind):
    job_id, _ = await registry.enqueue(pool, "slow_job", {})
    worker = Worker(pool, poll_seconds=0.1, heartbeat_seconds=0.2, grace_seconds=0.2)
    worker.beat_every_seconds = 0.1
    runner = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(slow_kind.wait(), 5)
        await asyncio.sleep(0.4)  # at least one beat after the claim
        health = await queue.worker_health(pool)
        w = next(x for x in health["workers"] if x["worker_id"] == worker.worker_id)
        assert w["alive"] is True and w["stopped_at"] is None
        assert w["running_jobs"] == 1 and w["caps"] == {"slow_job": 2} and w["max_parallel"] == 4
        running = [r for r in health["running"] if r["worker_id"] == worker.worker_id]
        assert running == [{"worker_id": worker.worker_id, "kind": "slow_job", "n": 1,
                            "oldest_started_at": running[0]["oldest_started_at"]}]
    finally:
        await worker.shutdown()
        await runner
    row = await pool.fetchrow("SELECT stopped_at, running_jobs FROM shared.job_worker WHERE worker_id = $1",
                              worker.worker_id)
    assert row["stopped_at"] is not None and row["running_jobs"] == 0
    health = await queue.worker_health(pool)
    assert next(x for x in health["workers"] if x["worker_id"] == worker.worker_id)["alive"] is False
    # the released job is back in the queue and counted in the depth
    assert {"kind": "slow_job", "n": 1, "ready": 1} == {
        k: v for k, v in health["queued"][0].items() if k in ("kind", "n", "ready")}
    assert (await queue.get(pool, job_id))["status"] == "queued"


@pytest.mark.asyncio
async def test_reaper_work_is_recorded_on_the_worker_row(pool, monkeypatch):
    monkeypatch.setattr(registry, "_KINDS", {})
    job_id, _ = await queue.enqueue(pool, "orphan", {})
    await queue.claim(pool, "dead-worker", {"orphan": 1}, 60)
    await pool.execute("UPDATE shared.job SET locked_until = now() - interval '1 second' WHERE id = $1::uuid",
                       job_id)
    worker = Worker(pool, poll_seconds=0.1, reap_every_seconds=0.1)
    runner = asyncio.create_task(worker.run())
    try:
        for _ in range(50):
            row = await pool.fetchrow(
                "SELECT reaped_requeued, last_reaped FROM shared.job_worker WHERE worker_id = $1",
                worker.worker_id)
            if row and row["reaped_requeued"]:
                break
            await asyncio.sleep(0.1)
    finally:
        await worker.shutdown()
        await runner
    assert row["reaped_requeued"] == 1
    assert job_id in row["last_reaped"]


@pytest.mark.asyncio
async def test_worker_register_prunes_rows_older_than_a_week(pool):
    await pool.execute(
        "INSERT INTO shared.job_worker (worker_id, host, max_parallel, last_seen_at) "
        "VALUES ('old', 'h', 4, now() - interval '8 days'), ('recent', 'h', 4, now() - interval '1 day')")
    await queue.worker_register(pool, "new", "h", 4, {"k": 1})
    ids = {r["worker_id"] for r in await pool.fetch("SELECT worker_id FROM shared.job_worker")}
    assert ids == {"recent", "new"}


@pytest.mark.asyncio
async def test_llm_calls_lists_the_job_calls_with_totals_and_split(pool):
    from shared.llm_client import call_log
    job_id, _ = await queue.enqueue(pool, "k", {"tour_id": "t-1"})
    with call_log.bind_job(job_id):
        for stage, cost in (("adhoc_aa687", 0.1), ("adhoc_aa687", 0.2)):
            await call_log.record_call_with_pool(
                pool, stage=stage, role="writer", model="haiku-4-5", tokens_in=10, tokens_out=5,
                cost_usd=cost, quality_signal={"texts": 3})
    out = await queue.llm_calls(pool, job_id)
    assert out["totals"]["calls"] == 2
    assert out["totals"]["cost_usd"] == pytest.approx(0.3)
    assert out["by_stage"] == [{"stage": "adhoc_aa687", "model": "haiku-4-5", "calls": 2,
                                "cost_usd": pytest.approx(0.3)}]
    assert out["calls"][0]["texts"] == 3 and out["truncated"] is False
    links = await queue.domain_links(pool, job_id, {"tour_id": "t-1"})
    assert links == {"tour_versions": [], "content_pieces": [], "tour_id": "t-1", "version_id": None}
