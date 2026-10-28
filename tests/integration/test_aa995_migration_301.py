"""AA-995 — migration 301: shared.place_photo.s3_key_medium (text, nullable). Real Postgres.

conftest applies every migration, 301 included (shared.place_photo comes from CIS 198). These tests check the replayed
shape, then run the file again directly (errors NOT swallowed): from the pre-301 state (column dropped, registration
removed) and twice (idempotent), and that a row keeps its other keys and reads NULL for the new one. Every test leaves
the post-301 state behind.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "301_place_photo_medium.sql")
PROBE_ROOT = "aa995_migration_301_probe"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.place_photo WHERE drive_root_id = %s", (PROBE_ROOT,))
        _run(c)                     # the post-301 state other tests see (idempotent)
        conn.close()


def _run(c):
    with open(MIGRATION, encoding="utf-8") as f:
        c.execute(f.read())


def _column(c):
    c.execute("""SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid),
                        col_description(a.attrelid, a.attnum)
                   FROM pg_attribute a
                   LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                  WHERE a.attrelid = 'shared.place_photo'::regclass AND a.attname = 's3_key_medium'
                    AND NOT a.attisdropped""")
    return c.fetchone()


def _registered(c):
    c.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '301'")
    return c.fetchone()[0]


def test_replay_applied_301(cur):
    typ, notnull, default, comment = _column(cur)
    assert (typ, notnull, default) == ("text", False, None)
    assert "960 w" in comment
    assert _registered(cur) == 1


def test_301_from_the_pre_301_state_and_again(cur):
    cur.execute("ALTER TABLE shared.place_photo DROP COLUMN s3_key_medium")
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '301'")
    assert _column(cur) is None
    _run(cur)
    _run(cur)                       # idempotent: ADD COLUMN IF NOT EXISTS, ON CONFLICT DO NOTHING
    assert _column(cur)[:3] == ("text", False, None)
    assert _registered(cur) == 1


def test_existing_rows_read_null_and_keep_their_keys(cur):
    file_id = f"probe-{uuid.uuid4().hex[:8]}"
    cur.execute("""INSERT INTO shared.place_photo (drive_file_id, drive_root_id, folder_path, country, file_name,
                                                   status, s3_key_large, s3_key_small)
                   VALUES (%s, %s, 'x', 'Bhutan', 'a.jpg', 'unmatched', 'photos/bhutan/a-1600.webp',
                           'photos/bhutan/a-600.webp')""", (file_id, PROBE_ROOT))
    _run(cur)
    cur.execute("SELECT s3_key_medium, s3_key_large, s3_key_small FROM shared.place_photo WHERE drive_file_id = %s",
                (file_id,))
    assert cur.fetchone() == (None, "photos/bhutan/a-1600.webp", "photos/bhutan/a-600.webp")
    cur.execute("UPDATE shared.place_photo SET s3_key_medium = 'photos/bhutan/a-960.webp' WHERE drive_file_id = %s",
                (file_id,))
    _run(cur)                       # a re-run never touches the data
    cur.execute("SELECT s3_key_medium FROM shared.place_photo WHERE drive_file_id = %s", (file_id,))
    assert cur.fetchone()[0] == "photos/bhutan/a-960.webp"
