"""AA-803 (S247), part A — the wider ITINERARY_MEAL_TIME_INVENTED detection.

On main the claim regex read only "<meal> [is] included|provided|served|arranged", meal codes and "Meals:", and the
check asked Jev only about sentences of at least MIN_WORDS words outside the day-title lines. So a plural, future or
generic claim ("Lunch and dinner are included", "dinner will be served", "full board") was never checked, and a short
"Dinner included." passed unchecked whenever another meal sentence got a Jev accept. The positive cases of group 1
and every test of groups 2 and 3 fail on main; group 4 guards the Jev call that did not change.
"""
from unittest.mock import patch

import pytest

from services.content_generation import brand_audit_node as ba
from services.content_generation import grounding as g
from shared.llm_client.decide import Verdict

CODE = "ITINERARY_MEAL_TIME_INVENTED"
JU = "services.content_generation.grounding.judge_units"

# A source with no meal at all: any meal claim in the rewrite is invented.
NO_MEAL_TOUR = {
    "name": "Western Bhutan Valleys", "duration": "3 Days / 2 Nights",
    "itineraries": ("Day 1: Arrive at Paro and visit Rinpung Dzong above the town.\n"
                    "Day 2: Hike to Taktsang Monastery on its cliff above the valley.\n"
                    "Day 3: Drive to the airport for the departure flight."),
    "inclusions": "Hotels, guide, driver.", "exclusions": "Flights, tips.",
}


def _gen(itin: str) -> dict:
    return {"name": "Western Bhutan Valleys", "itineraries": itin}


def _accept():
    return Verdict("a1_claim_supported", "enforce", "accept", probability=0.97)


def _accept_all(units, sources, *, tenant_id=None):
    return {i: _accept() for i in sources}


# ── 1. the regex reads the claim forms main missed ────────────────────────────────────────────────────

NEW_CLAIMS = [
    "Lunch and dinner are included.",
    "Dinner will be served at the lodge.",
    "Breakfasts are served on the terrace each morning.",
    "Meals on board the junk are prepared by the onboard chef.",
    "Evening meal included.",
    "All meals are included.",
    "The price includes all meals.",
    "The day ends at a lodge on a full board basis.",
    "Half-board at the lodge.",
    "Breakfast at the hotel is included.",
    "A welcome feast is hosted by the village elders.",
    "Lunch is also included.",
    "The walk ends at the lake, including a picnic lunch by the water.",
    "Three meals a day are part of the trek.",
    "A barbecue is offered on the beach.",
    "Day 3 — Hue (B/-/D)",
]


@pytest.mark.parametrize("sentence", NEW_CLAIMS)
def test_new_claim_forms_are_read(sentence):
    assert ba.ITIN_MEAL_INVENTED.search(sentence), sentence


# Narrative mentions, negations, options and advice stay out (they were out on main too).
NOT_CLAIMS = [
    "After breakfast, cycle to the next village for lunch.",
    "Arrive in Tsumago by dinner. The evening is free to wander.",
    "Lunch is not included.",
    "Lunch isn't included today.",
    "Dinner can be arranged on request.",
    "Dinner recommendations are provided by the guide.",
    "The afternoon includes free time before dinner.",
    "A lunch stop in a village where tea is served.",
    "Dinner at a restaurant where local dishes are served nearby.",
    "Travel across Vietnam following the programme.",
]


@pytest.mark.parametrize("sentence", NOT_CLAIMS)
def test_narrative_and_negated_mentions_are_not_claims(sentence):
    assert not ba.ITIN_MEAL_INVENTED.search(sentence), sentence


# ── 2. a plural / future / generic claim the source lacks now raises the code ─────────────────────────

@pytest.mark.parametrize("claim", [
    "Lunch and dinner are included at the guesthouse.",
    "Dinner will be served at the hotel in Paro tonight.",
    "The stay is on a full board basis at the hotel.",
])
def test_unsourced_wider_claim_raises_the_code(claim):
    itin = ("Day 1 — Arrival in Paro\nArrive at Paro and visit Rinpung Dzong above the town. " + claim + "\n\n"
            "Day 2 — Taktsang\nHike to Taktsang Monastery on its cliff above the valley.")
    with patch(JU, return_value={}):          # no Jev signal (off / outage): fail-closed
        codes = ba.pre_audit_checks(_gen(itin), NO_MEAL_TOUR)
    assert CODE in codes


