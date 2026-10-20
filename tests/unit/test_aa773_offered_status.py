"""AA-773 (S235) — offered vs included Status line on every moment, and every seed reader keeps the
Status text out of its facts / offered detection (S235 synthesis R9)."""
import subprocess
import sys

import pytest

from services.acp_content_writing import claim_rules as cr
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import trip_facts as tf
from services.acp_content_writing.prompts import SYSTEM_PROMPT
from services.acp_shared import offered as off
from services.acp_shared.atom_seed import (THIN_FACTS, compose_route_moment, compose_seed, group_route_moments,
                                           moment_fact_count, route_moment_statuses)

pytestmark = pytest.mark.real_atom_seed

KHOV = {"card_id": "c-khov", "display_name": "Lake Khövsgöl", "locality": None, "region": "Khövsgöl Province",
        "country": "Mongolia", "naming_note": None,
        "facts": [{"text": "It holds about 70 percent of Mongolia's fresh water."},
                  {"text": "The surrounding area is protected as Khövsgöl Nuur National Park."},
                  {"text": "It lies where steppe meets taiga."}]}
URAN = {"card_id": "c-uran", "display_name": "Uran Togoo Volcano", "locality": "Bulgan", "region": None,
        "country": "Mongolia", "naming_note": None, "facts": []}
MORON = {"card_id": "c-moron", "display_name": "Mörön", "locality": "Mörön", "region": None,
         "country": "Mongolia", "naming_note": None, "facts": []}
TSAATAN = {"card_id": "c-tsaatan", "display_name": "Tsaatan settlements", "locality": None, "region": None,
           "country": "Mongolia", "naming_note": None, "facts": []}
OPT = "Optional activities include boat rides on the water"


def _part(atom_id, day, text, evidence, card=None):
    return {"atom_id": atom_id, "day": day, "text": text, "evidence": evidence, "card": card,
            "place": None, "country": "Mongolia", "hook": None, "season": None}


def _r4_parts():
    """The S234 r4 route moments, in day order (seed printed in .tmp-session/s234/pieces_r4.json)."""
    return [
        _part("u", 3, "Uran Togoo — trek into the caldera of a dormant volcano",
              "Journey to Uran Togoo, a dormant volcano that erupted approximately 24,000 years ago. The volcano "
              "sits within green steppes and forests.", URAN),
        _part("s", 3, "Sod Camp — overnight in a standard ger camp",
              "Arrive at Sod Camp in the afternoon. Overnight in a standard ger camp."),
        _part("m", 4, "Moron — stop for lunch",
              "Stop for lunch near Moron, capital of Khövsgöl province, before continuing to the lakeshore camp.",
              MORON),
        _part("k1", 4, "Lake Khövsgöl — visit the boundary between Central Asian steppes and Siberian Taiga forests",
              "The lake sits at the boundary between Central Asian steppes and Siberian Taiga forests, creating a "
              "distinct ecological zone.", KHOV),
        _part("k2", 5, "Khövsgöl Lake — take boat rides on the water", OPT, KHOV),
        _part("k3", 5, "Khövsgöl Lake — horseback riding across surrounding terrain",
              OPT + ", horseback riding across surrounding terrain", KHOV),
        _part("k4", 5, "Khövsgöl Lake — visit local nomadic families",
              OPT + ", horseback riding across surrounding terrain, visits to local nomadic families", KHOV),
        _part("k5", 5, "Khövsgöl Lake — trek along shorelines and forest paths",
              "trekking along shorelines and forest paths", KHOV),
        _part("t", 6, "Tsaatan reindeer herders' settlements — excursions to Tsaatan reindeer herders' settlements",
              "excursions to Tsaatan reindeer herders' settlements", TSAATAN),
    ]


def _route_blocks(parts):
    return [compose_route_moment(g) for g in group_route_moments(route_moment_statuses(parts))]


# ---------------------------------------------------------------- offered.py (sentence level)

