"""AA-712 — migration 228 (catalog place / graph views over schema geo) against real Postgres.

Same method as test_aa679_catalog_views (lesson S236): conftest applies every migration as a SUPERUSER,
which bypasses RLS and owns everything; Dev RDS has neither. The module fixture drops schema catalog and
catalog_owner, hands what 225 + 228 read (incl. schema geo, which aa_cis_admin owns on Dev after 226) to a
NOSUPERUSER NOBYPASSRLS CREATEROLE runner, and applies 225, then 228 twice (idempotent) as that runner —
errors NOT swallowed. The tests seed live / trashed tours with geo rows in one transaction, read the views
as the consumers do (SET ROLE tripplanner / booking) and roll back.

CI Postgres has no pgvector: geo.tour_place has no embedding column there, so v_tour_component is created
without its last column (the NOTICE path of 228) and the tests do not read embedding.
"""
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
CONSUMERS = ("tripplanner", "booking")
RUNNER = "zz728_rds_runner"   # stands in for aa_cis_admin on RDS
HERE = os.path.dirname(__file__)
MIGRATION_225 = os.path.join(HERE, "..", "..", "api", "migrations", "225_catalog_views.sql")
MIGRATION_228 = os.path.join(HERE, "..", "..", "api", "migrations", "228_catalog_place_graph_views.sql")
# 235 replaces v_tour_place (day texts through a LATERAL per tour); every v_tour_place test below runs on it
MIGRATION_235 = os.path.join(HERE, "..", "..", "api", "migrations", "235_catalog_v_tour_place_lateral_day_text.sql")
NEW_VIEWS = ("v_tour_component", "v_tour_day_route", "v_tour_stop", "v_tour_leg", "v_graph_node",
             "v_graph_edge", "v_tour_junction")
VIEWS_225 = ("v_tour", "v_tour_day", "v_tour_place", "v_tour_photo", "v_destination", "v_destination_photo")
RUNNER_SCHEMAS = ("gold_aa_internal", "silver_aa_internal", "acp_contract", "acp_shared", "shared", "geo")
RUNNER_TABLES = ("gold_aa_internal.published_tours", "silver_aa_internal.raw_tours", "acp_contract.tour_atoms",
                 "acp_shared.place_card", "acp_shared.atom_place_card", "shared.destinations",
                 "shared.place_photo", "shared.schema_versions",
                 "geo.tour_build", "geo.tour_day", "geo.tour_place", "geo.place_name", "geo.place_outlier",
                 "geo.geocode_call", "geo.graph_build", "geo.tour_stop", "geo.graph_node", "geo.graph_edge",
                 "geo.tour_leg", "geo.tour_junction")
RUNNER_VIEWS = ("shared.v_tour_photos", "shared.v_destination_photos", "acp_contract.v_active_tour_atoms")

# 225's columns of the two changed views, in order: 228 may only append after them
V_DESTINATION_225 = ["source_system", "source_id", "destination_id", "name", "country", "lat", "lng",
                     "cover_photo_path", "cover_image_url", "photo_count", "created_at", "updated_at"]
V_TOUR_PLACE_225 = ["source_system", "source_id", "tour_id", "day_number", "position", "place_name",
                    "place_card_id", "place_display_name", "locality", "region", "country", "destination_id",
                    "destination_name", "lat", "lng", "activity_types", "atom_count"]

ITINERARY = (
    "Day 1 — Arrive in Paro\nVisit the dzong.\n\n"
    "Day 2 — Paro to Thimphu\nDrive to Thimphu.\n\n"
    "Day 3 — Depart\nTransfer to the airport."
)


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


