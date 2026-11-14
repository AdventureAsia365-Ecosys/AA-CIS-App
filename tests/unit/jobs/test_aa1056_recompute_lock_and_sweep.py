"""AA-1056 — the `recompute` job never runs twice side by side, names its failures with fixed codes, and a tour on/off
event that no recompute followed is measured, alerted on and swept.

* the kind: release delay = the worker's ECS stopTimeout (shared with a3_atomize), concurrency 1;
* the platform recompute lock (handler.job_session_lock, class `recompute`): two jobs queued together run one after
  the other, also across threads and event loops (the worker runs the body in its own thread + loop);
* the cancel event: a cancelled handler stops its thread at the next stage boundary and drops its late progress;
* fixed error codes: bad payload, lock timeout, stage failure (with the stage name);
* run_stages: `should_stop` before every stage, `on_stage` names it;
* the lifecycle measure / sweep (services/recompute/lifecycle) and the ops check `recompute_lifecycle_lag`;
* the api ops sampler: the lag every tick, the sweep only when the loop says it is due (every 2 h) and the kind is
  registered.

No DB: the lock runs against an in-memory lock table with Postgres session semantics (as the a3 G3 tests); the real
Postgres semantics are in tests/integration/test_aa1056_recompute_lifecycle.py.
"""
import asyncio
import copy
import pickle
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core import ops_metrics_sampler as sampler
from services.export import handler
from services.jobs import a3_atomize_job
from services.jobs import recompute_job as rc
from services.recompute import RecomputeScope, run_stages
from services.recompute import lifecycle
from services.recompute.errors import (
    BAD_PAYLOAD, ERROR_CODES, LOCK_TIMEOUT, STAGE_FAILED, STOPPED, RecomputeLockTimeout, RecomputeStageFailed,
    RecomputeStopped,
)
from shared import ops_checks
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext, NonRetryable

TOUR = "66666666-6666-6666-6666-666666666666"


# ── an in-memory session lock table, shared by every thread / event loop ────────────────────────────────────────────

class LockServer:
    def __init__(self):
        self.held = {}
        self.mutex = threading.Lock()
        self.connections = []

    def connect(self):
        c = FakeLockConn(self)
        self.connections.append(c)
        return c


class FakeLockConn:
    def __init__(self, server):
        self.server = server
        self.closed = False

    async def fetchval(self, sql, cls, key):
        k = (cls, key)
        with self.server.mutex:
            if sql == handler.A3_TOUR_LOCK_SQL:
                owner = self.server.held.get(k)
                if owner is None:
                    self.server.held[k] = self
                    return True
                return owner is self
            if sql == handler.A3_TOUR_UNLOCK_SQL:
                if self.server.held.get(k) is self:
                    del self.server.held[k]
                    return True
                return False
        raise AssertionError(sql)

    async def close(self):
        self.closed = True
        with self.server.mutex:
            for k in [k for k, v in self.server.held.items() if v is self]:
                del self.server.held[k]


@pytest.fixture
def locks(monkeypatch):
    server = LockServer()

    async def connect():
        return server.connect()

    monkeypatch.setattr(handler, "_connect_tour_lock", connect)
    monkeypatch.setattr(rc, "LOCK_POLL_SECONDS", 0.01)
    return server


def _ctx(payload, job_id="job-1"):
    job = queue.Job(id=job_id, kind="recompute", payload=payload, status="running", attempt=1, max_attempts=2,
                    progress={}, cost_usd=0.0)
    ctx = JobContext(None, job, {})
    ctx.progress = AsyncMock()
    return ctx


def _job_pool():
    pool = MagicMock()
    pool.close = AsyncMock()
    return pool


# ── the kind ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_kind_is_due_again_only_after_the_old_task_is_gone():
    kind = registry.get_kind("recompute")
    assert kind.release_delay_seconds == rc.RELEASE_DELAY_SECONDS == registry.WORKER_STOP_TIMEOUT_SECONDS == 120
    assert kind.concurrency == 1 and kind.expected_seconds == rc.EXPECTED_SECONDS == rc.LOCK_MAX_WAIT_SECONDS
    # a3_atomize reads the same constant (one number for the ECS stopTimeout)
    assert registry.get_kind("a3_atomize").release_delay_seconds == a3_atomize_job.RELEASE_DELAY_SECONDS == 120


