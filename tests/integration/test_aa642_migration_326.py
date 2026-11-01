"""AA-642 A1 — migration 326 (stage kill switch + declared degrade mode) against real Postgres.

conftest applies every migration, 326 included. Each test re-runs the file (idempotent, errors not swallowed): the
columns and their CHECKs, the three declared degrade modes, the ledger's new error_class, the cis_app column grant,
and the guard that stops the file when a row is inactive under the old meaning.
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

M326 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "326_llm_stage_switch_degrade_mode.sql")


def _run(c):
    c.execute(open(M326, encoding="utf-8").read())


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
        _run(c)
        conn.close()


def test_applies_twice_and_registers(cur):
    _run(cur)
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '326'")
    assert cur.fetchone()["ok"] == "326"


def test_the_declared_degrade_modes_and_no_other(cur):
    # 326 declares three; 335 (AA-1024 A2) declares queue_retry on the two segment research stages it creates.
    cur.execute("SELECT stage, degrade_mode, disabled_reason, is_active FROM shared.llm_role_config "
                "WHERE degrade_mode IS NOT NULL ORDER BY stage")
    rows = {r["stage"]: r for r in cur.fetchall()}
    assert {s: r["degrade_mode"] for s, r in rows.items()} == {
        "t2_generate": "queue_retry", "t9_write": "queue_retry", "tp_compose": "basic_narration",
        "segment_research": "queue_retry", "segment_research_batch": "queue_retry"}
    assert all(r["is_active"] and r["disabled_reason"] is None for r in rows.values())


def test_degrade_mode_and_reason_checks(cur):
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.llm_role_config SET degrade_mode = 'silently_skip' WHERE stage = 't9_write'")
    cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.llm_role_config SET disabled_reason = repeat('x', 501) WHERE stage = 't9_write'")
    cur.execute("ROLLBACK")


def test_the_ledger_accepts_disabled_and_still_refuses_an_unknown_class(cur):
    cols = ("stage, role, model, quality_signal, status, error_class, cost_usd")
    cur.execute("BEGIN")
    cur.execute(f"INSERT INTO shared.llm_call_log (id, {cols}) VALUES (gen_random_uuid(), 'zz_test', 'writer', "
                "'m', '{}'::jsonb, 'refused', 'disabled', 0)")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(f"INSERT INTO shared.llm_call_log (id, {cols}) VALUES (gen_random_uuid(), 'zz_test', 'writer', "
                    "'m', '{}'::jsonb, 'refused', 'switched_off', 0)")
    cur.execute("ROLLBACK")


def test_constraint_is_validated(cur):
    cur.execute("SELECT convalidated AS ok FROM pg_constraint WHERE conname = 'llm_call_log_error_class_check' "
                "AND conrelid = 'shared.llm_call_log'::regclass")
    assert cur.fetchone()["ok"] is True


def test_cis_app_may_switch_a_stage_but_not_declare_a_degrade_mode(cur):
    cur.execute("SELECT has_column_privilege('cis_app', 'shared.llm_role_config', 'disabled_reason', 'UPDATE') AS r, "
                "has_column_privilege('cis_app', 'shared.llm_role_config', 'is_active', 'UPDATE') AS a, "
                "has_column_privilege('cis_app', 'shared.llm_role_config', 'degrade_mode', 'UPDATE') AS d")
    r = cur.fetchone()
    # is_active UPDATE comes from the Dev role grants (X1), which the CI database does not carry (Dev read 01/11:
    # cis_app has UPDATE on is_active); 326 itself grants disabled_reason and never degrade_mode.
    assert r["r"] is True and r["d"] is False


def test_an_inactive_row_under_the_old_meaning_stops_the_first_apply(cur):
    cur.execute("BEGIN")
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '326'")
    cur.execute("UPDATE shared.llm_role_config SET is_active = false WHERE stage = 'a0_column_map'")
    with pytest.raises(psycopg2.errors.RaiseException, match="a0_column_map"):
        _run(cur)
    cur.execute("ROLLBACK")


def test_a_rerun_after_an_admin_switched_a_stage_off_passes(cur):
    cur.execute("BEGIN")
    cur.execute("UPDATE shared.llm_role_config SET is_active = false, disabled_reason = 'drill' "
                "WHERE stage = 't9_write'")
    cur.execute("SAVEPOINT s")
    cur.execute(open(M326, encoding="utf-8").read().replace("BEGIN;", "", 1).replace("COMMIT;", ""))
    cur.execute("SELECT is_active AS ok FROM shared.llm_role_config WHERE stage = 't9_write'")
    assert cur.fetchone()["ok"] is False
    cur.execute("ROLLBACK")
