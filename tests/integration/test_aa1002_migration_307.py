"""AA-1002 (gateway slice S3) — migration 307 (shared.llm_model_alias) against real Postgres.

conftest applies every migration, 307 included. Each test re-runs 307 (idempotent: CREATE TABLE IF NOT EXISTS, rows ON
CONFLICT DO NOTHING) and checks: the seed is the legacy chain's order; every alias target is a catalog row with an
enabled acc3 deployment whose wire_id is the legacy constant the chain invokes (so the alias path sends the very same
request); the gateway's own SELECT runs; constraints (FK RESTRICT, key / position shape); cis_app reads it.
"""
import os
import sys

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared.llm_client import bedrock_satellite as bs  # noqa: E402
from shared.llm_client.deployments import _ALIAS_SQL  # noqa: E402

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M307 = os.path.join(MIGRATIONS, "307_llm_model_alias.sql")


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute(open(M307, encoding="utf-8").read())
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("RESET ROLE")
        c.execute("DELETE FROM shared.llm_model_alias WHERE updated_by <> 'migration-307-aa1002'")
        c.execute(open(M307, encoding="utf-8").read())
        conn.close()


def test_the_seed_is_the_legacy_chain_order(cur):
    cur.execute(_ALIAS_SQL)
    rows = [(r["alias_key"], r["position"], r["model_key"]) for r in cur.fetchall()]
    assert rows == [("haiku", 1, "haiku"), ("sonnet", 1, "sonnet"), ("sonnet", 2, "haiku")]
    cur.execute("SELECT version FROM shared.schema_versions WHERE version = '307'")
    assert cur.fetchone() is not None


def test_every_target_runs_the_profile_the_legacy_chain_invokes(cur):
    """The alias path calls each target's enabled deployments: with the 304 rows that is exactly the chain's constant
    inference profile on acc3 (the acc2-native rows stay disabled, as LLM_LEGACY_ACC2_NATIVE is off)."""
    cur.execute("SELECT DISTINCT a.model_key, m.api_style, m.enabled FROM shared.llm_model_alias a "
                "JOIN shared.llm_model_catalog m USING (model_key)")
    targets = {r["model_key"]: r for r in cur.fetchall()}
    assert set(targets) == {"haiku", "sonnet"}
    assert all(r["api_style"] == "anthropic_native" for r in targets.values())
    cur.execute("SELECT d.model_key, d.connection_key, d.wire_id FROM shared.llm_model_deployment d "
                "JOIN shared.llm_provider_connection c USING (connection_key) "
                "WHERE d.model_key IN ('haiku', 'sonnet') AND d.enabled AND c.enabled ORDER BY d.model_key, d.priority")
    enabled = [(r["model_key"], r["connection_key"], r["wire_id"]) for r in cur.fetchall()]
    assert enabled == [("haiku", "acc3", bs._PROFILE_ID_HAIKU), ("sonnet", "acc3", bs._PROFILE_ID_SONNET)]


def test_rerunning_is_a_no_op(cur):
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_alias")
    before = cur.fetchone()["n"]
    cur.execute(open(M307, encoding="utf-8").read())
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_alias")
    assert cur.fetchone()["n"] == before == 3


def test_a_catalog_row_an_alias_names_cannot_be_deleted(cur):
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = 'haiku'")
    cur.execute("ROLLBACK")


@pytest.mark.parametrize("row,error", [
    (("haiku", 2, "no-such-model"), psycopg2.errors.ForeignKeyViolation),
    (("haiku", 0, "sonnet"), psycopg2.errors.CheckViolation),
    (("Bad Key", 1, "sonnet"), psycopg2.errors.CheckViolation),
    (("sonnet", 3, "haiku"), psycopg2.errors.UniqueViolation),          # haiku twice in one alias
])
def test_constraints(cur, row, error):
    cur.execute("BEGIN")
    with pytest.raises(error):
        cur.execute("INSERT INTO shared.llm_model_alias (alias_key, position, model_key, updated_by) "
                    "VALUES (%s, %s, %s, 'test')", row)
    cur.execute("ROLLBACK")


def test_cis_app_reads_the_aliases(cur):
    cur.execute("SELECT has_table_privilege('cis_app', 'shared.llm_model_alias', 'SELECT') AS ok")
    assert cur.fetchone()["ok"] is True
