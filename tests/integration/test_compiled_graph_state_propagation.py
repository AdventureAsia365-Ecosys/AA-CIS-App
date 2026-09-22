"""
AA-281: integration tests that drive the FULL compiled LangGraph StateGraph through
_rewrite_tour() — the real production entrypoint (api/routers/v1_pipeline.py), which builds
the graph via build_graph() and drives it with graph.astream(..., stream_mode="updates").

Every other file in tests/integration/ exercises pipeline stages by inserting directly into
Postgres or calling node functions in isolation — none of them route through the compiled
graph. That gap matters because of the AA-209 bug class: LangGraph silently strips any state
key that isn't declared in the ContentState TypedDict (services/content_generation/graph.py)
when state crosses a node boundary via astream()/invoke(). A field can be set correctly by a
node function called directly, or inserted correctly via raw SQL, and still be dropped once it
actually flows through the compiled graph — that failure mode is invisible to every existing
integration test. These tests close that gap.
"""
import os
import sys
import json
import pytest
from unittest.mock import MagicMock

# tests/integration/ has no __init__.py (unlike tests/unit/, which inherits repo-root
# importability via tests/__init__.py), so the repo root is never added to sys.path here —
# api.*/services.*/shared.* are not importable without this. conftest.py already does the same
# sys.path.insert(0, dirname(__file__)) trick for its own directory (to import _constants);
# this is the same fix one level up, scoped to this file only.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import SAMPLE_TOUR
from api.routers.v1_pipeline import _rewrite_tour
from shared.llm_client.models import LLMResponse

# A brand profile WITH core_idea, so judge_node's has_brand_signals guard actually lets the
# judge run (judge_node.py: has_brand_signals = core_idea or customer_mindset or voice_examples).
# Without this, judge_node short-circuits with judge_skipped/reason=no_brand_profile and
# judge_score stays None for reasons that have nothing to do with the AA-209 strip bug — that
# would make a "judge_score is not None" assertion meaningless.
BRAND_RULES = {
    "system_prompt": "Write professional tour content for WorldLux travel brand.",
    "style_guide": "Use formal tone. Highlight private-access moments.",
    "forbidden_words": [],
    "rewrite_language": "en-US",
    "core_idea": "Discreet, private-charter access to hidden corners of Vietnam.",
    "customer_segment": "Senior executives seeking privacy over crowds.",
    "customer_mindset": "Wants exclusivity without ostentation.",
    "voice_examples": ["quiet", "precise", "unhurried"],
    "good_examples": "A private cove, reached by no road map.",
}

# Passes validate_node (graph.py) cleanly: no MISSING_FIELD, no forbidden words, subtitle/
# highlights not generic, seo_meta in the 140-155 char band as a complete sentence, itinerary
# has a "Day N" marker and is long enough. Verified against the real validate_node checks
# before being hard-coded here (SEO_META_MIN/MAX from seo_meta_utils.py) — not guessed.
GOOD_GENERATED = {
    "name": "Ha Long Bay Private Cruise Escape",
    "subtitle": "A quiet three-day passage through Vietnam's limestone bay",
    "summary": (
        "Set sail across mirror-calm waters as ancient limestone towers rise from the sea on "
        "all sides. Your private cabin cruise threads through hidden lagoons before anchoring "
        "in a quiet cove for the night, far from the day-trip crowds that fill the main channel."
    ),
    "highlights": [
        "Private sunrise kayak through a hidden lagoon cave",
        "Onboard chef-prepared seafood dinner beneath open sky",
        "A quiet cove swim stop away from the main tourist channel",
    ],
    "itineraries": (
        "Day 1 — Board your private cabin cruise in Halong City and settle into your cabin "
        "before the open deck.\n\n"
        "Day 2 — Kayak at dawn through a hidden lagoon and swim in a quiet cove.\n\n"
        "Day 3 — Rise early for tai chi on deck before returning to shore by midday."
    ),
    "seo_title": "Ha Long Bay Private 3-Day Cruise",
    "seo_meta": (
        "Sail through Vietnam's Ha Long Bay on a private three-day cruise, kayaking hidden "
        "lagoons and dining on fresh seafood beneath open sky each quiet evening."
    ),
}

# Valid JSON but missing every field validate_node requires — triggers MISSING_FIELD, which
# caps quality_score at graph.MISSING_FIELD_CAP (4.0), forcing should_retry -> "retry" without
# depending on json_repair's behavior on unparseable text (which would make the retry trigger
# non-deterministic and unrelated to what this test is checking).
BAD_GENERATED = {"name": "Incomplete Attempt"}


def _judge_response(brand_fit=9, cross_brand_distinct=8, mission_present=True,
                     feedback="", cost_usd=0.01):
    return LLMResponse(
        content=json.dumps({
            "brand_fit_score": brand_fit,
            "cross_brand_distinct": cross_brand_distinct,
            "mission_present": mission_present,
            "feedback": feedback,
        }),
        model_used="gpt-4.1",
        provider="openai",
        cost_usd=cost_usd,
    )


def _generate_response(generated: dict, cost_usd=0.002):
    return LLMResponse(
        content=json.dumps(generated),
        model_used="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        provider="bedrock",
        cost_usd=cost_usd,
    )


