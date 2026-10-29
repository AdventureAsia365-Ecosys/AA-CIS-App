"""AA-997 S258 — strict acceptance rule aa997.3 (the P4 tune). The S257 re-audit's P4 gate failed: the strict rule
aa997.2 refused 34 / 251 verified-correct Geoapify pins (off_route 17, ambiguous_route 10, site_type 4, name 3; gate
<= 2 %). aa997.3 keeps aa997.2's checks but (a) takes route evidence only from routes that hold the place's own day
stop (an arrival / departure day, a flight, an unlocated trek night or the place itself being the night's stop leave
a route of the neighbouring days only), (b) uses the homonym distance for the re-judge's off-route check, and (c) lists
v1's name / type checks as unjudged on a re-judge. aa997.2 is unchanged. Coordinates are approximate real places;
candidates are synthetic; no live call."""
from __future__ import annotations

import json
import pickle
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.places import geocoder as gc
from services.places import geomatch as gm
from services.places import guards, relocate, store

V2, V3 = gm.ACCEPT_RULE, gm.ACCEPT_RULE_V3
XIAN, PINGYAO, BEIJING = (34.26, 108.94), (37.20, 112.18), (39.90, 116.40)
SHANGHAI, GUILIN, HONG_KONG = (31.23, 121.47), (25.27, 110.29), (22.32, 114.17)
JAIPUR, MUMBAI = (26.91, 75.79), (19.08, 72.88)
SAGA, LHASA = (29.33, 85.23), (29.65, 91.12)


def _r(*stops, anchored=True, tour="t1"):
    return guards.TourRoute(stops, tour=tour, anchored=anchored)


def _c(lat, lng, name, rtype="amenity", match="full_match", iso="cn"):
    return gm.Candidate(lat, lng, iso, name, rtype, 0.5, match, "p", "osm")


def _row(name, lat, lng, meta, country="China"):
    return {"id": "d-" + name, "name": name, "country": country, "lat": lat, "lng": lng, "located_by": "geoapify",
            "geo_status": "ok", "geo_precision": "point", "review_reason": None, "name_norm": name.lower(),
            "locality_norm": "", "geocoder_meta": json.dumps(meta)}


# ── the route's provenance ──────────────────────────────────────────────────────────────────────────────────────────

def test_a_tour_route_is_the_tuple_of_its_stops():
    r = _r(XIAN, PINGYAO, anchored=False, tour="abc")
    assert r == (XIAN, PINGYAO) and isinstance(r, tuple) and (r.tour, r.anchored) == ("abc", False)
    assert guards.route_km(*XIAN, [r]) == guards.route_km(*XIAN, [(XIAN, PINGYAO)]) == 0.0
    back = pickle.loads(pickle.dumps(r))
    assert back == r and (back.tour, back.anchored) == ("abc", False)
    assert gm.evidence_routes([r, (BEIJING,)], gm.strict_params(V3)) == ((BEIJING,),)     # a plain tuple is anchored
    assert gm.evidence_routes([r], gm.strict_params(V2)) == (r,)


@pytest.mark.asyncio
async def test_route_anchors_marks_the_routes_that_hold_their_own_day_stop():
    """Day 3 uses the name and its own night (day 3) is located: anchored. Day 5 is the row's own night (excluded by
    the query) or a night at an unlocated hut: only days 4 and 6 come back — not anchored."""
    key = ("bell tower", "china", "")
    rows = [{"n": key[0], "c": key[1], "l": key[2], "source_id": "s1", "day_index": d, "anchor_day": a,
             "lat": lat, "lng": lng}
            for d, a, (lat, lng) in ((3, 2, XIAN), (3, 3, XIAN), (3, 4, PINGYAO), (5, 4, BEIJING), (5, 6, SHANGHAI))]
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    got = (await store.route_anchors(conn, [key], exclude=["d-1"]))[key]
    assert got == ((XIAN, PINGYAO), (BEIJING, SHANGHAI))                 # same stops as before (XIAN once)
    assert [(r.tour, r.anchored) for r in got] == [("s1", True), ("s1", False)]


# ── aa997.3 vs aa997.2 on the P4 refusal classes ─────────────────────────────────────────────────────────────────────

