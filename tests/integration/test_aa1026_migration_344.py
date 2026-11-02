"""AA-1026 (tech lead S264) — migration 344 (the six tenant writer stages: vendor_direct, the OpenAI API model last)
against real Postgres.

conftest applies every migration, 344 included. The main test loads the routes after migration 339
(``tests/unit/snapshots/aa1026_routes_after_339.json``, the live Dev routes of the six stages), runs the file's body
inside one transaction and compares every row with ``tests/unit/snapshots/aa1026_routes_after_344.json``. Every test
rolls back what it wrote. Migration 344 only UPDATEs ``shared.llm_role_config`` rows, which the 304 replay test does
not reset, so it is not in that test's LATER list.
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
M344 = ROOT / "api" / "migrations" / "344_llm_tenant_writers_openai_last.sql"
SNAPSHOTS = ROOT / "tests" / "unit" / "snapshots"
BEFORE = SNAPSHOTS / "aa1026_routes_after_339.json"
EXPECTED = SNAPSHOTS / "aa1026_routes_after_344.json"
STAGES = ("t2_flag_fix", "t2_generate", "t2_itinerary_nudge", "t8_angle_gen", "t9_repair", "t9_write")
OPENAI_HOP = "gpt-6-luna-openai"
BOTH = ["aws_only", "vendor_direct"]
COLUMNS = ("stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
           "shadow_sample_pct, allowed_scopes")
ACTOR = "migration-344-aa1026"


def _body() -> str:
    return M344.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


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


def _load_routes(c) -> None:
    """The routes after 339 and the registry the file expects (openai-direct on, serving writers)."""
    c.execute("UPDATE shared.llm_provider_connection SET enabled = true "
              "WHERE connection_key IN ('acc3', 'openai-direct')")
    c.execute("DELETE FROM shared.llm_role_config")
    for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]:
        c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                  "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'test-aa1026-344', %s)",
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


def _route(c, stage: str) -> tuple:
    c.execute("SELECT model_id AS m, fallback_model_ids AS f, allowed_scopes AS s FROM shared.llm_role_config "
              "WHERE stage = %s", (stage,))
    r = c.fetchone()
    return r["m"], list(r["f"]), list(r["s"])


def test_registered(cur):
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '344'")
    assert cur.fetchone()["ok"] == "344"


def test_the_routes_become_the_expected_routes_and_a_second_run_changes_nothing(cur):
    _load_routes(cur)
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert _changed(cur) == sorted(STAGES)
    cur.execute("UPDATE shared.llm_role_config SET updated_by = 'test-aa1026-344-done' WHERE updated_by = %s",
                (ACTOR,))
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert _changed(cur) == []                                              # the re-apply finds every route in place


def test_the_expected_routes_change_only_the_six_tenant_writers():
    before = {r["stage"]: r for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]}
    after = {r["stage"]: r for r in _expected()}
    assert set(before) == set(after)
    for stage, r in after.items():
        b = before[stage]
        if stage in STAGES:
            assert b["role"] == "writer" and b["allowed_scopes"] == ["aws_only"], stage
            assert r == {**b, "fallback_model_ids": [OPENAI_HOP], "allowed_scopes": BOTH}, stage
        else:
            assert r == b, stage
    # TripPlanner and every other T-stage writer stay inside AWS
    for stage, r in after.items():
        if stage.startswith("tp_"):
            assert r["allowed_scopes"] == ["aws_only"], stage


def test_an_openai_hop_mid_route_moves_to_the_end_and_a_later_aws_model_is_dropped(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY[%s, 'haiku-5-5'] "
                "WHERE stage = 't9_write'", (OPENAI_HOP,))
    cur.execute(_body())
    assert _route(cur, "t9_write") == ("sonnet-5-5", [OPENAI_HOP], BOTH)


def test_a_fallback_reached_only_outside_aws_is_kept_before_the_hop(cur):
    """Only AWS models behind a vendor-capable one are dropped (T-3); a model with no AWS deployment may stay."""
    _load_routes(cur)
    cur.execute("INSERT INTO shared.llm_model_catalog (model_key, label, vendor, provider, api_style, "
                "bedrock_profile_ids, callable_via, supports_temperature, enabled, updated_by) "
                "VALUES ('zz-aa1026-344-outside', 'Outside test model', 'anthropic', 'anthropic', "
                "'anthropic_messages', '{}'::jsonb, ARRAY['llm_client'], false, false, 'test-aa1026-344')")
    cur.execute("INSERT INTO shared.llm_model_deployment (model_key, connection_key, wire_id, priority, enabled, "
                "origin, updated_by) VALUES ('zz-aa1026-344-outside', 'anthropic-direct', 'zz-outside-wire', 100, "
                "false, 'admin', 'test-aa1026-344')")
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['haiku-5-5', 'zz-aa1026-344-outside'] "
                "WHERE stage = 't2_generate'")
    cur.execute(_body())
    assert _route(cur, "t2_generate") == ("sonnet-5-5", ["zz-aa1026-344-outside", OPENAI_HOP], BOTH)


def test_a_stage_that_is_not_a_writer_on_an_anthropic_primary_is_left_as_it_is(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET model_id = 'gpt-6-luna', fallback_model_ids = '{}' "
                "WHERE stage = 't9_repair'")                               # re-routed by an admin to another maker
    cur.execute(_body())
    assert _route(cur, "t9_repair") == ("gpt-6-luna", [], ["aws_only"])
    assert "t9_repair" not in _changed(cur)


def test_a_missing_stage_row_is_skipped(cur):
    _load_routes(cur)
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = 't2_itinerary_nudge'")
    cur.execute(_body())
    assert _changed(cur) == sorted(set(STAGES) - {"t2_itinerary_nudge"})


def test_no_other_stage_changes(cur):
    _load_routes(cur)
    cur.execute(f"SELECT {COLUMNS} FROM shared.llm_role_config WHERE NOT (stage = ANY (%s)) ORDER BY stage",
                (list(STAGES),))
    others = [dict(r) for r in cur.fetchall()]
    cur.execute(_body())
    cur.execute(f"SELECT {COLUMNS} FROM shared.llm_role_config WHERE NOT (stage = ANY (%s)) ORDER BY stage",
                (list(STAGES),))
    assert [dict(r) for r in cur.fetchall()] == others


# ── guards ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_guard_refuses_without_327(cur):
    _load_routes(cur)
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '327'")
    with pytest.raises(psycopg2.errors.RaiseException, match="apply 327 first"):
        cur.execute(_body())


def test_guard_refuses_when_openai_direct_does_not_serve_writers(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_provider_connection SET served_roles = ARRAY['judge', 'validate'] "
                "WHERE connection_key = 'openai-direct'")
    with pytest.raises(psycopg2.errors.RaiseException, match="serving the writer role"):
        cur.execute(_body())


def test_guard_refuses_without_the_openai_hop(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_catalog SET enabled = false WHERE model_key = %s", (OPENAI_HOP,))
    with pytest.raises(psycopg2.errors.RaiseException, match="deployed on openai-direct"):
        cur.execute(_body())


def test_guard_refuses_a_hop_that_is_also_inside_aws(cur):
    _load_routes(cur)
    cur.execute("INSERT INTO shared.llm_model_deployment (model_key, connection_key, wire_id, priority, enabled, "
                "origin, updated_by) VALUES (%s, 'acc3', 'zz-aa1026-344-wire', 5, false, 'admin', 'test-aa1026-344')",
                (OPENAI_HOP,))
    with pytest.raises(psycopg2.errors.RaiseException, match="not an outside-only last rung"):
        cur.execute(_body())


def test_guard_refuses_a_writer_that_would_leave_aws_before_a_later_aws_model(cur):
    _load_routes(cur)
    body = _body().replace("            IF seen_vendor AND (k = ANY (inside) OR NOT (k = ANY (outside))) THEN\n"
                           "                CONTINUE;\n"
                           "            END IF;\n", "", 1)
    assert body != _body()                                                  # the mutation really applied
    with pytest.raises(psycopg2.errors.RaiseException, match="vendor API before an AWS deployment"):
        cur.execute(body)


def test_guard_refuses_a_stage_opened_outside_the_decided_set(cur):
    _load_routes(cur)
    body = _body().replace("         WHERE stage = stages[i];\n        changed",
                           "         WHERE stage = stages[i] OR stage = 'tp_compose';\n        changed", 1)
    assert body != _body()                                                  # the mutation really applied
    with pytest.raises(psycopg2.errors.RaiseException, match="not a listed tenant writer stage"):
        cur.execute(body)


def test_guard_refuses_a_change_to_another_row(cur):
    _load_routes(cur)
    body = _body().replace("        changed := changed || stages[i];\n",
                           "        changed := changed || stages[i];\n"
                           "        UPDATE shared.llm_role_config SET shadow_sample_pct = 7 "
                           "WHERE stage = 't2_judge';\n",
                           1)
    assert body != _body()                                                  # the mutation really applied
    with pytest.raises(psycopg2.errors.RaiseException, match="rows outside the six tenant writers changed"):
        cur.execute(body)
