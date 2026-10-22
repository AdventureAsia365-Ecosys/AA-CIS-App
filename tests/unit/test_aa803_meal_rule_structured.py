"""AA-803 (S247), part B — the narrowed meal rule: a meal claim is cleared in code ONLY on structured evidence.

Draft PR #905 cleared meal claims from free text before Jev; three adversarial review rounds found 65 false
clears (an unsourced meal that passed) and never went dry. The narrowed rule (``meal_claims.claim_sourced``) reads
a pure same-day ``Meals:`` / code list, or an unscoped every-day INCLUSIONS item, and nothing else; every other
claim still needs a Jev accept, so with the question off it keeps ITINERARY_MEAL_TIME_INVENTED.

Sections: 1. each clear path; 2. what is deliberately not cleared any more; 3. Jev wiring; 4. every false-clear
pattern of the S246 review rounds (round 1 review, rounds 3, 4 and 5), each built WITH the structured evidence
that would tempt a clear, so the guard that stops it is what the test exercises. Each must keep the code.
"""
from unittest.mock import patch

import pytest

from services.content_generation import brand_audit_node as ba
from services.content_generation import grounding as g
from services.content_generation import meal_claims as mc
from shared.llm_client.decide import Verdict

CODE = "ITINERARY_MEAL_TIME_INVENTED"


def _jev_off(units, sources, *, tenant_id=None):
    """decide() at mode=off: every asked unit is `skipped` (never enforced)."""
    return {i: Verdict("a1_claim_supported", "off", "skipped") for i in sources}


PLACES = ["Hanoi", "Ninh Binh", "Halong Bay", "Hue", "Hoi An", "Saigon", "Mekong Delta", "Dalat", "Sapa", "Can Tho"]


def _tour(days, inclusions="", exclusions="", duration=None):
    """A source tour; each day opens with its place (PLACES), as a real day does."""
    itin = "\n".join(f"Day {n}: {PLACES[n - 1]} - {text}" for n, text in enumerate(days, start=1))
    return {"name": "Test Tour", "country": "Vietnam",
            "duration": f"{len(days)} Days" if duration is None else duration,
            "itineraries": itin, "inclusions": inclusions, "exclusions": exclusions}


def _raw_tour(itineraries, inclusions="", exclusions="", duration=""):
    return {"name": "Test Tour", "country": "Vietnam", "duration": duration, "itineraries": itineraries,
            "inclusions": inclusions, "exclusions": exclusions}


def _itin(n_days, bodies, titles=None):
    """A written itinerary with output days 1..n_days (one per source day, as the writer does); day n is titled
    with its place (PLACES, or ``titles``), so it lines up with the source day of the same place."""
    titles = titles or {}
    return "\n\n".join(f"Day {d} — {titles.get(d, PLACES[d - 1])}\n{bodies.get(d, 'Time to look around.')}"
                       for d in range(1, n_days + 1))


def _fires(tour, itin):
    with patch.object(g, "judge_units", side_effect=_jev_off):
        return CODE in ba.pre_audit_checks({"name": "A Walk", "itineraries": itin}, tour)


def _claim(sentence, day):
    return {"field": "itineraries", "sentence": sentence, "day": day}


LIST_DAYS = ["Arrive in Hanoi and transfer to the hotel.",
             "Morning walk around Hoan Kiem Lake, afternoon at the Temple of Literature. Meals: B, L, D",
             "Drive to Halong Bay and board the junk. Meals: B, D",
             "Return to Hanoi and transfer to the airport."]
LUNCH = "Lunch is served at a local restaurant."
DINNER = "Dinner is served at a local restaurant."
BREAKFAST = "Breakfast is served at the hotel."


# ── 1. the clear paths ───────────────────────────────────────────────────────────────────────────────────────

def test_a_same_day_code_list_clears_without_jev():
    tour = _tour(LIST_DAYS)
    with patch.object(g, "judge_units", side_effect=_jev_off) as m:
        codes = ba.pre_audit_checks({"name": "A", "itineraries": _itin(4, {3: "Dinner is served on board the junk."})},
                                    tour)
    assert CODE not in codes
    m.assert_not_called()


@pytest.mark.parametrize("day_text,sentence", [
    ("Walk the lake. Meals: B, L, D", LUNCH),
    ("Walk the lake. Meals included: Breakfast, Lunch", LUNCH),
    ("Walk the lake. Meals provided - Breakfast | Dinner", DINNER),
    ("Walk the lake (B, L)", LUNCH),
    ("Walk the lake (B/-/D)", DINNER),
    ("Walk the lake. Meals: B/L/D.", "Breakfast and lunch are included."),
    ("Walk the lake.\nMeals: Breakfast & Dinner", DINNER),
])
def test_same_day_structured_lists_source_their_meals(day_text, sentence):
    tour = _tour(["Arrive.", day_text, "Cruise.", "Depart."])
    assert not _fires(tour, _itin(4, {2: sentence}))


@pytest.mark.parametrize("day_text,sentence", [
    ("Walk the lake (B/-/D)", LUNCH),
    ("Walk the lake. Meals: B, D", LUNCH),
    ("Walk the lake. Meals: None", BREAKFAST),
    ("Walk the lake. Meals: -", BREAKFAST),
])
def test_a_meal_the_day_list_leaves_out_is_not_sourced(day_text, sentence):
    tour = _tour(["Arrive.", day_text, "Cruise.", "Depart."])
    assert _fires(tour, _itin(4, {2: sentence}))


def test_short_and_title_line_claims_are_cleared_by_the_day_list():
    tour = _tour(["Arrive.", "Walk the lake. Meals: B, L", "Cruise. Meals: B, D", "Depart."])
    itin = ("Day 1 — Arrival\nArrive.\n\nDay 2 — Hoan Kiem Lake\nWalk the lake. Lunch is included.\n\n"
            "Day 3 — Halong Bay (B, D)\nCruise among the karsts.\n\nDay 4 — Departure\nDepart.")
    assert not _fires(tour, itin)


@pytest.mark.parametrize("inclusions,sentence", [
    ("Hotels, guide, driver, entrance fees, all meals.", DINNER),
    ("All meals", LUNCH),
    ("Full board", LUNCH),
    ("- Accommodation\n- Full board basis\n- Guide", DINNER),
    ("Included: * Guide * Transfers * All meals", LUNCH),
    ("Daily breakfast", BREAKFAST),
    ("Breakfast daily", BREAKFAST),
    ("Breakfast and dinner daily", DINNER),
    ("Hotels; daily breakfast; guide", BREAKFAST),
    ("Three meals a day", LUNCH),
    ("Half board", BREAKFAST),
    ("- Hotels\n- All meals (breakfast, lunch and dinner)\n- Guide", LUNCH),
])
def test_an_unscoped_every_day_inclusion_sources_a_middle_day(inclusions, sentence):
    tour = _tour(["Arrive.", "Walk the old town.", "Visit the temples.", "Depart."], inclusions=inclusions)
    assert not _fires(tour, _itin(4, {2: sentence}))


@pytest.mark.parametrize("inclusions,sentence", [("Half board", LUNCH), ("Half board", DINNER),
                                                 ("Daily breakfast", LUNCH), ("Breakfast and dinner daily", LUNCH)])
def test_an_inclusion_covers_only_its_meals(inclusions, sentence):
    tour = _tour(["Arrive.", "Walk the old town.", "Visit the temples.", "Depart."], inclusions=inclusions)
    assert _fires(tour, _itin(4, {2: sentence}))


@pytest.mark.parametrize("day", [1, 4])
def test_an_inclusion_never_covers_the_first_or_last_day(day):
    tour = _tour(["Arrive.", "Walk the old town.", "Visit the temples.", "Depart."], inclusions="All meals")
    assert _fires(tour, _itin(4, {day: LUNCH}))


def test_a_day_list_beats_the_inclusion():
    tour = _tour(["Arrive.", "Walk the old town. Meals: B", "Visit the temples.", "Depart."], inclusions="All meals")
    assert _fires(tour, _itin(4, {2: LUNCH}))
    assert not _fires(tour, _itin(4, {2: BREAKFAST}))


