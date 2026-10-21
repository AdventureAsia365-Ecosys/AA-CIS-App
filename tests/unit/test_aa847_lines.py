"""AA-847 (owner decision S246) — linear places are drawn as lines: what the validity branch does with them.

- validity V3: a linear name not stored as a line is `linear_feature` (a pin / circle on the map); a line whose shape
  is not drawable is `linear_without_shape`; both block the flip gate; an ok / manual / approximate shape passes;
- the point-distance same-place rule never applies to a line;
- graph: a line destination is never a stop / node — a river day with no overnight takes its other place, an
  overnight "on the Mekong" falls back to the day's main point place;
- validity_fix.shape_merge: which geo.place_shape row survives a merge;
- store: a line is written geo_status 'ok' (no hidden / suspect path left).
"""
from __future__ import annotations

import inspect

import pytest

from services.places import graph, store, validity as v, validity_fix


def _comp(src, day, name, dest, lat, lng, precision="point", shape=None, rt=None):
    return {"source_id": src, "day_index": day, "name": name, "name_norm": name.lower(), "destination_id": dest,
            "destination_name": name, "country": "Laos", "lat": lat, "lng": lng, "located_by": "geoapify",
            "precision": precision, "review_reason": None, "result_type": rt, "shape_status": shape}


TOUR = {"source_id": "s1", "tour_id": "t1", "title": "Mekong slow boat", "country": "Laos", "duration_raw": "2 days",
        "itinerary": "Day 1 — Huay Xai\nBoard the slow boat on the Mekong River.\nDay 2 — Pak Beng\nNam Ou River."}


@pytest.mark.parametrize("precision,shape,reason", [
    ("point", None, "linear_feature"),            # stored as a pin
    ("area", None, "linear_feature"),             # stored as a circle
    ("line", None, "linear_without_shape"),       # no place_shape row
    ("line", "pending", "linear_without_shape"),
    ("line", "not_found", "linear_without_shape"),
    ("line", "failed", "linear_without_shape"),
    ("line", "ok", None),
    ("line", "manual", None),
    ("line", "approximate", None),                # anchored "approximate route" counts as drawn (open question)
])
def test_v3_line_reasons(precision, shape, reason):
    items = v.evaluate([TOUR], [_comp("s1", 1, "Mekong River", "m1", 20.27, 100.41, precision, shape)])
    got = [i.reason for i in items if i.check == v.V3]
    assert got == ([reason] if reason else [])
    gate = v.summarize([TOUR], items)["catalog"]["gate"]
    assert gate["pass"] is (reason is None)


def test_a_line_named_without_a_linear_word_still_needs_its_shape():
    """An admin-set or OSM-confirmed line whose name has no linear word ("Tea Horse"): precision decides."""
    items = v.evaluate([TOUR], [_comp("s1", 1, "Tea Horse", "x1", 20.0, 101.0, "line", "pending")])
    assert [i.reason for i in items if i.check == v.V3] == ["linear_without_shape"]


def test_gate_lists_the_line_reasons():
    assert "linear_without_shape" in v.GATE_REASONS and "linear_feature" in v.GATE_REASONS
    assert set(v.GATE_REASONS) <= {r for rs in v.REASONS.values() for r in rs}


def test_point_distance_rule_never_merges_a_line():
    a = v.Place("m1", "Mekong River", "Laos", 19.89, 102.13, "geoapify", "line")
    b = v.Place("m2", "Mekong", "Laos", 19.89, 102.13, "llm", "line")
    c = v.Place("m3", "Mekong River", "Laos", 19.89, 102.13, "geoapify", "point")
    assert v.is_same_place(a, b) == (False, None)
    assert v.is_same_place(a, c) == (False, None)
    assert v.clusters([a, b, c]) == []


# ── graph ────────────────────────────────────────────────────────────────────────────────────────────────────────────

DESTS = {
    "huayxai": {"name": "Huay Xai", "country": "Laos", "lat": 20.27, "lng": 100.41, "precision": "point"},
    "pakbeng": {"name": "Pak Beng", "country": "Laos", "lat": 19.89, "lng": 101.13, "precision": "point"},
    "pakou": {"name": "Pak Ou Caves", "country": "Laos", "lat": 20.05, "lng": 102.22, "precision": "point"},
    "mekong": {"name": "Mekong River", "country": "Laos", "lat": 20.0, "lng": 101.0, "precision": "line"},
    "lp": {"name": "Luang Prabang", "country": "Laos", "lat": 19.89, "lng": 102.13, "precision": "point"},
}