def test_a_city_that_is_the_night_stop_itself_is_not_ambiguous_under_v3():
    """'Hong Kong' (correct, refused by aa997.2 as off_route / ambiguous_route): the row is the night stop of the days
    that use it, so its routes are the nights before / after — Beijing → Shanghai, Guilin — hundreds of km away."""
    routes = (_r(BEIJING, SHANGHAI, anchored=False, tour="a"), _r(GUILIN, anchored=False, tour="b"))
    hk = _c(*HONG_KONG, "Hong Kong", "city", iso="hk")
    assert gm.strict_reason("Hong Kong", hk, 1.0, None, None, routes, settlement=True,
                            params=gm.strict_params(V2)) == "ambiguous_route"
    assert gm.strict_reason("Hong Kong", hk, 1.0, None, None, routes, settlement=True,
                            params=gm.strict_params(V3)) is None
    row = _row("Hong Kong", *HONG_KONG, {"query": "Hong Kong", "name": "Hong Kong", "result_type": "city"},
               "Hong Kong")
    assert relocate.rejudge(row, routes)["reason"] == "off_route"                       # aa997.2 (the default)
    j = relocate.rejudge(row, routes, V3)
    assert j["ok"] and relocate.rejudge_outcome(j) == "kept"


def test_a_trek_site_whose_nights_are_unlocated_is_judged_on_anchored_routes_only():
    """'Mount Kailash' (correct, refused by aa997.2 as ambiguous_route): the kora nights are unlocated huts, so the
    routes are Saga / Lhasa, 400-1,000 km away. aa997.3 drops them; an anchored route still decides."""
    kailash = _c(31.067, 81.312, "Mount Kailash", "amenity")
    trek = (_r(SAGA, anchored=False, tour="a"), _r(SAGA, LHASA, anchored=False, tour="b"))
    assert gm.strict_reason("Mount Kailash", kailash, 1.0, None, None, trek,
                            params=gm.strict_params(V2)) == "ambiguous_route"
    assert gm.strict_reason("Mount Kailash", kailash, 1.0, None, None, trek, params=gm.strict_params(V3)) is None
    darchen = _r((30.99, 81.29), anchored=True, tour="c")
    assert gm.strict_reason("Mount Kailash", kailash, 1.0, None, None, trek + (darchen,),
                            params=gm.strict_params(V3)) is None


def test_v3_still_refuses_the_homonym_when_the_routes_are_anchored():
    """The class the rule exists for (S254 wrong rows): Xi'an's Bell Tower on a key the Beijing tours use too. Both
    tours have their own night stop on the day: the wrong-row catch is kept."""
    xian_bell = _c(34.261, 108.943, "Bell Tower")
    routes = [_r(XIAN, PINGYAO, tour="x"), _r(BEIJING, (40.43, 116.57), tour="b")]
    for rule in (V2, V3):
        v = gm.accept("Bell Tower", [xian_bell], None, None, "cn", routes=routes, strict=rule)
        assert v.accepted is None and v.reasons == ["0:ambiguous_route"]
    row = _row("Bell Tower", 34.261, 108.943, {"query": "Bell Tower", "name": "Bell Tower", "result_type": "amenity"})
    assert relocate.rejudge(row, tuple(routes), V3)["reason"] == "ambiguous_route"


def test_the_rejudge_off_route_check_uses_anchored_routes_and_the_homonym_distance():
    """'Delhi Airport' (correct, refused by aa997.2 as off_route): visited on the departure day, which has no night, so
    the route is the previous night (Jaipur, ~240 km). aa997.3: not evidence; an anchored route ~240 km away is within
    the homonym distance; an anchored route ~1,150 km away (Mumbai) still refuses it."""
    meta = {"query": "Delhi Airport", "name": "Indira Gandhi International Airport", "result_type": "amenity"}
    row = _row("Delhi Airport", 28.556, 77.100, meta, "India")
    assert 200 < guards.route_km(28.556, 77.100, [(JAIPUR,)]) < 300
    assert relocate.rejudge(row, (_r(JAIPUR, anchored=False),))["reason"] == "off_route"          # aa997.2
    assert relocate.rejudge_outcome(relocate.rejudge(row, (_r(JAIPUR, anchored=False),), V3)) == "kept"
    assert relocate.rejudge(row, (_r(JAIPUR),), V3)["ok"]
    assert relocate.rejudge(row, (_r(MUMBAI),), V3)["reason"] == "off_route"


