"""AA-997 S261 — strict acceptance rule aa997.4 = aa997.3 + the day-evidence guard (geomatch.day_disagree).

The S261 audit of the aa997.3 relocate chain found model points the S254 audit labelled CORRECT moved by a `confirmed`
outcome to a different feature with the same name: a temple of the same name in the next overnight town of the
itinerary (~140 km), a same-name temple near the base city of a day trip (~120 km), another fort / gurudwara of the
same name 4-5 km away in the same city. The tests reproduce those four shapes and four genuine moves that must still
be accepted, table-driven. Every name and place here is invented; coordinates are synthetic; no live call."""
from __future__ import annotations

import json
import math
import pickle
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.places import geocoder as gc
from services.places import geomatch as gm
from services.places import guards, relocate, store

V2, V3, V4 = gm.ACCEPT_RULE, gm.ACCEPT_RULE_V3, gm.ACCEPT_RULE_V4


def off(p, north_km, east_km):
    """The point `north_km` / `east_km` from p (equirectangular, good enough at test distances)."""
    return (round(p[0] + north_km / 110.57, 6), round(p[1] + east_km / (111.32 * math.cos(math.radians(p[0]))), 6))


def _r(*stops, own=None, day=(), anchored=True, tour="t1"):
    return guards.TourRoute(stops, tour=tour, anchored=anchored, own=own, day=day)


def _c(p, name, rtype="amenity", match="full_match"):
    return gm.Candidate(p[0], p[1], "in", name, rtype, 0.9, match, "pid-" + name, "osm")


def _plain(routes):
    """The same routes without the day evidence (what aa997.3 reads)."""
    return tuple(tuple(r) for r in routes)


# ── the invented itineraries ─────────────────────────────────────────────────────────────────────────────────────
# 1. far same-name, both on the route: day d sleeps in Velmora, day d+1 in Kasvenpur (~140 km); the model put
#    "Tolvaresh Temple" in Velmora (labelled correct), the geocoder's exact-name match is Kasvenpur's namesake.
VELMORA = (25.35, 78.64)
KASVENPUR = off(VELMORA, -50, 130)
TARNAGAR = off(VELMORA, 150, -20)
TOLVARESH_MODEL = off(VELMORA, 0.4, 0.3)
VELMORA_DAY = (off(VELMORA, 0.6, -0.2), off(VELMORA, -0.4, 0.5))          # the day's other sites, geocoder-located
FAR_ROUTE = (_r(TARNAGAR, VELMORA, KASVENPUR, own=VELMORA, day=VELMORA_DAY),)
# 2. far same-name on a day trip: the tour sleeps in Dorapur only; the day's sites are in Mirangar (~120 km), where the
#    model put "Pelvani Temple"; the geocoder, biased to Dorapur, finds a namesake near the base city.
DORAPUR = (27.70, 85.32)
MIRANGAR = off(DORAPUR, -100, 60)
PELVANI_MODEL = off(MIRANGAR, 0.3, -0.2)
MIRANGAR_DAY = (off(MIRANGAR, 0.5, 0.4), off(MIRANGAR, -0.6, -0.3))
DAY_TRIP_ROUTE = (_r(DORAPUR, own=DORAPUR, day=MIRANGAR_DAY),)
# 3. / 4. near same-name in one city: Kardhigarh (the night) and the day's old-town sites around the model's point.
KARDHIGARH = (26.30, 73.03)
FORT_MODEL = off(KARDHIGARH, 0.3, -1.6)                                       # 1.6 km from the town point
FORT_DAY = (off(FORT_MODEL, 0.8, 0.4), off(FORT_MODEL, -0.6, 0.3))
CITY_ROUTE = (_r(off(KARDHIGARH, 90, 40), KARDHIGARH, own=KARDHIGARH, day=FORT_DAY),)
SHRINE_MODEL = off(KARDHIGARH, -1.2, 0.9)
SHRINE_DAY = (off(SHRINE_MODEL, 0.7, -0.5), off(SHRINE_MODEL, -0.4, 0.6), off(SHRINE_MODEL, 0.2, 0.9))
SHRINE_ROUTE = (_r(KARDHIGARH, off(KARDHIGARH, -120, 30), own=KARDHIGARH, day=SHRINE_DAY),)
# genuine 1: the model put "Ambrakot Fort" in the wrong town ~100 km away; the day sleeps in Belvatgarh, its sites too.
BELVATGARH = (25.44, 75.64)
BELVATGARH_DAY = (off(BELVATGARH, 0.5, 0.6), off(BELVATGARH, -0.7, 0.2))
BELVATGARH_ROUTE = (_r(off(BELVATGARH, 60, -80), BELVATGARH, own=BELVATGARH, day=BELVATGARH_DAY),)
# genuine 2: the model's point 3 km off the site; the day's other sites are around the real one.
LORVAPUR = (24.60, 80.10)
TANK_TRUE = off(LORVAPUR, 1.0, 1.5)
TANK_DAY = (off(TANK_TRUE, 0.4, 0.3), off(TANK_TRUE, -0.5, 0.2))
LORVAPUR_ROUTE = (_r(LORVAPUR, own=LORVAPUR, day=TANK_DAY),)
# genuine 3: the model gave the town's point for a waterfall 8 km out of town; the day's other sites are in town.
SURALI = (23.10, 81.20)
SURALI_DAY = (off(SURALI, 0.6, 0.2), off(SURALI, -0.5, -0.7))
SURALI_ROUTE = (_r(SURALI, off(SURALI, 0, 90), own=SURALI, day=SURALI_DAY),)
# genuine 4: the model gave the overnight town for a cave 60 km away near the next night; the day's sites are there.
NAMPHU = (18.40, 104.80)
NAMKHE = off(NAMPHU, 60, 5)
CAVE_DAY = (off(NAMKHE, 0.8, -0.5), off(NAMKHE, 1.5, 0.4))
CAVE_ROUTE = (_r(NAMPHU, NAMKHE, own=NAMPHU, day=CAVE_DAY),)

