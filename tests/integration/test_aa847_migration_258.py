"""AA-847 — migration 258 (raw_tours.source_key under catalog._live_tour; v_tour_component's day stretch as a plain
left join on the current build), on top of 253 / 255 / 256 / 257.

conftest skips 253, 256, 257 and 258 in the replay (the X1 S22 catalog recipe needs 245's owners). Each test runs in ONE
transaction on the RDS-like state of test_aa860_migration_245 (module fixture): 253, 256 and 257 are applied as
cis_owner (DB Migrate's live-owner switch), the fixture is seeded, the pre-258 columns / rows / plans are taken, 258 is
applied the same way, the checks run, and the transaction is rolled back (the column add is rolled back with it).

Why 258 (step-18 gate diagnosis, S246): raw_tours has FORCE RLS, and 253's join key COALESCE(source_group_id,
tour_id)::text is not leakproof, so it can never be an index condition under the RLS quals: every per-component read of
_live_tour scanned raw_tours. The plan checks below are structural (the consumer's real SQL, read as tripplanner with
RLS on, lesson S238): seq / bitmap scans and hash / merge joins are off, so each join is a nested loop and the plan
shows whether raw_tours CAN be probed by key, which the leaky key made impossible. Natural plans at test size are free
to read the whole table (the planner's choice there depends on statistics other modules leave behind).

Checks: registered once, re-runnable, owners / grants / RLS unchanged; the column is a stored generated text that
follows its expression and cannot be written; same columns and same rows (count + md5, read as tripplanner) in every
catalog view on _live_tour or v_tour_component; destination detail's component read joins raw_tours through
raw_tours_source_key_idx; the tile read has no tour_segment node any more (it had one under 256); the top-k view's
liveness EXISTS is an index probe on raw_tours_source_key_idx, and with pgvector the HNSW order is still available.
"""
import os
import re
import sys
import uuid

import psycopg2
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
BEFORE_258 = ("253_catalog_l4_read_model.sql", "256_catalog_line_places.sql", "257_geo_place_dedupe.sql")
MIGRATION = "258_catalog_live_source_key.sql"
# every catalog view that reads _live_tour or v_tour_component (directly or through _live_stop), and v_destination
VIEWS = ("_live_tour", "_live_stop", "v_tour", "v_tour_day", "v_tour_place", "v_tour_photo", "v_tour_component",
         "v_tour_day_route", "v_tour_stop", "v_tour_leg", "v_graph_node", "v_graph_edge", "v_tour_junction",
         "v_destination", "v_component_embedding")
INTERNAL = ("_live_tour", "_live_stop")

# The TripPlanner cis readers, in their real form (AA-TripPlanner-Web backend/browse/destinations.py
# COMPONENTS_SQL_CIS, backend/browse/tiles.py cis tile SQL without the optional filters, backend/shared/vector_topk.py
# CIS scan and component_count), with psycopg2 placeholders.
DETAIL_COMPONENTS = """
    SELECT component_id AS id, name, activity, intensity_level, duration_hint, text_extract
    FROM catalog.v_tour_component
    WHERE destination_id = %s::uuid
    ORDER BY name, component_id
"""
TILES = """
    SELECT d.destination_id AS id, d.name, d.lat, d.lng, d.located_by, d.precision,
           d.shape_kind, d.shape_status, d.length_km,
           d.min_lat, d.min_lng, d.max_lat, d.max_lng,
           COUNT(c.component_id) AS component_count
    FROM catalog.v_destination d
    JOIN catalog.v_tour_component c ON c.destination_id = d.destination_id
    WHERE d.max_lat >= %s AND d.min_lat < %s
      AND d.max_lng >= %s AND d.min_lng < %s
    GROUP BY d.destination_id, d.name, d.lat, d.lng, d.located_by, d.precision,
             d.shape_kind, d.shape_status, d.length_km, d.min_lat, d.min_lng, d.max_lat, d.max_lng
    HAVING COUNT(c.component_id) > 0
    ORDER BY component_count DESC
"""
TOPK_SCAN = """
    SELECT c.id, c.destination_id, c.source_tour_id
    FROM (SELECT component_id AS id, source_id AS source_tour_id, destination_id, activity, intensity_level,
                 season_months, embedding FROM catalog.v_component_embedding) c
    WHERE c.embedding IS NOT NULL
"""
TOPK_ORDERED = TOPK_SCAN + " ORDER BY c.embedding <=> %s::vector LIMIT 50"
SEARCH_COUNT = """
    SELECT COUNT(*) FROM (SELECT component_id AS id, source_id AS source_tour_id, day_index AS source_day_index,
                                 destination_id, activity, intensity_level, season_months, embedding
                            FROM catalog.v_tour_component) c
     WHERE c.destination_id = %s::uuid AND c.embedding IS NOT NULL
"""


