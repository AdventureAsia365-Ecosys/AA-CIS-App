"""AA-1025 H1a / D3 — migrations 345 (shared.llm_app_profile) and 346 (geo.geocode_call status / latency_ms / app /
stage) against real Postgres.

conftest applies every migration, 345 and 346 included. Each test re-runs both files (idempotent: CREATE ... IF NOT
EXISTS, INSERT ... ON CONFLICT DO NOTHING, ADD COLUMN IF NOT EXISTS, DROP / ADD CONSTRAINT, registration ON CONFLICT
DO NOTHING) and checks the table, the seed rows, the constraints and the cis_app grants (writes as cis_app through
SET LOCAL SESSION AUTHORIZATION, the X1 lesson). The grant tests first give cis_app the table-wide privileges cis_owner's
default privileges give it on Dev (CIS 245; CI creates the table as the superuser, so they never apply here) and
re-run 345, so its REVOKE is proven against Dev's grants. Plain SQL only: this test ships with the migration-only PR and
imports no code of the later code PR. Every change is rolled back or put back.
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

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M345 = os.path.join(MIGRATIONS, "345_llm_app_profile.sql")
M346 = os.path.join(MIGRATIONS, "346_geocode_call_ledger_columns.sql")
SEED = {"cis": "queue_retry", "tripplanner": "basic_narration", "crm": "manual_reply_needed", "booking": "none"}


def _run(c, path):
    c.execute(open(path, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT to_regclass('geo.geocode_call') AS t")
    if c.fetchone()["t"] is None:
        conn.close()
        pytest.skip("geo.geocode_call (226) is not in this DB")
    _run(c, M345)
    _run(c, M346)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        conn.close()


def test_registration_and_shapes(cur):
    for version in ("345", "346"):
        cur.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = %s", (version,))
        assert cur.fetchone()["ok"] == 1
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name = 'llm_app_profile'")
    assert {r["column_name"] for r in cur.fetchall()} == {
        "app", "ai_enabled", "bulk_paused", "default_daily_budget_usd", "degrade_mode", "disabled_reason",
        "updated_at", "updated_by"}
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'geo' "
                "AND table_name = 'geocode_call' AND column_name IN ('status', 'latency_ms', 'app', 'stage')")
    assert {r["column_name"]: r["data_type"] for r in cur.fetchall()} == {
        "status": "text", "latency_ms": "integer", "app": "text", "stage": "text"}


def test_seed_rows_are_the_t21_degrade_modes(cur):
    cur.execute("SELECT app, ai_enabled, bulk_paused, default_daily_budget_usd, degrade_mode "
                "FROM shared.llm_app_profile")
    rows = {r["app"]: r for r in cur.fetchall()}
    assert {a: r["degrade_mode"] for a, r in rows.items()} == SEED
    assert all(r["ai_enabled"] and not r["bulk_paused"] and r["default_daily_budget_usd"] is None
               for r in rows.values())


def test_rerun_keeps_an_edited_row(cur):
    cur.execute("BEGIN")
    cur.execute("UPDATE shared.llm_app_profile SET ai_enabled = false, disabled_reason = 'test' WHERE app = 'crm'")
    _run_in_tx = open(M345, encoding="utf-8").read().replace("BEGIN;", "").replace("COMMIT;", "")
    cur.execute(_run_in_tx)
    cur.execute("SELECT ai_enabled AS ok FROM shared.llm_app_profile WHERE app = 'crm'")
    assert cur.fetchone()["ok"] is False                 # ON CONFLICT DO NOTHING: a re-run never resets a switch
    cur.execute("ROLLBACK")


@pytest.mark.parametrize("sql", [
    "INSERT INTO shared.llm_app_profile (app, degrade_mode) VALUES ('portal', 'none')",
    "UPDATE shared.llm_app_profile SET degrade_mode = 'shrug' WHERE app = 'cis'",
    "UPDATE shared.llm_app_profile SET default_daily_budget_usd = -1 WHERE app = 'cis'",
    "UPDATE shared.llm_app_profile SET default_daily_budget_usd = 100001 WHERE app = 'cis'",
    "UPDATE shared.llm_app_profile SET disabled_reason = repeat('x', 501) WHERE app = 'cis'",
    "UPDATE shared.llm_app_profile SET updated_by = repeat('x', 129) WHERE app = 'cis'",
])
def test_profile_checks(cur, sql):
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(sql)
    cur.execute("ROLLBACK")


@pytest.mark.parametrize("column,value", [
    ("status", "billed_unusable"), ("status", "OK"), ("latency_ms", -1), ("app", "CIS"), ("app", "1cis"),
    ("stage", "Place Geocode"), ("stage", "x" * 65),
])
def test_geocode_call_checks(cur, column, value):
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(f"INSERT INTO geo.geocode_call (provider, requests, cost_usd, endpoint, {column}) "
                    "VALUES ('geoapify', 1, 0, 'search', %s)", (value,))
    cur.execute("ROLLBACK")


def test_geocode_call_accepts_a_v3_row_and_an_old_shape_row(cur):
    cur.execute("BEGIN")
    cur.execute("INSERT INTO geo.geocode_call (provider, requests, cost_usd, endpoint, status, latency_ms, app, stage) "
                "VALUES ('geoapify', 3, 0, 'search', 'error', 812, 'cis', 'place_geocode') RETURNING id AS ok")
    assert cur.fetchone()["ok"] is not None
    cur.execute("INSERT INTO geo.geocode_call (provider, requests, cost_usd, endpoint) "
                "VALUES ('geoapify', 1, 0, 'search') RETURNING status, app")
    row = cur.fetchone()
    assert row["status"] is None and row["app"] is None       # rows before 346 keep NULL; readers derive them
    cur.execute("ROLLBACK")


EDITABLE = ("ai_enabled", "bulk_paused", "default_daily_budget_usd", "degrade_mode", "disabled_reason", "updated_at",
            "updated_by")


def _simulate_dev_default_privileges_then_rerun_345(cur):
    """On Dev DB Migrate creates the table as cis_owner, whose default privileges in shared (CIS 245) give cis_app
    SELECT, INSERT, UPDATE, DELETE on every new table. CI creates it as the superuser, so the default privileges never
    apply: grant them here (plus TRUNCATE etc. via ALL), then re-run 345 inside the open transaction, as on Dev."""
    cur.execute("GRANT ALL ON shared.llm_app_profile TO cis_app")
    cur.execute("GRANT ALL ON shared.llm_app_profile TO PUBLIC")
    cur.execute(open(M345, encoding="utf-8").read().replace("BEGIN;", "").replace("COMMIT;", ""))


def test_345_narrows_the_default_privileges(cur):
    cur.execute("SELECT 1 AS ok FROM pg_roles WHERE rolname = 'cis_app'")
    if cur.fetchone() is None:
        pytest.skip("role cis_app is not in this DB")
    cur.execute("BEGIN")
    _simulate_dev_default_privileges_then_rerun_345(cur)
    for role in ("cis_app", "public"):
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            cur.execute("SELECT has_table_privilege(%s, 'shared.llm_app_profile', %s) AS ok", (role, priv))
            assert cur.fetchone()["ok"] is False, (role, priv)
    cur.execute("SELECT has_table_privilege('cis_app', 'shared.llm_app_profile', 'SELECT') AS ok")
    assert cur.fetchone()["ok"] is True
    cur.execute("SELECT has_table_privilege('public', 'shared.llm_app_profile', 'SELECT') AS ok")
    assert cur.fetchone()["ok"] is False
    cur.execute("SELECT has_column_privilege('cis_app', 'shared.llm_app_profile', 'app', 'UPDATE') AS ok")
    assert cur.fetchone()["ok"] is False                         # the key is not editable
    for column in EDITABLE:
        cur.execute("SELECT has_column_privilege('cis_app', 'shared.llm_app_profile', %s, 'UPDATE') AS ok", (column,))
        assert cur.fetchone()["ok"] is True, column
    cur.execute("ROLLBACK")


def test_cis_app_grants(cur):
    cur.execute("SELECT 1 AS ok FROM pg_roles WHERE rolname = 'cis_app'")
    if cur.fetchone() is None:
        pytest.skip("role cis_app is not in this DB")
    cur.execute("BEGIN")
    _simulate_dev_default_privileges_then_rerun_345(cur)          # the refusals below hold on Dev's grants too
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")       # temporary: rolled back below
    cur.execute("SET LOCAL SESSION AUTHORIZATION cis_app")
    cur.execute("SELECT ai_enabled AS ok FROM shared.llm_app_profile WHERE app = 'cis'")
    assert cur.fetchone()["ok"] is True
    cur.execute("UPDATE shared.llm_app_profile SET ai_enabled = false, disabled_reason = 'grant test', "
                "updated_at = now(), updated_by = 'admin:test' WHERE app = 'cis'")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):    # the four rows are fixed by migration
        cur.execute("INSERT INTO shared.llm_app_profile (app, degrade_mode) VALUES ('cis', 'none')")
    cur.execute("ROLLBACK TO SAVEPOINT s")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):    # a missing row means AI on: no DELETE
        cur.execute("DELETE FROM shared.llm_app_profile WHERE app = 'cis'")
    cur.execute("ROLLBACK TO SAVEPOINT s")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("TRUNCATE shared.llm_app_profile")
    cur.execute("ROLLBACK TO SAVEPOINT s")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):    # the key is not editable
        cur.execute("UPDATE shared.llm_app_profile SET app = 'cis' WHERE app = 'cis'")
    cur.execute("ROLLBACK")
