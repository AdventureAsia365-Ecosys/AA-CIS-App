"""AA-990 N1 (S254) — a self-propelled journey typed 'transit' is pinned (no DB, no model).

Live case: the 14-day "Seoul to Busan" cycling tour (atoms 11, skipped transit 10, one place) and the 2-day Oze-Nikko
walk (atoms 8, skipped transit 8, no place) had every overnight town resolved and still no planner route: A3 atomize
types the day's ride / walk between two towns 'transit', and the place pipeline dropped every transit atom.
"""
from __future__ import annotations

import pytest

from services.places import components as cm
from services.places import names


def _atom(i, day, place, action, at="transit", **kw):
    return {"atom_id": f"atom_{i}", "itinerary_day": day, "place": place, "action": action, "activity_type": at,
            "text": f"{place} — {action}", **kw}


@pytest.mark.parametrize("action,expected", [
    ("cycle", "adventure_sport"),
    ("cycle along the Han River bike path", "adventure_sport"),
    ("Continue cycling to the coast", "adventure_sport"),
    ("bike through rice fields", "adventure_sport"),
    ("mountain bike down the valley", "adventure_sport"),
    ("ride e-bikes to the lake", "adventure_sport"),
    ("travel by bicycle", "adventure_sport"),
    ("pedal along the canal", "adventure_sport"),
    ("ski across the plateau", "adventure_sport"),
    ("walk", "trekking"),
    ("walk the Senjogahara boardwalk", "trekking"),
    ("hike over the pass", "trekking"),
    ("trek to the next village", "trekking"),
    ("set off hiking at dawn", "trekking"),
    ("continue on foot", "trekking"),
    ("paddle downstream", "water_activities"),
    ("kayak across the bay", "water_activities"),
    ("cross the lake by canoe", "water_activities"),
    ("raft the rapids", "water_activities"),
])
def test_journey_activity_self_propelled(action, expected):
    assert cm.journey_activity(action) == expected


@pytest.mark.parametrize("action", [
    "", None, "drive", "transfer to Busan", "travel by bus", "take the train", "fly to Jeju", "board the ferry",
    "ride the cable car", "ride the shinkansen", "arrive", "depart", "visit", "skip the queue", "check in",
    "travel", "continue by car", "take a boat ride",
])
def test_journey_activity_motorised_or_not_a_journey(action):
    assert cm.journey_activity(action) is None


def test_transit_still_skipped_unless_self_propelled():
    assert cm.skip_reason("Busan", "transit", "transfer") == "transit"
    assert cm.skip_reason("Busan", "transit") == "transit"                    # no action: unchanged
    assert cm.skip_reason("Seoul to Siheung", "transit", "cycle") is None
    assert cm.map_activity("transit", "Seoul to Siheung — cycle", "cycle") == "adventure_sport"
    assert cm.map_activity("transit", "x") is None                           # the old signature: unchanged
    # the logistics-noun rule still wins: a ride to a bus terminal is not a place to pin
    assert cm.skip_reason("Hadong Bus Terminal", "transit", "cycle") == "logistics_place"
    # a non-transit type ignores the journey rule (bike / trek keep their own mapping)
    assert cm.map_activity("trek", "Stok to Sakti — walk", "walk") == "trekking"


def test_action_read_from_text_when_the_column_is_absent():
    assert cm.atom_action({"place": "Senjogahara", "text": "Senjogahara — walk the boardwalk"}) == "walk the boardwalk"
    assert cm.atom_action({"place": "X", "action": "cycle", "text": "X — drive"}) == "cycle"
    assert cm.atom_action({"place": "X", "text": "something else"}) == ""
    a = {"atom_id": "a1", "itinerary_day": 2, "place": "Senjogahara", "activity_type": "transit",
         "text": "Senjogahara — walk the boardwalk"}
    comps, counters = cm.build_components("src", "Japan", [a])
    assert [c.activity for c in comps] == ["trekking"] and counters["transit_journeys"] == 1


def test_cycling_tour_gets_a_place_on_every_riding_day():
    """The live shape of 90b88118: one ride per day typed transit, a transfer and an arrival typed transit."""
    rides = ["Seoul to Siheung", "Siheung to Asan", "Asan to Buyeo", "Buyeo to Gunsan", "Gunsan to Byunsan",
             "Byunsan to Naebyunsan", "Naejangsan to Damyang", "Damyang to Gurye", "Gurye to Hadong"]
    atoms = [_atom(i, i + 2, place, "cycle") for i, place in enumerate(rides)]
    atoms += [_atom(20, 13, "Busan", "transfer"), _atom(21, 1, "Incheon Airport", "arrive"),
              _atom(22, 5, "Buyeo", "explore", at="culture")]
    comps, counters = cm.build_components("90b88118", "South Korea", atoms, {})
    days = {c.day_index for c in comps}
    assert days == set(range(2, 11)) | {5}
    assert len(days) * 2 >= 14                                 # TripPlanner's S236 rule: >= half of the 14 days
    assert counters["skipped"] == {"transit": 1, "logistics_place": 1}
    assert counters["transit_journeys"] == len(rides)
    ride = next(c for c in comps if c.name == "Seoul to Siheung")
    assert ride.activity == "adventure_sport" and ride.intensity_level == "strenuous"
    assert ride.component_id == names.component_id("90b88118", 2, "Seoul to Siheung", "South Korea",
                                                   "adventure_sport")


def test_components_hash_carries_the_components_rule_version(monkeypatch):
    atoms = [_atom(1, 1, "Seoul to Siheung", "cycle")]
    before = cm.components_hash(atoms)
    monkeypatch.setattr(cm, "COMPONENTS_RULE_VERSION", cm.COMPONENTS_RULE_VERSION + 1)
    assert cm.components_hash(atoms) != before          # a rule change makes every built tour stale once
