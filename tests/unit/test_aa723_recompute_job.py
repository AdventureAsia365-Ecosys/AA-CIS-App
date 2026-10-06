"""AA-723 PR2 nac 1 — the `recompute` job kind + enqueue_recompute debounce (ADR 0003).

The two fire-and-forget recompute tasks (atom-delete -> scope=tour, master-status -> scope=platform)
are now one durable job. Mocks the recompute orchestrators; the real recompute is covered by the
export/handler tests.
"""
from unittest.mock import AsyncMock, patch

import pytest

from services.jobs import recompute_job as rc
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext, NonRetryable

TOUR = "11111111-1111-1111-1111-111111111111"


def _ctx(payload):
    job = queue.Job(id="job-1", kind="recompute", payload=payload, status="running", attempt=1,
                    max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(None, job, {})
    ctx.progress = AsyncMock()
    return ctx


# ── registration ──────────────────────────────────────────────────────────────

def test_recompute_kind_registered_concurrency_1():
    k = registry.get_kind("recompute")
    assert k is not None and k.concurrency == 1 and k.max_attempts == 2


# ── payload validation ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bad_scope_is_not_retryable():
    with pytest.raises(NonRetryable):
        await rc.run(_ctx({"scope": "nonsense"}))


@pytest.mark.asyncio
async def test_tour_scope_without_tour_id_is_not_retryable():
    with pytest.raises(NonRetryable):
        await rc.run(_ctx({"scope": "tour"}))


# ── execution routes to the right orchestrator ──────────────────────────────────

@pytest.mark.asyncio
async def test_tour_scope_calls_segment_score_route():
    seg = AsyncMock(return_value={"segment": 1, "ranking": {}, "route": {}})
    plat = AsyncMock()
    with patch("services.export.handler.recompute_segment_score_route", seg), \
         patch("services.export.handler.recompute_rankings_and_routes", plat), \
         patch("shared.secrets.get_database_url", return_value="postgres://x"), \
         patch("asyncpg.connect", AsyncMock(return_value=AsyncMock())):
        out = await rc.run(_ctx({"scope": "tour", "tour_id": TOUR, "reason": "atom_delete:a1"}))
    seg.assert_awaited_once()
    plat.assert_not_awaited()
    assert out["scope"] == "tour" and out["tour_id"] == TOUR


@pytest.mark.asyncio
async def test_platform_scope_calls_rankings_and_routes():
    seg = AsyncMock()
    plat = AsyncMock(return_value={"ranking": {}, "route": {}})
    with patch("services.export.handler.recompute_segment_score_route", seg), \
         patch("services.export.handler.recompute_rankings_and_routes", plat), \
         patch("shared.secrets.get_database_url", return_value="postgres://x"), \
         patch("asyncpg.connect", AsyncMock(return_value=AsyncMock())):
        out = await rc.run(_ctx({"scope": "platform", "reason": "master_status=active:t1"}))
    plat.assert_awaited_once()
    seg.assert_not_awaited()
    assert out["scope"] == "platform"


# ── enqueue_recompute debounce ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_platform_debounces_against_a_pending_job():
    pending = [{"id": "existing", "payload": {"scope": "platform", "reason": "x"}}]
    with patch("shared.jobs.queue.list_jobs", AsyncMock(return_value=pending)), \
         patch("services.jobs.recompute_job.enqueue", AsyncMock()) as enq:
        job_id, created = await rc.enqueue_recompute(object(), scope="platform", reason="y")
    assert job_id == "existing" and created is False
    enq.assert_not_awaited()  # reused, no new row


@pytest.mark.asyncio
async def test_enqueue_tour_debounces_only_same_tour():
    # pending is a different tour -> must NOT debounce, must enqueue a new job
    pending = [{"id": "other", "payload": {"scope": "tour", "tour_id": "ffffffff-0000-0000-0000-000000000000"}}]
    with patch("shared.jobs.queue.list_jobs", AsyncMock(return_value=pending)), \
         patch("services.jobs.recompute_job.enqueue", AsyncMock(return_value=("new", True))) as enq:
        job_id, created = await rc.enqueue_recompute(object(), scope="tour", tour_id=TOUR, reason="d")
    assert job_id == "new" and created is True
    enq.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_fresh_platform_creates_a_job():
    with patch("shared.jobs.queue.list_jobs", AsyncMock(return_value=[])), \
         patch("services.jobs.recompute_job.enqueue", AsyncMock(return_value=("new", True))) as enq:
        job_id, created = await rc.enqueue_recompute(object(), scope="platform", reason="z")
    assert job_id == "new" and created is True
    enq.assert_awaited_once()
    # payload carries scope + reason, no tour_id for platform
    payload = enq.await_args.args[2]
    assert payload["scope"] == "platform" and "tour_id" not in payload


@pytest.mark.asyncio
async def test_enqueue_tour_scope_requires_tour_id():
    with pytest.raises(ValueError):
        await rc.enqueue_recompute(object(), scope="tour", reason="d")