def test_error_codes_are_fixed_and_lead_the_message():
    assert ERROR_CODES == ("recompute_bad_payload", "recompute_lock_timeout", "recompute_stopped",
                           "recompute_stage_failed")
    assert str(RecomputeLockTimeout("x")).startswith(f"{LOCK_TIMEOUT}: ")
    assert str(RecomputeStopped("x")).startswith(f"{STOPPED}: ")
    err = RecomputeStageFailed.for_stage("score", "KeyError: 'US'")
    assert str(err) == f"{STAGE_FAILED}: stage score: KeyError: 'US'" and err.stage == "score"
    # a copy / unpickle passes the full message back: still one prefix
    assert str(copy.copy(RecomputeLockTimeout("x"))) == f"{LOCK_TIMEOUT}: x"
    assert str(pickle.loads(pickle.dumps(err))) == str(err)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"scope": "nonsense"}, {"scope": "tour"}])
async def test_a_bad_payload_fails_at_once_with_its_code(payload):
    with pytest.raises(NonRetryable, match=f"^{BAD_PAYLOAD}: "):
        await rc.run(_ctx(payload))


# ── the lock: two jobs never run together ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_recompute_jobs_queued_together_run_one_after_the_other(locks):
    inside = []
    overlap = []
    gate = threading.Lock()

    async def body(*a, **kw):
        with gate:
            inside.append(1)
            if len(inside) > 1:
                overlap.append(True)
        await asyncio.sleep(0.15)
        with gate:
            inside.pop()
        return {"ranking": {}, "route": {}, "segment_gc": None}

    with patch("services.export.handler.recompute_rankings_and_routes", AsyncMock(side_effect=body)) as plat, \
            patch("services.export.handler.open_job_pool", AsyncMock(side_effect=lambda: _job_pool())):
        a = rc.run(_ctx({"scope": "platform", "reason": "master_status=trashed:t1"}, "job-a"))
        b = rc.run(_ctx({"scope": "platform", "reason": "lifecycle_sweep:1"}, "job-b"))
        await asyncio.wait_for(asyncio.gather(a, b), 5)
    assert plat.await_count == 2 and not overlap
    assert not locks.held and all(c.closed for c in locks.connections)


@pytest.mark.asyncio
async def test_a_tour_and_a_platform_job_share_the_one_lock(locks):
    seen = {}

    async def tour_body(*a, **kw):
        seen["tour"] = (rc.LOCK_CLASS, rc.LOCK_KEY) in locks.held
        return {"segment": {}, "ranking": {}, "route": {}}

    with patch("services.export.handler.recompute_segment_score_route", AsyncMock(side_effect=tour_body)), \
            patch("services.export.handler.open_job_pool", AsyncMock(side_effect=lambda: _job_pool())):
        out = await rc.run(_ctx({"scope": "tour", "tour_id": TOUR, "reason": "atom_delete:a1"}))
    assert seen["tour"] is True and out["scope"] == "tour" and not locks.held


@pytest.mark.asyncio
async def test_a_lock_held_too_long_is_a_lock_timeout(locks, monkeypatch):
    monkeypatch.setattr(rc, "LOCK_MAX_WAIT_SECONDS", 0.05)
    plat = AsyncMock()
    async with rc.recompute_lock():
        with patch("services.export.handler.recompute_rankings_and_routes", plat), \
                patch("services.export.handler.open_job_pool", AsyncMock(side_effect=lambda: _job_pool())):
            with pytest.raises(RecomputeLockTimeout, match=f"^{LOCK_TIMEOUT}: "):
                await rc.run(_ctx({"scope": "platform", "reason": "r"}))
    plat.assert_not_awaited()
    assert not isinstance(RecomputeLockTimeout("x"), NonRetryable)    # retryable


