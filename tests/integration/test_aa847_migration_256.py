"""AA-847 — migration 256 (catalog views expose line places; owner decision S246, D1) on top of 253 / 255.

conftest skips 253 and 256 in the replay (the X1 S22 catalog recipe needs 245's owners). Each test runs in ONE
transaction on the RDS-like state of test_aa860_migration_245 (module fixture): 253 then 256 are applied as
cis_owner (DB Migrate's live-owner switch), the checks run, and the transaction is rolled back — nothing to restore
(appended view columns cannot be dropped by CREATE OR REPLACE, so a committed apply could not be undone here).

Checks: columns only appended (in the agreed order); same rows; v_destination's bbox never NULL (a line's shape bbox,
else its point); v_destination_shape is tripplanner's only and shows only usable line destinations; v_tour_place and
v_tour_component carry the current build's day stretch; _live_stop never shows a line; the tile filter (bbox) and
the by-id reads are planned below the security barrier (EXPLAIN in the consumer's real form, lesson S238); re-runnable.
"""
import json
import os
import re
import sys

import psycopg2.errors
import psycopg2.extensions
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only)
import test_aa860_migration_245 as t245  # noqa: E402
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
NEW_COLS = {
    "v_destination": ["shape_kind", "shape_status", "shape_source", "length_km", "min_lat", "min_lng", "max_lat",
                      "max_lng", "shape_lo"],
    "v_tour_place": ["segment", "segment_basis", "segment_from_destination_id", "segment_to_destination_id",
                     "segment_km"],
    "v_tour_component": ["precision", "segment", "segment_basis", "segment_from_destination_id",
                         "segment_to_destination_id", "segment_km"],
}
MEKONG = {"type": "LineString", "coordinates": [[100.41, 20.27], [100.75, 20.05], [101.13, 19.89], [101.7, 19.95]]}


@pytest.fixture(scope="module", autouse=True)
def migration_255():
    """test_aa712_migration_226 drops schema geo and re-applies 226 only: re-apply 255 (idempotent)."""
    import psycopg2
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True
    with open(os.path.join(MIG, "255_geo_place_shape.sql")) as f:
        c.cursor().execute(f.read())
    c.close()


def _body(name):
    """The file without its own BEGIN / COMMIT, so it runs inside the test's transaction."""
    sql = open(os.path.join(MIG, name), encoding="utf-8").read()
    assert sql.count("\nBEGIN;\n") == 1 and sql.rstrip().endswith("COMMIT;")
    return re.sub(r"\nCOMMIT;\s*$", "\n", sql.replace("\nBEGIN;\n", "\n", 1))


def _apply(c, name):
    c.execute("SET LOCAL ROLE cis_owner")          # DB Migrate's live-owner switch (XD8)
    c.execute(_body(name))
    c.execute("RESET ROLE")


