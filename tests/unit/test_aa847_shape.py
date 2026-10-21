"""AA-847 (owner decision S246, D1) — line geometry rules (services/places/shape.py): country polygons, clipping,
simplification within the migration-255 CHECKs, acceptance, the per-day stretch, the anchored approximate route.
Recorded-format fixtures only (tests/unit/fixtures/aa847_shapes/), no network."""
from __future__ import annotations

import json
import os

import pytest

from services.places import shape as S

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "aa847_shapes")
MEKONG_A = [(100.41, 20.27), (100.55, 20.15), (100.75, 20.05), (100.95, 19.95), (101.13, 19.89)]
MEKONG_B = [(101.13, 19.89), (101.40, 19.85), (101.70, 19.95), (101.95, 20.00), (102.13, 19.89)]
MEKONG = [MEKONG_A + MEKONG_B[1:]]
HUAY_XAI = ("hx", 20.27, 100.41)
PAK_BENG = ("pb", 19.89, 101.13)
LUANG_PRABANG = ("lp", 19.89, 102.13)
NONG_KHIAW = ("nk", 20.57, 102.61)


# ── country polygons (not the bbox) ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lat,lng,iso,inside", [
    (19.89, 102.13, "la", True),    # Luang Prabang
    (19.89, 102.13, "th", False),   # … inside Thailand's BBOX (guards.BBOX) but not in Thailand
    (17.41, 102.79, "la", False),   # Udon Thani
    (17.41, 102.79, "th", True),
    (17.97, 102.60, "th", True),    # Vientiane is on the Mekong border: within the 3 km border buffer of Thailand
    (25.03, 121.56, "tw", True),
    (25.03, 121.56, "cn", False),
    (22.82, 104.98, "vn", True),    # Ha Giang
    (0.0, 0.0, "la", False),
    (19.89, 102.13, "xx", False),   # unknown country
])
def test_in_country_by_polygon(lat, lng, iso, inside):
    assert S.in_country(lat, lng, iso) is inside


def test_bbox_would_have_accepted_luang_prabang_for_thailand():
    """Why polygons: the rectangle guard says Thailand for Luang Prabang."""
    from services.places import guards
    lo_lat, hi_lat, lo_lng, hi_lng = guards.BBOX["th"]
    assert lo_lat <= 19.89 <= hi_lat and lo_lng <= 102.13 <= hi_lng


def test_every_catalog_country_has_a_polygon():
    from services.places import guards
    assert set(S.country_isos()) == set(guards.ISO2_COUNTRY)


def test_clip_cuts_at_the_border():
    """Vientiane → Udon Thani: only the piece in Laos (+ the 3 km border buffer) is kept."""
    road = [[(102.60, 17.97), (102.70, 17.70), (102.79, 17.41)]]
    clipped = S.clip_to_country(road, "la")
    assert len(clipped) == 1
    assert 5 < S.length_km(clipped) < 35 < S.length_km(road)
    assert clipped[0][0] == (102.60, 17.97)


def test_clip_drops_a_line_outside_the_country():
    assert S.clip_to_country([[(110.29, 25.27), (110.5, 24.8)]], "la") == []   # Li River, China


# ── GeoJSON, measures, simplification ────────────────────────────────────────────────────────────────────────────

def test_geojson_round_trip_and_multi():
    g = S.to_geojson([MEKONG_A, MEKONG_B])
    assert g["type"] == "MultiLineString"
    assert S.parts_from_geojson(g) == [MEKONG_A, MEKONG_B]
    assert S.to_geojson(MEKONG)["type"] == "LineString"
    assert S.parts_from_geojson({"type": "Point", "coordinates": [1, 2]}) == []
    assert S.parts_from_geojson({"type": "LineString", "coordinates": [[1, 2]]}) == []
    assert S.parts_from_geojson({"type": "LineString", "coordinates": "bad"}) == []


def _zigzag(n):
    return [[(100.0 + i * 0.001, 20.0 + (0.002 if i % 2 else 0.0)) for i in range(n)]]


def test_levels_fit_the_migration_255_checks():
    geom, mid, lo = S.levels(_zigzag(5000))
    assert 2 <= S.vertex_count(S.parts_from_geojson(geom)) <= S.MAX_VERTICES
    assert 2 <= S.vertex_count(S.parts_from_geojson(mid)) <= S.MID_VERTICES
    assert S.json_bytes(mid) <= S.MID_BYTES
    assert 2 <= S.vertex_count(S.parts_from_geojson(lo)) <= S.LO_VERTICES


