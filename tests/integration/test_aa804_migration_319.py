"""AA-804 (ledger v2) — migration 319 against real Postgres.

conftest applies every migration, 319 included. Each test runs the file again directly (errors not swallowed, lesson
S231): it is idempotent and registered, the new columns exist, the CHECKs refuse a bad status / error class / latency /
attempt number, the widened role CHECK takes the ADR 0009 roles and still refuses an unknown one, a TripPlanner-style
INSERT (its own column list, no v2 column) still lands, and the indexes exist. Every test removes its own rows.
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

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "319_llm_ledger_v2.sql")
STAGE = "zz_aa804_v2"

LLM_COLUMNS = ("app", "status", "error_class", "latency_ms", "connection_key", "deployment_wire_id", "attempt_no",
               "request_id", "tour_id", "reasoning_tokens", "effort", "is_shadow", "is_experiment", "prompt_hash",
               "prompt_version", "units", "unit_kind")


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute(open(MIGRATION, encoding="utf-8").read())
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.llm_call_log WHERE stage = %s", (STAGE,))
        c.execute("DELETE FROM shared.dfs_call_log WHERE endpoint = %s", (STAGE,))
        conn.close()


def _columns(cur, schema: str, table: str) -> dict:
    cur.execute("SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s", (schema, table))
    return {r["column_name"]: r for r in cur.fetchall()}


def test_idempotent_and_registered(cur):
    cur.execute(open(MIGRATION, encoding="utf-8").read())          # a second run changes nothing
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '319'")
    assert cur.fetchone()["ok"] == "319"


def test_the_ledger_columns_exist_nullable_without_default(cur):
    cols = _columns(cur, "shared", "llm_call_log")
    for name in LLM_COLUMNS:
        assert name in cols, name
        assert cols[name]["is_nullable"] == "YES" and cols[name]["column_default"] is None, name
    assert cols["tour_id"]["data_type"] == "uuid" and cols["is_shadow"]["data_type"] == "boolean"
    dfs = _columns(cur, "shared", "dfs_call_log")
    assert {"status", "latency_ms", "error", "stage", "app"} <= set(dfs)
    geo = _columns(cur, "geo", "geocode_call")
    assert geo["tenant_id"]["data_type"] == "uuid" and "failed_requests" in geo
    daily = _columns(cur, "shared", "llm_call_daily")
    for name in ("refused_calls", "error_calls", "latency_ms_sum", "latency_ms_count"):
        assert daily[name]["is_nullable"] == "NO" and daily[name]["column_default"] == "0", name


@pytest.mark.parametrize("column, value", [
    ("status", "failed"), ("status", "OK"), ("error_class", "timeout"), ("error_class", "request"),
    ("latency_ms", -1), ("attempt_no", 0),
])
def test_checks_refuse_bad_values(cur, column, value):
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(f"INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, {column}) "
                    "VALUES (%s, 'judge', 'm', '{}', %s)", (STAGE, value))


@pytest.mark.parametrize("status, error_class", [
    ("ok", None), ("refused", "credential"), ("refused", "budget"), ("error", "deadline"),
    ("billed_unusable", None), ("truncated", "truncated"), (None, None),
])
def test_checks_take_every_documented_value(cur, status, error_class):
    cur.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, status, error_class, "
                "latency_ms, attempt_no, cost_usd) VALUES (%s, 'writer', 'm', '{}', %s, %s, 0, 1, 0)",
                (STAGE, status, error_class))


@pytest.mark.parametrize("role", ["writer", "judge", "validate", "embed", "decide", "data", "guard"])
def test_the_role_check_takes_the_adr_0009_roles(cur, role):
    cur.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal) VALUES (%s, %s, 'm', '{}')",
                (STAGE, role))


def test_the_role_check_still_refuses_an_unknown_role(cur):
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal) "
                    "VALUES (%s, 'planner', 'm', '{}')", (STAGE,))


def test_a_tripplanner_style_insert_still_lands(cur):
    """TripPlanner names its columns (no v2 column): the row lands with NULL there, app read from quality_signal."""
    cur.execute("INSERT INTO shared.llm_call_log (stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal, "
                "provider) VALUES (%s, 'writer', 'haiku-4-5', 10, 5, 0.0001, '{\"app\": \"tripplanner\"}', "
                "'bedrock-satellite')", (STAGE,))
    cur.execute("SELECT coalesce(app, quality_signal->>'app', 'cis') AS app, status FROM shared.llm_call_log "
                "WHERE stage = %s", (STAGE,))
    row = cur.fetchone()
    assert (row["app"], row["status"]) == ("tripplanner", None)


def test_a_failed_dfs_call_row_lands(cur):
    cur.execute("INSERT INTO shared.dfs_call_log (endpoint, cost_usd, cache_hit, fetched_live, status, latency_ms, "
                "error, stage, app) VALUES (%s, 0, false, false, 'refused', NULL, 'budget', 's1_rewrite', 'cis')",
                (STAGE,))
    cur.execute("SELECT status AS ok FROM shared.dfs_call_log WHERE endpoint = %s", (STAGE,))
    assert cur.fetchone()["ok"] == "refused"


def test_the_indexes_exist(cur):
    cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE (schemaname, indexname) IN "
                "(('shared', 'llm_call_log_tour_idx'), ('shared', 'llm_call_log_status_created_idx'), "
                "('shared', 'decision_log_job_idx'))")
    defs = {r["indexname"]: r["indexdef"] for r in cur.fetchall()}
    assert set(defs) == {"llm_call_log_tour_idx", "llm_call_log_status_created_idx", "decision_log_job_idx"}
    assert "WHERE (tour_id IS NOT NULL)" in defs["llm_call_log_tour_idx"]
    assert "WHERE (status <> 'ok'::text)" in defs["llm_call_log_status_created_idx"]
    assert "WHERE (job_id IS NOT NULL)" in defs["decision_log_job_idx"]
