"""AA-679 — migration 225 (schema `catalog`, the CIS catalog read model) against real Postgres.

Lesson S231: conftest swallows migration errors, so the first test fails loudly when 225 did not
apply. The others seed live / not-live tours in one transaction, read the views as a consumer would
(SET ROLE booking / tripplanner), and roll back.

Lesson S236: conftest applies every migration as a SUPERUSER, which bypasses RLS — but Dev RDS has
no superuser and no BYPASSRLS role (the runner aa_cis_admin owns the tables, FORCE RLS on), so the
first 225 passed CI and failed on Dev. The module fixture below re-applies 225 the RDS way: as a
NOSUPERUSER NOBYPASSRLS CREATEROLE role that owns the tables it reads, with their FORCE RLS on.
"""
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import restore_266_catalog_owner_read  # noqa: E402

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-000000000099"
VIEWS = ("v_tour", "v_tour_day", "v_tour_place", "v_tour_photo", "v_destination", "v_destination_photo")
CONSUMERS = ("booking", "tripplanner")
RUNNER = "zz679_rds_runner"   # stands in for aa_cis_admin on RDS
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "225_catalog_views.sql")
# what 225 reads, owned by aa_cis_admin on Dev
RUNNER_SCHEMAS = ("gold_aa_internal", "silver_aa_internal", "acp_contract", "acp_shared", "shared")
RUNNER_TABLES = ("gold_aa_internal.published_tours", "silver_aa_internal.raw_tours", "acp_contract.tour_atoms",
                 "acp_shared.place_card", "acp_shared.atom_place_card", "shared.destinations",
                 "shared.place_photo", "shared.schema_versions")
RUNNER_VIEWS = ("shared.v_tour_photos", "shared.v_destination_photos", "acp_contract.v_active_tour_atoms")

CANONICAL = (
    "Day 1 — Arrive in Hanoi\nWelcome dinner in the Old Quarter.\n\n"
    "Day 2 — Ha Long Bay\nCruise among the karsts.\nOvernight on board.\n"
    "Day 3 — Depart\nTransfer to the airport."
)
REPEATED_DAY = "Day 1 — First\nA.\n\nDay 2 — Old title\nB.\n\nDay 2 — New title\nC."


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


@pytest.fixture(scope="module", autouse=True)
def rds_like_225():
    """Undo the superuser-applied 225, hand the objects it reads to a non-superuser, non-BYPASSRLS
    runner (as aa_cis_admin owns them on RDS) and apply 225 as that runner — errors are NOT swallowed.
    Teardown gives everything back to the test superuser and drops the runner."""
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    a.execute("""
        DROP SCHEMA IF EXISTS catalog CASCADE;
        DROP POLICY IF EXISTS catalog_owner_aa_master_read ON gold_aa_internal.published_tours;
        DROP POLICY IF EXISTS catalog_owner_aa_master_read ON silver_aa_internal.raw_tours;
        DELETE FROM shared.schema_versions WHERE version = '225';""")
    for table in ("gold_aa_internal.published_tours", "silver_aa_internal.raw_tours"):   # pre-225 shape (008)
        a.execute(f"ALTER POLICY tenant_isolation ON {table} "
                  "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)")
    a.execute("SELECT 1 FROM pg_roles WHERE rolname = 'catalog_owner'")
    if a.fetchone():
        a.execute("DROP OWNED BY catalog_owner")
        try:
            a.execute("DROP ROLE catalog_owner")      # 225 must create it from scratch
        except psycopg2.Error:
            pass                                      # owned objects in another DB of a shared cluster
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

    runner = _connect(RUNNER)
    runner.autocommit = True
    try:
        runner.cursor().execute(open(MIGRATION).read())   # raises on any error, incl. 225's own checks
    finally:
        runner.close()
    # AA-859 wave 4: DROP OWNED BY catalog_owner above also dropped 266's keep-alive policy on tour_atoms (its only
    # role); the views read atoms as catalog_owner, so put it back (the Dev state).
    restore_266_catalog_owner_read(a)
    try:
        yield
    finally:
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER; DROP OWNED BY {RUNNER}; DROP ROLE {RUNNER}")
        admin.close()


