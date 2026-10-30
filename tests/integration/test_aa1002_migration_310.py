"""AA-1002 (gateway slice S4) — migrations 310 (shared.llm_provider_health) and 311 (the admin edit grant) against real
Postgres, with the breaker's own SQL.

conftest applies every migration, 310 / 311 included. Each test re-runs both (idempotent: CREATE TABLE IF NOT EXISTS,
GRANT, registration ON CONFLICT DO NOTHING) and checks: the table and its constraints; the breaker's transition SQL
(row lock, then the pure transition) and snapshot read run as written; the availability read per connection runs;
health goes with its connection / model (CASCADE, no existing delete is blocked); cis_app writes the health table and
exactly the admin-editable connection columns.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.services import llm_availability  # noqa: E402
from api.services import llm_connections  # noqa: E402
from shared.llm_client import breaker  # noqa: E402

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M310 = os.path.join(MIGRATIONS, "310_llm_provider_health.sql")
M311 = os.path.join(MIGRATIONS, "311_llm_connection_admin_edit_grant.sql")
T0 = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
KEY = ("acc3", "sonnet-5")


def _run(c, path):
    c.execute(open(path, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _run(c, M310)
    _run(c, M311)
    c.execute("SELECT 1 FROM shared.llm_provider_connection WHERE connection_key = %s", (KEY[0],))
    if c.fetchone() is None:
        conn.close()
        pytest.skip("the 304 seed (connection acc3) is not in this DB")
    c.execute("SELECT 1 FROM shared.llm_model_catalog WHERE model_key = %s", (KEY[1],))
    if c.fetchone() is None:
        conn.close()
        pytest.skip("catalog row sonnet-5 is not in this DB")
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("RESET ROLE")
        c.execute("DELETE FROM shared.llm_provider_health")
        conn.close()


def _adb(fn):
    async def go():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


def test_table_and_registration(cur):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name = 'llm_provider_health'")
    assert {r["column_name"] for r in cur.fetchall()} == set(breaker._COLUMNS) | {"updated_at"}
    cur.execute("SELECT version FROM shared.schema_versions WHERE version IN ('310', '311') ORDER BY version")
    assert [r["version"] for r in cur.fetchall()] == ["310", "311"]


def test_the_breaker_transition_sql_runs_and_counts_under_the_row_lock(cur):
    for _ in range(4):
        _adb(lambda c: breaker.apply_transition(c, KEY, lambda h: breaker.after_failure(h, "other", "boom", T0)))
    before, after = _adb(lambda c: breaker.apply_transition(
        c, KEY, lambda h: breaker.after_failure(h, "other", "boom", T0)))
    assert before.consecutive_failures == 4 and after.consecutive_failures == 5
    rows = _adb(breaker.fetch_health)
    assert rows == [after] and after.circuit_open_until == T0 + timedelta(seconds=60)
    # the half-open probe lease: one caller gets it, the next does not
    half = T0 + timedelta(seconds=61)
    _, got = _adb(lambda c: breaker.apply_transition(c, KEY, lambda h: breaker.claimed(h, half)))
    _, again = _adb(lambda c: breaker.apply_transition(c, KEY, lambda h: breaker.claimed(h, half)))
    assert got is not None and again is None
    _adb(lambda c: breaker.apply_transition(c, KEY, lambda h: breaker.after_success(h, half)))
    (row,) = _adb(breaker.fetch_health)
    assert breaker.state_of(row, half) == breaker.CLOSED and row.last_ok_at == half


def test_the_availability_read_per_connection_runs(cur):
    _adb(lambda c: breaker.apply_transition(
        c, KEY, lambda h: breaker.after_failure(h, "access_denied", "Error 002", T0)))
    ready, conns = _adb(lambda c: llm_availability.read_connections(c, now=T0))
    by_key = {c["connection_key"]: c for c in conns}
    assert ready and by_key["acc3"]["status"] == "open" and by_key["acc3"]["models"][0]["model_key"] == "sonnet-5"
    assert by_key["anthropic-direct"]["status"] == "disabled"


def test_checks(cur):
    for sql in ("INSERT INTO shared.llm_provider_health (connection_key, model_key, last_error_class) "
                "VALUES ('acc3', 'sonnet-5', 'nonsense')",
                "INSERT INTO shared.llm_provider_health (connection_key, model_key, consecutive_failures) "
                "VALUES ('acc3', 'sonnet-5', -1)",
                "INSERT INTO shared.llm_provider_health (connection_key, model_key, last_error) "
                "VALUES ('acc3', 'sonnet-5', repeat('x', 301))"):
        cur.execute("BEGIN")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(sql)
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("INSERT INTO shared.llm_provider_health (connection_key, model_key) VALUES ('nope', 'sonnet-5')")
    cur.execute("ROLLBACK")


def test_health_goes_with_its_model_and_connection(cur):
    """CASCADE: no existing delete of a catalog row or a connection is ever blocked by health rows (lessons S259:
    an FK onto an existing table can turn an old test's DELETE red)."""
    cur.execute("BEGIN")
    cur.execute("INSERT INTO shared.llm_model_catalog (model_key, label, vendor, provider, api_style, "
                "bedrock_profile_ids, callable_via, supports_temperature, enabled, updated_at, updated_by) VALUES "
                "('zz-aa1002-s4', 'x', 'anthropic', 'anthropic', 'anthropic_messages', '{}', '{llm_client}', false, "
                "false, now(), 'test')")
    cur.execute("INSERT INTO shared.llm_provider_connection (connection_key, kind, auth_mode, label, data_scope, "
                "enabled, updated_by) VALUES ('zz-aa1002-s4c', 'anthropic', 'api_key', 't', 'vendor_direct', false, "
                "'test')")
    cur.execute("INSERT INTO shared.llm_provider_health (connection_key, model_key) VALUES "
                "('acc3', 'zz-aa1002-s4'), ('zz-aa1002-s4c', 'sonnet-5')")
    cur.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = 'zz-aa1002-s4'")
    cur.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = 'zz-aa1002-s4c'")
    cur.execute("SELECT count(*) AS n FROM shared.llm_provider_health")
    assert cur.fetchone()["n"] == 0
    cur.execute("ROLLBACK")


def test_cis_app_writes_health_and_the_editable_connection_columns(cur):
    cur.execute("SELECT column_name FROM information_schema.column_privileges WHERE table_schema = 'shared' "
                "AND table_name = 'llm_provider_connection' AND grantee = 'cis_app' AND privilege_type = 'UPDATE'")
    assert {r["column_name"] for r in cur.fetchall()} == set(llm_connections.EDITABLE) | {"updated_at", "updated_by"}
    cur.execute("BEGIN")
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")                   # Dev has it; rolled back below
    cur.execute("SET LOCAL SESSION AUTHORIZATION cis_app")
    cur.execute("INSERT INTO shared.llm_provider_health (connection_key, model_key) VALUES ('acc3', 'sonnet-5') "
                "ON CONFLICT (connection_key, model_key) DO NOTHING")
    cur.execute("SELECT 1 FROM shared.llm_provider_health WHERE connection_key = 'acc3' FOR UPDATE")
    cur.execute("UPDATE shared.llm_provider_health SET consecutive_failures = 1 WHERE connection_key = 'acc3'")
    cur.execute("UPDATE shared.llm_provider_connection SET rpm_limit = 60, max_concurrency = 4, timeout_s = 120, "
                "max_retries = 1, notes = 'n', monthly_budget_usd = 10, updated_at = now(), updated_by = 'admin:t' "
                "WHERE connection_key = 'anthropic-direct'")
    cur.execute("UPDATE shared.llm_provider_connection SET secret_name = 'aa-llm/dev/anthropic-direct-2', "
                "base_url = 'https://api.anthropic.com' WHERE connection_key = 'anthropic-direct'")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("UPDATE shared.llm_provider_connection SET role_name = 'x' WHERE connection_key = 'acc3'")
    cur.execute("ROLLBACK")
    cur.execute("SELECT has_table_privilege('tripplanner', 'shared.llm_provider_health', 'SELECT') AS tp")
    assert cur.fetchone()["tp"] is False
