"""AA-1042 follow-up (tech lead S269) — migration 376 against real Postgres: every Claude model of the Anthropic API key
in the gateway catalog (Fable 5.1 / 5, Opus 5 / 4.8 / 4.7 rows reached only by the API; the Opus 4.6 / 4.5 candidates
switched on), nine anthropic-direct deployments behind AWS (Sonnet 4.6 and Haiku 4.5 included), the 366 family chains
fitted to the six attempts per request, and one external route config version.

conftest applies every migration, 376 included. Each test sets 376's rows aside inside one transaction (the post-366 /
367 / 370 state), loads the routes 366 decided (``tests/unit/snapshots/aa1026_routes_after_366.json``), runs the file's
body and compares with ``tests/unit/snapshots/aa1042_routes_after_376.json`` (the table the unit tests check with the
chain rules and the route control preview). Every test rolls back.
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

from shared.llm_client import route_versions  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
M376 = ROOT / "api" / "migrations" / "376_llm_anthropic_api_catalog_fallbacks.sql"
SNAPSHOTS = ROOT / "tests" / "unit" / "snapshots"
BEFORE = SNAPSHOTS / "aa1026_routes_after_366.json"
EXPECTED = SNAPSHOTS / "aa1042_routes_after_376.json"
COLUMNS = ("stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
           "shadow_sample_pct, allowed_scopes")
ACTOR = "migration-376-aa1042"
# key -> (label, API model id, in, out, cache read, cache write): Anthropic list prices (claude-api skill)
NEW_ROWS = {
    "fable-5-1": ("Claude Fable 5.1", "claude-fable-5-1", 10.0, 50.0, 0.25, 12.5),
    "fable-5": ("Claude Fable 5", "claude-fable-5", 10.0, 50.0, 1.0, 12.5),
    "opus-5": ("Claude Opus 5", "claude-opus-5", 5.0, 25.0, 0.5, 6.25),
    "opus-4-8": ("Claude Opus 4.8", "claude-opus-4-8", 5.0, 25.0, 0.5, 6.25),
    "opus-4-7": ("Claude Opus 4.7", "claude-opus-4-7", 5.0, 25.0, 0.5, 6.25),
}
CANDIDATES = ("opus-4-6", "opus-4-5")
WIRES = {**{k: v[1] for k, v in NEW_ROWS.items()}, "opus-4-6": "claude-opus-4-6",
         "opus-4-5": "claude-opus-4-5-20251101", "sonnet-4-6": "claude-sonnet-4-6",
         "haiku-4-5": "claude-haiku-4-5-20251001"}
MAX_ATTEMPTS = 6


def _body() -> str:
    return M376.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


def _set_376_aside(c) -> None:
    """The state 376 starts from on Dev (post-366 / 367 / 370), inside the test's transaction."""
    # AA-1095: the deployments later migrations seeded (406's acc1 rows) did not exist then; attempt_cap counts them
    set_later_deployments_aside(c, 376)
    c.execute("DELETE FROM shared.llm_model_deployment WHERE updated_by = %s", (ACTOR,))
    c.execute("DELETE FROM shared.llm_model_deployment WHERE connection_key = 'anthropic-direct' "
              "AND model_key = ANY (%s)", (list(WIRES),))       # a 306 replay elsewhere may have seeded some
    c.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = ANY (%s)", (list(NEW_ROWS),))
    c.execute("UPDATE shared.llm_model_catalog SET enabled = false, updated_by = 'migration-367-aa1026', "
              "blocked_reason = 'Candidate (CIS 367): not routed.' WHERE model_key = ANY (%s)", (list(CANDIDATES),))
    # One statement from 376's first version on: a revert row another test committed in this shard may point at it
    # (reverted_from FK, checked at the end of the statement), S270 CIS #1329 shard 1.
    c.execute("DELETE FROM shared.llm_route_config_version WHERE version >= "
              "(SELECT min(version) FROM shared.llm_route_config_version WHERE created_by = %s)", (ACTOR,))
    c.execute("UPDATE shared.llm_route_settings SET current_version = "
              "(SELECT max(version) FROM shared.llm_route_config_version) WHERE singleton")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '376'")


def _load_routes(c) -> None:
    c.execute("DELETE FROM shared.llm_role_config")
    for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]:
        c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                  "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'test-aa1042-376', %s)",
                  (r["stage"], r["role"], r["provider"], r["model_id"], r["account_route"], r["is_active"],
                   r["fallback_model_ids"], r["shadow_model_id"], r["shadow_sample_pct"], r["allowed_scopes"]))


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("BEGIN")
    _set_376_aside(c)
    _load_routes(c)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        conn.close()


def _routes(c) -> list[dict]:
    c.execute(f"SELECT {COLUMNS} FROM shared.llm_role_config ORDER BY stage")
    return [dict(r) for r in c.fetchall()]


