"""AA-851 — migration 279: shared.countries (ISO 3166-1), the free-text resolver shared.country_iso2(), country_iso2
on raw_tours and shared.destinations (trigger + backfill), the orphan report shared.v_country_unmapped, and the
catalog contract view catalog.v_country (+ country_iso2 appended to catalog.v_destination).

conftest replays only 279's part A (part B needs 245's owners, 253-258 and the catalog recipe). Each test runs in ONE
transaction on the RDS-like state of test_aa860_migration_245 (module fixture): the replayed part A is dropped first
(the Dev state before 279), 253 / 256 / 257 / 258 are applied, a tour and three places are seeded, then 279 is applied
as cis_owner (DB Migrate's live-owner switch), the checks run, and the transaction is rolled back.

Checks: registered once and re-runnable; 249 rows, the AA spellings resolve exactly as services/places/guards.py
does; the views catalog_owner's and security_barrier, v_country SELECT to booking and tripplanner only, v_destination
keeps its grants and rows and gains country_iso2 as its last column; exact privileges on the shared objects; the
backfill and the orphan report; the trigger as cis_app (insert, rename, explicit override, unknown name); every writer
of the two tables reaches the resolver (the trigger runs as the writer, MIG-6) and the guard refuses the states that
break it; the Done-when reads as booking and tripplanner.
"""
import os
import re
import sys
import uuid

import psycopg2.errors
import psycopg2.extensions
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only)
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from services.places import guards  # noqa: E402

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
BEFORE = ("253_catalog_l4_read_model.sql", "256_catalog_line_places.sql", "257_geo_place_dedupe.sql",
          "258_catalog_live_source_key.sql")
MIGRATION = "279_countries_iso3166.sql"
UNDO_PART_A = """
    DROP VIEW IF EXISTS shared.v_country_unmapped;
    DROP TRIGGER IF EXISTS raw_tours_country_iso2 ON silver_aa_internal.raw_tours;
    DROP TRIGGER IF EXISTS destinations_country_iso2 ON shared.destinations;
    ALTER TABLE silver_aa_internal.raw_tours DROP COLUMN IF EXISTS country_iso2;
    ALTER TABLE shared.destinations DROP COLUMN IF EXISTS country_iso2;
    DROP FUNCTION IF EXISTS shared.set_country_iso2();
    DROP FUNCTION IF EXISTS shared.country_iso2(text);
    DROP TABLE IF EXISTS shared.countries;
"""
V_DEST_ROWS = """
    SELECT count(*), md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), ''))
    FROM (SELECT source_system, source_id, destination_id, name, country, lat, lng, photo_count, geo_status,
                 precision, shape_kind, min_lat, max_lng FROM catalog.v_destination) r
"""


@pytest.fixture(scope="module", autouse=True)
def migration_255():
    """test_aa712_migration_226 drops schema geo and re-applies 226 only: re-apply 255 (idempotent)."""
    c = t245._connect()
    c.autocommit = True
    with open(os.path.join(MIG, "255_geo_place_shape.sql")) as f:
        c.cursor().execute(f.read())
    c.close()


def _body(name):
    sql = open(os.path.join(MIG, name), encoding="utf-8").read()
    assert sql.count("\nBEGIN;\n") == 1 and sql.rstrip().endswith("COMMIT;")
    return re.sub(r"\nCOMMIT;\s*$", "\n", sql.replace("\nBEGIN;\n", "\n", 1))


def _apply(c, name=MIGRATION):
    c.execute("SET LOCAL ROLE cis_owner")          # DB Migrate's live-owner switch (XD8)
    c.execute(_body(name))
    c.execute("RESET ROLE")


