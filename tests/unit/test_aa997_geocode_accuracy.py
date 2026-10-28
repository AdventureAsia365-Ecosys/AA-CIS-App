"""AA-997 — geocode accuracy (S254 audit fix plan, generic steps): coordinates from the geocoder only, the
route-coherence guard, the non-place pre-filter, Hong Kong / Macao, the lenient country reader and the place_relocate
job."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.places import geocoder as gc
from services.places import geomatch, guards, locate, name_filter

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "aa997_place_names.json").read_text())


def _dropped(names):
    return [n for n in names if not name_filter.classify(n).is_place]


# ── 1. the non-place pre-filter, measured on the audit's labelled names ──────────────────────────────────────────────

def test_filter_keeps_verified_places():
    """Names the audit verified as correct pins: at most 1 % dropped (today 3 of 439, all region / description-like:
    "Muslim quarter", "the Himalayan ranges", "the largest Buddhist stupa")."""
    v = FIXTURE["verified"]
    places = v["correct"] + v["wrong_place"] + v["too_coarse"] + v["unsure"]
    assert len(_dropped(v["correct"])) <= 0.01 * len(v["correct"])
    assert len(_dropped(places)) <= 0.01 * len(places)


def test_filter_keeps_the_real_unresolved_places():
    """Unresolved names the audit classed as real places (obscure, spelling variant, well known, coast / island guard
    reject, country text): at most 2 % dropped."""
    u = FIXTURE["unresolved"]
    real = (u["obscure_real_place"] + u["spelling_variant"] + u["well_known_model_miss"]
            + u["guard_reject_coast_island_border"] + u["country_text_unparsed"])
    assert len(_dropped(real)) <= 0.02 * len(real)


def test_filter_catches_most_non_places():
    v, u = FIXTURE["verified"], FIXTURE["unresolved"]
    assert len(_dropped(v["not_a_place"])) >= 0.85 * len(v["not_a_place"])
    assert len(_dropped(u["route_segment"])) >= 0.85 * len(u["route_segment"])
    non_places = u["route_segment"] + u["not_a_place"] + u["descriptive_phrase"] + u["region_or_water_body"]
    assert len(_dropped(non_places)) >= 0.55 * len(non_places)


@pytest.mark.parametrize("name,cause", [
    ("Lhasa to Gyantse", name_filter.ROUTE_SEGMENT),
    ("Xepon to Tad Lo via Saravane", name_filter.ROUTE_SEGMENT),
    ("hotel in Seoul", name_filter.LODGING),
    ("a pre-booked hotel in Arusha", name_filter.LODGING),
    ("Ganpati Palace (or similar)", name_filter.LODGING),
    ("Korean cooking class", name_filter.ACTIVITY),
    ("Tsaatan family", name_filter.ACTIVITY),
    ("The Jaipur Balloon Ride", name_filter.ACTIVITY),
    ("Arabian Sea", name_filter.REGION),
    ("rural Laos", name_filter.REGION),
    ("international airport", name_filter.GENERIC_KIND),
    ("small riverside Lao village", name_filter.DESCRIPTIVE),
    ("At about 4040 m", name_filter.ANNOTATION_ONLY),
])
def test_non_place_causes(name, cause):
    v = name_filter.classify(name)
    assert not v.is_place and v.cause == cause and v.reason == f"non_place:{cause}"


@pytest.mark.parametrize("name,query", [
    ("Yali La Pass (4820 m)", "Yali La Pass"),
    ("Nyi Pass (4,010m)", "Nyi Pass"),
    ("the village of Gogina", "Gogina"),
    ("Jinshanling to Simatai West Great Wall", "Jinshanling to Simatai West Great Wall"),   # a stretch, kept
    ("Gao Family Courtyard", "Gao Family Courtyard"),
    ("Mount Kailash and Lake Manasarovar", "Mount Kailash and Lake Manasarovar"),
    ("the Tibet Museum in Lhasa", "the Tibet Museum in Lhasa"),
    ("Great Mosque", "Great Mosque"),
    ("countryside around Yangshuo", "countryside around Yangshuo"),
    ("太魯閣", "太魯閣"),
])
def test_places_are_kept_with_a_clean_query(name, query):
    v = name_filter.classify(name)
    assert v.is_place and v.query == query


def test_a_generic_kind_with_a_locality_is_a_place():
    assert not name_filter.classify("base camp").is_place
    assert name_filter.classify("base camp", "Annapurna").is_place


# ── 2. route coherence ────────────────────────────────────────────────────────────────────────────────────────────────
XIAN = (34.26, 108.94)
PINGYAO = (37.20, 112.18)
BEIJING_WALL = (39.90, 116.41)


def test_route_km_measures_to_the_segments_between_stops():
    route = (XIAN, PINGYAO)
    mid = ((XIAN[0] + PINGYAO[0]) / 2, (XIAN[1] + PINGYAO[1]) / 2)
    assert guards.route_km(*mid, [route]) < 5                       # on a long transfer day: on the line, not a stop
    assert guards.route_km(*BEIJING_WALL, [route]) > 400
    assert guards.route_km(*XIAN, []) is None and guards.on_route(*XIAN, ()) is None
    assert guards.on_route(*BEIJING_WALL, [route], "City Wall") is False
    assert guards.has_route([route]) and not guards.has_route([()])
    assert guards.route_bias([(), route]) == PINGYAO


def test_long_features_get_a_wider_route_radius():
    assert guards.route_radius("Erhai Lake") == guards.ROUTE_LONG_KM
    assert guards.route_radius("Kong Lor Cave") == guards.ROUTE_KM


def _cand(lat, lng, name="City Wall", iso="cn", rtype="amenity"):
    return geomatch.Candidate(lat, lng, iso, name, rtype, 0.5, None, "p", "osm")


def test_accept_uses_the_route_not_the_model_hint():
    """The Xi'an Bell Tower homonym: the model's hint is Beijing. The Beijing candidate (next to the hint) is off the
    route; the Xi'an one (900 km from the hint) is on it and accepted."""
    cands = [_cand(*BEIJING_WALL, "Bell Tower"), _cand(34.26, 108.95, "Bell Tower")]
    v = geomatch.accept("Bell Tower", cands, *BEIJING_WALL, "cn", routes=[(XIAN, PINGYAO)])
    assert v.accepted is cands[1] and v.reasons == ["0:off_route"]
    # without a route: the bake-off rule against the hint (unchanged)
    assert geomatch.accept("Bell Tower", cands, *BEIJING_WALL, "cn").accepted is cands[0]
    # nothing to check against: nothing accepted
    assert geomatch.accept("Bell Tower", cands, None, None, "cn").reasons == ["0:no_reference", "1:no_reference"]


