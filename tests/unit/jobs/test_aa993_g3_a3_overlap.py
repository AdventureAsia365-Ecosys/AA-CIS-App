"""AA-993 G3 (ADR 0016 D1) — a released `a3_atomize` job never runs side by side with the attempt that re-claims it.

* The job's thread stops at the next day boundary (or between the later steps) once the handler is cancelled
  (threading.Event → AtomizeStopped), and its late progress writes are dropped.
* The whole chain runs under a session advisory lock on the tour (handler.a3_tour_lock): a second run on the same
  tour waits until the first lets go — across all the first run's transactions — and gives up on a stop or a timeout.
* The kind's release_delay_seconds = 120 (the worker's ECS stopTimeout).

No LLM, no DB: the gateway and the pool are mocks; the lock runs against an in-memory lock table with the same
session semantics as Postgres (a lock belongs to a connection until unlocked or the connection closes). The real
Postgres semantics are covered by tests/integration/test_aa993_a3_tour_lock.py.
"""
import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import a3_atomize
from services.acp_contract.a3_atomize import AtomizeStopped
from services.export import handler
from services.jobs import a3_atomize_job
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext

TOUR_ID = "44444444-4444-4444-4444-444444444444"
OTHER_TOUR = "55555555-5555-5555-5555-555555555555"
VERSION_ID = "77777777-7777-7777-7777-777777777777"
THREE_DAYS = ("Day 1 — Arrival in Hanoi\nWalk through the Old Quarter.\n\n"
              "Day 2 — Halong Bay Cruise\nKayak through limestone caves.\n\n"
              "Day 3 — Departure\nTransfer to the airport.")


# ── the day boundary ──────────────────────────────────────────────────────────────────────────


def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    pool.close = AsyncMock()
    return pool


def _conn():
    conn = AsyncMock()
    conn.fetch.return_value = []          # no fingerprint yet: every day is read
    conn.execute.return_value = "UPDATE 0"
    return conn


class _Resp:
    def __init__(self, place):
        self.content = json.dumps({"atoms": [{"place": place, "action": "visit", "activity_type": "culture"}]})
        self.model_used, self.input_tokens, self.output_tokens, self.cost_usd = "m", 10, 5, 0.001
        self.stop_reason, self.satellite_account, self.fallback_used, self.provider = "end_turn", "acc3", False, "x"


async def _atomize(conn, generate, should_stop, itineraries=THREE_DAYS, version_id=VERSION_ID):
    client = MagicMock()
    client.generate = generate
    with patch.object(a3_atomize, "LLMClient", return_value=client), \
            patch.object(a3_atomize, "get_stage_config",
                         AsyncMock(return_value=MagicMock(model_id="m", role="writer"))), \
            patch.object(a3_atomize, "record_call_with_pool", AsyncMock()), \
            patch.object(a3_atomize, "record_unusable", AsyncMock()), \
            patch.object(a3_atomize, "ground_day_atoms", AsyncMock(side_effect=lambda atoms, *a, **k: atoms)):
        return await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": itineraries}, _pool(conn),
                                               version_id=version_id, should_stop=should_stop)


def _fingerprint_days(conn):
    return [c.args[2] for c in conn.execute.call_args_list if "atomize_day_fingerprint" in c.args[0]]


@pytest.mark.asyncio
async def test_a_cancel_stops_the_run_at_the_next_day_boundary():
    stop = threading.Event()

    def generate(req):
        stop.set()     # the job is released while day 1 is being read
        return _Resp("Old Quarter")

    conn = _conn()
    gen = MagicMock(side_effect=generate)
    with pytest.raises(AtomizeStopped, match="day 2"):
        await _atomize(conn, gen, stop.is_set)
    assert gen.call_count == 1                 # day 2 and 3 never asked
    assert _fingerprint_days(conn) == [1]      # the day being read finished whole: atoms + fingerprint


@pytest.mark.asyncio
async def test_a_run_asked_to_stop_before_it_starts_makes_no_llm_call():
    gen = MagicMock(side_effect=AssertionError("no LLM call expected"))
    with pytest.raises(AtomizeStopped):
        await _atomize(_conn(), gen, lambda: True)
    with pytest.raises(AtomizeStopped, match="legacy_read"):   # the whole-tour path (no canonical days)
        await _atomize(_conn(), gen, lambda: True, itineraries="no day headings here", version_id=None)
    gen.assert_not_called()


