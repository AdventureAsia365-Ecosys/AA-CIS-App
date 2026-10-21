"""AA-847 (owner decision S246, D1) — line geometry against real Postgres (migration 255 CHECKs, store SQL, the
place_shape run with fake sources, geo.tour_segment written by swap_graph, the /admin/places Lines routes).

One Laos tour-day takes the slow boat Huay Xai → Pak Beng on the Mekong (a line destination). No network, no paid
call: the Geoapify / Overpass clients are fakes returning the recorded fixtures' candidates.
"""
import json
import os
import random
import string
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_places  # noqa: E402
from services.places import graph, shape as S, shape_run, shape_sources as src, store  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATION_255 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "255_geo_place_shape.sql")
FIX = os.path.join(os.path.dirname(__file__), "..", "unit", "fixtures", "aa847_shapes")
SECRET = "aa847-shape-secret"
MEKONG = [[(100.41, 20.27), (100.55, 20.15), (100.75, 20.05), (100.95, 19.95), (101.13, 19.89), (101.40, 19.85),
           (101.70, 19.95), (101.95, 20.00), (102.13, 19.89)]]


@pytest.fixture(scope="module", autouse=True)
def migration_255():
    """test_aa712_migration_226 drops schema geo and re-applies 226 only: re-apply 255 (idempotent)."""
    import psycopg2
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True
    with open(MIGRATION_255) as f:
        c.cursor().execute(f.read())
    c.close()


async def _dest(conn, name, lat, lng, precision="point"):
    return await conn.fetchval(
        """INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, geo_precision,
                                            name_norm, locality_norm, geocoder_place_id, geocoder_provider)
           VALUES ($1, 'Laos', $2, $3, 'geoapify', 'ok', $4, geo.name_norm($1), '', $5, 'geoapify')
           RETURNING id::text""", name, lat, lng, precision, f"pid-{name}")


@pytest_asyncio.fixture
async def env(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)
    tag = "zs" + "".join(random.choice(string.ascii_lowercase) for _ in range(8))
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    source, tour_id = f"src-{tag}", str(uuid.uuid4())
    async with pool.acquire() as c:
        d = {"hx": await _dest(c, f"Huay Xai ({tag})", 20.27, 100.41),
             "pb": await _dest(c, f"Pak Beng ({tag})", 19.89, 101.13),
             "mekong": await _dest(c, f"Mekong River ({tag})", 20.0, 100.8, "line"),
             "loop": await _dest(c, f"Bolaven Loop ({tag})", 15.1, 106.3, "line")}
        for day, over in ((1, d["hx"]), (2, d["pb"])):
            await c.execute(
                """INSERT INTO geo.tour_day (source_id, day_index, tour_id, title, overnight_destination_id, places,
                                             extracted_by) VALUES ($1, $2, $3::uuid, 'd', $4::uuid, '{}', 'rule')""",
                source, day, tour_id, over)
        await c.execute(
            """INSERT INTO geo.tour_place (component_id, source_id, tour_id, day_index, name, name_norm, country,
                                           country_norm, destination_id, activity, intensity_level, duration_hint,
                                           text_extract, text_hash)
               VALUES ($1::uuid, $2, $3::uuid, 2, $4, geo.name_norm($4), 'Laos', 'laos', $5::uuid,
                       'water_activities', 'leisurely', 'full_day', 't', 'h')""",
            str(uuid.uuid4()), source, tour_id, f"Mekong River ({tag})", d["mekong"])
        for k in ("mekong", "loop"):
            assert await store.ensure_pending_shape(c, d[k])
    yield SimpleNamespace(pool=pool, tag=tag, d=d, source=source)
    async with pool.acquire() as c:
        ids = list(d.values())
        await c.execute("DELETE FROM geo.tour_segment WHERE source_id = $1", source)
        await c.execute("DELETE FROM geo.place_shape WHERE destination_id = ANY($1::uuid[])", ids)
        await c.execute("DELETE FROM geo.tour_place WHERE source_id = $1", source)
        await c.execute("DELETE FROM geo.tour_day WHERE source_id = $1", source)
        await c.execute("DELETE FROM shared.destinations WHERE id = ANY($1::uuid[])", ids)
        await c.execute("DELETE FROM shared.job WHERE kind = 'place_shape' AND created_by LIKE 'backfill:admin:%'")
    await pool.close()


def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


