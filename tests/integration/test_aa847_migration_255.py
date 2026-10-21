"""AA-847 — migration 255 (geo_precision += line, geo.place_shape, geo.tour_segment, geocode_call endpoint + credits)
against real Postgres.

The module applies 255 itself first: test_aa712_migration_226 (sorted before this file) drops schema geo and the
226 destinations columns and re-applies 226 only, so 255's objects may be gone by the time this module runs. That
also proves the file widens a narrowed CHECK again and is safe to re-run.

- shared.destinations accepts geo_precision 'line' and still refuses an unknown value;
- geo.place_shape: a pending row needs its (non-null) bbox and no geometry; a drawn row needs its three levels, its
  source and its length; status / source pairs (approximate = anchors, manual = manual); vertex and size caps;
- geo.tour_segment: 'feature' has no segment, the others have one; rows go with their graph build (cascade);
- geo.geocode_call: endpoint defaults to 'search', credits is nullable, an unknown endpoint is refused;
- privileges: cis_app DML on both tables, catalog_owner SELECT, nothing for tripplanner / booking;
- re-applying the file is a no-op.
"""
import json
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "255_geo_place_shape.sql")
TAG = "zz847-255"


def _sql() -> str:
    with open(MIGRATION) as f:
        return f.read()


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def _line(n: int, lng0: float = 102.0, lat0: float = 19.0) -> dict:
    return {"type": "LineString", "coordinates": [[round(lng0 + i * 0.001, 5), round(lat0 + i * 0.001, 5)]
                                                  for i in range(n)]}


@pytest.fixture(scope="module")
def su():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    cur.execute(_sql())
    yield cur
    cur.execute("DELETE FROM geo.graph_build WHERE params ->> 'tag' = %s", (TAG,))
    cur.execute("DELETE FROM shared.destinations WHERE name LIKE %s", (TAG + "%",))
    cur.execute("DELETE FROM geo.geocode_call WHERE provider = %s", (TAG,))
    cur.close()
    c.close()


def _dest(cur, suffix: str, precision: str = "line") -> str:
    cur.execute(
        """INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, geo_precision,
                                            name_norm, locality_norm)
           VALUES (%s, 'Laos', 19.5, 102.5, 'geoapify', 'ok', %s, %s, '') RETURNING id::text""",
        (f"{TAG} {suffix} {uuid.uuid4().hex[:6]}", precision, f"{TAG} {suffix} {uuid.uuid4().hex[:6]}"))
    return cur.fetchone()[0]


def _insert_shape(cur, dest: str, **cols) -> None:
    row = {"destination_id": dest, "min_lat": 19.5, "min_lng": 102.5, "max_lat": 19.5, "max_lng": 102.5, **cols}
    for k in ("geom", "geom_mid", "geom_lo"):
        if isinstance(row.get(k), dict):
            row[k] = json.dumps(row[k])
    names = ", ".join(row)
    cur.execute(f"INSERT INTO geo.place_shape ({names}) VALUES ({', '.join(['%s'] * len(row))})", list(row.values()))


def _refused(cur, fn, *args, **kw) -> None:
    cur.execute("SAVEPOINT s")
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            fn(cur, *args, **kw)
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT s")


def test_registered_and_line_precision(su):
    su.execute("SELECT 1 FROM shared.schema_versions WHERE version = '255'")
    assert su.fetchone()
    d = _dest(su, "river")
    su.execute("SELECT geo_precision FROM shared.destinations WHERE id = %s", (d,))
    assert su.fetchone()[0] == "line"
    su.connection.autocommit = False
    try:
        _refused(su, _dest, "curve", "curve")
    finally:
        su.connection.rollback()
        su.connection.autocommit = True


