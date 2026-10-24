"""AA-645 (S249) — the flag_fix day loss of the S249 shadow <=50 (India "Rajasthan Adventure": 15 source days, 10 on
the page, publishable at 9.75).

1. A meal fact line the writer v2 copied from the same source day's meal codes is source-backed: no
   ITINERARY_MEAL_TIME_INVENTED, Jev not asked. Real shadow cases (source text and page lines verbatim from
   wave50-packet.json); a meal or word the source day does not give is still flagged.
2. Repair-contract invariant ``days_kept``: a repair that drops, adds or renumbers a "Day N" block is reverted.
3. ITINERARY_DAY_COUNT_MISMATCH blocks Master on an A1 writer-v2 draft only (go/no-go option 1a); v1 and T2 unchanged.
Mock-only: no AWS, DB or model call.
"""
from unittest.mock import patch

import pytest

from services.content_generation import brand_audit_node as ba
from services.quality import repair_contract as rc
from shared.llm_client.decide import Verdict

MEAL = "ITINERARY_MEAL_TIME_INVENTED"

# ── real shadow sources (trimmed to the days used) ───────────────────────────────────────────────

NORTHERN_HEIGHTS = (  # Thailand, 12 days: "(B, L)" on the line under the marker
    "DAY 01: \nChiang Mai (B, L)\nWe meet at the Hotel after Breakfast to brief everyone thoroughly and make sure the "
    "bikes fit. Then, we take a short transfer out of town and start a nice, easy ride along the Ping River.\n"
    "Ride: 40 km (about 25 miles)\nOvernight in Chiang Mai\n\nDAY 02: \nThe Teak Forest Ride (B, L) \nAfter breakfast "
    "we transfer to the nearby reservoir where we take a boat to our trailhead.\n")
KHUVSGUL = (  # Mongolia: "(L+D)" codes and a "Day 2-3" range
    "Day 1: Flight to Moron\nIn the morning, meet with the local guide and take a flight to the town of Moron. Upon "
    "arrival, have lunch at the local restaurant and drive 120km towards our camp located by the lake shore.\n\n"
    "Overnight in tourist camp. (L+D)\nDay 2-3: Khuvsgul Lake\nEnjoy leisure days by the lake.\nOvernight in tourist "
    "camp. (B+L+D)\nDay 4: Return to Ulaanbaatar \nAfter breakfast at the camp, drive to the town of Moron. (B)")
SIKKIM = (  # India: a tabular "Meals  L D" line, no colon
    "Day 1   Meet at the group hotel in Delhi. Transfers from Delhi Airport are provided.\nYour holiday starts at the "
    "hotel in Delhi. Lunch and an afternoon at leisure at the hotel. Your guide will take dinner with you.\n\n"
    "Accommodation   Hotel\nMeals  L D\nDay 2   Fly to Bagdogra and drive to Darjeeling (2500m).\nAfter breakfast, "
    "we drive to the domestic terminal of Delhi Airport.\n")
CHINA_EXPRESS = (  # China: "Meals Included:" with the list as a bullet underneath
    "Day 1 Beijing Arrival\nNi Hao! Welcome to Beijing. Your guide will take you on an orientation walk.\n\n"
    "Accommodation:\n•A city hotel or similar (3 nights)\nMeals Included:\n• There are no meals "
    "included today\nDay 2 Beijing Tiananmen Square, Forbidden City and Hutongs\nThere will be a lot of walking "
    "today.\nMeals Included:\n• Breakfast\n")
RAJASTHAN_11_13 = (  # India "Rajasthan Adventure", the three days with a meal line among the five that were lost
    "Day 11      Ranthambore\nMeals included: Dinner\nDay 12      Ranthambore\nMorning and afternoon safaris.\n"
    "Meals included: Breakfast | Dinner\nDay 13      Agra\nMeals included: Breakfast\n")
PADDLE_AND_SURF = (  # Philippines: day 9 codes "( B )", dinner only in the prose
    "Day 08\nPAGUDPUD\nSurfing activity. ( B )\n\nDay 09\nPAGUDPUD / LAOAG / MANILA\nVisit of Cape Bojeador "
    "Lighthouse. Tour of Laoag. Leave Laoag for Manila. Transfer to hotel. Dinner and overnight stay at City Hotel. "
    ". .( B )\n")
