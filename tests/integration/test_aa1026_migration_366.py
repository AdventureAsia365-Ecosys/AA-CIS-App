"""AA-1026 (tech lead S267) — migration 366 (family fallback chains: Converse rows sonnet-4-6 / haiku-4-5 on acc3, the
model-first chains of 34 stage routes with GPT-6 Luna's OpenAI API twin right behind it, every active text stage
opened to vendor_direct ("AWS preferred, not AWS only"), monthly caps acc3 100 USD and anthropic-direct 150 USD)
against real Postgres.

conftest applies every migration, 366 included. The main test loads the live routes of 04/11/2026
(``tests/unit/snapshots/aa1026_routes_before_366.json``), runs the file's body inside one transaction and compares
every row with ``tests/unit/snapshots/aa1026_routes_after_366.json`` (the table the unit tests walk through the gateway
and check with ``llm_route_chains.violations``). Every test rolls back what it wrote. The body re-inserts its two
catalog rows and deployments ON CONFLICT DO NOTHING, so the file runs the same after the 304 replay test re-created the
deployment table.
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
from _later_deployments import set_later_deployments_aside  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
M366 = ROOT / "api" / "migrations" / "366_llm_family_fallback_chains.sql"
SNAPSHOTS = ROOT / "tests" / "unit" / "snapshots"
BEFORE = SNAPSHOTS / "aa1026_routes_before_366.json"
EXPECTED = SNAPSHOTS / "aa1026_routes_after_366.json"
COLUMNS = ("stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
           "shadow_sample_pct, allowed_scopes")
ACTOR = "migration-366-aa1026"
NEW_ROWS = {"sonnet-4-6": "sonnet", "haiku-4-5": "haiku"}
CAPS = {"acc3": 100, "anthropic-direct": 150}


def _body() -> str:
    return M366.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("BEGIN")
    # A re-run of 306 by its own replay test seeds Anthropic API rows for every Anthropic Converse model, the twins
    # included; on Dev 306 ran before 366, so the twins are on acc3 only (366's guard checks it).
    c.execute("DELETE FROM shared.llm_model_deployment WHERE model_key = ANY (%s) AND connection_key <> 'acc3'",
              (list(NEW_ROWS),))
    # AA-1095: the deployments later migrations seeded (406's acc1 rows) did not exist when 366 ran on Dev; its
    # attempt_cap rule counts every row, so they leave too (tests/integration/_later_deployments.py)
    set_later_deployments_aside(c, 366)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        conn.close()


def _load_routes(c) -> None:
    c.execute("DELETE FROM shared.llm_role_config")
    for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]:
        c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                  "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'test-aa1026-366', %s)",
                  (r["stage"], r["role"], r["provider"], r["model_id"], r["account_route"], r["is_active"],
                   r["fallback_model_ids"], r["shadow_model_id"], r["shadow_sample_pct"], r["allowed_scopes"]))


def _routes(c) -> list[dict]:
    c.execute(f"SELECT {COLUMNS} FROM shared.llm_role_config ORDER BY stage")
    return [dict(r) for r in c.fetchall()]


def _expected() -> list[dict]:
    return json.loads(EXPECTED.read_text(encoding="utf-8"))["routes"]


def _decided() -> list[str]:
    before = {r["stage"]: r for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]}
    return sorted(r["stage"] for r in _expected()
                  if r["fallback_model_ids"] != before[r["stage"]]["fallback_model_ids"])


def _widened() -> list[str]:
    """The active writer / judge / validate stages of the 04/11 table without vendor_direct (T-3's aws_only stages)."""
    return sorted(r["stage"] for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]
                  if r["is_active"] and r["role"] != "embed" and "vendor_direct" not in r["allowed_scopes"])


def _budget(c, conn: str):
    c.execute("SELECT c.monthly_budget_usd AS b, p.alert_pct AS a, p.hard_stop AS h "
              "FROM shared.llm_provider_connection c LEFT JOIN shared.llm_connection_cost_policy p "
              "ON p.connection_key = c.connection_key WHERE c.connection_key = %s", (conn,))
    r = c.fetchone()
    return (float(r["b"]) if r["b"] is not None else None, r["a"], r["h"])


