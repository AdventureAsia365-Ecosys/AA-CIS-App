"""AA-329(a)+(c) — validate_node's two new itinerary gates, and flag_fix_node's dedicated
ITINERARY_STILL_COMPRESSED repair.

(a) ITINERARY_DAY_COUNT_MISMATCH: generated itineraries dropped a whole day the source has.
(c) ITINERARY_STILL_COMPRESSED: a day survived AA-353's own one-shot nudge (_process_itineraries,
    called inside generate_node BEFORE validate_node ever runs) still outside
    [ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX] relative to its own source day.

Both checks are computed FRESH from state["tour"]["itineraries"] (source) and
state["generated"]["itineraries"] (already the canonical "Day N — Title\\nBody" string by the
time validate_node runs — see AA-329 implementation notes for why state["itinerary_day_ratios"]
itself is deliberately NOT used for gating).
"""
import json
from unittest.mock import MagicMock, patch

from services.content_generation.graph import validate_node
from services.content_generation.flag_fix_node import (
    flag_fix_node, _repair_still_compressed_days, _should_fix, _build_fix_keys,
    STAGE2_FIX_MAPPING, _DETERMINISTIC_SEO_CODES,
)
from services.content_generation.itinerary_utils import (
    ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX, MAX_NUDGES_PER_TOUR, parse_canonical_itinerary_days,
    serialize_itinerary_days,
)


def _source_days(word_counts: dict) -> str:
    """Line-anchored "Day N: ..." source text — one distinct word per position so word count is
    exactly controllable, matching the convention test_aa353_itinerary_compression.py uses."""
    return "\n\n".join(
        f"Day {d}: Segment title\n" + " ".join(["word"] * w)
        for d, w in sorted(word_counts.items())
    )


def _generated_days(word_counts: dict, titles: dict = None) -> str:
    """Canonical "Day N — Title\\nBody" string — exactly what generate_node's
    _process_itineraries serializes to, and what validate_node expects to read. Each day's body
    is built to an EXACT word count (repeats "word" N times, same convention as _source_days)
    so ratios in the tests below are exact, not approximate."""
    titles = titles or {}
    return "\n\n".join(
        f"Day {d} — {titles.get(d, f'Day {d} title')}\n" + " ".join(["word"] * w)
        for d, w in sorted(word_counts.items())
    )


def _validate_state(tour_itineraries: str, generated_itineraries: str, duration: str = "9 Days") -> dict:
    return {
        "tour": {"name": "Test Tour", "country": "Laos",
                  "itineraries": tour_itineraries, "duration": duration},
        "seo": {},
        "generated": {
            "name": "Test Tour Rewrite",
            "subtitle": "A concrete subtitle with route and duration details",
            "summary": "A factual editorial summary of the trip with real specifics included.",
            "highlights": ["Specific place A", "Specific place B", "Specific place C"],
            "itineraries": generated_itineraries,
            "seo_title": "Test Tour | Adventure Asia",
            "seo_meta": "x" * 145,
        },
        "failure_codes": [],
        "brand_forbidden_words": [],
    }


# ── (a) ITINERARY_DAY_COUNT_MISMATCH ────────────────────────────────────────────

def test_day_count_mismatch_fires_when_output_drops_trailing_days():
    """661aa058-style case (South Korea Cycling, per AA-329 Linear thread): source 9 days,
    output only carries 6 — the 3 dropped days must fire, named explicitly in the feedback."""
    source = _source_days({d: 40 for d in range(1, 10)})       # 9 source days
    generated = _generated_days({d: 40 for d in range(1, 7)})  # only days 1-6 survive

    result = validate_node(_validate_state(source, generated))

    assert "ITINERARY_DAY_COUNT_MISMATCH" in result["failure_codes"]
    assert "day 7/8/9" in result["feedback"]
    assert "9 source days -> 6 output days" in result["feedback"]


def test_day_count_mismatch_does_not_fire_when_all_source_days_present():
    """Same day count on both sides — must NOT fire, even though nothing else about the
    itinerary is being checked here (word density is a separate code, (c) below)."""
    source = _source_days({d: 40 for d in range(1, 10)})
    generated = _generated_days({d: 40 for d in range(1, 10)})

    result = validate_node(_validate_state(source, generated))

    assert "ITINERARY_DAY_COUNT_MISMATCH" not in result["failure_codes"]


