"""AA-847 — migration 253 (catalog L4 read model) applied the way DB Migrate applies a cis/ file since X1 S22.

253 is the first real catalog.* view change under the X1 S22 catalog recipe (GRANT CREATE ON SCHEMA catalog TO
catalog_owner; SET LOCAL ROLE catalog_owner; …; SET LOCAL ROLE cis_owner; REVOKE CREATE). conftest skips it in the
replay (its cis_owner steps need 245's grants, and conftest swallows migration errors). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships), applies 253
as db_migrator under the live-owner switch (errors NOT swallowed), twice, and checks:
  * it really applied: catalog._live_tour / _live_stop exist, every catalog view is catalog_owner's, no CREATE left
    on schema catalog, nothing owned by db_migrator, registered once;
  * no catalog view reads catalog.v_tour any more (the point of the file);
  * contract columns unchanged (names and types, in order) and rows identical: on seeded live / trashed /
    superseded tours, suspect and duplicate destinations, photos, atoms and a current + an old graph build, every
    view's row count and md5 of its ordered rows read as tripplanner are the same under 253 and under the restored
    225 / 228 / 235 definitions (same transaction, rolled back);
  * consumers read the views, not the internal ones; v_component_embedding (pgvector only) is tripplanner's only.
Teardown restores the 225 / 228 / 235 definitions, so later modules see the replay state.
"""
import os
import re
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only; its module fixture is not imported)
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa860_migration_246 as t246  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
MIG = os.path.join(HERE, "..", "..", "api", "migrations")
MIGRATION = os.path.join(MIG, "253_catalog_l4_read_model.sql")
CONTRACT_VIEWS = ("v_tour", "v_tour_day", "v_tour_place", "v_tour_photo", "v_destination", "v_destination_photo",
                  "v_tour_component", "v_tour_day_route", "v_tour_stop", "v_tour_leg", "v_graph_node",
                  "v_graph_edge", "v_tour_junction")
INTERNAL_VIEWS = ("_tour_day_text", "_live_tour", "_live_stop")
CONSUMERS = ("aa_app_user", "tripplanner", "booking")


def _read(name):
    return open(os.path.join(MIG, name), encoding="utf-8").read()


def _view_stmt(text, view):
    m = re.search(rf"CREATE OR REPLACE VIEW catalog\.{view} WITH \(security_barrier = true\) AS\n.*?;\n", text, re.S)
    assert m, f"{view} statement not found"
    return m.group(0)


def pre_253_statements():
    """The definitions 253 replaces, from the files that last set them (225, 228, 235), in dependency order."""
    s225, s228, s235 = (_read(f) for f in ("225_catalog_views.sql", "228_catalog_place_graph_views.sql",
                                           "235_catalog_v_tour_place_lateral_day_text.sql"))
    do_component = re.search(r"DO \$do\$.*?END \$do\$;", s228, re.S).group(0)
    return ([_view_stmt(s225, v) for v in ("v_tour", "_tour_day_text", "v_tour_photo")]
            + [_view_stmt(s235, "v_tour_place"), do_component]
            + [_view_stmt(s228, v) for v in ("v_tour_day_route", "v_tour_stop", "v_tour_leg", "v_graph_node",
                                             "v_graph_edge", "v_tour_junction", "v_destination")])


def restore_pre_253(c):
    """As the test superuser: the 225 / 228 / 235 definitions back (owners and grants are kept by CREATE OR
    REPLACE), the 253-only views dropped."""
    for stmt in pre_253_statements():
        c.execute(stmt)
    c.execute("DROP VIEW IF EXISTS catalog.v_component_embedding, catalog._live_stop, catalog._live_tour")


def _apply():
    c = t246._migrator()
    c.autocommit = True
    c.notices = []
    try:
        c.cursor().execute(t246._switched(open(MIGRATION, encoding="utf-8").read()))   # raises on any error
        return list(c.notices)
    finally:
        c.close()


