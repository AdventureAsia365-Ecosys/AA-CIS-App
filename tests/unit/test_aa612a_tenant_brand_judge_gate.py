"""AA-612a: tenant T2 rewrite must feed brand-diff fields so the judge brand-fit gate fires.

The bug: v1_tours.py's tenant brand fetch SELECTed only system_prompt/style_guide/forbidden_words,
omitting core_idea/customer_mindset/voice_examples. The shared graph's judge gate
(judge_node.has_brand_signals) keys off exactly those fields, so a tenant WITH a real brand still
hit judge_skipped(no_brand_profile). Admin's 'default' brand row legitimately skips because those
columns are blank in the DB — that data-driven skip must stay intact.

These tests pin the gate contract at the state level (the exact keys _rewrite_tour maps
brand_rules into): a populated tenant brand runs the judge; a blank/default brand skips it.
"""

import pytest
from unittest.mock import patch

from services.content_generation import brand_fit as brand_fit_mod
from services.content_generation import judge_node as judge_mod


def _state(**brand):
    """Minimal judge_node input state with a non-empty `generated` so the only variable under
    test is the brand-signal gate."""
    base = {
        "generated": {"name": "Bhutan Highlands", "summary": "x"},
        "quality_score": 8.0,
        "feedback": "",
        "brand_core_idea": "",
        "brand_customer_segment": "",
        "brand_customer_mindset": "",
        "brand_voice_examples": [],
        "brand_good_examples": "",
    }
    base.update(brand)
    return base


def test_blank_default_brand_skips_judge():
    """Admin/default contract: no brand-diff signals -> judge skipped, no LLM call."""
    with patch.object(brand_fit_mod, "LLMClient") as MockClient:
        out = judge_node_run(_state())
    MockClient.assert_not_called()
    # score untouched (gate returned state unchanged)
    assert out["quality_score"] == 8.0


def test_tenant_core_idea_runs_judge():
    """A tenant brand carrying core_idea must NOT be skipped — the judge must be invoked."""
    _assert_judge_runs(_state(brand_core_idea="Slow, local-first family journeys"))


def test_tenant_customer_mindset_runs_judge():
    _assert_judge_runs(_state(brand_customer_mindset="Wants unhurried, authentic immersion"))


def test_tenant_voice_examples_runs_judge():
    _assert_judge_runs(_state(brand_voice_examples=["warm", "grounded", "unhurried"]))


def test_empty_voice_examples_list_does_not_trip_gate():
    """A list of only falsy entries is still 'no signal' — must skip (matches the gate's own
    [v for v in ... if v] filter)."""
    with patch.object(brand_fit_mod, "LLMClient") as MockClient:
        judge_node_run(_state(brand_voice_examples=["", None]))
    MockClient.assert_not_called()


# ── helpers ──────────────────────────────────────────────────────────────────

def judge_node_run(state):
    return judge_mod.judge_node(state)


def _assert_judge_runs(state):
    """The judge is invoked (LLMClient.generate called) when brand signals are present."""
    fake_resp = type("R", (), {
        "content": '{"brand_fit_score": 8, "cross_brand_distinct": 8, '
                   '"mission_present": true, "feedback": ""}',
        "model_used": "gpt-4.1",
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "stop_reason": "stop",
    })()
    with patch.object(brand_fit_mod, "LLMClient") as MockClient, \
            patch.object(judge_mod, "record_call_sync"):
        MockClient.return_value.generate.return_value = fake_resp
        judge_node_run(state)
    MockClient.return_value.generate.assert_called_once()