# (case, query, model's point, routes, candidate, accepted under aa997.3, refused under aa997.4)
CASES = [
    ("far_same_name_next_town", "Tolvaresh Temple", TOLVARESH_MODEL, FAR_ROUTE,
     _c(off(KASVENPUR, 0.5, 0.5), "Tolvaresh Temple"), True, True),
    ("far_same_name_day_trip_base", "Pelvani Temple", PELVANI_MODEL, DAY_TRIP_ROUTE,
     _c(off(DORAPUR, 1.5, -1.0), "Pelvani Temple"), True, True),
    ("near_other_fort_4km", "Kardhigarh Fort", FORT_MODEL, CITY_ROUTE,
     _c(off(FORT_MODEL, -3.0, 3.1), "Kardhigarh Fort"), True, True),
    ("near_other_shrine_5km", "Gurudwara Tolsar Sahib", SHRINE_MODEL, SHRINE_ROUTE,
     _c(off(SHRINE_MODEL, 5.0, -1.4), "Gurudwara Tolsar Sahib"), True, True),
    ("genuine_far_correction", "Ambrakot Fort", off(BELVATGARH, 2.0, -100.0), BELVATGARH_ROUTE,
     _c(off(BELVATGARH, 0.8, 0.5), "Ambrakot Fort"), True, False),
    ("genuine_near_correction", "Kanaki Stepwell", off(TANK_TRUE, -2.0, -2.2), LORVAPUR_ROUTE,
     _c(TANK_TRUE, "Kanaki Stepwell"), True, False),
    ("genuine_town_point_waterfall", "Dhorkund Falls", off(SURALI, 0.2, 0.1), SURALI_ROUTE,
     _c(off(SURALI, 6.0, 5.5), "Dhorkund Falls"), True, False),
    ("genuine_cave_near_next_night", "Phalom Tura Cave", off(NAMPHU, 0.1, 0.1), CAVE_ROUTE,
     _c(off(NAMKHE, 1.0, 0.0), "Phalom Tura Cave"), True, False),
]


