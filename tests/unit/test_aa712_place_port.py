"""AA-712 — the TripPlanner extraction logic ported into CIS behaves as it did in TripPlanner.

Cases copied from AA-TripPlanner-Web backend/tests/test_tour_days.py and test_tour_graph.py
(@ 6523816), adapted to source_id keys and the DaysResult wrapper. No DB, no model, no geocoder."""
from __future__ import annotations

import json

import pytest

from services.places import days as td
from services.places import graph as tg

# Real shape of CIS aa_itineraries (Ladakh e-bike tour, Dev, 29/09/2026), shortened.
LADAKH = """Day 1 — Arrival in Leh & Team Briefing
Arrive in Leh and transfer to Stok. Rest and acclimatize to the altitude.

Day 2 — Old Leh, Bazaar & First E-Cycle Ride
Morning sightseeing of Old Leh and the bazaar. Afternoon ride around Stok.

Day 3 — Stok to Sakti via Thiksay—48 Kilometers
The route passes Thiksay monastery and continues to Sakti. Overnight stay in Sakti.

Day 4-5 — Nubra Valley
Two days exploring Diskit and Hunder. Stay in Hunder, with camel rides on the dunes.

Day 6 — Departure from Leh
Transfer to Leh airport for your onward flight."""


def test_parse_days_splits_blocks_and_expands_ranges():
    blocks = td.parse_days(LADAKH)
    assert [(b.day_from, b.day_to) for b in blocks] == [(1, 1), (2, 2), (3, 3), (4, 5), (6, 6)]
    assert blocks[2].title.startswith("Stok to Sakti")
    assert "Overnight stay in Sakti" in blocks[2].body
    assert td.parse_days("Free text with no day headers.") == []


def test_rule_day_a_to_b_overnight_and_theme_title():
    blocks = td.parse_days(LADAKH)
    assert td.rule_day(blocks[2]) == ("Stok", "Sakti", "Sakti")
    assert td.rule_day(blocks[3])[2] == "Hunder"
    assert td.rule_day(blocks[1]) == (None, None, None)


def test_grounded_is_case_and_punctuation_insensitive_and_word_bounded():
    assert td.grounded("old leh", "Morning sightseeing of Old Leh and the bazaar.")
    assert not td.grounded("Le", "Morning sightseeing of Old Leh")
    assert not td.grounded("Kargil", "Morning sightseeing of Old Leh")
    assert not td.grounded(None, "anything")


def test_ground_llm_day_drops_places_not_in_that_days_text():
    b = td.parse_days(LADAKH)[1]
    g = td.ground_llm_day({"start": "Stok", "end": "Leh", "overnight": "Kargil",
                           "places": ["Old Leh", "Pangong Lake"]}, b)
    assert g == {"start": "Stok", "end": "Leh", "overnight": None, "places": ["Old Leh"]}


def test_merge_prefers_grounded_model_values_and_expands_a_range():
    b = td.parse_days(LADAKH)[3]
    recs = td.merge_day(b, (None, None, "Hunder"),
                        {"start": "Diskit", "end": "Hunder", "overnight": None, "places": ["Diskit"]})
    assert [r.day_index for r in recs] == [4, 5]
    assert recs[0].overnight_place == "Hunder"
    assert recs[0].start_place == "Diskit" and recs[0].places == []
    assert recs[0].extracted_by == "rule+llm"


def test_needs_model_ignores_the_last_day_without_overnight():
    blocks = td.parse_days("Day 1 — A to B\nOvernight in B.\n\nDay 2 — Departure\nFly home.")
    assert not td.needs_model(blocks, [td.rule_day(b) for b in blocks])


@pytest.mark.asyncio
async def test_extract_tour_uses_the_model_only_for_what_is_grounded():
    calls = []

    async def fake_generate(system, user):
        calls.append(user)
        return json.dumps({"days": [
            {"day": 1, "start": "Leh", "end": "Stok", "overnight": "Stok", "places": []},
            {"day": 2, "start": "Stok", "end": "Stok", "overnight": "Stok", "places": ["Old Leh"]},
            {"day": 3, "start": "Stok", "end": "Sakti", "overnight": "Sakti", "places": ["Thiksay"]},
            {"day": 4, "start": "Diskit", "end": "Hunder", "overnight": "Hunder", "places": []},
            {"day": 6, "start": "Leh", "end": None, "overnight": "Delhi", "places": []},  # invented
        ]})

    res = await td.extract_tour({"tour_id": "t1", "aa_name": "Ladakh", "aa_itineraries": LADAKH}, fake_generate)
    assert len(calls) == 1 and res.model_called and not res.model_failed
    by_day = {r.day_index: r for r in res.records}
    assert sorted(by_day) == [1, 2, 3, 4, 5, 6]
    assert by_day[1].overnight_place == "Stok" and by_day[1].extracted_by == "llm"
    assert by_day[3].overnight_place == "Sakti" and by_day[3].extracted_by == "rule+llm"
    assert by_day[5].overnight_place == "Hunder"
    assert by_day[6].overnight_place is None
    assert by_day[6].is_transit_day                       # "onward flight"
    assert res.rule_missed_nights == 2                    # days 1 and 2