def test_an_inclusion_does_not_add_a_meal_another_days_list_leaves_out():
    tour = _tour(["Arrive.", "Walk the old town.", "Visit the temples. Meals: B, D", "Depart."], inclusions="All meals")
    assert _fires(tour, _itin(4, {2: LUNCH}))


# ── 2. deliberately left to Jev (the narrowing) ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("day_text,inclusions", [
    ("Hike to Taktsang. Lunch at the cafeteria viewpoint.", ""),                 # narrative same-day mention
    ("Walk the lake.", "Meals as per itinerary"),
    ("Walk the lake.", "Hotel with daily breakfast"),
    ("Walk the lake.", "11 breakfasts, 3 lunches"),
    ("Walk the lake. Breakfast: Yes / Lunch: Yes / Dinner: Yes", ""),
])
def test_free_text_and_qualified_evidence_is_not_read(day_text, inclusions):
    tour = _tour(["Arrive.", day_text, "Cruise.", "Depart."], inclusions=inclusions)
    assert _fires(tour, _itin(4, {2: LUNCH}))


def test_a_one_day_tour_is_left_to_jev():
    tour = _raw_tour("Pick-up at the hotel, drive to the floating market. Meals: L", duration="1 Day")
    assert _fires(tour, "Day 1 — Floating market\nLunch is served at a riverside restaurant.")


# ── 3. Jev wiring ──────────────────────────────────────────────────────────────────────────────────────────

def test_only_the_claims_the_rule_does_not_clear_go_to_jev():
    tour = _tour(LIST_DAYS)
    itin = _itin(4, {2: "Lunch is served near Hoan Kiem Lake.",
                     3: "A seafood feast is served on board after kayaking among the karsts."})
    with patch.object(g, "judge_units", side_effect=_jev_off) as m:
        codes = ba.pre_audit_checks({"name": "A", "itineraries": itin}, tour)
    assert CODE in codes
    asked = [u["sentence"] for u in m.call_args.args[0]]
    assert asked == ["A seafood feast is served on board after kayaking among the karsts."]


def test_a_jev_accept_still_clears_what_the_rule_does_not():
    tour = _tour(LIST_DAYS)
    itin = _itin(4, {3: "A seafood feast is served on board after kayaking among the karsts."})
    accept = Verdict("a1_claim_supported", "enforce", "accept", probability=0.97)
    with patch.object(g, "judge_units", side_effect=lambda u, s, **k: {i: accept for i in s}):
        assert CODE not in ba.pre_audit_checks({"name": "A", "itineraries": itin}, tour)


def test_an_unsourced_clock_time_still_raises_the_code_first():
    tour = _tour(LIST_DAYS)
    assert _fires(tour, _itin(4, {2: "Lunch is served at a local restaurant. The museum opens at 9:00 AM."}))


def test_an_exclusion_blocks_the_meal():
    tour = _tour(LIST_DAYS, exclusions="Lunches and drinks")
    assert _fires(tour, _itin(4, {2: LUNCH}))
    assert mc.exclusion_blocks("Flights, tips") == frozenset()


# ── 4. the S246 false-clear patterns: none may clear ──────────────────────────────────────────────────────

def _list_day(text, meals="B, L, D"):
    return f"{text} Meals: {meals}"


def _day2(day_text, sentence=LUNCH, inclusions="", exclusions="", meals="B, L, D", with_list=True):
    text = _list_day(day_text, meals) if with_list else day_text
    return _tour(["Arrive in Hanoi.", text, "Cruise on Halong Bay.", "Depart."], inclusions, exclusions), \
        _itin(4, {2: sentence})