def test_a_day_trip_off_the_route_is_taken_when_the_hint_agrees():
    """Ayutthaya on a Bangkok-based day (~75 km off the one-stop route): the geocoder's answer next to the model's
    point is a second choice within DAY_TRIP_KM; an on-route candidate still wins; a far homonym does not pass."""
    bangkok = ((13.75, 100.50),)
    ayutthaya = (14.36, 100.58)
    cands = [_cand(*ayutthaya, "Ayutthaya", "th", "city")]
    v = geomatch.accept("Ayutthaya", cands, *ayutthaya, "th", routes=[bangkok])
    assert v.accepted is cands[0] and v.reasons == ["0:day_trip"]
    # no hint agreement (the model put it elsewhere): off the route stays rejected
    assert geomatch.accept("Ayutthaya", cands, 15.5, 101.5, "th", routes=[bangkok]).accepted is None
    # an on-route candidate later in the list wins over the day-trip one
    near = _cand(13.76, 100.51, "Ayutthaya", "th", "city")
    v = geomatch.accept("Ayutthaya", [cands[0], near], *ayutthaya, "th", routes=[bangkok])
    assert v.accepted is near and v.reasons == ["0:day_trip"]
    # the Xi'an / Beijing homonym (~900 km) is not a day trip
    bell = [_cand(*BEIJING_WALL, "Bell Tower")]
    assert geomatch.accept("Bell Tower", bell, *BEIJING_WALL, "cn", routes=[(XIAN, PINGYAO)]).accepted is None


