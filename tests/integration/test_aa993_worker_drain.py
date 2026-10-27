"""AA-993 G1 (ADR 0016 D1) — worker drain against a real Postgres: claim -> SIGTERM -> release -> re-claim.

The worker runs under shared.jobs.worker.serve(), the same code `python -m shared.jobs.worker` runs on ECS, and the
test sends this process a real SIGTERM. Covers: a release keeps the attempt; a release while a worker of a newer
task revision is alive does not count toward MAX_RELEASES (a deploy never fails a job); the kind's
release_delay_seconds; progress.release_log and queue.released_since (GET /admin/job-runner/released).
"""
import asyncio
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs import registry  # noqa: E402
from shared.jobs.worker import Worker, serve  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
WORKER_IDS = ("aa993-old", "aa993-new", "aa993-newer-rev")


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.job_worker WHERE worker_id = ANY($1::text[])", list(WORKER_IDS))
    yield p
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.job_worker WHERE worker_id = ANY($1::text[])", list(WORKER_IDS))
    await p.close()


@pytest.fixture
def drain_kinds(monkeypatch):
    """`drain_job` blocks on its first run (the old task) and returns at once on the next (the new task)."""
    monkeypatch.delenv("JOB_WORKER_TASK_REVISION", raising=False)
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    monkeypatch.setattr(registry, "_KINDS", {})
    seen = {"runs": []}

    @registry.job_kind("drain_job", max_attempts=1)
    async def drain_job(ctx):
        seen["runs"].append(ctx.job.attempt)
        if len(seen["runs"]) == 1:
            await asyncio.sleep(60)
        return {"run": len(seen["runs"])}

    return seen


async def _row(pool, job_id):
    return await pool.fetchrow(
        "SELECT status, attempt, locked_by, run_after, progress FROM shared.job WHERE id = $1::uuid", job_id)


async def _progress(pool, job_id) -> dict:
    import json
    v = (await _row(pool, job_id))["progress"]
    return json.loads(v) if isinstance(v, str) else dict(v)


async def _wait_status(pool, job_id, statuses, timeout=10.0):
    row = None
    for _ in range(int(timeout / 0.1)):
        row = await _row(pool, job_id)
        if row["status"] in statuses:
            return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached {statuses}: {dict(row) if row else None}")


async def _sigterm_drain(old: Worker, pool, job_id, before_sigterm=None):
    """Runs `old` under serve() until the job runs, then SIGTERMs this process and waits for the drain."""
    served = asyncio.create_task(serve(old, signals=(signal.SIGTERM,)))
    try:
        await _wait_status(pool, job_id, ("running",))
        if before_sigterm is not None:
            await before_sigterm()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(served, timeout=15)
    finally:
        if not served.done():
            old._stopping.set()
            await asyncio.gather(served, return_exceptions=True)


async def _run_until(worker: Worker, pool, job_id, statuses):
    runner = asyncio.create_task(worker.run())
    try:
        return await _wait_status(pool, job_id, statuses)
    finally:
        await worker.shutdown()
        await runner


@pytest.mark.asyncio
async def test_claim_sigterm_release_reclaim(pool, drain_kinds):
    t0 = datetime.now(timezone.utc) - timedelta(seconds=1)
    job_id, _ = await registry.enqueue(pool, "drain_job", {})
    old = Worker(pool, worker_id="aa993-old", poll_seconds=0.1, heartbeat_seconds=0.2, grace_seconds=0.3)
    await _sigterm_drain(old, pool, job_id)

    row = await _row(pool, job_id)
    assert row["status"] == "queued" and row["attempt"] == 0 and row["locked_by"] is None
    p = await _progress(pool, job_id)
    # no newer revision was alive (no revision at all): the release counts toward MAX_RELEASES, as before G1
    assert p["released"] is True and p["releases"] == 1 and p["shutdown_releases"] == 1
    assert [e["counted"] for e in p["release_log"]] == [True] and p["release_log"][0]["worker"] == "aa993-old"

    new = Worker(pool, worker_id="aa993-new", poll_seconds=0.1, heartbeat_seconds=0.2)
    done = await _run_until(new, pool, job_id, ("succeeded",))
    assert done["attempt"] == 1                          # the release did not use the attempt (max_attempts=1)
    assert drain_kinds["runs"] == [1, 1]                 # both runs saw attempt 1

    out = await queue.released_since(pool, t0)
    assert out["released"] == 1 and out["counted"] == 1 and out["jobs"] == 1
    assert out["by_kind"] == {"drain_job": {"released": 1, "counted": 1, "jobs": 1}}
    later = await queue.released_since(pool, datetime.now(timezone.utc) + timedelta(seconds=5))
    assert later["released"] == 0 and later["by_kind"] == {}


