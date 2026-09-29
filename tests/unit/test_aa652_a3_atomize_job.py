"""AA-652 PR-C — A3 atomize as a durable job (services/jobs/a3_atomize_job.py)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.jobs import a3_atomize_job as a3
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext, NonRetryable

PAYLOAD = {"tour_id": "t-1", "version_id": "v-1", "country": "Vietnam",
           "rewritten": {"name": "Sapa", "summary": "", "highlights": "[]", "itineraries": ""}}


def _ctx(payload=PAYLOAD):
    job = queue.Job(id="j1", kind=a3.KIND, payload=payload, status="running", attempt=1,
                    max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(MagicMock(), job, {})
    ctx.progress = AsyncMock()
    return ctx


def test_kind_runs_one_tour_at_a_time():
    k = registry.get_kind(a3.KIND)
    assert k is not None and k.concurrency == 1 and k.max_attempts == 2


@pytest.mark.asyncio
async def test_runs_the_existing_atomize_chain_and_surfaces_failures():
    with patch("services.export.handler._run_a3_atomize_background", AsyncMock()) as m_bg:
        out = await a3.run(_ctx())
    kwargs = m_bg.call_args.kwargs
    assert kwargs["tour_id"] == "t-1" and kwargs["version_id"] == "v-1" and kwargs["reraise"] is True
    assert out == {"tour_id": "t-1", "version_id": "v-1"}


@pytest.mark.asyncio
async def test_bad_payload_is_not_retryable():
    with pytest.raises(NonRetryable):
        await a3.run(_ctx({"tour_id": "t-1"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("dedupe,key", [(True, "a3_atomize:v-1"), (False, None)])
async def test_enqueue_keys_on_the_content_version_only_for_the_publish_path(dedupe, key):
    with patch.object(a3, "enqueue", AsyncMock(return_value=("job-1", True))) as m:
        job_id = await a3.enqueue_a3_atomize(MagicMock(), tour_id="t-1", version_id="v-1", country="VN",
                                             rewritten={}, created_by="x", dedupe=dedupe)
    assert job_id == "job-1" and m.call_args.kwargs["idempotency_key"] == key


@pytest.mark.asyncio
async def test_reraise_propagates_an_atomize_error():
    from services.export import handler
    conn = AsyncMock()
    with patch.object(handler.asyncpg, "connect", AsyncMock(return_value=conn)), \
         patch("services.acp_produce.tenant_pipeline.run_t5_atomize", AsyncMock(side_effect=RuntimeError("bedrock"))):
        with pytest.raises(RuntimeError):
            await handler._run_a3_atomize_background("t-1", {}, "", "v-1", reraise=True)
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_survives_a_failed_enqueue(monkeypatch):
    """The publish is already committed when the atomize job is enqueued; an enqueue failure is
    logged, not raised (same contract as the in-process task it replaced)."""
    from services.export import handler
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"id": "gc-1", "tour_id": "t-1", "tenant_id": "t-1", "batch_id": None, "aa_name": "Tour",
         "aa_subtitle": "s", "aa_summary": "sum", "aa_description": "d", "aa_highlights": "[]",
         "aa_itineraries": "Day 1...", "mobile_card_text": None, "seo_title": "t", "seo_meta": "m" * 150,
         "seo_keywords_used": "[]", "og_tags": "{}", "quality_score_id": None, "quality_score": 9.0,
         "country": "Vietnam", "duration": "5 days"},
        {"id": "gc-1"},
    ]
    with patch.object(handler.asyncpg, "connect", AsyncMock(return_value=conn)), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock(side_effect=RuntimeError("db"))):
        out = await handler.process_export("gc-1")
    assert out["status"] == "exported"
