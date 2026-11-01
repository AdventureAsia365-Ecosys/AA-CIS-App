"""AA-1026 — migration 335 (legacy haiku / sonnet routes -> Converse keys, A-series writers vendor_direct with the
OpenAI API last, the openai-direct monthly budget) against real Postgres.

conftest applies every migration, 335 included. The main test replaces the route table with the 37 Dev routes of the
S3 fixture (``tests/unit/snapshots/aa1002_s3_routes_fixture.json``, Dev 29/10, with migration 311's scope backfill),
runs the file's body inside one transaction (errors not swallowed) and compares every row with
``tests/unit/snapshots/aa1026_routes_after_335.json``, the routes the unit test ``test_aa1026_route_moves.py`` walks
through the gateway. Every test rolls back what it wrote.
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
M335 = ROOT / "api" / "migrations" / "335_llm_routes_converse_keys_writer_scopes.sql"
M311 = ROOT / "api" / "migrations" / "311_llm_stage_allowed_scopes.sql"
FIXTURE = ROOT / "tests" / "unit" / "snapshots" / "aa1002_s3_routes_fixture.json"
EXPECTED = ROOT / "tests" / "unit" / "snapshots" / "aa1026_routes_after_335.json"
WRITERS = ("a0_column_map", "s1_flag_fix", "s1_generate", "s1_itinerary_nudge", "supply_content_writer")
COLUMNS = ("stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
           "shadow_sample_pct, allowed_scopes")


def _body() -> str:
    """The file without its own BEGIN / COMMIT, so a test runs it inside the test's transaction."""
    return M335.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


def _backfill_311() -> str:
    text = M311.read_text(encoding="utf-8")
    end = "WHERE r.allowed_scopes IS NULL;"
    return text[text.index("WITH reach AS"):text.index(end) + len(end)]


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


def _load_fixture_routes(c) -> None:
    c.execute("DELETE FROM shared.llm_role_config")
    for r in json.loads(FIXTURE.read_text(encoding="utf-8"))["routes"]:
        c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                  "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'test-aa1026', NULL)",
                  (r["stage"], r["role"], r["provider"], r["model_id"], r["account_route"], r["is_active"],
                   r["fallback_model_ids"], r["shadow_model_id"], r["shadow_sample_pct"]))
    c.execute(_backfill_311())


def _routes(c) -> list[dict]:
    c.execute(f"SELECT {COLUMNS} FROM shared.llm_role_config ORDER BY stage")
    return [dict(r) for r in c.fetchall()]


def test_registered(cur):
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '335'")
    assert cur.fetchone()["ok"] == "335"


def test_the_dev_routes_become_the_expected_routes(cur):
    _load_fixture_routes(cur)
    cur.execute(_body())
    assert _routes(cur) == json.loads(EXPECTED.read_text(encoding="utf-8"))["routes"]


def test_a_second_run_changes_nothing(cur):
    _load_fixture_routes(cur)
    cur.execute(_body())
    cur.execute("SELECT stage, updated_at FROM shared.llm_role_config ORDER BY stage")
    first = cur.fetchall()
    cur.execute("SAVEPOINT again")
    cur.execute(_body())
    cur.execute("SELECT stage, updated_at FROM shared.llm_role_config ORDER BY stage")
    assert cur.fetchall() == first            # same transaction: now() is fixed, so compare the rows, not the clock
    assert _routes(cur) == json.loads(EXPECTED.read_text(encoding="utf-8"))["routes"]


def test_no_legacy_key_left_and_tenant_and_tripplanner_stay_aws_only(cur):
    _load_fixture_routes(cur)
    cur.execute(_body())
    cur.execute("SELECT count(*) AS n FROM shared.llm_role_config WHERE model_id IN ('haiku', 'sonnet') "
                "OR fallback_model_ids && ARRAY['haiku', 'sonnet']::text[] OR shadow_model_id IN ('haiku', 'sonnet')")
    assert cur.fetchone()["n"] == 0
    cur.execute("SELECT stage, allowed_scopes FROM shared.llm_role_config "
                "WHERE (stage LIKE 't%%' OR stage LIKE 'tp\\_%%') AND role = 'writer'")
    rows = cur.fetchall()
    assert rows and all(r["allowed_scopes"] == ["aws_only"] for r in rows)
    cur.execute("SELECT stage, model_id, fallback_model_ids FROM shared.llm_role_config WHERE stage LIKE 'tp\\_%%' "
                "AND role = 'writer' ORDER BY stage")
    assert [(r["stage"], r["model_id"], r["fallback_model_ids"]) for r in cur.fetchall()] == [
        ("tp_compose", "sonnet-5-5", []), ("tp_extract", "sonnet-5-5", [])]   # one to one: no Haiku degrade added