RELIGIOUS_SOUTH_INDIA = (  # India: no meal list, "After breakfast" prose only
    "Day 9: Madurai → Trivandrum\nAfter breakfast, travel to Trivandrum, the capital of Kerala. Upon arrival, check "
    "in and relax. Overnight stay in Trivandrum.\n")


def _tour(itin):
    return {"name": "T", "itineraries": itin}


def _gen(itin):
    return {"name": "A Tour", "itineraries": itin}


def _grey(units, sources, *, tenant_id=None):
    return {i: Verdict("a1_claim_supported", "enforce", "grey", probability=0.5) for i in sources}


def _codes(itin, source, **kw):
    """The pre-audit codes with Jev answering grey: only a source-backed meal line can clear the code."""
    with patch("services.content_generation.grounding.judge_units", side_effect=_grey) as jev:
        codes = ba.pre_audit_checks(_gen(itin), _tour(source), **kw)
    return codes, jev.call_count


# ── 1. source-backed meal lines ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source,itin", [
    (NORTHERN_HEIGHTS,
     "Day 1 — Chiang Mai and the Ping River to Ban Tawai\nThe group meets at the hotel after breakfast for a briefing. "
     "A short transfer out of town leads to an easy ride along the Ping River.\nRide: 40 km (about 25 miles). "
     "Overnight: Chiang Mai. Meals: breakfast, lunch.\n\nDay 2 — Teak forest ride to Chiang Dao\nAfter breakfast a "
     "transfer leads to a nearby reservoir. Meals: breakfast, lunch."),
    (KHUVSGUL,
     "Day 1 — Flight to Moron and drive to Khuvsgul Lake\nIn the morning the group takes a flight to Moron. Distance: "
     "120 km. Overnight: tourist camp. Meals: lunch, dinner.\n\nDay 2 — Leisure day at Khuvsgul Lake\nThe day is "
     "spent by the lake. Overnight: tourist camp. Meals: breakfast, lunch, dinner.\n\nDay 3 — Second leisure day\nA "
     "second leisure day by the lake. Meals: breakfast, lunch, dinner.\n\nDay 4 — Return to Ulaanbaatar\nAfter "
     "breakfast the group drives to Moron. Meals: breakfast."),
    (SIKKIM,
     "Day 1 — Arrival at the group hotel in Delhi\nThe group meets at the hotel in Delhi, with transfers provided "
     "from Delhi Airport.\nOvernight: hotel in Delhi. Meals: lunch, dinner.\n\nDay 2 — Flight to Bagdogra\nAfter "
     "breakfast the group drives to the airport."),
    (CHINA_EXPRESS,
     "Day 1 — Beijing arrival and orientation walk\nThe local guide leads an orientation walk.\nOvernight: a city "
     "hotel or similar. Meals: none included.\n\nDay 2 — Tiananmen Square\nThe day involves a lot of walking. "
     "Meals: breakfast."),
    (RAJASTHAN_11_13,
     "Day 11 — Ranthambore\nThe group reaches Ranthambore. Meals: dinner.\n\nDay 12 — Safaris in Ranthambore\n"
     "Morning and afternoon safaris. Meals: breakfast, dinner.\n\nDay 13 — Agra\nThe drive continues to Agra. "
     "Meals: breakfast."),
], ids=["northern-heights-B-L", "khuvsgul-L+D-range", "sikkim-tabular", "china-express-bullets", "rajasthan-11-13"])
def test_meal_line_copied_from_the_source_day_is_not_flagged_and_jev_is_not_asked(source, itin):
    codes, jev_calls = _codes(itin, source)
    assert MEAL not in codes
    assert jev_calls == 0