def _expected() -> list[dict]:
    return json.loads(EXPECTED.read_text(encoding="utf-8"))["routes"]


def _decided() -> list[str]:
    before = {r["stage"]: r for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]}
    return sorted(r["stage"] for r in _expected()
                  if r["fallback_model_ids"] != before[r["stage"]]["fallback_model_ids"])


def _changed(c) -> list[str]:
    c.execute("SELECT stage AS s FROM shared.llm_role_config WHERE updated_by = %s ORDER BY 1", (ACTOR,))
    return [r["s"] for r in c.fetchall()]


def _fallbacks(c, stage: str) -> list:
    c.execute("SELECT fallback_model_ids AS f FROM shared.llm_role_config WHERE stage = %s", (stage,))
    return list(c.fetchone()["f"])


def _attempts(c) -> dict:
    """Stage -> possible attempts (llm_route_chains attempt_cap: every deployment row in the stage's scopes)."""
    c.execute("""
        SELECT rc.stage AS s, (SELECT count(*) FROM (SELECT DISTINCT unnest(ARRAY[rc.model_id] || rc.fallback_model_ids)
                                                    AS key) u
                                 JOIN shared.llm_model_deployment d ON d.model_key = u.key
                                 JOIN shared.llm_provider_connection p ON p.connection_key = d.connection_key
                                WHERE p.data_scope = ANY (coalesce(rc.allowed_scopes, ARRAY['aws_only']))) AS n
          FROM shared.llm_role_config rc WHERE rc.is_active""")
    return {r["s"]: r["n"] for r in c.fetchall()}


def _live(c) -> dict:
    c.execute("SELECT order_mode FROM shared.llm_route_settings WHERE singleton")
    order_mode = c.fetchone()["order_mode"]
    c.execute("SELECT stage, model_id, fallback_model_ids FROM shared.llm_role_config")
    routes = {r["stage"]: {"model_id": r["model_id"], "fallback_model_ids": list(r["fallback_model_ids"] or [])}
              for r in c.fetchall()}
    c.execute("SELECT model_key, connection_key, priority FROM shared.llm_model_deployment")
    priorities: dict = {}
    for r in c.fetchall():
        priorities.setdefault(r["model_key"], {})[r["connection_key"]] = int(r["priority"])
    return {"order_mode": order_mode, "routes": routes, "priorities": priorities}


# ── the file ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_registered_by_the_replay():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    try:
        c = conn.cursor()
        c.execute("SELECT version FROM shared.schema_versions WHERE version = '376'")
        assert c.fetchone() == ("376",)
    finally:
        conn.close()


def test_the_five_api_only_rows_carry_the_anthropic_list_prices(cur):
    cur.execute(_body())
    cur.execute("SELECT model_key, label, vendor, family, provider, api_style, bedrock_profile_ids, wire_model, "
                "callable_via, supports_temperature, max_output_tokens, enabled, blocked_reason, "
                "price_in_per_mtok AS p_in, price_out_per_mtok AS p_out, price_cache_read_per_mtok AS p_read, "
                "price_cache_write_per_mtok AS p_write, price_source, updated_by "
                "FROM shared.llm_model_catalog WHERE model_key = ANY (%s)", (list(NEW_ROWS),))
    rows = {r["model_key"]: r for r in cur.fetchall()}
    assert set(rows) == set(NEW_ROWS)
    for key, (label, wire, p_in, p_out, p_read, p_write) in NEW_ROWS.items():
        r = rows[key]
        assert (r["label"], r["wire_model"], r["vendor"], r["family"], r["provider"], r["api_style"]) == (
            label, wire, "anthropic", "anthropic", "anthropic", "anthropic_messages"), key
        assert r["bedrock_profile_ids"] == {} and list(r["callable_via"]) == ["llm_client"]
        assert r["enabled"] is True and r["blocked_reason"] is None and r["supports_temperature"] is False
        assert r["max_output_tokens"] == 128000 and r["updated_by"] == ACTOR
        assert [float(r[c]) for c in ("p_in", "p_out", "p_read", "p_write")] == [p_in, p_out, p_read, p_write], key
        assert r["price_source"].startswith("Anthropic first-party list price")


def test_the_opus_4x_candidates_are_switched_on_and_their_acc3_rows_stay_off(cur):
    cur.execute(_body())
    cur.execute("SELECT m.model_key, m.enabled, m.blocked_reason, d.enabled AS acc3_on FROM shared.llm_model_catalog m "
                "JOIN shared.llm_model_deployment d ON d.model_key = m.model_key AND d.connection_key = 'acc3' "
                "WHERE m.model_key = ANY (%s)", (list(CANDIDATES),))
    rows = {r["model_key"]: r for r in cur.fetchall()}
    assert set(rows) == set(CANDIDATES)
    assert all(r["enabled"] and r["blocked_reason"] is None and r["acc3_on"] is False for r in rows.values())