def test_writers_end_on_the_openai_api_with_both_scopes(cur):
    _load_fixture_routes(cur)
    cur.execute(_body())
    cur.execute("SELECT r.stage, r.model_id, r.fallback_model_ids, r.allowed_scopes, m.vendor "
                "FROM shared.llm_role_config r JOIN shared.llm_model_catalog m ON m.model_key = r.model_id "
                "WHERE r.stage = ANY (%s) ORDER BY r.stage", (list(WRITERS),))
    rows = cur.fetchall()
    assert [r["stage"] for r in rows] == sorted(WRITERS)
    for r in rows:
        assert r["vendor"] == "anthropic", r["stage"]
        assert r["fallback_model_ids"][-1] == "gpt-6-luna-openai"
        assert r["fallback_model_ids"].count("gpt-6-luna-openai") == 1
        assert r["allowed_scopes"] == ["aws_only", "vendor_direct"]


def test_openai_api_mid_route_moves_to_the_end(cur):
    _load_fixture_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['gpt-6-luna-openai', 'sonnet'] "
                "WHERE stage = 's1_flag_fix'")
    cur.execute(_body())
    cur.execute("SELECT model_id, fallback_model_ids FROM shared.llm_role_config WHERE stage = 's1_flag_fix'")
    r = cur.fetchone()
    assert (r["model_id"], r["fallback_model_ids"]) == ("haiku-5-5", ["sonnet-5-5", "gpt-6-luna-openai"])


def test_a_legacy_shadow_maps_and_is_cleared_when_it_becomes_the_primary(cur):
    _load_fixture_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET shadow_model_id = 'sonnet', shadow_sample_pct = 15 "
                "WHERE stage = 'a3_place_card_check'")                      # haiku -> sonnet route, shadow sonnet
    cur.execute("UPDATE shared.llm_role_config SET shadow_model_id = 'haiku', shadow_sample_pct = 20 "
                "WHERE stage = 'a3_place_dedupe_check'")                    # primary haiku: the shadow would equal it
    cur.execute(_body())
    cur.execute("SELECT stage, shadow_model_id, shadow_sample_pct FROM shared.llm_role_config "
                "WHERE stage IN ('a3_place_card_check', 'a3_place_dedupe_check') ORDER BY stage")
    assert [tuple(r.values()) for r in cur.fetchall()] == [("a3_place_card_check", "sonnet-5-5", 15),
                                                           ("a3_place_dedupe_check", None, 0)]


def test_openai_budget_and_policy_set_once_an_admin_value_is_kept(cur):
    cur.execute("SELECT monthly_budget_usd AS b FROM shared.llm_provider_connection "
                "WHERE connection_key = 'openai-direct'")
    assert float(cur.fetchone()["b"]) == 10.0                                 # conftest applied 335
    cur.execute("SELECT alert_pct, hard_stop FROM shared.llm_connection_cost_policy "
                "WHERE connection_key = 'openai-direct'")
    assert tuple(cur.fetchone().values()) == (80, True)
    cur.execute("UPDATE shared.llm_provider_connection SET monthly_budget_usd = 25 "
                "WHERE connection_key = 'openai-direct'")
    cur.execute("UPDATE shared.llm_connection_cost_policy SET alert_pct = 50 WHERE connection_key = 'openai-direct'")
    cur.execute(_body())
    cur.execute("SELECT c.monthly_budget_usd AS b, p.alert_pct AS a FROM shared.llm_provider_connection c "
                "JOIN shared.llm_connection_cost_policy p USING (connection_key) "
                "WHERE connection_key = 'openai-direct'")
    r = cur.fetchone()
    assert (float(r["b"]), r["a"]) == (25.0, 50)


def test_how_the_openai_key_reaches_the_task_is_unchanged(cur):
    cur.execute("SELECT credential_env, secret_name, data_scope FROM shared.llm_provider_connection "
                "WHERE connection_key = 'openai-direct'")
    assert tuple(cur.fetchone().values()) == ("OPENAI_API_KEY", None, "vendor_direct")


def test_guard_refuses_without_the_converse_keys(cur):
    cur.execute("UPDATE shared.llm_model_deployment SET enabled = false "
                "WHERE model_key = 'haiku-5-5' AND connection_key = 'acc3'")
    with pytest.raises(psycopg2.errors.RaiseException, match="Converse keys without"):
        cur.execute(_body())


def test_guard_refuses_a_tenant_stage_opened_by_this_file(cur):
    _load_fixture_routes(cur)
    body = _body().replace("WHERE r.stage IN ('s1_generate', 's1_flag_fix', 's1_itinerary_nudge', 'a0_column_map', "
                           "'supply_content_writer')\n       AND r.model_id",
                           "WHERE r.stage IN ('s1_generate', 's1_flag_fix', 's1_itinerary_nudge', 'a0_column_map', "
                           "'supply_content_writer', 't9_write')\n       AND r.model_id", 1)
    assert body != _body()                                                  # the mutation really applied
    with pytest.raises(psycopg2.errors.RaiseException, match="vendor_direct outside the decided set"):
        cur.execute(body)
