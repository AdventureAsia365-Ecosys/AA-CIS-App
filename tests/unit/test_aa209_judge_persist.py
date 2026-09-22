"""AA-209 PART 2: persist judge scores into generated_content.metadata JSONB (no migration).

The persist path builds metadata via api.routers.admin_pipeline._build_generated_metadata and writes
it to the generated_content.metadata column. These tests pin:
  * judge ran        → metadata["judge"] holds brand_fit/distinct/mission_present/feedback/judge_score
  * base keys        → never clobbered by the judge merge
  * judge did NOT run → metadata["judge"] absent (not null), no crash
  * Decimal-safe     → json.dumps(..., default=str) serializes the metadata without error
  * judge_node now returns judge_score alongside the existing judge_* fields
"""

import json
from decimal import Decimal

from api.routers.admin_pipeline import _build_generated_metadata
from services.content_generation.judge_node import judge_node
from shared.llm_client.models import LLMResponse
from unittest.mock import patch


_BASE_KW = dict(
    brand_rule_id="rule-1",
    brand_name="Discreet Executive",
    seo_mode="dataforseo",
    model_used="us.anthropic.claude-haiku-4-5",
    llm_cost_usd=0.0021,
    dataforseo_used=True,
)


def _judge_result(**over):
    base = {
        "judge_brand_fit": 8.0,
        "judge_cross_brand_distinct": 7.0,
        "judge_mission_present": True,
        "judge_feedback": "Lead with the executive-privacy angle.",
        "judge_score": 7.0,
    }
    base.update(over)
    return base


def test_metadata_judge_populated_when_judge_ran():
    meta = _build_generated_metadata(_judge_result(), **_BASE_KW)
    judge = meta["judge"]
    assert judge["brand_fit"] == 8.0
    assert judge["distinct"] == 7.0
    assert judge["mission_present"] is True
    assert judge["feedback"] == "Lead with the executive-privacy angle."
    assert judge["judge_score"] == 7.0


def test_base_metadata_keys_preserved():
    """The judge merge must not clobber any of the base metadata keys."""
    meta = _build_generated_metadata(_judge_result(), **_BASE_KW)
    assert meta["brand_rule_id"] == "rule-1"
    assert meta["brand_name"] == "Discreet Executive"
    assert meta["seo_mode"] == "dataforseo"
    assert meta["model_used"] == "us.anthropic.claude-haiku-4-5"
    assert meta["llm_cost_usd"] == 0.0021
    assert meta["dataforseo_used"] is True
    assert meta["pipeline_version"] == "v2"
    assert "generated_at" in meta
    # judge added alongside, base intact
    assert "judge" in meta


def test_metadata_judge_absent_when_judge_did_not_run():
    """Legacy/no-profile brand: judge_node returns no judge_* keys → metadata.judge omitted, no error."""
    meta = _build_generated_metadata({"status": "success"}, **_BASE_KW)
    assert "judge" not in meta
    # base metadata still fully built
    assert meta["brand_name"] == "Discreet Executive"
    assert meta["pipeline_version"] == "v2"


def test_metadata_judge_present_with_falsey_scores():
    """brand_fit/judge_score of 0.0 and mission_present False are valid — guard is `is not None`."""
    meta = _build_generated_metadata(
        _judge_result(judge_brand_fit=0.0, judge_score=0.0, judge_mission_present=False),
        **_BASE_KW,
    )
    assert "judge" in meta
    assert meta["judge"]["brand_fit"] == 0.0
    assert meta["judge"]["judge_score"] == 0.0
    assert meta["judge"]["mission_present"] is False


def test_metadata_is_decimal_safe_json():
    """json.dumps(default=str) must serialize Decimal-valued judge scores without raising."""
    meta = _build_generated_metadata(
        _judge_result(judge_brand_fit=Decimal("8.5"), judge_score=Decimal("7.0")),
        **_BASE_KW,
    )
    dumped = json.dumps(meta, default=str)
    round_tripped = json.loads(dumped)
    assert round_tripped["judge"]["brand_fit"] == "8.5"
    assert round_tripped["judge"]["judge_score"] == "7.0"


def test_judge_node_returns_judge_score():
    """judge_node must now surface judge_score so the persist path can record it."""
    judge_json = json.dumps({
        "brand_fit_score": 8, "cross_brand_distinct": 7,
        "mission_present": True, "feedback": "",
    })
    state = {
        "brand_core_idea": "Discreet executive adventure",
        "brand_customer_mindset": "Wants privacy and effortless logistics",
        "brand_voice_examples": ["understated", "assured"],
        "generated": {"name": "X", "subtitle": "y", "summary": "z", "highlights": ["a"],
                      "itineraries": "Day 1 -- board.", "seo_title": "t", "seo_meta": "m"},
        "quality_score": 9.0,
        "feedback": "",
    }
    with patch("services.content_generation.brand_fit.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = LLMResponse(
            content=judge_json, model_used="gpt-4.1", provider="openai",
            input_tokens=10, output_tokens=5, cost_usd=0.001,
        )
        result = judge_node(state)

    # judge_score = min(brand_fit 8, distinct 7) = 7; persisted score_overall = min(validate 9, 7)
    assert result["judge_score"] == 7.0
    assert result["quality_score"] == 7.0