def _cols(c, view):
    c.execute("""SELECT a.attname FROM pg_attribute a
                  WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
              (f"catalog.{view}",))
    return [r[0] for r in c.fetchall()]


class _Cur(psycopg2.extensions.cursor):
    """A cursor that carries the fixture's state (the pre-256 columns, the seeded ids)."""


@pytest.fixture
def cur(rds_like_245):  # noqa: F811
    conn = t245._connect()
    conn.autocommit = False
    c = conn.cursor(cursor_factory=_Cur)
    try:
        _apply(c, "253_catalog_l4_read_model.sql")
        before = {v: _cols(c, v) for v in NEW_COLS}
        seeded = _seed(c)
        _apply(c, "256_catalog_line_places.sql")
        c.before, c.seeded = before, seeded
        yield c
    finally:
        conn.rollback()
        conn.close()


def _seed(c):
    import uuid
    tag = uuid.uuid4().hex[:8]
    tour, src = t228._tour(c, name=f"Slow boat {tag}", country="Laos")
    hx = t228._dest(c, f"Huay Xai {tag}", country="Laos", lat=20.27, lng=100.41)
    pb = t228._dest(c, f"Pak Beng {tag}", country="Laos", lat=19.89, lng=101.13)
    mek = t228._dest(c, f"Mekong River {tag}", country="Laos", lat=20.0, lng=100.8, precision="line")
    pend = t228._dest(c, f"Nam Ou River {tag}", country="Laos", lat=20.57, lng=102.61, precision="line")
    c.execute("""INSERT INTO geo.place_shape (destination_id, kind, status, source, geom, geom_mid, geom_lo, min_lat,
                                              min_lng, max_lat, max_lng, length_km, osm_type, osm_id)
                 VALUES (%s, 'line', 'ok', 'overpass', %s, %s, %s, 19.89, 100.41, 20.27, 101.7, 150.0,
                         'relation', 2091005)""", (mek, json.dumps(MEKONG), json.dumps(MEKONG), json.dumps(MEKONG)))
    c.execute("""INSERT INTO geo.place_shape (destination_id, min_lat, min_lng, max_lat, max_lng)
                 VALUES (%s, 20.57, 102.61, 20.57, 102.61)""", (pend,))
    t228._atom(c, tour, day=1, place=f"Huay Xai {tag}")
    t228._atom(c, tour, day=2, place=f"Mekong River {tag}")
    t228._component(c, src, tour, day=1, name=f"Huay Xai {tag}", destination_id=hx)
    t228._component(c, src, tour, day=2, name=f"Mekong River {tag}", destination_id=mek)
    t228._day(c, src, tour, day=1, overnight=f"Huay Xai {tag}", overnight_destination_id=hx)
    t228._day(c, src, tour, day=2, overnight=f"Pak Beng {tag}", overnight_destination_id=pb)
    build = t228._build(c, current=True)
    t228._stop(c, build, src, 1, hx)
    t228._stop(c, build, src, 2, pb)
    t228._stop(c, build, src, 3, mek)                    # a line stop written by older code: never shown
    c.execute("""INSERT INTO geo.tour_segment (build_id, source_id, day_index, destination_id, segment, basis,
                                               from_destination_id, to_destination_id, segment_km)
                 VALUES (%s, %s, 2, %s, %s, 'day_anchors', %s, %s, 87.6)""",
              (build, src, mek, json.dumps({"type": "LineString", "coordinates": MEKONG["coordinates"][:3]}), hx, pb))
    return {"tag": tag, "src": src, "tour": tour, "hx": hx, "pb": pb, "mek": mek, "pend": pend}


def test_columns_are_only_appended(cur):
    for view, new in NEW_COLS.items():
        after = _cols(cur, view)
        assert after[:len(cur.before[view])] == cur.before[view], view
        assert after[len(cur.before[view]):] == new, view
    assert _cols(cur, "v_destination_shape") == ["destination_id", "shape_status", "shape_mid", "shape_lo"]


def test_registered_and_rerunnable(cur):
    _apply(cur, "256_catalog_line_places.sql")
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '256'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'catalog.v_destination_shape'::regclass")
    assert cur.fetchone()[0] == "catalog_owner"
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_destination_bbox_and_shape_columns(cur):
    s = cur.seeded
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("""SELECT destination_id::text, precision, shape_kind, shape_status, length_km, min_lat, min_lng,
                          max_lat, max_lng, shape_lo IS NOT NULL
                     FROM catalog.v_destination WHERE destination_id = ANY(%s::uuid[])""",
                ([s["hx"], s["mek"], s["pend"]],))
    rows = {r[0]: r[1:] for r in cur.fetchall()}
    assert rows[s["hx"]] == ("point", None, None, None, 20.27, 100.41, 20.27, 100.41, False)     # the point itself
    assert rows[s["mek"]] == ("line", "line", "ok", 150.0, 19.89, 100.41, 20.27, 101.7, True)
    assert rows[s["pend"]][2:4] == ("pending", None) and rows[s["pend"]][4] == 20.57              # pending: its point
    cur.execute("SELECT count(*) FROM catalog.v_destination WHERE min_lat IS NULL OR max_lng IS NULL")
    assert cur.fetchone()[0] == 0
    cur.execute("RESET ROLE")


def test_shape_view_is_tripplanners_only(cur):
    s = cur.seeded
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("SELECT destination_id::text, shape_status, shape_mid->>'type' FROM catalog.v_destination_shape "
                "WHERE destination_id = ANY(%s::uuid[]) ORDER BY 2", ([s["mek"], s["pend"], s["hx"]],))
    assert cur.fetchall() == [(s["mek"], "ok", "LineString"), (s["pend"], "pending", None)]
    cur.execute("RESET ROLE")
    for role, ok in (("tripplanner", True), ("booking", False), ("aa_app_user", False), ("public", False)):
        if role == "public":
            cur.execute("SELECT has_table_privilege('public', 'catalog.v_destination_shape', 'SELECT')")
        else:
            cur.execute("SELECT has_table_privilege(%s, 'catalog.v_destination_shape', 'SELECT')", (role,))
        assert cur.fetchone()[0] is ok, role
    cur.execute("SAVEPOINT s")
    cur.execute("SET LOCAL ROLE booking")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("SELECT 1 FROM catalog.v_destination_shape LIMIT 1")
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_day_stretch_on_place_and_component_views(cur):
    s = cur.seeded
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("""SELECT day_number, precision, segment_basis, segment_from_destination_id::text,
                          segment_to_destination_id::text, segment_km, segment->>'type'
                     FROM catalog.v_tour_place WHERE source_id = %s ORDER BY day_number""", (s["src"],))
    rows = cur.fetchall()
    assert rows[0][:3] == (1, "point", None)
    assert rows[1] == (2, "line", "day_anchors", s["hx"], s["pb"], 87.6, "LineString")
    cur.execute("""SELECT day_index, precision, segment_basis, segment_km FROM catalog.v_tour_component
                    WHERE source_id = %s ORDER BY day_index""", (s["src"],))
    assert cur.fetchall() == [(1, "point", None, None), (2, "line", "day_anchors", 87.6)]
    cur.execute("RESET ROLE")


def test_a_line_is_never_a_stop(cur):
    s = cur.seeded
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("SELECT day_index FROM catalog.v_tour_stop WHERE source_id = %s ORDER BY 1", (s["src"],))
    assert [r[0] for r in cur.fetchall()] == [1, 2]
    cur.execute("SELECT count(*) FROM catalog.v_graph_node WHERE destination_id = %s", (s["mek"],))
    assert cur.fetchone()[0] == 0
    cur.execute("RESET ROLE")


def _plan(cur, sql, *args):
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("EXPLAIN (COSTS OFF) " + sql, args)
    out = "\n".join(r[0] for r in cur.fetchall())
    cur.execute("RESET ROLE")
    return out


def _barrier_filter(plan: str) -> bool:
    """True when the security-barrier subquery has its OWN Filter (the qual was not pushed into it)."""
    lines = plan.splitlines()
    for i, ln in enumerate(lines):
        if "Subquery Scan" in ln and i + 1 < len(lines) and lines[i + 1].strip().startswith("Filter:"):
            return True
    return False


def test_tile_bbox_filter_is_planned_below_the_barrier(cur):
    """The TripPlanner tile read (bbox intersection, plain float8 comparisons): the filter reaches the scan of
    shared.destinations ⋈ geo.place_shape inside the barrier (leakproof), and the photo LATERAL is not planned at
    all for a read that does not select a photo column."""
    plan = _plan(cur, """
        SELECT d.destination_id, d.precision, d.shape_kind, d.length_km, d.min_lat, d.min_lng, d.max_lat, d.max_lng
          FROM catalog.v_destination d
         WHERE d.max_lat >= %s AND d.min_lat < %s AND d.max_lng >= %s AND d.min_lng < %s""", 19, 21, 100, 102)
    assert not _barrier_filter(plan), plan
    assert "COALESCE(s.max_lat, " in plan, plan                 # pushed into the join below the barrier
    assert "place_photo" not in plan, plan                      # no per-destination photo LATERAL


def test_by_id_reads_are_planned_by_id(cur):
    plan = _plan(cur, "SELECT shape_mid FROM catalog.v_destination_shape WHERE destination_id = ANY(%s::uuid[])",
                 [cur.seeded["mek"]])
    assert not _barrier_filter(plan), plan
    assert re.search(r"Index Cond: \(destination_id = ANY", plan), plan
    # TripPlanner reads v_tour_place by tour_id (the window's partition key: the qual is pushable, 253's shape)
    one = _plan(cur, "SELECT segment, segment_basis FROM catalog.v_tour_place WHERE tour_id = %s::uuid",
                cur.seeded["tour"])
    assert not _barrier_filter(one), one