FALSE_CLEARS = {
    # ── round 1 review (B1-B3) ──
    "r1-bracket-optional-not-included": _day2("Lunch at a 5-star hotel (optional, not included)."),
    "r1-bracket-own-account": _day2("Lunch at a local restaurant (own account)."),
    "r1-bracket-own-expense": _day2("Lunch at a local restaurant (own expense)."),
    "r1-meals-as-per-itinerary": _day2("Walk the old town.", inclusions="Meals included as per itinerary",
                                       with_list=False),
    "r1-code-legend": _day2("Walk the old town. B = Breakfast, L = Lunch, D = Dinner", with_list=False),
    # ── round 3 ──
    "r3-bracket-no-meal-provided": _day2("Free day (Overnight stay・No meal provided).", BREAKFAST,
                                         inclusions="Daily breakfast", with_list=False),
    "r3-bracket-no-meals": _day2("Free day at leisure in Hanoi (No meals).", BREAKFAST,
                                 inclusions="Daily breakfast", with_list=False),
    "r3-bracket-meals-none": _day2("Free day at leisure in Hanoi (Meals: None).", BREAKFAST,
                                   inclusions="Daily breakfast", with_list=False),
    "r3-bracket-meals-not-included": _day2("Free day at leisure in Hanoi (meals not included).", BREAKFAST,
                                           inclusions="Daily breakfast", with_list=False),
    "r3-full-board-while-on-trek": _day2("Walk the old town.", DINNER, with_list=False,
                                         inclusions="All Meals while on trek (Veg) from day 1st dinner to day 8th "
                                                    "breakfast."),
    "r3-all-meals-during-trek": _day2("Walk the old town.", DINNER, inclusions="All meals during the trek",
                                      with_list=False),
    "r3-full-board-on-safari-days": _day2("Walk the old town.", LUNCH, with_list=False,
                                          inclusions="Full board on safari days; breakfast only in Nairobi"),
    "r3-full-board-on-the-trek": _day2("Walk the old town.", LUNCH, inclusions="Full board on the trek",
                                       with_list=False),
    "r3-daily-cue-bleeds-to-day-3-lunch": _day2("Walk the old town.", LUNCH, with_list=False,
                                                inclusions="Daily breakfast and lunch on Day 3"),
    "r3-daily-cue-bleeds-to-days-3-5-dinner": _day2("Walk the old town.", DINNER, with_list=False,
                                                    inclusions="Breakfast daily, dinner on Days 3-5"),
    "r3-trailing-optional-bracket": _day2("Enjoy lunch at a 5-star hotel or a rooftop café (optional, depending on "
                                          "your package)."),
    "r3-trailing-optional-segment": _day2("Enjoy lunch at a luxury hotel in Agra, optional in your tour package."),
    "r3-at-passengers-expense": _day2("End the day with dinner at a local restaurant (at passenger’s expense).",
                                      DINNER),
    "r3-at-your-expense": _day2("Dinner at a local restaurant at your expense.", DINNER),
    "r3-bracket-own": _day2("Dinner at a local restaurant (own).", DINNER),
    "r3-dinner-own": _day2("Visit the old palace. Dinner: Own.", DINNER),
    "r3-price-in-bracket": _day2("Dinner at a local restaurant (approx. USD 10 pp).", DINNER),
    "r3-bracket-extra": _day2("Dinner at a lakeside restaurant (extra).", DINNER),
    "r3-self-arranged": _day2("Dinner is self-arranged in town.", DINNER),
    "r3-pay-direct": _day2("Dinner at the market, pay direct to the vendor.", DINNER),
    "r3-opt-for": _day2("This evening, opt for a traditional dinner with entertainment.", DINNER),
    "r3-free-to-find": _day2("Tonight, you’re free to find a local dinner.", DINNER),
    "r3-grab-tips": _day2("Your leader will share tips on great places to grab a local dinner.", DINNER),
    "r3-choose-your-dinner": _day2("Choose your dinner at various shops along the street.", DINNER),
    "r3-if-time-permits": _day2("We stop for lunch at the floating market if time permits."),
    "r3-those-who-wish": _day2("Those who wish can join the guide for lunch at a village home."),
    "r3-deluxe-option": _day2("Guests on the deluxe option have lunch at the palace hotel."),
    "r3-those-who-opt": _day2("Those who opt for this tour will return to the lodge for lunch."),
    "r3-inline-day-marker": (_raw_tour("Day 1: Arrive in Kathmandu.\nDay 2: Drive to Pokhara and walk the lakeside. "
                                       "Day 3: Boat on Phewa Lake (B, L).\nDay 3: Fly to Kathmandu.",
                                       duration="3 Days"),
                             _itin(3, {2: "Lunch is served by the lakeside in Pokhara."})),
    "r3-output-code-list-adds-dinner": _day2("Drive to Mai Chau.", "Walk the valley. Meals: B, L and D",
                                             meals="B, L"),
    "r3-output-pipe-list-adds-dinner": _day2("Drive to Mai Chau.", "Meals: B | L | D", meals="B, L"),
    "r3-second-meal-drinks-not-included": _day2("Board the junk.", "Breakfast is served at the hotel, and dinner is "
                                                "served on board (drinks not included).", meals="B"),
    "r3-second-meal-pearl-farm": _day2("Board the junk.", "Breakfast is served at the hotel and lunch at a pearl "
                                       "farm where jewellery is available for purchase.", meals="B"),
    "r3-second-meal-after-optional-kayak": _day2("Board the junk.", "Breakfast is served at the hotel, and after an "
                                                 "optional kayak, lunch is served on deck.", meals="B"),
    "r3-second-meal-meals-on-board": _day2("Board the junk.", "Breakfast is included, and meals on board the junk "
                                           "are prepared by the onboard chef.", meals="B"),
    "r3-second-meal-feast": _day2("Board the junk.", "Breakfast is served at the hotel, and a seafood feast awaits "
                                  "on board in the evening.", meals="B"),
    "r3-second-meal-bbq": _day2("Board the junk.", "Breakfast is included, and a beach BBQ is served on Titop "
                                "Island in the evening.", meals="B"),
    "r3-second-meal-picnic": _day2("Board the junk.", "Breakfast is included; a picnic is served on the beach at "
                                   "midday.", meals="B"),
    "r3-second-meal-brunch-buffet": _day2("Board the junk.", "Breakfast is served at the hotel, followed by a brunch "
                                          "buffet on board.", meals="B"),
    "r3-output-tomorrow": _day2("Board the junk.", "Breakfast is served at the hotel, and breakfast is provided "
                                "again as a boxed meal tomorrow before the transfer.", meals="B"),
    "r3-output-today-and-tomorrow": _day2("Board the junk.", "Breakfast is included today and tomorrow.", meals="B"),
    "r3-output-on-day-3": _day2("Board the junk.", "Breakfast is included, and a boxed breakfast is provided on "
                                "Day 3.", meals="B"),
    "r3-quoted-day-headers": (_raw_tour("Day 1 / Arrive in Nasu.\nDay 2 / Stage 1 (B)\nDay 3 / Stage 2 (B)\n"
                                        "Day 4 / Stage 3 (B)\n\"Day 5 / Stage 4 (B, D)\"\n\"Day 6 / Tour end\"",
                                        duration="6 DAYS"),
                              _itin(6, {4: "Dinner is served at the inn in Kaminoyama."})),
    "r3-extra-day-extension": (_tour([f"Stage {n}." for n in range(1, 9)] + [
        "Train to Ella. (B)\nExtra Day 01 Breakfast at the hotel. Picnic lunch will be provided. (B, L, D)"]),
        _itin(9, {9: "Dinner is served at the accommodation in Ella."})),
    "r3-grouped-header-drift": (_raw_tour("Day 1: Arrive in Colombo.\nDAY 2 & 3 - Ella: tea walks. Meals: B\n"
                                          "DAY 4 & 5 - Wellawaya: farm stay. Meals: B, D\nDay 6: Depart."),
                                _itin(7, {4: "Dinner is served at the guesthouse in Ella."})),
    "r3-grouped-block-split": (_raw_tour("Day 1: Arrive in Trincomalee.\nDAY 2 & 3 - Wilpattu. Meals: B, L, D\n"
                                         "Day 4: Depart. Meals: B"),
                               _itin(4, {2: "A packed lunch is provided at Anuradhapura."})),
    "r3-writer-split-a-day": (_tour(["Arrive.", "Stage 2. ( B )", "Stage 3. ( B )", "Sagada to Banaue. ( B )",
                                     "Banaue to Batad. ( B, D )", "Stage 6. ( B )", "Depart. ( B )"]),
                              _itin(8, {5: "Breakfast and dinner included."})),
    "r3-overnight-trip-no-markers": (_raw_tour("Kirifuri-Kogen Highlands → Kajikasou Inn (Overnight stay) → Ashio "
                                               "Copper Mine (B, D)", duration="OVERNIGHT TRIP",
                                               inclusions="Accommodation (1 night) with dinner and breakfast"),
                                     _itin(2, {1: "Dinner is served at the inn."})),
    "r3-tour-wide-count-after-last-day": (_tour(["Arrive in Hanoi. (D)"] + [f"Stage {n}. (B)" for n in range(2, 8)]
                                                + ["Transfer to the airport. (B)\n\nMeals included: 7 breakfasts, 3 "
                                                   "lunches, 2 dinners"]),
                                          _itin(8, {8: "Lunch is served at a riverside restaurant before the "
                                                       "transfer."})),
    # ── round 4 ──
    "r4-10-course-dinner": _day2("Walk the old town.", DINNER, with_list=False,
                                 inclusions="Hotels; daily breakfast; a 10-course dinner at a royal house in Hue"),
    "r4-welcome-4-course-dinner": _day2("Walk the old town.", DINNER, with_list=False,
                                        inclusions="Hotels; daily breakfast; welcome 4-course dinner"),
    "r4-3-hour-dinner-cruise": _day2("Walk the old town.", DINNER, with_list=False,
                                     inclusions="Hotels; daily breakfast; 3-hour dinner cruise on the Perfume River"),
    "r4-days-3-6-lunch": _day2("Walk the old town.", LUNCH, with_list=False,
                               inclusions="Hotels; daily breakfast; Days 3-6 lunch at trek camps"),
    "r4-count-in-kathmandu": _day2("Walk the old town.", BREAKFAST, with_list=False,
                                   inclusions="Hotels, 11 breakfasts in Kathmandu"),
    "r4-welcome-breakfasts": _day2("Walk the old town.", BREAKFAST, with_list=False,
                                   inclusions="11 welcome breakfasts"),
    "r4-count-on-trek-days": _day2("Walk the old town.", BREAKFAST, with_list=False,
                                   inclusions="11 breakfasts on trek days"),
    "r4-bring-a-picnic-lunch": _day2("The great idea is to bring a picnic lunch with you to enjoy at the top of the "
                                     "wall.", "A picnic lunch is served on top of the wall."),
    "r4-bring-your-own-packed-lunch": _day2("Please bring your own packed lunch and water.",
                                            "A packed lunch is served on the trail."),
    "r4-pack-your-own-lunch": _day2("Pack your own lunch for the trail.", "A packed lunch is served on the trail."),
    "r4-exclusions-meals-other-than-breakfast": _day2("Walk.", inclusions="Hotels; daily breakfast",
                                                      exclusions="Meals other than breakfast; tips"),
    "r4-exclusions-all-meals-except-breakfast": _day2("Walk.", inclusions="Hotels; daily breakfast",
                                                      exclusions="All meals except breakfast; tips"),
    "r4-daily-in-kathmandu": _day2("Walk.", DINNER, with_list=False,
                                   inclusions="Hotels; daily breakfast and dinner in Kathmandu"),
    "r4-daily-on-trekking-days": _day2("Walk.", LUNCH, with_list=False,
                                       inclusions="Hotels; breakfast and lunch daily on trekking days"),
    "r4-daily-on-the-cruise": _day2("Walk.", DINNER, with_list=False,
                                    inclusions="Hotels; lunch and dinner daily on the cruise"),
    "r4-comma-list-not-included": _day2("Lunch, dinner and drinks at the hotel restaurant are not included."),
    "r4-might-like-to-stop-for-lunch": _day2("You might like to stop for lunch at Sheroes Hangout, a vegetarian "
                                             "cafe."),
    "r4-may-wish-dinner": _day2("In the evening you may wish to have dinner at the night market.", DINNER),
    "r4-consider-dinner": _day2("Consider dinner at the famous night market.", DINNER),
    "r4-perhaps-dinner": _day2("Perhaps dinner at the famous night market.", DINNER),
    "r4-lunch-or-dinner": _day2("Cooking class at a local home, followed by lunch or dinner with the family.",
                                "Lunch and dinner are served with the family."),
    "r4-welcome-dinner-last-evening": _day2("Our welcome dinner takes place on the last evening of the tour.",
                                            "A welcome dinner is served in the evening."),
    "r4-farewell-dinner-end-of-tour": _day2("A farewell dinner is held at the end of the tour.",
                                            "A farewell dinner is served tonight."),
    "r4-following-morning-breakfast": _day2("Board the sleeper train to Lao Cai, arriving early the following "
                                            "morning for breakfast at a local restaurant.", BREAKFAST),
    "r4-next-morning-breakfast": _day2("Arrive in Lao Cai the next morning for breakfast.", BREAKFAST),
    "r4-last-nights-khantoke": _day2("After last night's khantoke dinner, today is spent at Doi Suthep.", DINNER),
    "r4-yesterdays-farewell-dinner": _day2("After yesterday's farewell dinner, transfer to the airport.", DINNER),
    "r4-previous-evenings-dinner": _day2("The previous evening's dinner was at the night market.", DINNER),
    "r4-midday-meal-and-evening-dinner": _day2("Trek to the camp.", "A midday meal and evening dinner are provided "
                                               "at the camp.", meals="B, D"),
    "r4-after-a-morning-barbecue": _day2("Speedboat to Phi Phi.", "After a morning of snorkelling, a beach barbecue "
                                         "is served on Phi Phi Don.", meals="B"),
    "r4-no-meals-except-picnic": _day2("Free day in Hanoi.", "No meals are included today apart from a picnic "
                                       "provided for the walk.", meals="None"),
    "r4-dotted-pm-time": _day2("Walk to the stilt house.", "Dinner is served at the stilt house at 7 p.m.",
                               meals="B, D"),
    "r4-spoken-time": _day2("Walk to the stilt house.", "Breakfast is served at six in the morning before the drive.",
                            meals="B, D"),
    "r4-seven-in-the-evening": _day2("Walk to the stilt house.", "Dinner is served at 7 in the evening.",
                                     meals="B, D"),
    "r4-output-meal-legend": _day2("Speedboat to Phi Phi.", "Meals: Breakfast (B), Lunch (L), Dinner (D)",
                                   meals="B"),
    "r4-own-private-balcony": _day2("Check in at the resort.", "Breakfast is included, and dinner is served on your "
                                    "own private balcony.", meals="B, L"),
    "r4-bracket-annotation-in-output-list": _day2("Walk to the homestay.", "Meals: B (hotel), D (homestay)",
                                                  meals="B"),
    "r4-evening-programme-meal": _day2("Walk to the homestay.", "Breakfast is included, and a traditional Thai meal "
                                       "is served during the evening programme.", meals="B"),
    "r4-body-line-another-day-number": (_raw_tour("Day 1: Meet in Tokyo. (B)\nDay 2: Ride to Aizu. (B)\nDay 3: "
                                                  "Ride on.\nDay 2 of cycling is the queen stage. (B, L, D)\nDay 4: "
                                                  "Break-up day. (B)", duration="4 days"),
                                        _itin(4, {3: "Lunch is served at a soba restaurant."})),
    "r4-restarted-day-numbering": (_raw_tour("Day 1: Arrive Hanoi.\nDay 2: Halong Bay cruise. (B, L, D)\nDay 3: "
                                             "Depart Hanoi.\nPOST-TOUR EXTENSION\nDay 1: Fly to Da Nang. (B, L)\n"
                                             "Day 2: Cooking class. (B, L, D)", duration="3 days"),
                                   _itin(3, {2: "Lunch is served on board the junk."})),
    "r4-alternative-day-later": (_raw_tour("Day 1: Arrive.\nDay 2: Temples. (B)\nDay 3: Kuang Si Falls. (B)\nDay 4: "
                                           "Depart.\nDay 3 (alternative in the rainy season): Cooking class. (B, L, D)",
                                           duration="4 days"),
                                 _itin(4, {3: LUNCH})),
    "r4-date-in-day-header": (_raw_tour("Day 1 - 12 Oct: Arrive.\nDay 2 - 13 Oct: Siem Reap. (B, L)\nDay 3 - 14 Oct: "
                                        "Angkor. (B, L)\nDay 4 - 15 Oct: Departure.", duration="4 days"),
                              _itin(4, {2: LUNCH})),
    "r4-output-body-line-other-day": (_tour(["Arrive.", "Trek to Jele Dzong. Meals: B, L, D", "Ridge walk. Meals: B",
                                             "Trek on.", "Depart."]),
                                      _itin(5, {3: "Walk the ridge.\nDay 2's packed lunch is provided on the trail "
                                                   "again."})),
    "r4-inclusion-day-1-2-prefix": _day2("Cross the Hillary bridge.", BREAKFAST, with_list=False,
                                         inclusions="Day 1-2: Hotel in Kathmandu with daily breakfast\nDay 3-5: "
                                                    "Teahouse accommodation on the trek"),
    "r4-inclusion-starting-on-day-2": _day2("Cross the Hillary bridge.", BREAKFAST, with_list=False,
                                            inclusions="Hotels, daily breakfast, starting on Day 2"),
    "r4-inclusion-from-second-morning": _day2("Cross the Hillary bridge.", BREAKFAST, with_list=False,
                                              inclusions="Hotels, daily breakfast from the second morning"),
    "r4-inclusion-from-day-2-onwards": _day2("Cross the Hillary bridge.", BREAKFAST, with_list=False,
                                             inclusions="Hotels, daily breakfast, from day 2 onwards"),
    # ── round 5 ──
    "r5-place-serves-meals-daily": _day2("Visit the Community Kitchen, serving thousands of free vegetarian meals "
                                         "daily to visitors.", "Dinner is included at the hotel.", with_list=False),
    "r5-however-own-expense": _day2("Lunch, however, is at your own expense."),
    "r5-while-own-expense": _day2("Lunch while in the Old Quarter is at your own expense."),
    "r5-but-own-expense": _day2("Lunch, but at your own expense."),
    "r5-abbreviation-period": _day2("Lunch at a local restaurant en route (approx. 1 hour), at own expense."),
    "r5-mt-abbreviation": _day2("Lunch at a restaurant near Mt. Fuji (own expense)."),
    "r5-not-included-in-price": _day2("Lunch stop of approx. 1 hour, not included in the tour price."),
    "r5-2-meals-daily-list": _day2("Walk.", LUNCH, with_list=False,
                                   inclusions="Accommodation; 2 meals daily (breakfast and dinner); guide"),
    "r5-daily-meals-colon": _day2("Walk.", LUNCH, with_list=False,
                                  inclusions="Accommodation; Daily meals: breakfast and dinner; guide"),
    "r5-half-board-one-other-meal": _day2("Walk.", DINNER, with_list=False,
                                          inclusions="Accommodation on half board (breakfast and one other meal)"),
    "r5-half-board-breakfast-and-lunch": _day2("Walk.", DINNER, with_list=False,
                                               inclusions="Accommodation in 3-star hotels on half board (breakfast "
                                                          "and lunch)"),
    "r5-2-meals-daily-no-list": _day2("Walk.", LUNCH, with_list=False, inclusions="Accommodation; 2 meals daily"),
    "r5-full-board-except-lunch": _day2("Walk.", LUNCH, with_list=False,
                                        inclusions="Full board - except lunch on day 3"),
    "r5-half-board-dinner-not-included": _day2("Walk.", DINNER, with_list=False,
                                               inclusions="Half board (dinner not included)"),
    "r5-own-expense-inside-code-list": _day2("Walk the lake.", meals="B, L (own expense), D"),
    "r5-lunch-n-a": _day2("Walk the lake.\nBreakfast: Hotel\nLunch: N/A\nDinner: N/A", with_list=False,
                          inclusions="All meals"),
    "r5-neither-nor": _day2("Neither lunch nor dinner is included today.", inclusions="All meals", with_list=False),
    "r5-tu-tuc": _day2("Lunch (tự túc).", inclusions="All meals", with_list=False),
    "r5-libre": _day2("Lunch: libre.", inclusions="All meals", with_list=False),
    "r5-for-your-account": _day2("Lunch is for your account.", inclusions="All meals", with_list=False),
    "r5-meal-after-time-marker": _day2("Drive to Mai Chau.", "After breakfast, drive to Mai Chau, where a meal is "
                                       "served in a stilt house.", inclusions="Daily breakfast", with_list=False),
    "r5-picnic-meal-evening": _day2("Camp on the sandbank.", "In the evening, a picnic meal is served around a "
                                    "campfire.", meals="B, L"),
    "r5-bare-meals-claim": _day2("Board the junk. Lunch on board.", "Meals are served on the sundeck.", meals="L"),
    "r5-output-placeholder-list": _day2("Visit the temple. Breakfast at hotel.", "Temples (B/-/D)",
                                        with_list=False),
    "r5-every-meal": _day2("Trek to camp.", "Every meal is prepared by the camp cook.", meals="L, D"),
    "r5-bare-hour": _day2("Fly to Pokhara.", "Dinner is served at 7 at a lakeside restaurant.", meals="B, D"),
    "r5-military-time": _day2("Fly to Pokhara.", "Breakfast is served at 0630 hrs before the flight.", meals="B, D"),
    "r5-h-time": _day2("Fly to Pokhara.", "Dinner is served at 19h at a lakeside restaurant.", meals="B, D"),
    "r5-lunchtime": _day2("Fly to Pokhara.", "Breakfast is served at the hotel, and at lunchtime the lakeside café "
                          "serves momos for the group.", meals="B"),
    "r5-forward-preview-line": (_raw_tour("Day 1: Arrive in Hanoi.\nDay 2: Halong Bay. Meals: B, L, D\nDay 3 begins "
                                          "with tai chi on the sundeck at dawn.\nDay 3: Halong Bay - Hanoi. Meals: B",
                                          duration="3 days"),
                                _itin(3, {2: "Dinner is served on board the junk."})),
    "r5-every-morning-of-the-tour": _day2("Visit Swayambhunath.", "Breakfast is included every morning of the tour.",
                                          meals="B"),
    "r5-daily-throughout": _day2("Visit Swayambhunath.", "Breakfast is served daily throughout the trip.",
                                 meals="B"),
    "r5-rest-of-the-trek": _day2("Visit Swayambhunath.", "For the rest of the trek, breakfast is served at "
                                 "teahouses.", meals="B"),
    "r5-two-days-later": _day2("Visit Swayambhunath.", "Two days later in Pokhara, breakfast is served at a garden "
                               "house.", meals="B"),
    "r5-both-days": _day2("Visit Swayambhunath.", "Breakfast and dinner are served on board on both days of the "
                          "cruise.", meals="B, D"),
    "r5-one-day-tour-version": (_raw_tour("Day 1: Floating market (L)\nDay 1 (alternative in the rainy season): Grand "
                                          "Palace (L)", duration="1 Day"),
                                "Day 1 — Floating market\nLunch is served at a riverside restaurant."),
    "r5-two-versions-of-a-day": (_raw_tour("Day 1: Arrive Paro.\nDay 2: Drive to Thimphu.\nDay 3 (Plan A): via Dochu "
                                           "La. (B)\nDay 3 (Plan B): by the valley road. (B, L)\nDay 4: Return.",
                                           duration="4 days"),
                                 _itin(4, {3: LUNCH})),
    "r5-same-header-twice": (_raw_tour("Day 1: Arrive.\nDay 2: Namche day\nHike.\nDay 2: Namche day\nMeals: B, L, D\n"
                                       "Day 3: Depart.", duration="3 days"),
                             _itin(3, {2: LUNCH})),
    "r5-2nd-evening": _day2("Dinner on the 2nd evening is a cooking class at a family home.", DINNER),
    "r5-night-2": _day2("Dinner will be a cooking class at a family home on night 2.", DINNER),
    "r5-2nd-night-dinner": _day2("Your 2nd-night dinner is a cooking class at a family home.", DINNER),
    "r5-second-night-dinner": _day2("Your second-night dinner is a cooking class at a family home.", DINNER),
}


