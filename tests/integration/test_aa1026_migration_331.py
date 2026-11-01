"""AA-1026 I6 — migration 331 (an acc2 Bedrock fallback, last in the route, for every platform judge / validate stage
that reaches only acc3) against real Postgres.

conftest applies every migration, 331 included. The main test replaces the route table with the 37 Dev routes after
migration 327 (``tests/unit/snapshots/aa1026_routes_after_327.json``), runs the file's body inside one transaction
(errors not swallowed) and compares every row with ``tests/unit/snapshots/aa1026_routes_after_331.json``, the routes
the unit test ``test_aa1026_acc2_fallback_routes.py`` walks through the gateway. Every test rolls back what it wrote.
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
M331 = ROOT / "api" / "migrations" / "331_llm_judge_validate_acc2_fallback.sql"
BEFORE = ROOT / "tests" / "unit" / "snapshots" / "aa1026_routes_after_327.json"
EXPECTED = ROOT / "tests" / "unit" / "snapshots" / "aa1026_routes_after_331.json"
PICKS = {"s1_brand_audit": "nova-lite", "s1_source_facts": "nova-lite", "a3_atomize": "nova-lite",
         "a3_place_card": "nova-lite", "a3_place_card_check": "llama4-scout", "a3_place_days": "nova-lite",
         "a3_place_locate": "nova-lite", "a3_place_dedupe_check": "nova-lite"}
TENANT = ("t2_brand_audit", "t10_claims", "t9_faq_answer_fit", "t9_faq_filter")
COLUMNS = ("stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
           "shadow_sample_pct, allowed_scopes")


def _body() -> str:
    """The file without its own BEGIN / COMMIT, so a test runs it inside the test's transaction."""
    return M331.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


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


def _seed_registry(c) -> None:
    """The registry rows the file reads, as on Dev (other modules may have switched them)."""
    c.execute("UPDATE shared.llm_provider_connection SET enabled = true WHERE connection_key IN ('acc2', 'acc3')")
    c.execute("UPDATE shared.llm_model_deployment SET enabled = true WHERE connection_key = 'acc2' "
              "AND model_key IN ('nova-lite', 'llama4-scout')")


def _load_routes(c) -> None:
    _seed_registry(c)
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
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '331'")
    assert cur.fetchone()["ok"] == "331"


def test_the_dev_routes_become_the_expected_routes(cur):
    _load_routes(cur)
    cur.execute(_body())
    assert _routes(cur) == _expected()


def test_the_expected_routes_append_exactly_the_picks():
    before = {r["stage"]: r for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]}
    for r in _expected():
        b = before[r["stage"]]
        if r["stage"] in PICKS:
            assert r == {**b, "fallback_model_ids": [*b["fallback_model_ids"], PICKS[r["stage"]]]}, r["stage"]
        else:
            assert r == b, r["stage"]


def test_a_second_run_changes_nothing(cur):
    _load_routes(cur)
    cur.execute(_body())
    cur.execute("SELECT stage, updated_at, updated_by FROM shared.llm_role_config ORDER BY stage")
    first = cur.fetchall()
    cur.execute(_body())
    cur.execute("SELECT stage, updated_at, updated_by FROM shared.llm_role_config ORDER BY stage")
    assert cur.fetchall() == first
    assert _routes(cur) == _expected()


def test_writers_tenant_and_tripplanner_stages_are_untouched(cur):
    _load_routes(cur)
    cur.execute(_body())
    cur.execute("SELECT stage, role FROM shared.llm_role_config WHERE updated_by = 'migration-331-aa1026' "
                "ORDER BY stage")
    rows = cur.fetchall()
    assert sorted(r["stage"] for r in rows) == sorted(PICKS)
    assert {r["role"] for r in rows} == {"judge", "validate"}
    cur.execute("SELECT count(*) AS n FROM shared.llm_role_config WHERE stage = ANY (%s) "
                "AND updated_by = 'migration-331-aa1026'", (list(TENANT),))
    assert cur.fetchone()["n"] == 0


def test_a_stage_with_a_second_domain_already_is_left_alone(cur):
    """An admin already gave a3_place_days the OpenAI API (vendor_direct scope): two failure domains, no acc2 added."""
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['sonnet-5-5', 'gpt-6-luna-openai'], "
                "allowed_scopes = ARRAY['aws_only', 'vendor_direct'] WHERE stage = 'a3_place_days'")
    cur.execute(_body())
    cur.execute("SELECT fallback_model_ids AS f FROM shared.llm_role_config WHERE stage = 'a3_place_days'")
    assert cur.fetchone()["f"] == ["sonnet-5-5", "gpt-6-luna-openai"]


def test_a_missing_stage_row_is_skipped(cur):
    _load_routes(cur)
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = 'a3_place_locate'")
    cur.execute(_body())
    cur.execute("SELECT count(*) AS n FROM shared.llm_role_config WHERE updated_by = 'migration-331-aa1026'")
    assert cur.fetchone()["n"] == len(PICKS) - 1


def test_guard_refuses_a_disabled_acc2_connection(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_provider_connection SET enabled = false WHERE connection_key = 'acc2'")
    with pytest.raises(psycopg2.errors.RaiseException, match="connection acc2 is not an enabled"):
        cur.execute(_body())


def test_guard_refuses_a_pick_without_its_acc2_deployment(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_deployment SET enabled = false "
                "WHERE model_key = 'llama4-scout' AND connection_key = 'acc2'")
    with pytest.raises(psycopg2.errors.RaiseException, match="picks without an enabled non-Anthropic"):
        cur.execute(_body())


def test_guard_refuses_a_check_pair_on_one_maker(cur):
    _load_routes(cur)
    body = _body().replace("'llama4-scout', 'nova-lite', 'nova-lite', 'nova-lite'];",
                           "'nova-lite', 'nova-lite', 'nova-lite', 'nova-lite'];", 1)
    assert body != _body()                                                  # the mutation really applied
    with pytest.raises(psycopg2.errors.RaiseException, match="share a maker"):
        cur.execute(body)


def test_guard_refuses_a_tenant_stage(cur):
    _load_routes(cur)
    body = _body().replace("'a3_place_dedupe_check'];", "'t9_faq_filter'];", 1)
    assert body != _body()
    with pytest.raises(psycopg2.errors.RaiseException, match="ADR 0009 decision 14"):
        cur.execute(body)


def test_guard_refuses_a_writer(cur):
    _load_routes(cur)
    # make s1_brand_audit a writer for the test: the UPDATE skips non judge / validate rows, so give the guard a writer
    # row that names an acc2-only model instead
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['nova-lite'] WHERE stage = 's1_flag_fix'")
    with pytest.raises(psycopg2.errors.RaiseException, match="writer routes naming an acc2-only chat model"):
        cur.execute(_body())
