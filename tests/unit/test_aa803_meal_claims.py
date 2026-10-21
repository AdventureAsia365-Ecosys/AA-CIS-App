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


def _gen(day_bodies, n_days=None):
    """A written itinerary. With ``n_days``, every day 1..n_days the test leaves out gets a neutral body: a real
    rewrite has one output day per source day, and the rule only matches days when the two line up."""
    bodies = dict(day_bodies)
    for n in range(1, (n_days or 0) + 1):
        bodies.setdefault(n, "Walk through the old town.")
    itin = "\n\n".join(f"Day {n} — Stage {n}\n{body}" for n, body in sorted(bodies.items()))
    return {"name": "A Walk", "itineraries": itin}


def _codes(day_bodies, tour):
    n_days = max(g._source_days(tour)[0])
    with patch.object(g, "judge_units", side_effect=_jev_off) as m:
        codes = ba.pre_audit_checks(_gen(day_bodies, n_days), tour)
    return codes, m


def _src(sentence, day_text, inclusions="", exclusions="", source_all=None, n_days=None):
    ok, _why = mc.meal_claim_sourced(sentence, day_text=day_text, inclusions=inclusions, exclusions=exclusions,
                                     source_all=source_all if source_all is not None else (day_text or "") + inclusions,
                                     n_days=n_days)
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
    ("Half board accommodation", "Dinner is included at the lodge."),
    ("Hotel accommodation with daily breakfast", "Breakfast is served at the hotel."),
    ("Breakfast every morning, airport transfers", "Breakfast is included before the drive."),
    ("Breakfast, lunch and dinner daily", "Lunch is provided on the trail."),
])
def test_inclusions_source_the_meal(inclusions, sentence):
    assert _src(sentence, "Visit the old town.", inclusions=inclusions)
    assert _src(sentence, None, inclusions=inclusions)       # INCLUSIONS also cover a day that cannot be matched


def test_an_inclusions_count_covers_every_day_only_when_it_reaches_the_tour_length():
    inc = "11 nights accommodation, 11 breakfasts and 8 dinners (Lunch is not included)"
    breakfast = "Overnight accommodation is provided; breakfast is included."
    assert _src(breakfast, "Visit the old town.", inclusions=inc, n_days=11)
    assert not _src(breakfast, "Visit the old town.", inclusions=inc, n_days=12)
    assert not _src(breakfast, "Visit the old town.", inclusions=inc)                  # length unknown
    # 8 dinners on an 11-day tour: only a day whose own text has dinner is sourced.
    assert not _src("Dinner is served at the hotel tonight.", "Free day to explore.", inclusions=inc, n_days=11)
    assert _src("Dinner is served at the hotel tonight.", "Dinner at the hotel.", inclusions=inc, n_days=11)
    # ... and the negated lunch is blocked even when the day mentions it.
    assert not _src("Lunch is included.", "Lunch at a local cafe.", inclusions=inc, n_days=11)


# B2 (review): a qualified INCLUSIONS mention, "meals as per the itinerary" or a code legend must not cover a
# free day with no meal in its source text. Each of these cleared an invented dinner before the fix.
@pytest.mark.parametrize("inclusions", [
    "Meals included as per itinerary",
    "Meals included in the itinerary",
    "All meals included as mentioned in the itinerary",
    "Meals included where indicated",
    "Hotel, all meals specified in the itinerary, entrance tickets",
    "Meals as indicated (B=Breakfast, L=Lunch, D=Dinner)",
    "Meals as specified: B - breakfast, L - lunch, D - dinner",
    "Accommodation; Welcome dinner; Transfers",
    "Farewell dinner on the last night",
    "1 dinner",
    "Dinner on Day 2",
    "11 nights accommodation, 11 breakfasts and 8 dinners",
    "Included: * English speaking guide * Chinese dinner",
])
def test_qualified_inclusions_do_not_cover_a_free_day(inclusions):
    assert not _src("Dinner is served at the hotel tonight.", "Free day to explore the city.",
                    inclusions=inclusions, n_days=11)
    assert not _src("Dinner is served at the hotel tonight.", None, inclusions=inclusions, n_days=11)


