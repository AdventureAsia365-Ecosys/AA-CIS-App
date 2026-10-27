"""AA-990 — migration 288: raw_tours.country is a foreign key to shared.countries (name), any ISO 3166-1 country as the
tour's source says, instead of the hand-made 20-name CHECK chk_raw_tours_country; country / country_iso2 are set
together; the S0 spellings become shared.countries aliases.

conftest skips 288. Each test runs in ONE transaction on the RDS-like state of test_aa860_migration_245 (module
fixture: Dev after 244 + 245, with 279's part A from the replay), seeds tours, applies 288 as cis_owner (DB Migrate's
live-owner switch), checks, and is rolled back.

Checks: registered once and re-runnable; the FK and the CHECK validated, the 20-name CHECK gone, the trigger kept; no
tour's country changed (a trek sold as India stays India); any ISO name is accepted and gets its code, a spelling that
is not the canonical name or a compound is refused; the table equals the in-code mirror shared/countries and
shared.country_iso2 agrees with shared.countries.resolve on every lookup key (the DB drift test); the guard refuses a
tour country that is not a shared.countries name.
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
import test_aa712_migration_228 as t228  # noqa: E402  (seed constants only)
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from shared import countries  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "288_raw_tours_country_iso.sql")


def _body():
    sql = open(MIGRATION, encoding="utf-8").read()
    assert sql.count("\nBEGIN;\n") == 1 and sql.rstrip().endswith("COMMIT;")
    return re.sub(r"\nCOMMIT;\s*$", "\n", sql.replace("\nBEGIN;\n", "\n", 1))


def _apply(c):
    c.execute("SET LOCAL ROLE cis_owner")          # DB Migrate's live-owner switch (XD8)
    c.execute(_body())
    c.execute("RESET ROLE")


def _tour(c, country, iso2=None):
    tour_id = str(uuid.uuid4())
    c.execute("""INSERT INTO silver_aa_internal.raw_tours
                     (tour_id, tenant_id, batch_id, src_name, country, country_iso2, source_group_id, source_status)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')""",
              (tour_id, t228.MASTER_TENANT, t228.BATCH_ID, f"zz288 {tour_id[:8]}", country, iso2, str(uuid.uuid4())))
    return tour_id


def _refused(c, exc, sql, args=()):
    c.execute("SAVEPOINT zz288")
    try:
        with pytest.raises(exc):
            c.execute(sql, args)
    finally:
        c.execute("ROLLBACK TO SAVEPOINT zz288")


def _constraints(c):
    c.execute("""SELECT conname::text, contype::text, convalidated FROM pg_constraint
                  WHERE conrelid = 'silver_aa_internal.raw_tours'::regclass
                    AND conname IN ('raw_tours_country_fkey', 'raw_tours_country_iso2_set', 'chk_raw_tours_country')
                  ORDER BY 1""")
    return c.fetchall()


class _Cur(psycopg2.extensions.cursor):
    """Carries the seeded tour ids."""


@pytest.fixture
def conn(rds_like_245):  # noqa: F811
    c = t245._connect()
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def cur(conn):
    c = conn.cursor(cursor_factory=_Cur)
    c.execute("DELETE FROM shared.schema_versions WHERE version = '288'")
    c.seeded = {"india": _tour(c, "India"), "none": _tour(c, None)}
    _apply(c)
    return c


def test_registered_rerunnable_and_the_list_is_gone(cur):
    _apply(cur)                                             # idempotent re-run
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '288'")
    assert cur.fetchone()[0] == 1
    assert _constraints(cur) == [("raw_tours_country_fkey", "f", True), ("raw_tours_country_iso2_set", "c", True)]
    cur.execute("""SELECT confrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE conname = 'raw_tours_country_fkey'""")
    assert cur.fetchone() == ("shared.countries", "FOREIGN KEY (country) REFERENCES shared.countries(name)")
    cur.execute("""SELECT count(*) FROM pg_trigger WHERE tgrelid = 'silver_aa_internal.raw_tours'::regclass
                    AND tgname = 'raw_tours_country_iso2' AND NOT tgisinternal""")
    assert cur.fetchone()[0] == 1