def test_simplify_keeps_a_small_line_as_is():
    assert S.simplify(MEKONG, S.LO_VERTICES) == MEKONG


def test_json_bytes_measures_like_postgres_jsonb_text():
    g = {"type": "LineString", "coordinates": [[100.1, 20.2], [100.3, 20.4]]}
    assert S.json_bytes(g) == len('{"type": "LineString", "coordinates": [[100.1, 20.2], [100.3, 20.4]]}')


def test_project_and_substring():
    p = S.project(MEKONG, 19.89, 101.13)
    assert p.km < 0.01 and p.part == 0
    total = S.length_km(MEKONG)
    assert abs(S.part_length_km(S.substring(MEKONG[0], 0, total)) - total) < 0.01
    back = S.substring(MEKONG[0], 50, 10)
    assert abs(S.part_length_km(back) - 40) < 0.5
    assert S.km(back[0][1], back[0][0], MEKONG[0][0][1], MEKONG[0][0][0]) > \
        S.km(back[-1][1], back[-1][0], MEKONG[0][0][1], MEKONG[0][0][0])        # reversed: starts farther


# ── names ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query,names,ok", [
    ("Mekong River", ["ແມ່ນ້ຳຂອງ", "Mekong"], True),
    ("Mekong", ["Sông Mê Kông"], False),                 # transliteration differs: needs name:en / int_name
    ("Nam Ou River", ["ນ້ຳອູ", "Nam Ou"], True),
    ("Nam Ou River", ["Nam Khan"], False),                 # both "nam": the region word is not distinctive
    ("Nam Ou", ["Nam Ou Bridge Road"], False),             # extra distinctive words: another feature
    ("Ho Chi Minh Trail", ["Đường Hồ Chí Minh"], True),    # "đường" = road
    ("Li River", ["Li Jiang", "漓江"], True),
    ("Karakoram Highway", ["Karakoram Hwy"], True),
    ("Annapurna Circuit", ["Annapurna Circuit Trek"], True),      # an activity word ("trek") is not distinctive
])
def test_names_match(query, names, ok):
    assert (S.names_match(query, names) >= S.NAME_SIM_MIN) is ok


# ── acceptance ───────────────────────────────────────────────────────────────────────────────────────────────────

def _cand(parts, names, osm_type="way", osm_id=1, tags=None, way_ids=None, member_of=None):
    return S.LineCandidate(parts, names, osm_type, osm_id, tags or {"waterway": "river"}, "overpass",
                           member_of or [], way_ids or [])


ANCHORS = [(HUAY_XAI[1], HUAY_XAI[2]), (PAK_BENG[1], PAK_BENG[2])]


def test_accept_a_river_relation():
    v = S.accept("Mekong River", [_cand(MEKONG, ["Mekong"], "relation", 9, {"type": "waterway",
                                                                             "waterway": "river"})], ANCHORS, "la")
    assert v.accepted is not None and v.length_km > 150


@pytest.mark.parametrize("cand,anchors,reason", [
    (lambda: _cand(MEKONG, ["Nam Khan"]), ANCHORS, "name"),
    (lambda: _cand(MEKONG, ["Mekong"], tags={"highway": "primary"}), ANCHORS, "kind"),
    (lambda: _cand([[(110.29, 25.27), (110.5, 24.8)]], ["Mekong"]), ANCHORS, "country"),
    (lambda: _cand(MEKONG, ["Mekong"]), [(17.41, 102.79)], "distance"),            # Udon Thani: > 50 km
    (lambda: _cand([[(102.13, 19.89), (102.135, 19.892)]], ["Mekong"]), [(19.89, 102.13)], "length"),
    (lambda: _cand([[(102.13, 19.89)]], ["Mekong"]), ANCHORS, "geometry"),
])
def test_accept_rejections(cand, anchors, reason):
    v = S.accept("Mekong River", [cand()], anchors, "la")
    assert v.accepted is None and v.reasons == [f"0:{reason}"]


def test_a_single_way_of_a_relation_backed_river_loses_to_the_relation():
    way = _cand([MEKONG_A], ["Mekong"], "way", 11, member_of=[9])
    rel = _cand(MEKONG, ["Mekong"], "relation", 9, {"type": "waterway", "waterway": "river"})
    v = S.accept("Mekong River", [way, rel], ANCHORS, "la")
    assert v.accepted is rel and "0:not_preferred" in v.reasons