@pytest.fixture
def patch_llm_client(monkeypatch):
    """AA-281: generate_node (graph.py) and the brand-fit judge call (AA-631: extracted into
    brand_fit.py, called by judge_node.py) each do their own
    `from shared.llm_client.client import LLMClient` at module-import time, then instantiate
    LLMClient() locally inside that module's namespace. Patching shared.llm_client.client.LLMClient
    has NO effect on either — verified experimentally (pre-AA-631, same finding applied to
    judge_node.py directly): with only that patch applied, LLMClient() still constructed the
    REAL client and the graph run failed on missing AWS/OpenAI credentials instead of using the
    mock, which would have been a false-pass hiding the very strip bug this test file exists to
    catch. Each consuming module bound its own local name at import time, so both must be
    patched separately, by their consuming-module path: services.content_generation.graph.
    LLMClient AND services.content_generation.brand_fit.LLMClient (AA-631 moved the judge's own
    LLMClient usage out of judge_node.py into brand_fit.py — judge_node.py no longer imports it
    at all, so patching judge_node.LLMClient would silently do nothing post-AA-631).

    request.model_tier distinguishes a generate_node call ("haiku"/"sonnet", whatever the test
    passes to _rewrite_tour) from the brand-fit judge call (always "gpt-4.1" per brand_fit.py),
    so one mock serves both call sites without either module knowing about the other.
    """
    def _install(haiku_responses, judge_response=None):
        queue = list(haiku_responses)
        judge_resp = judge_response or _judge_response()

        def _generate(request):
            if request.model_tier == "gpt-4.1":
                return judge_resp
            # Pop each queued response once (retry test), but keep returning the last one if
            # called more times than expected rather than raising IndexError mid-graph-run.
            return queue.pop(0) if len(queue) > 1 else queue[0]

        mock_client = MagicMock()
        mock_client.generate.side_effect = _generate
        mock_client_cls = MagicMock(return_value=mock_client)
        monkeypatch.setattr("services.content_generation.graph.LLMClient", mock_client_cls)
        monkeypatch.setattr("services.content_generation.brand_fit.LLMClient", mock_client_cls)
        return mock_client

    return _install


async def test_full_graph_run_returns_judge_fields_undropped(patch_llm_client):
    """AA-209 regression: judge_score (and the other judge_* fields) must survive the
    generate -> validate -> llm_judge -> brand_audit -> flag_fix -> revalidate -> END path
    driven via astream(), not just a direct judge_node() call."""
    patch_llm_client([_generate_response(GOOD_GENERATED)])

    result = await _rewrite_tour(
        tour=SAMPLE_TOUR, idx=1, total=1,
        brand_rules=BRAND_RULES, seo={}, model_tier="haiku",
    )

    assert result["status"] == "success", result.get("error")
    # judge_score = min(brand_fit_score=9, cross_brand_distinct=8) from the mocked judge response.
    assert result["judge_score"] is not None
    assert result["judge_score"] == pytest.approx(8.0)
    assert result["judge_brand_fit"] == pytest.approx(9.0)
    assert result["judge_cross_brand_distinct"] == pytest.approx(8.0)
    assert result["judge_mission_present"] is True
    # quality_score = min(validate's clean 10.0, judge_score 8.0) — graph.judge_node's stacked gate.
    assert result["quality_score"] == pytest.approx(8.0)


async def test_full_graph_run_propagates_model_tier_metadata(patch_llm_client):
    """model_used/cost_usd/fallback_used/satellite_account are declared in ContentState and must
    reach _rewrite_tour's result dict after a full graph run, not just after a direct node call."""
    patch_llm_client(
        [_generate_response(GOOD_GENERATED, cost_usd=0.002)],
        judge_response=_judge_response(cost_usd=0.01),
    )

    result = await _rewrite_tour(
        tour=SAMPLE_TOUR, idx=1, total=1,
        brand_rules=BRAND_RULES, seo={}, model_tier="haiku",
    )

    assert result["status"] == "success", result.get("error")
    # model_used is set by generate_node's response and never overwritten by judge_node.
    assert result["model_used"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    # cost_usd accumulates across both the generate call and the judge call.
    assert result["cost_usd"] == pytest.approx(0.002 + 0.01)
    assert result["fallback_used"] is False
    assert result["satellite_account"] is None


async def test_full_graph_run_retry_path_preserves_state(patch_llm_client):
    """First generate attempt is structurally incomplete (MISSING_FIELD caps quality_score
    below MIN_QUALITY) -> should_retry routes retry -> increment_retry -> generate again.
    The second attempt succeeds; retry_count and the final generated content must reflect
    that second, successful pass — not get lost/overwritten across the retry edge."""
    patch_llm_client([
        _generate_response(BAD_GENERATED, cost_usd=0.001),
        _generate_response(GOOD_GENERATED, cost_usd=0.002),
    ])

    result = await _rewrite_tour(
        tour=SAMPLE_TOUR, idx=1, total=1,
        brand_rules=BRAND_RULES, seo={}, model_tier="haiku",
    )

    assert result["status"] == "success", result.get("error")
    assert result["retry_count"] >= 1
    assert result["generated"]["name"] == GOOD_GENERATED["name"]
    assert result["generated"]["seo_meta"] == GOOD_GENERATED["seo_meta"]
