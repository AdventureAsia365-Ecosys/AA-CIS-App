"""AA-990 N1 (S255) — the tour-level transit rule (no DB, no model).

Live cases (docs/audits/2026-10-27-S254-aa990-close/transit-atoms.txt, root repo): the 14-day "Seoul to Busan" cycling
tour (11 atoms, 10 typed transit with "go" / "continue" / "move … by transportation", one culture atom) and the 2-day
"Oze Katashina to Nikko" drive (8 atoms, all transit: "start", "continue for approx. 13 km", "travel to"). The per-atom
journey rule (CIS #1089) keeps neither: their verbs are not self-propelled. Every overnight town was resolved, yet the
place build left 1 of 14 days and 0 of 2 days with a component, so TripPlanner's S236 rule (a route pins at least half
of the tour's days) refused both routes. The fixtures below copy the atoms' place, action and place-card fields.
"""
from __future__ import annotations

import pytest

from services.places import components as cm
from services.places import days as days_mod
from services.places import names


def _atom(i, day, place, action, at="transit", card=None, **kw):
    a = {"atom_id": f"atom_{i:02d}", "itinerary_day": day, "place": place, "action": action, "activity_type": at,
         "text": f"{place} — {action}", **kw}
    if card:
        a.update(card_id=f"card_{i:02d}", card_display_name=card[0], card_locality=card[1])
    return a


def seoul_busan_atoms():
    """90b88118 (South Korea, 14 days): A3 output as stored on Dev, S254."""
    legs = [
        (2, "Seoul to Siheung city", "go", ("Siheung", "Siheung")),
        (3, "Siheung to the Asan hot spring town", "continue", ("Onyang Hot Spring", "Asan")),
        (4, "Asan to Buyeo city", "go", ("Buyeo County", "Buyeo-eup")),
        (6, "Buyeo to Gunsan city", "go", ("Gunsan", "Gunsan")),
        (7, "Gunsan to Byunsan National Park", "go", ("Byeonsanbando National Park", "Buan")),
        (8, "Byunsan to Naebyunsan National Park", "continue", ("Naebyeonsan (Inner Byeonsan)", "Buan")),
        (10, "Naejangsan to Damyang county", "go", ("Damyang", None)),
        (11, "Damyang to Gurye county", "go", ("Gurye County", None)),
        (12, "Gurye to Hadong county", "go", ("Hadong County", None)),
        (13, "Busan city", "move to Busan city by transportation", ("Busan", "Busan")),
    ]
    atoms = [_atom(i, day, place, action, card=card) for i, (day, place, action, card) in enumerate(legs)]
    atoms.append(_atom(20, 5, "Buyeo", "take a cultural tour", at="culture", card=("Buyeo County", "Buyeo-eup")))
    return atoms


def oze_nikko_atoms():
    """2020d00f (Japan, 2 days): every atom typed transit, the route's sights are the legs' places."""
    legs = [
        (1, "Roadside Station Oze Katashina", "start", ("Roadside Station Oze Katashina", "Katashina")),
        (1, "Roadside Station Oze Katashina to the Nikko-Shiranesan Ropeway",
         "continue for approx. 13 km (25 minutes)", ("Nikko-Shiranesan Ropeway", "Katashina")),
        (1, "the Nikko-Shiranesan Ropeway to Marunuma Dam", "continue for approx. 2 km (5 minutes)",
         ("Nikko-Shiranesan Ropeway", "Katashina")),
        (1, "Marunuma Dam to Marunuma", "continue for approx. 3 km (6 minutes)", ("Lake Marunuma", "Katashina")),
        (2, "Yu-daki Falls", "travel to", ("Yudaki Falls", "Nikko")),
        (2, "the Senjogahara Walk", "travel to", ("Senjogahara Marshland", "Nikko")),
        (2, "Kegon Falls", "travel to", ("Kegon Falls", "Nikko")),
        (2, "the Roadside station of Nikko Nikko kaido nikoniko honjin", "travel to",
         ("Roadside Station Nikko", "Nikko")),
    ]
    return [_atom(i, day, place, action, card=card) for i, (day, place, action, card) in enumerate(legs)]


def _pinned_share_ok(comps, tour_days):
    """TripPlanner's S236 bar (backend/browse/routes.py): pinned days >= PLACES_MIN_DAY_SHARE * tour days."""
    return len({c.day_index for c in comps}) >= cm.ROUTE_MIN_DAY_SHARE * tour_days


def test_seoul_busan_is_routed_on_its_legs():
    comps, counters = cm.build_components("90b88118", "South Korea", seoul_busan_atoms(), {}, tour_days=14)
    days = {c.day_index for c in comps}
    assert days == {2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13}
    assert _pinned_share_ok(comps, 14)
    assert counters["transit_route_legs"] == 10 and counters["route_leg_days"] == 10
    assert "transit" not in counters["skipped"]
    # the culture day keeps its own place; a leg links to its place card
    assert [c.activity for c in comps if c.day_index == 5] == ["cultural_heritage"]
    ride = next(c for c in comps if c.day_index == 2)
    assert ride.name == "Seoul to Siheung city" and ride.link_name == "Siheung"
    assert ride.place_card_id == "card_00" and ride.activity == "local_immersion"
    assert ride.component_id == names.component_id("90b88118", 2, "Seoul to Siheung city", "South Korea",
                                                   "local_immersion")
    # the leg's own words pick the activity, never the motorised mode
    assert next(c for c in comps if c.day_index == 7).activity == "wildlife_nature"