@pytest.fixture(scope="module", autouse=True)
def migration_255():
    """test_aa712_migration_226 drops schema geo and re-applies 226 only: re-apply 255 (idempotent)."""
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


def _has_vector(c):
    c.execute("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
              "WHERE attrelid = 'geo.tour_place'::regclass AND attname = 'embedding' AND NOT attisdropped")
    row = c.fetchone()
    return bool(row) and row[0].startswith("vector")


def _cols(c, view):
    c.execute("""SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
                  WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
              (f"catalog.{view}",))
    return c.fetchall()


def _fingerprint(c, view):
    """(count, md5 of the ordered rows) as the view owner sees them for AA master (internal views) or as tripplanner."""
    if view in INTERNAL:
        c.execute("SET LOCAL ROLE catalog_owner")
        c.execute("SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-000000000000', true)")
    else:
        c.execute("SET LOCAL ROLE tripplanner")
    c.execute(f"SELECT count(*), md5(coalesce(string_agg(v::text, '|' ORDER BY v::text), '')) FROM catalog.{view} v")
    out = c.fetchone()
    c.execute("RESET ROLE")
    c.execute("SELECT set_config('app.tenant_id', '', true)")
    return out


# A structural plan: no seq / bitmap scan and no hash / merge join, so every join is a nested loop whose inner side is
# either a parameterized index probe (possible only with a leakproof key under RLS) or a full read with a Join Filter.
STRUCTURAL = ("enable_seqscan", "enable_bitmapscan", "enable_hashjoin", "enable_mergejoin")


def _plan(c, sql, *args, structural=False, sort=True):
    c.execute("SET LOCAL ROLE tripplanner")
    off = (STRUCTURAL if structural else ()) + (() if sort else ("enable_sort",))
    for guc in off:
        c.execute(f"SET LOCAL {guc} = off")
    c.execute("EXPLAIN (COSTS OFF) " + sql, args)
    out = "\n".join(r[0] for r in c.fetchall())
    for guc in off:
        c.execute(f"RESET {guc}")
    c.execute("RESET ROLE")
    return out


def _privileges(c):
    c.execute("""SELECT polname, polcmd, pg_get_expr(polqual, polrelid), polroles::regrole[]::text
                   FROM pg_policy WHERE polrelid = 'silver_aa_internal.raw_tours'::regclass ORDER BY polname""")
    policies = c.fetchall()
    c.execute("""SELECT relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner)
                   FROM pg_class WHERE oid = 'silver_aa_internal.raw_tours'::regclass""")
    return policies, c.fetchone()


class _Cur(psycopg2.extensions.cursor):
    """A cursor that carries the fixture's state (pre-258 columns / rows / plans, the seeded ids)."""