@pytest.mark.parametrize("case,query,model,routes,cand,v3_ok,v4_refused", CASES, ids=[c[0] for c in CASES])
def test_the_day_guard_on_the_s261_shapes(case, query, model, routes, cand, v3_ok, v4_refused):
    v3 = gm.accept(query, [cand], *model, "in", routes=routes, strict=V3, compact=True)
    assert (v3.accepted is cand) is v3_ok, (case, v3.reasons)
    v4 = gm.accept(query, [cand], *model, "in", routes=routes, strict=V4, compact=True)
    if v4_refused:
        assert v4.accepted is None and v4.reasons == ["0:day_disagree"], (case, v4.reasons)
    else:
        assert v4.accepted is cand, (case, v4.reasons)
    # aa997.3 reads neither the own stop nor the day's places: the same verdict without them
    plain = gm.accept(query, [cand], *model, "in", routes=_plain(routes), strict=V3, compact=True)
    assert (plain.accepted, plain.reasons) == (v3.accepted, v3.reasons)


def test_a_same_name_candidate_near_the_model_point_is_taken_after_the_far_one_is_refused():
    """When the geocoder offers several same-name features, the one the day backs wins (aa997.3 took the first)."""
    far = _c(off(KASVENPUR, 0.5, 0.5), "Tolvaresh Temple")
    near = _c(off(TOLVARESH_MODEL, 0.3, 0.2), "Tolvaresh Temple")
    assert gm.accept("Tolvaresh Temple", [far, near], *TOLVARESH_MODEL, "in", routes=FAR_ROUTE,
                     strict=V3).accepted is far
    v = gm.accept("Tolvaresh Temple", [far, near], *TOLVARESH_MODEL, "in", routes=FAR_ROUTE, strict=V4)
    assert v.accepted is near and v.rank == 1 and v.reasons == ["0:day_disagree"]


def test_the_own_stop_alone_refuses_a_far_move_to_another_stop_of_the_route():
    """No other place of the day is located: the model's point on the day's own night and the candidate near another
    night of the route (~140 km) are two features of one itinerary — the model's is kept. A far candidate near no
    stop (a day excursion 40 km out) is not judged by the own stop."""
    bare = (_r(TARNAGAR, VELMORA, KASVENPUR, own=VELMORA),)
    cand = _c(off(KASVENPUR, 0.5, 0.5), "Tolvaresh Temple")
    assert gm.accept("Tolvaresh Temple", [cand], *TOLVARESH_MODEL, "in", routes=bare,
                     strict=V4).reasons == ["0:day_disagree"]
    excursion = _c(off(VELMORA, -10, 38), "Tolvaresh Temple")
    assert gm.day_disagree("Tolvaresh Temple", excursion, *TOLVARESH_MODEL, bare) is False


def test_no_day_evidence_and_short_moves_are_never_judged():
    cand = _c(off(DORAPUR, 1.5, -1.0), "Pelvani Temple")
    no_day = (_r(DORAPUR, own=DORAPUR),)                       # the day's sites are not located yet
    assert gm.accept("Pelvani Temple", [cand], *PELVANI_MODEL, "in", routes=no_day, strict=V4).accepted is cand
    assert gm.accept("Pelvani Temple", [cand], *PELVANI_MODEL, "in", routes=_plain(DAY_TRIP_ROUTE),
                     strict=V4).accepted is cand
    step = _c(off(FORT_MODEL, 1.2, 1.0), "Kardhigarh Fort")       # 1.6 km: another entrance of the same fort
    assert gm.day_disagree("Kardhigarh Fort", step, *FORT_MODEL, CITY_ROUTE) is False


def test_without_the_model_point_the_guard_is_listed_unjudged_not_refused():
    cand = _c(off(KASVENPUR, 0.5, 0.5), "Tolvaresh Temple")
    unjudged: list = []
    assert gm.strict_reason("Tolvaresh Temple", cand, 1.0, None, None, FAR_ROUTE, unjudged=unjudged,
                            params=gm.strict_params(V4)) is None
    assert unjudged == ["day_disagree"]
    unjudged = []
    gm.strict_reason("Tolvaresh Temple", cand, 1.0, None, None, FAR_ROUTE, unjudged=unjudged,
                     params=gm.strict_params(V3))
    assert unjudged == []