def test_no_tour_country_changes(cur):
    cur.execute("SELECT tour_id::text, country, country_iso2 FROM silver_aa_internal.raw_tours "
                "WHERE tour_id = ANY(%s::uuid[]) ORDER BY country NULLS LAST",
                ([cur.seeded["india"], cur.seeded["none"]],))
    assert cur.fetchall() == [(cur.seeded["india"], "India", "IN"), (cur.seeded["none"], None, None)]


def test_any_iso_country_as_the_source_says(cur):
    tz = _tour(cur, "Tanzania")
    sg = _tour(cur, "Singapore")
    cur.execute("SELECT country_iso2 FROM silver_aa_internal.raw_tours WHERE tour_id = ANY(%s::uuid[]) ORDER BY 1",
                ([tz, sg],))
    assert [r[0] for r in cur.fetchall()] == ["SG", "TZ"]
    cur.execute("UPDATE silver_aa_internal.raw_tours SET country = 'Kazakhstan' WHERE tour_id = %s "
                "RETURNING country_iso2", (tz,))
    assert cur.fetchone()[0] == "KZ"                         # the trigger re-derives the code


def test_spellings_and_compounds_are_refused(cur):
    insert = """INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                    source_group_id, source_status) VALUES (gen_random_uuid(), %s, %s, 'zz288 x', %s,
                    gen_random_uuid(), 'active')"""
    # A spelling the trigger can map to a code still fails the name FK; a name it cannot map (compound, unknown)
    # leaves country_iso2 NULL, and the iso2-set CHECK fires before the FK (checks run before FK triggers).
    refused = (psycopg2.errors.ForeignKeyViolation, psycopg2.errors.CheckViolation)
    for bad in ("INDIA", "india", "Viet Nam", "India and Pakistan", "Tibet", "Atlantis"):
        _refused(cur, refused, insert, (t228.MASTER_TENANT, t228.BATCH_ID, bad))
    # a code with no country is refused too (the trigger never invents a country)
    _refused(cur, psycopg2.errors.CheckViolation,
             "UPDATE silver_aa_internal.raw_tours SET country_iso2 = 'IN' WHERE tour_id = %s", (cur.seeded["none"],))


def test_the_table_equals_the_mirror(cur):
    cur.execute("SELECT iso2::text, iso3::text, numeric_code::text, name, iso_name, aliases FROM shared.countries")
    table = {r[0]: (r[1], r[2], r[3], r[4], sorted(r[5])) for r in cur.fetchall()}
    mirror = {c.iso2: (c.iso3, c.numeric, c.name, c.iso_name, sorted(c.aliases)) for c in countries.all_countries()}
    assert table == mirror
    keys = sorted({k for c in countries.all_countries() for k in c.lookup_keys()})
    cur.execute("SELECT k, shared.country_iso2(k)::text FROM unnest(%s::text[]) k", (keys,))
    got = dict(cur.fetchall())
    assert {k: countries.resolve(k).iso2 for k in keys} == got


def test_guard_refuses_a_country_that_is_not_a_name(conn):
    c = conn.cursor()
    c.execute("DELETE FROM shared.schema_versions WHERE version = '288'")
    c.execute("ALTER TABLE silver_aa_internal.raw_tours DROP CONSTRAINT IF EXISTS chk_raw_tours_country")
    _tour(c, "INDIA", "IN")
    with pytest.raises(psycopg2.errors.RaiseException, match="not a shared.countries name"):
        _apply(c)


def test_new_aliases_backfill_place_codes(conn):
    """279's invariant after the 8 new aliases: a place whose country text is one of them ('thai', 'bali') gets its
    country_iso2; its country text is not touched (only country_iso2 is SET, the UPDATE OF country trigger does not
    fire)."""
    c = conn.cursor()
    c.execute("DELETE FROM shared.schema_versions WHERE version = '288'")
    ids = []
    for name, country in (("zz288 Wat Pho", "thai"), ("zz288 Ubud", "bali")):
        c.execute("""INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, locality_norm)
                     VALUES (%s, %s, 1.0, 1.0, 'llm', 'ok', '') RETURNING id::text, country_iso2""", (name, country))
        dest_id, iso2 = c.fetchone()
        assert iso2 is None                                  # not a spelling before 288
        ids.append(dest_id)
    _apply(c)
    c.execute("SELECT country, country_iso2::text FROM shared.destinations WHERE id = ANY(%s::uuid[]) ORDER BY 1",
              (ids,))
    assert c.fetchall() == [("bali", "ID"), ("thai", "TH")]