def _changed(c) -> list[str]:
    c.execute("SELECT stage AS s FROM shared.llm_role_config WHERE updated_by = %s ORDER BY 1", (ACTOR,))
    return [r["s"] for r in c.fetchall()]


def _fallbacks(c, stage: str) -> list:
    c.execute("SELECT fallback_model_ids AS f FROM shared.llm_role_config WHERE stage = %s", (stage,))
    return list(c.fetchone()["f"])


def test_registered(cur):
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '366'")
    assert cur.fetchone()["ok"] == "366"


def test_the_converse_twins_copy_their_legacy_rows_and_live_on_acc3_only(cur):
    cur.execute(_body())                                   # puts the rows back if a replay test re-created the tables
    for key, legacy in NEW_ROWS.items():
        cur.execute("SELECT n.api_style AS style, n.family AS fam, n.vendor AS vendor, n.enabled AS on_, "
                    "n.callable_via AS via, n.price_in_per_mtok = l.price_in_per_mtok AS same_in, "
                    "n.price_out_per_mtok = l.price_out_per_mtok AS same_out, "
                    "n.supports_temperature = l.supports_temperature AS same_temp, "
                    "n.bedrock_profile_ids AS profiles "
                    "FROM shared.llm_model_catalog n, shared.llm_model_catalog l "
                    "WHERE n.model_key = %s AND l.model_key = %s", (key, legacy))
        row = cur.fetchone()
        assert (row["style"], row["fam"], row["vendor"], row["on_"]) == ("converse", "anthropic", "anthropic", True)
        assert list(row["via"]) == ["llm_client"] and row["same_in"] and row["same_out"] and row["same_temp"]
        cur.execute("SELECT connection_key AS c, wire_id AS w, enabled AS e FROM shared.llm_model_deployment "
                    "WHERE model_key = %s", (key,))
        deps = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT wire_id AS w FROM shared.llm_model_deployment WHERE model_key = %s "
                    "AND connection_key = 'acc3'", (legacy,))
        wire = cur.fetchone()["w"]
        assert [(d["c"], d["w"]) for d in deps] == [("acc3", wire)] and deps[0]["e"]
        assert row["profiles"] == {"acc3": wire}


def test_the_routes_become_the_expected_routes_and_a_second_run_changes_nothing(cur):
    _load_routes(cur)
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert len(_decided()) == 34 and len(_widened()) == 15
    assert set(_widened()) <= set(_decided())
    assert _changed(cur) == _decided()
    cur.execute("UPDATE shared.llm_role_config SET updated_by = 'test-aa1026-366-done' WHERE updated_by = %s",
                (ACTOR,))
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert _changed(cur) == []                                             # the re-apply finds every route in place


def test_a_route_an_admin_changed_since_is_left_as_it_is(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['nova-lite'] "
                "WHERE stage = 'a3_place_dedupe_check'")
    cur.execute("UPDATE shared.llm_role_config SET model_id = 'sonnet-5-5' WHERE stage = 't9_repair'")
    cur.execute(_body())
    assert "a3_place_dedupe_check" in _changed(cur)                      # same as the snapshot: still applied
    assert _fallbacks(cur, "t9_repair") == ["gpt-6-luna-openai"]           # re-routed: kept
    assert "t9_repair" not in _changed(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['haiku-5-5'] "
                "WHERE stage = 'segment_research'")
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY[]::text[], updated_by = 'x' "
                "WHERE stage = 'a3_place_days'")
    cur.execute(_body())
    assert _fallbacks(cur, "a3_place_days") == []                          # not the old route any more: kept


def test_a_switched_off_or_missing_stage_is_skipped(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET is_active = false, disabled_reason = 'test' "
                "WHERE stage = 'tp_compose'")
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = 't2_flag_fix'")
    cur.execute(_body())
    assert _fallbacks(cur, "tp_compose") == []
    assert set(_decided()) - set(_changed(cur)) == {"tp_compose", "t2_flag_fix"}
    cur.execute("SELECT allowed_scopes AS s FROM shared.llm_role_config WHERE stage = 'tp_compose'")
    assert list(cur.fetchone()["s"]) == ["aws_only"]                       # switched off: not opened either


