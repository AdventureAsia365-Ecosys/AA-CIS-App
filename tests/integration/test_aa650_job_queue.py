"""AA-650 — shared.job queue against a real Postgres (migration 174 applied by conftest).

Covers the Done-when list: claim, retry, reaper and idempotency, plus the concurrency cap,
cancel, release-on-shutdown and a worker running a real handler end to end.
"""
import asyncio
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

# The integration run adds only this directory to sys.path (conftest); add the repo root for
# `shared.*`, same as test_compiled_graph_state_propagation.py.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.cost_guard import BudgetExceeded  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs import registry  # noqa: E402
from shared.jobs.worker import Worker  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    yield p
    await p.execute("DELETE FROM shared.job")
    await p.close()


async def _status(pool, job_id):
    return await pool.fetchrow("SELECT status, attempt, error, locked_by, run_after > now() AS delayed, "
                               "result, cost_usd::float AS cost FROM shared.job WHERE id = $1::uuid", job_id)


@pytest.mark.asyncio
async def test_idempotency_key_returns_the_existing_job(pool):
    a, created_a = await queue.enqueue(pool, "k", {"n": 1}, idempotency_key="rerun:VN")
    b, created_b = await queue.enqueue(pool, "k", {"n": 2}, idempotency_key="rerun:VN")
    assert created_a and not created_b and a == b
    assert await pool.fetchval("SELECT count(*) FROM shared.job") == 1
    c, created_c = await queue.enqueue(pool, "k", {})  # no key: always a new job
    assert created_c and c != a


@pytest.mark.asyncio
async def test_claim_takes_due_jobs_once_and_respects_the_kind_cap(pool):
    j1, _ = await queue.enqueue(pool, "research", {})
    j2, _ = await queue.enqueue(pool, "research", {})
    await queue.enqueue(pool, "other", {})
    first = await queue.claim(pool, "w1", {"research": 1}, 60)
    assert first.id == j1 and first.attempt == 1 and first.status == "running"
    # cap 1 for research is reached; "other" is not in caps
    assert await queue.claim(pool, "w2", {"research": 1}, 60) is None
    # two concurrent claimers with cap 2 never get the same job
    got = await asyncio.gather(queue.claim(pool, "w3", {"research": 2}, 60),
                               queue.claim(pool, "w4", {"research": 2}, 60))
    ids = [g.id for g in got if g]
    assert ids == [j2]


@pytest.mark.asyncio
async def test_claim_skips_jobs_not_due_yet(pool):
    from datetime import datetime, timedelta, timezone
    await queue.enqueue(pool, "k", {}, run_after=datetime.now(timezone.utc) + timedelta(hours=1))
    assert await queue.claim(pool, "w", {"k": 5}, 60) is None


@pytest.mark.asyncio
async def test_failure_retries_with_backoff_then_fails(pool):
    job_id, _ = await queue.enqueue(pool, "k", {}, max_attempts=2)
    job = await queue.claim(pool, "w", {"k": 1}, 60)
    assert await queue.fail(pool, job, "w", "boom", retryable=True, cost_usd=0.1) == "queued"
    row = await _status(pool, job_id)
    assert row["status"] == "queued" and row["delayed"] and row["locked_by"] is None
    await pool.execute("UPDATE shared.job SET run_after = now() WHERE id = $1::uuid", job_id)
    job = await queue.claim(pool, "w", {"k": 1}, 60)
    assert job.attempt == 2
    assert await queue.fail(pool, job, "w", "boom again", retryable=True, cost_usd=0.2) == "failed"
    assert (await _status(pool, job_id))["status"] == "failed"
    assert await queue.retry(pool, job_id)
    assert (await _status(pool, job_id))["attempt"] == 0


@pytest.mark.asyncio
async def test_non_retryable_failure_fails_at_once(pool):
    job_id, _ = await queue.enqueue(pool, "k", {}, max_attempts=3)
    job = await queue.claim(pool, "w", {"k": 1}, 60)
    assert await queue.fail(pool, job, "w", "bad payload", retryable=False, cost_usd=None) == "failed"


