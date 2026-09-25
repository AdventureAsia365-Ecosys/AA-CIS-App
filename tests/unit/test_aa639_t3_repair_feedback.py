"""AA-639 — T3 repair rounds tell the writer what failed instead of re-rolling blind."""
from unittest.mock import AsyncMock, patch

import pytest

from services.acp_produce import tenant_pipeline as tp


def test_feedback_lists_novel_numbers_with_their_sentences():
    fb = tp.t3_repair_feedback(
        ["FORBIDDEN_WORD"],
        [{"field": "summary", "sentence": "A  30-minute flight   to Lukla.", "novel_numbers": ["30"]}],
    )
    assert "FACT CHECK FAILED" in fb
    assert '- [summary] numbers 30: "A 30-minute flight to Lukla."' in fb
    assert "STRUCTURE CHECK FAILED — fix these issues: FORBIDDEN_WORD" in fb


def test_feedback_is_empty_when_nothing_failed():
    assert tp.t3_repair_feedback([], []) == ""


def test_feedback_is_capped():
    many = [{"field": "itineraries", "sentence": f"s{i}", "novel_numbers": [str(i)]} for i in range(20)]
    fb = tp.t3_repair_feedback([], many)
    assert fb.count("- [itineraries]") == tp._T3_FEEDBACK_MAX_SENTENCES
    assert "…and 12 more sentence(s)" in fb


@pytest.mark.asyncio
async def test_repair_round_passes_feedback_to_the_writer():
    bad = {"generated": {"summary": "A 30-minute flight."}}
    good = {"generated": {"summary": "A short flight."}}
    rewrite = AsyncMock(return_value=good)
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
         patch.object(tp, "_t3_structural_issues", return_value=[]):
        out = await tp.run_t3_qa_gate({"name": "T"}, ["no numbers here"], bad, {}, tenant_id="t1")

    assert out["passed"] is True and out["attempts"] == 1
    fb = rewrite.await_args.kwargs["feedback"]
    assert "30" in fb and "A 30-minute flight." in fb
    assert rewrite.await_args.kwargs["generate_stage"] == "t2_generate"


@pytest.mark.asyncio
async def test_rewrite_tour_seeds_graph_state_with_feedback():
    from api.routers import v1_pipeline

    seen = {}

    class FakeGraph:
        async def astream(self, state, stream_mode):
            seen.update(state)
            yield {"generate": {**state, "generated": {"name": "x"}}}

    with patch("services.content_generation.graph.build_graph_from_generated", return_value=FakeGraph()), \
         patch.object(v1_pipeline, "build_graph", return_value=FakeGraph()):
        await v1_pipeline._rewrite_tour({"name": "T"}, idx=0, total=1, feedback="FIX THIS")
    assert seen["feedback"] == "FIX THIS"