def test_the_false_clear_set_covers_the_s246_review():
    # 65 confirmed in S246 (round 3: 23, round 4: 22, round 5: 20) plus the round 1 review's B1-B3, with variants.
    assert len(FALSE_CLEARS) >= 68


@pytest.mark.parametrize("case_id", sorted(FALSE_CLEARS))
def test_an_s246_false_clear_pattern_keeps_the_code(case_id):
    tour, itin = FALSE_CLEARS[case_id]
    assert _fires(tour, itin), case_id


@pytest.mark.parametrize("case_id", sorted(k for k in FALSE_CLEARS if k.startswith(("r1-bracket", "r3-at", "r3-opt",
                                                                                    "r4-might", "r5-however"))))
def test_the_same_tour_without_the_trap_does_clear(case_id):
    """Control: the tempting list really is structured evidence, so the trap is what keeps the code."""
    tour, itin = FALSE_CLEARS[case_id]
    clean = dict(tour, itineraries="\n".join(
        ("Day 2: Ninh Binh - Walk the old town. Meals: B, L, D" if ln.startswith("Day 2:") else ln)
        for ln in tour["itineraries"].splitlines()))
    assert not _fires(clean, itin), case_id


# ── 5. building blocks ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sentence,claimed", [
    ("Lunch is included.", {"L"}), ("Breakfast and dinner are served.", {"B", "D"}),
    ("A picnic lunch is provided.", {"L"}), ("BBQ dinner is served.", {"D"}),
    ("Brunch is served on deck.", {"B", "L"}), ("Day 3 — Hue (B/-/D)", {"B", "D"}),
    ("Meals: B, L", {"B", "L"}), ("All meals are included.", {"B", "L", "D"}),
    ("A feast is hosted by the village.", {"B", "L", "D"}),
    ("Meals: B (hotel), D (homestay)", {"B", "L", "D"}),
])
def test_claimed_meals(sentence, claimed):
    assert mc.claimed_meals(sentence) == frozenset(claimed)