@pytest.fixture
def cur(rds_like_225):
    conn = _connect()
    conn.autocommit = False
    conn.notices = []          # unbounded (the default keeps the last 50)
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


class _as:
    """SET ROLE for a block of reads in the test transaction (seeding stays as the superuser)."""

    def __init__(self, c, role):
        self.c, self.role = c, role

    def __enter__(self):
        self.c.execute(f"SET ROLE {self.role}")
        return self.c

    def __exit__(self, *exc):
        if exc[0] is None:
            self.c.execute("RESET ROLE")
        return False


def _tour(c, *, name, itinerary, tenant=MASTER_TENANT, master_status="active", source_status="active",
          duration=None, country="Vietnam", group=None):
    tour_id = str(uuid.uuid4())
    c.execute("""
        INSERT INTO silver_aa_internal.raw_tours
            (tour_id, tenant_id, batch_id, src_name, country, duration, inclusions, exclusions,
             activities, source_group_id, source_status)
        VALUES (%s, %s, %s, %s, %s, %s, 'Guide', 'Flights', '["kayaking"]'::jsonb, %s, %s)""",
              (tour_id, tenant, BATCH_ID, name, country, duration, group or str(uuid.uuid4()), source_status))
    c.execute("""
        INSERT INTO gold_aa_internal.published_tours
            (tour_id, generated_content_id, tenant_id, aa_name, aa_summary, aa_highlights, aa_itineraries,
             master_status)
        VALUES (%s, %s, %s, %s, 'Summary', '["Karsts"]'::jsonb, %s, %s)""",
              (tour_id, str(uuid.uuid4()), tenant, name, itinerary, master_status))
    return tour_id


def _atom(c, tour_id, *, day, place, activity=None, deleted=False, empty=False):
    atom_id = f"zz679-{uuid.uuid4()}"
    c.execute("""
        INSERT INTO acp_contract.tour_atoms
            (atom_id, tour_id, text, itinerary_day, place, activity_type, deleted, is_empty_marker)
        VALUES (%s, %s, 'atom text', %s, %s, %s, %s, %s)""",
              (atom_id, tour_id, day, place, activity, deleted, empty))
    return atom_id


def test_migration_225_applied(cur):
    for v in VIEWS:
        cur.execute("SELECT to_regclass(%s)", (f"catalog.{v}",))
        assert cur.fetchone()[0], f"catalog.{v} missing — migration 225 did not apply (see conftest output)"
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '225'")
    assert cur.fetchone()
    for role in ("aa_app_user", "tripplanner", "booking"):
        cur.execute("SELECT has_schema_privilege(%s, 'catalog', 'USAGE')", (role,))
        assert cur.fetchone()[0], role
        for v in VIEWS:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"catalog.{v}"))
            assert cur.fetchone()[0], (role, v)
    # the internal helper is not part of the contract
    cur.execute("SELECT has_table_privilege('booking', 'catalog._tour_day_text', 'SELECT')")
    assert not cur.fetchone()[0]
    # the consumer roles get the read model only, never the pipeline tables behind it
    cur.execute("SELECT has_table_privilege('booking', 'gold_aa_internal.published_tours', 'SELECT')")
    assert not cur.fetchone()[0]


