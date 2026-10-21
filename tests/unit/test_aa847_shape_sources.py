"""AA-847 — line geometry sources (services/places/shape_sources.py): parsing recorded Geoapify Place Details and
Overpass answers, the query text, throttling / retries / the credit guard with a fake HTTP client. No network."""
from __future__ import annotations

import json
import os

import pytest

from services.places import geocoder as geocoder_mod
from services.places import shape as S
from services.places import shape_sources as src

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "aa847_shapes")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append(("GET", url, dict(params or {})))
        return self.responses.pop(0)

    async def post(self, url, data=None):
        self.calls.append(("POST", url, dict(data or {})))
        return self.responses.pop(0)

    async def aclose(self):
        pass


async def _nosleep(_s):
    return None


# ── parsing ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_details_relation():
    c = src.parse_details(_load("geoapify_details_nam_ou_relation.json"))
    assert (c.osm_type, c.osm_id, c.source) == ("relation", 5512345, "geoapify_details")
    assert "Nam Ou" in c.names and c.line_class == "water" and len(c.parts) == 2


def test_parse_details_point_is_no_line():
    p = _load("geoapify_details_point.json")
    assert src.parse_details(p) is None and src.details_is_short(p)


def test_parse_overpass_relation_ways_and_membership():
    cands = src.parse_overpass(_load("overpass_mekong_laos.json"))
    rel = [c for c in cands if c.osm_type == "relation"]
    assert len(rel) == 1 and rel[0].osm_id == 2091005 and len(rel[0].parts) == 1     # two member ways chained
    ways = [c for c in cands if c.osm_type == "way" and "Mekong" in c.names]
    assert len(ways) == 1 and sorted(ways[0].way_ids) == [11, 12] and ways[0].member_of == [2091005]
    assert any("Nam Khan" in c.names for c in cands)


def test_overpass_candidates_pass_acceptance_and_the_relation_wins():
    cands = src.parse_overpass(_load("overpass_mekong_laos.json"))
    v = S.accept("Mekong River", cands, [(20.27, 100.41), (19.89, 101.13)], "la")
    assert v.accepted.osm_type == "relation" and v.accepted.osm_id == 2091005 and 180 < v.length_km < 210


# ── query ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_name_regex():
    assert src.name_regex("Ho Chi Minh Trail") == "ho.{0,3}chi.{0,3}minh"
    assert src.name_regex("Nam Ou River") == "(^|[^a-z])ou([^a-z]|$)"
    assert src.name_regex("River") == "river"           # nothing distinctive: the plain word (the name rule decides)


def test_overpass_query_shape():
    q = src.overpass_query("Mekong River", "la", [(20.27, 100.41)], "water")
    assert q.startswith("[out:json][timeout:60][maxsize:67108864];")
    assert 'area["ISO3166-1"="LA"]["admin_level"="2"]->.c;' in q
    assert 'way["waterway"~"^(river|stream|canal)$"][~"^(name|name:en|int_name|name:[a-z_-]+)$"~"mekong",i]' in q
    assert "out body geom(" in q and "rel(bw)->.r;" in q
    s, w, n, e = src.search_bbox([(20.27, 100.41)], "la")
    assert s >= 13.6 and w >= 99.8 and n <= 22.8 and e <= 107.9      # cut to the country box
    assert src.overpass_query("Mekong River", "la", [], "water") is None


# ── clients (fake HTTP) ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_details_full_geometry_only_when_short():
    http = _Http([_Resp(200, _load("geoapify_details_point.json")),
                  _Resp(200, _load("geoapify_details_nam_ou_relation.json"))])
    d = src.GeoapifyDetails(http=http, api_key="SECRETKEY", sleep=_nosleep, rate_per_s=5)
    c = await d.line("51hcmnode")
    assert c is not None and c.osm_id == 5512345
    assert [call[2]["features"] for call in http.calls] == ["details", "details,details.full_geometry"]
    assert d.requests == 2 and d.credits == 3        # 1 + (1 + 1), assumed until the trial reads the dashboard


@pytest.mark.asyncio
async def test_details_one_call_when_the_line_is_complete():
    http = _Http([_Resp(200, _load("geoapify_details_nam_ou_relation.json"))])
    d = src.GeoapifyDetails(http=http, api_key="K", sleep=_nosleep)
    assert (await d.line("x")).osm_id == 5512345 and d.credits == 1


@pytest.mark.asyncio
async def test_details_credit_guard_and_key_never_in_errors():
    d = src.GeoapifyDetails(http=_Http([]), api_key="SECRETKEY", daily_credit_limit=10, credits_used_today=10,
                            sleep=_nosleep)
    with pytest.raises(geocoder_mod.CreditLimitReached):
        await d.fetch("x")
    d = src.GeoapifyDetails(http=_Http([_Resp(500)] * 4), api_key="SECRETKEY", sleep=_nosleep)
    with pytest.raises(src.ShapeSourceError) as e:
        await d.fetch("x")
    assert "SECRETKEY" not in str(e.value) and d.requests == 4
    d = src.GeoapifyDetails(http=_Http([_Resp(401)]), api_key="SECRETKEY", sleep=_nosleep)
    with pytest.raises(geocoder_mod.GeocoderUnavailable):
        await d.fetch("x")


@pytest.mark.asyncio
async def test_overpass_throttle_and_backoff():
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    clock = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    http = _Http([_Resp(429), _Resp(200, {"elements": []}), _Resp(200, _load("overpass_mekong_laos.json"))])
    o = src.Overpass(http=http, sleep=sleep, clock=lambda: next(clock))
    assert await o.query("q1") == {"elements": []}
    cands = await o.lines("Mekong River", "la", [(20.27, 100.41)], "water")
    assert o.requests == 3 and cands
    # backoff 5 s after the 429; then each request waits for the 2 s minimum gap (the fake clock does not move)
    assert sleeps == [5.0, 2.0, 2.0]
    assert http.calls[0][0] == "POST" and "data" in http.calls[0][2]


@pytest.mark.asyncio
async def test_overpass_hard_error():
    o = src.Overpass(http=_Http([_Resp(400)]), sleep=_nosleep)
    with pytest.raises(src.ShapeSourceError):
        await o.query("bad")