@pytest.mark.asyncio
async def test_a_waiting_run_that_is_cancelled_itself_stops_waiting(locks):
    stop = threading.Event()
    async with rc.recompute_lock():
        waiter = asyncio.create_task(rc.recompute_lock(stop.is_set).__aenter__())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        stop.set()
        with pytest.raises(RecomputeStopped):
            await asyncio.wait_for(waiter, 1)
    assert not locks.held


@pytest.mark.asyncio
async def test_a3_tour_lock_still_works_through_the_generic_lock(locks):
    async with handler.a3_tour_lock(TOUR, poll_seconds=0.01):
        assert (handler.A3_TOUR_LOCK_CLASS, TOUR) in locks.held
        # a different lock class never blocks it (the recompute lock is its own key)
        async with rc.recompute_lock():
            assert len(locks.held) == 2
    assert not locks.held


# ── stage failure and cancel ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failing_stage_is_named_in_a_retryable_stage_failure(locks):
    async def fail_in_score(pool, *, log_reason="", segment_ids=None, should_stop=None, on_stage=None):
        on_stage("landing")
        on_stage("score")
        raise KeyError("US")

    with patch("services.export.handler.recompute_rankings_and_routes", AsyncMock(side_effect=fail_in_score)), \
            patch("services.export.handler.open_job_pool", AsyncMock(side_effect=lambda: _job_pool())):
        with pytest.raises(RecomputeStageFailed, match=f"^{STAGE_FAILED}: stage score: KeyError") as ei:
            await rc.run(_ctx({"scope": "platform", "reason": "r"}))
    assert ei.value.stage == "score" and not isinstance(ei.value, NonRetryable)
    assert not locks.held