@pytest.mark.parametrize("source,itin", [
    # the codes say "( B )"; dinner comes from the prose: not the source's meal list
    (PADDLE_AND_SURF, "Day 9 — Laoag to Manila\nThe group visits Cape Bojeador Lighthouse. Overnight: City Hotel. "
                      "Meals: breakfast, dinner."),
    # no meal list at all: "After breakfast, travel…" is narration, not an included breakfast
    (RELIGIOUS_SOUTH_INDIA, "Day 9 — Madurai to Trivandrum\nThe group travels to Trivandrum. Meals: breakfast."),
    # a word the source day does not have
    (NORTHERN_HEIGHTS, "Day 1 — Chiang Mai\nAn easy ride along the Ping River. Meals: buffet breakfast, lunch."),
    # the right meals on the WRONG day (the source lists dinner on day 11 only)
    (RAJASTHAN_11_13, "Day 13 — Agra\nThe drive continues to Agra. Meals: dinner."),
    # a second claim in the same sentence still goes to Jev
    (NORTHERN_HEIGHTS, "Day 1 — Chiang Mai\nAn easy ride along the Ping River. Meals: breakfast; dinner included."),
    # an invented clock time is never cleared by a meal line
    (NORTHERN_HEIGHTS, "Day 1 — Chiang Mai\nThe ride starts at 7:00 AM. Meals: breakfast, lunch."),
], ids=["paddle-surf-dinner-from-prose", "south-india-after-breakfast", "extra-word", "wrong-day", "second-claim",
        "clock-time"])
def test_a_meal_or_word_the_source_day_does_not_list_is_still_flagged(source, itin):
    codes, _ = _codes(itin, source)
    assert MEAL in codes


def test_tenant_rewrite_keeps_the_old_path():
    itin = "Day 1 — Chiang Mai\nAn easy ride along the Ping River. Meals: breakfast, lunch."
    codes, _ = _codes(itin, NORTHERN_HEIGHTS, tenant_id="t1", is_tenant_rewrite=True)
    assert MEAL in codes      # no source-backed shortcut on T2: the short claim gets no Jev verdict, so it stays


def test_unmarked_source_counts_for_a_one_day_page_only():
    src = "Pick-up from the hotel, cycle through the hutongs and the Lama Temple.\nMeals: Lunch\n"
    one_day = "Day 1 — Hutongs by bike\nThe ride passes the Lama Temple. Meals: lunch."
    assert MEAL not in _codes(one_day, src)[0]
    two_days = one_day + "\n\nDay 2 — More hutongs\nAnother ride. Meals: lunch."
    assert MEAL in _codes(two_days, src)[0]


def test_lower_case_enumeration_is_not_a_meal_code():
    assert ba._source_day_meals("Choose (a) the river walk or (b) the temple visit.") == set()
    assert ba._source_day_meals("Overnight in camp. (B/-/D)") == {"B", "D"}
    assert ba.meal_set("Breakfast only; lunch and dinner not included") == {"B"}
    assert ba.meal_set("full board") == {"B", "L", "D"} and ba.meal_set("HB") == {"B", "D"}


# Review fixes: negation scoped to the meal it governs; an empty page meal set needs more than "included".
AMBER_FORT = "Day 2: Amber Fort (B)\nWe visit the fort. Lunch and dinner not included. Free evening, no plans.\n"
FREE_DAY = "Day 2: Free day.\n"
LEISURE_DAY = "Day 2: All day at leisure.\n"


@pytest.mark.parametrize("source,line", [
    (AMBER_FORT, "Meals: breakfast and dinner (lunch not included)."),   # dinner claimed; the source excludes it
    (AMBER_FORT, "Meals: dinner and no lunch."),                          # a meal outside the negated run
    (FREE_DAY, "Meals: included."),
    (FREE_DAY, "Meals: provided."),
    (FREE_DAY, "Meals: all included."),
    (LEISURE_DAY, "Meals: all meals."),                                   # "All day" in the prose backs nothing
], ids=["paren-negation", "dinner-and-no-lunch", "included", "provided", "all-included", "all-meals"])
def test_inclusion_claims_without_a_source_meal_list_stay_flagged(source, line):
    assert not ba.source_backed_meal_line(line, source)
    codes, _ = _codes(f"Day 2 — The day\nThe group spends the day in town. {line}", source)
    assert MEAL in codes      # not cleared by the shortcut; Jev (grey here) or the AA-803 no-unit rule keeps it


