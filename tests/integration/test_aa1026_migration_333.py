"""AA-1026 I6, "AWS first, outside last" — migration 333 (an acc2 pick right before the OpenAI API hop of every judge /
validate route that ends on it) against real Postgres.

conftest applies every migration, 333 included. The main test loads the routes after migration 332
(``tests/unit/snapshots/aa1026_routes_after_332.json``), runs the file's body inside one transaction and compares every
row with ``tests/unit/snapshots/aa1026_routes_after_333.json``. A second test replays 331 -> 332 -> 333 from the
routes after 327. Every test rolls back what it wrote.
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
MIGRATIONS = ROOT / "api" / "migrations"
M331 = MIGRATIONS / "331_llm_judge_validate_acc2_fallback.sql"
M332 = MIGRATIONS / "332_llm_tenant_judge_validate_acc2_fallback.sql"
M333 = MIGRATIONS / "333_llm_judge_validate_acc2_before_openai.sql"
SNAPSHOTS = ROOT / "tests" / "unit" / "snapshots"
AFTER_327 = SNAPSHOTS / "aa1026_routes_after_327.json"
BEFORE = SNAPSHOTS / "aa1026_routes_after_332.json"
EXPECTED = SNAPSHOTS / "aa1026_routes_after_333.json"
PICKS = {"s1_judge": "nova-lite", "t2_judge": "nova-lite", "t7_debate_judge": "nova-lite", "t10_judge": "nova-lite",
         "writing_memory_eval": "nova-lite", "writing_memory_mine": "nova-lite", "a3_place_dedupe": "llama4-scout"}
SMOKED_331_332 = {"s1_brand_audit": "nova-lite", "s1_source_facts": "nova-lite", "a3_atomize": "nova-lite",
                  "a3_place_card": "nova-lite", "a3_place_card_check": "llama4-scout", "a3_place_days": "nova-lite",
                  "a3_place_locate": "nova-lite", "a3_place_dedupe_check": "nova-lite", "t2_brand_audit": "nova-lite",
                  "t10_claims": "nova-lite", "t9_faq_answer_fit": "nova-lite", "t9_faq_filter": "nova-lite"}
OPENAI_HOP = "gpt-6-luna-openai"
COLUMNS = ("stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
           "shadow_sample_pct, allowed_scopes")
ACTOR = "migration-333-aa1026"


def _body(path: Path = M333) -> str:
    return path.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


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


def _smoke(c, stage: str, ok: bool, model: str, at: str = "2026-11-01 09:00+00") -> None:
    """One contract smoke ledger row, as services/jobs/llm_contract_smoke_job.py writes it."""
    c.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, account, connection_key, "
              "created_at) VALUES ('adhoc_aa1026', 'judge', %s, %s::jsonb, 'acc2', 'acc2', %s::timestamptz)",
              (model, json.dumps({"contract_smoke": stage, "parser_ok": ok, "actor": "job:llm_contract_smoke"}), at))


def _load_routes(c, path: Path = BEFORE, smoked: dict = PICKS) -> None:
    """The routes of ``path``, the registry the files expect (acc2 / acc3 / openai-direct on, the acc2 picks
    deployed) and a passing contract smoke of each stage in ``smoked``."""
    for stage, model in smoked.items():
        _smoke(c, stage, True, model)
    c.execute("UPDATE shared.llm_provider_connection SET enabled = true "
              "WHERE connection_key IN ('acc2', 'acc3', 'openai-direct')")
    c.execute("UPDATE shared.llm_model_deployment SET enabled = true WHERE connection_key = 'acc2' "
              "AND model_key IN ('nova-lite', 'llama4-scout')")
    c.execute("DELETE FROM shared.llm_role_config")
    for r in json.loads(path.read_text(encoding="utf-8"))["routes"]:
        c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                  "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'test-aa1026-333', %s)",
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


def _adjudicate_row(c) -> None:
    """t10_adjudicate as migration 308 seeds it (not in the 29/10 Dev snapshot; on Dev with the vendor_direct scope)."""
    c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
              "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) VALUES "
              "('t10_adjudicate', 'judge', 'openai', 'gpt-6-luna', 'acc3', true, '{gpt-5.6-luna,gpt-6-luna-openai}', "
              "NULL, 0, 'migration-308-aa1000', '{aws_only,vendor_direct}')")
    _smoke(c, "t10_adjudicate", True, "nova-lite")


def test_registered(cur):
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '333'")
    assert cur.fetchone()["ok"] == "333"


def test_the_routes_become_the_expected_routes_and_a_second_run_changes_nothing(cur):
    _load_routes(cur)
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert _changed(cur) == sorted(PICKS)
    cur.execute("UPDATE shared.llm_role_config SET updated_by = 'test-aa1026-333-done' WHERE updated_by = %s",
                (ACTOR,))
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert _changed(cur) == []                                              # the re-apply finds every pick in place


def test_the_expected_routes_put_the_pick_right_before_the_openai_api_hop_only():
    before = {r["stage"]: r for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]}
    for r in _expected():
        b = before[r["stage"]]
        if r["stage"] in PICKS:
            f = b["fallback_model_ids"]
            assert f[-1] == OPENAI_HOP and OPENAI_HOP not in f[:-1]
            assert r == {**b, "fallback_model_ids": [*f[:-1], PICKS[r["stage"]], OPENAI_HOP]}, r["stage"]
            assert r["role"] in ("judge", "validate")
        else:
            assert r == b, r["stage"]
            assert OPENAI_HOP not in r["fallback_model_ids"] or r["role"] not in ("judge", "validate"), r["stage"]


def test_331_332_333_replay_from_the_routes_after_327(cur):
    _load_routes(cur, AFTER_327, {**SMOKED_331_332, **PICKS})
    for path in (M331, M332, M333):
        cur.execute(_body(path))
    assert _routes(cur) == _expected()


def test_t10_adjudicate_gets_its_pick_before_the_openai_api_hop(cur):
    _load_routes(cur)
    _adjudicate_row(cur)
    cur.execute(_body())
    cur.execute("SELECT model_id AS m, fallback_model_ids AS f, allowed_scopes AS s FROM shared.llm_role_config "
                "WHERE stage = 't10_adjudicate'")
    row = cur.fetchone()
    assert (row["m"], row["f"], row["s"]) == ("gpt-6-luna", ["gpt-5.6-luna", "nova-lite", OPENAI_HOP],
                                              ["aws_only", "vendor_direct"])
    assert _changed(cur) == sorted([*PICKS, "t10_adjudicate"])
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = 't10_adjudicate'")
    assert _routes(cur) == _expected()


def test_a_pick_found_after_the_openai_hop_moves_before_it(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = fallback_model_ids || 'nova-lite'::text "
                "WHERE stage = 't10_judge'")                                # an admin appended the acc2 model last
    cur.execute(_body())
    assert _routes(cur) == _expected()


def test_a_route_not_ending_on_the_openai_api_is_left_as_it_is(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = fallback_model_ids || 'haiku-5-5'::text "
                "WHERE stage = 's1_judge'")
    cur.execute("SELECT fallback_model_ids AS f FROM shared.llm_role_config WHERE stage = 's1_judge'")
    kept = cur.fetchone()["f"]
    cur.execute(_body())
    cur.execute("SELECT fallback_model_ids AS f FROM shared.llm_role_config WHERE stage = 's1_judge'")
    assert cur.fetchone()["f"] == kept
    assert "s1_judge" not in _changed(cur)


def test_guard_refuses_without_332(cur):
    _load_routes(cur)
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '332'")
    with pytest.raises(psycopg2.errors.RaiseException, match="apply 332 first"):
        cur.execute(_body())


def test_guard_refuses_a_dedupe_pick_of_the_check_stage_maker(cur):
    _load_routes(cur)
    body = _body().replace("'llama4-scout', 'nova-lite'];", "'nova-lite', 'nova-lite'];", 1)
    assert body != _body()
    _smoke(cur, "a3_place_dedupe", True, "nova-lite")
    with pytest.raises(psycopg2.errors.RaiseException, match="share a maker"):
        cur.execute(body)


def test_guard_refuses_a_tripplanner_stage(cur):
    _load_routes(cur)
    cur.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) VALUES "
                "('tp_check', 'judge', 'openai', 'gpt-5.6-luna', 'acc3', true, '{gpt-6-luna-openai}', NULL, 0, 't', "
                "'{aws_only,vendor_direct}')")
    body = _body().replace("'t10_adjudicate'];", "'tp_check'];", 1)
    assert body != _body()
    _smoke(cur, "tp_check", True, "nova-lite")
    with pytest.raises(psycopg2.errors.RaiseException, match="TripPlanner stages changed"):
        cur.execute(body)


def test_guard_refuses_an_openai_api_model_as_a_pick(cur):
    _load_routes(cur)
    body = _body().replace("'llama4-scout', 'nova-lite'];", "'llama4-scout', 'gpt-6-luna-openai'];", 1)
    assert body != _body()
    with pytest.raises(psycopg2.errors.RaiseException, match="picks without an enabled non-Anthropic Converse"):
        cur.execute(body)


# ── the order gate: the contract smoke before the apply ─────────────────────────────────────────────────────────────

def test_the_order_gate_refuses_a_stage_whose_latest_smoke_failed(cur):
    _load_routes(cur)
    _smoke(cur, "t10_judge", False, "nova-lite", at="2026-11-01 10:00+00")
    with pytest.raises(psycopg2.errors.RaiseException, match=r"no passing contract smoke .*t10_judge \(nova-lite\)"):
        cur.execute(_body())


def test_the_order_gate_refuses_a_stage_with_no_smoke_on_its_pick(cur):
    _load_routes(cur)
    cur.execute("DELETE FROM shared.llm_call_log WHERE stage = 'adhoc_aa1026' "
                "AND quality_signal ->> 'contract_smoke' = 'a3_place_dedupe'")
    _smoke(cur, "a3_place_dedupe", True, "nova-lite")                          # a smoke on another model counts not
    with pytest.raises(psycopg2.errors.RaiseException, match=r"a3_place_dedupe \(llama4-scout\)"):
        cur.execute(_body())


def test_an_empty_ledger_is_a_replay_and_skips_the_gate(cur):
    _load_routes(cur, smoked={})
    cur.execute("DELETE FROM shared.llm_call_log")
    cur.execute(_body())
    assert _routes(cur) == _expected()
