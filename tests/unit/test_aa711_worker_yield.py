"""AA-711 — a worker on a draining (older) ECS task stops claiming once a newer worker is live."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.jobs import queue
from shared.jobs.worker import Worker, ecs_task_revision


def test_revision_from_env_override(monkeypatch):
    monkeypatch.setenv("JOB_WORKER_TASK_REVISION", "416")
    assert ecs_task_revision() == 416


def test_revision_none_outside_ecs(monkeypatch):
    monkeypatch.delenv("JOB_WORKER_TASK_REVISION", raising=False)
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    assert ecs_task_revision() is None


@pytest.mark.asyncio
async def test_unknown_revision_never_yields():
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=AssertionError("no query without a revision"))
    assert await queue.newer_worker_alive(pool, None) is False


@pytest.mark.asyncio
async def test_newer_worker_query_compares_revision():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=True)
    assert await queue.newer_worker_alive(pool, 416) is True
    sql, rev = pool.fetchval.await_args.args
    assert "task_revision > $1" in sql and "stopped_at IS NULL" in sql and rev == 416


@pytest.mark.asyncio
async def test_old_worker_stops_claiming(monkeypatch):
    monkeypatch.setenv("JOB_WORKER_TASK_REVISION", "416")
    w = Worker(MagicMock(), worker_id="old")
    with patch.object(queue, "newer_worker_alive", AsyncMock(return_value=True)), \
            patch.object(queue, "claim", AsyncMock(side_effect=AssertionError("must not claim"))):
        assert await w._fill_slots() == 0
    assert w.yielding is True


@pytest.mark.asyncio
async def test_current_worker_claims(monkeypatch):
    monkeypatch.setenv("JOB_WORKER_TASK_REVISION", "417")
    w = Worker(MagicMock(), worker_id="new")
    with patch.object(queue, "newer_worker_alive", AsyncMock(return_value=False)), \
            patch.object(queue, "claim", AsyncMock(return_value=None)) as claim:
        assert await w._fill_slots() == 0
    claim.assert_awaited_once()
    assert w.yielding is False
