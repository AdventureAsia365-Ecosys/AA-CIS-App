"""AA-687 — Jobs page backend pieces that need no database. The SQL (worker rows, reaper totals,
queue depth, LLM calls) is covered against real Postgres in
tests/integration/test_aa687_job_observability.py."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.jobs  # noqa: F401  (registers every kind)
from shared.jobs import queue, registry
from shared.jobs.worker import Worker


@pytest.mark.parametrize("kind,seconds", [
    ("t2_rewrite", 300), ("t9_write", 600), ("a3_atomize", 1800), ("segment_research", 3600),
])
def test_every_kind_has_a_long_running_threshold(kind, seconds):
    assert registry.get_kind(kind).expected_seconds == seconds


def test_default_threshold_is_ten_minutes():
    assert registry.JobKind("x", AsyncMock()).expected_seconds == 600


@pytest.mark.asyncio
async def test_a_failed_liveness_write_never_raises():
    worker = Worker(MagicMock(), worker_id="w1")
    failing = AsyncMock(side_effect=RuntimeError("relation shared.job_worker does not exist"))
    failing.__name__ = "worker_beat"
    await worker._liveness(failing, worker.pool, "w1", 0, None)  # no exception
    failing.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_cost_is_none_when_the_query_fails():
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("db down"))
    assert await Worker(pool, worker_id="w1")._llm_cost("j1") is None


def test_worker_alive_window_is_longer_than_the_beat():
    assert queue.WORKER_ALIVE_SECONDS > Worker(MagicMock()).beat_every_seconds * 3
