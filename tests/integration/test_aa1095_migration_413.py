"""AA-1095 (S278) — migration 413 (Bedrock connection acc1, seeded off, and its 13 chat-model deployments) against
real Postgres.

conftest applies every migration, 413 included. These tests run the file again directly (errors NOT swallowed): from
the pre-413 state, twice (idempotent), with an admin's later edit kept, and its guards. Every test leaves the post-413
state behind.
"""
import os
import sys

import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared.llm_client.connections import ACCOUNT_ID_ENVS  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "413_llm_acc1_bedrock_connection.sql")
MODELS = {
    "sonnet-5-5": "global.anthropic.claude-sonnet-5-5",
    "opus-5-5": "global.anthropic.claude-opus-5-5",
    "haiku-5-5": "global.anthropic.claude-haiku-5-5",
    "sonnet-5": "global.anthropic.claude-sonnet-5",
    "sonnet-4-6": "global.anthropic.claude-sonnet-4-6",
    "opus-4-6": "global.anthropic.claude-opus-4-6-v1",
    "haiku-4-5": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "gpt-6-luna": "global.openai.gpt-6-luna",
    "gpt-6-astra": "global.openai.gpt-6-astra",
    "gpt-6-sol": "global.openai.gpt-6-sol",
    "gpt-5.6-luna": "global.openai.gpt-5.6-luna",
    "gpt-6.1-sol": "global.openai.gpt-6.1-sol",
    "gpt-6.1-terra": "global.openai.gpt-6.1-terra",
}
ACTOR = "migration-413-aa1095"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _run(c)                         # another module may have replayed the registry: back to post-413
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        _undo(c)
        _run(c)
        conn.close()


def _run(c):
    c.execute(open(MIGRATION, encoding="utf-8").read())


def _undo(c):
    """The pre-413 state: no acc1 connection, deployments, cost policy or catalog profile; 304's env CHECK."""
    c.execute("DELETE FROM shared.llm_model_deployment WHERE connection_key = 'acc1'")
    c.execute("DELETE FROM shared.llm_connection_cost_policy WHERE connection_key = 'acc1'")
    c.execute("DELETE FROM shared.llm_provider_health WHERE connection_key = 'acc1'")
    c.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = 'acc1'")
    c.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = bedrock_profile_ids - 'acc1' "
              "WHERE bedrock_profile_ids ? 'acc1'")
    c.execute("ALTER TABLE shared.llm_provider_connection DROP CONSTRAINT llm_provider_connection_account_id_env_check")
    c.execute("ALTER TABLE shared.llm_provider_connection ADD CONSTRAINT llm_provider_connection_account_id_env_check "
              "CHECK (account_id_env IS NULL OR account_id_env IN ('BEDROCK_ACCT_PRIMARY'))")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '413'")


def _present(c) -> dict:
    """The models of MODELS that have a catalog row in this database (all 13 after the full replay)."""
    c.execute("SELECT model_key FROM shared.llm_model_catalog WHERE model_key = ANY (%s)", (list(MODELS),))
    return {r["model_key"]: MODELS[r["model_key"]] for r in c.fetchall()}


def _deployments(c) -> dict:
    c.execute("SELECT model_key, wire_id, priority, enabled, origin FROM shared.llm_model_deployment "
              "WHERE connection_key = 'acc1'")
    return {r["model_key"]: dict(r) for r in c.fetchall()}


def test_post_replay_state(cur):
    cur.execute("SELECT * FROM shared.llm_provider_connection WHERE connection_key = 'acc1'")
    row = cur.fetchone()
    assert row["kind"] == "bedrock" and row["auth_mode"] == "assume_role" and row["data_scope"] == "aws_only"
    assert row["role_name"] == "AA1-Bedrock-Invoker" and row["external_id"] == "aa1095-satellite-bedrock-acc1"
    assert row["region"] == "us-west-1" and row["account_id"] is None
    assert row["account_id_env"] == "BEDROCK_ACCT_PAYER" and row["account_id_env"] in ACCOUNT_ID_ENVS
    assert row["enabled"] is False                              # switched on by the run-script after the deploy
    assert list(row["served_roles"]) == ["writer", "judge", "validate"]
    assert row["balance_source"] == "none"
    assert row["failure_domain"] == "bedrock:BEDROCK_ACCT_PAYER:aws_only"
    cur.execute("SELECT monthly_budget_usd FROM shared.llm_provider_connection WHERE connection_key = 'acc3'")
    acc3_cap = cur.fetchone()["monthly_budget_usd"]
    assert row["monthly_budget_usd"] == (acc3_cap if acc3_cap is not None else 100)

    present = _present(cur)
    assert len(present) == 13, sorted(set(MODELS) - set(present))
    deps = _deployments(cur)
    assert {k: d["wire_id"] for k, d in deps.items()} == present
    assert all(d["priority"] == 100 and d["enabled"] and d["origin"] == "seed" for d in deps.values())
    cur.execute("SELECT model_key, bedrock_profile_ids ->> 'acc1' AS acc1 FROM shared.llm_model_catalog "
                "WHERE bedrock_profile_ids ? 'acc1'")
    assert {r["model_key"]: r["acc1"] for r in cur.fetchall()} == present

    cur.execute("SELECT alert_pct, hard_stop FROM shared.llm_connection_cost_policy WHERE connection_key = 'acc1'")
    assert cur.fetchone() is not None