def test_qualified_inclusions_end_to_end_keep_the_code_with_jev_off():
    for inc in ("Meals included as per itinerary", "Accommodation; Welcome dinner; Transfers",
                "Meals as per itinerary (B = Breakfast, L = Lunch, D = Dinner)"):
        tour = _tour(["Arrive in Jaipur.", "Free day to explore the city.", "Depart."], inclusions=inc)
        codes, _m = _codes([(2, "Dinner is served at the hotel tonight.")], tour)
        assert CODE in codes, inc


def test_a_qualified_inclusion_still_counts_through_the_same_day():
    assert _src("A Chinese lunch is included with the tour.", "Visit the Forbidden City and enjoy a Chinese lunch.",
                inclusions="Included: * English speaking guide * Chinese lunch")
    assert _src("A welcome dinner is served at the hotel.", "Arrive. Welcome dinner at the hotel.",
                inclusions="Welcome dinner")


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


# B1 (review): a negation in brackets belongs to the clause before it. Real source sentences (AA-925 export).
@pytest.mark.parametrize("day_text,sentence", [
    ("01:00 PM – Lunch Break Enjoy lunch at a recommended local restaurant or 5-star hotel (optional, not included). "
     "Relax.", "Lunch is included at a recommended local restaurant."),
    ("Proceed to Sagada for lunch at a local restaurant (own account). Then enjoy a walk.",
     "Lunch is served at a local restaurant in Sagada."),
    ("Dinner at the night market (own expense).", "Dinner is served at the night market."),
    ("Stop for lunch at a local restaurant (not included).", "Lunch is included at a local restaurant."),
    ("Dinner (not inclusive).", "Dinner is included."),
])
def test_a_bracketed_negation_negates_the_meal_before_it(day_text, sentence):
    assert not _src(sentence, day_text)


def test_a_bracketed_negation_end_to_end_keeps_the_code_with_jev_off():
    day = ("01:00 PM – Lunch Break Enjoy lunch at a recommended local restaurant or 5-star hotel "
           "(optional, not included). Relax.")
    tour = _tour(["Arrive in Jaipur.", day, "Depart."])
    codes, _m = _codes([(2, "Lunch is included at a recommended local restaurant.")], tour)
    assert CODE in codes


def test_a_bracket_that_names_its_own_meal_negates_only_that_meal():
    inc = "11 breakfasts and 11 dinners (Lunch is not included)"
    assert _src("Dinner is served at the hotel.", "Free day.", inclusions=inc, n_days=11)
    assert not _src("Lunch is served at the hotel.", "Free day.", inclusions=inc, n_days=11)


def test_a_plain_bracket_is_read_inline():
    assert _src("Dinner is served at the lodge.", "Dinner at the lodge (local specialities).")


# Weaker spots (review): a mention that says the meal is NOT provided is no source for "included".
@pytest.mark.parametrize("day_text", [
    "Free time for lunch at your leisure in the old town.",
    "Your guide can recommend a restaurant for dinner.",
    "Dinner with a local family can be arranged on request.",
    "Lunch on own.",
    "Lunch on your own.",
    "Self-pay lunch at the market.",
    "Dinner payable locally.",
    "Dinner excl.",
    "Lunch is available at the visitor centre for purchase.",
    "Rest before tomorrow's dinner cruise.",
    "Walk around the neighborhood and a group dinner is on your own choice.",
    "Shopping time for the day. Today\u2019s dinner is on your own.",
    "Lunch is payable directly unless booked in advance.",
    "Dinner at the hotel restaurant, which is famous for local food and wine, is not included.",
])
def test_a_not_provided_mention_does_not_source_the_meal(day_text):
    meal = "Lunch" if "unch" in day_text else "Dinner"
    assert not _src(f"{meal} is included.", day_text)


def test_on_your_own_far_from_the_meal_still_keeps_it():
    # The cue window stays: "exploring ... on your own, relaxing ..., and having a group dinner".
    assert _src("A group dinner is arranged near the hotel.",
                "Explore the local village on your own, relax at the hot springs in the hotel, "
                "and have a group dinner near the hotel.")


def test_no_extra_charge_is_not_a_negation():
    assert _src("Dinner is served with no extra charge.", "Dinner with the host family at no extra cost.")


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