def test_oze_nikko_is_routed_on_its_legs():
    comps, counters = cm.build_components("2020d00f", "Japan", oze_nikko_atoms(), {1: "Marunuma Onsen"}, tour_days=2)
    assert {c.day_index for c in comps} == {1, 2}
    assert _pinned_share_ok(comps, 2)
    assert counters["transit_route_legs"] == 8 and counters["route_leg_days"] == 2
    assert counters["skipped"] == {}
    assert {c.link_name for c in comps if c.day_index == 2} == {
        "Yudaki Falls", "Senjogahara Marshland", "Kegon Falls", "Roadside Station Nikko"}
    assert next(c for c in comps if c.name == "the Senjogahara Walk").activity == "trekking"


def test_a_tour_whose_own_places_cover_half_its_days_keeps_skipping_legs():
    """A normal tour: its sights cover the days, its transfers stay logistics (no new planner experiences)."""
    atoms = [_atom(1, 1, "Colombo", "explore", at="culture"), _atom(2, 2, "Kandy to Nuwara Eliya", "drive"),
             _atom(3, 2, "Temple of the Tooth", "visit", at="culture"), _atom(4, 3, "Nuwara Eliya", "transfer"),
             _atom(5, 4, "Horton Plains", "hike", at="trek")]
    comps, counters = cm.build_components("src", "Sri Lanka", atoms, {}, tour_days=4)
    assert {c.day_index for c in comps} == {1, 2, 4}
    assert counters["transit_route_legs"] == 0 and counters["skipped"] == {"transit": 2}


def test_legs_are_not_pinned_when_they_cannot_lift_the_tour_to_a_route():
    """One sight and one leg out of 10 days: still no route, so no leg becomes a planner experience."""
    atoms = [_atom(1, 3, "Pokhara", "explore", at="culture"), _atom(2, 2, "Kathmandu to Pokhara", "drive")]
    comps, counters = cm.build_components("src", "Nepal", atoms, {}, tour_days=10)
    assert [c.day_index for c in comps] == [3]
    assert counters["transit_route_legs"] == 0 and counters["skipped"] == {"transit": 1}


def test_a_covered_day_keeps_its_place_and_skips_its_legs():
    atoms = [_atom(1, 1, "Paro to Thimphu", "drive"), _atom(2, 1, "Tashichho Dzong", "visit", at="culture"),
             _atom(3, 2, "Thimphu to Punakha", "drive"), _atom(4, 3, "Punakha to Paro", "drive"),
             _atom(5, 4, "Paro to the airport", "drive")]
    comps, counters = cm.build_components("src", "Bhutan", atoms, {}, tour_days=4)
    assert {(c.day_index, c.name) for c in comps} == {
        (1, "Tashichho Dzong"), (2, "Thimphu to Punakha"), (3, "Punakha to Paro")}
    # without a card, a leg links to its arrival
    assert {c.link_name for c in comps if c.day_index in (2, 3)} == {"Punakha", "Paro"}
    # day 1 is covered; "Paro to the airport" is a logistics place (the noun rule wins)
    assert counters["skipped"] == {"transit": 1, "logistics_place": 1}
    assert counters["route_leg_days"] == 2


def test_logistics_and_unnamed_legs_stay_skipped_on_a_transit_tour():
    atoms = [_atom(1, 1, "Incheon Airport", "arrive"), _atom(2, 1, "your hotel", "transfer"),
             _atom(3, 2, "Seoul to Busan", "travel by KTX")]
    comps, counters = cm.build_components("src", "South Korea", atoms, {}, tour_days=2)
    assert [c.name for c in comps] == ["Seoul to Busan"]
    assert counters["skipped"] == {"logistics_place": 1, "unnamed": 1}


def test_the_tour_day_count_comes_from_the_days_when_not_given():
    # day_towns reach day 6: 1 leg day of 6 cannot reach the bar
    atoms = [_atom(1, 1, "Hanoi to Ha Long Bay", "drive")]
    comps, _ = cm.build_components("src", "Vietnam", atoms, {d: "Hanoi" for d in range(1, 7)})
    assert comps == []
    comps, _ = cm.build_components("src", "Vietnam", atoms, {1: "Ha Long"})
    assert [c.link_name for c in comps] == ["Ha Long Bay"]


@pytest.mark.parametrize("covered,legs,tour_days,expected", [
    (set(), {1: [1], 2: [1]}, 2, {1, 2}),
    ({5}, {d: [1] for d in (2, 3, 4, 6, 7, 8, 10, 11, 12, 13)}, 14, {2, 3, 4, 6, 7, 8, 10, 11, 12, 13}),
    ({1, 2}, {3: [1]}, 4, set()),            # already at the bar
    ({1}, {2: [1]}, 6, set()),               # legs cannot lift it to the bar
    ({1}, {1: [1], 2: [1]}, 4, {2}),         # a covered day's legs never count
    (set(), {}, 0, set()),
])
def test_route_leg_days(covered, legs, tour_days, expected):
    assert cm.route_leg_days(covered, legs, tour_days) == expected


def test_a_to_b():
    assert days_mod.a_to_b("Seoul to Siheung city") == ("Seoul", "Siheung city")
    assert days_mod.a_to_b("the Nikko-Shiranesan Ropeway to Marunuma Dam") == (
        "the Nikko-Shiranesan Ropeway", "Marunuma Dam")
    assert days_mod.a_to_b("Kegon Falls") is None
    assert cm.leg_link_name("Kegon Falls") == "Kegon Falls"


def test_rule_version_bumped_for_the_leg_rule():
    assert cm.COMPONENTS_RULE_VERSION >= 3
