"""AA-206 [AA-193·F1]: S1 two-model generate–judge.

Covers: (1) the strengthened brand differentiation block (itineraries/day-title + negative
contrast present when brand signals exist, "" when absent); (2) judge_node scoring + non-blocking
failure; (3) graph wiring (llm_judge node + validate→llm_judge edge).
"""

import json
from unittest.mock import patch

from services.content_generation.graph import _build_brand_diff_block, build_graph
from services.content_generation.judge_node import judge_node
from shared.llm_client.models import LLMResponse


# ── Step 1: _build_brand_diff_block ──────────────────────────────────────────

def test_diff_block_includes_itineraries_and_negative_contrast():
    """With brand signals present, the block now covers itineraries/day-title + a negative example."""
    state = {
        "brand_core_idea": "Discreet executive adventure",
        "brand_customer_mindset": "Wants privacy and effortless logistics",
        "brand_voice_examples": ["understated", "assured"],
    }
    block = _build_brand_diff_block(state)
    assert "CONTRAST REQUIREMENT" in block
    # widened scope: itineraries incl. each day-title + overall framing
    assert "itineraries" in block.lower()
    assert "day-title" in block.lower()
    assert "framing" in block.lower()
    # negative contrast section with a concrete generic anti-pattern
    assert "GENERIC PHRASING TO AVOID" in block
    assert "synonym-swap" in block.lower()


def test_diff_block_empty_when_no_brand_signals():
    """No differentiation signals → empty block (backward-compatible for legacy/default brands)."""
    state = {
        "brand_system_prompt": "legacy",
        "brand_style_guide": "legacy",
        "brand_forbidden_words": ["cheap"],
    }
    block = _build_brand_diff_block(state)
    assert block == ""
    assert "GENERIC PHRASING TO AVOID" not in block


# ── Step 2: judge_node ───────────────────────────────────────────────────────

def _branded_state(**kw) -> dict:
    base = {
        "brand_core_idea": "Discreet executive adventure",
        "brand_customer_segment": "Senior professionals, $250k+",
        "brand_customer_mindset": "Wants privacy and effortless logistics",
        "brand_voice_examples": ["understated", "assured"],
        "brand_good_examples": "Dawn over Halong, just your crew and the mist.",
        "generated": {
            "name": "Halong Bay Private Cruise",
            "subtitle": "3-night private cruise",
            "summary": "Three nights aboard a private vessel through Halong Bay.",
            "highlights": ["Kayaking Luon Cave", "Sunrise at Bai Tu Long", "Chef seafood dinner"],
            "itineraries": "Day 1 -- Board at Tuan Chau. Day 2 -- Kayaking. Day 3 -- Sunrise return.",
            "seo_title": "Halong Bay Private Cruise",
            "seo_meta": "A private three-night Halong Bay cruise.",
        },
        "quality_score": 9.0,
        "feedback": "",
        "cost_usd": 0.0,
    }
    base.update(kw)
    return base


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, model_used="gpt-4.1", provider="openai",
                       input_tokens=100, output_tokens=20, cost_usd=0.001)


def test_judge_node_sets_quality_and_feedback_on_fail():
    """Valid JUDGE JSON below threshold → quality_score lowered + judge feedback merged in."""
    judge_json = json.dumps({
        "brand_fit_score": 4,
        "cross_brand_distinct": 5,
        "mission_present": False,
        "feedback": "Summary reads generic; lead with the executive-privacy angle.",
    })
    state = _branded_state()
    with patch("services.content_generation.brand_fit.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = _resp(judge_json)
        result = judge_node(state)

    # judge_score = min(4,5)=4, mission absent caps at 6 → 4; min(validate 9, 4) = 4
    assert result["quality_score"] == 4.0
    assert "executive-privacy angle" in result["feedback"]
    assert result["judge_brand_fit"] == 4.0
    assert result["judge_mission_present"] is False


def test_judge_node_pass_keeps_high_score():
    """High judge scores → quality_score = min(validate, judge); no retry feedback merge needed."""
    judge_json = json.dumps({
        "brand_fit_score": 9,
        "cross_brand_distinct": 8,
        "mission_present": True,
        "feedback": "",
    })
    state = _branded_state(quality_score=9.0, feedback="")
    with patch("services.content_generation.brand_fit.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = _resp(judge_json)
        result = judge_node(state)

    assert result["quality_score"] == 8.0  # min(9, min(9,8))
    assert result["feedback"] == ""


def test_judge_node_parse_failure_is_non_blocking():
    """Malformed judge output → no raise, validate's quality_score preserved unchanged."""
    state = _branded_state(quality_score=8.5)
    with patch("services.content_generation.brand_fit.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = _resp("not json at all {{{")
        result = judge_node(state)

    assert result["quality_score"] == 8.5
    assert "judge_brand_fit" not in result


def test_judge_node_skips_when_no_brand_profile():
    """Legacy/no-profile brand → judge skipped, LLMClient never constructed, score untouched."""
    state = {
        "generated": {"name": "X", "summary": "y"},
        "quality_score": 7.5,
        "feedback": "",
    }
    with patch("services.content_generation.brand_fit.LLMClient") as MockClient:
        result = judge_node(state)
    MockClient.assert_not_called()
    assert result["quality_score"] == 7.5


# ── Step 3: graph wiring ─────────────────────────────────────────────────────

def test_graph_has_llm_judge_node_and_edge():
    """build_graph wires llm_judge between validate and the should_retry decision."""
    g = build_graph()
    graph = g.get_graph()
    nodes = set(graph.nodes.keys())
    edges = {(e.source, e.target) for e in graph.edges}
    assert "llm_judge" in nodes
    assert ("validate", "llm_judge") in edges
    # judge routes onward to brand_audit (done) and increment_retry (retry)
    assert ("llm_judge", "brand_audit") in edges
    assert ("llm_judge", "increment_retry") in edges