@pytest.fixture(scope="module", autouse=True)
def rds_like_228():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    a.execute("""
        DROP SCHEMA IF EXISTS catalog CASCADE;
        DROP POLICY IF EXISTS catalog_owner_aa_master_read ON gold_aa_internal.published_tours;
        DROP POLICY IF EXISTS catalog_owner_aa_master_read ON silver_aa_internal.raw_tours;
        DELETE FROM shared.schema_versions WHERE version IN ('225', '228', '235');""")
    a.execute("SELECT 1 FROM pg_roles WHERE rolname = 'catalog_owner'")
    if a.fetchone():
        a.execute("DROP OWNED BY catalog_owner")
        try:
            a.execute("DROP ROLE catalog_owner")      # 225 re-creates it, so the runner can grant it (PG16)
        except psycopg2.Error:
            pass
    a.execute(f"SELECT 1 FROM pg_roles WHERE rolname = '{RUNNER}'")
    if a.fetchone():
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER; DROP OWNED BY {RUNNER}; DROP ROLE {RUNNER}")
    a.execute(f"CREATE ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS CREATEROLE INHERIT", (DB_PASS,))
    a.execute(f"GRANT CREATE ON DATABASE {DB_NAME} TO {RUNNER}")
    for schema in RUNNER_SCHEMAS:
        a.execute(f"ALTER SCHEMA {schema} OWNER TO {RUNNER}")
    for table in RUNNER_TABLES:
        a.execute(f"ALTER TABLE {table} OWNER TO {RUNNER}")
    for view in RUNNER_VIEWS:
        a.execute(f"ALTER VIEW {view} OWNER TO {RUNNER}")
    a.execute(f"ALTER FUNCTION geo.name_norm(text) OWNER TO {RUNNER}")

    runner = _connect(RUNNER)
    runner.autocommit = True
    runner.notices = []
    try:
        c = runner.cursor()
        c.execute(open(MIGRATION_225, encoding="utf-8").read())
        sql = open(MIGRATION_228, encoding="utf-8").read()
        c.execute(sql)            # raises on any error, incl. 228's own checks
        c.execute(sql)            # idempotent re-run
        notices = list(runner.notices)
        sql_235 = open(MIGRATION_235, encoding="utf-8").read()
        c.execute(sql_235)        # raises on any error, incl. 235's own checks
        c.execute(sql_235)        # idempotent re-run
    finally:
        runner.close()
    try:
        yield {"notices": notices}
    finally:
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER; DROP OWNED BY {RUNNER}; DROP ROLE {RUNNER}")
        admin.close()


@pytest.fixture
def cur(rds_like_228):
    conn = _connect()
    conn.autocommit = False
    conn.notices = []
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


class _as:
    def __init__(self, c, role):
        self.c, self.role = c, role

    def __enter__(self):
        self.c.execute(f"SET ROLE {self.role}")
        return self.c

    def __exit__(self, *exc):
        if exc[0] is None:
            self.c.execute("RESET ROLE")
        return False


# ── seed helpers (as the test superuser, inside the test transaction) ───────────────────────────

def _tour(c, *, name, master_status="active", group=None, country="Bhutan"):
    tour_id = str(uuid.uuid4())
    group = group or str(uuid.uuid4())
    c.execute("""
        INSERT INTO silver_aa_internal.raw_tours
            (tour_id, tenant_id, batch_id, src_name, country, source_group_id, source_status)
        VALUES (%s, %s, %s, %s, %s, %s, 'active')""", (tour_id, MASTER_TENANT, BATCH_ID, name, country, group))
    c.execute("""
        INSERT INTO gold_aa_internal.published_tours
            (tour_id, generated_content_id, tenant_id, aa_name, aa_summary, aa_itineraries, master_status)
        VALUES (%s, %s, %s, %s, 'Summary', %s, %s)""",
              (tour_id, str(uuid.uuid4()), MASTER_TENANT, name, ITINERARY, master_status))
    return tour_id, group


def _dest(c, name, *, country="Bhutan", lat=27.4, lng=89.6, located_by="geoapify", precision="point",
          geo_status="ok", duplicate_of=None):
    c.execute("""
        INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, geo_precision,
                                         name_norm, duplicate_of)
        VALUES (%s, %s, %s, %s, %s, %s, %s, geo.name_norm(%s), %s) RETURNING id::text""",
              (name, country, lat, lng, located_by, geo_status, precision, name, duplicate_of))
    return c.fetchone()[0]


def _component(c, source_id, tour_id, *, day, name, destination_id, activity="cultural_heritage"):
    comp = str(uuid.uuid4())
    c.execute("""
        INSERT INTO geo.tour_place (component_id, source_id, tour_id, day_index, name, name_norm, country,
                                    country_norm, destination_id, activity, intensity_level, duration_hint,
                                    text_extract, text_hash)
        VALUES (%s, %s, %s, %s, %s, geo.name_norm(%s), 'Bhutan', 'bhutan', %s, %s, 'moderate', 'full_day',
                'text', 'h')""", (comp, source_id, tour_id, day, name, name, destination_id, activity))
    return comp


