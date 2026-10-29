"""AA-997 v2 (S257 interim audit: relocate `confirmed` only 18/29 right, model-placed rows ~62 %) — the stricter
acceptance rule (always on), the v2 query strategy (opt-in), the re-check of earlier confirmations, the request cap and
the readers' hide-unconfirmed policy. Names and coordinates follow the S254 verified rows; candidates are synthetic
(no recorded Geoapify answer exists for them); no live call, no secret."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.places import geocoder as gc
from services.places import geomatch as gm
from services.places import guards, locate

BAKEOFF = json.loads((Path(__file__).parent / "fixtures" / "aa712_geoapify_bakeoff.json").read_text())["rows"]

XIAN = (34.26, 108.94)
PINGYAO = (37.20, 112.18)
BEIJING = (39.90, 116.40)
XIAN_ROUTE = (XIAN, PINGYAO)
BEIJING_ROUTE = (BEIJING, (40.43, 116.57))          # Beijing → Mutianyu
KONG_LOR_ROUTE = ((17.40, 104.80), (18.00, 104.90))  # Thakhek → Kong Lor village


def _c(lat, lng, name, rtype="amenity", match="full_match", iso="cn"):
    return gm.Candidate(lat, lng, iso, name, rtype, 0.5, match, "p", "osm")


# ── 1. stricter acceptance ───────────────────────────────────────────────────────────────────────────────────────────

def test_a_site_is_never_answered_by_a_suburb_or_district():
    """S254: Mt Annapurna placed on the Annapurna rural municipality; Urumqi Airport on the Ürümqi centroid (sim 0.5:
    the city name is half the query)."""
    anna = _c(28.42, 83.74, "Annapurna", "district", iso="np")
    v = gm.accept("Mt Annapurna", [anna], 28.50, 83.80, "np")
    assert v.accepted is None and v.reasons == ["0:type"]
    assert gm.accept("Mt Annapurna", [anna], 28.50, 83.80, "np", strict=False).accepted is anna   # v1 took it
    urumqi = _c(43.82, 87.59, "Ürümqi", "city")
    assert gm.accept("Urumqi Airport", [urumqi], 43.85, 87.55, "cn").reasons == ["0:type"]
    # a town query still takes its town; a long feature may be an admin area
    assert gm.accept("Guangning", [_c(23.63, 112.44, "Guangning", "city")], 23.63, 112.44, "cn").accepted
    assert gm.accept("Punakha Valley", [_c(27.6, 89.86, "Punakha Valley", "district", iso="bt")], 27.6, 89.86,
                     "bt").accepted


def test_a_fallback_match_type_is_refused_for_anything_but_a_town():
    fallback = _c(13.36, 103.86, "Angkor Thom Temple", "amenity", "match_by_city_or_disrict", iso="kh")
    assert gm.accept("Angkor Thom Temple", [fallback], 13.40, 103.86, "kh").reasons == ["0:match"]
    town = _c(13.36, 103.86, "Siem Reap", "city", "match_by_city_or_disrict", iso="kh")
    assert gm.accept("Siem Reap", [town], 13.36, 103.86, "kh").accepted is town


def test_a_street_for_a_site_only_right_next_to_the_model_point():
    path = _c(27.49, 89.36, "Taktshang Monastery Road", "street", iso="bt")
    assert gm.accept("Taktshang Monastery", [path], 27.492, 89.363, "bt").accepted is path       # 0.4 km: access
    assert gm.accept("Taktshang Monastery", [path], 27.40, 89.40, "bt").reasons == ["0:type"]     # 11 km


def test_another_kind_of_poi_carrying_the_name_is_refused():
    """Bake-off R rows: "Terelj Hotel" for "Terelj valley", "Phi Phi Natural Resort" for "Koh Phi Phi village"."""
    hotel = _c(47.98, 107.46, "Terelj Hotel", iso="mn")
    assert gm.kind_conflict("Terelj valley", "Terelj Hotel") == "lodging"
    assert gm.accept("Terelj valley", [hotel], 47.85, 107.40, "mn").reasons == ["0:kind"]        # 14 km off
    assert gm.accept("Terelj valley", [hotel], 47.975, 107.455, "mn").accepted is hotel          # next to the hint
    assert gm.kind_conflict("Ganpati Palace Hotel", "Ganpati Palace Resort") is None             # same group
    assert gm.kind_conflict("Barafu", "Barafu Camp") is None and gm.kind_conflict("Kong Lor", "Kong Lor Cave") is None
    assert gm.kind_conflict("Tingtibi", "Tingtibi - Panbang Highway") == "road"


def test_one_pin_must_suit_every_tour_that_uses_the_name():
    """The key of "Bell Tower" (no town) serves the Xi'an tours and the Beijing tours alike: a Xi'an answer is wrong for
    the Beijing tours, so it is not a confirmation (the S254 homonym class). One miss is allowed per 5 routes."""
    xian_bell = _c(34.261, 108.943, "Bell Tower")
    v = gm.accept("Bell Tower", [xian_bell], None, None, "cn", routes=[XIAN_ROUTE, BEIJING_ROUTE])
    assert v.accepted is None and v.reasons == ["0:ambiguous_route"]
    assert gm.accept("Bell Tower", [xian_bell], None, None, "cn", routes=[XIAN_ROUTE] * 2).accepted is xian_bell
    assert gm.accept("Bell Tower", [xian_bell], None, None, "cn",
                     routes=[XIAN_ROUTE] * 4 + [BEIJING_ROUTE]).accepted is xian_bell
    assert gm.accept("Bell Tower", [xian_bell], None, None, "cn", routes=[XIAN_ROUTE, BEIJING_ROUTE],
                     strict=False).accepted is xian_bell                                           # v1 took it


def test_an_on_route_model_point_is_overruled_only_by_an_exact_poi():
    """Route reference: the model's point is on the route; a candidate 40 km from it needs an exact name, a POI type and
    a full match. A model point off the route (the Kong Lor case, 70 km) is overruled as before."""
    hint = (17.98, 104.82)
    partial = _c(17.70, 104.75, "Kong Lor Waterfall", iso="la")                    # sim 2/3 on "Tham Kong Lor"
    v = gm.accept("Tham Kong Lor", [partial], *hint, "la", routes=[KONG_LOR_ROUTE])
    assert v.accepted is None and v.reasons == ["0:hint_disagree"]
    assert gm.accept("Tham Kong Lor", [partial], *hint, "la", routes=[KONG_LOR_ROUTE], strict=False).accepted
    exact = _c(17.70, 104.75, "Tham Kong Lor", iso="la")
    assert gm.accept("Tham Kong Lor", [exact], *hint, "la", routes=[KONG_LOR_ROUTE]).accepted is exact
    far_hint = (17.30, 105.40)
    right = _c(17.98, 104.81, "Kong Lor Cave", "amenity", "inner_part", iso="la")
    assert gm.accept("Kong Lor Cave", [right], *far_hint, "la", routes=[KONG_LOR_ROUTE]).accepted is right


def test_the_strict_rule_loses_no_named_result_of_the_bakeoff():
    """On the 55 recorded S236 answers (no routes there): every correct named result (N) is still accepted, no wrong
    one is added; 3 'related' answers (a hotel / highway / resort carrying the name) are refused or replaced."""
    def tally(strict):
        out = {}
        for r in BAKEOFF:
            if r["generic"] or not r["results"]:
                continue
            v = gm.accept(r["name"], gm.candidates({"results": r["results"]}), *r["ref"],
                          guards.iso2(r["country"]), strict=strict)
            lab = "none" if v.accepted is None else r["labels"].get(str(v.rank), "unlabelled")
            out[lab] = out.get(lab, 0) + 1
        return out
    old, new = tally(False), tally(True)
    assert new["N"] == old["N"] == 30 and new.get("W", 0) <= old.get("W", 0)
    assert old["R"] - new["R"] <= 3


# ── 2. strategy v2: names, queries, bias ─────────────────────────────────────────────────────────────────────────────

def test_name_sim_v2_survives_transliteration_joins_and_mount():
    assert gm.name_sim("Konglor water cave", "Kong Lor Cave") == 0.0
    assert gm.name_sim_v2("Konglor water cave", "Kong Lor Cave") == 0.5
    assert gm.name_sim_v2("Kong Lor", "Konglor Cave") == 1.0
    assert gm.name_sim_v2("Pingan", "Ping'an") == 1.0
    assert gm.name_sim_v2("Mt Palay-Palay", "Mount Palay-Palay") == 1.0
    assert gm.name_sim_v2("Kumbum Monastery in Qinghai", "Kumbum Monastery") < 1.0    # context is not in the bare name
    assert gm.name_sim_v2("Kumbum Monastery", "Kumbum Monastery") == 1.0
    cave = _c(17.98, 104.81, "Kong Lor Cave", iso="la")
    assert gm.accept("Konglor Cave", [cave], 17.97, 104.80, "la").accepted is None
    assert gm.accept("Konglor Cave", [cave], 17.97, 104.80, "la", compact=True).accepted is cave


def test_query_plan_adds_context_but_judges_the_bare_name():
    # the name's own "in / at <Proper>" disambiguator goes first, then the bare name
    assert gm.query_plan("Kumbum Monastery in Qinghai") == [
        ("Kumbum Monastery, Qinghai", "Kumbum Monastery"), ("Kumbum Monastery", "Kumbum Monastery")]
    # a generic X ("Great Wall") is never searched alone: the whole name, then "<Context> X" (v2 sent nothing before)
    assert gm.query_plan("Great Wall at Mutianyu") == [
        ("Great Wall at Mutianyu", "Great Wall at Mutianyu"), ("Mutianyu Great Wall", "Great Wall at Mutianyu")]
    assert gm.name_sim_v2("Great Wall at Mutianyu", "Mutianyu Great Wall") == 1.0
    # a parenthetical naming a site is an alias, searched and judged on its own (S254 verified names)
    assert gm.query_plan("Palace of the Winds (Hawa Mahal)") == [
        ("Palace of the Winds", "Palace of the Winds"), ("Hawa Mahal", "Hawa Mahal")]
    assert gm.query_plan("National Museum (Hor Kham or Royal Palace)")[1] == ("Hor Kham palace", "Hor Kham palace")
    # a parenthetical / comma part with no proper word is neither alias nor context
    assert gm.query_plan("the Dzong (fort)") == [("the Dzong", "the Dzong")]
    assert gm.query_plan("Big Wild Goose Pagoda, north square", context="Xi'an") == [
        ("Big Wild Goose Pagoda", "Big Wild Goose Pagoda"), ("Big Wild Goose Pagoda, Xi'an", "Big Wild Goose Pagoda")]
    assert gm.query_plan("Seven Star Crags (Qixingyan)")[1] == ("Seven Star Crags, Qixingyan", "Seven Star Crags")
    assert gm.query_plan("Hanok Village, Jeonju")[1] == ("Hanok Village, Jeonju", "Hanok Village")
    assert gm.query_plan("Bell and Drum Tower", context="Xi'an") == [
        ("Bell and Drum Tower", "Bell and Drum Tower"), ("Bell and Drum Tower, Xi'an", "Bell and Drum Tower")]
    assert gm.query_plan("Tham Xang or Tham Hoi caves") == [("Tham Xang caves", "Tham Xang caves")]
    assert gm.query_plan("Yali La Pass (4820 m)") == [("Yali La Pass", "Yali La Pass")]
    assert gm.query_plan("Paro", context="Paro") == [("Paro", "Paro")]          # the town is the place itself
    assert len(gm.query_plan("Cho La Pass to Dzongla", context="Dzongla", max_queries=2)) == 2
    # the Hahoe homonym: a candidate matching only the context town fails on the bare name
    andong = _c(36.57, 128.73, "Andong Folk Village", iso="kr")
    text, bare = gm.query_plan("Hahoe Folk Village, Andong")[1]
    assert "Andong" in text and gm.accept(bare, [andong], 36.54, 128.52, "kr", compact=True).accepted is None


def test_search_bias_uses_the_model_point_when_it_is_trustworthy():
    assert guards.search_bias((), 17.9, 104.8) == (17.9, 104.8)                    # v1 sent no bias at all here
    assert guards.search_bias((), None, None) is None
    assert guards.search_bias((KONG_LOR_ROUTE,), 17.98, 104.82, "Kong Lor Cave") == (17.98, 104.82)
    assert guards.search_bias((XIAN_ROUTE,), *BEIJING, "Bell Tower") == PINGYAO    # off route: the nearest stop
    assert guards.search_bias((XIAN_ROUTE,), None, None) == guards.route_bias((XIAN_ROUTE,))


def _http(*payloads):
    http = MagicMock()
    http.get = AsyncMock(side_effect=[MagicMock(status_code=200, json=(lambda p=p: p), headers={}) for p in payloads])
    return http


def _res(name, lat, lng, rtype="amenity", cc="cn", match="full_match"):
    return {"name": name, "lat": lat, "lon": lng, "country_code": cc, "result_type": rtype,
            "rank": {"confidence": 1, "match_type": match}, "place_id": "x", "datasource": {"sourcename": "osm"}}


@pytest.mark.asyncio
async def test_geoapify_v2_tries_the_context_query_after_a_miss():
    http = _http({"results": [_res("Bell Tower", *BEIJING)]}, {"results": [_res("Bell Tower", 34.261, 108.943)]})
    g = gc.GeoapifyGeocoder(http=http, api_key="k")
    g._throttle.interval = 0.0
    got = await g.confirm("Bell Tower", *BEIJING, "China", routes=(XIAN_ROUTE,), strategy="v2", context="Xi'an")
    assert isinstance(got, gc.Confirmed) and got.lat == 34.261 and g.requests == 2
    assert got.meta["query"] == "Bell Tower, Xi'an" and got.meta["strategy"] == "v2"
    assert got.meta["rule"] == gm.ACCEPT_RULE and got.meta["tried"][0]["rejected"] == ["0:off_route"]
    first, second = (c[1]["params"] for c in http.get.call_args_list)
    assert first["text"] == "Bell Tower" and second["text"] == "Bell Tower, Xi'an"
    assert first["bias"] == f"proximity:{PINGYAO[1]:.5f},{PINGYAO[0]:.5f}"       # the hint is off the route


@pytest.mark.asyncio
async def test_geoapify_v1_is_unchanged_one_query_and_route_bias():
    http = _http({"results": []})
    g = gc.GeoapifyGeocoder(http=http, api_key="k")
    g._throttle.interval = 0.0
    got = await g.confirm("Bell Tower", 17.9, 104.8, "Laos", context="Xi'an")
    assert isinstance(got, gc.Unconfirmed) and g.requests == 1
    assert "bias" not in http.get.call_args[1]["params"]


# ── 3. place_relocate v2 ─────────────────────────────────────────────────────────────────────────────────────────────

def _row(name, lat, lng, country="Laos", **kw):
    return {"id": kw.pop("id", "d-" + name), "name": name, "country": country, "lat": lat, "lng": lng,
            "located_by": "llm", "geo_status": "ok", "geo_precision": "area", "review_reason": None,
            "name_norm": name.lower(), "locality_norm": "", "geocoder_meta": None, **kw}


def _confirmed_row(name, lat, lng, meta, country="Laos", outcome="confirmed"):
    stamp = {"v": "aa997.1", "outcome": outcome} if outcome else None
    m = dict(meta, **({"relocate": stamp} if stamp else {}))
    return _row(name, lat, lng, country, located_by="geoapify", geo_precision="point", geocoder_meta=json.dumps(m))


def test_rejudge_refuses_a_stored_answer_the_strict_rule_would_refuse():
    from services.places import relocate
    ok = _confirmed_row("Kong Lor Cave", 17.98, 104.81, {"query": "Kong Lor Cave", "name": "Kong Lor Cave",
                                                         "result_type": "amenity", "match_type": "full_match"})
    assert relocate.rejudge(ok, (KONG_LOR_ROUTE,)) == {"ok": True, "reason": None}
    hotel = _confirmed_row("Kong Lor Cave", 17.98, 104.81, {"query": "Kong Lor Cave",
                                                            "name": "Kong Lor Cave Guesthouse",
                                                            "result_type": "amenity"})
    assert relocate.rejudge(hotel, (KONG_LOR_ROUTE,))["reason"] == "kind"
    anna = _confirmed_row("Mt Annapurna", 28.42, 83.74, {"query": "Mt Annapurna", "name": "Annapurna",
                                                         "result_type": "district"}, "Nepal")
    assert relocate.rejudge(anna, ())["reason"] == "type"
    bell = _confirmed_row("Bell Tower", 34.261, 108.943, {"query": "Bell Tower", "name": "Bell Tower",
                                                          "result_type": "amenity"}, "China")
    assert relocate.rejudge(bell, (XIAN_ROUTE, BEIJING_ROUTE))["reason"] == "ambiguous_route"
    assert relocate.rejudge(_confirmed_row("X", 1.0, 1.0, {}), ())["reason"] == "no_meta"
    assert relocate.is_confirmed_recheck(bell) and not relocate.is_confirmed_recheck(_row("Kong Lor", 1, 1))


class _Geo:
    calls_external = True
    name = "fake"
    cost_per_request = 0.0

    def __init__(self, results=None):
        self.results, self.calls, self.requests = results or {}, [], 0

    async def confirm(self, name, lat, lng, country, *, native_name=None, routes=(), settlement=False, **kw):
        self.requests += 1
        self.calls.append({"name": name, "hint": None if lat is None else (lat, lng), "routes": routes, **kw})
        return self.results.get(name)


def _ok(lat, lng, country="Laos"):
    return gc.Confirmed(lat, lng, country, "pid", "geoapify", {"provider": "geoapify", "result_type": "amenity"})


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


@pytest.mark.asyncio
async def test_recheck_keeps_a_passing_confirmation_without_a_call(monkeypatch):
    from services.places import relocate, store
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    row = _confirmed_row("Kong Lor Cave", 17.98, 104.81, {"query": "Kong Lor Cave", "name": "Kong Lor Cave",
                                                          "result_type": "amenity"})
    conn, geo = _RecConn({"located_by": "geoapify", "geo_status": "ok", "merged": False}), _Geo()
    r = await relocate.apply_row(conn, row, (KONG_LOR_ROUTE,), geo, hide_unconfirmed=False)
    assert r == {"outcome": "kept"} and geo.calls == [] and conn.stamps()[0]["outcome"] == "kept"


@pytest.mark.asyncio
async def test_recheck_searches_again_and_writes_or_flags_weak_confirm(monkeypatch):
    from services.places import relocate, store
    written = []

    async def write_point(conn, dest_id, located, force):
        written.append((dest_id, located.lat))
    monkeypatch.setattr(store, "_write_point", write_point)
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    cur = {"located_by": "geoapify", "geo_status": "ok", "merged": False}
    row = _confirmed_row("Kong Lor Cave", 17.70, 104.75, {"query": "Kong Lor Cave",
                                                          "name": "Kong Lor Cave Guesthouse", "result_type": "amenity"})
    geo = _Geo({"Kong Lor Cave": _ok(17.98, 104.81)})
    conn = _RecConn(cur)
    r = await relocate.apply_row(conn, row, (KONG_LOR_ROUTE,), geo, hide_unconfirmed=False, strategy="v2",
                                 context="Ban Kong Lo")
    assert r["outcome"] == "reconfirmed" and written == [(row["id"], 17.98)]
    assert geo.calls[0]["hint"] is None and geo.calls[0]["strategy"] == "v2"         # the route is the reference
    stamp = conn.stamps()[0]
    assert stamp["prev"] == [17.70, 104.75, "geoapify"] and stamp["refused"] == "kind"
    conn = _RecConn(cur)
    r = await relocate.apply_row(conn, row, (KONG_LOR_ROUTE,), _Geo(), hide_unconfirmed=True)
    assert r["outcome"] == "weak_confirm"
    flag = [a for s, a in conn.sql if s.startswith("UPDATE shared.destinations SET review_reason")][0]
    assert flag == (row["id"], relocate.REVIEW_WEAK_CONFIRM, True)
    # no route: the current point is the reference of the new search
    geo = _Geo()
    await relocate.apply_row(_RecConn(cur), row, (), geo, hide_unconfirmed=False)
    assert geo.calls[0]["hint"] == (17.70, 104.75)


@pytest.mark.asyncio
async def test_v2_relocate_passes_strategy_and_town_and_keeps_the_previous_point(monkeypatch):
    from services.places import relocate, store
    monkeypatch.setattr(store, "_write_point", AsyncMock())
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    geo = _Geo({"Kong Lor Cave": _ok(17.98, 104.81)})
    conn = _RecConn()
    r = await relocate.apply_row(conn, _row("Kong Lor Cave", 17.30, 105.40), (KONG_LOR_ROUTE,), geo,
                                 hide_unconfirmed=False, strategy="v2", context="Ban Kong Lo")
    assert r["outcome"] == "confirmed" and geo.calls[0]["context"] == "Ban Kong Lo"
    assert conn.stamps()[0]["prev"] == [17.30, 105.40, "llm"] and conn.stamps()[0]["v"] == "aa997.2"


def test_context_town_is_the_most_used_overnight_town_not_the_place_itself():
    from services.places import relocate
    towns = [("Thakhek", 17.40, 104.80, 1), ("Ban Kong Lo", 18.00, 104.90, 3), ("Kong Lor Cave", 17.98, 104.81, 9)]
    assert relocate.context_town("Kong Lor Cave", towns, 17.9, 104.8) == "Ban Kong Lo"
    assert relocate.context_town("Kong Lor Cave", [], 17.9, 104.8) is None


@pytest.mark.asyncio
async def test_v2_dry_run_rejudges_confirmations_and_caps_the_estimate(monkeypatch):
    from services.places import relocate
    rows = [_row("Kong Lor Cave", 17.30, 105.40),
            _confirmed_row("Mt Annapurna", 28.42, 83.74, {"query": "Mt Annapurna", "name": "Annapurna",
                                                          "result_type": "district"}, "Nepal"),
            _confirmed_row("Tashilhunpo Monastery", 29.27, 88.87, {"query": "Tashilhunpo Monastery",
                                                                   "name": "Tashilhunpo Monastery",
                                                                   "result_type": "amenity"}, "China")]
    monkeypatch.setattr(relocate, "candidates", AsyncMock(return_value=rows))
    monkeypatch.setattr(relocate, "routes_by_destination", AsyncMock(return_value={rows[0]["id"]: (KONG_LOR_ROUTE,)}))
    monkeypatch.setattr(relocate, "towns_by_destination",
                        AsyncMock(return_value={rows[0]["id"]: [("Ban Kong Lo", 18.0, 104.9, 2)]}))
    out = await relocate.dry_run(None, located_by=["llm"], country=None, cost_per_request=0.0, daily_credits=1500,
                                 recheck=["confirmed"], strategy="v2", max_requests=2)
    assert out["rejudge_confirmed"] == {"rows": 2, "kept": 1, "refused": 1, "refused_by": {"type": 1}}
    assert out["rows"] == 3 and out["with_context"] == 1 and out["by_previous_outcome"] == {"untouched": 1,
                                                                                           "confirmed": 2}
    # Kong Lor: plain + town query (2); Annapurna re-searched: 1 → upper bound 3, capped at 2
    assert out["estimate"]["geoapify_requests"] == 3 and out["estimate"]["capped_requests"] == 2
    assert out["recheck"] == ["confirmed"] and out["strategy"] == "v2"


@pytest.mark.asyncio
async def test_apply_stops_at_the_request_cap(monkeypatch):
    from services.places import relocate
    rows = [_row(f"Wat {i}", 17.9, 102.6) for i in range(5)]
    monkeypatch.setattr(relocate, "candidates", AsyncMock(return_value=rows))
    monkeypatch.setattr(relocate, "routes_by_destination", AsyncMock(return_value={}))
    monkeypatch.setattr(relocate, "count_candidates", AsyncMock(return_value=3))
    geo = _Geo()

    async def one_row(conn, row, routes, geocoder, **kw):
        geocoder.requests += 1
        return {"outcome": "llm_point"}
    monkeypatch.setattr(relocate, "apply_row", one_row)

    class _P:
        def acquire(self):
            import contextlib

            @contextlib.asynccontextmanager
            async def acq():
                yield None
            return acq()
    out = await relocate.apply(_P(), geo, located_by=["llm"], country=None, limit=5, max_requests=2)
    assert out["done"] == 2 and out["stopped_request_cap"] and out["remaining"] == 3


def test_v2_payload_keys_and_an_old_payload_reads_as_before():
    from services.jobs import place_relocate_job as job
    old = job._payload({"mode": "apply", "located_by": ["llm"]})
    assert old == {"mode": "apply", "located_by": ["llm"], "country": None, "limit": 400, "hide_unconfirmed": False}
    p = job._payload({"mode": "apply", "recheck": ["confirmed", "bogus", "llm_point"], "strategy": "v2",
                      "max_requests": "900", "spent": 300, "rule_version": "aa997.2"})
    assert p["recheck"] == ["confirmed", "llm_point"] and p["strategy"] == "v2"
    assert (p["max_requests"], p["spent"], p["rule_version"]) == (900, 300, "aa997.2")
    h = job._payload({"mode": "hide", "reasons": ["llm_point", "manual"]})
    assert h["reasons"] == ["llm_point"] and h["confirm"] is False
    assert job._payload({"mode": "bogus"})["mode"] == "dry_run"


@pytest.mark.asyncio
async def test_a_chain_queued_under_the_old_rule_refuses_after_the_deploy(monkeypatch):
    from services.jobs import place_relocate_job as job
    monkeypatch.setenv("GEOCODER_PROVIDER", "geoapify")
    monkeypatch.delenv("GEOAPIFY_DAILY_CREDIT_LIMIT", raising=False)
    ctx = MagicMock()
    ctx.payload = {"mode": "apply", "reason": "place_relocate:continue"}          # an aa997.1 chain job
    out = await job.run(ctx)
    assert out["status"] == "refused_rule_version_changed" and out["payload_rule_version"] == "aa997.1"
    ctx.payload = {"mode": "apply", "rule_version": "aa997.2", "max_requests": 100, "spent": 100}
    assert (await job.run(ctx))["status"] == "stopped_request_cap"


class _HideConn:
    def __init__(self, rows):
        self.rows, self.sql, self.fetch_args = rows, [], []

    async def fetch(self, sql, *a):
        self.fetch_args.append(a)
        return self.rows

    async def execute(self, sql, *a):
        self.sql.append((" ".join(sql.split())[:50], a))
        return "UPDATE 1"

    def transaction(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def tx():
            yield
        return tx()


class _HidePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def acq():
            yield self.conn
        return acq()


@pytest.mark.asyncio
async def test_hide_reports_first_and_writes_only_on_confirm_and_unhide_reverts(monkeypatch):
    from services.places import relocate, store
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())
    rows = [{"id": "d1", "name": "Pelkor Chode", "country": "China", "review_reason": "llm_point", "located_by": "llm"},
            {"id": "d2", "name": "Barafu", "country": "Tanzania", "review_reason": "off_route", "located_by": "llm"}]
    conn = _HideConn(rows)
    out = await relocate.hide(_HidePool(conn), reasons=list(relocate.HIDE_REASONS), country=None, confirm=False)
    assert out["rows"] == 2 and out["by_reason"] == {"llm_point": 1, "off_route": 1} and conn.sql == []
    out = await relocate.hide(_HidePool(conn), reasons=list(relocate.HIDE_REASONS), country=None, confirm=True)
    assert out["hidden"] == 2
    hide_stamp = json.loads([a for s, a in conn.sql if s.startswith("UPDATE shared.destinations")][0][1])
    assert hide_stamp["by"] == relocate.HIDE_STAMP and hide_stamp["reason"] == "llm_point"
    assert len([s for s, _ in conn.sql if "geo.place_name" in s]) == 2
    conn = _HideConn([{"id": "d1"}])
    out = await relocate.unhide(_HidePool(conn), country=None)
    assert out == {"mode": "unhide", "rule_version": relocate.RULE_VERSION, "country": None, "rows": 1, "unhidden": 1}
    assert any("status = 'resolved'" in " ".join(s.split()) or "geo.place_name" in s for s, _ in conn.sql)


def test_hide_reasons_never_include_a_confirmed_or_manual_row():
    from services.places import relocate
    assert set(relocate.HIDE_REASONS) == {locate.REVIEW_LLM_POINT, locate.REVIEW_OFF_ROUTE,
                                          locate.REVIEW_UNCONFIRMED_HARD, relocate.REVIEW_WEAK_CONFIRM}
    assert relocate.hide_reasons(["manual", "centroid_fallback"]) == sorted(relocate.HIDE_REASONS)
    # non-place pins are opt-in: never in the default set, honoured when asked
    assert relocate.HIDE_NON_PLACE not in relocate.hide_reasons(None)
    assert relocate.hide_reasons(["non_place", "llm_point"]) == ["llm_point", "non_place"]


@pytest.mark.asyncio
async def test_hide_non_place_is_a_prefix_match_sent_as_its_own_flag(monkeypatch):
    from services.places import relocate
    conn = _HideConn([{"id": "d3", "name": "Kathmandu to Pokhara", "country": "Nepal",
                       "review_reason": "non_place:route_segment", "located_by": "llm"}])
    out = await relocate.hide(_HidePool(conn), reasons=["llm_point", "non_place"], country="Nepal", confirm=False)
    assert conn.fetch_args[0] == (["llm_point"], "Nepal", True, "non_place")
    assert out["by_reason"] == {"non_place:route_segment": 1} and out["hidden"] == 0
    await relocate.hide(_HidePool(conn), reasons=["llm_point"], country=None, confirm=False)
    assert conn.fetch_args[1] == (["llm_point"], None, False, "non_place")


class _AdminConn:
    def __init__(self, last):
        self.last = last

    async def fetchrow(self, sql, *a):
        return None if "'queued'" in sql else self.last


class _AdminPool(_HidePool):
    pass


@pytest.mark.asyncio
async def test_endpoint_apply_needs_a_dry_run_of_the_same_strategy_and_recheck(monkeypatch):
    from fastapi import HTTPException

    from api.routers import admin as admin_module
    from api.routers import admin_places
    from services.places import relocate
    from shared.jobs import registry
    monkeypatch.setattr(admin_module, "ADMIN_SECRET", "s")
    queued = []

    async def fake_enqueue(conn, kind, payload, **kw):
        queued.append(payload)
        return "job-1", True
    monkeypatch.setattr(registry, "enqueue", fake_enqueue)
    v1_dry = {"id": "dry-1", "result": json.dumps({"rule_version": relocate.RULE_VERSION, "located_by": ["llm"],
                                                   "country": None})}
    request = MagicMock()
    request.app.state.pool = _AdminPool(_AdminConn(v1_dry))
    body = admin_places.RelocateRequest(mode="apply", strategy="v2", recheck=["confirmed"], max_requests=3000)
    with pytest.raises(HTTPException) as e:
        await admin_places.relocate_run(body, request, x_admin_secret="s")
    assert e.value.status_code == 409
    v2_dry = {"id": "dry-2", "result": json.dumps({"rule_version": relocate.RULE_VERSION, "located_by": ["llm"],
                                                   "country": None, "recheck": ["confirmed"], "strategy": "v2"})}
    request.app.state.pool = _AdminPool(_AdminConn(v2_dry))
    out = await admin_places.relocate_run(body, request, x_admin_secret="s")
    p = queued[-1]
    assert out["job_id"] == "job-1" and p["strategy"] == "v2" and p["recheck"] == ["confirmed"]
    assert p["max_requests"] == 3000 and p["rule_version"] == relocate.RULE_VERSION
    out = await admin_places.relocate_run(admin_places.RelocateRequest(mode="hide"), request, x_admin_secret="s")
    assert queued[-1]["mode"] == "hide" and queued[-1]["confirm"] is False
    with pytest.raises(ValueError):
        admin_places.RelocateRequest(recheck=["non_place"])


# ── 5. GEOCODER_STRATEGY for new names (place_geocode) ──────────────────────────────────────────────────────────────

def test_default_strategy_reads_the_env_and_falls_back_to_v1(monkeypatch):
    monkeypatch.delenv("GEOCODER_STRATEGY", raising=False)
    assert gc.default_strategy() == "v1"
    monkeypatch.setenv("GEOCODER_STRATEGY", "V2")
    assert gc.default_strategy() == "v2"
    monkeypatch.setenv("GEOCODER_STRATEGY", "fancy")
    assert gc.default_strategy() == "v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("env, sent", [(None, {}), ("v2", {"strategy": "v2"})])
async def test_locate_sends_the_strategy_only_when_v2(monkeypatch, env, sent):
    if env is None:
        monkeypatch.delenv("GEOCODER_STRATEGY", raising=False)
    else:
        monkeypatch.setenv("GEOCODER_STRATEGY", env)
    geo = MagicMock()
    geo.calls_external = True
    geo.confirm = AsyncMock(return_value=gc.Confirmed(17.98, 104.81, "Laos", "p", "geoapify", {"provider": "geoapify"}))
    item = locate.Item(("kong lor cave", "laos", ""), "Kong Lor Cave", "Laos", routes=(KONG_LOR_ROUTE,))
    out = await locate._resolve(item, locate.Proposal(17.97, 104.80, "Laos"), geo, True)
    assert out.point is not None and geo.confirm.await_count == 1
    kwargs = geo.confirm.call_args.kwargs
    assert {k: v for k, v in kwargs.items() if k == "strategy"} == sent
