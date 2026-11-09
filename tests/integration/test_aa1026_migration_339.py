"""AA-1026 I6, "AWS first, outside last" — migration 339 (llama4-maverick on acc2 right before the OpenAI API hop of
a3_place_dedupe) against real Postgres.

conftest applies every migration, 339 included. The main test loads the routes after migration 333
(``tests/unit/snapshots/aa1026_routes_after_333.json``), runs the file's body inside one transaction and compares every
row with ``tests/unit/snapshots/aa1026_routes_after_339.json``. Every test rolls back what it wrote.
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
M339 = ROOT / "api" / "migrations" / "339_llm_place_dedupe_acc2_maverick.sql"
SNAPSHOTS = ROOT / "tests" / "unit" / "snapshots"
BEFORE = SNAPSHOTS / "aa1026_routes_after_333.json"
EXPECTED = SNAPSHOTS / "aa1026_routes_after_339.json"
STAGE = "a3_place_dedupe"
PICK = "llama4-maverick"
OPENAI_HOP = "gpt-6-luna-openai"
COLUMNS = ("stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
           "shadow_sample_pct, allowed_scopes")
ACTOR = "migration-339-aa1026"


def _body() -> str:
    return M339.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


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


def _smoke(c, ok: bool, model: str = PICK, at: str = "2026-11-02 03:00+00") -> None:
    """One contract smoke ledger row, as services/jobs/llm_contract_smoke_job.py writes it."""
    c.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, account, connection_key, "
              "created_at) VALUES ('adhoc_aa1026', 'validate', %s, %s::jsonb, 'acc2', 'acc2', %s::timestamptz)",
              (model, json.dumps({"contract_smoke": STAGE, "parser_ok": ok, "actor": "job:llm_contract_smoke"}), at))


def _load_routes(c, smoked: bool = True) -> None:
    """The routes after 333, the registry the file expects (acc2 / acc3 / openai-direct on, Maverick deployed on
    acc2, Maverick's catalog row as 337 leaves it) and, unless ``smoked`` is False, a passing smoke on the pick."""
    if smoked:
        _smoke(c, True)
    c.execute("UPDATE shared.llm_model_catalog SET capabilities = "
              "'{\"forced_tool_choice\": false, \"structured_output\": \"prompt\"}'::jsonb "
              "WHERE model_key = %s", (PICK,))
    c.execute("UPDATE shared.llm_provider_connection SET enabled = true "
              "WHERE connection_key IN ('acc2', 'acc3', 'openai-direct')")
    c.execute("UPDATE shared.llm_model_deployment SET enabled = true WHERE connection_key = 'acc2' "
              "AND model_key IN ('nova-lite', %s)", (PICK,))
    c.execute("DELETE FROM shared.llm_role_config")
    for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]:
        c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                  "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'test-aa1026-339', %s)",
                  (r["stage"], r["role"], r["provider"], r["model_id"], r["account_route"], r["is_active"],
                   r["fallback_model_ids"], r["shadow_model_id"], r["shadow_sample_pct"], r["allowed_scopes"]))


def _routes(c) -> list[dict]:
    c.execute(f"SELECT {COLUMNS} FROM shared.llm_role_config ORDER BY stage")
    return [dict(r) for r in c.fetchall()]


def _expected() -> list[dict]:
    return json.loads(EXPECTED.read_text(encoding="utf-8"))["routes"]


def _changed(c) -> list[str]:
    c.execute("SELECT stage AS s FROM shared.llm_role_config WHERE updated_by = %s ORDER BY 1", (ACTOR,))
    return [r["s"] for r in c.fetchall()]


def _dedupe(c) -> list:
    c.execute("SELECT fallback_model_ids AS f FROM shared.llm_role_config WHERE stage = %s", (STAGE,))
    return list(c.fetchone()["f"])


def test_registered(cur):
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '339'")
    assert cur.fetchone()["ok"] == "339"