@pytest.fixture(scope="module", autouse=True)
def applied_253(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        notices = _apply()
        notices += _apply()                                  # idempotent re-run
        yield {"notices": notices}
    finally:
        restore_pre_253(a)
        a.execute("DELETE FROM shared.schema_versions WHERE version = '253'")
        admin.close()


@pytest.fixture
def cur():
    c = t245._connect()
    c.autocommit = False
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


def _has_vector(c):
    c.execute("SELECT 1 FROM pg_attribute WHERE attrelid = 'geo.tour_place'::regclass AND attname = 'embedding' "
              "AND NOT attisdropped")
    return c.fetchone() is not None


def _cols(c, view):
    c.execute("""SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
                  WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                  ORDER BY a.attnum""", (f"catalog.{view}",))
    return c.fetchall()


def _fingerprint(c, view):
    c.execute("SET LOCAL ROLE tripplanner")
    c.execute(f"SELECT count(*), md5(coalesce(string_agg(v::text, '|' ORDER BY v::text), '')) FROM catalog.{view} v")
    out = c.fetchone()
    c.execute("RESET ROLE")
    return out


# ── applied the DB Migrate way ──────────────────────────────────────────────────────────────────────────────────

def test_registered_once_and_internal_views_exist(cur, applied_253):
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '253'")
    assert cur.fetchone()[0] == 1
    for v in INTERNAL_VIEWS:
        cur.execute("SELECT to_regclass(%s)", (f"catalog.{v}",))
        assert cur.fetchone()[0], f"catalog.{v} missing: 253 did not apply"
    assert any("migration 253: as catalog_owner live tours" in n for n in applied_253["notices"])


def test_every_catalog_view_is_catalog_owners_and_create_is_revoked(cur):
    cur.execute("""SELECT c.relname, pg_get_userbyid(c.relowner), coalesce(c.reloptions, '{}')
                     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'catalog' AND c.relkind = 'v'""")
    rows = {r[0]: r[1:] for r in cur.fetchall()}
    assert set(CONTRACT_VIEWS) | set(INTERNAL_VIEWS) <= set(rows)
    for name, (owner, _opts) in rows.items():
        assert owner == "catalog_owner", name
    for v in CONTRACT_VIEWS + ("_tour_day_text",):
        assert "security_barrier=true" in rows[v][1], v
    for v in ("_live_tour", "_live_stop"):                   # flattenable on purpose
        assert "security_barrier=true" not in rows[v][1], v
    cur.execute("SELECT pg_get_userbyid(nspowner), has_schema_privilege('catalog_owner', 'catalog', 'CREATE') "
                "FROM pg_namespace WHERE nspname = 'catalog'")
    assert cur.fetchone() == ("cis_owner", False)
    cur.execute("SELECT count(*) FROM pg_class WHERE relowner = 'db_migrator'::regrole")
    assert cur.fetchone()[0] == 0


def test_no_catalog_view_reads_v_tour_any_more(cur):
    cur.execute("""SELECT DISTINCT dep.relname
                     FROM pg_depend d
                     JOIN pg_rewrite r ON r.oid = d.objid
                     JOIN pg_class dep ON dep.oid = r.ev_class
                    WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = 'catalog.v_tour'::regclass
                      AND dep.oid <> 'catalog.v_tour'::regclass
                      AND dep.relname NOT LIKE 'zz%'""")    # test views of other modules (zz246_v_recipe)
    assert cur.fetchall() == []
    cur.execute("""SELECT DISTINCT dep.relname
                     FROM pg_depend d
                     JOIN pg_rewrite r ON r.oid = d.objid
                     JOIN pg_class dep ON dep.oid = r.ev_class
                    WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = 'catalog._live_stop'::regclass
                      AND dep.oid <> 'catalog._live_stop'::regclass""")
    assert sorted(r[0] for r in cur.fetchall()) == ["v_graph_node", "v_tour_junction", "v_tour_stop"]


def test_consumers_read_the_contract_views_not_the_internal_ones(cur):
    for role in CONSUMERS:
        for v in CONTRACT_VIEWS:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"catalog.{v}"))
            assert cur.fetchone()[0], (role, v)
        for v in INTERNAL_VIEWS:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"catalog.{v}"))
            assert not cur.fetchone()[0], (role, v)
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("SELECT count(*) FROM catalog.v_tour_component")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("SELECT 1 FROM catalog._live_tour LIMIT 1")