def test_place_shape_rules(su):
    su.connection.autocommit = False
    try:
        d = _dest(su, "shape")
        _insert_shape(su, d)                                  # pending: bbox = the point, no geometry
        su.execute("SELECT status, kind, tries FROM geo.place_shape WHERE destination_id = %s", (d,))
        assert su.fetchone() == ("pending", "line", 0)
        su.execute("DELETE FROM geo.place_shape WHERE destination_id = %s", (d,))
        # a drawn row without its levels / source / length
        _refused(su, _insert_shape, d, status="ok", geom=_line(5))
        # ok with everything
        _insert_shape(su, d, status="ok", source="overpass", geom=_line(2000), geom_mid=_line(300),
                      geom_lo=_line(60), length_km=12.3, osm_type="relation", osm_id=123)
        su.execute("DELETE FROM geo.place_shape WHERE destination_id = %s", (d,))
        full = dict(source="overpass", geom=_line(10), geom_mid=_line(10), geom_lo=_line(10), length_km=1)
        _refused(su, _insert_shape, d, status="ok", **{**full, "geom": _line(2001)})
        _refused(su, _insert_shape, d, status="ok", **{**full, "geom_mid": _line(301)})
        _refused(su, _insert_shape, d, status="ok", **{**full, "geom_lo": _line(61)})
        _refused(su, _insert_shape, d, status="ok", **{**full, "geom_lo": {"type": "Point", "coordinates": [1, 2]}})
        _refused(su, _insert_shape, d, status="approximate", **full)          # approximate = anchors only
        _refused(su, _insert_shape, d, status="ok", **{**full, "source": "anchors"})
        _refused(su, _insert_shape, d, status="ok", **{**full, "osm_type": "way"})   # osm pair
        _refused(su, _insert_shape, d, min_lat=20, max_lat=19)
        _insert_shape(su, d, status="approximate", **{**full, "source": "anchors"})
        su.execute("DELETE FROM geo.place_shape WHERE destination_id = %s", (d,))
        multi = {"type": "MultiLineString", "coordinates": [_line(150)["coordinates"], _line(150)["coordinates"]]}
        _insert_shape(su, d, status="manual", **{**full, "source": "manual", "geom_mid": multi})
        su.execute("SELECT geo.line_vertex_count(geom_mid) FROM geo.place_shape WHERE destination_id = %s", (d,))
        assert su.fetchone()[0] == 300
        # the destination goes, its shape goes
        su.execute("DELETE FROM shared.destinations WHERE id = %s", (d,))
        su.execute("SELECT count(*) FROM geo.place_shape WHERE destination_id = %s", (d,))
        assert su.fetchone()[0] == 0
    finally:
        su.connection.rollback()
        su.connection.autocommit = True


def test_tour_segment_rules_and_cascade(su):
    su.connection.autocommit = False
    try:
        d, a, b = _dest(su, "seg"), _dest(su, "from", "point"), _dest(su, "to", "point")
        su.execute("INSERT INTO geo.graph_build (params) VALUES (%s) RETURNING build_id::text",
                   (json.dumps({"tag": TAG}),))
        build = su.fetchone()[0]

        def seg(cur, day, basis, segment, km):
            cur.execute("""INSERT INTO geo.tour_segment (build_id, source_id, day_index, destination_id, segment,
                                                         basis, from_destination_id, to_destination_id, segment_km)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (build, TAG, day, d, json.dumps(segment) if segment else None, basis, a, b, km))

        seg(su, 1, "day_anchors", _line(20), 150.2)
        seg(su, 2, "feature", None, None)
        seg(su, 3, "anchor_window", _line(3), 30)
        _refused(su, seg, 4, "feature", _line(3), 3)
        _refused(su, seg, 4, "day_anchors", None, None)
        _refused(su, seg, 4, "day_anchors", _line(301), 3)
        _refused(su, seg, 4, "along", _line(3), 3)
        su.execute("DELETE FROM geo.graph_build WHERE build_id = %s", (build,))
        su.execute("SELECT count(*) FROM geo.tour_segment WHERE build_id = %s", (build,))
        assert su.fetchone()[0] == 0
    finally:
        su.connection.rollback()
        su.connection.autocommit = True


def test_geocode_call_columns(su):
    su.execute("INSERT INTO geo.geocode_call (provider, requests, cost_usd) VALUES (%s, 3, 0) "
               "RETURNING endpoint, credits", (TAG,))
    assert su.fetchone() == ("search", None)
    su.execute("INSERT INTO geo.geocode_call (provider, requests, cost_usd, endpoint, credits) "
               "VALUES (%s, 1, 0, 'place_details', 2) RETURNING endpoint, credits", (TAG,))
    assert su.fetchone() == ("place_details", 2)
    su.connection.autocommit = False
    try:
        def bad(cur):
            cur.execute("INSERT INTO geo.geocode_call (provider, requests, cost_usd, endpoint) "
                        "VALUES (%s, 1, 0, 'reverse')", (TAG,))
        _refused(su, bad)
    finally:
        su.connection.rollback()
        su.connection.autocommit = True


def test_privileges(su):
    su.execute("SELECT 1 FROM pg_roles WHERE rolname = 'catalog_owner'")
    catalog_owner = su.fetchone() is not None
    for t in ("geo.place_shape", "geo.tour_segment"):
        su.execute("SELECT has_table_privilege('cis_app', %s, 'SELECT, INSERT, UPDATE, DELETE')", (t,))
        assert su.fetchone()[0]
        if catalog_owner:
            su.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT')", (t,))
            assert su.fetchone()[0]
        for r in ("tripplanner", "booking"):
            su.execute("SELECT has_table_privilege(%s, %s, 'SELECT, INSERT, UPDATE, DELETE')", (r, t))
            assert not su.fetchone()[0]


def test_reapply_is_noop(su):
    su.execute("SELECT count(*) FROM pg_constraint WHERE conrelid = 'geo.place_shape'::regclass")
    before = su.fetchone()[0]
    su.execute(_sql())
    su.execute("SELECT count(*) FROM pg_constraint WHERE conrelid = 'geo.place_shape'::regclass")
    assert su.fetchone()[0] == before
    su.execute("SELECT count(*) FROM shared.spend_budget WHERE provider = 'geocoder' AND scope = 'job:place_shape'")
    assert su.fetchone()[0] == 1