@pytest.mark.parametrize("block,lists,impure", [
    ("Walk. Meals: B, L, D", [{"B", "L", "D"}], False),
    ("Walk (B/-/D)", [{"B", "D"}], False),
    ("Walk. Meals: None", [set()], False),
    ("Walk. Meals: B, L (own expense), D", [], True),
    ("Walk. Meals included: 7 breakfasts", [], True),
    ("Walk. Meals: Breakfast at the hotel", [], True),
    ("Walk.\nMeals Included:\n• Breakfast, Lunch", [{"B", "L"}], False),
    ("Walk.\nMeals Included:\n• 3 meals", [], True),
    ("Walk (lunch)", [], False),
])
def test_day_lists(block, lists, impure):
    got, bad = mc.day_lists(block)
    assert [set(x) for x in got] == lists and bad is impure


def test_a_sentence_with_an_unknown_day_is_not_cleared():
    tour = _tour(LIST_DAYS)
    itin = "Lunch is served at a local restaurant.\n\n" + _itin(4, {})
    assert mc.claim_sourced(_claim(LUNCH, None), tour, itin) == (False, "day_unknown")


# ── 6. guards added while building the narrow rule (own probes, S247) ─────────────────────────────────────

S247_PROBES = {
    "responsibility": _day2("Lunch at a local restaurant is the client's responsibility."),
    "cash-only": _day2("Lunch stop at the market (cash only)."),
    "on-you": _day2("Lunch is on you today."),
    "travellers-arrange-themselves": _day2("Lunch, which travellers arrange themselves."),
    "post-tour-extension": _day2("Walk the citadel.\nPost-tour: Hoi An lantern walk (B, L, D)", meals="B"),
    "inclusion-bracket-continuation": _day2("Walk.", LUNCH, inclusions="All meals\n(on trek days)", with_list=False),
    "inclusion-only-elsewhere": _day2("Walk.", LUNCH, inclusions="All meals\nTrekking days only", with_list=False),
    "inclusion-except-elsewhere": _day2("Walk.", LUNCH, inclusions="Full board\nexcept lunch on day 3",
                                        with_list=False),
    "inclusion-other-item-names-the-meal": _day2("Walk.", LUNCH, inclusions="Full board\nPacked lunches on the trek",
                                                 with_list=False),
}


@pytest.mark.parametrize("sentence", ["Breakfast is included on the second day.", "Dinner is served the day after.",
                                      "Lunch is provided for the whole tour.", "Breakfast is served on all trek days.",
                                      "On the final evening, dinner is served at the hotel."])
