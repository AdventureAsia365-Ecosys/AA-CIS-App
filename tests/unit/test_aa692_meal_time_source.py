"""AA-692 A1-3 — the meal/clock-time code only fires when the source does not state the same logistics."""
from unittest.mock import patch

from services.content_generation import brand_audit_node as ba
from shared.llm_client.decide import Verdict

TOUR = {"itineraries": "Day 5: Depart Peradeniya station at 12:30 PM. Dinner included at the lodge."}


def _gen(itin):
    return {"name": "A Walk", "itineraries": itin}


def _v(zone, mode="enforce"):
    return Verdict("a1_claim_supported", mode, zone, probability=0.95)


def test_clock_time_in_source_is_not_invented():
    with patch("services.content_generation.grounding.judge_units") as m:
        codes = ba.pre_audit_checks(_gen("Day 5 — Train\nBoard the train at 12:30 pm toward the hills."), TOUR)
    assert "ITINERARY_MEAL_TIME_INVENTED" not in codes and m.call_count == 0


def test_clock_time_not_in_source_is_invented():
    codes = ba.pre_audit_checks(_gen("Day 5 — Train\nBoard the train at 7:00 AM toward the hills."), TOUR)
    assert "ITINERARY_MEAL_TIME_INVENTED" in codes


def test_meal_claim_cleared_only_by_enforced_accept():
    itin = "Day 5 — Lodge\nArrive at the lodge in the evening. Dinner is included at the lodge tonight."
    with patch("services.content_generation.grounding.judge_units", return_value={0: _v("accept")}):
        assert "ITINERARY_MEAL_TIME_INVENTED" not in ba.pre_audit_checks(_gen(itin), TOUR)
    with patch("services.content_generation.grounding.judge_units", return_value={0: _v("grey")}):
        assert "ITINERARY_MEAL_TIME_INVENTED" in ba.pre_audit_checks(_gen(itin), TOUR)
    with patch("services.content_generation.grounding.judge_units", return_value={0: _v("accept", mode="shadow")}):
        assert "ITINERARY_MEAL_TIME_INVENTED" in ba.pre_audit_checks(_gen(itin), TOUR)
    with patch("services.content_generation.grounding.judge_units", return_value={}):
        assert "ITINERARY_MEAL_TIME_INVENTED" in ba.pre_audit_checks(_gen(itin), TOUR)


def test_without_tour_keeps_old_behaviour():
    assert "ITINERARY_MEAL_TIME_INVENTED" in ba.pre_audit_checks(_gen("Day 1 — X\nMeals: breakfast, dinner."))
