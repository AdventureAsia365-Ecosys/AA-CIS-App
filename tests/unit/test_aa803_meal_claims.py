"""AA-803 — a meal claim in an S1 itinerary is cleared in code when the source has the same meal.

Before Jev questions a1_claim_supported / a1_promise_unsupported go OFF, ITINERARY_MEAL_TIME_INVENTED must not
fire on meals the source itself states (J04: Jev cleared the code on 71 of 914 tours). The rule: same meal on
the same source day, or in INCLUSIONS (words, B/L/D codes, all meals / full board / half board); a day's own
structured list is authoritative; negations and EXCLUSIONS block; clock times are never inferred.
"""
from unittest.mock import patch

import pytest

from services.content_generation import brand_audit_node as ba
from services.content_generation import grounding as g
from services.content_generation import meal_claims as mc
from shared.llm_client.decide import Verdict

CODE = "ITINERARY_MEAL_TIME_INVENTED"


def _jev_off(units, sources, *, tenant_id=None):
    """decide() at mode=off: every asked unit is `skipped` (not enforced)."""
    return {i: Verdict("a1_claim_supported", "off", "skipped") for i in sources}


def _tour(days, inclusions="", exclusions="", duration=None):
    itin = "\n".join(f"Day {n}: {text}" for n, text in enumerate(days, start=1))
    return {"name": "Test Tour", "country": "Vietnam", "duration": duration or f"{len(days)} Days",
            "itineraries": itin, "inclusions": inclusions, "exclusions": exclusions}


def _gen(day_bodies):
    itin = "\n\n".join(f"Day {n} — Stage {n}\n{body}" for n, body in day_bodies)
    return {"name": "A Walk", "itineraries": itin}


def _codes(day_bodies, tour):
    with patch.object(g, "judge_units", side_effect=_jev_off) as m:
        codes = ba.pre_audit_checks(_gen(day_bodies), tour)
    return codes, m


def _src(sentence, day_text, inclusions="", exclusions="", source_all=None):
    ok, _why = mc.meal_claim_sourced(sentence, day_text=day_text, inclusions=inclusions, exclusions=exclusions,
                                     source_all=source_all if source_all is not None else (day_text or "") + inclusions)
    return ok


# ── 1. sourced: the same day names the meal ──────────────────────────────────────────────────────────────

def test_same_day_meal_word_clears_the_code_without_jev():
    tour = _tour(["Arrive in Hanoi and check in.",
                  "Drive to Terelj National Park and enjoy lunch at a local ger camp.",
                  "Return to the city."])
    codes, m = _codes([(2, "Lunch is served at a local ger camp within the park.")], tour)
    assert CODE not in codes
    m.assert_not_called()


@pytest.mark.parametrize("day_text,sentence", [
    ("Overnight stay. (Overnight stay・Breakfast & Dinner provided)", "Breakfast is included at the hotel."),
    ("Cycle to the lake. Meals: B, L, D", "Dinner is served at the lodge."),
    ("Cycle to the lake. Meals  B D", "Breakfast is included."),
    ("Beijing - Huangyaguan (L, D) Meet your guide and drive to the wall.", "Dinner is served at the guesthouse."),
    ("Krabi 4 Islands by Speed Boat with Picnic Lunch (B/L) Island hopping.", "Lunch is served on the beach."),
    ("Accommodation Narain Niwas Palace Meals included: Breakfast | Lunch | Dinner",
     "Meals included: Breakfast, Lunch, Dinner"),
    ("Hike the ridge. Meals Included: • Breakfast, Lunch", "Lunch is provided on the trail."),
    ("Full day safari (Picnic meals will be served during the safari)", "Lunch is served in the park."),
    ("Stop at the farm. Have dinner and stay overnight.", "Dinner is provided at the farm."),
    ("Spend the evening with the family over an evening meal.", "Dinner is served by the host family."),
])
def test_same_day_patterns_source_the_meal(day_text, sentence):
    assert _src(sentence, day_text)


def test_a_negation_far_from_the_meal_does_not_negate_it():
    day = ("The rest of the afternoon and evening will consist of exploring the local village on your own, "
           "relaxing at the hot springs in the hotel, and having a group dinner near the hotel.")
    assert _src("A group dinner is arranged near the hotel.", day)


def test_not_to_forget_is_not_a_negation():
    assert _src("A home-cooked lunch is provided by a Muslim family.",
                "And not to forget, a home cooked lunch by a Muslim family awaits you.")


def test_one_day_source_is_its_own_day():
    tour = {"name": "Terelj day tour", "duration": "1 Day", "inclusions": "Hotel pickup",
            "itineraries": "After touring the complex, depart for Terelj and enjoy lunch at a local ger camp."}
    codes, m = _codes([(1, "Lunch is served at a local ger camp within the park.")], tour)
    assert CODE not in codes
    m.assert_not_called()


