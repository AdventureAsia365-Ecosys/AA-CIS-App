"""AA-819 — migration 230 (shared.tenants.daily_publish_limit) against real Postgres.

conftest applies every migration (230 included) to the test DB. These tests check the column shape
(nullable, no default), the positive-only CHECK, the schema_versions row, and that the file is
idempotent: it is re-applied here twice, errors not swallowed.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "230_tenant_daily_publish_limit.sql")


@pytest.fixture
def conn():  # the session fixture in conftest has applied every migration
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True
    yield c
    c.close()


def _tenant(cur, limit="NULL"):
    tid = str(uuid.uuid4())
    cur.execute(f"""INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm,
                                                posts_per_week, daily_publish_limit)
                    VALUES (%s, 'AA-819 mig test', %s, 'starter', 60, 1, {limit})""", (tid, f"aa819m-{tid[:8]}"))
    return tid


def test_column_is_nullable_integer_without_default(conn):
    cur = conn.cursor()
    cur.execute("""SELECT data_type, is_nullable, column_default FROM information_schema.columns
                   WHERE table_schema = 'shared' AND table_name = 'tenants'
                     AND column_name = 'daily_publish_limit'""")
    assert cur.fetchone() == ("integer", "YES", None)


def test_new_tenant_gets_null_and_override_is_positive_only(conn):
    cur = conn.cursor()
    tid = _tenant(cur)
    try:
        cur.execute("SELECT daily_publish_limit FROM shared.tenants WHERE tenant_id = %s", (tid,))
        assert cur.fetchone() == (None,)
        cur.execute("UPDATE shared.tenants SET daily_publish_limit = 2 WHERE tenant_id = %s", (tid,))
        for bad in (0, -1):
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("UPDATE shared.tenants SET daily_publish_limit = %s WHERE tenant_id = %s", (bad, tid))
    finally:
        cur.execute("DELETE FROM shared.tenants WHERE tenant_id = %s", (tid,))


def test_registered_and_idempotent(conn):
    cur = conn.cursor()
    sql = open(MIGRATION).read()
    cur.execute(sql)
    cur.execute(sql)
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '230'")
    assert cur.fetchone() == (1,)
    cur.execute("""SELECT count(*) FROM pg_constraint
                   WHERE conname = 'tenants_daily_publish_limit_positive'
                     AND conrelid = 'shared.tenants'::regclass""")
    assert cur.fetchone() == (1,)