def test_the_rule_names_and_switches_take_aa997_4():
    assert gm.strict_rule(V4) == V4 and V4 in gm.ACCEPT_RULES and relocate.is_strict(V4)
    assert gm.strict_params(V4).day_guard and not gm.strict_params(V3).day_guard
    assert not gm.strict_params(V2).day_guard
    # aa997.4 keeps aa997.3's knobs
    assert gm.strict_params(V4) == gm.StrictParams(**{**gm.strict_params(V3).__dict__, "day_guard": True})
    assert gm.needs_day_places(V4) and not gm.needs_day_places(V3) and not gm.needs_day_places("v1")
    assert relocate._day_kw(V4) == {"day_places": True} and relocate._day_kw(V3) == {}
    assert relocate._rule_kw("v2", "Velmora", V4) == {"strategy": "v2", "context": "Velmora", "strict": V4}
    from services.jobs import place_relocate_job as job
    assert job._payload({"mode": "dry_run", "accept_rule": V4})["accept_rule"] == V4
    assert "day_disagree" in gm.STRICT_REASONS
    assert gm.strict_refusals(["0:day_disagree", "1:off_route"]) == ["day_disagree"]


def test_the_endpoint_and_the_env_accept_aa997_4(monkeypatch):
    from pydantic import ValidationError

    from api.routers.admin_places import RelocateRequest
    r = RelocateRequest(mode="dry_run", accept_rule=V4, strategy="v2", recheck=["llm_point", "off_route"])
    assert r.accept_rule == V4 and r.recheck == ["llm_point", "off_route"]
    assert RelocateRequest(mode="hide_flagged", stamp_accept=V4).stamp_accept == V4
    with pytest.raises(ValidationError):
        RelocateRequest(accept_rule="aa997.5")
    monkeypatch.setenv("GEOCODER_ACCEPT_RULE", "aa997.4")
    assert gc.default_accept_rule() == V4


# ── the route reader ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_tour_route_carries_its_own_stop_and_day_places_and_pickles():
    r = _r(TARNAGAR, VELMORA, own=VELMORA, day=VELMORA_DAY, tour="s9")
    assert r == (TARNAGAR, VELMORA) and r.own == VELMORA and r.day == VELMORA_DAY
    back = pickle.loads(pickle.dumps(r))
    assert back == r and (back.tour, back.anchored, back.own, back.day) == ("s9", True, VELMORA, VELMORA_DAY)
    plain = guards.TourRoute((VELMORA,))
    assert plain.own is None and plain.day == ()


@pytest.mark.asyncio
async def test_route_anchors_reads_the_day_places_only_when_asked():
    key = ("tolvaresh temple", "india", "")
    anchors = [{"n": key[0], "c": key[1], "l": key[2], "source_id": "s1", "day_index": 3, "anchor_day": a,
                "lat": p[0], "lng": p[1]} for a, p in ((2, TARNAGAR), (3, VELMORA), (4, KASVENPUR))]
    day = [{"n": key[0], "c": key[1], "l": key[2], "source_id": "s1", "day_index": 3, "lat": p[0], "lng": p[1]}
           for p in VELMORA_DAY]
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[anchors, day])
    got = (await store.route_anchors(conn, [key], exclude=["d-1"], day_places=True))[key]
    assert got == ((TARNAGAR, VELMORA, KASVENPUR),)
    assert got[0].own == VELMORA and got[0].day == VELMORA_DAY and got[0].anchored
    assert conn.fetch.await_count == 2
    sql, *args = conn.fetch.await_args_list[1].args
    assert "geo.tour_place" in sql and "IS DISTINCT FROM (u.n, u.c, u.l)" in sql and args[3] == ["d-1"]
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=anchors)
    got = (await store.route_anchors(conn, [key]))[key]
    assert conn.fetch.await_count == 1 and got[0].own == VELMORA and got[0].day == ()