def test_from_the_pre_413_state_and_idempotent(cur):
    _undo(cur)
    cur.execute("SELECT count(*) AS n FROM shared.llm_provider_connection WHERE connection_key = 'acc1'")
    assert cur.fetchone()["n"] == 0
    _run(cur)
    first = _deployments(cur)
    assert set(first) == set(_present(cur))
    # an admin's later edits are kept by a second run
    cur.execute("UPDATE shared.llm_provider_connection SET enabled = true, updated_by = 'admin:test' "
                "WHERE connection_key = 'acc1'")
    cur.execute("UPDATE shared.llm_model_deployment SET priority = 15, updated_by = 'admin:test' "
                "WHERE connection_key = 'acc1' AND model_key = 'sonnet-5-5'")
    _run(cur)
    cur.execute("SELECT enabled FROM shared.llm_provider_connection WHERE connection_key = 'acc1'")
    assert cur.fetchone()["enabled"] is True
    assert _deployments(cur)["sonnet-5-5"]["priority"] == 15
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '413'")
    assert cur.fetchone()


def test_account_env_check_allows_the_two_satellite_envs_only(cur):
    cur.execute("BEGIN")
    cur.execute("UPDATE shared.llm_provider_connection SET account_id_env = 'BEDROCK_ACCT_PAYER' "
                "WHERE connection_key = 'acc1'")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.llm_provider_connection SET account_id_env = 'HOME' WHERE connection_key = 'acc1'")
    cur.execute("ROLLBACK")


def test_guard_refuses_an_acc1_row_of_another_role(cur):
    _undo(cur)
    cur.execute("INSERT INTO shared.llm_provider_connection (connection_key, kind, auth_mode, label, region, "
                "role_name, external_id, data_scope, enabled, updated_by) VALUES ('acc1', 'bedrock', 'assume_role', "
                "'x', 'us-west-1', 'Some-Other-Role', 'x', 'aws_only', false, 'test')")
    with pytest.raises(psycopg2.Error, match="another kind / role / ExternalId"):
        _run(cur)
    cur.execute("ROLLBACK")                     # the file's own BEGIN is still open after the raise
    cur.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = 'acc1'")


def test_the_route_chains_and_existing_rows_are_not_touched(cur):
    cur.execute("SELECT stage, model_id, fallback_model_ids, updated_by FROM shared.llm_role_config")
    before_routes = {r["stage"]: dict(r) for r in cur.fetchall()}
    cur.execute("SELECT model_key, connection_key, priority, enabled, wire_id FROM shared.llm_model_deployment "
                "WHERE connection_key <> 'acc1'")
    before_deps = {(r["model_key"], r["connection_key"]): dict(r) for r in cur.fetchall()}
    _undo(cur)
    _run(cur)
    cur.execute("SELECT stage, model_id, fallback_model_ids, updated_by FROM shared.llm_role_config")
    assert {r["stage"]: dict(r) for r in cur.fetchall()} == before_routes
    cur.execute("SELECT model_key, connection_key, priority, enabled, wire_id FROM shared.llm_model_deployment "
                "WHERE connection_key <> 'acc1'")
    assert {(r["model_key"], r["connection_key"]): dict(r) for r in cur.fetchall()} == before_deps
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_catalog WHERE updated_by = %s "
                "AND NOT (model_key = ANY (%s))", (ACTOR, list(MODELS)))
    assert cur.fetchone()["n"] == 0
