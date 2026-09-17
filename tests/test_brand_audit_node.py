"""Tests for brand_audit_node — AA-133."""

import json
import pytest
from unittest.mock import MagicMock, patch

from services.content_generation.brand_audit_node import (
    pre_audit_checks,
    BRAND_AUDIT_SCHEMA,
    brand_audit_node,
)


# ── pre_audit_checks ──────────────────────────────────────────────────────────

def test_pre_audit_catches_city_list_subtitle():
    generated = {"subtitle": "Bangkok, Chiang Mai, Krabi", "name": "Thailand Tour",
                 "seo_meta": "Great tour.", "highlights": []}
    codes = pre_audit_checks(generated)
    assert "SUBTITLE_CITY_LIST" in codes


def test_pre_audit_catches_waypoint_subtitle():
    generated = {"subtitle": "Paro → Thimphu → Punakha", "name": "Bhutan Tour",
                 "seo_meta": "Great tour.", "highlights": []}
    codes = pre_audit_checks(generated)
    assert "SUBTITLE_WAYPOINT_FORMAT" in codes


def test_pre_audit_catches_name_all_caps():
    generated = {"name": "BHUTAN LUXURY TOUR", "subtitle": "A refined journey",
                 "seo_meta": "Great tour.", "highlights": []}
    codes = pre_audit_checks(generated)
    assert "NAME_ALL_CAPS" in codes


def test_pre_audit_catches_name_superlative():
    generated = {"name": "The Ultimate Bhutan Experience", "subtitle": "A refined journey",
                 "seo_meta": "Great tour.", "highlights": []}
    codes = pre_audit_checks(generated)
    assert "NAME_SUPERLATIVE" in codes


def test_pre_audit_catches_meta_robotic_opener():
    generated = {"name": "Bhutan Expedition", "subtitle": "A refined journey",
                 "seo_meta": "Discover the wonders of Bhutan.", "highlights": []}
    codes = pre_audit_checks(generated)
    assert "META_OPENER_ROBOTIC" in codes


def test_pre_audit_catches_meta_package_word():
    generated = {"name": "Bhutan Expedition", "subtitle": "A refined journey",
                 "seo_meta": "Explore our Bhutan package today.", "highlights": []}
    codes = pre_audit_checks(generated)
    assert "META_PACKAGE_WORD" in codes


def test_pre_audit_catches_optional_highlight():
    generated = {"name": "Bhutan Expedition", "subtitle": "A refined journey",
                 "seo_meta": "Great tour.", "highlights": ["Optional elephant ride", "Trek to monastery"]}
    codes = pre_audit_checks(generated)
    assert "HIGHLIGHTS_OPTIONAL_LANGUAGE" in codes


def test_pre_audit_catches_elephant_riding():
    generated = {"name": "Thailand Adventure", "subtitle": "A jungle journey",
                 "seo_meta": "Great tour.", "highlights": ["Elephant back ride through the jungle"]}
    codes = pre_audit_checks(generated)
    assert "FACT_CHECK_MANUAL_CHECK" in codes


def test_pre_audit_clean_content_returns_no_codes():
    generated = {
        "name": "Bhutan Highland Expedition",
        "subtitle": "A ten-day private traverse of Paro and Bumthang valleys",
        "seo_meta": "A private Bhutan expedition through highland valleys for discerning travelers.",
        "highlights": ["Traverse the Druk Path Trek at altitude", "Private audience at Taktshang monastery"],
    }
    codes = pre_audit_checks(generated)
    assert codes == []


# ── AA-608: meal-time flag must catch fabricated LOGISTICS, not narrative prose ──

