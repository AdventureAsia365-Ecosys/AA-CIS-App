"""F3 (AA-195): pre_audit_checks flags fabricated meals/clock-times in itineraries."""

from services.content_generation.brand_audit_node import pre_audit_checks
from services.content_generation.flag_fix_node import STAGE2_FIX_MAPPING


def _itin(generated_itin):
    """Minimal clean generated dict carrying only the itineraries under test."""
    return {
        "name": "Bhutan Highlands Journey",
        "subtitle": "A refined private journey",
        "seo_meta": "A discreet private journey across the highlands.",
        "highlights": [],
        "itineraries": generated_itin,
    }


def test_pre_audit_flags_itinerary_am_pm_clock_time():
    # A specific am/pm clock time is a fabricated logistic when the source gave none.
    codes = pre_audit_checks(_itin("7:00 AM departure to the valley"))
    assert "ITINERARY_MEAL_TIME_INVENTED" in codes


def test_pre_audit_flags_itinerary_meal_logistics_claim():
    # AA-608: only a logistics-style meal CLAIM ("lunch included") is a product-truth risk,
    # not a narrative reference to a meal.
    codes = pre_audit_checks(_itin("Enjoy a meal at a local family home; lunch included"))
    assert "ITINERARY_MEAL_TIME_INVENTED" in codes


def test_pre_audit_flags_itinerary_meal_codes():
    # "(B, L, D)" meal-code shorthand is a fabricated meal-inclusion logistic.
    codes = pre_audit_checks(_itin("Day 2: Drive to the coast (B, L, D)"))
    assert "ITINERARY_MEAL_TIME_INVENTED" in codes


def test_pre_audit_narrative_meal_word_not_flagged():
    # AA-608: a narrative meal reference (time-of-day / activity) is NOT a product-truth risk
    # and must not be flagged — this was the ~26/30 false-positive source before the fix.
    codes = pre_audit_checks(_itin("After breakfast, cycle to the next village for lunch"))
    assert "ITINERARY_MEAL_TIME_INVENTED" not in codes


def test_pre_audit_clean_itinerary_not_flagged():
    codes = pre_audit_checks(_itin("Explore the old quarter, then visit the temple"))
    assert "ITINERARY_MEAL_TIME_INVENTED" not in codes


def test_pre_audit_clock_word_boundary_holds():
    # "programme" must not trip the meal regex; "Travel" / no am-pm must not trip clock
    codes = pre_audit_checks(_itin("Travel across Vietnam following the programme"))
    assert "ITINERARY_MEAL_TIME_INVENTED" not in codes


def test_pre_audit_bare_24h_clock_not_flagged():
    # A 24h clock with no am/pm is treated as narrative, not a fabricated logistic (AA-608).
    codes = pre_audit_checks(_itin("Day 1: 08:00 breakfast then drive to the valley"))
    assert "ITINERARY_MEAL_TIME_INVENTED" not in codes


def test_pre_audit_itinerary_structured_list_is_detected():
    # itineraries passed as structured list -> coerced via json.dumps and still detected
    structured = [{"day": 1, "detail": "7:00 AM breakfast then drive"}]
    codes = pre_audit_checks(_itin(structured))
    assert "ITINERARY_MEAL_TIME_INVENTED" in codes


def test_pre_audit_itinerary_structured_dict_is_detected():
    structured = {"day1": "Lunch is provided at a local home"}
    codes = pre_audit_checks(_itin(structured))
    assert "ITINERARY_MEAL_TIME_INVENTED" in codes


def test_stage2_fix_mapping_routes_meal_time_code_to_itineraries():
    assert STAGE2_FIX_MAPPING["ITINERARY_MEAL_TIME_INVENTED"] == "itineraries"