def test_225_applied_the_rds_way(cur):
    """The fixture really mirrored RDS, and 225 left the least-privilege shape behind."""
    # the runner was an owner without superuser / BYPASSRLS, on tables with FORCE RLS
    cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (RUNNER,))
    assert cur.fetchone() == (False, False)
    cur.execute("""
        SELECT c.relname, pg_get_userbyid(c.relowner), c.relrowsecurity, c.relforcerowsecurity
          FROM pg_class c
         WHERE c.oid IN ('gold_aa_internal.published_tours'::regclass, 'silver_aa_internal.raw_tours'::regclass)""")
    assert {r[1:] for r in cur.fetchall()} == {(RUNNER, True, True)}
    # catalog_owner: NOLOGIN, no BYPASSRLS, owns every view of schema catalog, has no members left
    cur.execute("""SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole FROM pg_roles
                    WHERE rolname = 'catalog_owner'""")
    assert cur.fetchone() == (False, False, False, False)
    cur.execute("""SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c
                     JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'catalog' AND c.relkind = 'v'""")
    owners = dict(cur.fetchall())
    assert set(owners) == set(VIEWS) | {"_tour_day_text"} and set(owners.values()) == {"catalog_owner"}
    # the runner must not keep catalog_owner's privileges: the policy applies to inheriting members, so
    # aa_cis_admin's own sessions would see AA master rows in any tenant scope. (USAGE = inherits; on
    # PG16 the creator keeps an ADMIN-only grant, which neither inherits nor allows SET ROLE.)
    cur.execute("SELECT pg_has_role(%s, 'catalog_owner', 'USAGE')", (RUNNER,))
    assert not cur.fetchone()[0], "the runner still inherits catalog_owner"
    for consumer in ("aa_app_user",) + CONSUMERS:
        cur.execute("SELECT pg_has_role(%s, 'catalog_owner', 'MEMBER')", (consumer,))
        assert not cur.fetchone()[0], consumer
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert not cur.fetchone()[0]
    # one SELECT-only policy per FORCE-RLS table, for catalog_owner, on the AA master tenant
    cur.execute("""SELECT tablename, cmd, roles::text[], qual FROM pg_policies
                    WHERE policyname = 'catalog_owner_aa_master_read' ORDER BY tablename""")
    rows = cur.fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("published_tours", "SELECT", ["catalog_owner"]),
                                                  ("raw_tours", "SELECT", ["catalog_owner"])]
    assert all(MASTER_TENANT in r[3] for r in rows)
    # catalog_owner reads exactly what the views need; never writes
    cur.execute("SELECT has_table_privilege('catalog_owner', 'gold_aa_internal.published_tours', 'UPDATE')")
    assert not cur.fetchone()[0]
    cur.execute("SELECT has_table_privilege('catalog_owner', 'silver_aa_internal.generated_content', 'SELECT')")
    assert not cur.fetchone()[0]


def test_catalog_owner_sees_only_the_master_tenant(cur):
    """The policy, not only the views' WHERE, keeps other tenants away from catalog_owner."""
    tag = uuid.uuid4().hex[:8]
    master = _tour(cur, name=f"Master {tag}", itinerary=CANONICAL)
    other = _tour(cur, name=f"Other {tag}", itinerary=CANONICAL, tenant=OTHER_TENANT)
    with _as(cur, "catalog_owner"):
        cur.execute("SELECT tour_id::text FROM gold_aa_internal.published_tours WHERE aa_name LIKE %s",
                    (f"%{tag}",))
        assert {r[0] for r in cur.fetchall()} == {master}
        cur.execute("SELECT tour_id::text FROM silver_aa_internal.raw_tours WHERE src_name LIKE %s",
                    (f"%{tag}",))
        assert {r[0] for r in cur.fetchall()} == {master}
    assert other != master


def test_every_catalog_view_is_security_barrier(cur):
    """The views run with their owner's (catalog_owner) rights; the RLS policy limits it to the AA
    master tenant, but inside that tenant the views' WHERE (live status) is the only row limit.
    Without security_barrier a consumer's leaky function / qual can be pushed below those filters and
    observe hidden (inactive, trashed, superseded) rows. Every view in schema catalog must carry it."""
    cur.execute("""
        SELECT c.relname, coalesce(c.reloptions, '{}')
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'catalog' AND c.relkind = 'v'
        ORDER BY c.relname""")
    rows = cur.fetchall()
    names = {r[0] for r in rows}
    assert set(VIEWS) | {"_tour_day_text"} <= names, names
    missing = [name for name, opts in rows if "security_barrier=true" not in opts]
    assert not missing, f"catalog views without security_barrier=true: {missing}"