@pytest.mark.asyncio
async def test_a_cancelled_handler_stops_its_thread_at_the_next_stage_and_drops_late_progress(locks):
    in_landing, may_go_on, thread_done = threading.Event(), threading.Event(), threading.Event()
    ran = []

    async def landing(ctx, scope):
        ran.append("landing")
        in_landing.set()
        while not may_go_on.is_set():
            await asyncio.sleep(0.01)
        ctx.progress({"step": "late"})          # a write after the cancel: dropped
        return {}

    async def score(ctx, scope):
        ran.append("score")
        return {}

    stages = {"segment": SimpleNamespace(name="segment", run=landing), "landing": SimpleNamespace(name="landing",
                                                                                                 run=landing),
              "score": SimpleNamespace(name="score", run=score), "route": SimpleNamespace(name="route", run=score)}

    async def tour_chain(tour_id, pool, *, log_tour_id=None, progress=None, should_stop=None, on_stage=None):
        try:
            return await run_stages(["landing", "score"], RecomputeScope(tour_id=tour_id), pool=pool,
                                    progress=progress, should_stop=should_stop, on_stage=on_stage)
        finally:
            thread_done.set()

    ctx = _ctx({"scope": "tour", "tour_id": TOUR, "reason": "atom_delete:a1"})
    with patch("services.recompute.stages._STAGES", stages), \
            patch("services.export.handler.recompute_segment_score_route", AsyncMock(side_effect=tour_chain)), \
            patch("services.export.handler.open_job_pool", AsyncMock(side_effect=lambda: _job_pool())):
        task = asyncio.create_task(rc.run(ctx))
        await asyncio.to_thread(in_landing.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        may_go_on.set()
        assert await asyncio.to_thread(thread_done.wait, 2)
    await asyncio.sleep(0.05)
    assert ran == ["landing"]                                      # stopped before `score`
    fields = [c.kwargs for c in ctx.progress.await_args_list]
    assert {"step": "late"} not in fields
    for _ in range(100):                                           # the thread let the lock go
        if not locks.held:
            break
        await asyncio.sleep(0.01)
    assert not locks.held


# ── run_stages hooks ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_stages_names_each_stage_and_stops_before_the_next():
    calls, named = [], []

    async def s(ctx, scope):
        calls.append(1)
        return {}

    stages = {"a": SimpleNamespace(name="a", run=s), "b": SimpleNamespace(name="b", run=s)}
    with patch("services.recompute.stages._STAGES", stages):
        out = await run_stages(["a", "b"], RecomputeScope(), pool=None, on_stage=named.append)
        assert out == {"a": {}, "b": {}} and named == ["a", "b"]
        stop_after_first = iter([False, True])
        with pytest.raises(RecomputeStopped, match="before stage b"):
            await run_stages(["a", "b"], RecomputeScope(), pool=None, should_stop=lambda: next(stop_after_first))
    assert len(calls) == 3


# ── the lifecycle lag measure and the sweep ────────────────────────────────────────────────────────────────────────

def test_lag_sql_counts_from_the_first_recompute_started_after_the_event():
    sql = " ".join(lifecycle.LAG_SQL.split())
    assert "j.status = 'succeeded'" in sql and "j.kind = 'recompute'" in sql
    assert "min(rj.finished_at)" in sql and "rj.started_at > e.occurred_at" in sql
    assert lifecycle.lag_args() == (["retired", "paused", "restored"], 30, 1800.0)


def test_lag_sql_reads_the_recompute_jobs_once():
    # C4.3 W46: the per-event correlated subquery over shared.job (repeated once per `fin` reference by the planner)
    # was the top statement by DB time. The succeeded recompute jobs are read once into a MATERIALIZED CTE (a CTE
    # used once is inlined otherwise) and each event takes its min(finished_at) from it through a LATERAL join.
    sql = " ".join(lifecycle.LAG_SQL.split())
    assert sql.count("shared.job") == 1
    assert "WITH rj AS MATERIALIZED (" in sql and "CROSS JOIN LATERAL" in sql
    # the window cut on the jobs is safe: an event's follower started after the event, inside the same window
    assert "j.started_at > now() - make_interval(days => $2)" in sql
    assert "e.occurred_at > now() - make_interval(days => $2)" in sql
    assert "min(j.finished_at)" not in sql                       # the old per-event subquery shape


@pytest.mark.asyncio
async def test_lag_summary_rounds_and_keeps_only_counts_and_seconds():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={
        "events": 12, "followed": 12, "waiting": 0, "waiting_over_limit": 0, "late": 2, "lag_p50_s": 201.54,
        "lag_p95_s": 4000.0, "lag_max_s": 266724.24, "lag_max_min": 4446, "oldest_waiting_s": None})
    out = await lifecycle.lag_summary(db)
    assert out == {"events": 12, "followed": 12, "waiting": 0, "waiting_over_limit": 0, "late": 2, "lag_p50_s": 201.5,
                   "lag_p95_s": 4000.0, "lag_max_s": 266724.2, "oldest_waiting_s": None}
    db.fetchrow = AsyncMock(return_value=None)
    assert await lifecycle.lag_summary(db) == {}


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _sweep_pool(rows):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    pool.acquire = MagicMock(side_effect=lambda: _Acquire(MagicMock()))
    return pool


@pytest.mark.asyncio
async def test_sweep_does_nothing_when_every_event_was_followed():
    pool = _sweep_pool([])
    with patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock()) as enq:
        assert await lifecycle.sweep(pool) == {"events": 0}
    enq.assert_not_awaited()
    sql, events, window, grace = pool.fetch.await_args.args
    assert sql is lifecycle.SWEEP_SQL and events == ["retired", "paused", "restored"]
    assert window == float(lifecycle.SWEEP_WINDOW_S) and grace == float(lifecycle.SWEEP_GRACE_S)