def test_water_words_inside_a_name_keep_the_place():
    for name in ("Ocean Park", "Tanah Lot Sea Temple"):
        assert name_filter.classify(name).is_place, name
    for name in ("Arabian Sea", "Sea of Japan", "Gulf of Thailand", "the beach by the sea", "the Himalayas"):
        assert name_filter.classify(name).cause == name_filter.REGION, name


def test_accept_takes_hong_kong_for_china():
    c = _cand(22.32, 114.17, "Hong Kong", iso="hk", rtype="city")
    assert geomatch.accept("Hong Kong", [c], 22.3, 114.2, "cn").accepted is None             # country
    assert geomatch.accept("Hong Kong", [c], 22.3, 114.2, "cn", iso_codes=("cn", "hk", "mo")).accepted is c


# ── 3. countries ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_hong_kong_and_macao_points_are_not_china():
    assert guards.refine_country(22.28, 114.16, "China") == "Hong Kong"
    assert guards.refine_country(22.19, 113.54, "China") == "Macao"
    assert guards.refine_country(39.90, 116.41, "China") == "China"
    assert guards.refine_country(22.28, 114.16, "Thailand") == "Thailand"
    assert guards.search_codes("China") == ["cn", "hk", "mo"] and guards.search_codes("Laos") == ["la"]
    assert guards.same_country("China", "Hong Kong") and not guards.same_country("China", "Nepal")


def test_lenient_country_reads_compound_text():
    assert guards.lenient_country("Tibet, China") == "China"
    assert guards.lenient_country("Lhasa (China)") == "China"
    assert guards.lenient_country("India and Pakistan") is None
    assert guards.lenient_country("Nepal / India") is None


def test_answer_carries_the_town_and_reads_tibet_china():
    raw = ('{"Kong Lor Cave": [17.9, 104.7, "Laos", "Ban Kong Lo"], "Norbulingka": [29.65, 91.1, "Tibet, China"], '
           '"Taroko Gorge": [24.16, 121.62, "Taiwan", "Hualien", "太魯閣"]}')
    out = locate.parse_answer(raw, ["Kong Lor Cave", "Norbulingka", "Taroko Gorge"])
    assert out["Kong Lor Cave"].town == "Ban Kong Lo" and out["Norbulingka"].country == "China"
    assert out["Taroko Gorge"].town == "Hualien" and out["Taroko Gorge"].native_name == "太魯閣"


# ── 4. locate: the geocoder's point only ─────────────────────────────────────────────────────────────────────────────

class _Geo:
    calls_external = True
    name = "fake"
    cost_per_request = 0.0

    def __init__(self, results=None):
        self.results, self.calls, self.requests = results or {}, [], 0

    async def confirm(self, name, lat, lng, country, *, native_name=None, routes=(), settlement=False):
        self.requests += 1
        self.calls.append({"name": name, "hint": None if lat is None else (lat, lng), "routes": routes,
                           "settlement": settlement})
        return self.results.get(name)


def _ok(lat, lng, country="Laos", rtype="amenity"):
    return gc.Confirmed(lat, lng, country, "pid", "geoapify", {"provider": "geoapify", "result_type": rtype})


def _gen(answer: dict):
    async def generate(system, user):
        return json.dumps(answer)
    return generate


ROUTE = ((17.40, 104.80), (18.00, 104.90))      # Thakhek → Kong Lor village


@pytest.mark.asyncio
async def test_the_confirmed_point_is_taken_even_far_from_a_wrong_hint():
    geo = _Geo({"Kong Lor Cave": _ok(17.98, 104.81)})
    item = locate.Item(("kong lor cave", "laos", ""), "Kong Lor Cave", "Laos", routes=(ROUTE,))
    out = await locate.locate_many([item], _gen({"Kong Lor Cave": [17.30, 105.40, "Laos", "Ban Kong Lo"]}), geo)
    p = out[item.key].point
    assert (p.lat, p.lng, p.located_by, p.precision) == (17.98, 104.81, "geoapify", "point")
    assert geo.calls[0]["routes"] == (ROUTE,)


