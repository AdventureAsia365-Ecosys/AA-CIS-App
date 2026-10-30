"""AA-1002 (gateway slice S10) — migration 317 (shared.llm_connection_cost_policy, shared.llm_cost_reconcile) against
real Postgres, with the code's own SQL.

conftest applies every migration, 317 included. Each test re-runs it (idempotent: CREATE TABLE / INDEX IF NOT EXISTS,
GRANT, registration ON CONFLICT DO NOTHING) and checks: the tables, CHECKs and FKs (ON DELETE CASCADE with the
connection); the policy read, the month spend read, the reconcile upsert / prune and the admin read run as written;
cis_app may write both tables.
"""
import asyncio
import json
import os
import sys
from datetime import date

import asyncpg
import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.llm_cost import reconcile  # noqa: E402
from shared import cost_guard  # noqa: E402

M316 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "317_llm_connection_cost_policy_reconcile.sql")
KEY = "openai-direct"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute(open(M316, encoding="utf-8").read())
    c.execute("SELECT 1 FROM shared.llm_provider_connection WHERE connection_key = %s", (KEY,))
    if c.fetchone() is None:
        conn.close()
        pytest.skip("the 304 seed (connection openai-direct) is not in this DB")
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("RESET ROLE")
        c.execute("DELETE FROM shared.llm_cost_reconcile WHERE connection_key = %s", (KEY,))
        c.execute("DELETE FROM shared.llm_connection_cost_policy WHERE connection_key = %s", (KEY,))
        conn.close()


def _adb(fn):
    async def go():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


def test_tables_and_registration(cur):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name = 'llm_connection_cost_policy'")
    assert {r["column_name"] for r in cur.fetchall()} == {
        "connection_key", "alert_pct", "hard_stop", "usage_secret_name", "usage_scope_id", "notes", "created_at",
        "updated_at", "updated_by"}
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name = 'llm_cost_reconcile'")
    assert {r["column_name"] for r in cur.fetchall()} == {
        "connection_key", "day", "source", "status", "logged_usd", "logged_calls", "provider_usd", "diff_usd",
        "diff_pct", "details", "job_id", "checked_at"}
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '317'")
    assert cur.fetchone() is not None


@pytest.mark.parametrize("sql, args", [
    ("INSERT INTO shared.llm_connection_cost_policy (connection_key, alert_pct, updated_by) VALUES (%s, 0, 't')",
     (KEY,)),
    ("INSERT INTO shared.llm_connection_cost_policy (connection_key, usage_secret_name, updated_by) "
     "VALUES (%s, 'aa-cis/dev/rds', 't')", (KEY,)),
    ("INSERT INTO shared.llm_connection_cost_policy (connection_key, usage_scope_id, updated_by) "
     "VALUES (%s, 'bad id', 't')", (KEY,)),
    ("INSERT INTO shared.llm_cost_reconcile (connection_key, day, source, status) "
     "VALUES (%s, '2026-10-29', 'console', 'ok')", (KEY,)),
    ("INSERT INTO shared.llm_cost_reconcile (connection_key, day, source, status) "
     "VALUES (%s, '2026-10-29', 'none', 'fine')", (KEY,)),
    ("INSERT INTO shared.llm_cost_reconcile (connection_key, day, source, status, provider_usd) "
     "VALUES (%s, '2026-10-29', 'none', 'ok', -1)", (KEY,)),
])
def test_checks_refuse_bad_rows(cur, sql, args):
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(sql, args)


def test_unknown_connection_is_refused(cur):
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("INSERT INTO shared.llm_cost_reconcile (connection_key, day, source, status) "
                    "VALUES ('no-such-connection', '2026-10-29', 'none', 'ok')")


def test_the_code_sql_runs_as_cis_app(cur):
    cur.execute("SET ROLE cis_app")
    cur.execute("INSERT INTO shared.llm_connection_cost_policy (connection_key, alert_pct, hard_stop, "
                "usage_secret_name, usage_scope_id, updated_by) VALUES (%s, 70, false, 'aa-llm/dev/openai-usage', "
                "'proj_1', 'admin:t') ON CONFLICT (connection_key) DO UPDATE SET alert_pct = EXCLUDED.alert_pct",
                (KEY,))
    cur.execute("RESET ROLE")

    async def go(conn):
        policies = await cost_guard.read_cost_policies(conn)
        spend = await cost_guard.connection_month_spend(conn, [KEY])
        await conn.execute("SET ROLE cis_app")
        await conn.execute(reconcile._UPSERT_SQL, KEY, date(2026, 10, 29), "openai_costs", "mismatch", 1.0, 3, 1.5,
                           -0.5, -33.33, json.dumps({"usage_scope_id": "proj_1"}), None)
        await conn.execute(reconcile._UPSERT_SQL, KEY, date(2026, 10, 29), "openai_costs", "ok", 1.5, 4, 1.5,
                           0.0, 0.0, "{}", None)
        recent = await reconcile.read_recent(conn, days=30, today=date(2026, 10, 30))
        logged = await reconcile.logged_costs(conn, [KEY], date(2026, 10, 28), date(2026, 10, 30))
        await conn.execute(reconcile._PRUNE_SQL, date(2026, 10, 30))
        left = await conn.fetchval("SELECT count(*) FROM shared.llm_cost_reconcile WHERE connection_key = $1", KEY)
        await conn.execute("RESET ROLE")
        return policies, spend, recent, logged, left
    policies, spend, recent, logged, left = _adb(go)
    assert policies[KEY].alert_pct == 70 and policies[KEY].hard_stop is False
    assert isinstance(spend, dict) and isinstance(logged, dict)
    assert [r["status"] for r in recent[KEY]] == ["ok"] and recent[KEY][0]["logged_calls"] == 4   # upsert replaced it
    assert left == 0                                                                             # pruned


def test_rows_go_with_their_connection(cur):
    cur.execute("BEGIN")
    cur.execute("INSERT INTO shared.llm_provider_connection (connection_key, kind, auth_mode, label, data_scope, "
                "updated_by) VALUES ('s10-tmp', 'mock', 'none', 'tmp', 'aws_only', 't')")
    cur.execute("INSERT INTO shared.llm_connection_cost_policy (connection_key, updated_by) VALUES ('s10-tmp', 't')")
    cur.execute("INSERT INTO shared.llm_cost_reconcile (connection_key, day, source, status) "
                "VALUES ('s10-tmp', '2026-10-29', 'none', 'unsupported')")
    cur.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = 's10-tmp'")
    cur.execute("SELECT (SELECT count(*) FROM shared.llm_connection_cost_policy WHERE connection_key = 's10-tmp') + "
                "(SELECT count(*) FROM shared.llm_cost_reconcile WHERE connection_key = 's10-tmp') AS n")
    assert cur.fetchone()["n"] == 0
    cur.execute("ROLLBACK")