# ── 2. sourced: INCLUSIONS ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("inclusions,sentence", [
    ("Stay with all meals, bonfire, professional guide", "Dinner is served in the camp."),
    ("Full board on the trek", "Lunch is provided on the trail."),
    ("Half board accommodation", "Dinner is included at the lodge."),
    ("11 nights accommodation, 11 breakfasts and 8 dinners (Lunch is not included)",
     "Overnight accommodation is provided; breakfast is included."),
    ("Included: * English speaking guide * Chinese lunch", "A Chinese lunch is included with the tour."),
])
def test_inclusions_source_the_meal(inclusions, sentence):
    assert _src(sentence, "Visit the old town.", inclusions=inclusions)
    assert _src(sentence, None, inclusions=inclusions)       # INCLUSIONS also cover a day that cannot be matched


def test_inclusions_end_to_end_clear_the_code():
    tour = _tour(["Arrive.", "Hike the Great Wall.", "Depart."], inclusions="Guide, transport, all meals")
    codes, m = _codes([(2, "Lunch is provided on the Wall.")], tour)
    assert CODE not in codes
    m.assert_not_called()


# ── 3. not sourced (the code stays with Jev off) ─────────────────────────────────────────────────────────

def test_meal_not_in_source_keeps_the_code():
    tour = _tour(["Arrive.", "Hike the Great Wall and return to the hotel.", "Depart."],
                 inclusions="Guide, transport, entrance fees")
    codes, m = _codes([(2, "Lunch is provided on the Wall.")], tour)
    assert CODE in codes
    m.assert_called_once()                  # the unsourced sentence still goes to Jev (off: skipped)


def test_a_different_meal_keeps_the_code():
    tour = _tour(["Arrive.", "Breakfast at the hotel, then hike the wall.", "Depart."])
    codes, _m = _codes([(2, "Dinner is served at the guesthouse.")], tour)
    assert CODE in codes


def test_meal_on_the_neighbouring_day_only_keeps_the_code():
    tour = _tour(["Arrive and have dinner at the hotel.", "Hike the wall.", "Depart."])
    codes, _m = _codes([(2, "Dinner is served at the hotel.")], tour)
    assert CODE in codes


@pytest.mark.parametrize("sentence,source_day", [
    ("Dinner is served at 7:30 pm.", "Dinner at the lodge."),         # am/pm time the source never gives
    ("Dinner is served at 19:30 at the lodge.", "Dinner at the lodge."),  # 24-hour time
    ("Breakfast is included at 7 am before the drive.", "Meals: B"),
])
def test_an_invented_meal_time_is_never_sourced(sentence, source_day):
    assert not _src(sentence, source_day)


def test_a_meal_time_the_source_states_is_fine():
    assert _src("Dinner is served at 19:30 at the lodge.", "Dinner at the lodge at 19:30.")


def test_invented_clock_time_keeps_the_code_end_to_end():
    tour = _tour(["Arrive.", "Dinner at the lodge.", "Depart."])
    codes, _m = _codes([(2, "Dinner is served at 7:30 pm at the lodge.")], tour)
    assert CODE in codes


@pytest.mark.parametrize("day_text", [
    "Head out for an optional dinner with the group.",
    "Dinner at own expense in the night market.",
    "Free evening. Dinner is not included.",
    "Accommodation: Riverside Hotel. Meals Included: • No meals included today. Stop for dinner in town.",
    "Accommodation: Hotel Meals: None",
])
def test_a_negated_source_meal_is_not_sourced(day_text):
    assert not _src("Dinner is included tonight.", day_text)


def test_inclusions_negation_blocks_the_meal():
    assert not _src("Lunch is included.", None, inclusions="11 breakfasts and 8 dinners (Lunch is not included)")


def test_exclusions_block_a_meal_the_day_mentions():
    assert not _src("Lunch is included.", "Picnic lunch by the lake.", exclusions="Lunches and drinks")


def test_a_generic_exclusion_blocks_nothing():
    assert _src("Lunch is included.", "Picnic lunch by the lake.",
                exclusions="Meals not mentioned in the itinerary, drinks with meals")


def test_structured_day_list_is_authoritative():
    # A passing mention ("after lunch") does not add a meal the day's own list leaves out.
    assert not _src("Lunch is included.", "After lunch, we continue the walk. Meals  B D")
    assert not _src("Dinner is included tonight.", "Hike the ridge. Meals Included: • Breakfast, Lunch")
    # ... and INCLUSIONS do not either.
    assert not _src("Lunch is included.", "Meals: B", inclusions="All meals")


def test_the_same_day_saying_provided_beats_its_list():
    day = "A picnic lunch on the Wall is provided. Return to Beijing. Meals Included: •Breakfast"
    assert _src("A picnic lunch is provided on the Wall.", day)


def test_half_board_does_not_source_lunch():
    assert not _src("Lunch is provided.", "Breakfast at the hotel.", inclusions="Half board")


def test_all_meals_as_per_itinerary_means_the_itinerary_meals_only():
    inc = "Hotel, all meals specified in the itinerary, entrance tickets"
    assert _src("Lunch is served at a local ger camp.", "Enjoy lunch at a local ger camp.", inclusions=inc)
    assert not _src("Dinner is included tonight.", "Enjoy lunch at a local ger camp.", inclusions=inc)
    assert not _src("Dinner is included tonight.", None, inclusions=inc)


