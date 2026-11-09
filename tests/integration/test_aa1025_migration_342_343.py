"""AA-1025 B3 / B5 — migrations 342 (shared.provider_health, shared.provider_credit_topup) and 343 (the anthropic /
openai connections take balance_source topups_minus_spend) against real Postgres.

conftest applies every migration, 342 and 343 included. Each test re-runs both files (idempotent: CREATE ... IF NOT
EXISTS, GRANT, an UPDATE that only touches 'none' / NULL, registration ON CONFLICT DO NOTHING) and checks the tables,
their constraints, the cascade / no-cascade of the foreign keys, the cis_app grants (writes as cis_app through SET LOCAL
SESSION AUTHORIZATION, the X1 lesson) and what 343 changes. Plain SQL only: this test ships with the migration-only PR
and imports no code of the later code PR.
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
M342 = os.path.join(MIGRATIONS, "342_provider_health_and_credit_ledger.sql")
M343 = os.path.join(MIGRATIONS, "343_vendor_api_balance_from_ledger.sql")
TEST_KEY = "zz-aa1025-conn"


def _run(c, path):
    c.execute(open(path, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT to_regclass('shared.llm_provider_connection') AS t")
    if c.fetchone()["t"] is None:
        conn.close()
        pytest.skip("shared.llm_provider_connection (304) is not in this DB")
    _run(c, M342)
    _run(c, M343)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.provider_credit_topup WHERE connection_key = %s", (TEST_KEY,))
        c.execute("DELETE FROM shared.provider_health WHERE connection_key = %s", (TEST_KEY,))
        c.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = %s", (TEST_KEY,))
        conn.close()


def _connection(c):
    c.execute("INSERT INTO shared.llm_provider_connection (connection_key, kind, auth_mode, label, data_scope, "
              "enabled, updated_by) VALUES (%s, 'mock', 'none', 'aa1025 test', 'aws_only', false, 'test') "
              "ON CONFLICT (connection_key) DO NOTHING", (TEST_KEY,))


def test_tables_and_registration(cur):
    for version in ("342", "343"):
        cur.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = %s", (version,))
        assert cur.fetchone()["ok"] == 1
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name = 'provider_health'")
    assert {r["column_name"] for r in cur.fetchall()} == {
        "connection_key", "last_ok_at", "last_error_at", "last_error_class", "last_error", "consecutive_failures",
        "circuit_open_until", "open_streak", "opened_at", "probe_until", "updated_at"}
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name = 'provider_credit_topup'")
    assert {r["column_name"] for r in cur.fetchall()} == {
        "id", "connection_key", "kind", "effective_at", "amount_usd", "note", "created_by", "created_at"}


def test_health_row_constraints_and_cascade(cur):
    _connection(cur)
    cur.execute("INSERT INTO shared.provider_health (connection_key, last_error_class, consecutive_failures) "
                "VALUES (%s, 'other', 5)", (TEST_KEY,))
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.provider_health SET last_error_class = 'vibes' WHERE connection_key = %s",
                    (TEST_KEY,))
    cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("INSERT INTO shared.provider_health (connection_key) VALUES ('zz-aa1025-nobody')")
    cur.execute("ROLLBACK")
    cur.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = %s", (TEST_KEY,))
    cur.execute("SELECT count(*) AS n FROM shared.provider_health WHERE connection_key = %s", (TEST_KEY,))
    assert cur.fetchone()["n"] == 0                    # derived state goes with its connection


def test_ledger_constraints_and_no_cascade(cur):
    _connection(cur)
    cur.execute("INSERT INTO shared.provider_credit_topup (connection_key, kind, amount_usd) "
                "VALUES (%s, 'opening_balance', 0)", (TEST_KEY,))
    for kind, amount in (("topup", 0), ("topup", -1), ("opening_balance", -0.01), ("refund", 5)):
        cur.execute("BEGIN")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("INSERT INTO shared.provider_credit_topup (connection_key, kind, amount_usd) "
                        "VALUES (%s, %s, %s)", (TEST_KEY, kind, amount))
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("INSERT INTO shared.provider_credit_topup (connection_key, amount_usd, note) "
                    "VALUES (%s, 1, %s)", (TEST_KEY, "x" * 501))
    cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):        # a money record never goes with a registry edit
        cur.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = %s", (TEST_KEY,))
    cur.execute("ROLLBACK")


def test_cis_app_grants(cur):
    cur.execute("SELECT 1 AS ok FROM pg_roles WHERE rolname = 'cis_app'")
    if cur.fetchone() is None:
        pytest.skip("role cis_app is not in this DB")
    _connection(cur)
    cur.execute("BEGIN")
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")       # temporary: rolled back below
    cur.execute("SET LOCAL SESSION AUTHORIZATION cis_app")
    cur.execute("INSERT INTO shared.provider_health (connection_key, consecutive_failures) VALUES (%s, 1)",
                (TEST_KEY,))
    cur.execute("UPDATE shared.provider_health SET consecutive_failures = 2 WHERE connection_key = %s", (TEST_KEY,))
    cur.execute("INSERT INTO shared.provider_credit_topup (connection_key, kind, amount_usd, created_by) "
                "VALUES (%s, 'topup', 12.5, 'admin:test') RETURNING id AS ok", (TEST_KEY,))
    topup_id = cur.fetchone()["ok"]
    cur.execute("SELECT amount_usd::float AS ok FROM shared.provider_credit_topup WHERE id = %s", (topup_id,))
    assert cur.fetchone()["ok"] == 12.5
    cur.execute("DELETE FROM shared.provider_credit_topup WHERE id = %s", (topup_id,))
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):    # entries are never edited in place
        cur.execute("UPDATE shared.provider_credit_topup SET amount_usd = 1 WHERE connection_key = %s", (TEST_KEY,))
    cur.execute("ROLLBACK TO SAVEPOINT s")
    cur.execute("ROLLBACK")


def test_343_moves_only_the_vendor_api_rows(cur):
    cur.execute("SELECT connection_key, kind, balance_source FROM shared.llm_provider_connection")
    rows = {r["connection_key"]: r for r in cur.fetchall()}
    for r in rows.values():
        if r["kind"] in ("anthropic", "openai"):
            assert r["balance_source"] == "topups_minus_spend", r["connection_key"]
    if "acc3" in rows:
        assert rows["acc3"]["balance_source"] == "cost_explorer"
    if "typesafe" in rows:
        assert rows["typesafe"]["balance_source"] == "topups_minus_spend"
    if "dataforseo" in rows:
        assert rows["dataforseo"]["balance_source"] == "provider_api"


def test_343_leaves_a_deliberate_value_alone(cur):
    cur.execute("SELECT connection_key FROM shared.llm_provider_connection WHERE kind = 'openai' LIMIT 1")
    row = cur.fetchone()
    if row is None:
        pytest.skip("no openai connection in this DB")
    key = row["connection_key"]
    cur.execute("SELECT balance_source AS b FROM shared.llm_provider_connection WHERE connection_key = %s", (key,))
    before = cur.fetchone()["b"]
    cur.execute("UPDATE shared.llm_provider_connection SET balance_source = 'provider_api' WHERE connection_key = %s",
                (key,))
    try:
        _run(cur, M343)
        cur.execute("SELECT balance_source AS ok FROM shared.llm_provider_connection WHERE connection_key = %s",
                    (key,))
        assert cur.fetchone()["ok"] == "provider_api"
    finally:                 # shared state goes back (the S263 lesson on integration tests)
        cur.execute("UPDATE shared.llm_provider_connection SET balance_source = %s WHERE connection_key = %s",
                    (before, key))