@pytest.mark.asyncio
async def test_reaper_requeues_expired_lease_and_fails_when_attempts_are_used(pool):
    a, _ = await queue.enqueue(pool, "k", {}, max_attempts=2)
    b, _ = await queue.enqueue(pool, "k", {}, max_attempts=1)
    await queue.claim(pool, "dead-worker", {"k": 5}, 60)
    await queue.claim(pool, "dead-worker", {"k": 5}, 60)
    await pool.execute("UPDATE shared.job SET locked_until = now() - interval '1 second'")
    reaped = await queue.reap(pool)
    assert reaped == {"requeued": [a], "failed": [b]}
    assert (await _status(pool, a))["status"] == "queued"
    assert (await _status(pool, b))["status"] == "failed"
    # the dead worker can no longer finish a job it lost
    assert not await queue.complete(pool, a, "dead-worker", {}, 0)


@pytest.mark.asyncio
async def test_release_does_not_count_the_attempt(pool):
    job_id, _ = await queue.enqueue(pool, "k", {})
    await queue.claim(pool, "w", {"k": 1}, 60)
    assert await queue.release(pool, job_id, "w", 0.5) == "queued"
    row = await _status(pool, job_id)
    assert row["status"] == "queued" and row["attempt"] == 0 and row["cost"] == 0.5


@pytest.mark.asyncio
async def test_a_job_released_too_often_fails_instead_of_looping(pool):
    """S201 incident: a job that makes its own worker process get replaced would otherwise be
    released and re-claimed forever (a release does not count as an attempt)."""
    job_id, _ = await queue.enqueue(pool, "k", {}, max_attempts=5)
    statuses = []
    for _ in range(queue.MAX_RELEASES):
        await queue.claim(pool, "w", {"k": 1}, 60)
        statuses.append(await queue.release(pool, job_id, "w", None))
    assert statuses == ["queued"] * (queue.MAX_RELEASES - 1) + ["failed"]
    row = await _status(pool, job_id)
    assert row["status"] == "failed" and "stopped mid-run" in row["error"]


@pytest.mark.asyncio
async def test_release_of_a_cancel_requested_job_cancels_it(pool):
    job_id, _ = await queue.enqueue(pool, "k", {})
    await queue.claim(pool, "w", {"k": 1}, 60)
    await queue.request_cancel(pool, job_id)
    assert await queue.release(pool, job_id, "w", None) == "cancelled"


@pytest.mark.asyncio
async def test_cancel_queued_is_immediate_and_running_is_flagged(pool):
    q, _ = await queue.enqueue(pool, "k", {})
    assert await queue.request_cancel(pool, q) == "cancelled"
    r, _ = await queue.enqueue(pool, "k", {})
    await queue.claim(pool, "w", {"k": 1}, 60)
    assert await queue.request_cancel(pool, r) == "running"
    assert await queue.heartbeat(pool, r, "w", 60) == (True, True)


# ── worker end to end ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def test_kinds(monkeypatch):
    monkeypatch.setattr(registry, "_KINDS", {})
    seen = {}

    @registry.job_kind("ok_job", concurrency=2)
    async def ok_job(ctx):
        ctx.add_cost(0.25)
        await ctx.progress(step="half")
        return {"echo": ctx.payload["x"]}

    @registry.job_kind("budget_job")
    async def budget_job(ctx):
        ctx.set_result({"partial": True})
        raise BudgetExceeded("dfs daily budget $10.00 reached")

    async def record_terminal(pool, job_row):
        seen.setdefault("terminal", []).append((job_row["kind"], job_row["status"]))

    @registry.job_kind("doomed_job", max_attempts=1, on_terminal=record_terminal)
    async def doomed_job(ctx):
        raise RuntimeError("always fails")

    @registry.job_kind("llm_job")
    async def llm_job(ctx):
        from shared.llm_client import call_log
        ctx.add_cost(0.25)  # non-LLM spend the handler reports itself (e.g. DataForSEO)
        # A worker thread (to_thread / LangGraph executor) sees the bound job id too.
        seen["thread_job_id"] = await asyncio.to_thread(call_log.current_job_id)
        await call_log.record_call_with_pool(
            ctx.pool, stage="adhoc_aa652", role="writer", model="haiku-4-5", tokens_in=10,
            tokens_out=5, cost_usd=0.1, quality_signal={"source": "test"})

    @registry.job_kind("slow_job")
    async def slow_job(ctx):
        seen["started"] = True
        await asyncio.sleep(30)

    return seen