def _day(c, source_id, tour_id, *, day, overnight=None, overnight_destination_id=None):
    c.execute("""
        INSERT INTO geo.tour_day (source_id, day_index, tour_id, title, overnight_place, overnight_destination_id,
                                  places, extracted_by)
        VALUES (%s, %s, %s, %s, %s, %s, ARRAY[]::text[], 'rule')""",
              (source_id, day, tour_id, f"Day {day}", overnight, overnight_destination_id))


def _build(c, *, current):
    c.execute("INSERT INTO geo.graph_build (is_current) VALUES (false) RETURNING build_id::text")
    build = c.fetchone()[0]
    if current:
        c.execute("UPDATE geo.graph_build SET is_current = false WHERE is_current")
        c.execute("UPDATE geo.graph_build SET is_current = true WHERE build_id = %s", (build,))
    return build


def _stop(c, build, source_id, day, dest, source="overnight"):
    c.execute("INSERT INTO geo.tour_stop (build_id, source_id, day_index, destination_id, country, source) "
              "VALUES (%s, %s, %s, %s, 'Bhutan', %s)", (build, source_id, day, dest, source))


def _node(c, build, dest, sources):
    c.execute("""INSERT INTO geo.graph_node (build_id, destination_id, name, country, lat, lng, source_ids,
                                             stop_count, activities)
                 VALUES (%s, %s, 'snapshot name', 'Bhutan', 0, 0, %s, 99, ARRAY['trekking'])""",
              (build, dest, sources))


def _cols(c, view):
    c.execute("""SELECT a.attname FROM pg_attribute a
                  WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                  ORDER BY a.attnum""", (f"catalog.{view}",))
    return [r[0] for r in c.fetchall()]


# ── migration shape ─────────────────────────────────────────────────────────────────────────────

def test_migration_228_applied_the_rds_way(cur, rds_like_228):
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '228'")
    assert cur.fetchone(), "228 did not register"
    cur.execute("""SELECT c.relname, pg_get_userbyid(c.relowner), coalesce(c.reloptions, '{}')
                     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'catalog' AND c.relkind = 'v'""")
    rows = {r[0]: r[1:] for r in cur.fetchall()}
    assert set(NEW_VIEWS) | set(VIEWS_225) | {"_tour_day_text"} == set(rows)
    for name, (owner, opts) in rows.items():
        assert owner == "catalog_owner", name
        assert "security_barrier=true" in opts, name
    for role in ("aa_app_user",) + CONSUMERS:
        for v in NEW_VIEWS + VIEWS_225:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"catalog.{v}"))
            assert cur.fetchone()[0], (role, v)
        # geo stays CIS-internal: consumers read the views only
        cur.execute("SELECT has_schema_privilege(%s, 'geo', 'USAGE')", (role,))
        assert not cur.fetchone()[0], role
        cur.execute("SELECT pg_has_role(%s, 'catalog_owner', 'MEMBER')", (role,))
        assert not cur.fetchone()[0], role
    # catalog_owner reads geo, never writes; the runner keeps no membership; no CREATE left on catalog
    cur.execute("SELECT has_table_privilege('catalog_owner', 'geo.tour_place', 'SELECT'), "
                "has_table_privilege('catalog_owner', 'geo.tour_place', 'INSERT'), "
                "has_table_privilege('catalog_owner', 'geo.place_name', 'SELECT'), "
                "has_schema_privilege('catalog_owner', 'catalog', 'CREATE'), "
                "pg_has_role(%s, 'catalog_owner', 'USAGE')", (RUNNER,))
    assert cur.fetchone() == (True, False, False, False, False)
    # no pgvector in the test DB → the documented NOTICE path
    cur.execute("SELECT 1 FROM pg_type WHERE typname = 'vector'")
    if not cur.fetchone():
        assert any("embedding missing" in n for n in rds_like_228["notices"])
        assert "embedding" not in _cols(cur, "v_tour_component")


def test_225_views_only_gain_columns_at_the_end(cur):
    assert _cols(cur, "v_destination") == V_DESTINATION_225 + ["geo_status", "located_by", "precision"]
    assert _cols(cur, "v_tour_place") == V_TOUR_PLACE_225 + ["located_by", "precision"]
    # embedding (when present) is always the last component column
    cols = _cols(cur, "v_tour_component")
    assert cols[:6] == ["source_system", "source_id", "tour_id", "built_from_tour_id", "is_stale", "component_id"]
    assert cols[-1] in ("place_card_id", "embedding")