def _cols(c, rel):
    c.execute("""SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
                  WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
              (rel,))
    return c.fetchall()


def _as(c, role, sql, args=()):
    c.execute("SAVEPOINT aa851_279")
    try:
        c.execute(f"SET LOCAL ROLE {role}")
        c.execute(sql, args)
        out = c.fetchall() if c.description else c.rowcount
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa851_279")
    return out


def _dest(c, name, country):
    c.execute("""INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, geo_precision,
                                                  name_norm)
                 VALUES (%s, %s, 16.0, 106.0, 'manual', 'ok', 'point', lower(%s)) RETURNING id::text""",
              (name, country, name))
    return c.fetchone()[0]


class _Cur(psycopg2.extensions.cursor):
    """Carries the pre-279 state and the seeded ids."""


@pytest.fixture
def cur(rds_like_245):  # noqa: F811
    conn = t245._connect()
    conn.autocommit = False
    c = conn.cursor(cursor_factory=_Cur)
    try:
        c.execute(UNDO_PART_A)                              # Dev before 279
        c.execute("SELECT 1 FROM pg_attribute WHERE attrelid = 'geo.tour_place'::regclass AND attname = 'embedding'")
        if not c.fetchone():
            c.execute("ALTER TABLE geo.tour_place ADD COLUMN IF NOT EXISTS embedding float8[]")
        for name in BEFORE:
            _apply(c, name)
        tag = uuid.uuid4().hex[:8]
        tour, _ = t228._tour(c, name=f"Tour {tag}", country="Laos")
        c.seeded = {
            "tour": tour,
            "vn": _dest(c, f"Hoi An {tag}", "Vietnam"),
            "lk": _dest(c, f"Ella {tag}", "SRI-LANDKA"),
            "orphan": _dest(c, f"Atlantis {tag}", "Atlantis"),
        }
        c.before = {"v_dest_cols": _cols(c, "catalog.v_destination"),
                    "v_dest_rows": _as(c, "tripplanner", V_DEST_ROWS)}
        _apply(c)
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_registered_rerunnable_owned_and_granted(cur):
    _apply(cur)                                             # idempotent re-run
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '279'")
    assert cur.fetchone()[0] == 1
    cur.execute("""SELECT relname, pg_get_userbyid(relowner), reloptions FROM pg_class
                    WHERE oid IN ('catalog.v_country'::regclass, 'catalog.v_destination'::regclass)""")
    for name, owner, options in cur.fetchall():
        assert owner == "catalog_owner" and "security_barrier=true" in (options or []), name
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False
    for role, allowed in (("booking", True), ("tripplanner", True), ("aa_app_user", False), ("cis_app", False)):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_country', 'SELECT')", (role,))
        assert cur.fetchone()[0] is allowed, role
    for role in ("booking", "tripplanner", "aa_app_user"):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_destination', 'SELECT')", (role,))
        assert cur.fetchone()[0] is True, role


# Every role that may write raw_tours / shared.destinations: INSERT (table or column level), UPDATE (table level or
# on country). The country_iso2 trigger runs as that role (MIG-6: not SECURITY DEFINER).
WRITERS_SQL = """
    SELECT DISTINCT g::regrole::text FROM (
        SELECT a.grantee AS g
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.oid IN ('silver_aa_internal.raw_tours'::regclass, 'shared.destinations'::regclass)
           AND a.grantee <> c.relowner AND a.privilege_type IN ('INSERT', 'UPDATE')
        UNION ALL
        SELECT a.grantee
          FROM pg_attribute att JOIN pg_class c ON c.oid = att.attrelid
         CROSS JOIN LATERAL aclexplode(att.attacl) a
         WHERE att.attrelid IN ('silver_aa_internal.raw_tours'::regclass, 'shared.destinations'::regclass)
           AND a.grantee <> c.relowner
           AND (a.privilege_type = 'INSERT' OR (a.privilege_type = 'UPDATE' AND att.attname = 'country'))) w
     ORDER BY 1
"""
CANNOT_RESOLVE = """
    SELECT r FROM unnest(%s::text[]) r
     WHERE NOT has_schema_privilege(r, 'shared', 'USAGE')
        OR NOT has_table_privilege(r, 'shared.countries', 'SELECT')
        OR NOT has_function_privilege(r, 'shared.country_iso2(text)', 'EXECUTE')
