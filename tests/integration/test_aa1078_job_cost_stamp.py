"""AA-1078 (S277) — shared.job.cost_usd holds the job's whole cost (handler spend + its llm_call_log sum), against a
real Postgres: stamped on every save of a handler cost, never counted twice by the read side, a retry or a release.
Before, the column held only what the handler reported (0 on every LLM-only job, the S276 T-series audit)."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs.registry import JobContext  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "adhoc_aa1078"
KIND = "aa1078_cost"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    yield p
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
    await p.close()


async def _ledger(pool, job_id: str, *costs: float) -> None:
    from shared.llm_client import call_log
    with call_log.bind_job(job_id):
        for c in costs:
            await call_log.record_call_with_pool(pool, stage=STAGE, role="writer", model="haiku-4-5", tokens_in=1,
                                                 tokens_out=1, cost_usd=c, quality_signal={})


async def _claim(pool) -> queue.Job:
    job = await queue.claim(pool, "w-aa1078", {KIND: 1}, 60)
    assert job is not None
    return job


async def _row(pool, job_id: str) -> dict:
    row = await pool.fetchrow("SELECT cost_usd::float AS cost_usd, progress FROM shared.job WHERE id = $1::uuid",
                              job_id)
    return {"cost_usd": row["cost_usd"], "progress": queue._json(row["progress"])}


@pytest.mark.asyncio
async def test_complete_stamps_the_ledger_sum_and_the_read_side_does_not_add_it_twice(pool):
    job_id, _ = await queue.enqueue(pool, KIND, {})
    job = await _claim(pool)
    await _ledger(pool, job_id, 0.0125, 0.0300)
    ctx = JobContext(pool, job)
    ctx.add_cost(0.5)                      # a non-LLM spend the handler reports (e.g. DataForSEO)
    await ctx.progress(step="x")           # a progress save keeps the (absent) stamp
    assert (await _row(pool, job_id))["cost_usd"] == pytest.approx(0.5)
    assert await queue.complete(pool, job_id, "w-aa1078", {"ok": True}, round(ctx.cost_usd, 6))
    row = await _row(pool, job_id)
    assert row["cost_usd"] == pytest.approx(0.5425)                 # handler + ledger, in the column
    assert row["progress"]["cost_stamp"]["llm_usd"] == pytest.approx(0.0425)
    got = await queue.get(pool, job_id)
    assert got["cost_usd"] == pytest.approx(0.5425) and got["llm_cost_usd"] == pytest.approx(0.0425)
    by = {r["id"]: r for r in await queue.list_jobs(pool, kind=KIND, include_blobs=False)}
    assert by[job_id]["cost_usd"] == pytest.approx(0.5425)
    srt = await queue.list_jobs(pool, kind=KIND, sort="cost_usd")
    assert [r["id"] for r in srt] == [job_id]


@pytest.mark.asyncio
async def test_an_llm_only_job_gets_its_ledger_cost_and_a_late_row_shows_on_the_read_side(pool):
    job_id, _ = await queue.enqueue(pool, KIND, {})
    job = await _claim(pool)
    await _ledger(pool, job_id, 0.0664, 0.0380)
    await queue.complete(pool, job_id, "w-aa1078", None, round(JobContext(pool, job).cost_usd, 6))
    assert (await _row(pool, job_id))["cost_usd"] == pytest.approx(0.1044)   # 0 before AA-1078
    await _ledger(pool, job_id, 0.0010)                                       # a fire-and-forget write after the end
    assert (await queue.get(pool, job_id))["cost_usd"] == pytest.approx(0.1054)


@pytest.mark.asyncio
async def test_a_retry_and_a_release_never_count_the_stamped_part_twice(pool):
    job_id, _ = await queue.enqueue(pool, KIND, {}, max_attempts=3)
    job = await _claim(pool)
    await _ledger(pool, job_id, 0.02)
    ctx = JobContext(pool, job)
    ctx.add_cost(0.1)
    assert await queue.fail(pool, job, "w-aa1078", "boom", retryable=True, cost_usd=round(ctx.cost_usd, 6)) == "queued"
    assert (await _row(pool, job_id))["cost_usd"] == pytest.approx(0.12)
    await pool.execute("UPDATE shared.job SET run_after = now() WHERE id = $1::uuid", job_id)

    job2 = await _claim(pool)                       # attempt 2 starts from the handler part only
    ctx2 = JobContext(pool, job2)
    assert ctx2.cost_usd == pytest.approx(0.1)
    await _ledger(pool, job_id, 0.03)
    ctx2.add_cost(0.05)
    await ctx2.progress(step="y")                   # keeps the stamp: cost = handler + stamped part
    assert (await _row(pool, job_id))["cost_usd"] == pytest.approx(0.17)
    assert await queue.release(pool, job_id, "w-aa1078", round(ctx2.cost_usd, 6)) == "queued"
    row = await _row(pool, job_id)
    assert row["cost_usd"] == pytest.approx(0.2) and row["progress"]["cost_stamp"]["llm_usd"] == pytest.approx(0.05)

    await pool.execute("UPDATE shared.job SET run_after = now() WHERE id = $1::uuid", job_id)
    job3 = await _claim(pool)
    ctx3 = JobContext(pool, job3)
    assert ctx3.cost_usd == pytest.approx(0.15)
    await queue.complete(pool, job_id, "w-aa1078", None, round(ctx3.cost_usd, 6))
    assert (await _row(pool, job_id))["cost_usd"] == pytest.approx(0.2)
    assert (await queue.get(pool, job_id))["cost_usd"] == pytest.approx(0.2)
    # a manual Retry of a finished job keeps the stamp (progress), so the next run still starts from 0.15
    await pool.execute("UPDATE shared.job SET status = 'failed' WHERE id = $1::uuid", job_id)
    assert await queue.retry(pool, job_id)
    job4 = await _claim(pool)
    assert JobContext(pool, job4).cost_usd == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_a_reaped_job_is_stamped_and_a_row_without_a_stamp_reads_as_before(pool):
    job_id, _ = await queue.enqueue(pool, KIND, {}, max_attempts=1)
    await _claim(pool)
    await _ledger(pool, job_id, 0.04)
    await pool.execute("UPDATE shared.job SET locked_until = now() - interval '1 minute', cost_usd = 0.01 "
                       "WHERE id = $1::uuid", job_id)
    out = await queue.reap(pool)
    assert out["failed"] == [job_id]
    assert (await _row(pool, job_id))["cost_usd"] == pytest.approx(0.05)
    assert (await queue.get(pool, job_id))["cost_usd"] == pytest.approx(0.05)

    old_id, _ = await queue.enqueue(pool, KIND, {"old": True})            # a row written before AA-1078
    await pool.execute("UPDATE shared.job SET status = 'succeeded', cost_usd = 0.3, finished_at = now() "
                       "WHERE id = $1::uuid", old_id)
    await _ledger(pool, old_id, 0.2)
    assert (await queue.get(pool, old_id))["cost_usd"] == pytest.approx(0.5)
    summary = await pool.fetchval(f"SELECT sum({queue.JOB_COST_SQL})::float FROM shared.job j WHERE kind = $1", KIND)
    assert summary == pytest.approx(0.55)
