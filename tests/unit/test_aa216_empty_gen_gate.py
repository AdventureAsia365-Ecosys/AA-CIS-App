"""AA-216: empty/missing-field generation must not clear the 7.0 gate."""
from services.content_generation.graph import (
    validate_node, should_retry, min_quality, missing_field_cap, MAX_RETRIES,
)

_FULL = {
    "name": "Jeju Volcanic Trails", "subtitle": "Seven days across Korea's southern isle",
    "summary": "A grounded week of coastal hikes and island villages that rewards slow travel.",
    "highlights": ["Hike Hallasan's crater rim", "Walk the Olle coastal paths", "Stay in a haenyeo village"],
    "itineraries": "Day 1 -- Arrive in Jeju City and walk the eastern shore at dusk.\n\nDay 2 -- Climb toward Hallasan.",
    "seo_title": "Jeju Volcanic Trails: A Seven-Day Korean Island Walk",
    "seo_meta": "Walk Jeju's volcanic coast and island villages on a grounded seven-day Korean hiking journey for slow travellers.",
}


def _state(generated):
    return {"generated": dict(generated), "tour": {"country": "South Korea"}, "seo": {},
            "brand_forbidden_words": [], "retry_count": 0, "seo_mode": "dataforseo"}


def test_empty_generation_capped_below_gate():
    out = validate_node(_state({}))
    assert out["quality_score"] <= missing_field_cap()
    assert out["quality_score"] < min_quality()


def test_empty_generation_not_done():
    out = validate_node(_state({}))
    assert should_retry({**out, "retry_count": 0}) == "retry"
    assert should_retry({**out, "retry_count": MAX_RETRIES - 1}) == "hitl"


def test_partial_missing_field_capped():
    partial = dict(_FULL); partial["seo_meta"] = ""   # one missing field
    out = validate_node(_state(partial))
    assert "MISSING_FIELD" in out["failure_codes"]
    assert out["quality_score"] <= missing_field_cap()


def test_full_clean_generation_not_capped():
    out = validate_node(_state(_FULL))
    assert "MISSING_FIELD" not in out["failure_codes"]
    assert out["quality_score"] >= min_quality()   # clean content still passes (no regress)


def test_structural_fail_flag_only_on_missing():
    out = validate_node(_state(_FULL))
    # clean content: no structural cap applied, structure sub-score intact
    assert out["sub_scores"]["structure"] >= 7.0