def test_an_output_sentence_about_another_day_is_refused(sentence):
    assert mc.output_refused(sentence)


@pytest.mark.parametrize("sentence", ["The day ends with dinner served at the hotel.", "All meals are included.",
                                      "A picnic lunch is provided for the midday rest on the trek."])
def test_a_same_day_sentence_is_not_refused(sentence):
    assert not mc.output_refused(sentence)


@pytest.mark.parametrize("case_id", sorted(S247_PROBES))
def test_an_s247_probe_keeps_the_code(case_id):
    tour, itin = S247_PROBES[case_id]
    assert _fires(tour, itin), case_id


def test_a_day_the_writer_moved_is_not_matched_by_number_alone():
    """Same day count, but the writer merged two days and split another: output Day 2 is the source's Day 3."""
    tour = _tour(["Arrive.", "Temple of Literature. Meals: B, L", "Halong junk cruise. Meals: B, L, D", "Depart."])
    itin = _itin(4, {2: "Lunch is served on board the junk."}, titles={2: "Halong Bay junk cruise"})
    assert _fires(tour, itin)
    assert not _fires(tour, _itin(4, {2: "Lunch is served near the Temple of Literature."}))
    assert mc.days_aligned("Day 3 — Halong Bay\nCruise on the junk.", "Halong junk cruise. Meals: B, L, D")


def test_a_meal_doubt_in_the_description_stops_both_paths():
    tour = _tour(LIST_DAYS, inclusions="All meals")
    tour["description"] = "Lunches on the free days are at your own expense."
    assert _fires(tour, _itin(4, {2: LUNCH}))
    tour["description"] = "A classic loop of northern Vietnam."
    assert not _fires(tour, _itin(4, {2: LUNCH}))


# ── 7. second own probe round (S247): free text beside a list, INCLUSIONS layout ──────────────────────────

S247_PROBES_2 = {
    "organised-by-participants": _day2("Lunch to be organised by participants."),
    "not-included-abbreviation": _day2("Lunch n.i."),
    "children-only": _day2("Lunch for children under 12 is included."),
    "full-board-dash-scope": _day2("Walk.", LUNCH, inclusions="Full board – trekking portion", with_list=False),
    "plain-line-heading": _day2("Walk.", LUNCH, inclusions="Trek portion\nAll meals", with_list=False),
    "colon-heading": _day2("Walk.", BREAKFAST, inclusions="Kathmandu: daily breakfast", with_list=False),
    "indented-sub-list": _day2("Walk.", LUNCH, inclusions="- Trek\n  - All meals\n- Guide", with_list=False),
    "narrative-names-the-meal": _day2("Lunch at a riverside restaurant."),
    "narrative-names-meals-generically": _day2("The cook prepares meals for the group."),
    "qualified-meals-label": (_tour(["Arrive.", "Walk Hoan Kiem. Group B meals: B, L", "Cruise.", "Depart."]),
                              _itin(4, {2: LUNCH})),
    "lowercase-code-is-not-a-code": (_tour(["Arrive.", "Walk Hoan Kiem (l)", "Cruise.", "Depart."]),
                                     _itin(4, {2: LUNCH})),
    "description-scopes-the-inclusion": (dict(_tour(["Arrive.", "Walk Hoan Kiem.", "Cruise.", "Depart."],
                                                    inclusions="All meals"),
                                              description="Meals are provided on trek days."), _itin(4, {2: LUNCH})),
    "bullet-continuation": _day2("Walk.", LUNCH, inclusions="- All meals\n- (trek days)", with_list=False),
}


@pytest.mark.parametrize("case_id", sorted(S247_PROBES_2))
def test_an_s247_second_round_probe_keeps_the_code(case_id):
    tour, itin = S247_PROBES_2[case_id]
    assert _fires(tour, itin), case_id


def test_a_time_marker_is_not_a_narrative_mention():
    tour, itin = _day2("After breakfast, walk around Hoan Kiem Lake.", LUNCH)
    assert not _fires(tour, itin)


# ── 8. independent review of part B (S247): doubt in another day's block, a list scoped by the text above it ───

def _hue_itin(n_days=3):
    """Output days 1..n_days; Day 2 (Hue Citadel) claims lunch, as in the review probes."""
    titles = {1: "Hanoi", 2: "Hue Citadel", 3: "Hoi An", 4: "Saigon"}
    return "\n\n".join(f"Day {d} — {titles[d]}\n" + (LUNCH if d == 2 else "Time to look around.")
                       for d in range(1, n_days + 1))


REVIEW_PROBES = {
    # Finding 1: the note sits in another day's block (Jev's unit_source holds the Day 1 text, so main refused it).
    "note-in-day-1": "Day 1: Hanoi - Arrive. Note: lunches are at your own expense throughout the trip.\n"
                     "Day 2: Hue - Citadel. Meals: B, L, D\nDay 3: Hoi An - Old town.",
    "note-after-the-last-day": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel. Meals: B, L, D\n"
                               "Day 3: Hoi An - Old town. Meals: B\n"
                               "Important: on private departures lunch is not included.",
    # Finding 2: the qualifier is on the line or in the sentence before the list.
    "heading-line-above-the-list": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel.\nSuperior cabin guests:\n"
                                   "Meals: B, L, D\nDay 3: Hoi An - Old town.",
    "option-sentence-before-the-list": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel.\n"
                                       "Option 1 - bike tour. Meals: B, L\nOption 2 - free day.\n"
                                       "Day 3: Hoi An - Old town.",
    "option-heading-on-the-list-line": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel.\n"
                                       "Option A: kayak in Lan Ha. Meals: B, L, D\nOption B: stay in Hanoi.\n"
                                       "Day 3: Hoi An - Old town.",
    # Own variants of the same two classes (no variant word, so the narrower guards are what stops them).
    "group-heading": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel.\nGroup A:\nMeals: B, L, D\n"
                     "Day 3: Hoi An - Old town.",
    "plain-heading": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel.\nFor the long route:\nWalk the walls.\n"
                     "Meals: B, L, D\nDay 3: Hoi An - Old town.",
    "audience-line-above": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel.\nGuests on the long trek.\n"
                           "Meals: B, L, D\nDay 3: Hoi An - Old town.",
    "audience-before-on-the-line": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel. Cabin class passengers. "
                                   "Meals: B, L, D\nDay 3: Hoi An - Old town.",
    "audience-before-a-code-bracket": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel.\nChildren under 12\n"
                                      "Citadel walk (B, L, D)\nDay 3: Hoi An - Old town.",
    "variant-word-in-the-day": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel. Premium package. Meals: B, L, D\n"
                               "Day 3: Hoi An - Old town.",
    "other-day-names-this-day": "Day 1: Hanoi - Arrive. Please note lunch on Day 2 may be a packed lunch.\n"
                                "Day 2: Hue - Citadel. Meals: B, L, D\nDay 3: Hoi An - Old town.",
    "other-day-following-days": "Day 1: Hanoi - Arrive. Lunch is not included today or on the following days.\n"
                                "Day 2: Hue - Citadel. Meals: B, L, D\nDay 3: Hoi An - Old town.",
    "other-day-plural-remark": "Day 1: Hanoi - Arrive. Lunches can be bought at the stalls.\n"
                               "Day 2: Hue - Citadel. Meals: B, L, D\nDay 3: Hoi An - Old town.",
    "other-day-departures": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel. Meals: B, L, D\n"
                            "Day 3: Hoi An - Old town. On group departures, lunch may be replaced by a snack.",
    "other-day-all-meals-own": "Day 1: Hanoi - Arrive. All meals other than breakfast are at your own expense.\n"
                               "Day 2: Hue - Citadel. Meals: B, L, D\nDay 3: Hoi An - Old town.",
    "other-day-strong-cue": "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel. Meals: B, L, D\n"
                            "Day 3: Hoi An - Old town. Lunch is at your own cost.",
}