# B3 (review): on the output side only an explicit exclusion drops a meal from the claim.
@pytest.mark.parametrize("sentence", [
    "Breakfast is included at the hotel, and dinner is served at a restaurant not far from the river.",
    "Breakfast is included and dinner is served with no extra charge.",
    "Breakfast is included; dinner is served at a spot you will never forget.",
    "Breakfast is included and dinner is served without delay at the lodge.",
    "No meals are included except breakfast and dinner.",
])
def test_an_unrelated_negation_does_not_hide_an_invented_meal(sentence):
    assert "dinner" in mc.claimed_meals(sentence)[0]
    for day in ("After breakfast, drive to the lake. Meals: B", "After breakfast, drive to the lake and walk."):
        assert not _src(sentence, day)


def test_an_unrelated_negation_end_to_end_keeps_the_code_with_jev_off():
    tour = _tour(["Arrive.", "After breakfast, drive to the lake. Meals: B", "Depart."])
    codes, _m = _codes([(2, "Breakfast is included at the hotel, and dinner is served at a restaurant "
                            "not far from the river.")], tour)
    assert CODE in codes


@pytest.mark.parametrize("sentence,claimed", [
    ("Breakfast is included; lunch is at your own expense.", {"breakfast"}),
    ("Breakfast and dinner are included, lunch is at own expense.", {"breakfast", "dinner"}),
    ("Breakfast is included, and an optional dinner can be booked.", {"breakfast"}),
    ("Breakfast is included; lunch is not included.", {"breakfast"}),
    ("Breakfast is included (lunch at your own expense).", {"breakfast"}),
])
def test_an_explicit_exclusion_drops_only_its_meal(sentence, claimed):
    assert mc.claimed_meals(sentence)[0] == claimed


# Times in words are times too: never inferred.
@pytest.mark.parametrize("sentence", ["Lunch is served at noon.", "Breakfast is served at 7 o'clock.",
                                      "Dinner is served at half past seven."])
def test_a_time_in_words_must_be_in_the_source(sentence):
    day = "Breakfast at the hotel. Lunch at the farm. Dinner at the farm."
    assert not _src(sentence, day)
    assert _src(sentence, day + " Meals at noon, 7 o'clock and half past seven.")


def test_a_clock_time_must_be_on_the_same_source_day():
    src_all = "DAY 1: Dinner at 19:30.\nDAY 2: Dinner at the farm."
    assert not _src("Dinner is served at 19:30 at the farm.", "Dinner at the farm.", source_all=src_all)


# ── 4. Jev wiring and what still guards numbers ──────────────────────────────────────────────────────────

def test_only_unsourced_sentences_are_sent_to_jev():
    tour = _tour(["Arrive.", "Breakfast at the hotel, then hike.", "Depart."])
    gen = [(2, "Breakfast is served at the hotel. Lunch is provided on the trail.")]
    with patch.object(g, "judge_units", return_value={}) as m:
        codes = ba.pre_audit_checks(_gen(gen, 3), tour)
    assert CODE in codes
    units = m.call_args.args[0]
    assert [u["sentence"] for u in units] == ["Lunch is provided on the trail."]


def test_jev_accept_still_clears_what_the_rule_does_not_while_the_question_is_on():
    tour = _tour(["Arrive.", "Hike the wall.", "Depart."])
    with patch.object(g, "judge_units",
                      return_value={0: Verdict("a1_claim_supported", "enforce", "accept", probability=0.95)}):
        assert CODE not in ba.pre_audit_checks(_gen([(2, "Lunch is provided on the Wall.")], 3), tour)


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
        codes = ba.pre_audit_checks(_gen([(2, "The boat visits four islands in the bay. Dinner included.")], 3),
                                    tour)
    assert CODE in codes
    m.assert_not_called()


def test_a_meal_code_in_a_day_title_is_checked():
    tour = _tour(["Arrive.", "Hue citadel. Meals: B, L", "Depart."])
    itin = "Day 1 — Arrival\nArrive.\n\nDay 2 — Hue Citadel (B, L)\nWalk the citadel walls and the royal tombs."
    itin += "\n\nDay 3 — Departure\nDepart."
    with patch.object(g, "judge_units", side_effect=_jev_off):
        assert CODE not in ba.pre_audit_checks({"name": "A Walk", "itineraries": itin}, tour)
    itin_d = itin.replace("(B, L)", "(B, L, D)")
    with patch.object(g, "judge_units", side_effect=_jev_off):
        assert CODE in ba.pre_audit_checks({"name": "A Walk", "itineraries": itin_d}, tour)