def test_the_routes_become_the_expected_routes_and_a_second_run_changes_nothing(cur):
    _load_routes(cur)
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert _changed(cur) == [STAGE]
    cur.execute("UPDATE shared.llm_role_config SET updated_by = 'test-aa1026-339-done' WHERE updated_by = %s",
                (ACTOR,))
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert _changed(cur) == []                                              # the re-apply finds the pick in place


def test_the_expected_routes_change_only_a3_place_dedupe():
    before = {r["stage"]: r for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]}
    for r in _expected():
        b = before[r["stage"]]
        if r["stage"] == STAGE:
            assert b["fallback_model_ids"] == [OPENAI_HOP]
            assert r == {**b, "fallback_model_ids": [PICK, OPENAI_HOP]}
        else:
            assert r == b, r["stage"]


def test_a_pick_found_after_the_openai_hop_moves_before_it(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = fallback_model_ids || %s::text "
                "WHERE stage = %s", (PICK, STAGE))                          # an admin appended the acc2 model last
    cur.execute(_body())
    assert _dedupe(cur) == [PICK, OPENAI_HOP]


def test_a_route_not_ending_on_the_openai_api_is_left_as_it_is(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = '{gpt-6-luna-openai,nova-2-lite}' "
                "WHERE stage = %s", (STAGE,))
    cur.execute(_body())
    assert _dedupe(cur) == [OPENAI_HOP, "nova-2-lite"]
    assert _changed(cur) == []


def test_guard_refuses_without_337(cur):
    _load_routes(cur)
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '337'")
    with pytest.raises(psycopg2.errors.RaiseException, match="apply 337 first"):
        cur.execute(_body())


def test_guard_refuses_a_pick_with_no_structured_mode(cur):
    """Maverick refuses toolChoice.tool (live, S263); without 337's prompt mode it could not answer the verdict."""
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = '{\"forced_tool_choice\": false}'::jsonb "
                "WHERE model_key = %s", (PICK,))
    with pytest.raises(psycopg2.errors.RaiseException, match="states no other structured_output mode"):
        cur.execute(_body())


def test_a_pick_that_takes_a_forced_tool_choice_passes(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = '{}'::jsonb WHERE model_key = %s", (PICK,))
    cur.execute(_body())
    assert _dedupe(cur) == [PICK, OPENAI_HOP]


def test_guard_refuses_a_pick_without_an_acc2_deployment(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_deployment SET enabled = false WHERE connection_key = 'acc2' "
                "AND model_key = %s", (PICK,))
    with pytest.raises(psycopg2.errors.RaiseException, match="no enabled non-Anthropic Converse catalog row"):
        cur.execute(_body())


def test_guard_refuses_when_the_place_pair_would_share_a_maker(cur):
    """The check stage gains a Meta fallback: dedupe {OpenAI, Meta} and check {Anthropic, Amazon, Meta} share Meta."""
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_deployment SET enabled = true WHERE connection_key = 'acc2' "
                "AND model_key = 'llama4-scout'")
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = fallback_model_ids || 'llama4-scout'::text "
                "WHERE stage = 'a3_place_dedupe_check'")
    with pytest.raises(psycopg2.errors.RaiseException, match="would share a maker: meta"):
        cur.execute(_body())


# ── the order gate: the contract smoke before the apply ─────────────────────────────────────────────────────────────

def test_the_order_gate_refuses_when_the_latest_smoke_failed(cur):
    _load_routes(cur)
    _smoke(cur, False, at="2026-11-02 04:00+00")
    with pytest.raises(psycopg2.errors.RaiseException, match="no passing contract smoke of a3_place_dedupe"):
        cur.execute(_body())


def test_the_order_gate_refuses_a_smoke_on_another_model(cur):
    _load_routes(cur, smoked=False)
    _smoke(cur, True, model="llama4-scout")
    with pytest.raises(psycopg2.errors.RaiseException, match="no passing contract smoke of a3_place_dedupe"):
        cur.execute(_body())


def test_an_empty_ledger_is_a_replay_and_skips_the_gate(cur):
    _load_routes(cur, smoked=False)
    cur.execute("DELETE FROM shared.llm_call_log")
    cur.execute(_body())
    assert _routes(cur) == _expected()