# ── components / day route ──────────────────────────────────────────────────────────────────────

def test_components_and_day_route_follow_liveness_and_usable_destinations(cur):
    tag = uuid.uuid4().hex[:8]
    live, src = _tour(cur, name=f"Live {tag}")
    trashed, trashed_src = _tour(cur, name=f"Trashed {tag}", master_status="trashed")
    paro = _dest(cur, f"Paro {tag}")
    thimphu = _dest(cur, f"Thimphu {tag}", located_by="llm", precision="area")
    bad = _dest(cur, f"Outlier {tag}", geo_status="suspect")
    keeper = _dest(cur, f"Keeper {tag}")
    dup = _dest(cur, f"Dup {tag}", duplicate_of=keeper, geo_status="suspect")

    ok = _component(cur, src, live, day=1, name=f"Paro {tag}", destination_id=paro)
    _component(cur, src, live, day=2, name=f"Thimphu {tag}", destination_id=thimphu)
    _component(cur, src, live, day=2, name=f"Pending {tag}", destination_id=None)       # unresolved
    _component(cur, src, live, day=2, name=f"Outlier {tag}", destination_id=bad)        # suspect
    _component(cur, src, live, day=2, name=f"Dup {tag}", destination_id=dup)            # duplicate
    _component(cur, trashed_src, trashed, day=1, name=f"Paro {tag}", destination_id=paro)
    _day(cur, src, live, day=1, overnight=f"Paro {tag}", overnight_destination_id=paro)
    _day(cur, src, live, day=2, overnight=f"Outlier {tag}", overnight_destination_id=bad)
    _day(cur, trashed_src, trashed, day=1, overnight=f"Paro {tag}", overnight_destination_id=paro)

    for role in CONSUMERS:
        with _as(cur, role):
            cur.execute("""SELECT source_id, tour_id::text, built_from_tour_id::text, is_stale, day_index, name,
                                  destination_id::text, activity
                             FROM catalog.v_tour_component WHERE name LIKE %s ORDER BY day_index, name""",
                        (f"%{tag}",))
            assert cur.fetchall() == [
                (src, live, live, False, 1, f"Paro {tag}", paro, "cultural_heritage"),
                (src, live, live, False, 2, f"Thimphu {tag}", thimphu, "cultural_heritage"),
            ], role
            cur.execute("""SELECT day_index, overnight_place, overnight_destination_id::text
                             FROM catalog.v_tour_day_route WHERE source_id IN (%s, %s) ORDER BY day_index""",
                        (src, trashed_src))
            # the suspect night keeps its text but shows no destination; the trashed tour is absent
            assert cur.fetchall() == [(1, f"Paro {tag}", paro), (2, f"Outlier {tag}", None)], role
    cur.execute("SELECT component_id::text FROM catalog.v_tour_component WHERE tour_id = %s AND day_index = 1",
                (live,))
    assert cur.fetchall() == [(ok,)]


def test_previous_build_keeps_serving_as_stale_after_set_active(cur):
    """set-active: the live tour_id of the source moves; geo rows built from the old version stay visible
    (is_stale) until the rebuild lands, instead of the tour dropping off the map."""
    tag = uuid.uuid4().hex[:8]
    old, group = _tour(cur, name=f"Old {tag}")
    paro = _dest(cur, f"Paro {tag}")
    _component(cur, group, old, day=1, name=f"Paro {tag}", destination_id=paro)
    _day(cur, group, old, day=1, overnight=f"Paro {tag}", overnight_destination_id=paro)
    cur.execute("UPDATE silver_aa_internal.raw_tours SET source_status = 'superseded' WHERE tour_id = %s", (old,))
    new, _ = _tour(cur, name=f"New {tag}", group=group)
    with _as(cur, "tripplanner"):
        cur.execute("SELECT tour_id::text, built_from_tour_id::text, is_stale FROM catalog.v_tour_component "
                    "WHERE source_id = %s", (group,))
        assert cur.fetchall() == [(new, old, True)]
        cur.execute("SELECT tour_id::text, is_stale FROM catalog.v_tour_day_route WHERE source_id = %s", (group,))
        assert cur.fetchall() == [(new, True)]