def test_a_rejudge_that_cannot_rebuild_the_judged_text_is_unjudged_not_refused():
    """'agricultural flatlands surrounding Yangpyeong' (correct, refused by aa997.2 as `name`): accepted on its
    native-script query; the re-judge measures Latin tokens against it and gets 0. v1's checks were passed at accept
    time: aa997.3 lists them as unjudged (kept_unjudged, no re-search)."""
    meta = {"query": "양평군", "judged": "양평군", "name": "양평군", "result_type": "city"}
    row = _row("agricultural flatlands surrounding Yangpyeong", 37.49, 127.49, meta, "South Korea")
    assert relocate.rejudge(row, ())["reason"] == "name"
    j = relocate.rejudge(row, (), V3)
    assert j["ok"] and "v1_name" in j["unjudged"] and relocate.rejudge_outcome(j) == "kept_unjudged"


def test_a_site_type_refusal_is_unchanged_by_default_and_the_named_admin_knob_is_narrow():
    """site_type stays as aa997.2 in aa997.3 (S257 P4: 4 correct vs 3 wrong refusals). The knob the tune script
    sweeps: an admin-type answer named with the query's site word ("Horombo Hut") passes; "Annapurna" for
    "Mt Annapurna" does not."""
    hut = _c(-3.18, 37.45, "Horombo Hut", "suburb", iso="tz")
    for rule in (V2, V3):
        assert gm.strict_reason("Horombo Hut", hut, 1.0, None, None, (), params=gm.strict_params(rule)) == "site_type"
    knob = gm.StrictParams(site_admin_named=True)
    assert gm.strict_reason("Horombo Hut", hut, 1.0, None, None, (), params=knob) is None
    anna = _c(28.42, 83.74, "Annapurna", "district", iso="np")
    assert gm.strict_reason("Mt Annapurna", anna, 1.0, None, None, (), params=knob) == "site_type"


def test_per_tour_counts_a_tour_once():
    """The tune script's per-tour knob: one tour using a name on 3 far days and one near tour = 1 miss of 2 tours."""
    near, far = _r(XIAN, tour="x"), [_r(BEIJING, tour="b")] * 3
    assert gm.route_cover(*XIAN, [near, *far], 300.0) == (1, 4)
    assert gm.route_cover(*XIAN, [near, *far], 300.0, per_tour=True) == (1, 2)
    assert gm.route_cover(*XIAN, [(XIAN,), (BEIJING,)], 300.0, per_tour=True) == (1, 2)   # no tour: own group


# ── the switches ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_rule_names_and_payloads_take_aa997_3():
    assert gm.strict_rule(True) == V2 and gm.strict_rule(V3) == V3 and gm.strict_rule("v1") is None
    assert gm.strict_rule(False) is None and gm.strict_rule("aa997.9") is None
    assert relocate.is_strict(V3) and relocate.is_strict(V2) and not relocate.is_strict("v1")
    assert relocate.recheck_for(["confirmed"], V3) == ["confirmed"]
    from services.jobs import place_relocate_job as job
    assert job._payload({"mode": "dry_run", "accept_rule": V3})["accept_rule"] == V3
    assert "accept_rule" not in job._payload({"mode": "dry_run", "accept_rule": "aa997.9"})
    assert relocate._rule_kw("v1", None, V3) == {"strict": V3}


def test_the_endpoint_accepts_aa997_3():
    from pydantic import ValidationError

    from api.routers.admin_places import RelocateRequest
    assert RelocateRequest(accept_rule=V3, recheck=["confirmed"]).accept_rule == V3
    with pytest.raises(ValidationError):
        RelocateRequest(accept_rule="aa997.4")


def test_default_accept_rule_reads_aa997_3(monkeypatch):
    monkeypatch.setenv("GEOCODER_ACCEPT_RULE", "aa997.3")
    assert gc.default_accept_rule() == V3


@pytest.mark.asyncio
async def test_geoapify_records_the_rule_that_judged_the_answer():
    """An unanchored far route makes aa997.2 refuse the Xi'an Bell Tower (ambiguous_route); aa997.3 takes it and the
    stored answer says which rule judged it."""
    res = {"name": "Bell Tower", "lat": 34.261, "lon": 108.943, "country_code": "cn", "result_type": "amenity",
           "rank": {"confidence": 1, "match_type": "full_match"}, "place_id": "x", "datasource": {"sourcename": "osm"}}
    routes = (_r(XIAN, PINGYAO, tour="x"), _r(BEIJING, anchored=False, tour="b"))
    for rule, ok in ((V2, False), (V3, True)):
        http = MagicMock()
        http.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"results": [res]}, headers={}))
        g = gc.GeoapifyGeocoder(http=http, api_key="k")
        g._throttle.interval = 0.0
        got = await g.confirm("Bell Tower", None, None, "China", routes=routes, strict=rule)
        assert isinstance(got, gc.Confirmed) is ok
        assert got.meta["rule"] == rule
