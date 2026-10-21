"""AA-847 (D1) — the place_shape run (services/places/shape_run.py), the job kind (services/jobs/place_shape_job.py),
the per-day stretches of the graph build (graph.line_segments) and the shared Geoapify lock. Fakes only: no DB, no
network, no paid call."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from services.places import graph, shape as S, shape_run, shape_sources as src, store

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "aa847_shapes")
MEKONG = [[(100.41, 20.27), (100.55, 20.15), (100.75, 20.05), (100.95, 19.95), (101.13, 19.89), (101.40, 19.85),
           (101.70, 19.95), (101.95, 20.00), (102.13, 19.89)]]


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield SimpleNamespace()


class _Writes:
    def __init__(self, monkeypatch, rows, infos):
        self.drawn, self.miss = [], []

        async def due(conn, limit, statuses=store.SHAPE_DUE_STATUSES):
            return rows[:limit]

        async def uses(conn, ids):
            return {i: infos.get(i, {}) for i in ids}

        async def write_shape(conn, did, **kw):
            self.drawn.append((did, kw))
            return True

        async def write_miss(conn, did, **kw):
            self.miss.append((did, kw))
            return True
        monkeypatch.setattr(store, "due_shapes", due)
        monkeypatch.setattr(store, "shape_uses", uses)
        monkeypatch.setattr(store, "write_shape", write_shape)
        monkeypatch.setattr(store, "write_shape_miss", write_miss)


def _row(did, name, lat, lng, place_id=None, status="pending", country="Laos"):
    return {"destination_id": did, "name": name, "country": country, "lat": lat, "lng": lng,
            "geocoder_place_id": place_id, "geocoder_provider": "geoapify" if place_id else None, "status": status,
            "source": None, "tries": 0}


# Huay Xai (day 1 stop) → Pak Beng (day 2 stop): the slow boat day uses the Mekong
SLOW_BOAT = {"uses": [("s1", 2)],
             "stops": {("s1", 1): ("hx", 20.27, 100.41), ("s1", 2): ("pb", 19.89, 101.13)},
             "points": {("s1", 2): [("pb", 19.89, 101.13)]}}


class _Details:
    def __init__(self, cand):
        self.cand, self.credits, self.requests, self.asked = cand, 0, 0, []

    async def line(self, place_id):
        self.asked.append(place_id)
        self.requests += 1
        self.credits += 1
        return self.cand


class _Overpass:
    def __init__(self, cands):
        self.cands, self.requests, self.asked = cands, 0, []

    async def lines(self, name, iso2, anchors, line_class):
        self.asked.append((name, iso2, line_class))
        self.requests += 1
        return self.cands


@pytest.mark.asyncio
async def test_a_single_way_from_details_is_replaced_by_the_overpass_relation(monkeypatch):
    w = _Writes(monkeypatch, [_row("m", "Mekong River", 20.0, 100.8, "51mekongway11")], {"m": SLOW_BOAT})
    details = _Details(src.parse_details(_load("geoapify_details_mekong_way.json")))
    over = _Overpass(src.parse_overpass(_load("overpass_mekong_laos.json")))
    out = await shape_run.resolve_shapes(_Pool(), details=details, overpass=over, limit=10)
    assert out["ok"] == 1 and over.asked == [("Mekong River", "la", "water")]
    did, kw = w.drawn[0]
    assert kw["status"] == "ok" and kw["osm_type"] == "relation" and kw["osm_id"] == 2091005
    geom, mid, lo = kw["levels"]
    assert geom["type"] == "LineString" and len(lo["coordinates"]) <= S.LO_VERTICES
    assert kw["meta"]["attribution"] == "© OpenStreetMap contributors"


@pytest.mark.asyncio
async def test_details_relation_needs_no_overpass(monkeypatch):
    nk = {"uses": [("s2", 1)], "stops": {("s2", 1): ("nk", 20.57, 102.61)}, "points": {}}
    w = _Writes(monkeypatch, [_row("o", "Nam Ou River", 20.57, 102.61, "51namourel")], {"o": nk})
    over = _Overpass([])
    out = await shape_run.resolve_shapes(
        _Pool(), details=_Details(src.parse_details(_load("geoapify_details_nam_ou_relation.json"))),
        overpass=over, limit=10)
    assert out["ok"] == 1 and over.asked == [] and w.drawn[0][1]["source"] == "geoapify_details"


@pytest.mark.asyncio
async def test_no_feature_falls_back_to_the_anchored_approximate_route(monkeypatch):
    """Ha Giang Loop: no single OSM feature → the day's anchors drawn as an approximate route (never nothing)."""
    loop = {"uses": [("s3", 2)],
            "stops": {("s3", 1): ("hg", 22.82, 104.98), ("s3", 2): ("dv", 23.27, 105.32)},
            "points": {("s3", 2): [("qb", 23.06, 105.03)]}}
    w = _Writes(monkeypatch, [_row("l", "Ha Giang Loop", 23.0, 105.1, country="Vietnam")], {"l": loop})
    out = await shape_run.resolve_shapes(_Pool(), details=None, overpass=_Overpass([]), limit=10)
    assert out["approximate"] == 1
    kw = w.drawn[0][1]
    assert kw["status"] == "approximate" and kw["source"] == "anchors" and kw["osm_id"] is None
    assert len(kw["levels"][0]["coordinates"]) == 3                 # Ha Giang → Quan Ba → Dong Van


