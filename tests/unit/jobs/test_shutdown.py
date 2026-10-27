"""AA-993 G1 (ADR 0016 D1) — worker drain on shutdown, without a database.

- the drain window comes from JOB_WORKER_GRACE_SECONDS and is checked against the ECS stopTimeout;
- a job that ends within the window completes; a longer one is released (with the kind's release delay);
- when a worker of a newer task revision is alive (a deploy), the release does not count toward MAX_RELEASES;
- the registry fields, the test-only kind noop_sleep and GET /admin/job-runner/released.

The SQL itself (claim -> SIGTERM -> release -> re-claim, the release counter) runs on Postgres in
tests/integration/test_aa993_worker_drain.py.
"""
from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from shared.jobs import queue, registry
from shared.jobs import worker as worker_mod
from shared.jobs.worker import Worker


# ── drain window ────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def no_grace_env(monkeypatch):
    monkeypatch.delenv("JOB_WORKER_GRACE_SECONDS", raising=False)
    monkeypatch.delenv("JOB_WORKER_STOP_TIMEOUT_SECONDS", raising=False)


def test_grace_defaults_to_the_pre_g1_window_when_the_env_is_absent(no_grace_env):
    assert worker_mod.worker_grace_seconds() == 20.0


def test_grace_comes_from_the_env(no_grace_env, monkeypatch):
    monkeypatch.setenv("JOB_WORKER_GRACE_SECONDS", "100")   # design Q1: 100 s of the 120 s stopTimeout
    assert worker_mod.worker_grace_seconds() == 100.0


def test_grace_up_to_the_margin_is_accepted(no_grace_env, monkeypatch):
    monkeypatch.setenv("JOB_WORKER_GRACE_SECONDS", "105")
    assert worker_mod.worker_grace_seconds() == 105.0


@pytest.mark.parametrize("grace, stop", [
    ("106", None),          # leaves less than 15 s of the default 120 s stopTimeout
    ("100", "100"),         # a shorter stopTimeout
    ("20", "30"),           # the ECS default stopTimeout is too short for 20 s + margin
    ("-1", None), ("abc", None), ("nan", None), ("inf", None),
    ("20", "abc"),
])
def test_bad_grace_refuses_to_start(no_grace_env, monkeypatch, grace, stop):
    monkeypatch.setenv("JOB_WORKER_GRACE_SECONDS", grace)
    if stop is not None:
        monkeypatch.setenv("JOB_WORKER_STOP_TIMEOUT_SECONDS", stop)
    with pytest.raises(ValueError):
        worker_mod.worker_grace_seconds()


def test_the_worker_class_default_is_unchanged():
    """The in-API worker (local/dev, JOB_WORKER_IN_API) keeps 20 s; only the service worker reads the env."""
    assert Worker(MagicMock(), worker_id="w").grace_seconds == 20.0


# ── one worker, mocked queue ────────────────────────────────────────────────────────────────────

@pytest.fixture
def kinds(monkeypatch):
    monkeypatch.setattr(registry, "_KINDS", {})

    @registry.job_kind("quick_job")
    async def quick_job(ctx):
        await asyncio.sleep(0.05)
        return {"ok": True}

    @registry.job_kind("long_job", release_delay_seconds=120)
    async def long_job(ctx):
        await asyncio.sleep(30)


def _job(kind: str) -> queue.Job:
    return queue.Job(id=f"00000000-0000-0000-0000-00000000000{1 if kind == 'quick_job' else 2}", kind=kind,
                     payload={}, status="running", attempt=1, max_attempts=3, progress={}, cost_usd=0.0)


def _pool():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=0.0)   # Worker._llm_cost
    return pool


async def _run_one(kind: str, *, grace: float, newer_alive=(False, False), release_status="queued",
                   monkeypatch=None, revision="416"):
    """Claims one job of `kind`, then shuts the worker down. Returns (complete mock, release mock, logs)."""
    monkeypatch.setenv("JOB_WORKER_TASK_REVISION", revision)
    w = Worker(_pool(), worker_id="w-old", grace_seconds=grace, heartbeat_seconds=0.05)
    complete = AsyncMock(return_value=True)
    release = AsyncMock(return_value=release_status)
    with patch.object(queue, "newer_worker_alive", AsyncMock(side_effect=list(newer_alive))), \
            patch.object(queue, "claim", AsyncMock(side_effect=[_job(kind), None])), \
            patch.object(queue, "heartbeat", AsyncMock(return_value=(True, False))), \
            patch.object(queue, "complete", complete), patch.object(queue, "release", release), \
            patch.object(queue, "worker_stopped", AsyncMock()), \
            capture_logs() as logs:
        assert await w._fill_slots() == 1
        await asyncio.sleep(0.01)
        await w.shutdown()
    return complete, release, logs


def _events(logs, name):
    return [entry for entry in logs if entry["event"] == name]


