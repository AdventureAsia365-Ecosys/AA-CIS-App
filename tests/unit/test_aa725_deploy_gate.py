"""AA-725 part 1 — the pre-deploy guard endpoint.

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
    assert out == {"active": 3, "by_status": {"queued": 2, "running": 1}}
    # No time window: the guard must see every outstanding job, however old.
    sql = pool.fetch.call_args[0][0]
    assert "interval" not in sql.lower()
    assert "status IN ('queued', 'running')" in sql


@pytest.mark.asyncio
async def test_active_count_zero_when_idle():
    assert await queue.active_count(_pool_with_counts([])) == {"active": 0, "by_status": {}}


@pytest.mark.asyncio
async def test_deploy_gate_unsafe_when_jobs_active():
    from api.routers import admin_job_runner

    pool = _pool_with_counts([{"status": "running", "n": 1}])
    result = await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret=_TEST_SECRET)
    assert result["active"] == 1
    assert result["safe_to_deploy"] is False


@pytest.mark.asyncio
async def test_deploy_gate_safe_when_idle():
    from api.routers import admin_job_runner

    pool = _pool_with_counts([])
    result = await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret=_TEST_SECRET)
    assert result == {"active": 0, "by_status": {}, "safe_to_deploy": True}


@pytest.mark.asyncio
async def test_deploy_gate_rejects_wrong_secret():
    from fastapi import HTTPException

    from api.routers import admin_job_runner

    pool = _pool_with_counts([])
    with pytest.raises(HTTPException) as exc:
        await admin_job_runner.deploy_gate(_make_request(pool), x_admin_secret="wrong")
    assert exc.value.status_code == 403