def test_v_tour_keeps_only_live_master_content(cur):
    tag = uuid.uuid4().hex[:8]
    group = str(uuid.uuid4())
    live = _tour(cur, name=f"Hanoi and Ha Long {tag}", itinerary=CANONICAL, duration="8 Days", group=group)
    inactive = _tour(cur, name=f"Inactive {tag}", itinerary=CANONICAL, master_status="inactive")
    trashed = _tour(cur, name=f"Trashed {tag}", itinerary=CANONICAL, master_status="trashed")
    superseded = _tour(cur, name=f"Superseded {tag}", itinerary=CANONICAL, source_status="superseded")
    other = _tour(cur, name=f"Other tenant {tag}", itinerary=CANONICAL, tenant=OTHER_TENANT)
    cur.execute("UPDATE gold_aa_internal.published_tours SET deleted_at = now() WHERE tour_id = %s", (trashed,))

    for role in CONSUMERS:
        with _as(cur, role):
            cur.execute("SELECT tour_id::text FROM catalog.v_tour WHERE title LIKE %s", (f"%{tag}",))
            assert {r[0] for r in cur.fetchall()} == {live}, role
            # a consumer that sets app.tenant_id (tenant_isolation is TO public) still gets master only
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (OTHER_TENANT,))
            cur.execute("SELECT tour_id::text FROM catalog.v_tour WHERE title LIKE %s", (f"%{tag}",))
            assert {r[0] for r in cur.fetchall()} == {live}, role
            cur.execute("SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-000000000000', true)")
    assert not {inactive, trashed, superseded, other} & {live}

    cur.execute("""
        SELECT source_system, source_id, slug, title, country, duration_days, summary, highlights,
               inclusions, exclusions, activities, itinerary_text, content_hash, published_at, updated_at
          FROM catalog.v_tour WHERE tour_id = %s""", (live,))
    (system, source_id, slug, title, country, days, summary, highlights, inc, exc, activities, text,
     content_hash, published_at, updated_at) = cur.fetchone()
    assert (system, source_id) == ("cis", group)              # stable across re-uploads, not tour_id
    assert slug == f"hanoi-and-ha-long-{tag}-vietnam-{group.replace('-', '')[:8]}"
    assert (title, country, days, summary) == (f"Hanoi and Ha Long {tag}", "Vietnam", 8, "Summary")
    assert highlights == ["Karsts"] and activities == ["kayaking"]
    assert (inc, exc, text) == ("Guide", "Flights", CANONICAL)
    assert len(content_hash) == 32 and updated_at == published_at

    # content_hash moves when content moves
    cur.execute("UPDATE gold_aa_internal.published_tours SET aa_summary = 'New' WHERE tour_id = %s", (live,))
    cur.execute("SELECT content_hash FROM catalog.v_tour WHERE tour_id = %s", (live,))
    assert cur.fetchone()[0] != content_hash


def test_duration_days_falls_back_to_itinerary(cur):
    t = _tour(cur, name=f"No duration {uuid.uuid4().hex[:8]}", itinerary=CANONICAL, duration="Flexible")
    cur.execute("SELECT duration_days FROM catalog.v_tour WHERE tour_id = %s", (t,))
    assert cur.fetchone()[0] == 3