def test_no_meals_stated_as_the_source_says():
    assert _src("Meals: None included.", "Arrive and transfer to the hotel. Meals: None")
    assert not _src("Meals: None included.", "Arrive. Welcome dinner at the hotel.")


def test_itinerary_sentences_keep_titles_and_short_sentences():
    units = mc.itinerary_sentences("Day 1 — Arrival (D)\nArrive. Dinner included.\n\nDay 2 — Hue\nWalk.")
    assert [(u["day"], u["sentence"]) for u in units] == [
        (1, "Day 1 — Arrival (D)"), (1, "Arrive."), (1, "Dinner included."), (2, "Day 2 — Hue"), (2, "Walk.")]


# ── 6. review round 3 (S246): false clears found by an adversarial probe, each fixed fail-closed ─────────────
# Real source sentences (AA-925 export) where the probe found them; synthetic ones are marked as such.

def _src_day(sentence, day_text, day, n_days, inclusions="", exclusions=""):
    ok, _why = mc.meal_claim_sourced(sentence, day_text=day_text, inclusions=inclusions, exclusions=exclusions,
                                     source_all=(day_text or "") + inclusions, n_days=n_days, day=day)
    return ok


def _itin(n_days, bodies):
    """A full written itinerary: every day 1..n_days, the given bodies on their days."""
    return _gen(list(bodies.items()), n_days)["itineraries"]


def _fires(itin, tour):
    with patch.object(g, "judge_units", side_effect=_jev_off):
        return CODE in ba.pre_audit_checks({"name": "A Walk", "itineraries": itin}, tour)


@pytest.mark.parametrize("day_text", [
    "/ Meet day – [No Cycling] Meet at Nikko Kanaya Hotel ... (Overnight stay・No meal provided) *French dinner using "
    "local fresh fish or meat at a classic style restaurant in the hotel can be arranged at extra cost.",
    "Free day at leisure in Kathmandu (No meals).",                       # synthetic
    "Free day at leisure in Kathmandu (Meals: None).",
    "Free day at leisure in Kathmandu (meals not included).",
])
def test_a_bracketed_no_meals_blocks_a_daily_inclusion(day_text):
    assert not _src_day("Breakfast is served at the hotel.", day_text, 1, 5,
                        inclusions="Accommodation with breakfast daily")


@pytest.mark.parametrize("inclusions,day_text,sentence,day", [
    ("All Meals while on trek (Veg) from day 1st dinner to day 8th breakfast.", "Depart from Dehradun.",
     "Dinner is served at the hotel in Dehradun.", 9),
    ("All Meals while on trek (Veg) from day 1st dinner to day 8th breakfast.",
     "Arrive in Dehradun, transfer to hotel.", "Lunch is served at the hotel on arrival in Dehradun.", 1),
    ("All meals during the trek", "Arrive in Delhi.", "Dinner is served at the Delhi hotel.", 1),
    ("Full board on safari days; breakfast only in Nairobi", "Arrive in Nairobi.", "Lunch is served at the hotel.", 1),
    ("Full board on the trek", "Arrive in Kathmandu.", "Lunch is provided on the trail.", 1),
])
def test_a_scoped_full_board_covers_no_day_by_itself(inclusions, day_text, sentence, day):
    assert not _src_day(sentence, day_text, day, 9, inclusions=inclusions)


@pytest.mark.parametrize("inclusions", ["Stay with all meals, bonfire, professional guide",
                                        "All meals throughout the tour", "Guide, transport, all meals",
                                        "Accommodation on a full board basis"])
def test_an_unscoped_full_board_still_covers_every_day(inclusions):
    assert _src_day("Dinner is served at the camp.", "Free day.", 4, 9, inclusions=inclusions)


@pytest.mark.parametrize("inclusions,day_text,sentence,day", [
    ("Daily breakfast and lunch on Day 3", "Drive to Dehradun and on to Sankri.",
     "Lunch is served at a roadside dhaba en route to Sankri.", 2),
    ("Breakfast daily, dinner on Days 3-5", "Drive to Dehradun, overnight at hotel.",
     "Dinner is served at the hotel in Dehradun.", 6),
])
def test_a_daily_cue_does_not_bleed_to_the_next_meal(inclusions, day_text, sentence, day):
    assert not _src_day(sentence, day_text, day, 7, inclusions=inclusions)
    assert _src_day("Breakfast is served at the hotel.", day_text, day, 7, inclusions=inclusions)