def test_day_count_mismatch_skipped_when_source_has_no_reliable_day_markers():
    """No tour itinerary at all (or unstructured prose) -> parse_source_day_word_counts falls
    back to an even split -> nothing trustworthy to compare against -> must not fire."""
    generated = _generated_days({1: 10, 2: 10})  # only 2 days, would look like a huge mismatch
    state = _validate_state(tour_itineraries="", generated_itineraries=generated)

    result = validate_node(state)

    assert "ITINERARY_DAY_COUNT_MISMATCH" not in result["failure_codes"]


# ── (c) ITINERARY_STILL_COMPRESSED ──────────────────────────────────────────────

def test_still_compressed_fires_when_a_day_is_far_below_clamp():
    """Day 1 written at ~10% of its source length (well under ITINERARY_CLAMP_MIN=0.6) — the
    exact shape AA-353's one-shot nudge is supposed to catch but sometimes doesn't."""
    source = _source_days({1: 100, 2: 50})
    generated = _generated_days({1: 10, 2: 50})  # day 1: ratio 0.1; day 2: ratio 1.0 (in clamp)

    result = validate_node(_validate_state(source, generated, duration="2 Days"))

    assert "ITINERARY_STILL_COMPRESSED" in result["failure_codes"]
    assert "day 1:" in result["feedback"]
    assert "day 2:" not in result["feedback"]  # day 2 is in-clamp, must not be listed


def test_still_compressed_does_not_fire_when_every_day_in_clamp():
    source = _source_days({1: 100, 2: 50})
    generated = _generated_days({1: 90, 2: 55})  # ratios 0.9 and 1.1 — both in [0.6, 1.5]

    result = validate_node(_validate_state(source, generated, duration="2 Days"))

    assert "ITINERARY_STILL_COMPRESSED" not in result["failure_codes"]


def test_still_compressed_skipped_when_source_has_no_reliable_day_markers():
    generated = _generated_days({1: 2})  # absurdly short, would fire if compared to anything
    state = _validate_state(tour_itineraries="", generated_itineraries=generated)

    result = validate_node(state)

    assert "ITINERARY_STILL_COMPRESSED" not in result["failure_codes"]


# ── flag_fix_node routing: ITINERARY_STILL_COMPRESSED is deterministic, not brand-audit-gated ──

def test_still_compressed_routes_to_fix_even_when_brand_audit_pass():
    """Mirrors AA-204's own test shape for the other deterministic codes: a validate_node code
    must force a repair pass independently of what the (non-deterministic) brand audit found."""
    assert "ITINERARY_STILL_COMPRESSED" in _DETERMINISTIC_SEO_CODES
    assert STAGE2_FIX_MAPPING.get("ITINERARY_STILL_COMPRESSED") == "itineraries"
    state = {"brand_audit_status": "pass", "brand_audit_codes": [], "brand_audit_fields": [],
              "failure_codes": ["ITINERARY_STILL_COMPRESSED"]}
    assert _should_fix(state) is True
    assert "itineraries" in _build_fix_keys(state)


# ── _repair_still_compressed_days (unit-level, no full flag_fix_node) ───────────

def _resp(content: str, cost=0.001) -> MagicMock:
    r = MagicMock()
    r.content = content
    r.cost_usd = cost
    return r


def test_repair_still_compressed_days_fixes_the_violating_day_only():
    source = _source_days({1: 100, 2: 50})
    itinerary_text = _generated_days({1: 10, 2: 50})  # day 1 violates, day 2 doesn't
    tour = {"itineraries": source, "duration": "2 Days"}
    state = {"tour": tour}

    nudge_reply = _resp(json.dumps({
        "title": "Repaired Day 1", "body": " ".join(["fixed"] * 100),
    }))

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = nudge_reply
        new_text, extra_cost, applied, _nudges = _repair_still_compressed_days(
            state, itinerary_text, MAX_NUDGES_PER_TOUR)

    assert applied is True
    assert extra_cost == 0.001
    days = parse_canonical_itinerary_days(new_text)
    assert days[1]["title"] == "Repaired Day 1"
    assert len(days[1]["body"].split()) == 100
    # day 2 must be byte-for-byte untouched — it never violated the clamp
    assert days[2]["body"] == " ".join(["word"] * 50)