# ── graph views ─────────────────────────────────────────────────────────────────────────────────

def test_graph_views_read_the_current_build_and_live_tours_only(cur):
    tag = uuid.uuid4().hex[:8]
    a_tour, a_src = _tour(cur, name=f"A {tag}")
    b_tour, b_src = _tour(cur, name=f"B {tag}")
    t_tour, t_src = _tour(cur, name=f"Trashed {tag}", master_status="trashed")
    paro = _dest(cur, f"Paro {tag}")
    thimphu = _dest(cur, f"Thimphu {tag}", located_by="llm", precision="area")
    punakha = _dest(cur, f"Punakha {tag}")
    bumthang = _dest(cur, f"Bumthang {tag}")
    bad = _dest(cur, f"Outlier {tag}", geo_status="suspect")

    old = _build(cur, current=True)
    _stop(cur, old, a_src, 1, bumthang)                       # only in the old build
    cur_build = _build(cur, current=True)
    for src, days in ((a_src, (paro, thimphu)), (b_src, (paro, thimphu)), (t_src, (thimphu, punakha))):
        for i, dest in enumerate(days, start=1):
            _stop(cur, cur_build, src, i, dest)
    _stop(cur, cur_build, b_src, 3, bad)                      # a suspect stop: dropped
    for dest, sources in ((paro, [a_src, b_src]), (thimphu, [a_src, b_src, t_src]), (punakha, [t_src]),
                          (bad, [b_src])):
        _node(cur, cur_build, dest, sources)
    for frm, to, sources in ((paro, thimphu, [a_src, b_src, t_src]), (thimphu, punakha, [t_src]),
                             (thimphu, bad, [b_src])):
        cur.execute("""INSERT INTO geo.graph_edge (build_id, from_destination_id, to_destination_id, tour_count,
                                                   source_ids, km)
                       VALUES (%s, %s, %s, %s, %s, 54.3)""", (cur_build, frm, to, len(sources), sources))
    for src, dests in ((a_src, [paro, thimphu]), (b_src, [paro, thimphu, bad]), (t_src, [thimphu, punakha])):
        cur.execute("""INSERT INTO geo.tour_leg (build_id, source_id, day_from, day_to, days, start_destination_id,
                                                 end_destination_id, destination_ids, countries, activities)
                       VALUES (%s, %s, 1, %s, %s, %s, %s, %s::uuid[], ARRAY['Bhutan'], ARRAY['trekking'])""",
                    (cur_build, src, len(dests), len(dests), dests[0], dests[-1], dests))
    for x, y in ((paro, thimphu), (thimphu, paro), (thimphu, punakha), (paro, bad)):
        cur.execute("INSERT INTO geo.tour_junction (build_id, destination_a, destination_b, km) "
                    "VALUES (%s, %s, %s, 54.3)", (cur_build, x, y))

    live_sources = sorted([a_src, b_src])
    tours_by_source = {a_src: a_tour, b_src: b_tour}
    for role in CONSUMERS:
        with _as(cur, role):
            cur.execute("SELECT source_id, day_index, destination_id::text FROM catalog.v_tour_stop "
                        "WHERE source_id IN (%s, %s, %s) ORDER BY source_id, day_index", (a_src, b_src, t_src))
            assert cur.fetchall() == sorted([(a_src, 1, paro), (a_src, 2, thimphu),
                                             (b_src, 1, paro), (b_src, 2, thimphu)]), role
            cur.execute("""SELECT destination_id::text, name, lat, located_by, precision, source_ids, tour_ids::text[],
                                  stop_count, activities
                             FROM catalog.v_graph_node WHERE destination_id IN (%s, %s, %s, %s)
                            ORDER BY name""", (paro, thimphu, punakha, bad))
            nodes = cur.fetchall()
            # punakha: only the trashed tour stops there; bad: suspect
            assert [n[0] for n in nodes] == [paro, thimphu], role
            assert nodes[0][1:5] == (f"Paro {tag}", 27.4, "geoapify", "point")      # current values, not snapshot
            assert nodes[1][3:5] == ("llm", "area")
            for n in nodes:
                assert n[5] == live_sources and n[6] == [tours_by_source[s] for s in live_sources]
                assert n[7] == 2 and n[8] == ["trekking"]
            cur.execute("""SELECT from_destination_id::text, to_destination_id::text, tour_count, source_ids, km
                             FROM catalog.v_graph_edge
                            WHERE from_destination_id IN (%s, %s, %s)""", (paro, thimphu, bad))
            assert cur.fetchall() == [(paro, thimphu, 2, live_sources, 54.3)], role
            cur.execute("SELECT source_id, destination_ids::text[] FROM catalog.v_tour_leg "
                        "WHERE source_id IN (%s, %s, %s)", (a_src, b_src, t_src))
            assert cur.fetchall() == [(a_src, [paro, thimphu])], role   # b passes a suspect place, t trashed
            cur.execute("SELECT destination_a::text, destination_b::text, km FROM catalog.v_tour_junction "
                        "WHERE destination_a IN (%s, %s) ORDER BY 1, 2", (paro, thimphu))
            assert cur.fetchall() == sorted([(paro, thimphu, 54.3), (thimphu, paro, 54.3)]), role