@pytest.fixture
def cur(rds_like_245):  # noqa: F811
    conn = t245._connect()
    conn.autocommit = False
    c = conn.cursor(cursor_factory=_Cur)
    try:
        if not _has_vector(c):
            # CI (postgres:15, no pgvector): a stand-in embedding column BEFORE 253, so 253 / 256 / 258 all build the
            # same shape (v_component_embedding + the embedding column of v_tour_component); rolled back with the rest
            c.execute("ALTER TABLE geo.tour_place ADD COLUMN IF NOT EXISTS embedding float8[]")
        for name in BEFORE_258:
            _apply(c, name)
        c.seeded = _seed(c)
        c.before = {
            "cols": {v: _cols(c, v) for v in VIEWS},
            "rows": {v: _fingerprint(c, v) for v in VIEWS},
            "privileges": _privileges(c),
            "tiles": _plan(c, TILES, 19, 21, 100, 102),
        }
        _apply(c, MIGRATION)
        yield c
    finally:
        conn.rollback()
        conn.close()


def _seed(c):
    """Two live tours (one grouped, one without source_group_id: its key is its tour_id), a trashed one, usable /
    suspect / line destinations, a day stretch in the current build and one in an old build, embeddings."""
    tag = uuid.uuid4().hex[:8]
    grouped, g_src = t228._tour(c, name=f"Grouped {tag}", country="Laos")
    solo, _ = t228._tour(c, name=f"Solo {tag}", country="Laos")
    c.execute("UPDATE silver_aa_internal.raw_tours SET source_group_id = NULL WHERE tour_id = %s", (solo,))
    s_src = solo                                            # COALESCE(source_group_id, tour_id)::text
    dead, d_src = t228._tour(c, name=f"Dead {tag}", country="Laos", master_status="trashed")
    hx = t228._dest(c, f"Huay Xai {tag}", country="Laos", lat=20.27, lng=100.41)
    pb = t228._dest(c, f"Pak Beng {tag}", country="Laos", lat=19.89, lng=101.13)
    mek = t228._dest(c, f"Mekong River {tag}", country="Laos", lat=20.0, lng=100.8, precision="line")
    bad = t228._dest(c, f"Outlier {tag}", country="Laos", lat=20.1, lng=100.5, geo_status="suspect")
    for tour, src in ((grouped, g_src), (solo, s_src), (dead, d_src)):
        t228._atom(c, tour, day=1, place=f"Huay Xai {tag}")
        t228._atom(c, tour, day=2, place=f"Mekong River {tag}")
        t228._component(c, src, tour, day=1, name=f"Huay Xai {tag}", destination_id=hx)
        t228._component(c, src, tour, day=2, name=f"Mekong River {tag}", destination_id=mek)
        t228._component(c, src, tour, day=3, name=f"Outlier {tag}", destination_id=bad)
        t228._day(c, src, tour, day=1, overnight=f"Huay Xai {tag}", overnight_destination_id=hx)
        t228._day(c, src, tour, day=2, overnight=f"Pak Beng {tag}", overnight_destination_id=pb)
    old = t228._build(c, current=True)
    build = t228._build(c, current=True)
    for b, km in ((old, 11.1), (build, 87.6)):
        for src in (g_src, s_src, d_src):
            c.execute("""INSERT INTO geo.tour_segment (build_id, source_id, day_index, destination_id, segment, basis,
                                                       from_destination_id, to_destination_id, segment_km)
                         VALUES (%s, %s, 2, %s, '{"type": "LineString", "coordinates": [[100.41, 20.27],
                                 [101.13, 19.89]]}', 'day_anchors', %s, %s, %s)""", (b, src, mek, hx, pb, km))
    for src in (g_src, s_src, d_src):
        t228._stop(c, build, src, 1, hx)
        t228._stop(c, build, src, 2, pb)
    if _has_vector(c):
        c.execute("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                  "WHERE attrelid = 'geo.tour_place'::regclass AND attname = 'embedding'")
        dim = int(re.search(r"\((\d+)\)", c.fetchone()[0]).group(1))
        value, typ = "[" + ",".join(["0.1"] * dim) + "]", "vector"
    else:
        value, typ = "{0.1,0.1,0.1}", "float8[]"
    c.execute(f"UPDATE geo.tour_place SET embedding = %s::{typ} WHERE name LIKE %s", (value, f"% {tag}"))
    c.execute("ANALYZE silver_aa_internal.raw_tours")
    return {"tag": tag, "grouped": grouped, "g_src": g_src, "solo": solo, "s_src": s_src, "d_src": d_src,
            "hx": hx, "mek": mek, "bad": bad, "vector": value}


# ── applied the DB Migrate way ──────────────────────────────────────────────────────────────────────────────────

def test_registered_rerunnable_and_owned(cur):
    _apply(cur, MIGRATION)                                  # idempotent re-run
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '258'")
    assert cur.fetchone()[0] == 1
    cur.execute("""SELECT format_type(atttypid, atttypmod), attgenerated FROM pg_attribute
                    WHERE attrelid = 'silver_aa_internal.raw_tours'::regclass AND attname = 'source_key'""")
    assert cur.fetchone() == ("text", "s")
    cur.execute("SELECT pg_get_indexdef('silver_aa_internal.raw_tours_source_key_idx'::regclass)")
    assert cur.fetchone()[0].endswith("USING btree (source_key)")
    cur.execute("""SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c
                    WHERE c.oid IN ('catalog._live_tour'::regclass, 'catalog.v_tour_component'::regclass)""")
    assert {r[1] for r in cur.fetchall()} == {"catalog_owner"}
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False
    cur.execute("SELECT coalesce(reloptions, '{}') FROM pg_class WHERE oid = 'catalog.v_tour_component'::regclass")
    assert "security_barrier=true" in cur.fetchone()[0]


def test_grants_policies_and_tripplanner_column_grant_unchanged(cur):
    assert _privileges(cur) == cur.before["privileges"]
    assert cur.before["privileges"][1][:2] == (True, True)  # FORCE RLS: the plan checks below are not vacuous
    for role in ("aa_app_user", "tripplanner", "booking"):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_tour_component', 'SELECT'), "
                    "has_table_privilege(%s, 'catalog._live_tour', 'SELECT')", (role, role))
        assert cur.fetchone() == (True, False), role
    cur.execute("""SELECT has_column_privilege('tripplanner', 'silver_aa_internal.raw_tours', 'source_key', 'SELECT'),
                          has_column_privilege('tripplanner', 'silver_aa_internal.raw_tours', 'country', 'SELECT'),
                          has_column_privilege('tripplanner', 'silver_aa_internal.raw_tours', 'tour_id', 'SELECT')""")
    assert cur.fetchone() == (False, True, True)
    cur.execute("SELECT has_table_privilege('tripplanner', 'catalog.v_component_embedding', 'SELECT'), "
                "has_table_privilege('booking', 'catalog.v_component_embedding', 'SELECT')")
    assert cur.fetchone() == (True, False)