# ── relocate: the kept_model outcome ─────────────────────────────────────────────────────────────────────────────────

class _RecConn:
    def __init__(self, current=None):
        self.sql = []
        self.current = current or {"located_by": "llm", "geo_status": "ok", "merged": False}

    def transaction(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def tx():
            yield
        return tx()

    async def execute(self, sql, *args):
        self.sql.append((" ".join(sql.split()), args))
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        return self.current

    def stamps(self):
        return [json.loads(a[1]) for s, a in self.sql if "jsonb_build_object('relocate'" in s]

    def flags(self):
        return [a for s, a in self.sql if s.startswith("UPDATE shared.destinations SET review_reason")]


class _Geo:
    calls_external = True
    name = "fake"
    cost_per_request = 0.0

    def __init__(self, result=None):
        self.result, self.calls, self.requests = result, [], 0

    async def confirm(self, name, lat, lng, country, *, routes=(), **kw):
        self.requests += 1
        self.calls.append({"name": name, "routes": routes, **kw})
        return self.result


def _llm_row(name, p, review_reason="llm_point"):
    stamp = {"v": "aa997.1", "outcome": "llm_point"}
    return {"id": "d-" + name, "name": name, "country": "India", "lat": p[0], "lng": p[1], "located_by": "llm",
            "geo_status": "ok", "geo_precision": "point", "review_reason": review_reason,
            "name_norm": name.lower(), "locality_norm": "", "geocoder_meta": json.dumps({"relocate": stamp})}


def _refused(*reasons):
    return gc.Unconfirmed({"provider": "geoapify", "outcome": "no_match", "rule": V4,
                           "tried": [{"query": "q", "results": 1, "rejected": list(reasons), "top": None}]})


@pytest.mark.asyncio
async def test_a_day_refusal_keeps_the_model_point_listed_but_never_hidden(monkeypatch):
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    write = AsyncMock()
    monkeypatch.setattr(store, "_write_point", write)
    row = _llm_row("Tolvaresh Temple", TOLVARESH_MODEL)
    conn = _RecConn()
    geo = _Geo(_refused("0:day_disagree"))
    r = await relocate.apply_row(conn, row, FAR_ROUTE, geo, hide_unconfirmed=True, strategy="v2",
                                 accept_rule=V4)
    assert r == {"outcome": "kept_model", "country_fixed": False}
    assert geo.calls[0]["strict"] == V4 and write.await_count == 0
    assert conn.flags() == [(row["id"], relocate.REVIEW_SAME_NAME, False)]       # hide_unconfirmed ignored
    stamp = conn.stamps()[0]
    assert stamp["outcome"] == "kept_model" and stamp["refused"] == "day_disagree" and stamp["accept_rule"] == V4
    assert relocate.REVIEW_SAME_NAME not in relocate.HIDE_REASONS
    assert "kept_model" not in relocate.HIDE_FLAGGED_OUTCOMES and "kept_model" in relocate.OUTCOMES


@pytest.mark.asyncio
async def test_other_refusals_and_earlier_rules_flag_as_before(monkeypatch):
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    row = _llm_row("Tolvaresh Temple", TOLVARESH_MODEL)
    for rule, res in ((V4, _refused("0:hint_disagree")), (V3, _refused("0:day_disagree")), (V4, None)):
        conn = _RecConn()
        r = await relocate.apply_row(conn, row, FAR_ROUTE, _Geo(res), hide_unconfirmed=True, accept_rule=rule)
        assert r["outcome"] == "llm_point" and conn.flags() == [(row["id"], "llm_point", True)], rule


@pytest.mark.asyncio
async def test_a_policy_hidden_row_the_day_backs_is_shown_again(monkeypatch):
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    hidden = {"by": relocate.HIDE_FLAGGED_STAMP, "reason": "llm_point", "at": "t"}
    conn = _RecConn({"located_by": "llm", "geo_status": "suspect", "merged": False, "hidden": json.dumps(hidden)})
    r = await relocate.apply_row(conn, _llm_row("Pelvani Temple", PELVANI_MODEL), DAY_TRIP_ROUTE,
                                 _Geo(_refused("0:day_disagree", "1:off_route")), hide_unconfirmed=False,
                                 accept_rule=V4)
    assert r["outcome"] == "kept_model" and r["unhidden"] is True
    reveal = [a for s, a in conn.sql if "SET geo_status = 'ok'" in s][0]
    assert json.loads(reveal[1])["unhidden_by"] == "relocate:kept_model"


@pytest.mark.asyncio
async def test_relocate_reads_the_day_places_only_for_aa997_4(monkeypatch):
    rows = [_llm_row("Tolvaresh Temple", TOLVARESH_MODEL)]
    seen = []

    async def routes_by(conn, rs, **kw):
        seen.append(kw)
        return {}
    monkeypatch.setattr(relocate, "candidates", AsyncMock(return_value=rows))
    monkeypatch.setattr(relocate, "routes_by_destination", routes_by)
    for rule in (V4, V3):
        await relocate.dry_run(None, located_by=["llm"], country=None, cost_per_request=0.0, daily_credits=1500,
                               recheck=["llm_point"], accept_rule=rule)
    assert seen == [{"day_places": True}, {}]


def test_the_rejudge_of_a_stored_confirmation_applies_the_guard():
    """A stored aa997.3 confirmation re-judged under aa997.4: refused when its stored model's point is backed by the
    day; kept (not kept_unjudged) when the hint is unknown — the guard needs the point itself."""
    meta = {"query": "Tolvaresh Temple", "judged": "Tolvaresh Temple", "name": "Tolvaresh Temple",
            "result_type": "amenity", "match_type": "full_match", "strategy": "v2",
            "hint": list(TOLVARESH_MODEL)}
    moved = off(KASVENPUR, 0.5, 0.5)
    row = {**_llm_row("Tolvaresh Temple", moved), "located_by": "geoapify", "geocoder_meta": json.dumps(meta)}
    assert relocate.rejudge(row, FAR_ROUTE, V3)["ok"]
    assert relocate.rejudge(row, FAR_ROUTE, V4)["reason"] == "day_disagree"
    no_hint = {**row, "geocoder_meta": json.dumps({k: v for k, v in meta.items() if k != "hint"})}
    j = relocate.rejudge(no_hint, FAR_ROUTE, V4)
    assert j["ok"] and "day_disagree" in j["unjudged"] and relocate.rejudge_outcome(j) == "kept"


# ── the Geoapify path end to end (recorded-shape payload, no call) ─────────────────────────────────────────────────

def _res(p, name):
    return {"name": name, "lat": p[0], "lon": p[1], "country_code": "in", "result_type": "amenity",
            "rank": {"confidence": 1, "match_type": "full_match"}, "place_id": "pid-" + name,
            "datasource": {"sourcename": "osm"}}


@pytest.mark.asyncio
async def test_geoapify_confirm_under_aa997_4_takes_the_namesake_the_day_backs():
    far, near = off(KASVENPUR, 0.5, 0.5), off(TOLVARESH_MODEL, 0.3, 0.2)
    for results, rule, want in (([far, near], V3, far), ([far, near], V4, near), ([far], V4, None)):
        http = MagicMock()
        payload = {"results": [_res(p, "Tolvaresh Temple") for p in results]}
        http.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda payload=payload: payload, headers={}))
        g = gc.GeoapifyGeocoder(http=http, api_key="k")
        g._throttle.interval = 0.0
        got = await g.confirm("Tolvaresh Temple", *TOLVARESH_MODEL, "India", routes=FAR_ROUTE, strict=rule,
                              strategy="v2", max_queries=1)
        if want is None:
            assert isinstance(got, gc.Unconfirmed) and relocate._day_refused(got)
        else:
            assert isinstance(got, gc.Confirmed) and (got.lat, got.lng) == want and got.meta["rule"] == rule
