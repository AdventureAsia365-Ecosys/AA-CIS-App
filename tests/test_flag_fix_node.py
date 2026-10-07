"""Tests for flag_fix_node — AA-134, and write_lessons_log — AA-132."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.content_generation.flag_fix_node import (
    flag_fix_node,
    STAGE2_FIX_MAPPING,
    write_lessons_log,
)


# ── flag_fix_node pass-through cases ─────────────────────────────────────────

def _make_state(**overrides) -> dict:
    base = {
        "brand_audit_status": "flagged",
        "brand_audit_codes":  ["META_OPENER_ROBOTIC"],
        "brand_audit_issues": ["seo_meta opens with 'Discover'"],
        "brand_audit_fields": ["seo_meta"],
        "lessons_extracted":  [],
        "generated": {
            "name": "Bhutan Expedition",
            "subtitle": "A highland journey",
            "summary": "A refined expedition.",
            "highlights": ["Monastery visit", "Yak farm"],
            "itineraries": "Day 1 — Arrive.\nDay 2 — Trek.",
            "seo_title": "Bhutan Private Tours",
            "seo_meta": "Discover Bhutan with us.",
        },
        "tour": {"duration": "10 days", "country": "Bhutan"},
        "seo": {},
        "cost_usd": 0.0,
        "model_tier": "haiku",
    }
    base.update(overrides)
    return base


def test_flag_fix_skips_when_pass_status():
    state = _make_state(brand_audit_status="pass")
    result = flag_fix_node(state)
    assert result["fix_pass_applied"] is False
    assert result["generated"] == state["generated"]


def test_flag_fix_skips_when_manual_check_status():
    state = _make_state(brand_audit_status="manual_check")
    result = flag_fix_node(state)
    assert result["fix_pass_applied"] is False
    assert result["generated"] == state["generated"]


def test_stage2_fix_mapping_covers_all_20_codes():
    """All 20 canonical failure codes must be in STAGE2_FIX_MAPPING."""
    expected_codes = {
        "SUBTITLE_TRIP_TYPE_MISMATCH", "SUBTITLE_CITY_LIST", "SUBTITLE_WAYPOINT_FORMAT",
        "SUMMARY_OFF_BRAND", "SUMMARY_HONEYMOON_LANGUAGE", "SUMMARY_SELF_REFERENTIAL",
        "GENERIC_AI_WORDING", "HIGHLIGHTS_TOO_GENERIC", "HIGHLIGHTS_ORDERING_WRONG",
        "HIGHLIGHTS_OPTIONAL_LANGUAGE", "ITINERARY_STRUCTURE_WEAK",
        "SEO_TITLE_WEAK", "SEO_TITLE_WRONG_ACTIVITY",
        "META_INCOMPLETE_SENTENCE", "META_OPENER_ROBOTIC", "META_PACKAGE_WORD",
        "META_DFS_VERBATIM", "DFS_INTENT_UNDERUSED",
        "NAME_ALL_CAPS", "NAME_SUPERLATIVE",
    }
    assert expected_codes.issubset(set(STAGE2_FIX_MAPPING.keys()))


def test_flag_fix_only_touches_flagged_fields():
    """Fields not in fix_keys must remain identical after fix."""
    state = _make_state(
        brand_audit_codes=["META_OPENER_ROBOTIC"],
        brand_audit_fields=["seo_meta"],
    )
    original_name    = state["generated"]["name"]
    original_subtitle = state["generated"]["subtitle"]
    # AA-641/AA-724: seo_meta must be forbidden-free, else the per-field forbidden-revert
    # guard restores the pre-repair value and seo_meta never lands in fix_pass_fields.
    fixed_meta = "A measured Bhutan journey through Thimphu and Paro for independent travelers."

    mock_resp = MagicMock()
    mock_resp.content = json.dumps({"seo_meta": fixed_meta})
    mock_resp.cost_usd = 0.001

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = mock_resp
        result = flag_fix_node(state)

    assert result["generated"]["name"] == original_name
    assert result["generated"]["subtitle"] == original_subtitle
    assert result["generated"]["seo_meta"] == fixed_meta
    assert result["fix_pass_applied"] is True
    assert "seo_meta" in result["fix_pass_fields"]


def test_flag_fix_graceful_fallback_on_llm_error():
    """When LLMClient raises, node returns state unchanged with fix_pass_applied=False."""
    state = _make_state()

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        MockClient.return_value.generate.side_effect = Exception("Bedrock down")
        result = flag_fix_node(state)

    assert result["fix_pass_applied"] is False
    assert result["generated"] == state["generated"]


# ── write_lessons_log ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_lessons_log_deduplicates_by_stage_field_pattern():
    """Lessons with the same (stage, field, pattern) should only be inserted once."""
    lessons = [
        {"failure_code": "META_OPENER_ROBOTIC", "field": "seo_meta",
         "pattern": "opens with Discover", "example_before": "Discover Bhutan.",
         "severity": "high"},
        # Duplicate
        {"failure_code": "META_OPENER_ROBOTIC", "field": "seo_meta",
         "pattern": "opens with Discover", "example_before": "Discover Bhutan again.",
         "severity": "high"},
    ]

    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = None  # no existing record
    mock_conn.execute = AsyncMock()

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)), \
         patch("services.content_generation.flag_fix_node.get_database_url", return_value="postgres://test"):
        count = await write_lessons_log(lessons, min_frequency=2)

    # Only 1 unique lesson should be inserted
    assert count == 1
    assert mock_conn.execute.call_count == 1


@pytest.mark.asyncio
async def test_write_lessons_log_respects_min_frequency():
    """Lessons appearing fewer than min_frequency times should be skipped."""
    lessons = [
        {"failure_code": "NAME_ALL_CAPS", "field": "name",
         "pattern": "all caps name", "example_before": "BHUTAN TOUR",
         "severity": "medium"},
        # This one appears only once — should be skipped with min_frequency=2
    ]

    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = None
    mock_conn.execute = AsyncMock()

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)), \
         patch("services.content_generation.flag_fix_node.get_database_url", return_value="postgres://test"):
        count = await write_lessons_log(lessons, min_frequency=2)

    assert count == 0
    mock_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_write_lessons_log_empty_input():
    """Empty lessons list should return 0 immediately without DB calls."""
    count = await write_lessons_log([], min_frequency=1)
    assert count == 0