def test_multi_day_source_that_cannot_be_split_uses_inclusions_only():
    tour = {"name": "Loop", "duration": "5 Days", "inclusions": "Guide",
            "itineraries": "A loop through the hills with dinner at a homestay on the second night."}
    codes, _m = _codes([(2, "Dinner is served at the homestay.")], tour)
    assert CODE in codes


def test_an_output_sentence_without_a_positive_meal_is_left_to_jev():
    ok, why = mc.meal_claim_sourced("Dinner is served at own expense in the night market.",
                                    day_text="Dinner at the lodge.", source_all="Dinner at the lodge.")
    assert not ok and why == "no_positive_meal_claim"


def test_generic_meals_claim_needs_some_source_meal():
    assert _src("Meals included: as listed for the day.", "Meals: B, D")
    assert not _src("Meals included: as listed for the day.", "Walk to the temple.")


def test_claimed_meals_drop_the_negated_part():
    claimed, generic = mc.claimed_meals("Breakfast is included; lunch is at your own expense.")
    assert claimed == {"breakfast"} and generic is False


# ── 4. Jev wiring and what still guards numbers ──────────────────────────────────────────────────────────

def test_only_unsourced_sentences_are_sent_to_jev():
    tour = _tour(["Arrive.", "Breakfast at the hotel, then hike.", "Depart."])
    gen = [(2, "Breakfast is served at the hotel. Lunch is provided on the trail.")]
    with patch.object(g, "judge_units", return_value={}) as m:
        codes = ba.pre_audit_checks(_gen(gen), tour)
    assert CODE in codes
    units = m.call_args.args[0]
    assert [u["sentence"] for u in units] == ["Lunch is provided on the trail."]


def test_jev_accept_still_clears_what_the_rule_does_not_while_the_question_is_on():
    tour = _tour(["Arrive.", "Hike the wall.", "Depart."])
    with patch.object(g, "judge_units",
                      return_value={0: Verdict("a1_claim_supported", "enforce", "accept", probability=0.95)}):
        assert CODE not in ba.pre_audit_checks(_gen([(2, "Lunch is provided on the Wall.")]), tour)


def test_unsupported_number_stays_fail_closed_with_jev_off():
    units = [{"field": "itineraries", "sentence": "The trek climbs 1,200 metres to the pass.", "day": 2}]
    res = g.classify(units, {0: ["1200"]}, {0: Verdict("a1_claim_supported", "off", "skipped")})
    assert [v["code"] for v in res["violations"]] == ["UNSUPPORTED_NUMBER"]


# ── 5. short sentences and day-title lines (never asked to Jev) ──────────────────────────────────────────

def test_a_short_sourced_meal_sentence_clears_the_code():
    # "Lunch is included." has fewer than MIN_WORDS words, so grounding never asked Jev about it and the code
    # fired on every such tour, Jev or not. The rule now reads it.
    tour = _tour(["Arrive.", "Boat trip to the islands with a picnic lunch.", "Depart."])
    codes, m = _codes([(2, "The boat visits four islands. Lunch is included.")], tour)
    assert CODE not in codes
    m.assert_not_called()


def test_a_short_unsourced_meal_sentence_keeps_the_code_without_asking_jev():
    tour = _tour(["Arrive.", "Boat trip to the islands.", "Depart."])
    accept = {0: Verdict("a1_claim_supported", "enforce", "accept", probability=0.99)}
    with patch.object(g, "judge_units", return_value=accept) as m:
        codes = ba.pre_audit_checks(_gen([(2, "The boat visits four islands in the bay. Dinner included.")]), tour)
    assert CODE in codes
    m.assert_not_called()


def test_a_meal_code_in_a_day_title_is_checked():
    tour = _tour(["Arrive.", "Hue citadel. Meals: B, L", "Depart."])
    itin = "Day 2 — Hue Citadel (B, L)\nWalk the citadel walls and the royal tombs."
    with patch.object(g, "judge_units", side_effect=_jev_off):
        assert CODE not in ba.pre_audit_checks({"name": "A Walk", "itineraries": itin}, tour)
    itin_d = "Day 2 — Hue Citadel (B, L, D)\nWalk the citadel walls and the royal tombs."
    with patch.object(g, "judge_units", side_effect=_jev_off):
        assert CODE in ba.pre_audit_checks({"name": "A Walk", "itineraries": itin_d}, tour)


def test_no_meals_stated_as_the_source_says():
    assert _src("Meals: None included.", "Arrive and transfer to the hotel. Meals: None")
    assert not _src("Meals: None included.", "Arrive. Welcome dinner at the hotel.")


def test_itinerary_sentences_keep_titles_and_short_sentences():
    units = mc.itinerary_sentences("Day 1 — Arrival (D)\nArrive. Dinner included.\n\nDay 2 — Hue\nWalk.")
    assert [(u["day"], u["sentence"]) for u in units] == [
        (1, "Day 1 — Arrival (D)"), (1, "Arrive."), (1, "Dinner included."), (2, "Day 2 — Hue"), (2, "Walk.")]