@pytest.mark.asyncio
async def test_a_job_within_the_grace_window_completes(kinds, monkeypatch):
    complete, release, logs = await _run_one("quick_job", grace=2.0, monkeypatch=monkeypatch)
    complete.assert_awaited_once()
    release.assert_not_awaited()
    draining = _events(logs, "job_worker_draining")
    assert draining and draining[0]["grace_seconds"] == 2.0 and draining[0]["kinds"] == ["quick_job"]
    assert _events(logs, "job_worker_drained")[0]["released"] == 0
    assert not _events(logs, "job_released_on_shutdown")


@pytest.mark.asyncio
async def test_a_longer_job_is_released_with_the_kind_delay(kinds, monkeypatch):
    complete, release, logs = await _run_one("long_job", grace=0.1, monkeypatch=monkeypatch)
    complete.assert_not_awaited()
    release.assert_awaited_once()
    args, kw = release.await_args
    assert args[1:3] == (_job("long_job").id, "w-old")
    assert kw == {"count_release": True, "delay_seconds": 120}   # no newer worker: the release counts
    released = _events(logs, "job_released_on_shutdown")
    assert released and released[0]["counted"] is True and released[0]["release_delay_seconds"] == 120
    assert released[0]["next_status"] == "queued"
    assert _events(logs, "job_worker_drained")[0]["released"] == 1


@pytest.mark.asyncio
async def test_newer_revision_alive_the_release_is_not_counted(kinds, monkeypatch):
    # claim time: no newer worker (it claims); release time: the deploy's new revision is live
    complete, release, logs = await _run_one("long_job", grace=0.1, newer_alive=(False, True),
                                             monkeypatch=monkeypatch)
    assert release.await_args.kwargs["count_release"] is False
    assert _events(logs, "job_released_on_shutdown")[0]["counted"] is False


@pytest.mark.asyncio
async def test_a_failed_newer_check_counts_the_release(kinds, monkeypatch):
    """Unknown = as before G1: the release counts, so a job that keeps killing its worker still ends."""
    w = Worker(_pool(), worker_id="w")
    with patch.object(queue, "newer_worker_alive", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await w._newer_worker_alive_safe() is False


@pytest.mark.asyncio
async def test_a_counted_release_that_fails_the_job_runs_the_terminal_hook(kinds, monkeypatch):
    with patch.object(worker_mod, "run_terminal_hook", AsyncMock()) as hook:
        await _run_one("long_job", grace=0.1, release_status="failed", monkeypatch=monkeypatch)
    hook.assert_awaited_once()


# ── queue.release arguments (the SQL runs in the integration test) ──────────────────────────────

@pytest.mark.asyncio
async def test_release_passes_the_count_flag_and_delay():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"status": "queued"})
    assert await queue.release(pool, "j", "w", 0.5, count_release=False, delay_seconds=120) == "queued"
    sql, *args = pool.fetchrow.await_args.args
    assert args == ["j", "w", 0.5, queue.MAX_RELEASES, False, 120, queue.RELEASE_LOG_KEEP]
    # an uncounted release adds 0 to progress.releases and can never fail the job
    assert "CASE WHEN $5 THEN 1 ELSE 0 END" in sql and "WHEN $5 AND cur.releases >= $4 THEN 'failed'" in sql
    assert "run_after = now() + make_interval(secs => $6)" in sql and "'release_log'" in sql


@pytest.mark.asyncio
async def test_release_defaults_count_and_are_due_at_once():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await queue.release(pool, "j", "w", None) == ""   # no longer ours
    args = pool.fetchrow.await_args.args[1:]
    assert args[4] is True and args[5] == 0


@pytest.mark.asyncio
async def test_released_since_sums_the_kinds():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[
        {"kind": "noop_sleep", "released": 2, "counted": 0, "jobs": 1},
        {"kind": "s1_rewrite", "released": 1, "counted": 1, "jobs": 1},
    ])
    since = datetime(2026, 10, 27, tzinfo=timezone.utc)
    out = await queue.released_since(pool, since)
    assert out == {"released": 3, "counted": 1, "jobs": 2, "by_kind": {
        "noop_sleep": {"released": 2, "counted": 0, "jobs": 1},
        "s1_rewrite": {"released": 1, "counted": 1, "jobs": 1}}}
    sql, arg = pool.fetch.await_args.args
    assert arg == since and "release_log" in sql and "j.updated_at >= $1" in sql


# ── registry ────────────────────────────────────────────────────────────────────────────────────

def test_registry_defaults(monkeypatch):
    monkeypatch.setattr(registry, "_KINDS", {})
    registry.job_kind("plain")(AsyncMock())
    k = registry.get_kind("plain")
    assert k.release_delay_seconds == 0 and k.deploy_blocking is False and k.deploy_gate_exempt is False


def test_registry_keeps_the_new_fields(monkeypatch):
    monkeypatch.setattr(registry, "_KINDS", {})
    registry.job_kind("thready", release_delay_seconds=120, deploy_blocking=True)(AsyncMock())
    k = registry.get_kind("thready")
    assert k.release_delay_seconds == 120 and k.deploy_blocking is True