def test_source_key_follows_its_expression_and_cannot_be_written(cur):
    s = cur.seeded
    cur.execute("SELECT tour_id::text, source_key FROM silver_aa_internal.raw_tours WHERE tour_id IN (%s, %s)",
                (s["grouped"], s["solo"]))
    assert dict(cur.fetchall()) == {s["grouped"]: s["g_src"], s["solo"]: s["solo"]}
    other = str(uuid.uuid4())
    cur.execute("UPDATE silver_aa_internal.raw_tours SET source_group_id = %s WHERE tour_id = %s RETURNING source_key",
                (other, s["solo"]))
    assert cur.fetchone()[0] == other
    cur.execute("SELECT count(*) FROM silver_aa_internal.raw_tours "
                "WHERE source_key IS DISTINCT FROM (COALESCE(source_group_id, tour_id))::text")
    assert cur.fetchone()[0] == 0
    cur.execute("SAVEPOINT w")
    with pytest.raises(psycopg2.errors.GeneratedAlways):
        cur.execute("UPDATE silver_aa_internal.raw_tours SET source_key = 'x' WHERE tour_id = %s", (s["grouped"],))
    cur.execute("ROLLBACK TO SAVEPOINT w")


# ── same columns, same rows ─────────────────────────────────────────────────────────────────────────────────────

