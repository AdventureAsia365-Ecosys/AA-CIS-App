"""AA-712 X4 — migration 270: tripplanner keeps only the catalog views and the Model Gateway grants of CIS.

conftest replays 270 like every file. The grants 270 removes are partly out of band on Dev (published_tours SELECT,
schema USAGE on gold_aa_internal), so the module first restores Dev's pre-270 grant set as the test superuser (the
owner here, so the migration's REVOKE removes them like cis_owner's does on Dev), then re-applies the file itself
(errors not swallowed; a second run must be a no-op) and checks the effect by privilege:
  * no table / column privilege of tripplanner on published_tours, shared.destinations, the two photo views,
    acp_contract.v_active_tour_atoms, nor anything else of gold_aa_internal / silver_aa_internal / acp_contract /
    acp_shared / shared outside the three gateway tables;
  * USAGE on gold_aa_internal and acp_contract gone; USAGE on shared and catalog kept;
  * kept: the gateway grants (CIS 173) and the catalog view reads;
  * registered once;
  * the guards refuse (review round, S248): a schema USAGE granted by another grantor, which cis_owner's REVOKE
    leaves in place, and a leftover TRUNCATE / REFERENCES / TRIGGER table privilege; nothing changes (the file's
    transaction rolls back).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

import psycopg2  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "270_tripplanner_cis_reads_revoked.sql")
ATOM_COLS = "atom_id, tour_id, owner_scope, itinerary_day, place, activity_type, action, text, season_note, deleted, " \
            "is_empty_marker"
# Dev's tripplanner grants on CIS objects before 270 (db-schema-reference.md, 22/10/2026), where the objects exist.
PRE_270 = [
    ("schema", "gold_aa_internal", "GRANT USAGE ON SCHEMA gold_aa_internal TO tripplanner"),
    ("schema", "acp_contract", "GRANT USAGE ON SCHEMA acp_contract TO tripplanner"),
    ("schema", "shared", "GRANT USAGE ON SCHEMA shared TO tripplanner"),
    ("relation", "gold_aa_internal.published_tours", "GRANT SELECT ON gold_aa_internal.published_tours TO tripplanner"),
    ("relation", "shared.destinations", "GRANT SELECT ON shared.destinations TO tripplanner"),
    ("relation", "shared.v_destination_photos", "GRANT SELECT ON shared.v_destination_photos TO tripplanner"),
    ("relation", "shared.v_tour_photos", "GRANT SELECT ON shared.v_tour_photos TO tripplanner"),
    ("relation", "acp_contract.v_active_tour_atoms",
     f"GRANT SELECT ({ATOM_COLS}) ON acp_contract.v_active_tour_atoms TO tripplanner"),
    ("relation", "shared.llm_role_config", "GRANT SELECT ON shared.llm_role_config TO tripplanner"),
    ("relation", "shared.llm_model_catalog", "GRANT SELECT ON shared.llm_model_catalog TO tripplanner"),
    ("relation", "shared.llm_call_log", "GRANT INSERT ON shared.llm_call_log TO tripplanner"),
]
REVOKED = ("gold_aa_internal.published_tours", "shared.destinations", "shared.v_destination_photos",
           "shared.v_tour_photos", "acp_contract.v_active_tour_atoms")
GATEWAY = {"shared.llm_role_config": "SELECT", "shared.llm_model_catalog": "SELECT", "shared.llm_call_log": "INSERT"}

ANY_PRIVILEGE_SQL = """
SELECT n.nspname || '.' || c.relname
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('gold_aa_internal', 'silver_aa_internal', 'acp_contract', 'acp_shared', 'shared')
   AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND (has_table_privilege('tripplanner', c.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
        OR EXISTS (SELECT 1 FROM pg_attribute a
                    WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                      AND has_column_privilege('tripplanner', c.oid, a.attnum, 'SELECT, INSERT, UPDATE, REFERENCES')))
 ORDER BY 1
"""


def _exists(k, kind, name):
    k.execute("SELECT to_regnamespace(%s) IS NOT NULL" if kind == "schema" else "SELECT to_regclass(%s) IS NOT NULL",
              (name,))
    return k.fetchone()[0]


@pytest.fixture(scope="module")
def cur():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True
    k = c.cursor()
    k.execute("SELECT to_regrole('tripplanner') IS NOT NULL")
    if not k.fetchone()[0]:
        pytest.skip("role tripplanner missing in this database (conftest creates it in CI)")
    seeded = []
    for kind, name, sql in PRE_270:
        if _exists(k, kind, name):
            k.execute(sql)
            seeded.append(name)
    k.execute(ANY_PRIVILEGE_SQL)
    before = [r[0] for r in k.fetchall()]
    c.notices.clear()
    sql = open(MIGRATION, encoding="utf-8").read()
    k.execute(sql)                                            # errors raise here (conftest swallows its own run)
    k.execute(sql)                                            # a second run is a no-op
    yield k, list(c.notices), seeded, before
    c.close()


def test_the_seed_reproduced_the_pre_270_grants(cur):
    _, _, seeded, before = cur
    assert "gold_aa_internal.published_tours" in seeded and "shared.destinations" in seeded
    assert {"gold_aa_internal.published_tours", "shared.destinations"} <= set(before)


def test_only_the_gateway_tables_keep_a_privilege(cur):
    k, notices, _, _ = cur
    k.execute(ANY_PRIVILEGE_SQL)
    left = [r[0] for r in k.fetchall()]
    assert set(left) <= set(GATEWAY), left
    assert not set(left) & set(REVOKED)
    assert any("revoked" in n for n in notices), notices


def test_the_gateway_grants_are_kept(cur):
    k, _, _, _ = cur
    for rel, priv in GATEWAY.items():
        if _exists(k, "relation", rel):
            k.execute("SELECT has_table_privilege('tripplanner', %s, %s)", (rel, priv))
            assert k.fetchone()[0] is True, (rel, priv)


def test_schema_usage(cur):
    k, _, _, _ = cur
    k.execute("SELECT has_schema_privilege('tripplanner', 'gold_aa_internal', 'USAGE'), "
              "has_schema_privilege('tripplanner', 'acp_contract', 'USAGE'), "
              "has_schema_privilege('tripplanner', 'shared', 'USAGE')")
    gold, acp, shared = k.fetchone()
    assert gold is False and acp is False and shared is True
    if _exists(k, "schema", "catalog"):
        k.execute("SELECT has_schema_privilege('tripplanner', 'catalog', 'USAGE')")
        assert k.fetchone()[0] is True


def test_the_catalog_reads_are_kept(cur):
    k, _, _, _ = cur
    if not _exists(k, "relation", "catalog.v_tour"):
        pytest.skip("catalog.v_tour (CIS 225) not in this database")
    k.execute("SELECT has_table_privilege('tripplanner', 'catalog.v_tour', 'SELECT'), "
              "has_table_privilege('tripplanner', 'catalog.v_destination', 'SELECT')")
    assert k.fetchone() == (True, True)


def test_registered_once(cur):
    k, _, _, _ = cur
    k.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '270'")
    assert k.fetchone()[0] == 1


def _apply_expecting_failure(k, setup, cleanup):
    """Runs `setup`, then the migration (must raise), rolls the failed transaction back, then `cleanup`; returns the
    error text and whether 270's work was undone (USAGE on shared still there, i.e. nothing else changed)."""
    for q in setup:
        k.execute(q)
    error = None
    try:
        k.execute(open(MIGRATION, encoding="utf-8").read())
    except psycopg2.Error as e:
        error = str(e)
        k.execute("ROLLBACK")                             # autocommit: the file's BEGIN block is left aborted
    finally:
        for q in cleanup:
            k.execute(q)
    return error


def test_a_schema_usage_by_another_grantor_fails_the_apply(cur):
    k, _, _, _ = cur
    k.execute("SELECT to_regnamespace('gold_aa_internal') IS NOT NULL")
    if not k.fetchone()[0]:
        pytest.skip("schema gold_aa_internal not in this database")
    error = _apply_expecting_failure(
        k,
        ["CREATE ROLE aa712_other_grantor NOLOGIN",
         "GRANT USAGE ON SCHEMA gold_aa_internal TO aa712_other_grantor WITH GRANT OPTION",
         "SET ROLE aa712_other_grantor",
         "GRANT USAGE ON SCHEMA gold_aa_internal TO tripplanner",
         "RESET ROLE"],
        ["REVOKE USAGE ON SCHEMA gold_aa_internal FROM aa712_other_grantor CASCADE",
         "DROP ROLE aa712_other_grantor"])
    assert error and "still holds USAGE on schema gold_aa_internal" in error and "aa712_other_grantor" in error, error
    k.execute("SELECT has_schema_privilege('tripplanner', 'gold_aa_internal', 'USAGE')")
    assert k.fetchone()[0] is False                       # the cleanup's CASCADE removed the out-of-band grant


@pytest.mark.parametrize("privilege", ["TRUNCATE", "REFERENCES", "TRIGGER"])
def test_a_leftover_non_dml_privilege_fails_the_apply(cur, privilege):
    k, _, _, _ = cur
    if not _exists(k, "relation", "silver_aa_internal.raw_tours"):
        pytest.skip("silver_aa_internal.raw_tours not in this database")
    error = _apply_expecting_failure(
        k,
        [f"GRANT {privilege} ON silver_aa_internal.raw_tours TO tripplanner"],
        [f"REVOKE {privilege} ON silver_aa_internal.raw_tours FROM tripplanner"])
    assert error and "still holds privileges on" in error and "silver_aa_internal.raw_tours" in error, error