@pytest.mark.parametrize("kw", [{"release_delay_seconds": -1},
                                {"deploy_blocking": True, "deploy_gate_exempt": True}])
def test_registry_refuses_contradictions(monkeypatch, kw):
    monkeypatch.setattr(registry, "_KINDS", {})
    with pytest.raises(ValueError):
        registry.job_kind("bad", **kw)


# ── noop_sleep (test-only kind) ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, registered", [(None, False), ("", False), ("0", False), ("1", True),
                                               ("true", True)])
def test_noop_sleep_is_registered_only_with_job_test_kinds(monkeypatch, value, registered):
    from services.jobs import noop_sleep_job
    monkeypatch.setattr(registry, "_KINDS", {})
    if value is None:
        monkeypatch.delenv("JOB_TEST_KINDS", raising=False)
    else:
        monkeypatch.setenv("JOB_TEST_KINDS", value)
    importlib.reload(noop_sleep_job)
    assert ("noop_sleep" in registry.kinds()) is registered
    if registered:
        k = registry.get_kind("noop_sleep")
        assert k.deploy_gate_exempt and not k.deploy_blocking and k.max_attempts == 1


@pytest.mark.asyncio
async def test_noop_sleep_is_exempt_from_the_deploy_gate(monkeypatch):
    from services.jobs import noop_sleep_job
    monkeypatch.setattr(registry, "_KINDS", {})
    noop_sleep_job.register()
    with patch.object(worker_mod, "load_kinds", lambda: None), \
            patch.object(queue, "active_count", AsyncMock(return_value={"active": 0, "by_status": {},
                                                                         "scheduled_later": 0})) as active, \
            patch.object(queue, "deploy_blockers", AsyncMock(return_value={"open_waves": 0, "blocking_running": {},
                                                                            "blocking_due": {}})):
        out = await worker_mod.deploy_gate_status(MagicMock())
    assert out["exempt_kinds"] == ["noop_sleep"] and out["safe_to_deploy"] is True
    assert active.await_args.kwargs["exempt_kinds"] == ["noop_sleep"]


def _ctx(payload, progress=None):
    ctx = MagicMock()
    ctx.payload = payload
    ctx.job = MagicMock(progress=progress or {})
    ctx.progress = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_noop_sleep_sleeps_and_resumes_from_its_progress(monkeypatch):
    from services.jobs import noop_sleep_job
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(noop_sleep_job.asyncio, "sleep", fake_sleep)
    out = await noop_sleep_job.run(_ctx({"seconds": 12}, {"slept_seconds": 5}))
    assert slept == [5.0, 2.0]   # resumed at 5 s: 7 s left in steps of 5
    assert out == {"slept_seconds": 12.0, "resumed_at_seconds": 5.0}


@pytest.mark.parametrize("payload", [{}, {"seconds": 0}, {"seconds": 3601}, {"seconds": "300"},
                                     {"seconds": True}])
@pytest.mark.asyncio
async def test_noop_sleep_rejects_a_bad_payload(payload):
    from services.jobs import noop_sleep_job
    with pytest.raises(registry.NonRetryable):
        await noop_sleep_job.run(_ctx(payload))


# ── GET /admin/job-runner/released ──────────────────────────────────────────────────────────────

def _request():
    req = MagicMock()
    req.app.state.pool = MagicMock()
    return req


@pytest.mark.asyncio
async def test_released_endpoint_defaults_to_the_last_24_hours():
    from api.routers import admin_job_runner as r
    out_q = {"released": 1, "counted": 0, "jobs": 1, "by_kind": {"noop_sleep": {"released": 1, "counted": 0,
                                                                                "jobs": 1}}}
    with patch.object(queue, "released_since", AsyncMock(return_value=out_q)) as rs:
        out = await r.released(_request(), since=None)
    since = rs.await_args.args[1]
    assert abs((datetime.now(timezone.utc) - timedelta(hours=24) - since).total_seconds()) < 5
    assert out["released"] == 1 and out["counted"] == 0 and out["max_releases"] == queue.MAX_RELEASES
    assert out["since"] == since.isoformat()


@pytest.mark.asyncio
async def test_released_endpoint_reads_since_as_utc_and_refuses_garbage():
    from fastapi import HTTPException

    from api.routers import admin_job_runner as r
    with patch.object(queue, "released_since", AsyncMock(return_value={"released": 0, "counted": 0, "jobs": 0,
                                                                      "by_kind": {}})) as rs:
        await r.released(_request(), since="2026-10-27T08:00:00")
    assert rs.await_args.args[1] == datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
    with pytest.raises(HTTPException) as e:
        await r.released(_request(), since="yesterday")
    assert e.value.status_code == 422


def test_released_endpoint_is_a_machine_get():
    from api.core.staff_authz import MACHINE_ROUTES
    assert ("GET", "/admin/job-runner/released") in MACHINE_ROUTES