def test_merged_ways_beat_a_single_way():
    single = _cand([MEKONG_A], ["Mekong"], "way", 11)
    merged = _cand(MEKONG, ["Mekong"], "way", 12, way_ids=[11, 12])
    assert S.accept("Mekong River", [single, merged], ANCHORS, "la").accepted is merged


def test_kind_aware_minimum_length():
    """A ~1 km kora passes (circuit 0.5 km); the same length fails for a river (5 km)."""
    short = [[(91.1315, 29.6530), (91.1330, 29.6560), (91.1360, 29.6555)]]      # Barkhor, Lhasa (~0.6 km)
    kora = S.accept("Barkhor Circuit", [_cand(short, ["Barkhor Circuit"], tags={"highway": "footway"})],
                    [(29.6545, 91.1325)], "cn")
    assert kora.accepted is not None
    river = S.accept("Lhasa River", [_cand(short, ["Lhasa River"])], [(29.6545, 91.1325)], "cn")
    assert river.reasons == ["0:length"]


def test_merge_ways_chains_connected_same_name_ways():
    a = _cand([MEKONG_A], ["Mekong"], "way", 11)
    b = _cand([list(reversed(MEKONG_B))], ["Mekong"], "way", 12)      # drawn the other way
    other = _cand([[(102.14, 19.88), (102.55, 19.62)]], ["Nam Khan"], "way", 31)
    merged = S.merge_ways([a, b, other])
    mek = [c for c in merged if "Mekong" in c.names][0]
    assert len(mek.parts) == 1 and len(mek.parts[0]) == len(MEKONG[0]) and sorted(mek.way_ids) == [11, 12]
    assert any(c.names == ["Nam Khan"] for c in merged)


# ── the per-day stretch (geo.tour_segment) ───────────────────────────────────────────────────────────────────────

def test_day_between_two_stops_on_the_river():
    """Huay Xai → Pak Beng slow boat: the substring between the two stops."""
    seg = S.day_segment(MEKONG, HUAY_XAI, PAK_BENG)
    assert seg.basis == "day_anchors" and seg.from_destination_id == "hx" and seg.to_destination_id == "pb"
    assert 80 < seg.segment_km < 95
    assert seg.segment["coordinates"][0] == [100.41, 20.27] and seg.segment["coordinates"][-1] == [101.13, 19.89]


def test_out_and_back_gets_a_window_not_the_whole_river():
    seg = S.day_segment(MEKONG, PAK_BENG, PAK_BENG)
    assert seg.basis == "anchor_window" and seg.segment_km <= 2 * S.WINDOW_KM + 0.5


def test_one_stop_on_the_river():
    seg = S.day_segment(MEKONG, NONG_KHIAW, LUANG_PRABANG)      # Nong Khiaw is ~85 km off the Mekong
    assert seg.basis == "anchor_window" and seg.from_destination_id == seg.to_destination_id == "lp"


def test_no_stop_near_the_line_is_the_feature():
    seg = S.day_segment(MEKONG, NONG_KHIAW, None)
    assert (seg.basis, seg.segment, seg.segment_km) == ("feature", None, None)


def test_no_shape_yet_keeps_the_stops_for_the_provisional_line():
    seg = S.day_segment([], HUAY_XAI, PAK_BENG)
    assert seg.basis == "feature" and (seg.from_destination_id, seg.to_destination_id) == ("hx", "pb")


def test_segment_fits_the_check():
    long = [[(100.0 + i * 0.0005, 20.0 + (0.0005 if i % 2 else 0)) for i in range(4000)]]
    seg = S.day_segment(long, ("a", 20.0, 100.0), ("b", 20.0, 101.99))
    assert seg.basis == "day_anchors" and len(seg.segment["coordinates"]) <= S.SEGMENT_VERTICES


# ── approximate route from anchors ───────────────────────────────────────────────────────────────────────────────

def test_anchored_line_needs_two_distinct_anchors():
    assert S.anchored_line([(22.82, 104.98), (23.06, 105.03), (23.27, 105.32)], "vn")          # Ha Giang Loop
    assert S.anchored_line([(22.82, 104.98), (22.8205, 104.9805)], "vn") == []
    assert S.anchored_line([(22.82, 104.98)], "vn") == []


def test_anchored_line_drops_points_outside_the_country():
    parts = S.anchored_line([(22.82, 104.98), (17.41, 102.79), (23.27, 105.32)], "vn")
    assert len(parts[0]) == 2


def test_fixture_files_are_valid_json():
    for f in os.listdir(FIX):
        with open(os.path.join(FIX, f), encoding="utf-8") as fh:
            json.load(fh)