def test_component_embedding_view_is_tripplanner_only(cur, applied_253):
    if not _has_vector(cur):
        cur.execute("SELECT to_regclass('catalog.v_component_embedding')")
        assert cur.fetchone()[0] is None
        assert any("v_component_embedding not created" in n for n in applied_253["notices"])
        assert all(c != "embedding" for c, _ in _cols(cur, "v_tour_component"))
        return
    for role, allowed in (("tripplanner", True), ("booking", False), ("aa_app_user", False)):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_component_embedding', 'SELECT')", (role,))
        assert cur.fetchone()[0] is allowed, role
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("SET LOCAL enable_seqscan = off")
    cur.execute("EXPLAIN SELECT component_id FROM catalog.v_component_embedding "
                "ORDER BY embedding <=> (SELECT embedding FROM catalog.v_component_embedding LIMIT 1) LIMIT 50")
    assert "tour_place_embedding_hnsw" in "\n".join(r[0] for r in cur.fetchall())


# ── same columns, same rows ─────────────────────────────────────────────────────────────────────────────────────

def _seed(c):
    """Live, trashed and superseded tours with atoms, photos, components, days and a current + an old build."""
    tag = uuid.uuid4().hex[:8]
    a_tour, a_src = t228._tour(c, name=f"A {tag}")
    b_tour, b_src = t228._tour(c, name=f"B {tag}")
    t_tour, t_src = t228._tour(c, name=f"Trashed {tag}", master_status="trashed")
    old, s_src = t228._tour(c, name=f"Old {tag}")
    c.execute("UPDATE silver_aa_internal.raw_tours SET source_status = 'superseded' WHERE tour_id = %s", (old,))
    new, _ = t228._tour(c, name=f"New {tag}", group=s_src)
    paro = t228._dest(c, f"Paro {tag}")
    thimphu = t228._dest(c, f"Thimphu {tag}", located_by="llm", precision="area")
    punakha = t228._dest(c, f"Punakha {tag}")
    bad = t228._dest(c, f"Outlier {tag}", geo_status="suspect")
    dup = t228._dest(c, f"Dup {tag}", duplicate_of=paro, geo_status="suspect")
    for i, (tour, dest) in enumerate(((a_tour, paro), (a_tour, paro), (b_tour, thimphu), (None, punakha))):
        c.execute("""INSERT INTO shared.place_photo (id, drive_file_id, drive_root_id, folder_path, country, file_name,
                                                     tour_id, destination_id, match_source, status, s3_key_large)
                     VALUES (gen_random_uuid(), %s, 'root', 'Bhutan/x', 'Bhutan', %s, %s, %s, %s, 'matched', 'k')""",
                  (f"zz847-{tag}-{i}", f"{i}.jpg", tour, dest, "manual" if i == 1 else "auto"))
    for tour, src in ((a_tour, a_src), (b_tour, b_src), (t_tour, t_src), (old, s_src)):
        t228._atom(c, tour, day=1, place=f"Paro {tag}")
        t228._atom(c, tour, day=2, place=f"Thimphu {tag}")
        t228._component(c, src, tour, day=1, name=f"Paro {tag}", destination_id=paro)
        t228._component(c, src, tour, day=2, name=f"Thimphu {tag}", destination_id=thimphu)
        t228._component(c, src, tour, day=2, name=f"Outlier {tag}", destination_id=bad)
        t228._component(c, src, tour, day=3, name=f"Dup {tag}", destination_id=dup)
        t228._day(c, src, tour, day=1, overnight=f"Paro {tag}", overnight_destination_id=paro)
        t228._day(c, src, tour, day=2, overnight=f"Outlier {tag}", overnight_destination_id=bad)
    old_build = t228._build(c, current=True)
    t228._stop(c, old_build, a_src, 1, punakha)
    build = t228._build(c, current=True)
    for src, days in ((a_src, (paro, thimphu)), (b_src, (paro, thimphu, bad)), (t_src, (thimphu, punakha)),
                      (s_src, (punakha, paro))):
        for i, dest in enumerate(days, start=1):
            t228._stop(c, build, src, i, dest)
    for dest, sources in ((paro, [a_src, b_src, s_src]), (thimphu, [a_src, b_src, t_src]), (punakha, [t_src, s_src]),
                          (bad, [b_src])):
        t228._node(c, build, dest, sources)
    for frm, to, sources in ((paro, thimphu, [a_src, b_src, t_src]), (thimphu, punakha, [t_src]),
                             (thimphu, bad, [b_src]), (punakha, paro, [s_src])):
        c.execute("""INSERT INTO geo.graph_edge (build_id, from_destination_id, to_destination_id, tour_count,
                                                 source_ids, km) VALUES (%s, %s, %s, %s, %s, 54.3)""",
                  (build, frm, to, len(sources), sources))
    for src, dests in ((a_src, [paro, thimphu]), (b_src, [paro, thimphu, bad]), (t_src, [thimphu, punakha]),
                       (s_src, [punakha, paro])):
        c.execute("""INSERT INTO geo.tour_leg (build_id, source_id, day_from, day_to, days, start_destination_id,
                                               end_destination_id, destination_ids, countries, activities)
                     VALUES (%s, %s, 1, %s, %s, %s, %s, %s::uuid[], ARRAY['Bhutan'], ARRAY['trekking'])""",
                  (build, src, len(dests), len(dests), dests[0], dests[-1], dests))
    for x, y in ((paro, thimphu), (thimphu, paro), (thimphu, punakha), (paro, bad), (punakha, paro)):
        c.execute("INSERT INTO geo.tour_junction (build_id, destination_a, destination_b, km) "
                  "VALUES (%s, %s, %s, 54.3)", (build, x, y))
    return {"live": [a_src, b_src, s_src], "trashed": t_src, "tag": tag}