@pytest.mark.parametrize("day_text,sentence", [
    ("Enjoy a delicious lunch at a premium 5-star hotel or a charming rooftop café with a view of the Taj Mahal "
     "(optional, depending on your package).", "Lunch is served at a 5-star hotel or a rooftop café."),
    ("Enjoy a delicious multi-cuisine lunch at a luxury 5-star hotel in Agra or a popular rootop cafe, optional in "
     "your tour package.", "Lunch is served at a luxury hotel in Agra."),
])
def test_a_long_sentence_with_a_trailing_optional_does_not_source_the_meal(day_text, sentence):
    assert not _src_day(sentence, day_text, 1, 1)


def test_a_weak_bracket_does_not_wipe_out_a_meals_list_before_it():
    day = ("Meals included: breakfast | dinner Bullet train to Gyeongju, faster than you think (not by car) aboard a "
           "high-speed train. Finish your day with a traditional dinner.")
    assert _src_day("Meals included: breakfast, dinner", day, 4, 9)


@pytest.mark.parametrize("day_text", [
    "End the day with a scrumptious dinner at a popular local restaurant (at passenger’s expense).",
    "Dinner at a local restaurant at your expense.",                      # synthetic from here
    "Dinner at a local restaurant (at guest's expense).",
    "Dinner at a local restaurant (own).",
    "Visit the old palace. Dinner: Own.",
    "Dinner at a local restaurant (approx. USD 10 pp).",
    "Dinner at a lakeside restaurant (extra).",
    "Dinner is self-arranged in town.",
    "Dinner at the market, pay direct to the vendor.",
])
def test_a_guest_paid_meal_is_not_sourced(day_text):
    assert not _src_day("Dinner is served at a local restaurant.", day_text, 2, 4)


@pytest.mark.parametrize("day_text", [
    "This evening, opt for a traditional dinner with entertainment in the desert.",
    "Fly to Delhi for your final evening in India, and opt to join your group for a farewell dinner.",
    "Tonight, you’re free to find a local dinner or a good spot to watch the sunset.",
    "The rest of the afternoon is free for your own discoveries. Your leader will be happy to share some tips on "
    "great places to grab a local dinner.",
    "Choose your dinner at various shops along the street",
])
def test_an_opt_in_or_find_your_own_meal_is_not_sourced(day_text):
    assert not _src_day("Dinner is served at a local restaurant.", day_text, 4, 15)


def test_a_pick_cue_after_the_meal_keeps_it():
    assert _src_day("Breakfast is served at the hotel.", "After breakfast, choose a hike or a rest by the pool.", 2, 4)


@pytest.mark.parametrize("day_text", [
    "We stop for lunch at the floating market if time permits.",          # synthetic
    "Those who wish can join the guide for lunch at a village home.",
    "Guests on the deluxe option have lunch at the palace hotel.",
    "Those who opt for this tour will return to the lodge for lunch",     # real wording (breakfast in the source)
    "Lunch at the floating market, if time permits.",
])
def test_a_conditional_or_partial_group_meal_is_not_sourced(day_text):
    assert not _src_day("Lunch is served at a local restaurant.", day_text, 2, 4)


def test_a_condition_about_the_next_activity_keeps_the_listed_meal():
    day = ("Enjoy a home-cooked picnic lunch somewhere along the way, and if you have enough free time, visit a local "
           "ceramics factory. Overnight in Kanchanaburi Meals: Breakfast / Lunch")
    assert _src_day("A home-cooked picnic lunch is arranged along the route.", day, 7, 10)


def test_an_inline_day_marker_makes_the_day_unmatched():
    tour = {"name": "T", "duration": "4 Days", "inclusions": "", "exclusions": "",
            "itineraries": "Day 1: Arrive in Kathmandu and transfer to the hotel.\nDay 2: Drive to Pokhara and walk "
                           "the lakeside. Day 3: Boat on Phewa Lake, picnic lunch at the World Peace Pagoda.\n"
                           "Day 4: Fly to Kathmandu and depart."}
    assert _fires(_itin(4, {2: "Lunch is served by the lakeside in Pokhara."}), tour)