def test_s267_every_active_text_stage_is_opened_and_the_embed_stages_stay_inside_aws(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['haiku-4-5', 'nova-lite'] "
                "WHERE stage = 't9_faq_filter'")                           # an admin's chain: kept, still opened
    cur.execute(_body())
    cur.execute("SELECT stage, role, is_active, allowed_scopes AS s FROM shared.llm_role_config ORDER BY stage")
    rows = cur.fetchall()
    for r in rows:
        if r["is_active"] and r["role"] != "embed":
            assert list(r["s"]) == ["aws_only", "vendor_direct"], r["stage"]
        else:
            assert list(r["s"]) == ["aws_only"], r["stage"]
    assert _fallbacks(cur, "t9_faq_filter") == ["haiku-4-5", "nova-lite"]
    assert "t9_faq_filter" in _changed(cur)                                # opened (updated_by), chain kept
    notices = " ".join(cur.connection.notices)
    assert "text stages opened to vendor_direct (AWS preferred, not AWS only): a3_atomize" in notices


def test_s267_the_connection_caps_and_their_cost_policies(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_provider_connection SET monthly_budget_usd = NULL "
                "WHERE connection_key = ANY (%s)", (list(CAPS),))
    cur.execute("DELETE FROM shared.llm_connection_cost_policy WHERE connection_key = ANY (%s)", (list(CAPS),))
    cur.execute(_body())
    for conn, cap in CAPS.items():
        assert _budget(cur, conn) == (cap, 80, True), conn