@pytest.mark.asyncio
async def test_unconfirmed_name_falls_back_to_its_town_as_an_area():
    geo = _Geo({"Ban Kong Lo": _ok(17.97, 104.79, rtype="city")})
    item = locate.Item(("kong lor cave", "laos", ""), "Kong Lor Cave", "Laos", routes=(ROUTE,))
    out = await locate.locate_many([item], _gen({"Kong Lor Cave": [17.30, 105.40, "Laos", "Ban Kong Lo"]}), geo)
    p = out[item.key].point
    assert (p.lat, p.located_by, p.precision, p.review_reason) == (17.97, "geoapify", "area", "centroid_fallback")
    assert p.meta["outcome"] == "town_fallback" and p.place_id is None
    assert [c["settlement"] for c in geo.calls] == [False, True]


@pytest.mark.asyncio
async def test_no_geocoder_answer_means_no_point_and_a_final_reason():
    geo = _Geo()
    item = locate.Item(("x", "laos", ""), "Wat Somewhere", "Laos")
    out = await locate.locate_many([item], _gen({"Wat Somewhere": [17.9, 102.6, "Laos", "Vientiane"]}), geo)
    o = out[item.key]
    assert o.point is None and o.reason == locate.REASON_UNCONFIRMED and o.final
    assert len(geo.calls) == 2                       # the name, then the town


@pytest.mark.asyncio
async def test_a_model_null_gets_one_geocoder_only_search_when_the_tour_has_a_route():
    geo = _Geo({"Ban Na Hin": _ok(17.98, 104.85, rtype="city")})
    with_route = locate.Item(("ban na hin", "laos", ""), "Ban Na Hin", "Laos", routes=(ROUTE,))
    no_route = locate.Item(("nowhere", "laos", ""), "Nowhere", "Laos")
    out = await locate.locate_many([with_route, no_route], _gen({"Ban Na Hin": None, "Nowhere": None}), geo)
    assert out[with_route.key].point.located_by == "geoapify"
    assert geo.calls == [{"name": "Ban Na Hin", "hint": None, "routes": (ROUTE,), "settlement": False}]
    assert out[no_route.key].point is None and out[no_route.key].reason == locate.REASON_NULL
    assert out[no_route.key].final


@pytest.mark.asyncio
async def test_a_hong_kong_answer_for_china_is_stored_as_hong_kong():
    geo = _Geo({"Yuen Po Street Bird Market": _ok(22.3259, 114.1738, "Hong Kong")})
    item = locate.Item(("yuen po street bird market", "china", ""), "Yuen Po Street Bird Market", "China")
    out = await locate.locate_many([item], _gen({"Yuen Po Street Bird Market": [22.33, 114.17, "China", "Kowloon"]}),
                                   geo)
    assert out[item.key].point.country == "Hong Kong"


@pytest.mark.asyncio
async def test_model_only_keeps_the_model_point_flagged():
    item = locate.Item(("x", "laos", ""), "Phou Khoun", "Laos")
    out = await locate.locate_many([item], _gen({"Phou Khoun": [19.45, 102.44, "Laos", "Phou Khoun"]}),
                                   gc.ModelOnlyGeocoder())
    p = out[item.key].point
    assert (p.located_by, p.precision, p.review_reason) == ("llm", "area", locate.REVIEW_LLM_POINT)


@pytest.mark.asyncio
async def test_geoapify_skips_a_name_with_nothing_to_check_against_and_biases_to_the_route():
    http = MagicMock()
    http.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"results": []}, headers={}))
    g = gc.GeoapifyGeocoder(http=http, api_key="k")
    g._throttle.interval = 0.0
    got = await g.confirm("Ban Na Hin", None, None, "Laos")
    assert isinstance(got, gc.Unconfirmed) and got.meta["outcome"] == "skipped_no_reference" and g.requests == 0
    await g.confirm("Ban Na Hin", None, None, "Laos", routes=(ROUTE,))
    params = http.get.call_args[1]["params"]
    assert params["bias"] == "proximity:104.90000,18.00000" and params["filter"] == "countrycode:la"


