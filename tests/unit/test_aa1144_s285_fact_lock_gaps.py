"""AA-1144 follow-up (S285 live check of the agency T2 fact lock) — the two gaps it found.

1. "This seven-day trek" -> "This ten-day trek" went through (200) while "eleven-day" and "23-day" were refused: the
   figure base held the raw source texts, and a raw "10 days" cleared the new length. A day / night / week figure is now
   checked against the Master's length and the figures AA wrote (the version, the Master), never the raw texts.
2. Adding "International flights included." went through: any mention of flights in a base text cleared the claim. A
   new inclusion claim now needs the Master to state it (the version / Master text says it is included, or the raw
   inclusions list names it, with the same scope).
Invented tour (Hollin Ridge); no text from the DB, audits or prompts.
"""
from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from services.content_generation import t2_fact_lock as lock

ITINERARY = "\n".join(f"Day {n} · Stage {n} of the Hollin Ridge walk" for n in range(1, 8))
SUMMARY = ("This seven-day trek crosses Hollin Ridge to Emberfall Lodge, with two days at Quillon camp. "
           "A drive returns to Brackwater on the seventh day.")
PARENT = {
    "name": "Hollin Ridge Trek: 7 Days",
    "subtitle": "Ridge paths and lodges",
    "summary": SUMMARY,
    "highlights": ["Sunrise from Hollin Ridge", "Night at Emberfall Lodge"],
    "itineraries": ITINERARY,
    "seo_title": "Hollin Ridge Trek",
    "seo_meta": "Walk Hollin Ridge over seven days.",
}
MASTER = [PARENT["name"], SUMMARY, ITINERARY]
# The raw source: another departure of 10 days, a flight named in passing, a visa rule with its own day count.
RAW = ["Also sold as a 10 days extended route with 9 nights.", "A scenic flight over Quillon can be booked.",
       "Visa on arrival is valid for 30 days."]
DURATION = "7 days 6 nights"
INCLUSIONS = "Lodge accommodation\nGuide\nAirport transfers"
EXCLUSIONS = "Tips"


def _check(summary=None, *, master=MASTER, inclusions=INCLUSIONS, exclusions=EXCLUSIONS, duration=DURATION,
           edited=None):
    edited = edited if edited is not None else {"summary": summary}
    _new, locked = lock.check_edit(PARENT, edited, master + RAW + [inclusions, exclusions], ITINERARY, exclusions,
                                   master_texts=master, source_inclusions=inclusions, duration=duration)
    return [(x.field, x.fact, x.value) for x in locked]


# ── 1. the tour's length ──────────────────────────────────────────────────────

@pytest.mark.parametrize("summary,value", [
    (SUMMARY.replace("seven-day", "ten-day"), "ten-day"),               # the live case (raw says "10 days")
    (SUMMARY.replace("seven-day", "10-day"), "10-day"),
    (SUMMARY.replace("seven-day", "eleven-day"), "eleven-day"),
    (SUMMARY.replace("seven-day", "23-day"), "23-day"),
    (SUMMARY + " Nine nights in lodges.", "Nine nights"),               # raw "9 nights" never clears it
    (SUMMARY + " Seven nights in lodges.", "Seven nights"),             # the Master has 6 nights
    (SUMMARY + " Two weeks on the ridge.", "Two weeks"),
    (SUMMARY.replace("seven-day", "ten days of"), "ten days"),
], ids=["ten-day-word", "ten-day-digits", "eleven-day", "23-day", "raw-nights", "seven-nights", "two-weeks",
        "ten-days"])
def test_a_length_other_than_the_masters_is_locked(summary, value):
    assert ("summary", "figure", value) in _check(summary)


def test_a_length_is_locked_in_every_wording_field():
    locked = _check(edited={"seo_meta": "Walk Hollin Ridge over ten days.",
                            "highlights": ["A 10-day walk to Emberfall Lodge", "Night at Emberfall Lodge"]})
    assert ("seo_meta", "figure", "ten days") in locked
    assert ("highlights", "figure", "10-day") in locked


@pytest.mark.parametrize("summary", [
    "Seven days on Hollin Ridge, from Brackwater to Emberfall Lodge.",
    "A 7-day walk over Hollin Ridge, six nights in lodges.",
    "Over 7 days and 6 nights you cross Hollin Ridge.",
    "A one-week trek on Hollin Ridge.",
    SUMMARY.replace("This seven-day trek crosses", "Our seven-day route walks over"),
    "Two days at Quillon camp break the seven-day walk.",               # a figure AA wrote (the version / Master)
], ids=["seven-days", "7-day-six-nights", "digits", "one-week-idiom", "wording", "aa-sub-length"])
def test_the_masters_length_and_wording_pass(summary):
    assert _check(summary) == []


def test_the_raw_visa_days_never_clear_a_length():
    assert ("summary", "figure", "30 days") in _check(SUMMARY + " Allow 30 days for the walk.")


