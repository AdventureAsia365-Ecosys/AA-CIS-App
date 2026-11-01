"""AA-1026 I6, tenant part — migration 332 (the acc2 fallback, last in the route, for the four tenant judge / validate
stages that reach only acc3; held for the tech lead's yes, ADR 0009 decision 14) against real Postgres.

conftest applies every migration, 332 included. The main test loads the routes after migration 331
(``tests/unit/snapshots/aa1026_routes_after_331.json``), runs the file's body inside one transaction and compares every
row with ``tests/unit/snapshots/aa1026_routes_after_332.json``. Every test rolls back what it wrote.
"""
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
M332 = ROOT / "api" / "migrations" / "332_llm_tenant_judge_validate_acc2_fallback.sql"
BEFORE = ROOT / "tests" / "unit" / "snapshots" / "aa1026_routes_after_331.json"
EXPECTED = ROOT / "tests" / "unit" / "snapshots" / "aa1026_routes_after_332.json"
TENANT = ("t10_claims", "t2_brand_audit", "t9_faq_answer_fit", "t9_faq_filter")
COLUMNS = ("stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
           "shadow_sample_pct, allowed_scopes")


def _body() -> str:
    return M332.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("BEGIN")
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        conn.close()


def _smoke(c, stage: str, ok: bool, at: str = "2026-11-01 09:00+00", model: str = "nova-lite") -> None:
    """One contract smoke ledger row, as services/jobs/llm_contract_smoke_job.py writes it."""
    c.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, account, connection_key, "
              "created_at) VALUES ('adhoc_aa1026', 'validate', %s, %s::jsonb, 'acc2', 'acc2', %s::timestamptz)",
              (model, json.dumps({"contract_smoke": stage, "parser_ok": ok, "actor": "job:llm_contract_smoke"}), at))


def _load_routes(c, smoked: bool = True) -> None:
    """The routes after 331, and (``smoked``) a passing contract smoke of each tenant stage on Nova Lite (the order
    gate of the file needs them)."""
    if smoked:
        for stage in TENANT:
            _smoke(c, stage, True)
    c.execute("UPDATE shared.llm_provider_connection SET enabled = true WHERE connection_key IN ('acc2', 'acc3')")
    c.execute("UPDATE shared.llm_model_deployment SET enabled = true WHERE connection_key = 'acc2' "
              "AND model_key = 'nova-lite'")
    c.execute("DELETE FROM shared.llm_role_config")
    for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]:
        c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                  "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'test-aa1026-i6', %s)",
                  (r["stage"], r["role"], r["provider"], r["model_id"], r["account_route"], r["is_active"],
                   r["fallback_model_ids"], r["shadow_model_id"], r["shadow_sample_pct"], r["allowed_scopes"]))


def _routes(c) -> list[dict]:
    c.execute(f"SELECT {COLUMNS} FROM shared.llm_role_config ORDER BY stage")
    return [dict(r) for r in c.fetchall()]


def _expected() -> list[dict]:
    return json.loads(EXPECTED.read_text(encoding="utf-8"))["routes"]


def test_registered(cur):
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '332'")
    assert cur.fetchone()["ok"] == "332"


def test_the_routes_become_the_expected_routes_and_a_second_run_changes_nothing(cur):
    _load_routes(cur)
    cur.execute(_body())
    assert _routes(cur) == _expected()
    cur.execute("SELECT stage AS s FROM shared.llm_role_config WHERE updated_by = 'migration-332-aa1026' ORDER BY 1")
    assert [r["s"] for r in cur.fetchall()] == sorted(TENANT)
    cur.execute(_body())
    assert _routes(cur) == _expected()


def test_the_expected_routes_append_nova_lite_to_the_four_tenant_stages_only():
    before = {r["stage"]: r for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]}
    for r in _expected():
        b = before[r["stage"]]
        tail = ["nova-lite"] if r["stage"] in TENANT else []
        assert r == {**b, "fallback_model_ids": [*b["fallback_model_ids"], *tail]}, r["stage"]


def test_guard_refuses_without_331(cur):
    _load_routes(cur)
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '331'")
    with pytest.raises(psycopg2.errors.RaiseException, match="apply 331 first"):
        cur.execute(_body())


def test_guard_refuses_a_tripplanner_stage(cur):
    _load_routes(cur)
    cur.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) VALUES "
                "('tp_check', 'validate', 'claude', 'haiku-5-5', 'acc3', true, '{}', NULL, 0, 't', '{aws_only}')")
    body = _body().replace("'t9_faq_filter'];", "'tp_check'];", 1)
    assert body != _body()
    with pytest.raises(psycopg2.errors.RaiseException, match="TripPlanner stages changed"):
        cur.execute(body)


# ── the order gate: the contract smoke before the apply (review fix pass) ───────────────────────────────────────────

def test_the_order_gate_refuses_a_stage_whose_latest_smoke_failed(cur):
    _load_routes(cur)
    _smoke(cur, "t9_faq_filter", False, at="2026-11-01 10:00+00")
    with pytest.raises(psycopg2.errors.RaiseException,
                       match=r"no passing contract smoke .*t9_faq_filter \(nova-lite\)"):
        cur.execute(_body())


def test_the_order_gate_refuses_a_stage_with_no_smoke(cur):
    _load_routes(cur)
    cur.execute("DELETE FROM shared.llm_call_log WHERE stage = 'adhoc_aa1026' "
                "AND quality_signal ->> 'contract_smoke' = 't2_brand_audit'")
    with pytest.raises(psycopg2.errors.RaiseException, match=r"t2_brand_audit \(nova-lite\)"):
        cur.execute(_body())


def test_an_empty_ledger_is_a_replay_and_skips_the_gate(cur):
    _load_routes(cur, smoked=False)
    cur.execute("DELETE FROM shared.llm_call_log")
    cur.execute(_body())
    assert _routes(cur) == _expected()
