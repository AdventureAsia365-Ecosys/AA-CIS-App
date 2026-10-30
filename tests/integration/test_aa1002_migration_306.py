"""AA-1002 (gateway slice S2) — migration 306 (Anthropic API deployments behind acc3 + the admin switch grant) against
real Postgres.

conftest applies every migration, 306 included. Each test re-runs 304 then 306 (another module may have dropped the
304 tables or deleted catalog rows), checks the seed, that no route changes (the first enabled deployment of every
model is still not on the Anthropic API, and the connection stays off), idempotence, and that cis_app may write the
three switch columns and nothing else.
"""
import os
import re
import sys

import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared.llm_client.adapters.anthropic_messages import _effort_facts  # noqa: E402

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M304 = os.path.join(MIGRATIONS, "304_llm_connection_deployment.sql")
M306 = os.path.join(MIGRATIONS, "306_llm_anthropic_deployments.sql")


def _run(c, path):
    c.execute(open(path, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _run(c, M304)
    _run(c, M306)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("RESET ROLE")
        c.execute("UPDATE shared.llm_provider_connection SET enabled = false, updated_by = 'migration-304-aa1002' "
                  "WHERE connection_key = 'anthropic-direct'")
        c.execute("DELETE FROM shared.llm_model_deployment WHERE updated_by = 'migration-306-aa1002'")   # edits too
        _run(c, M304)
        _run(c, M306)
        conn.close()


def _expected_wire(profile: str):
    wire = re.sub(r"-v[0-9]+:[0-9]+$", "", re.sub(r"^(global|us|eu|apac|jp|au)\.anthropic\.", "", profile))
    return wire if re.fullmatch(r"claude-[a-z0-9-]+", wire) else None


def test_one_api_deployment_per_anthropic_converse_model(cur):
    cur.execute("SELECT model_key, bedrock_profile_ids ->> 'acc3' AS acc3 FROM shared.llm_model_catalog "
                "WHERE vendor = 'anthropic' AND provider = 'bedrock' AND api_style = 'converse' "
                "AND coalesce(bedrock_profile_ids ->> 'acc3', '') <> ''")
    expected = {r["model_key"]: _expected_wire(r["acc3"]) for r in cur.fetchall()}
    expected = {k: v for k, v in expected.items() if v}
    cur.execute("SELECT model_key, wire_id, priority, enabled, origin FROM shared.llm_model_deployment "
                "WHERE connection_key = 'anthropic-direct'")
    rows = {r["model_key"]: r for r in cur.fetchall()}
    assert {k: r["wire_id"] for k, r in rows.items()} == expected
    assert all(r["priority"] == 100 and r["enabled"] and r["origin"] == "seed" for r in rows.values())
    assert "haiku" not in rows and "sonnet" not in rows                       # legacy keys: slice S3
    assert len(rows) >= 2                                                     # sonnet-5-5, haiku-5-5 at least
    for model_key, r in rows.items():                                         # every seeded family is known
        assert _effort_facts(r["wire_id"])[0], (model_key, r["wire_id"])


def test_no_route_changes_the_first_enabled_deployment_is_never_the_api(cur):
    cur.execute("SELECT DISTINCT ON (d.model_key) d.model_key, d.connection_key FROM shared.llm_model_deployment d "
                "JOIN shared.llm_provider_connection c USING (connection_key) "
                "WHERE d.enabled AND c.enabled ORDER BY d.model_key, d.priority, d.connection_key")
    assert all(r["connection_key"] != "anthropic-direct" for r in cur.fetchall())
    cur.execute("SELECT enabled FROM shared.llm_provider_connection WHERE connection_key = 'anthropic-direct'")
    assert cur.fetchone()["enabled"] is False


def test_rerun_is_idempotent_and_keeps_an_admin_edit(cur):
    cur.execute("UPDATE shared.llm_model_deployment SET priority = 5, origin = 'admin' "
                "WHERE connection_key = 'anthropic-direct' AND model_key = 'haiku-5-5'")
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_deployment")
    before = cur.fetchone()["n"]
    _run(cur, M306)
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_deployment")
    assert cur.fetchone()["n"] == before
    cur.execute("SELECT priority FROM shared.llm_model_deployment "
                "WHERE connection_key = 'anthropic-direct' AND model_key = 'haiku-5-5'")
    assert cur.fetchone()["priority"] == 5
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '306'")
    assert cur.fetchone()


# AA-1002 S4: conftest applies every migration, so 310's grant (the admin-editable columns) is there too.
S311_COLUMNS = {"secret_name", "base_url", "timeout_s", "max_retries", "max_concurrency", "rpm_limit",
                "monthly_budget_usd", "notes"}


def test_cis_app_writes_the_switch_columns_only(cur):
    cur.execute("SELECT column_name FROM information_schema.column_privileges WHERE table_schema = 'shared' "
                "AND table_name = 'llm_provider_connection' AND grantee = 'cis_app' AND privilege_type = 'UPDATE'")
    assert {r["column_name"] for r in cur.fetchall()} == {"enabled", "updated_at", "updated_by"} | S311_COLUMNS
    cur.execute("BEGIN")
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")                   # Dev has it; rolled back below
    cur.execute("SET LOCAL SESSION AUTHORIZATION cis_app")
    cur.execute("SELECT connection_key FROM shared.llm_provider_connection WHERE connection_key = 'anthropic-direct' "
                "FOR UPDATE")                                                  # the switch's row lock
    cur.execute("UPDATE shared.llm_provider_connection SET enabled = true, updated_at = now(), "
                "updated_by = 'admin:test' WHERE connection_key = 'anthropic-direct'")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):               # identity stays migration-only (310)
        cur.execute("UPDATE shared.llm_provider_connection SET data_scope = 'aws_only' "
                    "WHERE connection_key = 'anthropic-direct'")
    cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    cur.execute("SET LOCAL SESSION AUTHORIZATION cis_app")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("UPDATE shared.llm_model_deployment SET priority = 1")
    cur.execute("ROLLBACK")


def test_enabling_without_a_secret_is_still_refused_by_the_304_check(cur):
    cur.execute("BEGIN")
    cur.execute("UPDATE shared.llm_provider_connection SET secret_name = NULL "
                "WHERE connection_key = 'anthropic-direct'")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.llm_provider_connection SET enabled = true "
                    "WHERE connection_key = 'anthropic-direct'")
    cur.execute("ROLLBACK")


def test_guard_refuses_a_missing_304(cur):
    cur.execute("BEGIN")
    cur.execute("DELETE FROM shared.llm_model_deployment WHERE connection_key = 'anthropic-direct'")
    cur.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = 'anthropic-direct'")
    sql = open(M306, encoding="utf-8").read().replace("BEGIN;", "", 1).replace("COMMIT;", "")
    with pytest.raises(psycopg2.errors.RaiseException, match="anthropic-direct"):
        cur.execute(sql)
    cur.execute("ROLLBACK")