def test_sentence_level_no_reverse_flip():
    ev = "Day starts at the lake. Optional boat rides are available."
    assert off.evidence_status(ev, "Lake Khövsgöl — take boat rides")[0] == off.OFFERED
    assert off.evidence_status(ev, "Lake Khövsgöl — walk by the lake") == (off.INCLUDED, None)
    # one atom seed: included line only because the same evidence offers something else
    assert off.single_status_line(ev, "Lake Khövsgöl — walk by the lake") == off.INCLUDED_LINE
    assert off.single_status_line("Walk by the lake.", "Lake Khövsgöl — walk") is None


def test_route_same_day_contrast_and_offered_quote():
    parts = route_moment_statuses([
        _part("a", 2, "Lake — walk by the lake", "Day starts at the lake. Optional boat rides are available."),
        _part("b", 2, "Lake — take boat rides", "Day starts at the lake. Optional boat rides are available."),
        _part("c", 3, "Town — market walk", "Walk the market in town."),
    ])
    assert [p["status"] for p in parts] == [off.INCLUDED, off.OFFERED, off.INCLUDED]
    assert parts[0]["status_line"] == off.INCLUDED_LINE
    assert parts[1]["status_line"].startswith('Status: OFFERED, NOT INCLUDED (the itinerary says: "Optional boat')
    assert parts[2]["status_line"] is None  # no offered item that day or at that place


def test_r4_khovsgol_fixture():
    parts = {p["atom_id"]: p for p in route_moment_statuses(_r4_parts())}
    assert {a for a, p in parts.items() if p["status"] == off.OFFERED} == {"k2", "k3", "k4", "k5"}
    # the lower-case continuation keeps the list's quote
    assert "… trekking along shorelines and forest paths" in parts["k5"]["status_line"]
    # Day 4 boundary moment shares the Khövsgöl place with the offered ones → included line
    assert parts["k1"]["status_line"] == off.INCLUDED_LINE
    # the lower-case Tsaatan fragment follows an offered moment of ANOTHER place and day → not offered
    assert parts["t"]["status"] == off.INCLUDED and parts["t"]["status_line"] is None
    for a in ("u", "s", "m"):
        assert parts[a]["status_line"] is None


def test_continuation_needs_same_day_and_place():
    parts = route_moment_statuses([
        _part("a", 5, "Lake — boat rides", OPT, KHOV),
        _part("b", 5, "Camp — trekking", "trekking along shorelines", URAN),
    ])
    assert parts[1]["status"] == off.INCLUDED


# ---------------------------------------------------------------- rendering + fact count

def test_status_is_its_own_line_and_never_a_fact():
    blocks = _route_blocks(_r4_parts())
    khov = next(b for b in blocks if b.startswith("Lake Khövsgöl"))
    lines = khov.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("Status:"):
            assert "Source itinerary:" in lines[i - 1]
        if "Source itinerary:" in ln:
            assert "Status" not in ln
    assert sum(ln.startswith("Status: OFFERED") for ln in lines) == 4
    assert sum(ln == off.INCLUDED_LINE for ln in lines) == 1
    stripped = off.strip_status_lines(khov)
    assert moment_fact_count(khov) == moment_fact_count(stripped)
    for b in blocks:
        assert moment_fact_count(b) == moment_fact_count(off.strip_status_lines(b))
        assert ("Thin moment:" in b) == (moment_fact_count(b) < THIN_FACTS)


def test_compose_seed_single_atom_status():
    seed = compose_seed("Khövsgöl Lake — take boat rides on the water", OPT, None, None, [], place="Lake Khövsgöl",
                        day=5, card=KHOV)
    lines = seed.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("Source itinerary:"))
    assert lines[i + 1].startswith("Status: OFFERED, NOT INCLUDED")
    plain = compose_seed("Punakha Dzong — visit", "Visit Punakha Dzong.", None, None, [], place="Punakha Dzong")
    assert "Status:" not in plain
    none = compose_seed("Khövsgöl Lake — boat", OPT, None, None, [], status=None)
    assert "Status:" not in none


# ---------------------------------------------------------------- parsers keep Status out (R9)

def test_claim_rules_skips_status_lines():
    blocks = _route_blocks(_r4_parts())
    with_status = cr.number_source_lines("\n\n".join(blocks))
    without = cr.number_source_lines("\n\n".join(off.strip_status_lines(b) for b in blocks))
    assert [(ln.id, ln.text) for ln in with_status] == [(ln.id, ln.text) for ln in without]
    assert not any("Status" in ln.text for ln in with_status)