def test_wider_claim_is_sent_to_jev_and_cleared_only_by_an_enforced_accept():
    itin = ("Day 1 — Arrival in Paro\nArrive at Paro and visit Rinpung Dzong. Lunch and dinner are included at "
            "the guesthouse in the valley.")
    with patch(JU, side_effect=_accept_all) as m:
        assert CODE not in ba.pre_audit_checks(_gen(itin), NO_MEAL_TOUR)
    asked = [u["sentence"] for u in m.call_args.args[0]]
    assert asked == ["Lunch and dinner are included at the guesthouse in the valley."]


# ── 3. a short or title-line claim Jev is never asked about keeps the code ────────────────────────────

def test_short_claim_keeps_the_code_even_when_the_long_one_is_accepted():
    itin = ("Day 1 — Arrival in Paro\nArrive at Paro and visit Rinpung Dzong. Breakfast is served at the hotel "
            "before the drive.\n\nDay 2 — Taktsang\nHike to Taktsang Monastery. Dinner included.")
    with patch(JU, side_effect=_accept_all) as m:
        codes = ba.pre_audit_checks(_gen(itin), NO_MEAL_TOUR)
    assert CODE in codes
    m.assert_not_called()                     # Jev could not clear "Dinner included." anyway


def test_title_line_code_list_keeps_the_code_even_when_the_body_claim_is_accepted():
    itin = ("Day 1 — Arrival in Paro (B, L, D)\nArrive at Paro and visit Rinpung Dzong. Breakfast is served at "
            "the hotel before the drive.")
    with patch(JU, side_effect=_accept_all) as m:
        codes = ba.pre_audit_checks(_gen(itin), NO_MEAL_TOUR)
    assert CODE in codes
    m.assert_not_called()


def test_claim_sentences_keep_short_sentences_and_title_lines_with_their_day():
    itin = "Day 2 — Hue (B, L)\nLunch is included. The citadel walk follows.\nDay 3 - Hoi An\nDinner included."
    got = [(u["sentence"], u["day"]) for u in ba.itinerary_claim_sentences(itin)]
    assert got == [("Day 2 — Hue (B, L)", 2), ("Lunch is included.", 2), ("The citadel walk follows.", 2),
                   ("Day 3 - Hoi An", 3), ("Dinner included.", 3)]


# ── 4. the Jev call itself is unchanged for askable sentences ─────────────────────────────────────────

def test_askable_claims_go_to_jev_with_the_grounding_units_and_sources():
    """Same unit dicts as grounding.sentence_units and the same per-unit source, so the same subject key (a
    cache hit on the grounding call)."""
    itin = ("Day 1 — Arrival in Paro\nArrive at Paro and visit Rinpung Dzong. Breakfast is served at the hotel "
            "before the drive.\n\nDay 2 — Taktsang\nHike to Taktsang Monastery. Lunch is provided at the cafeteria "
            "on the trail.")
    with patch(JU, return_value={}) as m:
        ba.meal_time_claims_supported(itin, NO_MEAL_TOUR)
    units, sources = m.call_args.args
    expected = [u for u in g.sentence_units({"itineraries": itin}) if ba.ITIN_MEAL_INVENTED.search(u["sentence"])]
    assert units == expected and len(units) == 2
    assert sources == {i: g.unit_source(NO_MEAL_TOUR, u) for i, u in enumerate(expected)}


def test_every_askable_claim_needs_an_enforced_accept():
    itin = ("Day 1 — Arrival in Paro\nArrive at Paro. Breakfast is served at the hotel before the drive.\n\n"
            "Day 2 — Taktsang\nHike to Taktsang. Lunch is provided at the cafeteria on the trail.")
    grey = Verdict("a1_claim_supported", "enforce", "grey", probability=0.6)
    with patch(JU, return_value={0: _accept(), 1: grey}):
        assert ba.meal_time_claims_supported(itin, NO_MEAL_TOUR) is False
    with patch(JU, side_effect=_accept_all):
        assert ba.meal_time_claims_supported(itin, NO_MEAL_TOUR) is True


def test_ownerless_tenant_text_is_still_never_sent():
    itin = "Day 1 — Arrival\nArrive at Paro. Lunch and dinner are included at the guesthouse."
    with patch(JU) as m:
        assert ba.meal_time_claims_supported(itin, NO_MEAL_TOUR, tenant_id=None, is_tenant_rewrite=True) is False
    m.assert_not_called()