@pytest.mark.parametrize("source,line", [
    (AMBER_FORT, "Meals: breakfast (lunch not included)."),
    (FREE_DAY, "Meals: none included."),
    ("Day 2: Hike\nMeals: picnic meals\n", "Meals: picnic meals."),
    ("Day 2: Old town\nLunch optional, payable directly.\n", "Meals: lunch optional, payable directly."),
])
def test_negative_or_content_word_lines_the_day_backs_are_still_cleared(source, line):
    assert ba.source_backed_meal_line(line, source)


def test_page_meal_set_scopes_negation():
    assert ba.page_meal_set("lunch and dinner not included") == set()
    assert ba.page_meal_set("dinner at own expense") == set()
    assert ba.page_meal_set("breakfast, lunch optional") == {"B"}
    assert ba.page_meal_set("breakfast and dinner (lunch not included)") == {"B", "D"}
    assert ba.page_meal_set("dinner and no lunch") is None
    assert ba.page_meal_set("dinner but not lunch") is None


def test_a_prose_meal_label_line_is_not_a_meal_list():
    assert ba._source_day_meals("Meal times are flexible; dinner on the boat.") == set()
    assert ba._source_day_meals("Meals  L D") == {"L", "D"}
    assert ba._source_day_meals("Meals: Dinner on the boat") == {"D"}


# ── 2. repair contract: days_kept ────────────────────────────────────────────────────────────────

def _itin(days):
    return "\n\n".join(f"Day {d} — Place {d}\nThe group visits place {d}. Overnight: town {d}." for d in days)


def test_rajasthan_15_to_10_is_reverted_by_days_kept():
    before, after = {"itineraries": _itin(range(1, 16))}, {"itineraries": _itin(range(1, 11))}
    kept, res = rc.enforce_fields(before, after, allowed={"itineraries"}, names=False)
    assert kept == before
    assert res.invariants() == ["days_kept"]
    assert "dropped Day 11, 12, 13, 14, 15" in res.violations[0]["detail"]
    assert "15 day blocks before, 10 after" in res.violations[0]["detail"]


@pytest.mark.parametrize("after_days,why", [
    ([1, 2, 3, 4, 5], "added Day 5"),
    ([1, 3, 2, 4], "reordered or duplicated"),
    ([1, 2, 2, 3, 4], "reordered or duplicated"),
    ([1, 2, 3, 5], "dropped Day 4"),
])
def test_added_renumbered_or_duplicated_days_are_reverted(after_days, why):
    res = rc.check({"itineraries": _itin([1, 2, 3, 4])}, {"itineraries": _itin(after_days)})
    days = [v for v in res.violations if v["invariant"] == "days_kept"]
    assert days and why in days[0]["detail"]


def test_a_repair_that_keeps_every_day_passes():
    before = _itin(range(1, 16))
    after = before.replace("The group visits place 7.", "The group walks through place 7.")
    assert rc.check({"itineraries": before}, {"itineraries": after}).ok


def test_loose_and_list_formats_are_read():
    before = "Day 1: Delhi. Arrive.\nDay 2: Mandawa. Drive.\nDay 3: Bikaner. Drive."
    assert rc.check({"itineraries": before}, {"itineraries": "Day 1: Delhi. Arrive.\nDay 2: Mandawa. Drive."}
                    ).invariants() == ["days_kept"]
    as_list = [{"day": 1, "title": "Delhi", "body": "Arrive."}, {"day": 2, "title": "Mandawa", "body": "Drive."}]
    assert rc.check({"itineraries": _itin([1, 2, 3])}, {"itineraries": as_list}).invariants() == ["days_kept"]


def test_no_day_headings_before_or_another_field_is_not_checked():
    assert rc.check({"itineraries": "A short walk in the old town."},
                    {"itineraries": "Day 1 — Old town\nA short walk in the old town."}).ok
    assert rc.check({"summary": "Day 1 — A\nx.\n\nDay 2 — B\ny."}, {"summary": "Day 1 — A\nx."}).ok
    assert "days_kept" in rc.INVARIANTS