def test_s267_a_lower_cap_and_an_admin_policy_are_kept_a_higher_cap_is_lowered(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_provider_connection SET monthly_budget_usd = 40 WHERE connection_key = 'acc3'")
    cur.execute("UPDATE shared.llm_provider_connection SET monthly_budget_usd = 900 "
                "WHERE connection_key = 'anthropic-direct'")
    cur.execute("DELETE FROM shared.llm_connection_cost_policy WHERE connection_key = 'anthropic-direct'")
    cur.execute("INSERT INTO shared.llm_connection_cost_policy (connection_key, alert_pct, hard_stop, updated_by) "
                "VALUES ('anthropic-direct', 50, false, 'test-aa1026-366')")
    cur.execute(_body())
    assert _budget(cur, "acc3")[0] == 40
    assert _budget(cur, "anthropic-direct") == (150, 50, False)
    assert "the cost policy of anthropic-direct is kept" in " ".join(cur.connection.notices)


def test_guard_refuses_without_the_anthropic_api_connection(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_provider_connection SET data_scope = 'aws_only' "
                "WHERE connection_key = 'anthropic-direct'")
    with pytest.raises(psycopg2.errors.RaiseException, match="anthropic-direct is not an anthropic vendor_direct"):
        cur.execute(_body())


def test_no_unlisted_stage_changes(cur):
    _load_routes(cur)
    cur.execute(f"SELECT {COLUMNS} FROM shared.llm_role_config WHERE NOT (stage = ANY (%s)) ORDER BY stage",
                (_decided(),))
    others = [dict(r) for r in cur.fetchall()]
    cur.execute(_body())
    cur.execute(f"SELECT {COLUMNS} FROM shared.llm_role_config WHERE NOT (stage = ANY (%s)) ORDER BY stage",
                (_decided(),))
    assert [dict(r) for r in cur.fetchall()] == others


def test_a_rule_break_on_an_unchanged_route_is_only_a_notice(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['gpt-6-luna', 'haiku-5-5'] "
                "WHERE stage = 's1_brand_audit'")                         # a judge on a Claude fallback (admin's)
    cur.execute(_body())
    notices = " ".join(cur.connection.notices)
    assert "route s1_brand_audit (fallbacks not changed here) breaks the chain rule judge_writer_family" in notices


# ── guards ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_guard_refuses_without_344(cur):
    _load_routes(cur)
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '344'")
    with pytest.raises(psycopg2.errors.RaiseException, match="apply 335 and 344 first"):
        cur.execute(_body())


def test_guard_refuses_a_legacy_row_without_its_acc3_profile(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_deployment SET wire_id = 'global.anthropic.claude-sonnet-5-5' "
                "WHERE model_key = 'sonnet' AND connection_key = 'acc3'")
    with pytest.raises(psycopg2.errors.RaiseException, match="acc3 deployment of sonnet"):
        cur.execute(_body())


def test_guard_refuses_a_twin_key_that_is_another_kind_of_row(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_catalog SET api_style = 'anthropic_native' WHERE model_key = 'haiku-4-5'")
    with pytest.raises(psycopg2.errors.RaiseException, match="haiku-4-5 exists and is not an Anthropic"):
        cur.execute(_body())


def test_guard_refuses_a_chain_model_that_is_switched_off(cur):
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_catalog SET enabled = false WHERE model_key = 'nova-lite'")
    with pytest.raises(psycopg2.errors.RaiseException, match="nova-lite"):
        cur.execute(_body())


def test_guard_refuses_a_twin_deployed_outside_acc3(cur):
    _load_routes(cur)
    cur.execute(_body())
    cur.execute("INSERT INTO shared.llm_model_deployment (model_key, connection_key, wire_id, priority, enabled, "
                "origin, updated_by) VALUES ('sonnet-4-6', 'anthropic-direct', 'claude-sonnet-4-6', 100, false, "
                "'admin', 'test-aa1026-366') ON CONFLICT DO NOTHING")
    with pytest.raises(psycopg2.errors.RaiseException, match="deployed on acc3 only"):
        cur.execute(_body())


def test_guard_refuses_a_new_route_past_the_attempt_cap(cur):
    """A seventh possible attempt on a changed route (here: two more AWS deployments of s1_generate's primary)."""
    _load_routes(cur)
    cur.execute("INSERT INTO shared.llm_provider_connection (connection_key, kind, auth_mode, label, region, "
                "data_scope, enabled, updated_by) VALUES ('zz-aa1026-366', 'bedrock', 'task_role', 'test', "
                "'us-west-2', 'aws_only', false, 'test-aa1026-366')")
    for conn in ("acc2", "zz-aa1026-366"):
        cur.execute("INSERT INTO shared.llm_model_deployment (model_key, connection_key, wire_id, priority, enabled, "
                    "origin, updated_by) VALUES ('sonnet-5-5', %s, 'zz-wire', 20, false, 'admin', 'test-aa1026-366') "
                    "ON CONFLICT DO NOTHING", (conn,))
    with pytest.raises(psycopg2.errors.RaiseException, match="breaks the chain rule attempt_cap"):
        cur.execute(_body())


def test_s267_the_openai_api_twin_sits_right_behind_gpt_6_luna(cur):
    """Model first (tech lead S267 Q2: "openai-direct for a GPT twin if one exists"): the guard knows gpt-6-luna-openai
    as GPT-6 Luna's vendor twin, so it may come before an AWS model when it sits right behind gpt-6-luna."""
    _load_routes(cur)
    cur.execute(_body())
    assert "vendor twins (vendor-only key > AWS key, part of that model's slot): gpt-6-luna-openai>gpt-6-luna" \
        in " ".join(cur.connection.notices)
    assert _fallbacks(cur, "a3_atomize") == ["gpt-6-luna-openai", "haiku-5-5", "haiku-4-5", "nova-lite"]
    assert _fallbacks(cur, "s1_judge") == ["gpt-6-luna", "gpt-6-luna-openai", "nova-lite"]


def test_guard_refuses_a_vendor_only_model_ahead_of_aws_when_it_is_no_twin(cur):
    """The same chains with gpt-6-luna-openai no longer naming GPT-6 Luna (another wire model): it is then a vendor-only
    model ahead of AWS models, the vendor_not_last rule."""
    _load_routes(cur)
    cur.execute("UPDATE shared.llm_model_catalog SET wire_model = 'gpt-6-astra' WHERE model_key = 'gpt-6-luna-openai'")
    with pytest.raises(psycopg2.errors.RaiseException, match="breaks the chain rule vendor_not_last"):
        cur.execute(_body())