def _place(day, dest, src="s1", activity="water_activities"):
    return {"source_id": src, "day_index": day, "destination_id": dest, "activity": activity,
            "intensity_level": "moderate", "season_months": [1]}


def test_river_day_without_overnight_takes_its_point_place():
    places = [_place(1, "mekong"), _place(1, "mekong"), _place(1, "pakou")]
    assert graph.main_component_destinations(places, DESTS) == {("s1", 1): "pakou"}


def test_river_only_day_has_no_stop():
    days = [{"source_id": "s1", "day_index": 1, "overnight_destination_id": None}]
    rows = graph.stop_rows(days, [_place(1, "mekong")], DESTS)
    assert graph.choose_stops(rows) == []


def test_night_on_the_mekong_falls_back_to_the_day_point():
    days = [{"source_id": "s1", "day_index": 1, "overnight_destination_id": "huayxai"},
            {"source_id": "s1", "day_index": 2, "overnight_destination_id": "mekong"},
            {"source_id": "s1", "day_index": 3, "overnight_destination_id": "lp"}]
    places = [_place(2, "mekong"), _place(2, "pakbeng")]
    stops = graph.choose_stops(graph.stop_rows(days, places, DESTS))
    assert [(s.day_index, s.destination_id, s.source) for s in stops] == [
        (1, "huayxai", "overnight"), (2, "pakbeng", "component"), (3, "lp", "overnight")]
    g = graph.build_graph(graph.stop_rows(days, places, DESTS), DESTS, places)
    assert "mekong" not in {n["destination_id"] for n in g.nodes}
    assert all("mekong" not in leg["destination_ids"] for leg in g.legs)


def test_build_graph_drops_a_line_stop_even_if_a_caller_passes_one():
    rows = [{"source_id": "s1", "day_index": 1, "overnight_destination_id": "mekong", "component_destination_id": None}]
    assert graph.build_graph(rows, DESTS, []).stops == []


# ── merge of shapes ──────────────────────────────────────────────────────────────────────────────────────────────────

def _shape(status, dest="x"):
    return {"status": status, "row": {"destination_id": dest, "status": status}}


@pytest.mark.parametrize("keeper,dup,expected", [
    (None, None, {}),
    (_shape("ok", "k"), None, {}),
    (None, _shape("ok", "d"), {"moved": True}),
    (None, _shape("pending", "d"), {"moved": True}),
    (_shape("ok", "k"), _shape("ok", "d"), {"deleted_dup": {"destination_id": "d", "status": "ok"}}),
    (_shape("pending", "k"), _shape("pending", "d"), {"deleted_dup": {"destination_id": "d", "status": "pending"}}),
    (_shape("approximate", "k"), _shape("ok", "d"), {"deleted_dup": {"destination_id": "d", "status": "ok"}}),
    (_shape("pending", "k"), _shape("ok", "d"),
     {"deleted_keeper": {"destination_id": "k", "status": "pending"}, "moved": True}),
    (_shape("not_found", "k"), _shape("manual", "d"),
     {"deleted_keeper": {"destination_id": "k", "status": "not_found"}, "moved": True}),
])
def test_shape_merge(keeper, dup, expected):
    assert validity_fix.shape_merge(keeper, dup) == expected


# ── store ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_store_never_hides_a_line():
    assert "suspect" not in store._DEST_INSERT_SQL
    src = inspect.getsource(store._write_point)
    assert "THEN 'suspect'" not in src and "'linear_feature'" in src     # only the un-hide of an old row

    class L:
        precision, meta, review_reason = "line", {"provider": "geoapify"}, None
    assert store._geo_args(L()) == ("line", "geoapify", '{"provider": "geoapify"}', None)


def test_pending_shape_sql_only_for_a_live_line():
    sql = store._PENDING_SHAPE_SQL
    assert "geo_precision = 'line'" in sql and "duplicate_of IS NULL" in sql and "ON CONFLICT" in sql
    assert "'pending'" in sql


def test_outlier_rule_skips_lines():
    assert "IS DISTINCT FROM 'line'" in inspect.getsource(store.night_stops)


def test_credit_guard_counts_credits_of_every_endpoint():
    src = inspect.getsource(store.geocoder_requests_today)
    assert "coalesce(credits, requests)" in src and "endpoint =" not in src
