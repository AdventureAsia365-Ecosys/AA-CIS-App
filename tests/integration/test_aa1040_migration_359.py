"""AA-1040 (EU AI Act Art. 50(2)) — migration 359: catalog.v_tour appends text_updated_at, when the served Master
text last changed (the Master row's updated_at, else the first publish); a photo change moves updated_at, not it.

conftest skips 359 (it needs 253 / 273 / 289's catalog part and the catalog recipe). Each test runs in ONE transaction
on the RDS-like state of test_aa860_migration_245 (module fixture), the way test_aa990_migration_289 builds it: 273's
replayed column is dropped first, 253 / 256 / 257 / 258 / 273 / 289 and 359 are applied as cis_owner (DB Migrate's
live-owner switch), the checks run, and the transaction is rolled back.

Checks: registered once, re-runnable; still catalog_owner's with security_barrier, the same three consumer grants and
catalog_owner loses CREATE again; 289's columns first and unchanged, text_updated_at appended; every old column keeps
its value; text_updated_at = the Master row's updated_at; a newer photo moves updated_at but not text_updated_at; a
re-publish moves both; the consumers' reads run as tripplanner / booking / aa_app_user.
"""
import os
import sys
import uuid

import psycopg2.extensions
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only)
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa990_migration_289 as t289  # noqa: E402  (helpers: _apply, _cols, migration_255)
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa990_migration_289 import migration_255  # noqa: E402,F401  (module autouse fixture)

BEFORE = t289.BEFORE + (t289.MIGRATION,)
MIGRATION = "359_catalog_tour_text_updated_at.sql"
OLD_COLUMNS = """
    t.source_system, t.source_id, t.tour_id, t.source_version, t.sku, t.slug, t.title, t.subtitle, t.summary,
    t.description, t.highlights, t.country, t.duration_raw, t.duration_days, t.activities, t.inclusions,
    t.exclusions, t.best_time_to_go, t.group_size, t.lifecycle_stage, t.itinerary_text, t.cover_photo_path,
    t.photo_count, t.published_at, t.updated_at, t.content_hash
"""
# What CIS services/catalog_http/reader.py and TripPlanner backend/browse/catalog.py select once the column is there.
MARK_SQL = """SELECT t.published_at, t.updated_at, t.text_updated_at FROM catalog.v_tour t
               WHERE t.source_system = 'cis' AND t.tour_id = %s::uuid"""


class _Cur(psycopg2.extensions.cursor):
    """Carries the seeded tours and the pre-359 state."""


def _as(c, role, sql, *args):
    c.execute(f"SET LOCAL ROLE {role}")
    c.execute(sql, args)
    out = c.fetchall()
    c.execute("RESET ROLE")
    return out


def _snapshot(c):
    return _as(c, "tripplanner", f"SELECT {OLD_COLUMNS} FROM catalog.v_tour t ORDER BY t.tour_id")


def _photo(c, tour_id, tag):
    c.execute("""INSERT INTO shared.place_photo (id, drive_file_id, drive_root_id, folder_path, country, file_name,
                                                 tour_id, match_source, status, s3_key_large)
                 VALUES (%s, %s, 'root', 'Laos/x', 'Laos', 'a.jpg', %s, 'manual', 'matched', 'k/large.jpg')""",
              (str(uuid.uuid4()), f"drive-1040-{tag}", tour_id))


@pytest.fixture
def cur(rds_like_245):  # noqa: F811
    conn = t245._connect()
    conn.autocommit = False
    c = conn.cursor(cursor_factory=_Cur)
    try:
        c.execute("ALTER TABLE gold_aa_internal.published_tours DROP COLUMN IF EXISTS updated_at")   # Dev before 273
        c.execute("SELECT 1 FROM pg_attribute WHERE attrelid = 'geo.tour_place'::regclass AND attname = 'embedding'")
        if not c.fetchone():
            c.execute("ALTER TABLE geo.tour_place ADD COLUMN IF NOT EXISTS embedding float8[]")
        for name in BEFORE:
            t289._apply(c, name)
        tag = uuid.uuid4().hex[:8]
        c.plain, _ = t228._tour(c, name=f"Mark plain {tag}", country="Laos")
        c.photo, _ = t228._tour(c, name=f"Mark photo {tag}", country="Laos")
        # the text was published a day ago; the photo arrives afterwards (its updated_at defaults to now())
        c.execute("""UPDATE gold_aa_internal.published_tours
                        SET published_at = now() - interval '2 days', updated_at = now() - interval '1 day'
                      WHERE tour_id IN (%s, %s)""", (c.plain, c.photo))
        _photo(c, c.photo, tag)
        c.before_cols = t289._cols(c, "catalog.v_tour")
        c.before_rows = _snapshot(c)
        t289._apply(c, MIGRATION)
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_registered_rerunnable_owned_and_granted(cur):
    t289._apply(cur, MIGRATION)                              # idempotent re-run
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '359'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT pg_get_userbyid(relowner), coalesce(reloptions, '{}') FROM pg_class "
                "WHERE oid = 'catalog.v_tour'::regclass")
    owner, options = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in options
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False
    for role, allowed in (("tripplanner", True), ("booking", True), ("aa_app_user", True), ("cis_app", False)):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_tour', 'SELECT')", (role,))
        assert cur.fetchone()[0] is allowed, role


def test_old_columns_unchanged_and_one_appended(cur):
    after = t289._cols(cur, "catalog.v_tour")
    assert after[:-1] == cur.before_cols
    assert after[-1] == ("text_updated_at", "timestamp with time zone")
    assert _snapshot(cur) == cur.before_rows


def test_text_updated_at_is_the_master_publish_not_the_photo(cur):
    cur.execute("SELECT tour_id::text, updated_at FROM gold_aa_internal.published_tours WHERE tour_id IN (%s, %s)",
                (cur.plain, cur.photo))
    master = dict(cur.fetchall())
    (pub, upd, text_at), = _as(cur, "tripplanner", MARK_SQL, cur.plain)
    assert text_at == master[cur.plain] == upd and pub < text_at
    (pub, upd, text_at), = _as(cur, "tripplanner", MARK_SQL, cur.photo)
    assert text_at == master[cur.photo]
    assert upd > text_at                                     # the photo moved updated_at only


def test_republish_moves_text_updated_at(cur):
    cur.execute("UPDATE gold_aa_internal.published_tours SET updated_at = now() + interval '1 minute' "
                "WHERE tour_id = %s RETURNING updated_at", (cur.photo,))
    new = cur.fetchone()[0]
    (_, upd, text_at), = _as(cur, "booking", MARK_SQL, cur.photo)
    assert text_at == new == upd


def test_every_consumer_role_reads_the_column(cur):
    for role in ("tripplanner", "booking", "aa_app_user"):
        rows = _as(cur, role, MARK_SQL, cur.plain)
        assert len(rows) == 1 and rows[0][2] is not None, role


def test_closing_check_refuses_a_changed_old_column(cur):
    """Mutation check: the after-guard catches a statement that changes an existing column's value."""
    body = t289._body(MIGRATION).replace(
        "GREATEST(e.published_at, e.master_updated_at, e.photos_updated_at) AS updated_at",
        "GREATEST(e.published_at, e.master_updated_at) AS updated_at")
    cur.execute("SAVEPOINT aa1040_359")
    try:
        cur.execute("SET LOCAL ROLE cis_owner")
        with pytest.raises(psycopg2.Error, match="an existing catalog.v_tour column changed value"):
            cur.execute(body)
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT aa1040_359")