def test_unused_source_facts_never_returns_status_text():
    blocks = _route_blocks(_r4_parts())
    seed = "\n\n".join(blocks)
    unused = qg.unused_source_facts(seed, "", limit=100)
    assert unused == qg.unused_source_facts(off.strip_status_lines(seed), "", limit=100)
    assert not any("Describe it as available" in u or "Never call it optional" in u or "NOT INCLUDED" in u
                   for u in unused)
    line = off.offered_line("Optional activities include boat rides on the water and horse treks")
    assert qg.unused_source_facts(line, "") == []


def test_trip_facts_status_lines_are_not_trip_lines():
    seed = "\n\n".join(_route_blocks(_r4_parts()))
    for m in tf.parse_moments(seed):
        assert not any(ln.startswith("Status:") for ln in m.trip_lines)
    acts = tf.optional_activities(tf.parse_moments(seed))
    assert {a.phrase for a in acts} == {"boat rides on the water", "horseback riding across surrounding terrain",
                                        "visits to local nomadic families",
                                        "trekking along shorelines and forest paths"}


def test_trip_facts_status_included_overrides_loose_marks():
    """F17's own marks include 'available' / 'can'; an included Status line decides instead (one decision)."""
    seed = ("Lake Khövsgöl — kayaking on the lake\nDay 5 of the trip\nSource itinerary: Kayaks are available at "
            "the camp.\n" + off.INCLUDED_LINE)
    assert tf.optional_activities(tf.parse_moments(seed)) == []
    legacy = seed.replace("\n" + off.INCLUDED_LINE, "")
    assert tf.optional_activities(tf.parse_moments(legacy))  # pre-AA-773 seed: unchanged rule


# ---------------------------------------------------------------- F17 owns "optional stated as planned"
# AA-787 dropped promises_an_option (CIS ADR 0009). These are its AA-773 cases, now asserted on F17_trip_fact:
# the same sentences must still be caught (blocking, sentence-repairable), and the hedged / included ones not.

def _f17(text: str, seed: str) -> dict:
    return qg.gate_trip_facts(text, tf.TripFactContext(seed=seed))[0]


def test_f17_ignores_an_included_only_block():
    included_only = ("Khövsgöl Lake — walk\nSource itinerary: Walk by the lake.\n" + off.INCLUDED_LINE)
    assert _f17("You will walk by the lake. You will see the shore.", included_only)["passed"]


def test_f17_flags_only_the_offered_activity_single_atom():
    seed = compose_seed("Khövsgöl Lake — take boat rides on the water", OPT, None, None, [])
    text = "You take boat rides on the water every morning. The lake is very large. You can take a boat ride."
    gate = _f17(text, seed)
    assert gate["gate"] == "F17_trip_fact" and gate["blocking"] and gate["repairable"]
    optional = [v for v in gate["violations"] if v.startswith("presents 'boat rides on the water' as part of the plan")]
    assert len(optional) == 1 and optional[0].endswith("'You take boat rides on the water every morning.'")
    assert not any("You can take a boat ride." in v for v in gate["violations"])  # hedged: never flagged


def test_f17_flags_the_offered_activity_in_a_route_block_not_the_included_boundary():
    blocks = _route_blocks(_r4_parts())
    text = ("The lake sits where steppe meets taiga. "
            "Families ride horseback across the terrain and take boat rides on the water. "
            "There is time to take boat rides on the water.")
    gate = _f17(text, "\n\n".join(blocks))
    flagged = [v.rsplit(": '", 1)[-1] for v in gate["violations"]]
    assert flagged[0].startswith("Families ride horseback")
    assert not any(f.startswith("The lake sits") for f in flagged)  # the included steppe/taiga moment
    # Known difference (AA-787 notes): F17's hedge list has no 'there is time to', which the retired gate
    # accepted, so F17 also flags this hedged sentence. Stricter, not a recall loss; F17 tuning owns it.
    assert flagged[1:] == ["There is time to take boat rides on the water.'"]


def test_promises_an_option_is_gone():
    for name in ("gate_promises_an_option", "promised_sentences", "_is_offered", "_OFFERED_PHRASES"):
        assert not hasattr(qg, name)


