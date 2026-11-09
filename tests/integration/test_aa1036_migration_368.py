"""AA-1036 R3 slice 3 — migration 368: shared.tenants.api_key_hash is set NULL (tenant API keys are gone).

conftest replays 368 like every file. The module then seeds three synthetic tenants as the pre-368 state on Dev looked
(an active tenant with a sha256 hash, an offboarded one with the old REVOKED_<id> marker, a suspended one), plus one
with no hash, re-applies the file itself (errors not swallowed; a second run must be a no-op) and checks:
  * no row keeps a hash;
  * account_status, is_active, cancelled_at, suspend_reason and updated_at of every row are unchanged;
  * the column carries the retired comment; 368 is registered once.

CIS 369 (destructive) then dropped the column, and the replay runs 369 too: the module puts a stand-in column back for
its own run (key_hash_standin, dropped at teardown) and checks that 368 on a DB without the column is a no-op.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import key_hash_standin, migration_body  # noqa: E402

import psycopg2  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "368_tenant_api_key_hash_null.sql")
TAG = uuid.uuid4().hex[:8]
STATE_SQL = ("SELECT tenant_id::text AS tenant_id, account_status, is_active, cancelled_at, suspend_reason, updated_at "
             "FROM shared.tenants WHERE slug LIKE %s ORDER BY slug")


@pytest.fixture(scope="module")
def cur():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True
    k = c.cursor()
    with key_hash_standin(k):                                 # AA-1036: the replay ran 369 (column dropped)
        ids = [str(uuid.uuid4()) for _ in range(4)]
        rows = [
            (ids[0], f"Brightfern Journeys {TAG}", f"aa1036-a-{TAG}", "a" * 64, True, None),
            (ids[1], f"Quillmoor Travel {TAG}", f"aa1036-b-{TAG}", f"REVOKED_{ids[1]}", False, "now()"),
            (ids[2], f"Saltmarsh Tours {TAG}", f"aa1036-c-{TAG}", "c" * 64, False, None),
            (ids[3], f"Tidewell Trips {TAG}", f"aa1036-d-{TAG}", None, True, None),
        ]
        for tid, name, slug, key_hash, active, cancelled in rows:
            k.execute(
                "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, api_key_hash, "
                "rate_limit_rpm, is_active, cancelled_at) VALUES (%s, %s, %s, 'starter', 3, %s, 60, %s, "
                + ("now()" if cancelled else "NULL") + ")",
                (tid, name, slug, key_hash, active))
        k.execute(STATE_SQL, (f"aa1036-%-{TAG}",))
        before = k.fetchall()
        k.execute("SELECT count(*) AS n FROM shared.tenants WHERE api_key_hash IS NOT NULL AND slug LIKE %s",
                  (f"aa1036-%-{TAG}",))
        seeded = k.fetchone()[0]
        c.notices.clear()
        sql = open(MIGRATION, encoding="utf-8").read()
        k.execute(sql)                                        # errors raise here (conftest swallows its own run)
        k.execute(sql)                                        # a second run is a no-op
        yield k, before, seeded, list(c.notices)
        k.execute("DELETE FROM shared.tenants WHERE slug LIKE %s", (f"aa1036-%-{TAG}",))
    c.close()


def test_the_seed_had_three_hashes(cur):
    _, _, seeded, _ = cur
    assert seeded == 3


def test_no_hash_is_left(cur):
    k, _, _, notices = cur
    k.execute("SELECT count(*) AS n FROM shared.tenants WHERE api_key_hash IS NOT NULL")
    assert k.fetchone()[0] == 0
    assert any("migration 368: cleared api_key_hash" in n for n in notices), notices


def test_the_account_state_of_every_row_is_unchanged(cur):
    k, before, _, _ = cur
    k.execute(STATE_SQL, (f"aa1036-%-{TAG}",))
    assert k.fetchall() == before
    assert [r[1] for r in before] == ["active", "offboarding", "suspended", "active"]


def test_the_column_is_marked_retired_and_368_is_registered_once(cur):
    k, _, _, _ = cur
    k.execute("SELECT col_description('shared.tenants'::regclass, attnum) AS c FROM pg_attribute "
              "WHERE attrelid = 'shared.tenants'::regclass AND attname = 'api_key_hash'")
    assert k.fetchone()[0].startswith("Retired (AA-1036")
    k.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '368'")
    assert k.fetchone()[0] == 1


def test_368_on_a_db_without_the_column_is_a_noop():
    """The replay state after CIS 369 (the column dropped, here inside a rolled-back transaction whatever the module
    fixture did): 368 says so and changes nothing."""
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    try:
        k = c.cursor()
        k.execute("ALTER TABLE shared.tenants DROP COLUMN IF EXISTS api_key_hash")
        c.notices.clear()
        k.execute(migration_body(368))
        assert any("migration 368: shared.tenants.api_key_hash is absent" in n for n in c.notices), c.notices
        k.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '368'")
        assert k.fetchone()[0] == 1
    finally:
        c.rollback()
        c.close()