class _Details:
    def __init__(self, cand):
        self.cand, self.credits, self.requests = cand, 0, 0

    async def line(self, place_id):
        self.requests += 1
        self.credits += 1
        return self.cand


class _Overpass:
    def __init__(self, cands):
        self.cands, self.requests = cands, 0

    async def lines(self, name, iso2, anchors, line_class):
        self.requests += 1
        return self.cands


def _overpass_mekong():
    with open(os.path.join(FIX, "overpass_mekong_laos.json"), encoding="utf-8") as fh:
        return src.parse_overpass(json.load(fh))


async def _shape(env, key):
    async with env.pool.acquire() as c:
        return await c.fetchrow("SELECT * FROM geo.place_shape WHERE destination_id = $1::uuid", env.d[key])


async def _only_mine(env, monkeypatch):
    """Due rows of the shared test DB are filtered to this test's destinations (names carry the tag in parentheses: the
    name rule drops parentheticals, so "Mekong River (zs…)" still matches "Mekong")."""
    real = store.due_shapes

    async def due(conn, limit, statuses=store.SHAPE_DUE_STATUSES):
        return [r for r in await real(conn, 1000, statuses) if r["destination_id"] in env.d.values()][:limit]
    monkeypatch.setattr(store, "due_shapes", due)


async def test_pending_row_carries_the_point_as_bbox(env):
    r = await _shape(env, "mekong")
    assert (r["status"], r["kind"], r["min_lat"], r["max_lng"]) == ("pending", "line", 20.0, 100.8)
    async with env.pool.acquire() as c:
        assert not await store.ensure_pending_shape(c, env.d["mekong"])          # idempotent
        assert not await store.ensure_pending_shape(c, env.d["hx"])              # a point gets no row


async def test_run_draws_the_relation_and_the_approximate_route_within_the_checks(env, monkeypatch):
    await _only_mine(env, monkeypatch)
    out = await shape_run.resolve_shapes(env.pool, details=_Details(None), overpass=_Overpass(_overpass_mekong()),
                                         limit=10)
    assert out["ok"] == 1 and out["not_found"] == 1, out          # the loop has no tour-day: no anchors, not found
    r = await _shape(env, "mekong")
    assert (r["status"], r["source"], r["osm_type"], r["osm_id"]) == ("ok", "overpass", "relation", 2091005)
    assert 180 < float(r["length_km"]) < 210 and r["min_lng"] == pytest.approx(100.41)
    assert json.loads(r["meta"])["attribution"] == "© OpenStreetMap contributors"
    lo = await _shape(env, "loop")
    assert lo["status"] == "not_found" and lo["tries"] == 1 and lo["next_retry_at"] is not None


async def test_a_long_dense_line_fits_the_size_checks(env):
    """5,000 vertices along the river: the three levels pass the 255 CHECKs as PostgreSQL measures them."""
    dense = [[(100.41 + i * 0.00034, 20.27 - i * 0.00008 + (0.0004 if i % 2 else 0.0)) for i in range(5000)]]
    async with env.pool.acquire() as c:
        assert await store.write_shape(c, env.d["mekong"], status="ok", source="overpass", levels=S.levels(dense),
                                       bbox=S.bbox(dense), length_km=S.length_km(dense), osm_type="way", osm_id=1,
                                       meta={}, retry_days=365)
        got = await c.fetchrow(
            """SELECT geo.line_vertex_count(geom) AS g, geo.line_vertex_count(geom_mid) AS m,
                      octet_length(geom_mid::text) AS mb, geo.line_vertex_count(geom_lo) AS l
                 FROM geo.place_shape WHERE destination_id = $1::uuid""", env.d["mekong"])
    assert got["g"] <= 2000 and got["m"] <= 300 and got["mb"] <= 12288 and got["l"] <= 60


async def test_manual_shape_is_never_overwritten_by_the_job(env):
    async with env.pool.acquire() as c:
        assert await store.write_manual_shape(c, env.d["mekong"], S.levels(MEKONG), S.bbox(MEKONG), 200.0, {})
        assert not await store.write_shape(c, env.d["mekong"], status="ok", source="overpass",
                                           levels=S.levels(MEKONG), bbox=S.bbox(MEKONG), length_km=1.0,
                                           osm_type=None, osm_id=None, meta={}, retry_days=1)
        assert await store.clear_manual_shape(c, env.d["mekong"])
    assert (await _shape(env, "mekong"))["status"] == "pending"