@pytest.mark.asyncio
async def test_no_feature_and_no_anchors_is_not_found(monkeypatch):
    w = _Writes(monkeypatch, [_row("x", "Tea Horse Route", 22.0, 101.0, country="Laos")], {"x": {}})
    out = await shape_run.resolve_shapes(_Pool(), details=None, overpass=None, limit=10)
    assert out["not_found"] == 1 and w.miss[0][1]["status"] == "not_found" and not w.drawn


@pytest.mark.asyncio
async def test_model_only_makes_no_call_but_still_draws_the_approximate_route(monkeypatch):
    loop = {"uses": [("s3", 2)], "stops": {("s3", 1): ("hg", 22.82, 104.98), ("s3", 2): ("dv", 23.27, 105.32)},
            "points": {}}
    w = _Writes(monkeypatch, [_row("l", "Ha Giang Loop", 23.0, 105.1, country="Vietnam")], {"l": loop})
    out = await shape_run.resolve_shapes(_Pool(), details=None, overpass=None, limit=10)
    assert out["approximate"] == 1 and w.drawn


@pytest.mark.asyncio
async def test_an_approximate_route_rechecked_without_a_feature_stays_approximate(monkeypatch):
    w = _Writes(monkeypatch, [_row("l", "Ha Giang Loop", 23.0, 105.1, status="approximate", country="Vietnam")],
                {"l": {}})
    out = await shape_run.resolve_shapes(_Pool(), details=None, overpass=_Overpass([]), limit=10)
    assert out["approximate"] == 1 and not w.drawn and w.miss[0][1]["retry_days"] == shape_run.RETRY_APPROXIMATE_DAYS


@pytest.mark.asyncio
async def test_credit_limit_stops_the_run_without_a_try(monkeypatch):
    from services.places.geocoder import CreditLimitReached
    w = _Writes(monkeypatch, [_row("m", "Mekong River", 20.0, 100.8, "p1"), _row("n", "Nam Ou", 20.5, 102.6, "p2")],
                {})

    class _Broke(_Details):
        async def line(self, place_id):
            raise CreditLimitReached("guard")
    out = await shape_run.resolve_shapes(_Pool(), details=_Broke(None), overpass=None, limit=10)
    assert out.get("stopped_credit_limit") and out["names"] == 0 and not w.miss and not w.drawn


@pytest.mark.asyncio
async def test_one_failing_name_does_not_stop_the_run(monkeypatch):
    w = _Writes(monkeypatch, [_row("a", "Mekong River", 20.0, 100.8, "p1"),
                              _row("b", "Ha Giang Loop", 23.0, 105.1, country="Vietnam")], {})

    class _Err(_Details):
        async def line(self, place_id):
            raise src.ShapeSourceError("overpass: HTTP 400")
    out = await shape_run.resolve_shapes(_Pool(), details=_Err(None), overpass=None, limit=10)
    assert out["failed"] == 1 and out["not_found"] == 1
    assert [m[1]["status"] for m in w.miss] == ["failed", "not_found"]


def test_anchor_points_and_day_routes():
    row = _row("m", "Mekong River", 20.0, 100.8)
    pts = shape_run.anchor_points(row, SLOW_BOAT)
    assert (20.0, 100.8) in pts and (20.27, 100.41) in pts and (19.89, 101.13) in pts
    assert shape_run.day_routes(SLOW_BOAT) == [[(20.27, 100.41), (19.89, 101.13)]]


# ── the job kind ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_job_is_off_by_default(monkeypatch):
    from services.jobs import place_shape_job as job
    monkeypatch.delenv(job.ENABLED_ENV, raising=False)
    assert (await job.run(SimpleNamespace(payload={}, pool=None, job_id="j")))["status"] == "disabled"