@pytest.mark.parametrize("sentence", ["All meals are included.", "Evening meal included.",
                                      "Camp accommodation and all meals are provided.",
                                      "Lodge accommodation with full board meals.", "Meals are provided.",
                                      "Lunch and dinner are included on board the junk.",
                                      "Dinner will be served on board.", "Lunches are provided on board."])
def test_generic_plural_and_future_meal_claims_are_checked(sentence):
    assert ba.ITIN_MEAL_INVENTED.search(sentence)
    tour = _tour(["Arrive. Meals: None", "Hike to the resort. Meals: B", "Depart. Meals: None"])
    assert _fires(_itin(3, {2: sentence}), tour)


@pytest.mark.parametrize("sentence", ["Lunch is not included.", "Dinner recommendations are provided by the guide.",
                                      "After breakfast, drive to the lake."])
def test_the_claim_regex_skips_negated_and_advice_sentences(sentence):
    assert not ba.ITIN_MEAL_INVENTED.search(sentence)


@pytest.mark.parametrize("claim", ["Drive to Mai Chau and walk the valley. Meals: B, L and D", "Meals: B | L | D",
                                   "Meals: B · L · D"])
def test_every_code_of_a_list_is_claimed(claim):
    assert mc.claimed_meals(claim)[0] == {"breakfast", "lunch", "dinner"}
    assert not _src_day(claim, "Drive to Mai Chau, lunch at a local house, walk the valley. Meals: B, L", 2, 3)


@pytest.mark.parametrize("sentence,meal", [
    ("Breakfast is served at the hotel, and dinner is served on board (drinks not included).", "dinner"),
    ("Breakfast is served at the hotel and lunch at a pearl farm where jewellery is available for purchase.", "lunch"),
    ("Breakfast is served at the hotel, and after an optional kayak, lunch is served on deck.", "lunch"),
    ("Breakfast is included, and meals on board the junk are prepared by the onboard chef.", "lunch"),
    ("Breakfast is served at the hotel, and a seafood feast awaits on board in the evening.", "dinner"),
    ("Breakfast is included, and a beach BBQ is served on Titop Island in the evening.", "dinner"),
    ("Breakfast is included; a picnic is served on the beach at midday.", "lunch"),
    ("Breakfast is served at the hotel, followed by a brunch buffet on board.", "lunch"),
])
def test_a_second_meal_beside_a_sourced_breakfast_stays_claimed(sentence, meal):
    assert meal in mc.claimed_meals(sentence)[0]
    assert not _src_day(sentence, "Drive to Halong Bay and board the junk. Meals: B", 2, 3)


def test_a_specified_generic_meals_claims_only_its_list():
    assert mc.claimed_meals("Overnight accommodation and meals (breakfast and dinner) are provided.")[0] == {
        "breakfast", "dinner"}


@pytest.mark.parametrize("sentence", [
    "Breakfast is served at the hotel, and breakfast is provided again as a boxed meal tomorrow before the transfer.",
    "Breakfast is included today and tomorrow.",
    "Breakfast is included, and a boxed breakfast is provided on Day 3.",
])
def test_a_meal_on_another_day_is_not_judged_on_this_day(sentence):
    assert not _src_day(sentence, "Drive to Halong Bay and board the junk. Meals: B", 2, 3)


def test_quoted_day_headers_do_not_fold_later_days_into_one():
    itin = ("Day 1 / Arrive in Nasu.\nDay 2 / Stage 1 (Overnight stay・Breakfast provided)\n"
            "Day 3 / Stage 2 (Overnight stay・Breakfast provided)\n"
            "Day 4 / Stage 3 (Overnight stay・Breakfast provided)\n"
            "\"Day 5 / Stage 4 (Overnight stay・Breakfast & Dinner provided)\"\n\"Day 6 / Tour Conclusion\"")
    tour = {"name": "T", "duration": "6 DAYS", "inclusions": "", "exclusions": "", "itineraries": itin}
    assert _fires(_itin(6, {4: "Dinner is served at the inn in Kaminoyama."}), tour)