async def test_swap_graph_writes_the_day_stretch(env):
    async with env.pool.acquire() as c:
        await store.write_shape(c, env.d["mekong"], status="ok", source="overpass", levels=S.levels(MEKONG),
                                bbox=S.bbox(MEKONG), length_km=S.length_km(MEKONG), osm_type="relation",
                                osm_id=2091005, meta={}, retry_days=365)
        days, places, dests = await store.graph_inputs(c, [env.source])
        g = graph.build_graph(graph.stop_rows(days, places, dests), dests, places)
        assert {str(s.destination_id) for s in g.stops} == {env.d["hx"], env.d["pb"]}      # the river is no stop
        shapes = await store.line_shapes(c, [d for d in dests if graph.is_line(dests, d)])
        g.segments = graph.line_segments(g.stops, places, days, dests, shapes)
        prev = await c.fetchval("SELECT build_id::text FROM geo.graph_build WHERE is_current")
        build = await store.swap_graph(c, g, {"test": "aa847"}, None)
        try:
            row = await c.fetchrow("SELECT * FROM geo.tour_segment WHERE build_id = $1::uuid AND source_id = $2",
                                   build, env.source)
            assert row["basis"] == "day_anchors" and str(row["destination_id"]) == env.d["mekong"]
            assert (str(row["from_destination_id"]), str(row["to_destination_id"])) == (env.d["hx"], env.d["pb"])
            assert 80 < float(row["segment_km"]) < 95 and json.loads(row["segment"])["type"] == "LineString"
        finally:
            await c.execute("DELETE FROM geo.graph_build WHERE build_id = $1::uuid", build)
            if prev:
                await c.execute("UPDATE geo.graph_build SET is_current = true WHERE build_id = $1::uuid", prev)
        assert await c.fetchval("SELECT count(*) FROM geo.tour_segment WHERE build_id = $1::uuid", build) == 0


async def test_lines_route_and_backfill_dry_run(env, monkeypatch):
    monkeypatch.delenv("PLACE_SHAPES_ENABLED", raising=False)
    out = await admin_places.lines(_request(env.pool), country="Laos", x_admin_secret=SECRET)
    mine = {i["destination_id"]: i for i in out["items"] if i["destination_id"] in env.d.values()}
    assert set(mine) == {env.d["mekong"], env.d["loop"]} and mine[env.d["mekong"]]["tour_days"] == 1
    assert out["enabled"] is False and "OpenStreetMap" in out["attribution"]["osm"]
    body = admin_places.ShapeBackfillRequest(destination_ids=[env.d["mekong"]], limit=5)
    dry = await admin_places.shapes_backfill(body, _request(env.pool), x_admin_secret=SECRET, x_admin_user_id="t")
    assert dry["dry_run"] and [i["destination_id"] for i in dry["items"]] == [env.d["mekong"]]
    assert "PLACE_SHAPES_ENABLED is off" in dry["would_refuse"]
    body.dry_run = False
    with pytest.raises(HTTPException) as e:
        await admin_places.shapes_backfill(body, _request(env.pool), x_admin_secret=SECRET, x_admin_user_id="t")
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        await admin_places.lines(_request(env.pool), status="bogus", x_admin_secret=SECRET)
    assert e.value.status_code == 422


async def test_manual_route_clips_to_the_country(env):
    req = admin_places.ManualShapeRequest(geojson=S.to_geojson(MEKONG), note="test")
    out = await admin_places.put_manual_shape(env.d["mekong"], req, _request(env.pool), x_admin_secret=SECRET,
                                              x_admin_user_id="t")
    assert out["status"] == "manual" and out["length_km"] > 150
    with pytest.raises(HTTPException) as e:          # Li River, China: nothing inside Laos
        await admin_places.put_manual_shape(
            env.d["loop"], admin_places.ManualShapeRequest(geojson={"type": "LineString", "coordinates": [
                [110.29, 25.27], [110.5, 24.8]]}), _request(env.pool), x_admin_secret=SECRET)
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:          # a point destination is not a line
        await admin_places.put_manual_shape(env.d["hx"], req, _request(env.pool), x_admin_secret=SECRET)
    assert e.value.status_code == 409
    assert (await admin_places.delete_manual_shape(env.d["mekong"], _request(env.pool),
                                                   x_admin_secret=SECRET))["status"] == "pending"