@pytest.mark.asyncio
async def test_sweep_queues_one_platform_recompute_scoped_to_the_swept_tours_segments():
    now = datetime.now(timezone.utc)
    rows = [{"tour_id": "t-a", "event": "retired", "occurred_at": now - timedelta(hours=3)},
            {"tour_id": "t-a", "event": "restored", "occurred_at": now - timedelta(hours=2)},
            {"tour_id": "t-b", "event": "paused", "occurred_at": now - timedelta(hours=1)}]
    segs = {"t-a": ["seg-1", "seg-2"], "t-b": ["seg-2", "seg-3"]}
    with patch("services.export.handler.tour_segment_ids", AsyncMock(side_effect=lambda c, t: segs[t])), \
            patch("services.jobs.recompute_job.enqueue_recompute",
                  AsyncMock(return_value=("job-s", True))) as enq:
        out = await lifecycle.sweep(_sweep_pool(rows))
    enq.assert_awaited_once()
    kw = enq.await_args.kwargs
    assert kw["scope"] == "platform" and kw["segment_ids"] == ["seg-1", "seg-2", "seg-3"]
    assert kw["created_by"] == "scheduler:recompute_sweep" and kw["reason"] == "lifecycle_sweep:3"
    assert out["events"] == 3 and out["tours"] == 2 and out["segments"] == 3 and out["job_id"] == "job-s"
    assert 3 * 3600 - 5 < out["oldest_event_age_s"] < 3 * 3600 + 5


def test_sweep_sql_leaves_events_a_queued_platform_or_a_later_run_already_covers():
    sql = " ".join(lifecycle.SWEEP_SQL.split())
    assert "j.status = 'queued' AND j.payload->>'scope' = 'platform'" in sql
    assert "j.status IN ('running', 'succeeded') AND j.started_at > e.occurred_at" in sql
    assert lifecycle.SWEEP_EVERY_S == 7200 and lifecycle.LAG_LIMIT_S == 1800


# ── the ops check ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_event_waiting_over_30_minutes_is_a_finding():
    tick = {"recompute_lifecycle": {"events": 12, "waiting_over_limit": 1, "oldest_waiting_s": 4000.0}}
    found = [f for f in ops_checks.state_findings(tick) if f.check == "recompute_lifecycle_lag"]
    assert len(found) == 1 and "1 tour on/off event(s)" in found[0].summary and "4000 s" in found[0].summary
    assert ops_checks.check_for("recompute_lifecycle_lag").page == ops_checks.JOB_RUNNER_PAGE


@pytest.mark.parametrize("lag", [{}, {"events": 12, "waiting_over_limit": 0, "late": 2}])
def test_no_finding_when_nothing_waits(lag):
    assert not [f for f in ops_checks.state_findings({"recompute_lifecycle": lag})
                if f.check == "recompute_lifecycle_lag"]


# ── the api ops sampler ────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("due,kinds,swept", [(True, {"recompute": 1}, True), (False, {"recompute": 1}, False),
                                             (True, {}, False)])
async def test_the_part_reads_the_lag_every_tick_and_sweeps_only_when_due(due, kinds, swept):
    out = {}
    with patch.object(lifecycle, "lag_summary", AsyncMock(return_value={"events": 1, "waiting_over_limit": 0})), \
            patch.object(lifecycle, "sweep", AsyncMock(return_value={"events": 1, "job_id": "j"})) as sw:
        await sampler._recompute_lifecycle_part(MagicMock(), kinds, out, due)
    assert out["recompute_lifecycle"] == {"events": 1, "waiting_over_limit": 0}
    assert sw.await_count == (1 if swept else 0) and ("recompute_sweep" in out) is swept


@pytest.mark.asyncio
async def test_the_loop_sweeps_on_the_first_tick_then_every_interval():
    seen = []

    async def fake_sample(pool, client, **kw):
        seen.append(kw["recompute_sweep"])
        return {"sampled_at": "x"}

    app = SimpleNamespace(state=SimpleNamespace(pool=MagicMock()))
    with patch.object(sampler, "sample_once", side_effect=fake_sample), \
            patch.object(sampler, "jev_credit_tick", AsyncMock()), \
            patch.object(sampler, "provider_credit_tick", AsyncMock()):
        task = asyncio.create_task(sampler.run_ops_metrics_sampler(
            app, 0.01, 0, client_factory=lambda: object(), jev_check_every_s=3600, recompute_sweep_every_s=0.2))
        started = time.monotonic()
        while len(seen) < 3 or time.monotonic() - started < 0.3:
            await asyncio.sleep(0.01)
        await sampler.stop_ops_metrics_sampler(task)
    assert seen[0] is True and seen[1] is False
    assert 2 <= seen.count(True) <= 4                    # roughly one sweep per 0.2 s, never one per tick