@pytest.mark.asyncio
async def test_extract_tour_falls_back_to_rules_when_the_model_fails():
    async def broken(system, user):
        return "not json"

    res = await td.extract_tour({"tour_id": "t1", "aa_name": "", "aa_itineraries": LADAKH}, broken)
    by_day = {r.day_index: r for r in res.records}
    assert res.model_failed
    assert by_day[3].overnight_place == "Sakti" and by_day[3].extracted_by == "rule"
    assert by_day[1].overnight_place is None


@pytest.mark.asyncio
async def test_no_model_call_when_rules_find_every_night():
    async def must_not_call(system, user):
        raise AssertionError("model called")

    text = "Day 1 — Hanoi to Ninh Binh\nOvernight in Ninh Binh.\n\nDay 2 — Departure\nTransfer home."
    res = await td.extract_tour({"tour_id": "t", "aa_name": "", "aa_itineraries": text}, must_not_call)
    assert not res.model_called and res.records[0].overnight_place == "Ninh Binh"


def test_anchor_place_skips_transit_and_lodging():
    assert td.anchor_place("overnight bullet train", "Kyoto") == "Kyoto"
    assert td.anchor_place("Hotel Shanti", "Pokhara") == "Pokhara"
    assert td.anchor_place("Everest Base Camp", None) == "Everest Base Camp"
    assert td.anchor_place("sleeper train", None) is None


def test_days_hash_tracks_text_and_rule_version(monkeypatch):
    a = td.days_hash("x")
    assert a == td.days_hash("x") and a != td.days_hash("y")
    monkeypatch.setattr(td, "RULE_VERSION", td.RULE_VERSION + 1)
    assert td.days_hash("x") != a


def test_transit_day_keywords():
    assert td.is_transit_day("Fly from Delhi to Leh this morning.")
    assert td.is_transit_day("Board the overnight bus to Pokhara")
    assert not td.is_transit_day("Drive to Punakha over the Dochula pass.")


def test_stay_atom_lever_counts_nights_the_rules_missed():
    text = ("Day 1 — Arrival\nWelcome.\n\nDay 2 — Countryside\nVillages.\n\n"
            "Day 3 — Hue to Hoi An\nOvernight in Hoi An.\n\nDay 4 — Depart\nHome.")
    atoms = [{"activity_type": "stay", "itinerary_day": 1, "place": "Hanoi"},
             {"activity_type": "stay", "itinerary_day": 2, "place": "Riverside Homestay", "card_locality": "Mai Chau"},
             {"activity_type": "culture", "itinerary_day": 2, "place": "Temple"}]
    stay = td.stay_atom_overnights(atoms)
    assert stay == {1: "Hanoi", 2: "Mai Chau"}
    rule_only = td.rule_overnights(text)
    assert rule_only == {1: None, 2: None, 3: "Hoi An", 4: None}
    recs = [td.DayRecord(d, "") for d in (1, 2, 3, 4)]
    assert td.stay_lever_fills(recs, rule_only, stay) == 2


# ── graph (TripPlanner test_tour_graph.py fixture, keyed by source_id) ─────────────────────────

DESTS = {
    "paro": {"id": "paro", "name": "Paro", "country": "Bhutan", "lat": 27.43, "lng": 89.41},
    "thimphu": {"id": "thimphu", "name": "Thimphu", "country": "Bhutan", "lat": 27.47, "lng": 89.64},
    "punakha": {"id": "punakha", "name": "Punakha", "country": "Bhutan", "lat": 27.59, "lng": 89.87},
    "phuentsholing": {"id": "phuentsholing", "name": "Phuentsholing", "country": "Bhutan", "lat": 26.85,
                      "lng": 89.39},
    "jaigaon": {"id": "jaigaon", "name": "Jaigaon", "country": "India", "lat": 26.85, "lng": 89.38},
    "leh": {"id": "leh", "name": "Leh", "country": "India", "lat": 34.16, "lng": 77.58},
}


def _row(src, day, overnight=None, component=None):
    return {"source_id": src, "day_index": day, "overnight_destination_id": overnight,
            "component_destination_id": component}


ROWS = [
    _row("t1", 1, "paro"), _row("t1", 2, "thimphu"), _row("t1", 3, "thimphu"), _row("t1", 4, "punakha"),
    _row("t1", 5),
    _row("t2", 1, None, "thimphu"), _row("t2", 2, "punakha", "thimphu"), _row("t2", 3, "paro"),
    _row("t3", 1, "phuentsholing"), _row("t3", 2, "jaigaon"),
]
PLACES = [
    {"source_id": "t1", "day_index": 1, "destination_id": "paro", "activity": "cultural_heritage",
     "intensity_level": "leisurely", "season_months": [3, 4]},
    {"source_id": "t1", "day_index": 4, "destination_id": "punakha", "activity": "trekking",
     "intensity_level": "active", "season_months": [10]},
    {"source_id": "t2", "day_index": 3, "destination_id": "paro", "activity": "trekking",
     "intensity_level": "strenuous", "season_months": [4, 5]},
]