def test_meal_narrative_prose_not_flagged():
    """AA-608: ordinary editorial prose mentioning a meal time of day is NOT a product-truth
    risk and must not fire ITINERARY_MEAL_TIME_INVENTED (the old rule false-positived here)."""
    generated = {
        "name": "Nakasendo Way", "subtitle": "A refined valley traverse",
        "seo_meta": "A private walk along the historic Nakasendo post road through cedar forest.",
        "highlights": ["Walk the preserved Magome-Tsumago trail"],
        "itineraries": ("Day 1 -- Arrive in Magome. After breakfast, set out along the old post "
                        "road; pause for lunch at a mountain teahouse before reaching Tsumago by "
                        "dinner. The evening is free to wander the lantern-lit street."),
    }
    codes = pre_audit_checks(generated)
    assert "ITINERARY_MEAL_TIME_INVENTED" not in codes


def test_meal_logistics_claim_flagged():
    """AA-608: a meal presented as a fabricated logistics claim (code / 'included') still fires."""
    for itin in (
        "Day 1 -- Arrive. Overnight at ryokan. Meals: Breakfast, Dinner",
        "Day 2 -- Cross the pass and descend to the valley. (B, L, D)",
        "Day 3 -- Transfer to the coast; breakfast included at the hotel.",
        "Day 4 -- 7:00 AM departure for the summit trailhead.",
    ):
        generated = {"name": "Test", "subtitle": "A journey", "seo_meta": "A tour.",
                     "highlights": ["x"], "itineraries": itin}
        codes = pre_audit_checks(generated)
        assert "ITINERARY_MEAL_TIME_INVENTED" in codes, f"should flag: {itin!r}"


# ── Schema validation ─────────────────────────────────────────────────────────

def test_brand_audit_schema_validates_correct_json():
    """The BRAND_AUDIT_SCHEMA structure should match a valid audit response."""
    valid_payload = {
        "brand_audit": {
            "status": "pass",
            "publish_ready": True,
            "fields_to_fix": [],
            "failure_codes": [],
            "issues": [],
            "scores": {
                "brand_fit": 1,
                "human_read": 1,
                "seo_fit": 1,
                "trip_type_accuracy": 1,
                "publish_readiness": 1,
            },
            "notes": "Content meets all brand standards.",
            "lessons_extracted": [],
        }
    }
    # Validate required keys exist
    ba = valid_payload["brand_audit"]
    required = BRAND_AUDIT_SCHEMA["properties"]["brand_audit"]["required"]
    for key in required:
        assert key in ba, f"Missing required key: {key}"
    assert ba["status"] in ("pass", "flagged", "manual_check")
    assert isinstance(ba["scores"]["brand_fit"], int)


# ── brand_audit_node graceful fallback ────────────────────────────────────────

def test_brand_audit_node_graceful_fallback_on_openai_error():
    """When OpenAI raises an exception, node should return pass and not re-raise."""
    state = {
        "generated": {
            "name": "Bhutan Tour",
            "subtitle": "A highland journey",
            "summary": "A refined expedition.",
            "highlights": ["Trek to monastery", "Private yak farm visit"],
            "itineraries": "Day 1 — Arrive Paro.\nDay 2 — Trek to Taktshang.",
            "seo_title": "Bhutan Private Trek Tours",
            "seo_meta": "A curated highland trek for discerning travelers.",
        },
        "tour": {"duration": "10 days", "country": "Bhutan"},
        "seo": {"top_keywords": [{"keyword": "bhutan tours"}]},
        "cost_usd": 0.0,
        "seo_mode": "dataforseo",
    }

    with patch(
        "services.content_generation.brand_audit_node.OpenAI",
        side_effect=Exception("OpenAI is down"),
    ):
        result = brand_audit_node(state)

    assert result["brand_audit_status"] == "pass"
    assert result["brand_audit_codes"] == []
    assert result["brand_audit_issues"] == []


def test_brand_audit_node_skips_empty_generated():
    """When generated is empty, node returns pass immediately without calling OpenAI."""
    state = {
        "generated": {},
        "tour": {},
        "seo": {},
        "cost_usd": 0.0,
    }
    result = brand_audit_node(state)
    assert result["brand_audit_status"] == "pass"
