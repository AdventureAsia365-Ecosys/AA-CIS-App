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


# ---------------------------------------------------------------- promises_an_option (Status-aware)

def test_is_offered_ignores_status_text():
    included_only = ("Khövsgöl Lake — walk\nSource itinerary: Walk by the lake.\n" + off.INCLUDED_LINE)
    assert not qg._is_offered(included_only)
    assert qg.promised_sentences("You will walk by the lake. You will see the shore.", included_only) == []
    offered = compose_seed("Khövsgöl Lake — take boat rides on the water", OPT, None, None, [])
    assert qg._is_offered(offered)
    assert qg._is_offered("Lake — boat\nSource itinerary: Optional boat rides.")  # legacy seed, origin rule


def test_single_atom_flags_only_the_offered_activity():
    seed = compose_seed("Khövsgöl Lake — take boat rides on the water", OPT, None, None, [])
    text = "You take boat rides on the water every morning. The lake is very large. You can take a boat ride."
    flagged = [s for s, _, _ in qg.promised_sentences(text, seed)]
    assert flagged == ["You take boat rides on the water every morning."]


def test_route_block_flags_offered_activity_not_the_included_boundary():
    blocks = _route_blocks(_r4_parts())
    segs = [(f"r{i}", b) for i, b in enumerate(blocks)]
    kid = next(a for a, b in segs if b.startswith("Lake Khövsgöl"))
    text = (f"The lake sits where steppe meets taiga [R:{kid}]. "
            f"Families ride horseback across the terrain and take boat rides on the water [R:{kid}]. "
            f"There is time to take boat rides on the water [R:{kid}].")
    flagged = qg.promised_sentences(text, "\n\n".join(blocks), segs)
    assert len(flagged) == 1 and flagged[0][0].startswith("Families ride horseback")
    gate = qg.gate_promises_an_option(text, "\n\n".join(blocks), segs)
    assert len(gate["violations"]) == 1


# ---------------------------------------------------------------- one list, light module, prompt

def test_offered_phrases_single_source():
    assert qg._OFFERED_PHRASES is off.OFFERED_PHRASES


def test_atom_seed_does_not_import_quality_gates():
    code = ("import sys; import services.acp_shared.atom_seed; "
            "print('services.acp_content_writing.quality_gates' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_system_prompt_has_status_rule():
    assert "Status line says OFFERED, NOT INCLUDED is described as a choice" in SYSTEM_PROMPT
    assert 'says included is never called optional, extra or "if you wish"' in SYSTEM_PROMPT
