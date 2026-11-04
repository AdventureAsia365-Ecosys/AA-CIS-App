"""AA-1042 (S268) — CIS migration 370 against real Postgres: shared.llm_route_settings, the route config versions with
their baseline, llm_call_log.route_config_version and its trigger, the cis_app grants, the retired gpt-4.1 deployment
off. conftest applies every migration, 370 included; each test re-runs the file (idempotent, errors not swallowed).
The route control code that uses these tables is tested in tests/integration/test_aa1042_route_control_db.py.
"""
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

M370 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "370_llm_route_control_versions.sql")


def _run(c):
    c.execute(open(M370, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _run(c)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        conn.close()


# ── the migration ────────────────────────────────────────────────────────────────────────────────────────────────

def test_applies_twice_and_registers(cur):
    _run(cur)
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '370'")
    assert cur.fetchone()["ok"] == "370"
    cur.execute("SELECT count(*) AS n FROM shared.llm_route_settings")
    assert cur.fetchone()["n"] == 1


def test_baseline_version_snapshots_every_route_and_deployment(cur):
    cur.execute("SELECT action, created_by, snapshot, diff FROM shared.llm_route_config_version WHERE version = 1")
    row = cur.fetchone()
    assert row["action"] == "baseline" and row["created_by"] == "migration-370-aa1042" and row["diff"] == []
    snap = row["snapshot"]
    assert snap["order_mode"] == "model_first"
    cur.execute("SELECT count(*) AS n FROM shared.llm_role_config")
    assert len(snap["routes"]) == cur.fetchone()["n"]
    cur.execute("SELECT count(DISTINCT model_key) AS n FROM shared.llm_model_deployment")
    assert len(snap["priorities"]) == cur.fetchone()["n"]
    any_route = next(iter(snap["routes"].values()))
    assert set(any_route) == {"model_id", "fallback_model_ids"} and isinstance(any_route["fallback_model_ids"], list)


def test_settings_checks(cur):
    cur.execute("BEGIN")
    try:
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE shared.llm_route_settings SET order_mode = 'random'")
        cur.execute("ROLLBACK TO SAVEPOINT s")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute("INSERT INTO shared.llm_route_settings (singleton, current_version) VALUES (true, 1)")
        cur.execute("ROLLBACK TO SAVEPOINT s")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("INSERT INTO shared.llm_route_config_version (version, created_by, action, snapshot) "
                        "VALUES (99999, 'admin:zz', 'revert', '{}')")       # a revert names the version it restored
    finally:
        cur.execute("ROLLBACK")


def test_the_retired_gpt41_deployment_is_off(cur):
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_deployment WHERE model_key = 'gpt-4.1' AND enabled")
    assert cur.fetchone()["n"] == 0


def test_cis_app_grants(cur):
    cur.execute("SELECT 1 AS ok FROM pg_roles WHERE rolname = 'cis_app'")
    if cur.fetchone() is None:
        pytest.skip("no cis_app role in this database")
    checks = {
        ("shared.llm_route_settings", "SELECT"): True, ("shared.llm_route_settings", "UPDATE"): True,
        ("shared.llm_route_settings", "DELETE"): False,
        ("shared.llm_route_config_version", "SELECT"): True, ("shared.llm_route_config_version", "INSERT"): True,
        ("shared.llm_route_config_version", "UPDATE"): False, ("shared.llm_route_config_version", "DELETE"): False,
    }
    for (table, priv), want in checks.items():
        cur.execute("SELECT has_table_privilege('cis_app', %s, %s) AS ok", (table, priv))
        assert cur.fetchone()["ok"] is want, (table, priv)
    for col, want in (("priority", True), ("updated_by", True), ("enabled", False), ("wire_id", False)):
        cur.execute("SELECT has_column_privilege('cis_app', 'shared.llm_model_deployment', %s, 'UPDATE') AS ok", (col,))
        assert cur.fetchone()["ok"] is want, col
    cur.execute("SELECT has_function_privilege('public', 'shared.llm_call_log_stamp_route_version()', 'EXECUTE') "
                "AS ok")
    assert cur.fetchone()["ok"] is False


def test_the_trigger_stamps_cis_rows_only(cur):
    cur.execute("SELECT current_version AS v FROM shared.llm_route_settings")
    version = cur.fetchone()["v"]
    ids = [str(uuid.uuid4()) for _ in range(3)]
    cur.execute("BEGIN")
    try:
        for i, (app, explicit) in enumerate((("cis", None), ("tripplanner", None), ("cis", 7))):
            cur.execute("INSERT INTO shared.llm_call_log (id, stage, role, model, quality_signal, created_at, app, "
                        "route_config_version) VALUES (%s, 'zz_aa1042_stage', 'validate', 'zz-model', '{}', now(), "
                        "%s, %s)", (ids[i], app, explicit))
        cur.execute("SELECT id::text AS id, route_config_version AS v FROM shared.llm_call_log "
                    "WHERE id = ANY (%s::uuid[])", (ids,))
        got = {r["id"]: r["v"] for r in cur.fetchall()}
        assert got == {ids[0]: version, ids[1]: None, ids[2]: 7}
    finally:
        cur.execute("ROLLBACK")
