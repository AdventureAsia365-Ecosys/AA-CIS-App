"""AA-747 item 2 — itinerary nudge cap (MAX_NUDGES_PER_TOUR=3 across both call sites, worst days
first, skipped days recorded nudged=false/skipped_reason="nudge_cap") + the per-day-targets prompt
flag (S1_PER_DAY_TARGETS, default OFF). No live Bedrock — LLMClient is mocked.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from services.content_generation.flag_fix_node import _repair_still_compressed_days
from services.content_generation.graph import generate_node
from services.content_generation.itinerary_utils import (
    ITINERARY_CLAMP_MAX, ITINERARY_CLAMP_MIN, MAX_NUDGES_PER_TOUR, NUDGE_SKIP_REASON,
    clamp_distance, serialize_itinerary_days,
)
from services.content_generation.prompts import (
    build_rewrite_prompt, parse_source_day_word_counts, s1_per_day_targets_enabled,
)
from shared.llm_client.models import LLMResponse


def _resp(content: str) -> LLMResponse:
    return LLMResponse(
        content=content, model_used="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        provider="bedrock", input_tokens=100, output_tokens=50, cost_usd=0.0002,
        cache_read_tokens=0, cache_write_tokens=0,
    )


# A 6-day source where every day has a clear, DIFFERENT word count so clamp distances differ.
_SRC = "\n\n".join(
    f"Day {d}: Place {d}\n" + " ".join(["word"] * w)
    for d, w in {1: 100, 2: 100, 3: 100, 4: 100, 5: 100, 6: 100}.items()
)


def _make_state(**kwargs):
    base = {
        "tour": {"name": "Multi Day", "country": "Nepal", "duration": "6 Days", "itineraries": _SRC},
        "seo": {}, "few_shots": [], "generated": {}, "quality_score": 0.0, "retry_count": 0,
        "feedback": "", "error": "", "cost_usd": 0.0, "model_used": "",
        "brand_system_prompt": "", "brand_style_guide": "", "brand_forbidden_words": [],
        "rewrite_language": "en-US", "model_tier": "haiku", "subtitle_focus": "standard",
        "is_tenant_rewrite": False, "is_branded": True, "failure_codes": [], "sub_scores": {},
        "passed_count": 0, "failed_count": 0,
    }
    base.update(kwargs)
    return base


def _array_output(day_bodies: dict) -> str:
    return json.dumps({
        "name": "Multi Day", "subtitle": "6 days", "summary": "A six day trip.",
        "highlights": ["a", "b", "c"],
        "itineraries": [{"day": d, "title": f"Day {d}", "body": body}
                        for d, body in sorted(day_bodies.items())],
        "seo_title": "Multi Day", "seo_meta": "x" * 145, "trip_type": "cultural",
    })


# ── clamp_distance ───────────────────────────────────────────────────────────

def test_clamp_distance():
    assert clamp_distance(1.0) == 0.0                 # in band
    assert clamp_distance(ITINERARY_CLAMP_MIN) == 0.0
    assert round(clamp_distance(0.1), 3) == round(ITINERARY_CLAMP_MIN - 0.1, 3)
    assert round(clamp_distance(3.0), 3) == round(3.0 - ITINERARY_CLAMP_MAX, 3)
    # a day far below the band is "worse" than one just over it
    assert clamp_distance(0.05) > clamp_distance(1.6)


# ── generate_node: cap at MAX_NUDGES_PER_TOUR, worst days first ──────────────

def test_generate_node_caps_nudges_at_three_worst_first():
    """Five of six days are out of clamp with different severities. Only the 3 WORST (by distance
    outside the band) are nudged; the other 2 are recorded nudged=false, skipped_reason='nudge_cap'.
    The one in-band day is untouched. day 6 is 3x over (distance ~1.44) — the WORST — so it is
    nudged ahead of the very-short days 1/2 (distance ~0.58)."""
    # source ~102 words/day. ratios: d1≈0.02, d2≈0.05, d3≈0.10, d4≈0.20, d5≈1.0 (in band), d6≈2.94.
    # distances outside band: d6 ~1.44 (worst), d1 ~0.58, d2 ~0.55, d3 ~0.50, d4 ~0.40.
    bodies = {1: 2, 2: 5, 3: 10, 4: 20, 5: 100, 6: 300}
    bodies = {d: " ".join(["w"] * n) for d, n in bodies.items()}
    state = _make_state()

    # the first call is the main generate; each subsequent is a nudge (we expect exactly 3)
    nudge = _resp(json.dumps({"title": "Fixed", "body": " ".join(["x"] * 100)}))
    with patch("services.content_generation.graph.LLMClient") as MockClient:
        inst = MockClient.return_value
        inst.generate.side_effect = [_resp(_array_output(bodies))] + [nudge] * 10
        result = generate_node(state)

    # 1 main + exactly MAX_NUDGES_PER_TOUR nudge calls
    assert inst.generate.call_count == 1 + MAX_NUDGES_PER_TOUR
    assert result["itinerary_nudges_used"] == MAX_NUDGES_PER_TOUR

    by_day = {r["day"]: r for r in result["itinerary_day_ratios"]}
    # worst three (d6 over 3x, then the two shortest d1,d2) nudged
    assert [by_day[d]["nudged"] for d in (6, 1, 2)] == [True, True, True]
    # the two out-of-clamp days that lost the budget are recorded as skipped
    for d in (3, 4):
        assert by_day[d]["nudged"] is False
        assert by_day[d]["skipped_reason"] == NUDGE_SKIP_REASON
    # the single in-band day is neither nudged nor skipped
    assert by_day[5]["nudged"] is False
    assert "skipped_reason" not in by_day[5]


# ── flag_fix repair respects the REMAINING budget across both sites ──────────

def test_repair_still_compressed_respects_remaining_budget():
    """generate_node already used 2 nudges → flag_fix's repair may nudge at most 1 more; the rest
    of the still-violating days are recorded skipped on the per-day ratio record."""
    # three still-violating days in the serialized itinerary
    days = {1: {"title": "D1", "body": "w"}, 2: {"title": "D2", "body": "w"},
            3: {"title": "D3", "body": "w"}}
    itinerary_text = serialize_itinerary_days(days)
    src = "\n\n".join(f"Day {d}: Place {d}\n" + " ".join(["word"] * 100) for d in (1, 2, 3))
    state = {
        "tour": {"itineraries": src, "duration": "3 Days"},
        "itinerary_day_ratios": [{"day": d, "nudged": False, "ratio": 0.01} for d in (1, 2, 3)],
    }
    remaining = MAX_NUDGES_PER_TOUR - 2  # == 1

    nudge = _resp(json.dumps({"title": "Fixed", "body": " ".join(["x"] * 100)}))
    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = nudge
        new_text, cost, applied, nudges = _repair_still_compressed_days(state, itinerary_text, remaining)

    assert nudges == remaining == 1
    assert applied is True
    # the two days that didn't get the budget are marked skipped on the shared ratio record
    skipped = [r for r in state["itinerary_day_ratios"] if r.get("skipped_reason") == NUDGE_SKIP_REASON]
    assert len(skipped) == 2


def test_repair_zero_budget_nudges_nothing():
    days = {1: {"title": "D1", "body": "w"}, 2: {"title": "D2", "body": "w"}}
    itinerary_text = serialize_itinerary_days(days)
    src = "\n\n".join(f"Day {d}: Place {d}\n" + " ".join(["word"] * 100) for d in (1, 2))
    state = {"tour": {"itineraries": src, "duration": "2 Days"},
             "itinerary_day_ratios": [{"day": d, "nudged": False} for d in (1, 2)]}

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        new_text, cost, applied, nudges = _repair_still_compressed_days(state, itinerary_text, 0)
        MockClient.return_value.generate.assert_not_called()

    assert nudges == 0
    assert applied is False
    assert new_text == itinerary_text
    assert all(r["skipped_reason"] == NUDGE_SKIP_REASON for r in state["itinerary_day_ratios"])


# ── S1_PER_DAY_TARGETS flag: default OFF, prompt unchanged ───────────────────

def test_per_day_targets_flag_default_off(monkeypatch):
    monkeypatch.delenv("S1_PER_DAY_TARGETS", raising=False)
    assert s1_per_day_targets_enabled() is False
    assert s1_per_day_targets_enabled({}) is False


def test_per_day_targets_flag_env_and_tenant(monkeypatch):
    monkeypatch.setenv("S1_PER_DAY_TARGETS", "true")
    assert s1_per_day_targets_enabled() is True
    # tenant flag wins over env
    assert s1_per_day_targets_enabled({"S1_PER_DAY_TARGETS": "false"}) is False
    monkeypatch.setenv("S1_PER_DAY_TARGETS", "false")
    assert s1_per_day_targets_enabled({"S1_PER_DAY_TARGETS": "on"}) is True


def test_prompt_unchanged_when_flag_off():
    tour = {"name": "N", "country": "Nepal", "duration": "6 Days", "itineraries": _SRC}
    off = build_rewrite_prompt(tour, {}, per_day_targets=False)
    default = build_rewrite_prompt(tour, {})  # default is OFF
    assert off == default
    assert "target ~" not in off
    assert "PER-DAY SOURCE LENGTH" in off  # the existing block is still there


def test_prompt_adds_targets_when_flag_on():
    tour = {"name": "N", "country": "Nepal", "duration": "6 Days", "itineraries": _SRC}
    on = build_rewrite_prompt(tour, {}, per_day_targets=True)
    assert "target ~" in on
    # target = source day words × clamp midpoint (1.05 for [0.6, 1.5])
    src = parse_source_day_word_counts(_SRC, "6 Days")["day_word_counts"]
    mid = (ITINERARY_CLAMP_MIN + ITINERARY_CLAMP_MAX) / 2.0
    expected = max(1, round(next(iter(src.values())) * mid))
    assert f"target ~{expected} words" in on


def test_cap_skip_never_erases_a_real_generate_nudge():
    from services.content_generation.flag_fix_node import _mark_day_ratio_skipped
    state = {"itinerary_day_ratios": [{"day": 1, "ratio": 0.3, "nudged": True},
                                      {"day": 2, "ratio": 0.3, "nudged": False}]}
    _mark_day_ratio_skipped(state, 1)
    _mark_day_ratio_skipped(state, 2)
    day1, day2 = state["itinerary_day_ratios"]
    assert day1["nudged"] is True and "skipped_reason" not in day1
    assert day2["nudged"] is False and day2["skipped_reason"] == "nudge_cap"