# ── 5. geocode_pending: non-places never reach a paid call ───────────────────────────────────────────────────────────

class _Conn:
    async def fetch(self, *a, **k):
        return []

    def transaction(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def tx():
            yield
        return tx()

    async def execute(self, *a, **k):
        return "UPDATE 0"

    async def fetchval(self, *a, **k):
        return 0


class _Pool:
    def acquire(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def acq():
            yield _Conn()
        return acq()


@pytest.mark.asyncio
async def test_geocode_pending_drops_non_places_before_any_call(monkeypatch):
    from services.places import pipeline, store
    due = [{"name_norm": n.lower(), "country_norm": "laos", "locality_norm": "", "display_name": n, "country": "Laos",
            "tour_name": "", "locality": None, "place_card_id": None}
           for n in ("Pakse to Champasak", "hotel in Vientiane", "Wat Phou")]
    marked = []

    async def mark_name(conn, key, **kw):
        marked.append((key[0], kw))
        return "unknown" if kw.get("final") else "pending"
    monkeypatch.setattr(store, "due_names", AsyncMock(return_value=due))
    monkeypatch.setattr(store, "known_destinations", AsyncMock(return_value={}))
    monkeypatch.setattr(store, "mark_name", mark_name)
    monkeypatch.setattr(store, "route_anchors", AsyncMock(return_value={("wat phou", "laos", ""): (ROUTE,)}))
    monkeypatch.setattr(store, "upsert_destination", AsyncMock(return_value=("d1", "inserted")))
    monkeypatch.setattr(store, "relink", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "apply_outlier_rule", AsyncMock(return_value=(0, [])))
    seen = []

    async def generate(system, user):
        seen.append(user)
        return json.dumps({"Wat Phou": [17.90, 104.85, "Laos", "Champasak"]})
    geo = _Geo({"Wat Phou": _ok(17.95, 104.86)})
    out = await pipeline.geocode_pending(_Pool(), generate, geo, limit=10)
    assert out["non_place"] == 2 and out["non_place_by"] == {"route_segment": 1, "lodging": 1}
    assert out["resolved"] == 1 and out["with_route"] == 1
    asked = [ln for ln in seen[0].splitlines() if ln.startswith("- ")]
    assert asked == ["- Wat Phou | tour country: Laos | tour: "]
    assert [m for m in marked if m[1].get("final")] == [
        ("pakse to champasak", {"error": "non_place:route_segment", "final": True}),
        ("hotel in vientiane", {"error": "non_place:lodging", "final": True})]


# ── 6. place_relocate ────────────────────────────────────────────────────────────────────────────────────────────────

def _row(name, lat, lng, country="Laos", **kw):
    return {"id": kw.pop("id", "d-" + name), "name": name, "country": country, "lat": lat, "lng": lng,
            "located_by": "llm", "geo_status": "ok", "geo_precision": "area", "review_reason": None,
            "name_norm": name.lower(), "locality_norm": "", **kw}


def test_classify_row_reports_without_a_call():
    from services.places import relocate
    c = relocate.classify_row(_row("Kong Lor Cave", 17.30, 105.40), (ROUTE,))
    assert c["on_route"] is False and c["route_km"] > 50 and c["requests"] == 1 and c["non_place"] is None
    assert relocate.classify_row(_row("Pakse to Champasak", 15.1, 105.8), ())["non_place"] == "route_segment"
    assert relocate.classify_row(_row("Central Hong Kong", 22.28, 114.16, "China"), ())["hk_mo"] == "Hong Kong"


class _RecConn(_Conn):
    def __init__(self, current=None):
        self.sql = []
        self.current = {"located_by": "llm", "geo_status": "ok", "merged": False} if current is None else current

    async def execute(self, sql, *args):
        self.sql.append((" ".join(sql.split())[:60], args))
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        return self.current or None


@pytest.mark.asyncio
async def test_relocate_row_writes_the_confirmed_point(monkeypatch):
    from services.places import relocate, store
    written = []

    async def write_point(conn, dest_id, located, force):
        written.append((dest_id, located.lat, located.located_by))
    monkeypatch.setattr(store, "_write_point", write_point)
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    conn = _RecConn()
    r = await relocate.apply_row(conn, _row("Kong Lor Cave", 17.30, 105.40), (ROUTE,),
                                 _Geo({"Kong Lor Cave": _ok(17.98, 104.81)}), hide_unconfirmed=False)
    assert r["outcome"] == "confirmed" and r["moved_km"] > 60
    assert written == [("d-Kong Lor Cave", 17.98, "geoapify")]
    stamp = json.loads([a for _, a in conn.sql if len(a) == 2 and str(a[1]).startswith("{")][0][1])
    assert stamp["v"] == relocate.RULE_VERSION and stamp["outcome"] == "confirmed"


@pytest.mark.asyncio
async def test_relocate_row_flags_an_unconfirmed_point_and_hides_only_when_asked(monkeypatch):
    from services.places import relocate, store
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    conn = _RecConn()
    r = await relocate.apply_row(conn, _row("Kong Lor Cave", 17.30, 105.40), (ROUTE,), _Geo(), hide_unconfirmed=False)
    assert r["outcome"] == "off_route"
    flag = [a for s, a in conn.sql if s.startswith("UPDATE shared.destinations SET review_reason")][0]
    assert flag == ("d-Kong Lor Cave", "off_route", False)
    assert not [s for s, _ in conn.sql if "geo.place_name" in s]
    conn = _RecConn()
    r = await relocate.apply_row(conn, _row("Phou Khoun", 19.45, 102.44, review_reason="unconfirmed_hard_type"), (),
                                 _Geo(), hide_unconfirmed=True)
    assert r["outcome"] == "llm_point"
    flag = [a for s, a in conn.sql if s.startswith("UPDATE shared.destinations SET review_reason")][0]
    assert flag == ("d-Phou Khoun", "unconfirmed_hard_type", True)          # the prior reason is kept; hidden
    assert [s for s, _ in conn.sql if "geo.place_name" in s]


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [{"located_by": "manual", "geo_status": "manual", "merged": False},
                                     {"located_by": "llm", "geo_status": "ok", "merged": True}, {}])
async def test_relocate_row_skips_a_row_fixed_or_merged_meanwhile(monkeypatch, current):
    """An admin fix (or a merge) between the run's selection and the row's turn wins: no write, no stamp."""
    from services.places import relocate, store
    write_point = AsyncMock()
    monkeypatch.setattr(store, "_write_point", write_point)
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    conn = _RecConn(current)
    r = await relocate.apply_row(conn, _row("Kong Lor Cave", 17.30, 105.40), (ROUTE,),
                                 _Geo({"Kong Lor Cave": _ok(17.98, 104.81)}), hide_unconfirmed=False)
    assert r == {"outcome": "skipped"} and not write_point.called and conn.sql == []
    r = await relocate.apply_row(_RecConn(current), _row("hotel in Pakse", 15.1, 105.8), (), _Geo(),
                                 hide_unconfirmed=False)
    assert r == {"outcome": "skipped"}


@pytest.mark.asyncio
async def test_relocate_non_place_makes_no_call(monkeypatch):
    from services.places import relocate, store
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    geo = _Geo()
    r = await relocate.apply_row(_RecConn(), _row("hotel in Pakse", 15.1, 105.8), (), geo, hide_unconfirmed=False)
    assert r == {"outcome": "non_place", "cause": "lodging"} and geo.calls == []


@pytest.mark.asyncio
async def test_relocate_dry_run_counts_and_estimates(monkeypatch):
    from services.places import relocate
    rows = [_row("Kong Lor Cave", 17.30, 105.40), _row("Pakse to Champasak", 15.1, 105.8),
            _row("Koto to Meta Village", 28.6, 84.2, country="Nepal"),
            _row("Central Hong Kong", 22.28, 114.16, "China")]
    monkeypatch.setattr(relocate, "candidates", AsyncMock(return_value=rows))
    monkeypatch.setattr(relocate, "routes_by_destination", AsyncMock(return_value={rows[0]["id"]: (ROUTE,)}))
    out = await relocate.dry_run(None, located_by=["llm"], country=None, cost_per_request=0.0, daily_credits=1500)
    assert out["rows"] == 4 and out["off_route"] == 1 and out["no_route"] == 3 and out["hk_mo"] == 1
    assert out["non_place"] == 2 and out["non_place_by"] == {"route_segment": 2}
    assert out["estimate"]["geoapify_credits"] == 2 and out["estimate"]["cost_usd"] == 0.0
    assert out["estimate"]["days"] == 1 and out["estimate"]["model_calls"] == 0


def test_relocate_payload_is_clamped_and_shares_the_day():
    from services.jobs import place_relocate_job as job
    p = job._payload({"mode": "apply", "located_by": ["llm", "manual"], "limit": 10_000})
    assert p == {"mode": "apply", "located_by": ["llm"], "country": None, "limit": 400, "hide_unconfirmed": False}
    assert job._payload({})["mode"] == "dry_run"
    assert job.daily_credits() == gc.DEFAULT_DAILY_CREDITS - 1000


@pytest.mark.asyncio
async def test_relocate_apply_refuses_without_a_geocoder(monkeypatch):
    from services.jobs import place_relocate_job as job
    monkeypatch.setenv("GEOCODER_PROVIDER", "model_only")
    ctx = MagicMock()
    ctx.payload = {"mode": "apply"}
    out = await job.run(ctx)
    assert out["status"] == "refused_no_geocoder"


def test_relocate_kind_is_registered_background_cap_one():
    from shared.jobs.registry import kinds
    from shared.jobs.worker import load_kinds
    load_kinds()
    k = kinds()["place_relocate"]
    assert k.background and k.concurrency == 1 and not k.deploy_blocking


class _AdminConn:
    def __init__(self, last):
        self.last, self.enqueued = last, []

    async def fetchrow(self, sql, *a):
        return self.last


class _AdminPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def acq():
            yield self.conn
        return acq()


@pytest.mark.asyncio
async def test_relocate_endpoint_needs_a_matching_dry_run_before_apply(monkeypatch):
    from fastapi import HTTPException

    from api.routers import admin as admin_module
    from api.routers import admin_places
    from services.places import relocate
    from shared.jobs import registry
    monkeypatch.setattr(admin_module, "ADMIN_SECRET", "s")
    queued = []

    async def fake_enqueue(conn, kind, payload, **kw):
        queued.append((kind, payload))
        return "job-1", True
    monkeypatch.setattr(registry, "enqueue", fake_enqueue)
    request = MagicMock()
    request.app.state.pool = _AdminPool(_AdminConn(None))
    body = admin_places.RelocateRequest(mode="apply")
    with pytest.raises(HTTPException) as e:
        await admin_places.relocate_run(body, request, x_admin_secret="s")
    assert e.value.status_code == 409 and queued == []
    # a dry run of another country does not count
    other = {"id": "dry-0", "result": json.dumps({"rule_version": relocate.RULE_VERSION, "located_by": ["llm"],
                                                  "country": "Laos"})}
    request.app.state.pool = _AdminPool(_AdminConn(other))
    with pytest.raises(HTTPException):
        await admin_places.relocate_run(body, request, x_admin_secret="s")
    last = {"id": "dry-1", "result": json.dumps({"rule_version": relocate.RULE_VERSION, "located_by": ["llm"],
                                                 "country": None})}
    request.app.state.pool = _AdminPool(_AdminConn(last))
    out = await admin_places.relocate_run(body, request, x_admin_secret="s")
    assert out["job_id"] == "job-1" and queued[0][0] == "place_relocate" and queued[0][1]["dry_run_job"] == "dry-1"
    # the dry run itself needs nothing first
    out = await admin_places.relocate_run(admin_places.RelocateRequest(), request, x_admin_secret="s")
    assert queued[1][1]["mode"] == "dry_run"
    with pytest.raises(ValueError):
        admin_places.RelocateRequest(located_by=["manual"])