def test_rows_and_columns_equal_the_pre_253_definitions(cur):
    seeded = _seed(cur)
    after = {v: (_cols(cur, v), _fingerprint(cur, v)) for v in CONTRACT_VIEWS}
    restore_pre_253(cur)
    before = {v: (_cols(cur, v), _fingerprint(cur, v)) for v in CONTRACT_VIEWS}
    for v in CONTRACT_VIEWS:
        assert after[v][0] == before[v][0], f"{v}: columns changed"
        assert after[v][1] == before[v][1], f"{v}: rows changed (count, md5) {after[v][1]} != {before[v][1]}"
    # the fixture is not vacuous: every graph view has seeded rows
    for v in ("v_tour_component", "v_tour_stop", "v_tour_leg", "v_graph_node", "v_graph_edge", "v_tour_junction",
              "v_tour_place", "v_destination"):
        assert after[v][1][0] > 0, v
    assert seeded["live"]


def test_a_trashed_tour_drops_at_once_and_suspect_places_stay_hidden(cur):
    seeded = _seed(cur)
    tag = seeded["tag"]
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("SELECT DISTINCT source_id FROM catalog.v_tour_component WHERE name LIKE %s", (f"%{tag}",))
    assert sorted(r[0] for r in cur.fetchall()) == sorted(seeded["live"])
    cur.execute("SELECT count(*) FROM catalog.v_tour_component WHERE name IN (%s, %s)",
                (f"Outlier {tag}", f"Dup {tag}"))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT 1 FROM catalog.v_tour_stop WHERE source_id = %s", (seeded["trashed"],))
    assert cur.fetchall() == []
    cur.execute("RESET ROLE")
    cur.execute("UPDATE gold_aa_internal.published_tours SET master_status = 'trashed' "
                "WHERE tour_id IN (SELECT tour_id FROM silver_aa_internal.raw_tours WHERE source_group_id::text = %s)",
                (seeded["live"][0],))
    cur.execute("SET LOCAL ROLE tripplanner")
    for v in ("v_tour_component", "v_tour_stop", "v_tour_leg", "v_tour_day_route", "v_tour_place"):
        cur.execute(f"SELECT count(*) FROM catalog.{v} WHERE source_id = %s", (seeded["live"][0],))
        assert cur.fetchone()[0] == 0, v