def test_repair_still_compressed_days_keeps_pre_fix_day_when_retry_still_out_of_clamp():
    """Deterministic guard: if the ONE repair attempt is still outside clamp, the pre-fix day
    text is kept — never overwritten with something no better."""
    source = _source_days({1: 100})
    itinerary_text = _generated_days({1: 10})
    tour = {"itineraries": source, "duration": "1 Days"}
    state = {"tour": tour}

    still_bad_reply = _resp(json.dumps({"title": "Still short", "body": "still too short"}))

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = still_bad_reply
        new_text, extra_cost, applied, _nudges = _repair_still_compressed_days(
            state, itinerary_text, MAX_NUDGES_PER_TOUR)

    assert applied is False
    assert new_text == itinerary_text  # unchanged — pre-fix day kept


def test_repair_still_compressed_days_noop_when_nothing_violates():
    source = _source_days({1: 100})
    itinerary_text = _generated_days({1: 95})  # ratio 0.95 — in clamp already
    state = {"tour": {"itineraries": source, "duration": "1 Days"}}

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        new_text, extra_cost, applied, _nudges = _repair_still_compressed_days(
            state, itinerary_text, MAX_NUDGES_PER_TOUR)
        MockClient.assert_not_called()

    assert applied is False
    assert extra_cost == 0.0
    assert new_text == itinerary_text


# ── flag_fix_node end-to-end: ITINERARY_STILL_COMPRESSED bypasses the generic FIX_SYSTEM path ──

def test_flag_fix_node_uses_dedicated_repair_not_generic_prompt_for_still_compressed():
    source = _source_days({1: 100, 2: 50})
    itinerary_text = _generated_days({1: 10, 2: 50})
    state = {
        "brand_audit_status": "pass",
        "brand_audit_codes": [],
        "brand_audit_issues": [],
        "brand_audit_fields": [],
        "failure_codes": ["ITINERARY_STILL_COMPRESSED"],
        "lessons_extracted": [],
        "generated": {
            "name": "Laos Explorer",
            "itineraries": itinerary_text,
            "seo_meta": "x" * 145,
        },
        "tour": {"itineraries": source, "duration": "2 Days", "country": "Laos"},
        "seo": {},
        "cost_usd": 0.0,
        "model_tier": "haiku",
    }
    nudge_reply = _resp(json.dumps({
        "title": "Repaired Day 1", "body": " ".join(["fixed"] * 100),
    }))

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = nudge_reply
        result = flag_fix_node(state)

    # Exactly one LLM call — the dedicated per-day nudge, NOT the generic FIX_SYSTEM prompt
    # (which would additionally ask the model to rewrite the whole itineraries field blind).
    assert MockClient.return_value.generate.call_count == 1
    call_prompt = MockClient.return_value.generate.call_args[0][0].user_prompt
    assert "TARGET LENGTH" in call_prompt  # nudge_itinerary_day's own prompt shape
    assert result["fix_pass_applied"] is True
    assert "itineraries" in result["fix_pass_fields"]
    assert "Repaired Day 1" in result["generated"]["itineraries"]
    assert result["cost_usd"] == 0.001


def test_flag_fix_node_skips_when_dedicated_repair_finds_nothing_to_fix():
    """failure_codes says ITINERARY_STILL_COMPRESSED but the itinerary is actually fine (stale
    code / already fixed upstream) -> _repair_still_compressed_days finds no violation -> no other
    fix_keys -> fix_pass_applied False, no LLM call at all."""
    source = _source_days({1: 100})
    itinerary_text = _generated_days({1: 95})
    state = {
        "brand_audit_status": "pass",
        "brand_audit_codes": [],
        "brand_audit_issues": [],
        "brand_audit_fields": [],
        "failure_codes": ["ITINERARY_STILL_COMPRESSED"],
        "lessons_extracted": [],
        "generated": {"name": "X", "itineraries": itinerary_text, "seo_meta": "x" * 145},
        "tour": {"itineraries": source, "duration": "1 Days"},
        "seo": {},
        "cost_usd": 0.0,
        "model_tier": "haiku",
    }

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        result = flag_fix_node(state)
        MockClient.return_value.generate.assert_not_called()

    assert result["fix_pass_applied"] is False
    assert result["generated"]["itineraries"] == itinerary_text