@pytest.mark.parametrize("itinerary,duration,expected", [
    (ITINERARY, DURATION, {("7", "day"), ("6", "night"), ("1", "week")}),
    (ITINERARY, "10 days 9 nights", {("7", "day"), ("6", "night"), ("1", "week")}),   # the Master's days win
    ("", "5 days 5 nights", {("5", "day"), ("5", "night")}),
    ("", "10-12 days", {("10", "day"), ("9", "night"), ("12", "day"), ("11", "night")}),
    ("", "3 hours", {("1", "day")}),
    ("", "", set()),
], ids=["master-days", "raw-disagrees", "raw-only", "range", "sub-day", "nothing"])
def test_master_length(itinerary, duration, expected):
    assert lock.master_length(itinerary, "", duration) == expected


def test_master_length_falls_back_to_the_version_itinerary():
    assert ("7", "day") in lock.master_length("", ITINERARY, "")


# ── 2. inclusion claims ───────────────────────────────────────────────────────

@pytest.mark.parametrize("sentence,group", [
    ("International flights included.", "flights"),                    # the live case (flights named in passing)
    ("Flights included.", "flights"),
    ("Visa included.", "visa"),
    ("Travel insurance is included.", "insurance"),
    ("All meals included.", "meals"),
    ("Price covers park entrance fees.", "entrance_fees"),
], ids=["international-flights", "flights", "visa", "insurance", "meals", "entrance-fees"])
def test_a_new_inclusion_the_master_does_not_state_is_locked(sentence, group):
    assert ("content", "included", group) in _check(SUMMARY + " " + sentence)


@pytest.mark.parametrize("sentence", [
    "Airport transfers included.",                                       # the raw inclusions list names them
    "Guide and lodge accommodation included.",
    "Lodges are included, but tips are not included.",
], ids=["transfers-listed", "guide-lodges", "tips-excluded"])
def test_an_inclusion_the_list_names_passes(sentence):
    assert _check(SUMMARY + " " + sentence) == []


def test_a_scoped_claim_needs_the_same_scope():
    inclusions = INCLUSIONS + "\nDomestic flight Brackwater - Quillon"
    assert _check(SUMMARY + " Domestic flights included.", inclusions=inclusions) == []
    assert ("content", "included", "flights") in _check(SUMMARY + " International flights included.",
                                                        inclusions=inclusions)
    assert ("content", "included", "flights") in _check(SUMMARY + " Flights included.", inclusions=inclusions)


def test_a_flight_the_master_names_in_passing_never_clears_a_claim():
    master = MASTER + ["Day 7 ends with the short flight back to Brackwater."]
    assert ("content", "included", "flights") in _check(SUMMARY + " International flights included.", master=master)


def test_a_claim_the_master_text_makes_passes_and_covers_a_narrower_scope():
    master = MASTER + ["Flights are included from Brackwater."]
    assert _check(SUMMARY + " International flights included.", master=master) == []


def test_a_claim_only_the_raw_text_makes_is_locked():
    raw_claim = ["Flights included."]                                   # the operator's text, not the Master's
    _new, locked = lock.check_edit(PARENT, {"summary": SUMMARY + " Flights included."}, MASTER + RAW + raw_claim,
                                   ITINERARY, EXCLUSIONS, master_texts=MASTER, source_inclusions=INCLUSIONS,
                                   duration=DURATION)
    assert lock.Locked("content", "included", "flights") in locked


def test_a_scoped_exclusion_locks_an_unscoped_claim():
    locked = _check(SUMMARY + " Flights included.", inclusions=INCLUSIONS + "\nDomestic flights",
                    exclusions="International flights")
    assert ("content", "included", "flights") in locked
    assert _check(SUMMARY + " Domestic flights included.", inclusions=INCLUSIONS + "\nDomestic flights",
                  exclusions="International flights") == []


# ── the route's base read ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_against_base_reads_length_and_inclusions_from_master_and_lists():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "aa_name": PARENT["name"], "aa_summary": SUMMARY, "aa_itineraries": ITINERARY, "src_description": RAW[0],
        "src_itineraries": RAW[1], "duration": DURATION, "inclusions": INCLUSIONS, "exclusions": EXCLUSIONS})
    _new, locked = await lock.check_against_base(conn, uuid4(), PARENT,
                                                 {"summary": SUMMARY.replace("seven-day", "ten-day")})
    assert locked == [lock.Locked("summary", "figure", "ten-day")]
    _new, locked = await lock.check_against_base(conn, uuid4(), PARENT,
                                                 {"summary": SUMMARY + " International flights included."})
    assert locked == [lock.Locked("content", "included", "flights")]
    _new, locked = await lock.check_against_base(conn, uuid4(), PARENT,
                                                 {"summary": SUMMARY + " Airport transfers included."})
    assert locked == []


def test_master_keys_are_the_aa_texts_of_the_base_sql():
    assert all(k.startswith("aa_") and f"pt.{k}" in lock.BASE_SQL for k in lock.MASTER_KEYS)