"""


def _writers(c):
    c.execute(WRITERS_SQL)
    return [r[0] for r in c.fetchall()]


def test_shared_privileges_are_exact(cur):
    writers = _writers(cur)
    assert "PUBLIC" not in writers and "aa_app_user" not in writers and "-" not in writers
    cur.execute("""
        SELECT c.relname::text, CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
               a.privilege_type
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.oid IN ('shared.countries'::regclass, 'shared.v_country_unmapped'::regclass)
           AND a.grantee <> c.relowner""")
    # cis_app and catalog_owner, plus SELECT on shared.countries for every other writer of the two tables (CI's
    # legacy app_user and the test runner roles; on Dev the set is cis_app alone).
    expected = {("countries", "catalog_owner", "SELECT"), ("countries", "cis_app", "SELECT"),
                ("v_country_unmapped", "cis_app", "SELECT")} | {("countries", w, "SELECT") for w in writers}
    assert sorted(cur.fetchall()) == sorted(expected)
    cur.execute("""
        SELECT p.proname::text, CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
               a.privilege_type
          FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
         WHERE p.oid IN ('shared.country_iso2(text)'::regprocedure, 'shared.set_country_iso2()'::regprocedure)
           AND a.grantee <> p.proowner""")
    assert sorted(cur.fetchall()) == sorted({("country_iso2", r, "EXECUTE") for r in ["cis_app", *writers]})
    assert cur.execute(CANNOT_RESOLVE, (writers,)) is None and cur.fetchall() == []
    for role, fn, allowed in (("cis_app", "shared.country_iso2(text)", True),
                              ("aa_app_user", "shared.country_iso2(text)", False),
                              ("booking", "shared.country_iso2(text)", False),
                              ("cis_app", "shared.set_country_iso2()", False)):
        cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, fn))
        assert cur.fetchone()[0] is allowed, (role, fn)
    for role in ("aa_app_user", "booking", "tripplanner"):
        cur.execute("SELECT has_table_privilege(%s, 'shared.countries', 'SELECT')", (role,))
        assert cur.fetchone()[0] is False, role


def test_seed_and_resolver(cur):
    cur.execute("SELECT count(*), count(DISTINCT iso3), count(DISTINCT numeric_code) FROM shared.countries")
    assert cur.fetchone() == (249, 249, 249)
    cur.execute("SELECT iso2, iso3, numeric_code, name, iso_name FROM shared.countries "
                "WHERE iso2 IN ('VN', 'KR', 'LA') ORDER BY iso2")
    assert cur.fetchall() == [("KR", "KOR", "410", "South Korea", "Korea, Republic of"),
                              ("LA", "LAO", "418", "Laos", "Lao People's Democratic Republic"),
                              ("VN", "VNM", "704", "Vietnam", "Viet Nam")]
    # Every spelling services/places/guards.py knows resolves to the same code in SQL.
    spellings = sorted(set(guards.COUNTRY_ISO2) | set(guards._ALIASES))
    for name in spellings:
        cur.execute("SELECT shared.country_iso2(%s)", (f"  {name.upper()} ",))
        assert cur.fetchone()[0] == guards.iso2(name).upper(), name
    for text, expected in (("Pakistan", "PK"), ("Philippines", "PH"), ("viet nam", "VN"), ("in", "IN"),
                           ("THA", "TH"), ("Turkey", "TR"), ("Atlantis", None), ("", None), (None, None)):
        cur.execute("SELECT shared.country_iso2(%s)", (text,))
        assert cur.fetchone()[0] == expected, text


def test_backfill_and_orphan_report(cur):
    cur.execute("SELECT country_iso2 FROM silver_aa_internal.raw_tours WHERE tour_id = %s", (cur.seeded["tour"],))
    assert cur.fetchone()[0] == "LA"
    cur.execute("SELECT id::text, country_iso2 FROM shared.destinations WHERE id = ANY(%s::uuid[])",
                ([cur.seeded["vn"], cur.seeded["lk"], cur.seeded["orphan"]],))
    got = dict(cur.fetchall())
    assert got == {cur.seeded["vn"]: "VN", cur.seeded["lk"]: "LK", cur.seeded["orphan"]: None}
    rows = _as(cur, "cis_app", "SELECT source, country, row_count FROM shared.v_country_unmapped")
    assert ("shared.destinations.country", "Atlantis", 1) in rows
    assert not [r for r in rows if r[1] in ("Laos", "Vietnam", "SRI-LANDKA")]


def test_trigger_as_cis_app(cur):
    tag = uuid.uuid4().hex[:8]
    out = _as(cur, "cis_app", """
        WITH ins AS (
            INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, name_norm)
            VALUES (%s, 'thailand', 13.7, 100.5, 'manual', 'ok', %s) RETURNING id, country_iso2)
        SELECT id::text, country_iso2 FROM ins""", (f"Ayutthaya {tag}", f"ayutthaya {tag}"))
    assert out[0][1] == "TH"
    cur.execute("SAVEPOINT aa851_trigger")
    cur.execute("SET LOCAL ROLE cis_app")
    cur.execute("""INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, name_norm)
                   VALUES (%s, 'Thailand', 13.7, 100.5, 'manual', 'ok', %s) RETURNING id""",
                (f"Sukhothai {tag}", f"sukhothai {tag}"))
    dest = cur.fetchone()[0]
    cur.execute("UPDATE shared.destinations SET country = 'Burma' WHERE id = %s RETURNING country_iso2", (dest,))
    assert cur.fetchone()[0] == "MM"                                      # rename re-derives
    cur.execute("UPDATE shared.destinations SET country = 'Siam', country_iso2 = 'TH' WHERE id = %s "
                "RETURNING country_iso2", (dest,))
    assert cur.fetchone()[0] == "TH"                                      # an explicit new value is kept
    cur.execute("UPDATE shared.destinations SET country = 'Atlantis' WHERE id = %s RETURNING country_iso2", (dest,))
    assert cur.fetchone()[0] is None                                      # unknown: NULL, never an error
    cur.execute("UPDATE shared.destinations SET lat = 13.8 WHERE id = %s RETURNING country_iso2", (dest,))
    assert cur.fetchone()[0] is None                                      # trigger only on country
    cur.execute("ROLLBACK TO SAVEPOINT aa851_trigger")
    cur.execute("SAVEPOINT aa851_fk")
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("UPDATE shared.destinations SET country_iso2 = 'ZZ' WHERE id = %s", (cur.seeded["vn"],))
    cur.execute("ROLLBACK TO SAVEPOINT aa851_fk")


def test_done_when_reads_as_booking_and_tripplanner(cur):
    for role in ("booking", "tripplanner"):
        rows = _as(cur, role, "SELECT * FROM catalog.v_country LIMIT 5")
        assert len(rows) == 5 and all(len(r) == 5 for r in rows), role
        assert _as(cur, role, "SELECT count(*) FROM catalog.v_country") == [(249,)], role
    joined = _as(cur, "tripplanner", """
        SELECT d.country, c.iso2, c.name FROM catalog.v_destination d
          JOIN catalog.v_country c ON c.iso2 = d.country_iso2
         WHERE d.destination_id = %s::uuid""", (cur.seeded["vn"],))
    assert joined == [("Vietnam", "VN", "Vietnam")]


def test_v_destination_keeps_columns_and_rows(cur):
    assert _cols(cur, "catalog.v_destination") == cur.before["v_dest_cols"] + [("country_iso2", "character(2)")]
    assert _as(cur, "tripplanner", V_DEST_ROWS) == cur.before["v_dest_rows"]
    assert _cols(cur, "catalog.v_country") == [("iso2", "character(2)"), ("iso3", "character(3)"),
                                               ("numeric_code", "character(3)"), ("name", "text"),
                                               ("iso_name", "text")]


def _reapply(c, match):
    c.execute("SAVEPOINT aa851_reapply")
    try:
        if match is None:
            _apply(c)
        else:
            with pytest.raises(psycopg2.errors.RaiseException, match=match):
                _apply(c)
    finally:
        if match is not None:
            c.execute("ROLLBACK TO SAVEPOINT aa851_reapply")


def test_a_new_writer_gets_the_resolver_and_its_inserts_resolve(cur):
    """The trigger runs as the writer (CI failure 25/10: "permission denied for function country_iso2" as app_user).
    279 gives every writer it sees SELECT on shared.countries and EXECUTE on the resolver, column-level writers
    included; a role that may only read or update other columns gets nothing."""
    cur.execute("CREATE ROLE zz851_writer NOLOGIN; CREATE ROLE zz851_col NOLOGIN; CREATE ROLE zz851_reader NOLOGIN")
    cur.execute("GRANT USAGE ON SCHEMA shared TO zz851_writer, zz851_col, zz851_reader")
    cur.execute("GRANT SELECT, INSERT ON shared.destinations TO zz851_writer")
    cur.execute("GRANT SELECT, UPDATE (country) ON shared.destinations TO zz851_col")
    cur.execute("GRANT SELECT, UPDATE (lat) ON shared.destinations TO zz851_reader")
    _reapply(cur, None)
    for role, allowed in (("zz851_writer", True), ("zz851_col", True), ("zz851_reader", False)):
        cur.execute("SELECT has_function_privilege(%s, 'shared.country_iso2(text)', 'EXECUTE'), "
                    "has_table_privilege(%s, 'shared.countries', 'SELECT')", (role, role))
        assert cur.fetchone() == (allowed, allowed), role
    tag = uuid.uuid4().hex[:8]
    out = _as(cur, "zz851_writer", """
        INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, name_norm)
        VALUES (%s, 'Cambodia', 13.4, 103.9, 'manual', 'ok', %s) RETURNING country_iso2""",
              (f"Siem Reap {tag}", f"siem reap {tag}"))
    assert out == [("KH",)]
    out = _as(cur, "zz851_col", "UPDATE shared.destinations SET country = 'Nepal' WHERE id = %s::uuid "
                                "RETURNING country_iso2", (cur.seeded["vn"],))
    assert out == [("NP",)]


def test_guard_refuses_a_writer_that_cannot_reach_the_resolver(cur):
    cur.execute("CREATE ROLE zz851_noschema NOLOGIN")
    cur.execute("GRANT USAGE ON SCHEMA silver_aa_internal TO zz851_noschema")
    cur.execute("GRANT INSERT ON silver_aa_internal.raw_tours TO zz851_noschema")
    _reapply(cur, r"zz851_noschema may write raw_tours / shared.destinations but cannot resolve")


def test_guard_refuses_public_or_aa_app_user_as_writer(cur):
    cur.execute("SAVEPOINT aa851_ro")
    cur.execute("GRANT UPDATE (country) ON shared.destinations TO aa_app_user")
    _reapply(cur, "PUBLIC or aa_app_user may write")
    cur.execute("ROLLBACK TO SAVEPOINT aa851_ro")
    cur.execute("GRANT INSERT ON silver_aa_internal.raw_tours TO PUBLIC")
    _reapply(cur, "PUBLIC or aa_app_user may write")


def test_guard_refuses_a_resolver_grant_beyond_the_writers(cur):
    cur.execute("GRANT EXECUTE ON FUNCTION shared.set_country_iso2() TO booking")
    _reapply(cur, r"grants on the country objects beyond the writers: set_country_iso2:booking")


def test_standing_every_writer_can_resolve_a_country():
    """Standing check on the shared test DB (replayed part A + whatever later tests granted): every role that may
    write raw_tours / shared.destinations reaches shared.country_iso2() and shared.countries, or its writes fail."""
    c = t245._connect()
    c.autocommit = True
    try:
        cur = c.cursor()
        writers = _writers(cur)
        assert "aa_app_user" not in writers and "-" not in writers
        cur.execute(CANNOT_RESOLVE, (writers,))
        assert cur.fetchall() == []
    finally:
        c.close()