def test_an_admin_change_of_a_candidate_is_kept(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET updated_by = 'admin', blocked_reason = 'kept off by the admin' "
                "WHERE model_key = 'opus-4-5'")
    cur.execute(_body())
    cur.execute("SELECT enabled, blocked_reason FROM shared.llm_model_catalog WHERE model_key = 'opus-4-5'")
    assert dict(cur.fetchone()) == {"enabled": False, "blocked_reason": "kept off by the admin"}
    assert "admin changed since 367, left as they are: opus-4-5" in " ".join(cur.connection.notices)


def test_nine_api_deployments_behind_every_aws_deployment(cur):
    cur.execute(_body())
    cur.execute("SELECT model_key, wire_id, priority, enabled, origin, access_state, request_defaults, "
                "price_in_override, price_out_override, price_cache_read_override AS p_read, "
                "price_cache_write_override AS p_write FROM shared.llm_model_deployment "
                "WHERE connection_key = 'anthropic-direct' AND updated_by = %s", (ACTOR,))
    rows = {r["model_key"]: r for r in cur.fetchall()}
    assert {k: r["wire_id"] for k, r in rows.items()} == WIRES
    for key, r in rows.items():
        assert (r["priority"], r["enabled"], r["origin"], r["access_state"]) == (100, True, "seed", "unknown"), key
        assert r["price_in_override"] is None and r["price_out_override"] is None
    assert rows["opus-4-5"]["request_defaults"] == {"effort": "high"}
    assert all(r["request_defaults"] == {} for k, r in rows.items() if k != "opus-4-5")
    assert (float(rows["sonnet-4-6"]["p_read"]), float(rows["sonnet-4-6"]["p_write"])) == (0.3, 3.75)
    assert (float(rows["haiku-4-5"]["p_read"]), float(rows["haiku-4-5"]["p_write"])) == (0.1, 1.25)
    cur.execute("SELECT d.model_key FROM shared.llm_model_deployment d "
                "JOIN shared.llm_model_deployment a ON a.model_key = d.model_key "
                "JOIN shared.llm_provider_connection c ON c.connection_key = a.connection_key "
                "WHERE d.connection_key = 'anthropic-direct' AND c.data_scope = 'aws_only' "
                "AND d.priority <= a.priority")
    assert cur.fetchall() == []
    # the five new models are reached only by the Anthropic API
    cur.execute("SELECT model_key, array_agg(connection_key) AS c FROM shared.llm_model_deployment "
                "WHERE model_key = ANY (%s) GROUP BY 1", (list(NEW_ROWS),))
    assert {r["model_key"]: list(r["c"]) for r in cur.fetchall()} == {k: ["anthropic-direct"] for k in NEW_ROWS}


def test_an_admin_deployment_row_is_kept(cur):
    cur.execute("INSERT INTO shared.llm_model_deployment (model_key, connection_key, wire_id, priority, enabled, "
                "origin, updated_by) VALUES ('sonnet-4-6', 'anthropic-direct', 'claude-sonnet-4-6', 5, false, "
                "'admin', 'admin')")
    cur.execute(_body())
    cur.execute("SELECT priority, enabled, origin FROM shared.llm_model_deployment "
                "WHERE model_key = 'sonnet-4-6' AND connection_key = 'anthropic-direct'")
    assert dict(cur.fetchone()) == {"priority": 5, "enabled": False, "origin": "admin"}


def test_the_routes_become_the_expected_routes_and_a_second_run_changes_nothing(cur):
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert len(_decided()) == 20
    assert _changed(cur) == _decided()
    cur.execute("SELECT max(version) AS v FROM shared.llm_route_config_version")
    version = cur.fetchone()["v"]
    cur.execute(_body())                                                    # re-apply: nothing left to do
    assert _routes(cur) == _expected()
    cur.execute("SELECT max(version) AS v FROM shared.llm_route_config_version")
    assert cur.fetchone()["v"] == version                                   # and no second version


def test_every_active_route_stays_within_the_six_attempts_and_the_old_ones_would_not(cur):
    cur.execute(_body())
    after = _attempts(cur)
    assert after and max(after.values()) <= MAX_ATTEMPTS, after
    assert after["tp_compose"] == 6 and after["s1_generate"] == 5 and after["a3_atomize"] == 5
    # the 366 routes on the post-376 deployments: every decided stage would pass the cap, none other would
    _load_routes(cur)
    over = {s for s, n in _attempts(cur).items() if n > MAX_ATTEMPTS}
    assert over == set(_decided())


