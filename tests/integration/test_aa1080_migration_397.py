"""AA-1080 — migration 397: the tenant pool (aa_app_user) may INSERT shared.tenant_brand_rules.custom_validators, and
nothing else:

  * applied by conftest (registered, the column grant present, no UPDATE on the column, no DELETE / TRUNCATE);
  * idempotent re-run, and a re-run after 262 alone (262 starts its grant set from nothing) puts the grant back.
The Brand Identity INSERT that uses the grant is tested in test_aa1080_brand_save_pg.py (the code PR).
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body, revoke_platform_reads  # noqa: E402

TENANT_ROLE = "aa_app_user"
BRAND = "shared.tenant_brand_rules"


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


@pytest.fixture
def cur():
    conn = _connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def _col_privs(c, column):
    c.execute("""SELECT has_column_privilege(%s, %s, %s, 'INSERT') AS ins,
                        has_column_privilege(%s, %s, %s, 'UPDATE') AS upd""",
              (TENANT_ROLE, BRAND, column, TENANT_ROLE, BRAND, column))
    return c.fetchone()


def test_applied_by_conftest(cur):
    cur.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = '397'")
    assert cur.fetchone(), "conftest swallowed an error in 397 (see its log)"
    assert _col_privs(cur, "custom_validators") == (True, False)
    cur.execute("SELECT has_table_privilege(%s, %s, 'DELETE, TRUNCATE') AS bad", (TENANT_ROLE, BRAND))
    assert cur.fetchone()[0] is False


def test_rerun_is_idempotent_and_restores_the_grant_after_262(cur):
    cur.execute(migration_body(397))
    revoke_platform_reads(cur)
    cur.execute(migration_body(262))                  # 262 alone drops the column grant (start from nothing)
    assert _col_privs(cur, "custom_validators") == (False, False)
    cur.execute(migration_body(397))
    assert _col_privs(cur, "custom_validators") == (True, False)
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '397'")
    assert cur.fetchone()[0] == 1