def test_an_extra_day_extension_is_not_the_day_it_follows():
    days = [f"Stage {n}." for n in range(1, 9)] + [
        "Breakfast at the hotel. Train to Ella for an overnight stay\nExtra Day 01 Breakfast at the hotel. Picnic "
        "lunch will be provided. Dinner and overnight stay in a remote home stay"]
    tour = _tour(days)
    assert _fires(_itin(9, {9: "Dinner is served at the accommodation in Ella."}), tour)


def _grouped_tour(*blocks):
    return {"name": "T", "duration": "", "inclusions": "", "exclusions": "", "itineraries": "\n".join(blocks)}


def test_grouped_headers_with_drifting_output_days_do_not_match():
    tour = _grouped_tour("Day 1: Arrive in Colombo.", "DAY 2 & 3 - Ella: tea walks. Meals: breakfast only",
                         "DAY 4 & 5 - Wellawaya: farm stay. On the 2nd night an outdoor BBQ dinner by the campfire.",
                         "Day 6: Depart.")
    # The writer made 7 days, so output Day 4 is an Ella day: source block 4 (Wellawaya) is not its day.
    assert _fires(_itin(7, {4: "Dinner is served at the guesthouse in Ella."}), tour)


def test_a_grouped_block_the_writer_split_matches_neither_of_its_days():
    tour = _grouped_tour("Day 1: Arrive in Trincomalee.",
                         "DAY 2 & 3 - Wilpattu NP Two Nights Explore Anuradhapura. On day two, a full day safari in "
                         "Wilpattu with picnic meals.", "Day 4: Depart. Breakfast at the lodge.")
    assert _fires(_itin(4, {2: "A picnic lunch is provided at Anuradhapura."}), tour)
    # ... while the other days still match (output 1..4 = the source days with the block expanded).
    assert not _fires(_itin(4, {4: "Breakfast is served at the lodge."}), tour)


def test_a_grouped_block_the_writer_kept_as_one_day_still_matches():
    tour = _grouped_tour("Day 1: Arrive.", "Day 2, 3 and 4 in Passikudah: a home cooked lunch by a Muslim family.",
                         "Day 5: Depart.")
    itin = "\n\n".join(f"Day {n} — Stage {n}\n{b}" for n, b in (
        (1, "Arrive."), (2, "A home-cooked lunch is provided by a Muslim family."), (5, "Depart.")))
    assert not _fires(itin, tour)


def test_a_writer_that_split_a_day_shifts_the_rest():
    days = ["Arrive.", "Stage 2. ( B )", "Stage 3. ( B )", "Sagada to Banaue. ( B )", "Banaue to Batad. ( B, D )",
            "Stage 6. ( B )", "Depart. ( B )"]
    tour = _tour(days)
    # 8 output days for 7 source days: output Day 5 is the Sagada-Banaue drive, which has breakfast only.
    assert _fires(_itin(8, {5: "Breakfast and dinner included."}), tour)
    assert not _fires(_itin(7, {5: "Breakfast and dinner included."}), tour)


def test_an_overnight_trip_is_two_days_for_inclusions():
    tour = {"name": "T", "duration": "OVERNIGHT TRIP", "exclusions": "",
            "inclusions": "Accommodation at Kajikasou People's Inn (1 night) with dinner and breakfast",
            "itineraries": "Kirifuri-Kogen Highlands → Kajikasou People's Inn (Overnight stay here!) → Ashio Copper "
                           "Mine → Tomihiro Art Museum"}
    assert _fires(_itin(2, {2: "Dinner is served in Midori City before the trip ends."}), tour)
    assert _fires(_itin(2, {1: "Breakfast is served before the drive up to Kirifuri-Kogen."}), tour)


def test_a_tour_wide_meal_count_after_the_last_day_is_not_that_days_meal():
    days = ["Arrive in Hanoi. (D)"] + [f"Stage {n}. (B)" for n in range(2, 8)] + [
        "Transfer to Da Nang airport for your departure flight. (B)\n\nMeals included: 7 breakfasts, 3 lunches, "
        "2 dinners"]
    tour = _tour(days, inclusions="Accommodation in 3-4 star hotels / All transfers")
    assert _fires(_itin(8, {8: "Lunch is served at a riverside restaurant before the transfer."}), tour)
    assert not _fires(_itin(8, {8: "Breakfast is served before the transfer."}), tour)
