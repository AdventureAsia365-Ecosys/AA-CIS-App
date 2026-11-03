"""AA-1027 E5 (root plan S263, gap TP-1) — migration 351: tripplanner loses the grandfathered SELECT of CIS 173 on
shared.llm_role_config and shared.llm_model_catalog; it keeps the ledger INSERT and the catalog contract views.

conftest replays 351 like every file. The module first restores Dev's pre-351 grants (CIS 173's SELECT on the two
tables, USAGE on shared, INSERT on the ledger) as the test superuser (the owner here, so the migration's REVOKE removes
them like cis_owner's does on Dev), then re-applies the file itself (errors not swallowed; a second run must be a
no-op) and checks the effect by privilege:
  * no table / column privilege of tripplanner on either table, and on schema shared only the ledger INSERT;
  * kept: USAGE on shared, INSERT on shared.llm_call_log;
  * registered once;
  * the guards refuse: a SELECT granted by another grantor (which the owner's REVOKE leaves in place), and a leftover
    privilege on another shared table; nothing changes (the file's transaction rolls back).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

import psycopg2  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "351_tripplanner_gateway_table_reads_revoked.sql")
TABLES = ("shared.llm_role_config", "shared.llm_model_catalog")
# Dev's tripplanner grants on these objects before 351 (db-schema-reference.md, 02/11/2026; CIS 173).
PRE_351 = [
    "GRANT USAGE ON SCHEMA shared TO tripplanner",
    "GRANT SELECT ON shared.llm_role_config TO tripplanner",
    "GRANT SELECT ON shared.llm_model_catalog TO tripplanner",
    "GRANT INSERT ON shared.llm_call_log TO tripplanner",
]
SHARED_PRIVILEGES_SQL = """
SELECT c.relname
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'shared'
 WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND (has_table_privilege('tripplanner', c.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
        OR EXISTS (SELECT 1 FROM pg_attribute a
                    WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                      AND has_column_privilege('tripplanner', c.oid, a.attnum, 'SELECT, INSERT, UPDATE, REFERENCES')))
 ORDER BY 1
"""


@pytest.fixture(scope="module")
def cur():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True
    k = c.cursor()
    k.execute("SELECT to_regrole('tripplanner') IS NOT NULL AS ok")
    if not k.fetchone()[0]:
        pytest.skip("role tripplanner missing in this database (conftest creates it in CI)")
    for sql in PRE_351:
        k.execute(sql)
    k.execute("SELECT has_table_privilege('tripplanner', 'shared.llm_role_config', 'SELECT') AS ok")
    seeded = k.fetchone()[0]
    c.notices.clear()
    sql = open(MIGRATION, encoding="utf-8").read()
    k.execute(sql)                                            # errors raise here (conftest swallows its own run)
    k.execute(sql)                                            # a second run is a no-op
    yield k, list(c.notices), seeded
    c.close()


def test_the_seed_reproduced_the_pre_351_grants(cur):
    _, _, seeded = cur
    assert seeded is True


def test_the_two_tables_are_revoked(cur):
    k, notices, _ = cur
    for rel in TABLES:
        k.execute("SELECT has_table_privilege('tripplanner', %s, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, "
                  "REFERENCES, TRIGGER') AS t, has_any_column_privilege('tripplanner', %s, 'SELECT') AS c",
                  (rel, rel))
        assert k.fetchone() == (False, False), rel
    assert any("351" in n and "revoked" in n for n in notices), notices


def test_only_the_ledger_insert_is_left_on_shared(cur):
    k, _, _ = cur
    k.execute(SHARED_PRIVILEGES_SQL)
    assert [r[0] for r in k.fetchall()] == ["llm_call_log"]
    k.execute("SELECT has_table_privilege('tripplanner', 'shared.llm_call_log', 'INSERT') AS ins, "
              "has_table_privilege('tripplanner', 'shared.llm_call_log', 'SELECT') AS sel, "
              "has_schema_privilege('tripplanner', 'shared', 'USAGE') AS usage")
    assert k.fetchone() == (True, False, True)


def test_registered_once(cur):
    k, _, _ = cur
    k.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '351'")
    assert k.fetchone()[0] == 1


def _apply_expecting_failure(k, setup, cleanup):
    """Runs `setup`, then the migration (must raise), rolls the failed transaction back, then `cleanup` (always as the
    session user again, so a failed setup never leaves the module's cursor under another role)."""
    error = None
    try:
        for q in setup:
            k.execute(q)
        k.execute(open(MIGRATION, encoding="utf-8").read())
    except psycopg2.Error as e:
        error = str(e)
        k.execute("ROLLBACK")                             # autocommit: the file's BEGIN block is left aborted
    finally:
        k.execute("RESET ROLE")
        for q in cleanup:
            k.execute(q)
    return error


def test_a_select_by_another_grantor_fails_the_apply(cur):
    k, _, _ = cur
    error = _apply_expecting_failure(
        k,
        ["CREATE ROLE aa1027_other_grantor NOLOGIN",
         "GRANT USAGE ON SCHEMA shared TO aa1027_other_grantor",
         "GRANT SELECT ON shared.llm_role_config TO aa1027_other_grantor WITH GRANT OPTION",
         "SET ROLE aa1027_other_grantor",
         "GRANT SELECT ON shared.llm_role_config TO tripplanner",
         "RESET ROLE"],
        ["REVOKE SELECT ON shared.llm_role_config FROM aa1027_other_grantor CASCADE",
         "REVOKE USAGE ON SCHEMA shared FROM aa1027_other_grantor",
         "DROP ROLE aa1027_other_grantor"])
    assert error and "still holds a privilege on shared.llm_role_config" in error, error
    assert "aa1027_other_grantor" in error, error
    k.execute("SELECT has_table_privilege('tripplanner', 'shared.llm_role_config', 'SELECT') AS ok")
    assert k.fetchone()[0] is False                       # the cleanup's CASCADE removed the out-of-band grant


def test_a_leftover_privilege_on_another_shared_table_fails_the_apply(cur):
    k, _, _ = cur
    k.execute("SELECT to_regclass('shared.stage_model_params') IS NOT NULL AS ok")
    if not k.fetchone()[0]:
        pytest.skip("shared.stage_model_params (320) not in this database")
    error = _apply_expecting_failure(
        k,
        ["GRANT SELECT ON shared.stage_model_params TO tripplanner"],
        ["REVOKE SELECT ON shared.stage_model_params FROM tripplanner"])
    assert error and "still holds privileges on shared.stage_model_params" in error, error