def test_choose_stops_prefers_overnight_then_component_and_skips_empty_days():
    stops = {(s.source_id, s.day_index): s for s in tg.choose_stops(ROWS)}
    assert stops[("t2", 1)].destination_id == "thimphu" and stops[("t2", 1)].source == "component"
    assert stops[("t2", 2)].destination_id == "punakha" and stops[("t2", 2)].source == "overnight"
    assert ("t1", 5) not in stops


def test_edges_skip_stays_count_tours_and_bridge_empty_days():
    edges = {(e["from_destination_id"], e["to_destination_id"]): e
             for e in tg.build_edges(tg.choose_stops(ROWS), DESTS)}
    assert set(edges) == {("paro", "thimphu"), ("thimphu", "punakha"), ("punakha", "paro"),
                          ("phuentsholing", "jaigaon")}
    assert edges[("thimphu", "punakha")]["source_ids"] == ["t1", "t2"]
    assert 20 < edges[("paro", "thimphu")]["km"] < 30
    rows = [_row("t", 1, "paro"), _row("t", 2), _row("t", 3, "punakha")]
    assert [(e["from_destination_id"], e["to_destination_id"])
            for e in tg.build_edges(tg.choose_stops(rows), DESTS)] == [("paro", "punakha")]


def test_legs_are_every_span_between_stop_days():
    legs = {(leg["source_id"], leg["day_from"], leg["day_to"]): leg
            for leg in tg.build_legs(tg.choose_stops(ROWS), DESTS, PLACES)}
    assert sorted(k for k in legs if k[0] == "t1") == [
        ("t1", 1, 2), ("t1", 1, 3), ("t1", 1, 4), ("t1", 2, 3), ("t1", 2, 4), ("t1", 3, 4)]
    whole = legs[("t1", 1, 4)]
    assert whole["days"] == 4 and whole["destination_ids"] == ["paro", "thimphu", "punakha"]
    assert whole["activities"] == ["cultural_heritage", "trekking"]
    assert legs[("t3", 1, 2)]["countries"] == ["Bhutan", "India"]


def test_junctions_symmetric_within_threshold_and_border_flag():
    pairs = {(j["destination_a"], j["destination_b"]): j for j in tg.build_junctions(DESTS, DESTS, 150)}
    assert ("paro", "thimphu") in pairs and ("thimphu", "paro") in pairs
    assert pairs[("phuentsholing", "jaigaon")]["cross_border"] is True
    assert not any("leh" in k for k in pairs)


def test_nodes_and_stats():
    nodes = {n["destination_id"]: n for n in tg.build_nodes(tg.choose_stops(ROWS), DESTS, PLACES)}
    paro = nodes["paro"]
    assert paro["source_ids"] == ["t1", "t2"] and paro["stop_count"] == 2
    assert (paro["intensity_min"], paro["intensity_max"]) == ("leisurely", "strenuous")
    assert paro["season_months"] == [3, 4, 5]
    s = tg.build_graph(ROWS, DESTS, PLACES, 150).stats
    assert (s["tours"], s["stops"], s["stops_from_components"]) == (3, 9, 1)
    assert (s["nodes"], s["edges"], s["cross_border_pairs"]) == (5, 4, 4)


def test_stop_rows_pick_the_main_place_destination_per_day():
    days = [{"source_id": "s", "day_index": 1, "overnight_destination_id": None},
            {"source_id": "s", "day_index": 2, "overnight_destination_id": "paro"}]
    places = [{"source_id": "s", "day_index": 1, "destination_id": "thimphu"},
              {"source_id": "s", "day_index": 1, "destination_id": "punakha"},
              {"source_id": "s", "day_index": 1, "destination_id": "punakha"},
              {"source_id": "s", "day_index": 1, "destination_id": "unknown"}]   # not a usable dest
    rows = tg.stop_rows(days, places, DESTS)
    assert rows[0]["component_destination_id"] == "punakha"
    assert rows[1]["overnight_destination_id"] == "paro"


@pytest.mark.asyncio
async def test_extract_tour_propagates_a_budget_stop():
    # review finding 6: a budget stop must not become a rules-only `partial` build
    from shared.cost_guard import BudgetExceeded

    async def gen(system, user):
        raise BudgetExceeded("bedrock daily budget reached", "bedrock", "per_day")
    with pytest.raises(BudgetExceeded):
        await td.extract_tour({"tour_id": "t1", "aa_name": "", "aa_itineraries": LADAKH}, gen)