@pytest.mark.asyncio
async def test_without_should_stop_every_day_is_read():
    conn = _conn()
    out = await _atomize(conn, MagicMock(side_effect=[_Resp("a"), _Resp("b"), _Resp("c")]), None)
    assert out["status"] == "success" and _fingerprint_days(conn) == [1, 2, 3]


@pytest.mark.asyncio
async def test_the_chain_stops_before_segment_score_route_and_still_closes_its_pool():
    stop = threading.Event()
    pool = _pool(_conn())

    async def atomize(*a, **k):
        stop.set()     # released while the last day was being read
        return {"status": "success", "atom_count": 3}

    run_stages = AsyncMock()
    with patch.object(handler, "open_job_pool", AsyncMock(return_value=pool)), \
            patch("services.acp_contract.a3_atomize.run_a3_atomize", atomize), \
            patch("services.recompute.run_stages", run_stages):
        with pytest.raises(AtomizeStopped, match="segment_score_route"):
            # reraise=False: a stop is not a failure to swallow, it always ends the run
            await handler._run_a3_atomize_background(TOUR_ID, {}, "", VERSION_ID, should_stop=stop.is_set)
    run_stages.assert_not_awaited()
    pool.close.assert_awaited_once()


# ── the tour lock ─────────────────────────────────────────────────────────────────────────────


class LockServer:
    """Session advisory locks as Postgres keeps them: held by a connection until unlocked or the connection ends."""

    def __init__(self):
        self.held: dict[tuple, object] = {}
        self.connections = []

    def connect(self):
        c = FakeLockConn(self)
        self.connections.append(c)
        return c


class FakeLockConn:
    def __init__(self, server: LockServer):
        self.server = server
        self.closed = False

    async def fetchval(self, sql, cls, tour):
        key = (cls, tour)
        if sql == handler.A3_TOUR_LOCK_SQL:
            owner = self.server.held.get(key)
            if owner is None:
                self.server.held[key] = self
                return True
            return owner is self
        if sql == handler.A3_TOUR_UNLOCK_SQL:
            if self.server.held.get(key) is self:
                del self.server.held[key]
                return True
            return False
        raise AssertionError(sql)

    async def close(self):
        self.closed = True
        for k in [k for k, v in self.server.held.items() if v is self]:
            del self.server.held[k]


@pytest.fixture
def locks(monkeypatch):
    server = LockServer()
    monkeypatch.setattr(handler, "_connect_tour_lock", AsyncMock(side_effect=lambda: server.connect()))
    return server


@pytest.mark.asyncio
async def test_a_second_run_on_the_tour_waits_until_the_first_lets_go(locks):
    order = []
    first_in = asyncio.Event()
    first_may_leave = asyncio.Event()

    async def first():
        async with handler.a3_tour_lock(TOUR_ID, poll_seconds=0.01):
            order.append("first in")
            first_in.set()
            for _ in range(5):            # many transactions on other connections: the session lock stays
                await asyncio.sleep(0.01)
            await first_may_leave.wait()
            order.append("first out")

    async def second():
        await first_in.wait()
        async with handler.a3_tour_lock(TOUR_ID, poll_seconds=0.01):
            order.append("second in")

    t1, t2 = asyncio.create_task(first()), asyncio.create_task(second())
    await asyncio.sleep(0.1)
    assert order == ["first in"]            # the second run is still waiting
    first_may_leave.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), 2)
    assert order == ["first in", "first out", "second in"]
    assert not locks.held and all(c.closed for c in locks.connections)


@pytest.mark.asyncio
async def test_another_tour_is_not_blocked(locks):
    async with handler.a3_tour_lock(TOUR_ID, poll_seconds=0.01):
        async with handler.a3_tour_lock(OTHER_TOUR, poll_seconds=0.01):
            assert len(locks.held) == 2


@pytest.mark.asyncio
async def test_a_waiting_run_that_is_cancelled_itself_stops_waiting(locks):
    stop = threading.Event()
    async with handler.a3_tour_lock(TOUR_ID, poll_seconds=0.01):
        waiter = asyncio.create_task(handler.a3_tour_lock(TOUR_ID, stop.is_set, poll_seconds=0.01).__aenter__())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        stop.set()
        with pytest.raises(AtomizeStopped):
            await asyncio.wait_for(waiter, 1)
    assert locks.connections[1].closed       # the waiter's own connection is closed too