def test_graph_views_are_empty_without_a_current_build(cur):
    tag = uuid.uuid4().hex[:8]
    _, src = _tour(cur, name=f"No build {tag}")
    dest = _dest(cur, f"Paro {tag}")
    cur.execute("UPDATE geo.graph_build SET is_current = false WHERE is_current")
    build = _build(cur, current=False)
    _stop(cur, build, src, 1, dest)
    with _as(cur, "booking"):
        cur.execute("SELECT count(*) FROM catalog.v_tour_stop WHERE source_id = %s", (src,))
        assert cur.fetchone()[0] == 0


# ── v_tour_place / v_destination ────────────────────────────────────────────────────────────────

def _atom(c, tour_id, *, day, place, activity="sightseeing"):
    atom_id = f"zz728-{uuid.uuid4()}"
    c.execute("""INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, itinerary_day, place, activity_type)
                 VALUES (%s, %s, 'atom text', %s, %s, %s)""", (atom_id, tour_id, day, place, activity))
    return atom_id


def test_v_tour_place_resolves_by_pipeline_link_then_card_then_name(cur):
    tag = uuid.uuid4().hex[:8]
    t, src = _tour(cur, name=f"Places {tag}")
    paro_geo = _dest(cur, f"Paro Dzong area {tag}")                         # what the pipeline linked
    paro_name = _dest(cur, f"Paro {tag}", located_by="mapbox", precision=None)   # 225 name match
    thimphu_card = _dest(cur, f"Thimphu city {tag}", located_by="manual")
    legacy = _dest(cur, f"Dochula {tag}", located_by=None, precision=None)
    suspect = _dest(cur, f"Haa {tag}", geo_status="suspect")
    _atom(cur, t, day=1, place=f"Paro {tag}")
    a2 = _atom(cur, t, day=2, place=f"Thimphu {tag}")
    _atom(cur, t, day=2, place=f"Dochula {tag}")
    _atom(cur, t, day=3, place=f"Haa {tag}")
    # the pipeline's component of day 1 "Paro" (another spelling of the same name_norm key) → paro_geo
    _component(cur, src, t, day=1, name=f"PARO {tag}", destination_id=paro_geo)
    # a component on another day with the same name does not leak into day 1/2
    _component(cur, src, t, day=3, name=f"Thimphu {tag}", destination_id=paro_geo)
    card = str(uuid.uuid4())
    cur.execute("""INSERT INTO acp_shared.place_card (card_id, place_key, display_name, country, destination_id)
                   VALUES (%s, %s, %s, 'Bhutan', %s)""", (card, f"thimphu-{tag}", f"Thimphu {tag}", thimphu_card))
    cur.execute("INSERT INTO acp_shared.atom_place_card (atom_id, card_id) VALUES (%s, %s)", (a2, card))

    for role in CONSUMERS:
        with _as(cur, role):
            cur.execute("""SELECT day_number, place_name, destination_id::text, destination_name, located_by,
                                  precision
                             FROM catalog.v_tour_place WHERE tour_id = %s ORDER BY day_number, position""", (t,))
            assert cur.fetchall() == [
                (1, f"Paro {tag}", paro_geo, f"Paro Dzong area {tag}", "geoapify", "point"),   # 1. pipeline
                (2, f"Dochula {tag}", legacy, f"Dochula {tag}", None, None),                     # 3. name
                (2, f"Thimphu {tag}", thimphu_card, f"Thimphu city {tag}", "manual", "point"),  # 2. card
                (3, f"Haa {tag}", None, None, None, None),                                       # suspect
            ], role
    assert paro_name and suspect