def test_every_changed_route_keeps_a_model_with_an_enabled_way_in(cur):
    cur.execute(_body())
    cur.execute("""
        SELECT rc.stage AS s FROM shared.llm_role_config rc
         WHERE rc.updated_by = %s AND NOT EXISTS (
               SELECT 1 FROM unnest(ARRAY[rc.model_id] || rc.fallback_model_ids) AS u (key)
                 JOIN shared.llm_model_catalog m ON m.model_key = u.key AND m.enabled
                 JOIN shared.llm_model_deployment d ON d.model_key = u.key AND d.enabled
                 JOIN shared.llm_provider_connection c ON c.connection_key = d.connection_key AND c.enabled
                WHERE c.data_scope = ANY (rc.allowed_scopes))""", (ACTOR,))
    assert cur.fetchall() == []


def test_a_route_an_admin_changed_since_is_left_as_it_is(cur):
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['haiku-4-5', 'nova-lite'] "
                "WHERE stage = 'a3_place_days'")                            # already within the cap: kept
    cur.execute("UPDATE shared.llm_role_config SET is_active = false, disabled_reason = 'test' "
                "WHERE stage = 't9_repair'")                                # switched off: skipped
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = 't2_flag_fix'")
    cur.execute(_body())
    assert _fallbacks(cur, "a3_place_days") == ["haiku-4-5", "nova-lite"]
    assert _fallbacks(cur, "t9_repair") == ["haiku-4-5", "sonnet-4-6", "gpt-6-luna-openai"]
    assert set(_decided()) - set(_changed(cur)) == {"a3_place_days", "t9_repair", "t2_flag_fix"}
    notices = " ".join(cur.connection.notices)
    assert "left as they are" in notices and "t9_repair" in notices


def test_an_admin_route_the_new_deployments_would_cut_short_refuses_the_file(cur):
    # 5 possible attempts today (Sonnet 5.5 x2, Haiku 4.5, Sonnet 4.6, the OpenAI API), 7 with the 4.x API rows
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = "
                "ARRAY['haiku-4-5', 'sonnet-4-6', 'gpt-6-luna-openai'] WHERE stage = 't9_write'")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException, match="t9_write .* 7 possible attempts, more than 6"):
        cur.execute(_body())
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_a_chain_model_switched_off_in_the_catalog_refuses_the_file(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET enabled = false WHERE model_key = 'llama4-scout'")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException, match="not enabled catalog rows callable by llm_client: "
                                                             "llama4-scout"):
        cur.execute(_body())
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_a_new_key_taken_by_another_kind_of_row_refuses_the_file(cur):
    cur.execute("INSERT INTO shared.llm_model_catalog (model_key, label, vendor, provider, api_style, "
                "bedrock_profile_ids, callable_via, supports_temperature, enabled, updated_by) VALUES "
                "('opus-5', 'x', 'anthropic', 'bedrock', 'converse', '{}', ARRAY['llm_client'], false, false, 't')")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException, match="opus-5 exists and is not an Anthropic Messages API row"):
        cur.execute(_body())
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_one_external_route_version_records_exactly_what_changed(cur):
    previous = _live(cur)
    cur.execute("SELECT current_version AS v FROM shared.llm_route_settings WHERE singleton")
    before_version = cur.fetchone()["v"]
    cur.execute("SELECT snapshot FROM shared.llm_route_config_version WHERE version = %s", (before_version,))
    previous_snapshot = cur.fetchone()["snapshot"]
    cur.execute(_body())
    cur.execute("SELECT current_version AS v FROM shared.llm_route_settings WHERE singleton")
    assert cur.fetchone()["v"] == before_version + 1
    cur.execute("SELECT created_by, action, preset, snapshot, diff FROM shared.llm_route_config_version "
                "WHERE version = %s", (before_version + 1,))
    row = cur.fetchone()
    assert (row["created_by"], row["action"], row["preset"]) == (ACTOR, "external", None)
    live = _live(cur)
    # the app's own reading of the live rows: nothing left to record (route_versions.begin finds no change)
    assert route_versions.normalize(row["snapshot"]) == route_versions.normalize(live)
    assert route_versions.diff(row["snapshot"], live) == []
    # the stored diff is the app's diff, in the app's order
    assert row["diff"] == route_versions.diff(previous_snapshot, live)
    kinds = {(c["kind"], c.get("stage") or c.get("model")) for c in row["diff"]}
    assert {("route", s) for s in _decided()} <= kinds
    assert {("provider_order", k) for k in NEW_ROWS} <= kinds
    assert ("provider_order", "sonnet-4-6") in kinds and ("provider_order", "haiku-4-5") in kinds
    assert previous["priorities"]["sonnet-4-6"] == {"acc3": 10}