@pytest.mark.asyncio
async def test_a_lock_held_too_long_times_out(locks):
    async with handler.a3_tour_lock(TOUR_ID, poll_seconds=0.01):
        with pytest.raises(handler.TourLockTimeout):
            async with handler.a3_tour_lock(TOUR_ID, poll_seconds=0.01, max_wait_seconds=0.05):
                pass


@pytest.mark.asyncio
async def test_the_lock_is_released_when_the_body_fails(locks):
    with pytest.raises(RuntimeError):
        async with handler.a3_tour_lock(TOUR_ID, poll_seconds=0.01):
            raise RuntimeError("atomize blew up")
    assert not locks.held


@pytest.mark.asyncio
async def test_the_job_chain_runs_under_the_tour_lock(locks):
    seen = {}

    async def chain(tour_id, *a, **k):
        seen["held"] = (handler.A3_TOUR_LOCK_CLASS, tour_id) in locks.held
        return {"segment_score_route": "ok"}

    with patch.object(handler, "_a3_atomize_chain", chain):
        out = await handler._run_a3_atomize_background(TOUR_ID, {}, "", VERSION_ID, tour_lock=True)
        assert out == {"segment_score_route": "ok"} and seen["held"] is True and not locks.held
        seen.clear()
        await handler._run_a3_atomize_background(TOUR_ID, {}, "", VERSION_ID)   # other callers: no lock
        assert seen["held"] is False


@pytest.mark.asyncio
async def test_a_lock_timeout_is_raised_with_reraise_and_reported_without(locks, monkeypatch):
    monkeypatch.setattr(handler, "A3_TOUR_LOCK_MAX_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(handler, "A3_TOUR_LOCK_POLL_SECONDS", 0.01)
    chain = AsyncMock()
    with patch.object(handler, "_a3_atomize_chain", chain):
        async with handler.a3_tour_lock(TOUR_ID, poll_seconds=0.01):
            with pytest.raises(handler.TourLockTimeout):
                await handler._run_a3_atomize_background(TOUR_ID, {}, "", VERSION_ID, reraise=True, tour_lock=True)
            out = await handler._run_a3_atomize_background(TOUR_ID, {}, "", VERSION_ID, tour_lock=True)
    assert out["tour_lock"].startswith("failed:") and out["segment_score_route"] == "not_run"
    chain.assert_not_awaited()


# ── the job ───────────────────────────────────────────────────────────────────────────────────


def test_the_kind_is_due_again_only_after_the_old_task_is_gone():
    kind = registry.get_kind("a3_atomize")
    assert kind.release_delay_seconds == a3_atomize_job.RELEASE_DELAY_SECONDS == 120
    assert not kind.deploy_blocking


def _ctx():
    job = queue.Job(id="job-g3", kind="a3_atomize",
                    payload={"tour_id": TOUR_ID, "version_id": VERSION_ID, "country": "VN",
                             "rewritten": {"name": "T", "itineraries": THREE_DAYS}},
                    status="running", attempt=1, max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(None, job, {})
    ctx.progress = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_a_cancelled_handler_stops_its_thread_and_drops_its_late_progress():
    started, finished = threading.Event(), threading.Event()
    seen = {}

    async def chain(**kw):
        seen.update(kw)
        started.set()
        try:
            while not kw["should_stop"]():      # "reading days"
                await asyncio.sleep(0.01)
            kw["progress"]({"step": "late write from the released thread"})
            raise AtomizeStopped("stopped before day 2")
        finally:
            finished.set()

    async def bg(tour_id, rewritten, country, version_id, **kw):
        return await chain(**kw)

    ctx = _ctx()
    with patch("services.export.handler._run_a3_atomize_background", bg):
        task = asyncio.create_task(a3_atomize_job.run(ctx))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()                           # the worker's shutdown cancel after the grace window
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 2), "the thread kept running after the cancel"
    assert seen["tour_lock"] is True
    await asyncio.sleep(0.05)
    fields = [c.kwargs for c in ctx.progress.await_args_list] + [c.kwargs for c in ctx.progress.call_args_list]
    assert not any(f.get("step") == "late write from the released thread" for f in fields)