@pytest.mark.asyncio
async def test_job_waits_while_place_geocode_holds_the_geoapify_lock(monkeypatch):
    from services.jobs import place_shape_job as job
    monkeypatch.setenv(job.ENABLED_ENV, "true")

    @asynccontextmanager
    async def taken(pool):
        yield False
    enq = []

    async def fake_enqueue(pool, kind, **kw):
        enq.append((kind, kw))
        return "next", True
    monkeypatch.setattr(job, "geoapify_lock", taken)
    monkeypatch.setattr(job, "enqueue_debounced", fake_enqueue)
    out = await job.run(SimpleNamespace(payload={}, pool=object(), job_id="j"))
    assert out["status"] == "waiting_for_place_geocode" and enq[0][0] == "place_shape"


@pytest.mark.asyncio
async def test_place_geocode_waits_while_place_shape_holds_the_lock(monkeypatch):
    from services.jobs import place_geocode_job as job

    @asynccontextmanager
    async def taken(pool):
        yield False

    async def fake_enqueue(pool, kind, **kw):
        return "next", True
    monkeypatch.setattr(job, "geoapify_lock", taken)
    monkeypatch.setattr(job, "enqueue_debounced", fake_enqueue)
    out = await job.run(SimpleNamespace(payload={}, pool=object(), job_id="j"))
    assert out == {"status": "waiting_for_place_shape", "next_job": "next"}


def test_model_only_has_no_clients():
    from services.jobs import place_shape_job as job
    assert job.make_clients("model_only", 0) == (None, None)


@pytest.mark.asyncio
async def test_geoapify_lock_is_released():
    from services.places.geolock import GEOAPIFY_LOCK_KEY, geoapify_lock
    calls = []

    class Conn:
        async def fetchval(self, sql, key):
            calls.append(("try", key))
            return True

        async def execute(self, sql, key):
            calls.append(("unlock", key))

    class Pool:
        async def acquire(self):
            return Conn()

        async def release(self, conn):
            calls.append(("release",))
    async with geoapify_lock(Pool()) as held:
        assert held
    assert calls == [("try", GEOAPIFY_LOCK_KEY), ("unlock", GEOAPIFY_LOCK_KEY), ("release",)]


# ── graph build: per-day stretches ───────────────────────────────────────────────────────────────────────────────

def test_line_segments_rows():
    dests = {"hx": {"name": "Huay Xai", "lat": 20.27, "lng": 100.41, "precision": "point"},
             "pb": {"name": "Pak Beng", "lat": 19.89, "lng": 101.13, "precision": "point"},
             "m": {"name": "Mekong River", "lat": 20.0, "lng": 100.8, "precision": "line"},
             "o": {"name": "Nam Ou River", "lat": 20.6, "lng": 102.6, "precision": "line"}}
    stops = [graph.Stop("s1", 1, "hx", "overnight"), graph.Stop("s1", 2, "pb", "overnight")]
    places = [{"source_id": "s1", "day_index": 2, "destination_id": "m"},
              {"source_id": "s1", "day_index": 2, "destination_id": "m"},          # the same river twice: one row
              {"source_id": "s1", "day_index": 2, "destination_id": "o"}]
    days = [{"source_id": "s1", "day_index": 1, "overnight_destination_id": "hx"},
            {"source_id": "s1", "day_index": 2, "overnight_destination_id": "pb"}]
    shapes = {"m": {"status": "ok", "geom": S.to_geojson(MEKONG)}, "o": {"status": "pending", "geom": None}}
    rows = graph.line_segments(stops, places, days, dests, shapes)
    by = {r["destination_id"]: r for r in rows}
    assert len(rows) == 2
    assert by["m"]["basis"] == "day_anchors" and 80 < by["m"]["segment_km"] < 95
    assert (by["m"]["from_destination_id"], by["m"]["to_destination_id"]) == ("hx", "pb")
    # no shape yet: no segment, the two stops kept for the provisional dashed line
    assert by["o"]["basis"] == "feature" and by["o"]["segment"] is None
    assert (by["o"]["from_destination_id"], by["o"]["to_destination_id"]) == ("hx", "pb")


def test_overnight_on_a_line_gets_a_segment_row():
    dests = {"pb": {"name": "Pak Beng", "lat": 19.89, "lng": 101.13, "precision": "point"},
             "m": {"name": "Mekong River", "lat": 20.0, "lng": 100.8, "precision": "line"}}
    stops = [graph.Stop("s1", 1, "pb", "overnight")]
    days = [{"source_id": "s1", "day_index": 1, "overnight_destination_id": "pb"},
            {"source_id": "s1", "day_index": 2, "overnight_destination_id": "m"}]     # "a night on the Mekong"
    rows = graph.line_segments(stops, [], days, dests, {"m": {"status": "ok", "geom": S.to_geojson(MEKONG)}})
    assert [(r["day_index"], r["basis"]) for r in rows] == [(2, "anchor_window")]