# ---------------------------------------------------------------- light module, prompt

def test_atom_seed_does_not_import_quality_gates():
    code = ("import sys; import services.acp_shared.atom_seed; "
            "print('services.acp_content_writing.quality_gates' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_system_prompt_has_status_rule():
    assert "Status line says OFFERED, NOT INCLUDED is kept in the piece and described as a" in SYSTEM_PROMPT
    assert 'says included is never called optional, extra or "if you wish"' in SYSTEM_PROMPT


# ---------------------------------------------------------------- review S235 (B1-B3 + follow-ups)

@pytest.mark.parametrize("evidence,label", [
    ("Visit the Tiger's Nest monastery (entrance fees not included).", "Paro — visit Tiger's Nest monastery"),
    ("Hike to the Tiger's Nest, lunch not included.", "Paro — hike to Tiger's Nest"),
    ("Hike to the Tiger's Nest. Lunch is not included.", "Paro — hike to Tiger's Nest"),
    ("Overnight in a ger camp with a single supplement for solo travellers.", "Steppe — overnight in a ger camp"),
    ("Perhaps the highlight of the trip, the Tsaatan camp visit includes a night in a tepee.",
     "Taiga — Tsaatan camp visit"),
    ("A perfect day for those who love history: tour the Punakha Dzong.", "Punakha — tour the Punakha Dzong"),
    ("Visit the market; a cooking class is possible at additional cost.", "Hanoi — visit the market"),
    ("Enjoy a boat trip on Ha Long Bay (pre-booked for the group).", "Ha Long — boat trip"),
    ("Kayaks are available for those who wish to paddle; we hike the ridge trail.", "Lake — hike the ridge trail"),
])
def test_b3_included_content_is_never_offered(evidence, label):
    assert off.evidence_status(evidence, label)[0] != off.OFFERED


@pytest.mark.parametrize("evidence,label", [
    ("Drive to the lake. Horse riding is available at extra cost.", "Lake — horse riding"),
    ("Kayaking at an additional fee.", "Lake — kayaking"),
    ("Horse riding at an extra charge.", "Lake — horse riding"),
    ("A cooking class for a fee.", "Hanoi — cooking class"),
    ("Visit the market; a cooking class is possible at additional cost.", "Hanoi — cooking class"),
    ("Hike the ridge trail. Optional kayaking or a sunset cruise can be arranged.", "Lake — sunset cruise"),
])
def test_b2_cost_wording_is_offered(evidence, label):
    assert off.evidence_status(evidence, label)[0] == off.OFFERED


def test_b2_loose_mark_without_offer_gets_no_line():
    assert off.evidence_status("Kayaks are available at the camp.", "Lake — kayaks") == (None, None)
    parts = route_moment_statuses([
        _part("a", 5, "Khövsgöl Lake — boat rides", OPT, KHOV),
        _part("b", 5, "Khövsgöl Lake — kayaks", "Kayaks are available at the camp.", KHOV),
    ])
    assert parts[1]["status_line"] is None


def test_b2_market_moment_is_included_against_the_cooking_class():
    ev = "Visit the market; a cooking class is possible at additional cost."
    assert off.single_status_line(ev, "Hanoi — visit the market") == off.INCLUDED_LINE
    assert off.single_status_line(ev, "Hanoi — cooking class").startswith("Status: OFFERED")


def test_b1_included_status_keeps_other_offers_on_the_line():
    seed = compose_seed("Khövsgöl Lake — drive to the lake",
                        "Drive to the lake. Optional boat rides. Horse riding is available at extra cost.",
                        None, None, [], day=5)
    assert off.INCLUDED_LINE in seed
    phrases = {a.phrase for a in tf.optional_activities(tf.parse_moments(seed))}
    assert any("boat rides" in p for p in phrases) and any("Horse riding" in p for p in phrases)


def test_b1_offered_status_covers_only_its_own_sentence():
    seed = compose_seed("Khövsgöl Lake — horse riding",
                        "Drive to the lake shore and hike the ridge trail. Horse riding at an extra charge.",
                        None, None, [], day=5)
    ms = tf.parse_moments(seed)
    assert [a.phrase for a in tf.optional_activities(ms)] == ["Horse riding"]
    assert tf.optional_as_fixed("We drive to the lake shore and hike the ridge trail together.", ms) == []
    assert tf.optional_as_fixed("We go horse riding along the shore.", ms)


def test_b1_horse_moment_keeps_boat_rides_from_the_same_line():
    seed = compose_seed("Khövsgöl Lake — horse riding",
                        "Drive to the lake. Optional boat rides. Horse riding is available at extra cost.",
                        None, None, [], day=5)
    phrases = {a.phrase for a in tf.optional_activities(tf.parse_moments(seed))}
    assert "Horse riding" in phrases and any("boat rides" in p for p in phrases)


def test_continuation_breaks_on_included_fragment():
    parts = route_moment_statuses([
        _part("a", 5, "Khövsgöl Lake — boat rides", OPT, KHOV),
        _part("b", 5, "Khövsgöl Lake — visit to the Tsaatan",
              "visit to the Tsaatan reindeer herders included in the trip", KHOV),
    ])
    assert parts[1]["status"] == off.INCLUDED


def test_continuation_needs_a_shared_place_card():
    parts = route_moment_statuses([
        _part("a", 5, "Lake — boat rides", OPT, KHOV),
        _part("b", 5, "Lake — hike to the ridge", "hike to the ridge with your guide"),  # no card
    ])
    assert parts[1]["status"] == off.INCLUDED



def test_status_words_never_become_allowed_scope_words():
    from services.acp_content_writing import hygiene as h
    seed = compose_seed("Khövsgöl Lake — take boat rides on the water", OPT, None, None, [])
    lex = h.build_scope_lexicon(seed)
    assert "Status:" not in lex.own_text and "NOT INCLUDED" not in lex.own_text


# ---------------------------------------------------------------- review S235 round 2 (N1)

@pytest.mark.parametrize("clause,activity", [
    ("kayaking and an optional hot-air balloon flight.", "hot-air balloon flight"),
    ("with an optional stop at the water puppet show.", "stop at the water puppet show"),
    ("then an optional sunset cruise.", "sunset cruise"),
    ("Optional boat rides", "boat rides"),
    ("Horse riding is available at extra cost.", "Horse riding"),
    ("Kayaking at an additional fee.", "Kayaking"),
    ("Optional kayaking or a sunset cruise can be arranged.", "kayaking or a sunset cruise"),
])
def test_n1_activity_text_takes_the_offered_side(clause, activity):
    assert off.activity_text(clause) == activity


def test_n1_list_with_one_optional_item():
    seed = compose_seed("Mekong — hot-air balloon flight",
                        "Activities include hiking, kayaking and an optional hot-air balloon flight.", None, None, [],
                        day=3)
    assert seed.count("Status: OFFERED") == 1
    ms = tf.parse_moments(seed)
    assert [a.phrase for a in tf.optional_activities(ms)] == ["hot-air balloon flight"]
    assert tf.optional_as_fixed("We spend the morning kayaking on the river.", ms) == []
    assert tf.optional_as_fixed("At dawn we take a hot-air balloon flight over the river.", ms)


@pytest.mark.parametrize("evidence,label,phrase,promise", [
    ("Walk the old quarter, with an optional stop at the water puppet show.", "Hanoi — water puppet show",
     "stop at the water puppet show", "We stop at the water puppet show in the evening."),
    ("Walk the river bank, then an optional sunset cruise.", "Mekong — sunset cruise",
     "sunset cruise", "We end the day on a sunset cruise."),
])
def test_n1_trailing_optional_item_is_the_one_caught(evidence, label, phrase, promise):
    ms = tf.parse_moments(compose_seed(label, evidence, None, None, [], day=2))
    assert [a.phrase for a in tf.optional_activities(ms)] == [phrase]
    assert tf.optional_as_fixed(promise, ms)


def test_not_included_window_covers_longer_fee_wording():
    ev = "Entrance fees to the monastery and museum are not included."
    assert off.evidence_status(ev, "Paro — monastery")[0] != off.OFFERED


def test_hygiene_status_prefix_matches_offered():
    from services.acp_content_writing import hygiene as h
    assert h.STATUS_LINE_PREFIX == off.STATUS_PREFIX
