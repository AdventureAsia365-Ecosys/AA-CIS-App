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


# ── AA-639 root causes found in CloudWatch (Manaslu, version 24e60fb3) ─────────

from services.acp_shared.grounding import find_novel_numeric_claims  # noqa: E402


@pytest.mark.parametrize("sentence,source", [
    ("Cross Larkya La at 4,460 m before descending.", "Larkya La (5,106m) ... camp at 4,460m"),
    ("Soti Khola sits at 710 metres.", "Drive to Soti Khola (710m)."),
    ("Samagaun lies at 3,520m.", "Samagaun 3520m"),
    ("A 12 km walk.", "walk 12km today"),
])
def test_unit_suffixed_and_comma_numbers_match_the_source(sentence, source):
    # these were all flagged as "novel" live (e.g. 460, 710) purely from formatting
    assert find_novel_numeric_claims(sentence, [source]) == []


def test_genuinely_new_numbers_are_still_flagged():
    assert find_novel_numeric_claims("A 30-minute flight to Lukla (2,804m).", ["Fly to Lukla (2,804m)."]) == ["30"]
    assert find_novel_numeric_claims("Summit at 6,189 m.", ["Summit Island Peak."]) == ["6189"]


def test_t3_structural_only_fails_on_hard_codes():
    with patch("services.content_generation.graph.validate_node",
               return_value={"failure_codes": ["HIGHLIGHTS_TOO_GENERIC", "FORBIDDEN_WORD", "GENERIC_AI_WORDING"]}):
        assert tp._t3_structural_issues({}, {}, {}) == ["FORBIDDEN_WORD"]
    with patch("services.content_generation.graph.validate_node",
               return_value={"failure_codes": ["HIGHLIGHTS_TOO_GENERIC"]}):
        assert tp._t3_structural_issues({}, {}, {}) == []


# ── AA-639: over-long SEO title fixed without a rewrite (live: 1 full rewrite for this alone) ──

@pytest.mark.parametrize("title,expected", [
    ("Manaslu Circuit Trek — 18 Days Around Nepal's Eighth-Highest Peak", "Manaslu Circuit Trek"),
    ("Manaslu Circuit Trek: 18-Day Himalayan Loop | Adventure Asia Private Trip",
     "Manaslu Circuit Trek: 18-Day Himalayan Loop"),
    ("Short title — fine", "Short title — fine"),
])
def test_fit_seo_title_drops_trailing_segments(title, expected):
    assert tp.fit_seo_title(title) == expected


def test_fit_seo_title_word_boundary_and_dangling_words():
    t = "Trekking through the remote valleys of Upper Mustang and the ancient walled city of Lo"
    out = tp.fit_seo_title(t)
    assert len(out) <= 60 and not out.endswith((" of", " the", " and"))
    assert t.startswith(out)


def test_fit_seo_title_always_within_limit():
    for t in ["x" * 90, "A " * 50, "One — Two — Three — Four — Five — Six — Seven — Eight — Nine"]:
        assert 0 < len(tp.fit_seo_title(t)) <= 60


@pytest.mark.asyncio
async def test_t3_fixes_long_seo_title_without_a_rewrite():
    long_title = "Manaslu Circuit Trek — 18 Days Around Nepal's Eighth-Highest Peak"
    result = {"generated": {"seo_title": long_title, "summary": "No numbers."}}
    rewrite = AsyncMock()

    def fake_validate(state):  # only the rule under test: >60-char title is a hard failure
        long_ = len(state["generated"].get("seo_title", "")) > 60
        return {"failure_codes": ["SEO_TITLE_TOO_LONG"] if long_ else []}

    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
         patch("services.content_generation.graph.validate_node", side_effect=fake_validate):
        out = await tp.run_t3_qa_gate({"name": "T"}, ["src"], result, {}, tenant_id="t1")
    rewrite.assert_not_awaited()
    assert out["passed"] is True
    assert out["result"]["generated"]["seo_title"] == "Manaslu Circuit Trek"