def test_v_tour_day_matches_python_parser(cur):
    from services.content_generation.itinerary_utils import parse_canonical_itinerary_days

    for text in (CANONICAL, REPEATED_DAY):
        t = _tour(cur, name=f"Days {uuid.uuid4().hex[:8]}", itinerary=text)
        cur.execute("""SELECT day_number, title, body FROM catalog.v_tour_day
                        WHERE tour_id = %s ORDER BY day_number""", (t,))
        got = {d: {"title": title, "body": body} for d, title, body in cur.fetchall()}
        assert got == parse_canonical_itinerary_days(text)

    legacy = _tour(cur, name=f"Legacy {uuid.uuid4().hex[:8]}", itinerary="Day one: arrive. Day two: leave.")
    cur.execute("SELECT count(*) FROM catalog.v_tour_day WHERE tour_id = %s", (legacy,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT itinerary_text FROM catalog.v_tour WHERE tour_id = %s", (legacy,))
    assert cur.fetchone()[0] == "Day one: arrive. Day two: leave."


def test_v_tour_place_and_day_places(cur):
    tag = uuid.uuid4().hex[:8]
    hanoi, halong, cave = f"Hanoi {tag}", f"Ha Long Bay {tag}", f"Sung Sot Cave {tag}"
    itinerary = (
        f"Day 1 — Arrive in {hanoi}\nWelcome dinner in the Old Quarter.\n\n"
        f"Day 2 — {halong}\nCruise among the karsts, then visit {cave.lower()}.\n\n"
        "Day 3 — Depart\nTransfer to the airport."
    )
    t = _tour(cur, name=f"Places {tag}", itinerary=itinerary)
    a1 = _atom(cur, t, day=1, place=hanoi.lower(), activity="culture")     # same place, other case,
    _atom(cur, t, day=1, place=hanoi.lower())                              # more frequent than the
    _atom(cur, t, day=1, place=hanoi, activity="food")                     # spelling of the text
    _atom(cur, t, day=2, place=f"Zz Unnamed {tag}")                        # not in the day text: last
    _atom(cur, t, day=2, place=cave, activity="caving")                    # inserted before Ha Long Bay
    _atom(cur, t, day=2, place=halong, activity="cruise")                  # but named first in the text
    _atom(cur, t, day=2, place=f"Ghost {tag}", deleted=True)                # deleted atom: not a place
    _atom(cur, t, day=2, place=f"Marker {tag}", empty=True)                 # empty marker: not a place
    _atom(cur, t, day=3, place="  ")                                        # blank place: skipped

    card = str(uuid.uuid4())
    cur.execute("""INSERT INTO acp_shared.place_card (card_id, place_key, display_name, locality, country)
                   VALUES (%s, %s, %s, 'Hoan Kiem', 'Vietnam')""", (card, f"hanoi-{tag}", hanoi))
    cur.execute("INSERT INTO acp_shared.atom_place_card (atom_id, card_id) VALUES (%s, %s)", (a1, card))
    dest = str(uuid.uuid4())
    cur.execute("""INSERT INTO shared.destinations (id, name, country, lat, lng)
                   VALUES (%s, %s, 'Vietnam', 21.03, 105.85)""", (dest, hanoi))
    # same name as a place, wrong country: never pinned
    cur.execute("""INSERT INTO shared.destinations (name, country, lat, lng)
                   VALUES (%s, 'Thailand', 1, 1)""", (halong,))

    cur.execute("""
        SELECT day_number, position, place_name, place_card_id::text, locality, country,
               destination_id::text, lat, lng, activity_types, atom_count
          FROM catalog.v_tour_place WHERE tour_id = %s ORDER BY day_number, position""", (t,))
    rows = cur.fetchall()
    # itinerary order (first mention in the day text), spelling as the text writes it
    assert [(r[0], r[1], r[2]) for r in rows] == [
        (1, 1, hanoi), (2, 1, halong), (2, 2, cave), (2, 3, f"Zz Unnamed {tag}"),
    ]
    day1, halong_row, cave_row, _ = rows
    assert day1[3:9] == (card, "Hoan Kiem", "Vietnam", dest, 21.03, 105.85)
    assert sorted(day1[9]) == ["culture", "food"] and day1[10] == 3
    assert halong_row[3] is None and halong_row[6] is None and halong_row[7] is None  # no card, no pin
    assert halong_row[9] == ["cruise"] and cave_row[9] == ["caving"]

    cur.execute("SELECT day_number, places FROM catalog.v_tour_day WHERE tour_id = %s ORDER BY 1", (t,))
    assert cur.fetchall() == [(1, [hanoi]), (2, [halong, cave, f"Zz Unnamed {tag}"]), (3, [])]


def test_place_spelling_without_text_match_is_deterministic(cur):
    """No spelling in the day text: the most frequent wins, a tie goes to C collation — never a random id."""
    tag = uuid.uuid4().hex[:8]
    t = _tour(cur, name=f"Spelling {tag}", itinerary=CANONICAL)
    for place in (f"hue {tag}", f"Hue {tag}", f"HUE {tag}", f"Hue {tag}"):
        _atom(cur, t, day=2, place=place)
    _atom(cur, t, day=3, place=f"hoi an {tag}")
    _atom(cur, t, day=3, place=f"Hoi An {tag}")
    cur.execute("""SELECT day_number, place_name FROM catalog.v_tour_place
                    WHERE tour_id = %s ORDER BY day_number""", (t,))
    assert cur.fetchall() == [(2, f"Hue {tag}"), (3, f"Hoi An {tag}")]  # 'H' < 'h' in C collation


def test_photos_and_destinations(cur):
    tag = uuid.uuid4().hex[:8]
    t = _tour(cur, name=f"Photos {tag}", itinerary=CANONICAL)
    dest = str(uuid.uuid4())
    cur.execute("""INSERT INTO shared.destinations (id, name, country, lat, lng, cover_image_url)
                   VALUES (%s, %s, 'Vietnam', 10.0, 106.0, 'https://example.test/c.jpg')""",
                (dest, f"Saigon {tag}"))
    photo = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO shared.place_photo
            (id, drive_file_id, drive_root_id, folder_path, country, file_name, tour_id, destination_id,
             match_source, status, s3_key_large)
        VALUES (%s, %s, 'root', 'Vietnam/x', 'Vietnam', 'a.jpg', %s, %s, 'manual', 'matched', 'k/large.jpg')""",
                (photo, f"drive-{tag}", t, dest))

    cur.execute("SELECT cover_photo_path, photo_count FROM catalog.v_tour WHERE tour_id = %s", (t,))
    assert cur.fetchone() == (f"/content/photos/{photo}", 1)
    cur.execute("SELECT photo_id::text, position FROM catalog.v_tour_photo WHERE tour_id = %s", (t,))
    assert cur.fetchall() == [(photo, 1)]

    cur.execute("""SELECT source_system, source_id, name, lat, lng, cover_photo_path, cover_image_url, photo_count
                     FROM catalog.v_destination WHERE destination_id = %s""", (dest,))
    assert cur.fetchone() == ("cis", dest, f"Saigon {tag}", 10.0, 106.0, f"/content/photos/{photo}",
                              "https://example.test/c.jpg", 1)
    cur.execute("SELECT photo_id::text FROM catalog.v_destination_photo WHERE destination_id = %s", (dest,))
    assert cur.fetchall() == [(photo,)]


def test_consumers_cannot_read_the_pipeline_tables(cur):
    tag = uuid.uuid4().hex[:8]
    _tour(cur, name=f"Direct {tag}", itinerary=CANONICAL)
    for table in ("gold_aa_internal.published_tours", "silver_aa_internal.raw_tours",
                  "acp_contract.tour_atoms", "catalog._tour_day_text"):
        cur.execute("SAVEPOINT direct")
        cur.execute("SET ROLE booking")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(f"SELECT count(*) FROM {table}")
        cur.execute("ROLLBACK TO SAVEPOINT direct")
        cur.execute("RESET ROLE")


LEAK_FN = """
    CREATE FUNCTION public.zz679_leak(text) RETURNS boolean LANGUAGE plpgsql COST 0.0000001 AS $$
    BEGIN
        RAISE NOTICE 'zz679-leak:%', $1;
        RETURN true;
    END $$"""


def _leaked(c, sql):
    c.connection.notices.clear()
    c.execute(sql)
    c.fetchall()
    return " ".join(c.connection.notices)


def test_leaky_function_probe_reveals_no_hidden_row(cur):
    """A consumer's cheap, non-leakproof function in a WHERE must only ever see rows the view returns.
    Control: the same probe on a non-barrier copy does leak, so the probe is a real one."""
    tag = uuid.uuid4().hex[:8]
    live = f"Live {tag}"
    hidden = [f"Inactive {tag}", f"Trashed {tag}", f"Superseded {tag}", f"Other tenant {tag}"]
    t = _tour(cur, name=live, itinerary=CANONICAL)
    _atom(cur, t, day=1, place=f"Hanoi {tag}")
    _tour(cur, name=hidden[0], itinerary=CANONICAL, master_status="inactive")
    trashed = _tour(cur, name=hidden[1], itinerary=CANONICAL, master_status="trashed")
    cur.execute("UPDATE gold_aa_internal.published_tours SET deleted_at = now() WHERE tour_id = %s", (trashed,))
    _tour(cur, name=hidden[2], itinerary=CANONICAL, source_status="superseded")
    o = _tour(cur, name=hidden[3], itinerary=CANONICAL, tenant=OTHER_TENANT)
    _atom(cur, o, day=1, place=f"Other place {tag}")
    cur.execute(LEAK_FN)

    with _as(cur, "booking"):
        seen = _leaked(cur, "SELECT title FROM catalog.v_tour WHERE public.zz679_leak(title)")
        assert live in seen
        assert not [h for h in hidden if h in seen], seen
        seen = _leaked(cur, "SELECT place_name FROM catalog.v_tour_place WHERE public.zz679_leak(place_name)")
        assert f"Hanoi {tag}" in seen and f"Other place {tag}" not in seen, seen
        # even with app.tenant_id pointing at the other tenant (tenant_isolation ORs it in for
        # catalog_owner) the view's own master filter holds behind the barrier
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (OTHER_TENANT,))
        seen = _leaked(cur, "SELECT title FROM catalog.v_tour WHERE public.zz679_leak(title)")
        assert live in seen and not [h for h in hidden if h in seen], seen
        cur.execute("SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-000000000000', true)")

    # control: without the barrier the same probe sees hidden master rows (RLS still hides the
    # other tenant: security quals always run before a user qual)
    cur.execute("ALTER VIEW catalog.v_tour RESET (security_barrier)")
    with _as(cur, "booking"):
        seen = _leaked(cur, "SELECT title FROM catalog.v_tour WHERE public.zz679_leak(title)")
    assert [h for h in hidden[:3] if h in seen], "control probe leaked nothing — the probe is not testing"
    assert hidden[3] not in seen


def test_tenant_isolation_treats_empty_tenant_as_none(cur):
    """225 rewrote tenant_isolation with NULLIF(..., ''); everything else about the policy is unchanged."""
    cur.execute("""SELECT tablename, permissive, roles::text[], cmd, qual, with_check FROM pg_policies
                    WHERE policyname = 'tenant_isolation'
                      AND tablename IN ('published_tours', 'raw_tours') ORDER BY tablename""")
    rows = cur.fetchall()
    assert [r[:4] for r in rows] == [("published_tours", "PERMISSIVE", ["public"], "ALL"),
                                     ("raw_tours", "PERMISSIVE", ["public"], "ALL")]
    assert all("nullif" in r[4].lower() and r[5] is None for r in rows), rows


def test_catalog_read_on_a_pooled_connection_with_empty_tenant(rds_like_225):
    """A connection that set app.tenant_id LOCAL in an earlier transaction holds '' afterwards
    (TripPlanner's pool). Reading catalog.* on it used to raise invalid input syntax for type uuid as
    soon as the scan met a non-master row; now it returns the live master rows."""
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (MASTER_TENANT,))
        conn.commit()                                                     # previous transaction ends
        c.execute("SELECT current_setting('app.tenant_id', true)")
        assert c.fetchone()[0] == ""
        tag = uuid.uuid4().hex[:8]
        live = _tour(c, name=f"Pooled {tag}", itinerary=CANONICAL)
        _tour(c, name=f"Pooled other {tag}", itinerary=CANONICAL, tenant=OTHER_TENANT)   # forces the cast
        for role in CONSUMERS:
            with _as(c, role):
                c.execute("SELECT tour_id::text FROM catalog.v_tour WHERE title LIKE %s", (f"%{tag}",))
                assert {r[0] for r in c.fetchall()} == {live}, role
                c.execute("SELECT count(*) FROM catalog.v_tour_place")    # whole views scan too
                c.execute("SELECT count(*) FROM catalog.v_tour_day")
    finally:
        conn.rollback()
        conn.close()


def test_cis_roles_with_empty_tenant_see_no_rows(cur):
    """'' is 'no tenant' for the CIS roles too: zero rows, no exception (was: invalid uuid error)."""
    tag = uuid.uuid4().hex[:8]
    _tour(cur, name=f"Empty {tag}", itinerary=CANONICAL)
    _tour(cur, name=f"Empty other {tag}", itinerary=CANONICAL, tenant=OTHER_TENANT)
    cur.execute("SELECT set_config('app.tenant_id', '', true)")
    for role in ("app_user", RUNNER):                  # RUNNER = table owner under FORCE RLS, as aa_cis_admin
        with _as(cur, role):
            cur.execute("SELECT count(*) FROM gold_aa_internal.published_tours WHERE aa_name LIKE %s",
                        (f"%{tag}",))
            assert cur.fetchone()[0] == 0, role
            cur.execute("SELECT count(*) FROM silver_aa_internal.raw_tours WHERE src_name LIKE %s",
                        (f"%{tag}",))
            assert cur.fetchone()[0] == 0, role


def test_tenant_scoped_reads_unchanged(cur):
    tag = uuid.uuid4().hex[:8]
    master = _tour(cur, name=f"Scoped {tag}", itinerary=CANONICAL)
    other = _tour(cur, name=f"Scoped other {tag}", itinerary=CANONICAL, tenant=OTHER_TENANT)
    for role in ("app_user", RUNNER):
        with _as(cur, role):
            for tenant, expected in ((MASTER_TENANT, {master}), (OTHER_TENANT, {other})):
                cur.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
                cur.execute("SELECT tour_id::text FROM gold_aa_internal.published_tours WHERE aa_name LIKE %s",
                            (f"%{tag}",))
                assert {r[0] for r in cur.fetchall()} == expected, (role, tenant)
                cur.execute("SELECT tour_id::text FROM silver_aa_internal.raw_tours WHERE src_name LIKE %s",
                            (f"%{tag}",))
                assert {r[0] for r in cur.fetchall()} == expected, (role, tenant)
        # a write into another tenant is still refused (USING doubles as WITH CHECK)
        cur.execute("SAVEPOINT w")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with _as(cur, role):
                cur.execute("SELECT set_config('app.tenant_id', %s, true)", (MASTER_TENANT,))
                cur.execute("UPDATE gold_aa_internal.published_tours SET tenant_id = %s WHERE tour_id = %s",
                            (OTHER_TENANT, master))
        cur.execute("ROLLBACK TO SAVEPOINT w")
        cur.execute("RESET ROLE")