async def _wait_for(pool, job_id, statuses, timeout=10.0):
    for _ in range(int(timeout / 0.1)):
        row = await _status(pool, job_id)
        if row["status"] in statuses:
            return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached {statuses}: {dict(row)}")


@pytest.mark.asyncio
async def test_worker_runs_handlers_to_their_final_status(pool, test_kinds):
    ok, _ = await registry.enqueue(pool, "ok_job", {"x": 7})
    budget, _ = await registry.enqueue(pool, "budget_job", {})
    worker = Worker(pool, poll_seconds=0.1, heartbeat_seconds=0.2)
    runner = asyncio.create_task(worker.run())
    try:
        done = await _wait_for(pool, ok, ("succeeded",))
        assert done["cost"] == 0.25 and "echo" in done["result"]
        stopped = await _wait_for(pool, budget, ("stopped_budget",))
        assert "budget" in stopped["error"] and "partial" in stopped["result"]
    finally:
        await worker.shutdown()
        await runner


@pytest.mark.asyncio
async def test_worker_shutdown_releases_a_running_job(pool, test_kinds):
    slow, _ = await registry.enqueue(pool, "slow_job", {})
    worker = Worker(pool, poll_seconds=0.1, heartbeat_seconds=0.2, grace_seconds=0.2)
    runner = asyncio.create_task(worker.run())
    for _ in range(50):
        if test_kinds.get("started"):
            break
        await asyncio.sleep(0.1)
    await worker.shutdown()
    await runner
    row = await _status(pool, slow)
    assert row["status"] == "queued" and row["attempt"] == 0  # a new worker picks it up again


@pytest.mark.asyncio
async def test_worker_stops_a_running_job_on_admin_cancel(pool, test_kinds):
    slow, _ = await registry.enqueue(pool, "slow_job", {})
    worker = Worker(pool, poll_seconds=0.1, heartbeat_seconds=0.2)
    runner = asyncio.create_task(worker.run())
    try:
        await _wait_for(pool, slow, ("running",))
        await queue.request_cancel(pool, slow)
        await _wait_for(pool, slow, ("cancelled",))
    finally:
        await worker.shutdown()
        await runner


@pytest.mark.asyncio
async def test_worker_runs_the_terminal_hook_when_a_job_finally_fails(pool, test_kinds):
    """AA-652 — the domain row (e.g. a tour version) is told when its job ends without success."""
    doomed, _ = await registry.enqueue(pool, "doomed_job", {})
    worker = Worker(pool, poll_seconds=0.1, heartbeat_seconds=0.2)
    runner = asyncio.create_task(worker.run())
    try:
        await _wait_for(pool, doomed, ("failed",))
        for _ in range(20):
            if test_kinds.get("terminal"):
                break
            await asyncio.sleep(0.1)
        assert test_kinds["terminal"] == [("doomed_job", "failed")]
    finally:
        await worker.shutdown()
        await runner


@pytest.mark.asyncio
async def test_job_cost_includes_the_llm_calls_it_logged(pool, test_kinds):
    from shared.llm_client import call_log
    job_id, _ = await registry.enqueue(pool, "llm_job", {})
    worker = Worker(pool, poll_seconds=0.1, heartbeat_seconds=0.2)
    runner = asyncio.create_task(worker.run())
    try:
        await _wait_for(pool, job_id, ("succeeded",))
    finally:
        await worker.shutdown()
        await runner
    try:
        assert test_kinds["thread_job_id"] == job_id
        assert await pool.fetchval(
            "SELECT count(*) FROM shared.llm_call_log WHERE job_id = $1::uuid", job_id) == 1
        row = await queue.get(pool, job_id)
        assert row["llm_cost_usd"] == pytest.approx(0.1)
        assert row["cost_usd"] == pytest.approx(0.35)  # 0.25 reported + 0.1 logged
        listed = await queue.list_jobs(pool, kind="llm_job")
        assert listed[0]["cost_usd"] == pytest.approx(0.35)
        assert call_log.current_job_id() is None  # nothing leaks outside the handler
    finally:
        await pool.execute("DELETE FROM shared.llm_call_log WHERE stage = 'adhoc_aa652'")