@pytest.mark.parametrize("tail", [
    "\n\nDay 11-15 — Trek\nFive days of trekking.",
    "\n\nDays 11–15 — Trek\nFive days of trekking.",
    "\n\n" + "\n".join(f"Day {d}: Trek stage {d}." for d in range(11, 16)),
], ids=["range-heading", "days-range-heading", "loose-after-canonical"])
def test_mixed_canonical_and_range_or_loose_headings(tail):
    mixed = _itin(range(1, 11)) + tail
    lost = rc.check({"itineraries": mixed}, {"itineraries": _itin(range(1, 11))})
    assert lost.invariants() == ["days_kept"]
    assert "dropped Day 11, 12, 13, 14, 15" in lost.violations[0]["detail"]
    # turning the mixed page into canonical Days 1-15 keeps every day: not a days_kept violation (the new "12, 13,
    # 14" of the test filler text is invariant 2's business)
    full = rc.check({"itineraries": mixed}, {"itineraries": _itin(range(1, 16))})
    assert "days_kept" not in full.invariants()


def test_removing_a_duplicated_day_keeps_the_days():
    assert rc.check({"itineraries": _itin([1, 2, 2, 3])}, {"itineraries": _itin([1, 2, 3])}).ok


def test_flag_fix_contract_revert_keeps_the_15_days():
    from services.content_generation.flag_fix_node import _contract_revert, _invariants

    before = {"itineraries": _itin(range(1, 16)), "name": "Rajasthan Adventure"}
    after = {"itineraries": _itin(range(1, 11)), "name": "Rajasthan Adventure"}
    reverted = _contract_revert(before, after, {"itineraries"}, [], {"itineraries": _itin(range(1, 16))})
    assert after["itineraries"] == before["itineraries"]
    assert _invariants(reverted) == ["days_kept"]


# ── 3. ITINERARY_DAY_COUNT_MISMATCH: hard for an A1 writer-v2 draft only ──────────────────────────

RAJASTHAN_ROW = {  # the shadow row (wave50-shadow.json), the fields _is_publishable reads
    "quality_score": 9.75, "brand_audit_status": "fixed", "fix_pass_applied": True,
    "failure_codes": ["ITINERARY_DAY_COUNT_MISMATCH"],
}


def test_rajasthan_row_is_not_publishable_as_a_v2_draft_and_unchanged_otherwise():
    from api.routers.admin_pipeline import _is_publishable, _surviving_hard_codes

    assert _is_publishable(RAJASTHAN_ROW)                                     # v1: soft, as before
    v2 = {**RAJASTHAN_ROW, "writer_version": "v2"}
    assert not _is_publishable(v2)
    assert _surviving_hard_codes(v2) == {"ITINERARY_DAY_COUNT_MISMATCH"}
    assert _is_publishable({**v2, "is_tenant_rewrite": True})                 # never a tenant rewrite


def test_hard_block_codes_and_gate_count():
    from services.content_generation import graph

    assert "ITINERARY_DAY_COUNT_MISMATCH" not in graph._HARD_BLOCK_CODES      # the static set is unchanged
    assert graph.hard_block_codes({}) == graph._HARD_BLOCK_CODES
    assert graph.hard_block_codes({"writer_version": "v2"}) == graph._HARD_BLOCK_CODES | {
        "ITINERARY_DAY_COUNT_MISMATCH"}
    assert graph.hard_block_codes({"writer_version": "v2", "is_tenant_rewrite": True}) == graph._HARD_BLOCK_CODES
    assert graph.HARD_CODE_REPAIR["ITINERARY_DAY_COUNT_MISMATCH"].startswith("review:")
    # option 1a: no new code, the departure-day oracle code stays shadow
    from services.quality import day_oracle
    assert day_oracle.DAY_ORACLE_MODES[day_oracle.DAY_COUNT_VS_DURATION] == "shadow"
    assert all(m == "shadow" for c, m in day_oracle.DAY_ORACLE_MODES.items() if "DEPARTURE" in c)


def test_human_edit_gate_blocks_a_v2_day_loss_only():
    from services.content_generation.graph import human_edit_gate_node

    base = {"quality_score": 9.75, "brand_audit_status": "fixed", "failure_codes": ["ITINERARY_DAY_COUNT_MISMATCH"]}
    assert human_edit_gate_node(dict(base))["revalidate_passed"] is True
    assert human_edit_gate_node({**base, "writer_version": "v2"})["revalidate_passed"] is False