def test_migration_235_keeps_owner_grants_and_reads_day_text_per_tour(cur):
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '235'")
    assert cur.fetchone(), "235 did not register"
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class "
                "WHERE oid = 'catalog.v_tour_place'::regclass")
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in opts
    cur.execute("SELECT pg_get_viewdef('catalog.v_tour_place'::regclass)")
    viewdef = cur.fetchone()[0]
    assert "jsonb_object_agg" in viewdef and "LATERAL" in viewdef
    for role in ("aa_app_user",) + CONSUMERS:
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_tour_place', 'SELECT')", (role,))
        assert cur.fetchone()[0], role
    cur.execute("SELECT pg_has_role(%s, 'catalog_owner', 'USAGE')", (RUNNER,))
    assert cur.fetchone()[0] is False


def test_v_tour_place_orders_and_spells_by_day_text_through_an_any_filter(cur):
    # ITINERARY day 2 is "Paro to Thimphu": Paro comes first, the verbatim spelling "Thimphu" wins over the
    # more frequent "thimphu", a place the text does not name comes last, a day without text keeps its row
    t, _ = _tour(cur, name=f"Order {uuid.uuid4().hex[:8]}")
    other, _ = _tour(cur, name=f"Other {uuid.uuid4().hex[:8]}")
    _atom(cur, t, day=2, place="Zhemgang")
    _atom(cur, t, day=2, place="thimphu")
    _atom(cur, t, day=2, place="thimphu")
    _atom(cur, t, day=2, place="Thimphu")
    _atom(cur, t, day=2, place="Paro")
    _atom(cur, t, day=7, place="Bumthang")         # no "Day 7" block in the text
    _atom(cur, other, day=1, place="Paro")
    with _as(cur, "tripplanner"):
        cur.execute("""SELECT tour_id::text, day_number, position, place_name, atom_count
                         FROM catalog.v_tour_place WHERE tour_id = ANY(%s::uuid[])
                        ORDER BY tour_id = %s DESC, day_number, position""", ([t, other], t))
        assert cur.fetchall() == [
            (t, 2, 1, "Paro", 1),
            (t, 2, 2, "Thimphu", 3),
            (t, 2, 3, "Zhemgang", 1),
            (t, 7, 1, "Bumthang", 1),
            (other, 1, 1, "Paro", 1),
        ]


def test_v_tour_place_name_match_follows_a_duplicate_to_its_keeper(cur):
    tag = uuid.uuid4().hex[:8]
    t, _ = _tour(cur, name=f"Dup {tag}")
    keeper = _dest(cur, f"Hoi-An {tag}")
    _dest(cur, f"Hoi An {tag}", duplicate_of=keeper, geo_status="suspect")
    _atom(cur, t, day=1, place=f"Hoi An {tag}")
    cur.execute("SELECT destination_id::text FROM catalog.v_tour_place WHERE tour_id = %s", (t,))
    assert cur.fetchall() == [(keeper,)]


def test_v_tour_place_name_match_skips_a_suspect_card_name_match(cur):
    """The card's display name matches a suspect row first (225's ORDER BY); the itinerary name matches a
    usable row. The first USABLE match wins instead of the pin going null."""
    tag = uuid.uuid4().hex[:8]
    t, _ = _tour(cur, name=f"Card {tag}")
    _dest(cur, f"Punakha Dzong {tag}", geo_status="suspect")
    usable = _dest(cur, f"Punakha {tag}")
    a = _atom(cur, t, day=1, place=f"Punakha {tag}")
    card = str(uuid.uuid4())
    cur.execute("""INSERT INTO acp_shared.place_card (card_id, place_key, display_name, country)
                   VALUES (%s, %s, %s, 'Bhutan')""", (card, f"punakha-dzong-{tag}", f"Punakha Dzong {tag}"))
    cur.execute("INSERT INTO acp_shared.atom_place_card (atom_id, card_id) VALUES (%s, %s)", (a, card))
    with _as(cur, "tripplanner"):
        cur.execute("SELECT destination_id::text FROM catalog.v_tour_place WHERE tour_id = %s", (t,))
        assert cur.fetchall() == [(usable,)]


