"""AA-642 A8 (review S263) — a fail-closed tenant budget refusal reaches the job runner as a retryable failure.

``TenantBudgetUnavailable`` (the tenant's spend counter could not be read) used to be swallowed by the T-stage
pipelines: the T2 generate node turned it into a failed draft and the T9 write marked the piece 'failed'. These tests
drive each pipeline with the refusal and check that the job runner sees it (retryable, not account-level) and that
the piece / version is left for the retry.

No database (the in-memory store of test_aa1016_approval_requeue behind a fake pool), no provider call. Invented
tenants and places only.
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.jobs.worker import failure_outcome
from shared.llm_client import tenant_budget
from tests.unit.test_aa1016_approval_requeue import PIECE_ID, REQUEST_ID, TENANT, VERSION_ID, Pool, Store, _budget, _ctx

import services.jobs.t2_rewrite_job  # noqa: F401,E402 — registers the kinds
import services.jobs.t9_write_job  # noqa: F401,E402


def _unavailable():
    return tenant_budget.counter_unavailable(TENANT)


def _retryable(exc: BaseException) -> None:
    error, retryable, reason = failure_outcome(exc)
    assert retryable and reason is None, (error, retryable, reason)
    assert error.startswith("TenantBudgetUnavailable: " + tenant_budget.UNREADABLE_REASON)


T9_CONTEXT = {"atom_text": "Velmora Ridge has a lake.", "goal": {"key": "promotion"},
              "channel_style": {"channel": "facebook"}, "brand_audience": {}, "chosen": {}, "cta": "Ask us.",
              "destination": None, "trip_name": None, "brand_rubric_text": "", "channel": "facebook", "atom_id": "a1",
              "tenant_id": TENANT}


@pytest.mark.asyncio
async def test_the_t9_write_raises_the_unreadable_counter_instead_of_a_failed_piece():
    from services.acp_content_writing import service as writing_service
    with patch.object(writing_service, "write_content", side_effect=_unavailable()), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        with pytest.raises(tenant_budget.TenantBudgetUnavailable):
            await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), dict(T9_CONTEXT),
                                                       pool=MagicMock())
    fin.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_t9_job_refused_by_an_unreadable_counter_retries_and_keeps_its_piece():
    """Through the real t9_write handler and the real T9 service: the job fails retryable, the piece stays
    'processing' (no on_terminal on a retry), and the retry attempt writes it."""
    from services.acp_content_writing import service as writing_service
    from services.jobs import t9_write_job as t9
    payload = {"request_id": REQUEST_ID, "piece_id": PIECE_ID, "tenant_id": TENANT, "context": dict(T9_CONTEXT)}
    store = Store("t9_write", "processing", payload, _budget())
    with patch.object(writing_service, "write_content", side_effect=_unavailable()), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        with pytest.raises(Exception) as ei:
            await t9.run(_ctx(Pool(store), "t9_write", payload))
    fin.assert_not_awaited()
    _retryable(ei.value)
    assert store.domain == "processing"

    async def writes(request_id, piece_id, context, pool):
        store.domain = "approved"
    with patch.object(t9.service, "run_write_background", AsyncMock(side_effect=writes)) as bg:
        out = await t9.run(_ctx(Pool(store), "t9_write", payload))
    bg.assert_awaited_once()
    assert out == {"piece_id": PIECE_ID, "piece_status": "approved"}


def test_the_t2_graph_and_rewrite_tour_raise_the_unreadable_counter():
    from api.routers import v1_pipeline as vp
    from services.content_generation import graph
    refusal = _unavailable()
    state = {"tour": {"name": "Velmora Ridge Trek", "country": "VN", "duration": "2 Days", "itineraries": ""},
             "seo": {}, "few_shots": [], "generated": {}, "retry_count": 0, "feedback": "", "error": "",
             "cost_usd": 0.0, "model_used": "", "brand_system_prompt": "", "brand_style_guide": "",
             "brand_forbidden_words": [], "rewrite_language": "en-US", "model_tier": "haiku",
             "is_tenant_rewrite": True, "is_branded": True, "tenant_id": TENANT}
    with patch("services.content_generation.graph.LLMClient") as client:
        client.return_value.generate.side_effect = refusal
        with pytest.raises(tenant_budget.TenantBudgetUnavailable):
            graph.generate_node(state)

    class _Graph:
        async def astream(self, *_a, **_k):
            raise RuntimeError("node failed") from refusal
            yield  # pragma: no cover

    with patch.object(vp, "build_graph", return_value=_Graph()):
        with pytest.raises(tenant_budget.TenantBudgetUnavailable) as ei:
            asyncio.run(vp._rewrite_tour({"name": "T", "country": "VN", "duration": "2 days"}, 0, 1,
                                         brand_rules={}, seo={}))
    assert ei.value is refusal


@pytest.mark.asyncio
async def test_a_t2_job_refused_by_an_unreadable_counter_retries_and_keeps_its_version():
    from services.jobs import t2_rewrite_job as t2
    payload = {"version_id": VERSION_ID, "tenant_id": TENANT, "published_tour_id": "p-1",
               "rewrite_language": "EN-US"}
    store = Store("t2_rewrite", "pending", payload, _budget())
    prepare = AsyncMock(return_value=({"name": "Velmora Ridge Trek"}, {"_brand_loaded": True}))
    facts = AsyncMock(side_effect=lambda pool, tour, tenant: (tour, None))
    with patch.object(t2, "_prepare", prepare), \
            patch("services.content_generation.t2_facts.with_facts", facts), \
            patch.object(t2, "_rewrite_and_save", AsyncMock(side_effect=_unavailable())):
        with pytest.raises(tenant_budget.TenantBudgetUnavailable) as ei:
            await t2.run(_ctx(Pool(store), "t2_rewrite", payload))
    _retryable(ei.value)
    assert store.domain == "pending"