@pytest.mark.asyncio
async def test_release_during_a_deploy_is_not_counted(pool, drain_kinds):
    job_id, _ = await registry.enqueue(pool, "drain_job", {})
    old = Worker(pool, worker_id="aa993-old", poll_seconds=0.1, heartbeat_seconds=0.2, grace_seconds=0.3)
    old.task_revision = 416

    async def deploy_starts_revision_417():
        await queue.worker_register(pool, "aa993-newer-rev", "host", 1, {"drain_job": 1}, 417, None)

    await _sigterm_drain(old, pool, job_id, before_sigterm=deploy_starts_revision_417)

    row = await _row(pool, job_id)
    assert row["status"] == "queued" and row["attempt"] == 0
    p = await _progress(pool, job_id)
    assert p["releases"] == 0 and p["shutdown_releases"] == 1    # the MAX_RELEASES counter is unchanged
    assert [e["counted"] for e in p["release_log"]] == [False]
    out = await queue.released_since(pool, datetime.now(timezone.utc) - timedelta(minutes=1))
    assert out["released"] == 1 and out["counted"] == 0


@pytest.mark.asyncio
async def test_uncounted_releases_never_fail_the_job(pool):
    job_id, _ = await queue.enqueue(pool, "k", {}, max_attempts=1)
    for _ in range(queue.MAX_RELEASES + 2):
        await queue.claim(pool, "w", {"k": 1}, 60)
        assert await queue.release(pool, job_id, "w", None, count_release=False) == "queued"
    p = await _progress(pool, job_id)
    assert p["releases"] == 0 and p["shutdown_releases"] == queue.MAX_RELEASES + 2
    # counted releases still fail a job that keeps killing its worker
    statuses = []
    for _ in range(queue.MAX_RELEASES):
        await queue.claim(pool, "w", {"k": 1}, 60)
        statuses.append(await queue.release(pool, job_id, "w", None))
    assert statuses == ["queued"] * (queue.MAX_RELEASES - 1) + ["failed"]


@pytest.mark.asyncio
async def test_release_delay_postpones_the_reclaim(pool):
    job_id, _ = await queue.enqueue(pool, "k", {})
    await queue.claim(pool, "w", {"k": 1}, 60)
    assert await queue.release(pool, job_id, "w", None, delay_seconds=120) == "queued"
    row = await _row(pool, job_id)
    assert row["run_after"] > datetime.now(timezone.utc) + timedelta(seconds=100)
    assert await queue.claim(pool, "w2", {"k": 1}, 60) is None


@pytest.mark.asyncio
async def test_release_log_keeps_the_last_entries(pool):
    job_id, _ = await queue.enqueue(pool, "k", {})
    n = queue.RELEASE_LOG_KEEP + 2
    for i in range(n):
        await queue.claim(pool, f"w{i}", {"k": 1}, 60)
        await queue.release(pool, job_id, f"w{i}", None, count_release=False)
    p = await _progress(pool, job_id)
    assert len(p["release_log"]) == queue.RELEASE_LOG_KEEP and p["shutdown_releases"] == n
    assert [e["worker"] for e in p["release_log"]] == [f"w{i}" for i in range(2, n)]   # oldest dropped, in order


@pytest.mark.asyncio
async def test_release_survives_a_handler_written_release_log(pool):
    """A non-array progress.release_log (never written by the runner) is replaced, not an SQL error."""
    job_id, _ = await queue.enqueue(pool, "k", {})
    await queue.claim(pool, "w", {"k": 1}, 60)
    await queue.set_progress(pool, job_id, {"release_log": "oops"})
    assert await queue.release(pool, job_id, "w", None) == "queued"
    assert len((await _progress(pool, job_id))["release_log"]) == 1
    assert (await queue.released_since(pool, datetime.now(timezone.utc) - timedelta(minutes=1)))["released"] == 1