def test_place_views_survive_revoking_public_execute_on_geo_functions(cur):
    """A view calls functions with the READER's EXECUTE right: v_tour_place must not depend on the default
    EXECUTE TO PUBLIC of geo.name_norm (hardening geo must not break every catalog place read)."""
    tag = uuid.uuid4().hex[:8]
    t, src = _tour(cur, name=f"Exec {tag}")
    dest = _dest(cur, f"Paro {tag}")
    _atom(cur, t, day=1, place=f"Paro {tag}")
    _component(cur, src, t, day=1, name=f"Paro {tag}", destination_id=dest)
    cur.execute("REVOKE EXECUTE ON FUNCTION geo.name_norm(text) FROM PUBLIC")   # rolled back with the test
    for role in ("aa_app_user",) + CONSUMERS:
        with _as(cur, role):
            cur.execute("SELECT destination_id::text FROM catalog.v_tour_place WHERE tour_id = %s", (t,))
            assert cur.fetchall() == [(dest,)], role
            cur.execute("SELECT count(*) FROM catalog.v_tour_day WHERE tour_id = %s", (t,))
            assert cur.fetchone()[0] >= 1, role


def test_v_destination_exposes_geo_quality(cur):
    tag = uuid.uuid4().hex[:8]
    d = _dest(cur, f"Bumthang {tag}", located_by="llm", precision="area", geo_status="suspect")
    with _as(cur, "tripplanner"):
        cur.execute("SELECT name, geo_status, located_by, precision FROM catalog.v_destination "
                    "WHERE destination_id = %s", (d,))
        assert cur.fetchone() == (f"Bumthang {tag}", "suspect", "llm", "area")


# ── security ────────────────────────────────────────────────────────────────────────────────────

LEAK_FN = """
    CREATE FUNCTION public.zz728_leak(text) RETURNS boolean LANGUAGE plpgsql COST 0.0000001 AS $$
    BEGIN
        RAISE NOTICE 'zz728-leak:%', $1;
        RETURN true;
    END $$"""


def test_leaky_function_probe_reveals_no_hidden_component(cur):
    tag = uuid.uuid4().hex[:8]
    live, src = _tour(cur, name=f"Live {tag}")
    trashed, trashed_src = _tour(cur, name=f"Trashed {tag}", master_status="trashed")
    dest = _dest(cur, f"Paro {tag}")
    bad = _dest(cur, f"Outlier {tag}", geo_status="suspect")
    _component(cur, src, live, day=1, name=f"Visible {tag}", destination_id=dest)
    _component(cur, src, live, day=1, name=f"Suspect {tag}", destination_id=bad)
    _component(cur, trashed_src, trashed, day=1, name=f"Hidden {tag}", destination_id=dest)
    cur.execute(LEAK_FN)
    with _as(cur, "booking"):
        cur.connection.notices.clear()
        cur.execute("SELECT name FROM catalog.v_tour_component WHERE public.zz728_leak(name)")
        cur.fetchall()
        seen = " ".join(cur.connection.notices)
    assert f"Visible {tag}" in seen
    assert f"Hidden {tag}" not in seen and f"Suspect {tag}" not in seen, seen


def test_consumers_cannot_read_geo(cur):
    for table in ("geo.tour_place", "geo.graph_node", "geo.tour_day"):
        cur.execute("SAVEPOINT direct")
        cur.execute("SET ROLE tripplanner")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(f"SELECT count(*) FROM {table}")
        cur.execute("ROLLBACK TO SAVEPOINT direct")
        cur.execute("RESET ROLE")


def test_new_views_read_on_a_pooled_connection_with_empty_tenant(rds_like_228):
    """TripPlanner's pool holds app.tenant_id = '' after an earlier set_config(..., true): every view reads."""
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (MASTER_TENANT,))
        conn.commit()
        tag = uuid.uuid4().hex[:8]
        live, src = _tour(c, name=f"Pooled {tag}")
        dest = _dest(c, f"Paro {tag}")
        _component(c, src, live, day=1, name=f"Paro {tag}", destination_id=dest)
        for role in CONSUMERS:
            with _as(c, role):
                for v in NEW_VIEWS + ("v_tour_place", "v_destination"):
                    c.execute(f"SELECT count(*) FROM catalog.{v}")
                c.execute("SELECT count(*) FROM catalog.v_tour_component WHERE source_id = %s", (src,))
                assert c.fetchone()[0] == 1, role
    finally:
        conn.rollback()
        conn.close()
