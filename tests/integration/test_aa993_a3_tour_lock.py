"""AA-993 G3 (ADR 0016 D1) — the `a3_atomize` tour lock against a real Postgres.

handler.a3_tour_lock holds a SESSION advisory lock on one dedicated connection for a whole A3 run, so it must:
* survive every transaction the run commits on other connections (an xact lock would not, critic 10);
* keep a second run on the same tour waiting until the first lets go, and let another tour through;
* be dropped by Postgres when the holder's connection ends (the old ECS task exits) — no manual cleanup.
"""
import asyncio
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.export import handler  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TOUR = "aa993-g3-tour-a"
OTHER = "aa993-g3-tour-b"


@pytest_asyncio.fixture
async def pool(monkeypatch):
    monkeypatch.setattr(handler, "_connect_tour_lock", lambda: asyncpg.connect(DSN))
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield p
    await p.close()


async def _advisory_holders(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
        " AND classid = hashtext($1)::oid AND objid = hashtext($2)::oid AND objsubid = 2",
        handler.A3_TOUR_LOCK_CLASS, TOUR)


@pytest.mark.asyncio
async def test_second_run_waits_across_the_first_runs_transactions(pool):
    order = []
    first_in, first_may_leave = asyncio.Event(), asyncio.Event()

    async def first():
        async with handler.a3_tour_lock(TOUR, poll_seconds=0.05):
            order.append("first in")
            first_in.set()
            for _ in range(3):             # the run's own transactions, committed on other connections
                async with pool.acquire() as c:
                    async with c.transaction():
                        await c.execute("SELECT 1")
            await first_may_leave.wait()
            order.append("first out")

    async def second():
        await first_in.wait()
        async with handler.a3_tour_lock(TOUR, poll_seconds=0.05):
            order.append("second in")

    t1, t2 = asyncio.create_task(first()), asyncio.create_task(second())
    await first_in.wait()
    await asyncio.sleep(0.5)
    assert order == ["first in"]
    assert await _advisory_holders(pool) == 1
    first_may_leave.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), 10)
    assert order == ["first in", "first out", "second in"]
    assert await _advisory_holders(pool) == 0


@pytest.mark.asyncio
async def test_another_tour_is_not_blocked(pool):
    async with handler.a3_tour_lock(TOUR, poll_seconds=0.05):
        async with handler.a3_tour_lock(OTHER, poll_seconds=0.05, max_wait_seconds=1):
            pass


@pytest.mark.asyncio
async def test_the_lock_ends_with_the_holders_connection(pool):
    """The old task exits mid-run (no finally ran): Postgres drops its session lock, the new run gets in."""
    holder = await asyncpg.connect(DSN)
    assert await holder.fetchval(handler.A3_TOUR_LOCK_SQL, handler.A3_TOUR_LOCK_CLASS, TOUR)
    pid = await holder.fetchval("SELECT pg_backend_pid()")

    lock = handler.a3_tour_lock(TOUR, poll_seconds=0.05)
    waiter = asyncio.create_task(lock.__aenter__())
    try:
        await asyncio.sleep(0.3)
        assert not waiter.done()
        await pool.execute("SELECT pg_terminate_backend($1)", pid)
        await asyncio.wait_for(waiter, 10)
        assert await _advisory_holders(pool) == 1
    finally:
        holder.terminate()
        if waiter.done() and not waiter.exception():
            await lock.__aexit__(None, None, None)
    assert await _advisory_holders(pool) == 0