def test_columns_and_rows_unchanged(cur):
    for v in VIEWS:
        assert _cols(cur, v) == cur.before["cols"][v], f"{v}: columns changed"
        assert _fingerprint(cur, v) == cur.before["rows"][v], f"{v}: rows changed"
    # the fixture is not vacuous: the seeded tours, stretches and embeddings are in the compared rows
    s = cur.seeded
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("""SELECT source_id, day_index, segment_km FROM catalog.v_tour_component
                    WHERE source_id IN (%s, %s, %s) ORDER BY 1, 2""", (s["g_src"], s["s_src"], s["d_src"]))
    rows = cur.fetchall()
    cur.execute("SELECT count(*) FROM catalog.v_component_embedding WHERE source_id IN (%s, %s)",
                (s["g_src"], s["s_src"]))
    n_emb = cur.fetchone()[0]
    cur.execute("RESET ROLE")
    assert sorted(rows) == sorted([(s["g_src"], 1, None), (s["g_src"], 2, 87.6),       # current build's stretch only
                                   (s["s_src"], 1, None), (s["s_src"], 2, 87.6)])    # no dead tour, no suspect place
    assert n_emb == 4


def test_no_current_build_means_no_stretch_not_an_error(cur):
    s = cur.seeded
    cur.execute("UPDATE geo.graph_build SET is_current = false WHERE is_current")
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("SELECT count(*), count(segment_km) FROM catalog.v_tour_component WHERE source_id = %s",
                (s["g_src"],))
    assert cur.fetchone() == (2, 0)
    cur.execute("RESET ROLE")


# ── plans, read as tripplanner with RLS on (the consumer's real SQL) ────────────────────────────────────────────

def test_detail_components_join_raw_tours_through_the_key_index(cur):
    plan = _plan(cur, DETAIL_COMPONENTS, cur.seeded["mek"], structural=True)
    assert "raw_tours_source_key_idx" in plan, plan
    assert re.search(r"Index Cond: \(source_key = ", plan), plan
    assert "Seq Scan on raw_tours" not in plan, plan
    assert "COALESCE(" not in plan, plan


def test_search_component_count_probes_the_key_index(cur):
    plan = _plan(cur, SEARCH_COUNT, cur.seeded["hx"], structural=True)
    assert "raw_tours_source_key_idx" in plan, plan
    assert "Seq Scan on raw_tours" not in plan, plan


def test_tiles_read_has_no_segment_join_any_more(cur):
    assert "tour_segment" in cur.before["tiles"], cur.before["tiles"]       # 256's per-row LATERAL
    plan = _plan(cur, TILES, 19, 21, 100, 102)
    assert "tour_segment" not in plan, plan                  # the left join is removed (no segment column read)
    # what may remain is the current-build InitPlan: an init plan runs only when its param is read, and nothing
    # reads it once the join is gone


def test_component_embedding_liveness_is_an_index_probe(cur):
    plan = _plan(cur, TOPK_SCAN, structural=True)
    assert "raw_tours_source_key_idx" in plan, plan
    assert "Seq Scan on raw_tours" not in plan, plan
    assert not re.search(r"Join Filter: .*source_id", plan), plan
    if not _has_vector(cur):
        return
    cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
    # the step-18 structural check: with the sort and seq scans off, the order comes from the HNSW index and the
    # liveness semi-join probes raw_tours by key (before 258 only a Join Filter over a scan of raw_tours could)
    forced = _plan(cur, TOPK_ORDERED, cur.seeded["vector"], structural=True, sort=False)
    assert "tour_place_embedding_hnsw" in forced, forced
    assert re.search(r"Order By: \(embedding <=> ", forced), forced
    assert "raw_tours_source_key_idx" in forced, forced
    assert not re.search(r"Join Filter: .*source_id", forced), forced