# The guard each probe must be stopped by (so a later change cannot pass it through another, looser guard).
REVIEW_REASON = {
    "note-in-day-1": "source_itinerary_doubt", "note-after-the-last-day": "source_itinerary_doubt",
    "heading-line-above-the-list": "source_day_has_variants", "option-sentence-before-the-list":
    "source_day_has_variants", "option-heading-on-the-list-line": "source_day_has_variants",
    "group-heading": "source_list_unreadable", "plain-heading": "source_list_unreadable",
    "audience-line-above": "source_list_unreadable", "audience-before-on-the-line": "source_list_unreadable",
    "audience-before-a-code-bracket": "source_list_unreadable", "variant-word-in-the-day": "source_day_has_variants",
    "other-day-names-this-day": "source_itinerary_doubt", "other-day-following-days": "source_itinerary_doubt",
    "other-day-plural-remark": "source_itinerary_doubt", "other-day-departures": "source_itinerary_doubt",
    "other-day-all-meals-own": "source_itinerary_doubt", "other-day-strong-cue": "source_itinerary_doubt",
}


def test_every_review_probe_names_its_guard():
    assert set(REVIEW_REASON) == set(REVIEW_PROBES)


@pytest.mark.parametrize("case_id", sorted(REVIEW_PROBES))
def test_a_review_probe_keeps_the_code(case_id):
    tour = _raw_tour(REVIEW_PROBES[case_id])
    assert mc.claim_sourced(_claim(LUNCH, 2), tour, _hue_itin()) == (False, REVIEW_REASON[case_id]), case_id
    assert _fires(tour, _hue_itin()), case_id


def test_the_scoped_list_guards_hold_without_the_variant_check():
    """The heading / previous-line probes of the review also stop at day_lists alone (the variant check in
    claim_sourced is a second, block-wide guard)."""
    for case_id in ("heading-line-above-the-list", "option-sentence-before-the-list"):
        block = mc.source_day_blocks(REVIEW_PROBES[case_id])[2]
        assert mc.day_lists(block)[1] is True, case_id


@pytest.mark.parametrize("itineraries", [
    # control: the same tour with no trap clears
    "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel. Meals: B, L, D\nDay 3: Hoi An - Old town.",
    # another day's own list is that day's fact, not a remark about Day 2
    "Day 1: Hanoi - Arrive. Meals: None\nDay 2: Hue - Citadel. Meals: B, L, D\nDay 3: Hoi An - Old town.",
    # the plain section headings of the structured layouts do not scope the list; narrative audience words far
    # from the list do not either
    "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel. Check into your cabin.\n\nAccommodation:\n• Hotel (1 night)\n"
    "Included transportation and activities:\n• Support vehicle\nMeals Included:\n• Breakfast, Lunch\n"
    "Day 3: Hoi An - Old town.",
])
def test_the_review_guards_leave_unscoped_lists_readable(itineraries):
    tour = _raw_tour(itineraries)
    assert mc.claim_sourced(_claim(LUNCH, 2), tour, _hue_itin()) == (True, "day_list")


@pytest.mark.parametrize("block,impure", [
    ("Citadel.\nSuperior cabin guests:\nMeals: B, L, D", True),
    ("Citadel.\nGroup A:\nMeals: B, L, D", True),
    ("Citadel.\nGuests on the long trek.\nMeals: B, L, D", True),
    ("Citadel. Option 1 - bike tour. Meals: B, L", True),
    ("Citadel.\nNote:\n(B, L)", True),
    ("Citadel.\nAccommodation:\n• Hotel\nMeals Included:\n• Breakfast", False),
    ("Citadel. Check into your cabin.\nWalk the walls.\nMeals: B, L", True),
    ("Citadel.\nFor trekkers on the long route\n• Walk 10 km\nMeals: B, L, D", True),
    ("Citadel. Check into your cabin.\n\nWalk the walls.\nMeals: B, L", False),
])
def test_day_lists_scoped_by_the_text_above(block, impure):
    assert mc.day_lists(block)[1] is impure


def test_an_inclusion_is_not_read_on_a_day_with_variants():
    tour = _tour(["Arrive.", "Hue citadel. Option A: kayak. Option B: stay in town.", "Cruise.", "Depart."],
                 inclusions="All meals")
    assert _fires(tour, _itin(4, {2: LUNCH}, titles={2: "Hue citadel"}))
    tour = _tour(["Arrive.", "Hue citadel walk.", "Cruise.", "Depart."], inclusions="All meals")
    assert not _fires(tour, _itin(4, {2: LUNCH}, titles={2: "Hue citadel"}))


def test_a_tour_wide_note_in_another_day_stops_the_inclusion_path():
    tour = _tour(["Arrive. Note: lunches are at your own expense throughout the trip.", "Hue citadel walk.",
                  "Cruise.", "Depart."], inclusions="All meals")
    assert _fires(tour, _itin(4, {2: LUNCH}, titles={2: "Hue citadel"}))


# ── 9. second independent review of part B (S247, at f1ea929c) ─────────────────────────────────────────────
# Finding 1: the day-list path never read INCLUSIONS (Jev's unit_source does). Finding 2: a neighbouring day's
# sentence about the meal with a weak cue and no tour-wide / remark marker passed (Jev sees days d-1..d+1).
# Finding 3: a claim sentence that pairs a sourced meal with something else ("..., followed by a hotpot") or adds
# an invented detail ("a Michelin-starred restaurant") was cleared whole without Jev.

HUE3 = "Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel. Meals: B, L, D\nDay 3: Hoi An - Old town."


def _hue5(note_day=None, note=""):
    """A 5-day source; Day 2 (Hue) has ``Meals: B, L, D``; ``note`` is appended to day ``note_day``
    (``note_day=6``: after the last day)."""
    days = {1: "Hanoi - Arrive.", 2: "Hue - Citadel. Meals: B, L, D", 3: "Hoi An - Old town.",
            4: "Saigon - Markets.", 5: "Mekong Delta - Boats."}
    lines = [f"Day {n}: {t}" + (f" {note}" if n == note_day else "") for n, t in days.items()]
    if note_day == 6:
        lines.append(note)
    return "\n".join(lines)


def _hue_itin_with(sentence, n_days=3):
    """Output days 1..n_days; Day 2 (Hue Citadel) holds ``sentence`` (as its title line when it is one)."""
    titles = {1: "Hanoi", 2: "Hue Citadel", 3: "Hoi An", 4: "Saigon", 5: "Mekong Delta"}
    title_claim = sentence.startswith("Day 2 ")
    return "\n\n".join((sentence if d == 2 and title_claim else f"Day {d} — {titles[d]}") + "\n"
                       + (sentence if d == 2 and not title_claim else "Time to look around.")
                       for d in range(1, n_days + 1))


INCLUSION_PROBES = {
    "combined-incl-excl-field": "Included: hotels, guide, meals as per itinerary\nNot included: lunches, drinks, tips",
    "as-per-itinerary-except": "Meals as per itinerary, except lunch on Day 2 which is at own expense",
    "payable-on-board": "Meals as indicated (B/L/D). Lunch on the cruise is payable on board.",
    "excluded-item-in-a-list": ["Hotels", "Guide", "EXCLUDED: lunch on cruise days"],
    "cabin-scoped-meals": "Meals as listed in the itinerary for Superior cabins; Standard cabin guests receive "
                          "breakfast only",
    # own variants: a "Not included:" heading with the meal on its own line, a weak cue, an audience word
    "not-included-heading-own-line": "Included:\n- Hotels\n- Meals as per itinerary\nNot included:\n- Lunches\n- Tips",
    "weak-cue-in-inclusions": "Meals as per itinerary; lunch may be a packed lunch",
    "audience-in-inclusions": "Meals as per itinerary for trekkers",
}


@pytest.mark.parametrize("case_id", sorted(INCLUSION_PROBES))
def test_inclusions_doubt_stops_the_day_list_path(case_id):
    tour = _raw_tour(HUE3, inclusions=INCLUSION_PROBES[case_id])
    cleared, reason = mc.claim_sourced(_claim(LUNCH, 2), tour, _hue_itin())
    assert not cleared and reason in ("excluded", "inclusions_doubt"), (case_id, reason)
    assert _fires(tour, _hue_itin()), case_id


@pytest.mark.parametrize("inclusions", ["", "Included: hotels, guide, meals as per itinerary",
                                        "Hotels, guide, entrance fees, meals as indicated (B/L/D)",
                                        "Not included: drinks, tips"])
