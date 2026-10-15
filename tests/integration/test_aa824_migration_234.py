"""AA-824 — migration 234: per-role connection limits (data standards C2.2) + pg_stat_statements.

conftest applies every migration to the CI database; this checks what 234 left behind and that the
file is idempotent (re-applied inside a transaction that is rolled back).
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "234_connection_limits_pg_stat_statements.sql")
EXPECTED = {"tripplanner": 30, "booking": 15, "aa_app_user": 15}


def _body() -> str:
    with open(MIGRATION) as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = False
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_connection_limits(cur):
    cur.execute("select rolname, rolconnlimit from pg_roles where rolname = any(%s)", (list(EXPECTED),))
    assert dict(cur.fetchall()) == EXPECTED


def test_cis_runtime_role_not_limited(cur):
    # The migration runner / CIS main pool must never be limited here (it would lock DB Migrate out).
    assert "aa_cis_admin" not in _body().split("ALTER ROLE", 1)[-1].split("DO $$")[0]


def test_pg_stat_statements_extension(cur):
    cur.execute("select 1 from pg_extension where extname = 'pg_stat_statements'")
    assert cur.fetchone() is not None


def test_registered(cur):
    cur.execute("select description from shared.schema_versions where version = '234'")
    row = cur.fetchone()
    assert row is not None and "CONNECTION LIMIT" in row[0]


def test_idempotent(cur):
    cur.execute(_body())
    cur.execute("select rolname, rolconnlimit from pg_roles where rolname = any(%s)", (list(EXPECTED),))
    assert dict(cur.fetchall()) == EXPECTED
