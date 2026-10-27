"""AA-725 part 1 / AA-993 G5 — the pre-deploy guard endpoint.

Since AA-993 G5 (design D1.4) the gate is unsafe only while a rerun wave is open or a job of a
`deploy_blocking` kind runs; queued/running counts stay in the answer as information.

CI has no RDS access (private subnet), so the deploy workflow asks the live API how many jobs are
queued or running before it rolls the containers. A deploy restarts them and would interrupt any
such job (S212: 3 PRs merged mid Thailand wave killed 16 S1 jobs). The endpoint counts with NO
time filter and is gated by the admin secret.

Mocks the asyncpg pool — no live DB. Same x-admin-secret convention as test_aa527_admin_dashboard.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.jobs import queue

_TEST_SECRET = "test-admin-secret"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)


def _make_request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


def _pool_with_counts(rows):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    return pool


@pytest.mark.asyncio
async def test_active_count_sums_queued_and_running():
    pool = _pool_with_counts([
        {"status": "queued", "n": 2},
        {"status": "running", "n": 1},
    ])
    out = await queue.active_count(pool)
    assert out == {"active": 3, "by_status": {"queued": 2, "running": 1}, "scheduled_later": 0}
    # No lower time bound: the guard must see every outstanding job, however old. AA-990: only a queued row due more
    # than DEPLOY_GATE_HORIZON_S AHEAD is set apart (scheduled_later), never an old one.
    sql = pool.fetch.call_args[0][0]
    assert "now() -" not in sql and "run_after <" not in sql
    assert "run_after > now() + make_interval(secs => $2)" in sql
    assert pool.fetch.call_args[0][2] == queue.DEPLOY_GATE_HORIZON_S
    assert "status IN ('queued', 'running')" in sql


@pytest.mark.asyncio
async def test_active_count_sets_far_future_queued_rows_apart():
    """AA-990: the place_geocode run queued for the next Geoapify credit reset (up to ~24 h ahead) does not block."""
    pool = _pool_with_counts([{"status": "scheduled_later", "n": 1}])
    assert await queue.active_count(pool) == {"active": 0, "by_status": {}, "scheduled_later": 1}
    pool = _pool_with_counts([{"status": "scheduled_later", "n": 1}, {"status": "queued", "n": 2}])
    assert await queue.active_count(pool) == {"active": 2, "by_status": {"queued": 2}, "scheduled_later": 1}


@pytest.mark.asyncio
async def test_active_count_zero_when_idle():
    assert await queue.active_count(_pool_with_counts([])) == {"active": 0, "by_status": {}, "scheduled_later": 0}


def _gate_pool(counts=(), waves=0, blocking=(), due=()):
    """A pool whose fetch answers active_count (shared.job counts) and deploy_blockers (open waves + running and due
    queued deploy-blocking kinds, recognised by the rerun_wave read) separately."""
    async def fetch(sql, *args):
        if "shared.rerun_wave" in sql:
            return [{"what": "wave", "kind": None, "n": waves},
                    *({"what": "running", "kind": k, "n": n} for k, n in blocking),
                    *({"what": "due", "kind": k, "n": n} for k, n in due)]
        return list(counts)
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=fetch)
    return pool


@pytest.mark.asyncio
async def test_deploy_blockers_reads_open_waves_and_running_blocking_kinds():
    pool = _gate_pool(waves=2, blocking=[("s1_batch_ingest", 1), ("data_erasure", 1)], due=[("data_erasure", 2)])
    out = await queue.deploy_blockers(pool, blocking_kinds=["data_erasure", "s1_batch_ingest"])
    assert out == {"open_waves": 2, "blocking_running": {"data_erasure": 1, "s1_batch_ingest": 1},
                   "blocking_due": {"data_erasure": 2}}
    sql, kinds_arg, horizon = pool.fetch.call_args[0]
    assert "FROM shared.rerun_wave WHERE status = 'open'" in sql
    assert "kind = ANY($1::text[])" in sql
    assert "status = 'queued' AND run_after <= now() + make_interval(secs => $2)" in sql
    assert kinds_arg == ["data_erasure", "s1_batch_ingest"]
    assert horizon == queue.DEPLOY_BLOCKING_HORIZON_S == 900


@pytest.mark.asyncio
async def test_deploy_gate_safe_with_queued_and_running_jobs_and_no_wave():
    """AA-993 G5: ordinary queued/running jobs no longer block (the worker drains and releases them)."""
    from api.routers import admin_job_runner

    pool = _gate_pool(counts=[{"status": "queued", "n": 4}, {"status": "running", "n": 3}])
    result = await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret=_TEST_SECRET)
    assert result["safe_to_deploy"] is True and result["blocked_by"] == []
    assert result["active"] == 7 and result["by_status"] == {"queued": 4, "running": 3}
    assert result["open_waves"] == 0 and result["blocking_running"] == {}


@pytest.mark.asyncio
async def test_deploy_gate_unsafe_while_a_wave_is_open():
    from api.routers import admin_job_runner

    pool = _gate_pool(waves=1)
    result = await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret=_TEST_SECRET)
    assert result["safe_to_deploy"] is False and result["open_waves"] == 1
    assert result["blocked_by"] == ["1 rerun wave(s) open"]
    assert result["active"] == 0  # a wave with nothing queued yet (a 1-tour test wave, not dispatched) still blocks


@pytest.mark.asyncio
async def test_deploy_gate_unsafe_while_a_blocking_kind_runs():
    from api.routers import admin_job_runner

    pool = _gate_pool(counts=[{"status": "running", "n": 1}], blocking=[("data_erasure", 1)])
    result = await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret=_TEST_SECRET)
    assert result["safe_to_deploy"] is False
    assert result["blocking_running"] == {"data_erasure": 1}
    assert result["blocked_by"] == ["1 data_erasure job(s) running (deploy-blocking kind)"]
    assert result["blocking_due"] == {}
    # the blocking kinds come from the registry flag
    _, kinds_arg, _ = [c for c in pool.fetch.call_args_list if "rerun_wave" in c[0][0]][0][0]
    assert {"data_erasure", "s1_batch_ingest"} <= set(kinds_arg) == set(result["blocking_kinds"])


@pytest.mark.asyncio
async def test_deploy_gate_unsafe_while_a_blocking_kind_is_queued_and_due():
    """AA-993 G5 review: the guard runs before the image build; the old task would claim a due queued row of a
    blocking kind before its SIGTERM and be cut mid-run, so such a row blocks too (a far-future one does not: the
    SQL horizon leaves it out)."""
    from api.routers import admin_job_runner

    pool = _gate_pool(counts=[{"status": "queued", "n": 1}], due=[("data_erasure", 1)])
    result = await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret=_TEST_SECRET)
    assert result["safe_to_deploy"] is False
    assert result["blocking_running"] == {} and result["blocking_due"] == {"data_erasure": 1}
    assert result["blocked_by"] == ["1 data_erasure job(s) due (deploy-blocking kind, queued to start within 15 min)"]


@pytest.mark.asyncio
async def test_deploy_gate_fails_closed_when_the_wave_read_fails():
    from api.routers import admin_job_runner

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=[[], RuntimeError("relation shared.rerun_wave does not exist")])
    with pytest.raises(RuntimeError):
        await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret=_TEST_SECRET)


@pytest.mark.asyncio
async def test_deploy_gate_safe_when_idle():
    from api.routers import admin_job_runner

    pool = _gate_pool()
    result = await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret=_TEST_SECRET)
    # AA-832: exempt kinds (deploy_gate_exempt) are listed, not counted; AA-751: none left (noop_sleep is test-only)
    assert result == {"active": 0, "by_status": {}, "scheduled_later": 0, "safe_to_deploy": True, "exempt_kinds": [],
                      "open_waves": 0, "blocking_kinds": result["blocking_kinds"], "blocking_running": {},
                      "blocking_due": {}, "blocked_by": []}


def test_the_deploy_blocking_kinds_are_the_ones_not_safe_to_rerun():
    """AA-993 G5: the registry flags exactly the kinds a release cannot safely re-run. s1_rewrite and a3_atomize are
    drain-safe since G2/G3; noop_sleep is exempt. AA-919 WM-19/WM-21: a released R2 / shadow run would start over
    and pay again (max_attempts 1, a release is not an attempt), so both block a deploy too."""
    from shared.jobs import registry
    from shared.jobs.worker import load_kinds

    load_kinds()
    assert sorted(k.name for k in registry.kinds().values() if k.deploy_blocking) == [
        "data_erasure", "s1_batch_ingest", "writing_memory_r2", "writing_memory_shadow"]


@pytest.mark.asyncio
async def test_deploy_gate_rejects_wrong_secret():
    from fastapi import HTTPException

    from api.routers import admin_job_runner

    pool = _pool_with_counts([])
    with pytest.raises(HTTPException) as exc:
        await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret="wrong")
    assert exc.value.status_code == 403