def test_plain_inclusions_leave_the_day_list_readable(inclusions):
    tour = _raw_tour(HUE3, inclusions=inclusions)
    assert mc.claim_sourced(_claim(LUNCH, 2), tour, _hue_itin()) == (True, "day_list")


def test_inclusions_excluded_section_reads_like_exclusions():
    assert mc.inclusions_excluded("Included: guide\nNot included: lunches, drinks") == frozenset({"L"})
    assert mc.inclusions_excluded("Included: guide\nExcluded:\n- Meals\n- Tips") == mc.ALL
    assert mc.inclusions_excluded("Included: guide, all meals") == frozenset()


NEIGHBOUR_PROBES = {
    # the review's probes, 3-day tour, claim on Day 2 (Day 1 and the text after Day 3 are both in Jev's window)
    "d1-charged-separately": (1, "Lunch on the junk is charged separately."),
    "d1-guests-pay": (1, "Guests pay for lunch on the junk."),
    "d1-self-funded": (1, "Lunch on the cruise is self-funded."),
    "after-low-season-not-served": (4, "In the low season (May to September) lunch is not served on board."),
    "after-no-lunch-during-tet": (4, "No lunch on the cruise during Tet."),
    "after-private-tours-extra": (4, "On private tours, lunch is extra."),
    "after-solo-travellers-extra": (4, "For solo travellers lunch is extra."),
    "after-not-available-sundays": (4, "Lunch on the junk is not available on Sundays."),
    # own variants: a neighbour that only talks about the meal, with no cue at all, or restricts another meal
    "d3-plain-mention": (3, "Lunch is at a street stall in Hoi An."),
    "d1-meals-generic": (1, "Meals on the train are simple."),
    "d3-replaced": (3, "Lunch may be replaced by a snack box."),
    "d1-only-breakfast": (1, "Only breakfast is provided in the towns."),
    "d3-a-la-carte": (3, "Lunch on the boat is a la carte."),
    "d1-cancelled": (1, "Lunch on the junk is cancelled in bad weather."),
}


def _neighbour_tour(where, note):
    lines = HUE3.split("\n")
    if where == 4:
        lines.append(note)
    else:
        lines[where - 1] += f" {note}"
    return _raw_tour("\n".join(lines))


@pytest.mark.parametrize("case_id", sorted(NEIGHBOUR_PROBES))
def test_a_neighbouring_day_that_talks_about_the_meal_stops_the_clear(case_id):
    tour = _neighbour_tour(*NEIGHBOUR_PROBES[case_id])
    assert mc.claim_sourced(_claim(LUNCH, 2), tour, _hue_itin()) == (False, "source_itinerary_doubt"), case_id
    assert _fires(tour, _hue_itin()), case_id


@pytest.mark.parametrize("day1", ["Meal plan: full board on trek days, breakfast only in towns.",
                                  "Only breakfast is provided in the towns."])
def test_a_neighbouring_day_stops_the_inclusion_path(day1):
    tour = _tour([f"Arrive. {day1}", "Hue citadel walk.", "Cruise.", "Depart."], inclusions="Full board")
    assert _fires(tour, _itin(4, {2: LUNCH}, titles={2: "Hue citadel"}))
    tour = _tour(["Arrive.", "Hue citadel walk.", "Cruise.", "Depart."], inclusions="Full board")
    assert not _fires(tour, _itin(4, {2: LUNCH}, titles={2: "Hue citadel"}))


@pytest.mark.parametrize("note", ["There are no meals included today.", "Lunch can be bought on the train.",
                                  "A riverside picnic and an optional short hike end the day.",
                                  "Lunch is at a street stall."])
def test_a_day_further_away_keeps_the_reach_gate(note):
    """Days outside Jev's window (d-1..d+1) keep the reach gate: a sentence scoped to its own day does not reach
    the claim; the same sentence next to the claimed day does stop it."""
    assert mc.claim_sourced(_claim(LUNCH, 2), _raw_tour(_hue5(4, note)), _hue_itin_with(LUNCH, 5)) == \
        (True, "day_list")
    assert mc.claim_sourced(_claim(LUNCH, 2), _raw_tour(_hue5(3, note)), _hue_itin_with(LUNCH, 5)) == \
        (False, "source_itinerary_doubt")


@pytest.mark.parametrize("note", ["Note: lunch on the cruise is replaced by a snack.",
                                  "Note: lunch is cancelled during Tet.", "Please note lunch is served à la carte.",
                                  "Important: lunch is charged on board.", "Note: lunch only on trek days.",
                                  "Note: lunch is not available on Sundays."])
def test_new_negation_cues_reach_from_a_day_further_away(note):
    for where in (5, 6):
        tour = _raw_tour(_hue5(where, note))
        assert mc.claim_sourced(_claim(LUNCH, 2), tour, _hue_itin_with(LUNCH, 5)) == \
            (False, "source_itinerary_doubt"), (note, where)


EXTRA_PROBES = {
    # Day 2 says "Meals: B": the sentence implies an unsourced dinner beside the sourced breakfast
    "tasting-menu-on-deck": ("B", "Breakfast is included, and a tasting menu awaits on deck at night."),
    "followed-by-hotpot": ("B", "Breakfast is included, followed by a hotpot on deck at sunset."),
    "seafood-spread-at-sunset": ("B", "Breakfast is served at the hotel; at sunset a seafood spread is laid out on "
                                      "deck."),
    # Day 2 says "Meals: B, L, D": the meal is sourced, the detail is invented
    "michelin-starred": ("B, L, D", "Dinner is served at a Michelin-starred restaurant in town."),
    "wine-pairing": ("B, L, D", "Lunch with a wine pairing is served on the sundeck."),
    "private-chef": ("B, L, D", "Dinner is prepared by a private chef at the villa."),
    # own variants
    "and-a-cooking-class": ("B, L, D", "Lunch is included and a cooking class follows."),
    "noodle-shop": ("B, L, D", "Lunch is served at a noodle shop."),
    "sundeck-venue": ("B, L, D", "Dinner is served on the sundeck."),
    "title-with-a-food-noun": ("B, L, D", "Day 2 — Hue Street Food Night (B, L, D)"),
}


def _extra_tour(meals):
    return _raw_tour(f"Day 1: Hanoi - Arrive.\nDay 2: Hue - Citadel. Meals: {meals}\nDay 3: Hoi An - Old town.")


@pytest.mark.parametrize("case_id", sorted(EXTRA_PROBES))
def test_a_claim_sentence_with_more_than_the_claim_goes_to_jev(case_id):
    meals, sentence = EXTRA_PROBES[case_id]
    tour = _extra_tour(meals)
    itin = _hue_itin_with(sentence)
    cleared, reason = mc.claim_sourced(_claim(sentence, 2), tour, itin)
    assert not cleared, case_id
    assert reason.startswith("output_"), (case_id, reason)
    assert _fires(tour, itin), case_id


@pytest.mark.parametrize("meals,sentence", [
    ("B, L, D", "Lunch is served at a local restaurant."),
    ("B, L, D", "Lunch is included."),
    ("B, L, D", "Breakfast, lunch and dinner are included."),
    ("B, L, D", "Dinner is served at the hotel in Hue."),
    ("B, L, D", "Lunch is served near the Citadel."),
    ("B", "Breakfast is served at the hotel."),
    ("B, L, D", "Day 2 — Hue Citadel (B, L, D)"),
])
def test_a_bare_meal_claim_still_clears(meals, sentence):
    tour = _extra_tour(meals)
    assert mc.claim_sourced(_claim(sentence, 2), tour, _hue_itin_with(sentence)) == (True, "day_list")


def test_output_extra_reads_words_against_the_source_day():
    assert mc.output_extra("Dinner is served on the sundeck.", "Cruise. Relax on the sundeck. Meals: B, L, D") is None
    assert mc.output_extra("Dinner is served on the sundeck.", "Cruise. Meals: B, L, D") == "word_not_in_source_day"
    assert mc.output_extra("Breakfast is included, and lunch.", "Meals: B, L") == "second_clause"
    assert mc.output_extra("A hotpot dinner is served.", "Hotpot night. Meals: D") == "food_noun"
