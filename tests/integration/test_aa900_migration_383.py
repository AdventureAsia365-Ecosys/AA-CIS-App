"""AA-900 / AA-741 (S271 W0, tech lead D7) — migration 383 against real Postgres: GPT-6 Luna on the OpenAI API
(``gpt-6-luna-openai``) off every writer route, Nova Lite (``nova-lite``) off every judge route and the t10_claims
instrument, ``degrade_mode`` queue_retry on the changed writers that had none, and one external route config version.

conftest applies every migration, 383 included. Each test sets 383's rows aside inside one transaction (the post-376
state), loads the routes 376 decided (``tests/unit/snapshots/aa1042_routes_after_376.json``) with the degrade modes
Dev has (326 / 335), runs the file's body and compares with ``tests/unit/snapshots/aa900_routes_after_383.json`` (the
table the unit lint ``tests/unit/test_aa900_w0_route_lint.py`` checks). Then the route rules run on the rows the
database holds: ``llm_route_chains.violations`` (catalog, deployments and connections read from the same database)
and the admin Routing tab's ``llm_route_control.chain_problems`` on every changed row. Every test rolls back.
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
M383 = ROOT / "api" / "migrations" / "383_llm_w0_remove_uncalibrated_rungs.sql"
SNAPSHOTS = ROOT / "tests" / "unit" / "snapshots"
BEFORE = SNAPSHOTS / "aa1042_routes_after_376.json"
EXPECTED = SNAPSHOTS / "aa900_routes_after_383.json"
COLUMNS = ("stage, role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
           "shadow_sample_pct, allowed_scopes")
ACTOR = "migration-383-aa900"
WRITER_RUNG = "gpt-6-luna-openai"
JUDGE_RUNG = "nova-lite"
# the degrade modes Dev holds before 383 (326: t2_generate, t9_write, tp_compose; 335: segment research)
DEV_DEGRADE = {"t2_generate": "queue_retry", "t9_write": "queue_retry", "tp_compose": "basic_narration",
               "segment_research": "queue_retry", "segment_research_batch": "queue_retry"}
WRITERS = ("a0_column_map", "s1_flag_fix", "s1_generate", "s1_itinerary_nudge", "segment_research",
           "segment_research_batch", "supply_content_writer", "t2_flag_fix", "t2_generate", "t2_itinerary_nudge",
           "t8_angle_gen", "t9_repair", "t9_write")
INSTRUMENTS = ("s1_brand_audit", "s1_judge", "t10_adjudicate", "t10_claims", "t10_judge", "t2_brand_audit",
               "t2_judge", "t7_debate_judge", "writing_memory_eval", "writing_memory_mine")


def _body() -> str:
    return M383.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


def _set_383_aside(c) -> None:
    """The state 383 starts from on Dev (post-376), inside the test's transaction."""
    # AA-1095: the deployments later migrations seeded (406's acc1 rows) did not exist then; attempt_cap counts them
    set_later_deployments_aside(c, 383)
    # One statement from 383's first version on: a later revert row may point at it (reverted_from FK).
    c.execute("DELETE FROM shared.llm_route_config_version WHERE version >= "
              "(SELECT min(version) FROM shared.llm_route_config_version WHERE created_by = %s)", (ACTOR,))
    c.execute("UPDATE shared.llm_route_settings SET current_version = "
              "(SELECT max(version) FROM shared.llm_route_config_version) WHERE singleton")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '383'")


def _load_routes(c) -> None:
    c.execute("DELETE FROM shared.llm_role_config")
    for r in json.loads(BEFORE.read_text(encoding="utf-8"))["routes"]:
        c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
                  "fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by, allowed_scopes, degrade_mode) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'test-aa900-383', %s, %s)",
                  (r["stage"], r["role"], r["provider"], r["model_id"], r["account_route"], r["is_active"],
                   r["fallback_model_ids"], r["shadow_model_id"], r["shadow_sample_pct"], r["allowed_scopes"],
                   DEV_DEGRADE.get(r["stage"])))


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("BEGIN")
    _set_383_aside(c)
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


def _registry(c):
    """The catalog, reach, per-deployment scopes and acc2-only chat models as the admin reads them (every deployment
    row, enabled or not), from this database."""
    from shared.llm_client.catalog import _row_to_model
    c.execute("SELECT * FROM shared.llm_model_catalog")
    models = [_row_to_model(r) for r in c.fetchall()]
    c.execute("SELECT d.model_key AS k, d.connection_key AS conn, p.data_scope AS scope "
              "FROM shared.llm_model_deployment d JOIN shared.llm_provider_connection p "
              "ON p.connection_key = d.connection_key")
    reach: dict = {}
    scopes: dict = {}
    where: dict = {}
    for r in c.fetchall():
        reach.setdefault(r["k"], set()).add(r["scope"])
        scopes.setdefault(r["k"], []).append(r["scope"])
        where.setdefault(r["k"], set()).add(r["conn"])
    styles = {m.model_key: m.api_style for m in models}
    acc2_only = {k for k, cs in where.items() if cs == {"acc2"} and styles.get(k) != "embed"}
    return models, {k: frozenset(v) for k, v in reach.items()}, scopes, acc2_only


def _rows(c) -> list[dict]:
    c.execute("SELECT stage, role, provider, model_id, account_route, fallback_model_ids, shadow_model_id, "
              "shadow_sample_pct, allowed_scopes, is_active, pii_class FROM shared.llm_role_config")
    return [{**dict(r), "fallback_model_ids": list(r["fallback_model_ids"] or []),
             "allowed_scopes": list(r["allowed_scopes"]) if r["allowed_scopes"] else None} for r in c.fetchall()]


# ── the file ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_registered_by_the_replay():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    try:
        c = conn.cursor()
        c.execute("SELECT version FROM shared.schema_versions WHERE version = '383'")
        assert c.fetchone() == ("383",)
    finally:
        conn.close()


def test_the_routes_become_the_expected_routes_and_a_second_run_changes_nothing(cur):
    cur.execute(_body())
    assert _routes(cur) == _expected()
    assert _decided() == sorted(WRITERS + INSTRUMENTS)
    assert _changed(cur) == _decided()
    cur.execute("SELECT max(version) AS v FROM shared.llm_route_config_version")
    version = cur.fetchone()["v"]
    cur.execute(_body())                                                    # re-apply: nothing left to do
    assert _routes(cur) == _expected()
    cur.execute("SELECT max(version) AS v FROM shared.llm_route_config_version")
    assert cur.fetchone()["v"] == version                                   # and no second version


def test_no_writer_keeps_the_openai_rung_and_no_instrument_keeps_nova_lite(cur):
    cur.execute(_body())
    cur.execute("SELECT stage AS s FROM shared.llm_role_config WHERE role = 'writer' AND %s = ANY "
                "(ARRAY[model_id] || fallback_model_ids)", (WRITER_RUNG,))
    assert cur.fetchall() == []
    cur.execute("SELECT stage AS s FROM shared.llm_role_config WHERE (role = 'judge' OR stage = 't10_claims') "
                "AND %s = ANY (ARRAY[model_id] || fallback_model_ids)", (JUDGE_RUNG,))
    assert cur.fetchall() == []
    # kept on purpose: the FAQ checks and the A3 / S1 evidence producers (validate rows, the lint's allowlist)
    cur.execute("SELECT stage AS s FROM shared.llm_role_config WHERE %s = ANY (fallback_model_ids) ORDER BY 1",
                (JUDGE_RUNG,))
    assert [r["s"] for r in cur.fetchall()] == ["a3_atomize", "a3_place_card", "a3_place_days",
                                                "a3_place_dedupe_check", "a3_place_locate", "s1_source_facts",
                                                "t9_faq_answer_fit", "t9_faq_filter"]


def test_the_changed_writers_declare_queue_retry_and_no_other_degrade_mode_moves(cur):
    cur.execute(_body())
    cur.execute("SELECT stage, degrade_mode AS d FROM shared.llm_role_config")
    modes = {r["stage"]: r["d"] for r in cur.fetchall()}
    assert all(modes[s] == "queue_retry" for s in WRITERS), modes
    assert modes["tp_compose"] == "basic_narration"                         # TripPlanner's degrade, untouched
    assert all(modes[s] is None for s in INSTRUMENTS)
    assert {s for s, d in modes.items() if d} == set(WRITERS) | {"tp_compose"}
    assert "degrade_mode queue_retry set on: a0_column_map, s1_flag_fix, s1_generate, s1_itinerary_nudge, " \
           "supply_content_writer, t2_flag_fix, t2_itinerary_nudge, t8_angle_gen, t9_repair" \
           in " ".join(cur.connection.notices)


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


# ── the route rules on the rows the database holds after the file ──────────────────────────────────────────────────

def test_the_chain_rules_hold_on_every_changed_row_and_nothing_new_breaks(cur):
    from api.services.llm_route_chains import violations
    models, reach, scopes, acc2_only = _registry(cur)
    before = set(violations(_rows(cur), models, reach, acc2_only, scopes))
    cur.execute(_body())
    after = violations(_rows(cur), models, reach, acc2_only, scopes)
    changed = set(_changed(cur))
    assert [v for v in after if v[0] in changed] == []
    assert set(after) <= before                         # the rows not changed here report what they reported


def test_the_admin_routing_rules_accept_every_changed_chain(cur):
    """llm_route_control.chain_problems: the rules the Routing tab's PATCH and the route control preview apply (the
    S235 vendor_last_resort rule, the writer / judge family options, data scopes, the AWS-before-vendor order)."""
    from api.services.llm_route_control import chain_problems
    models, reach, _, _ = _registry(cur)
    cur.execute(_body())
    rows = {r["stage"]: r for r in _rows(cur)}
    problems = {s: chain_problems(rows[s], [rows[s]["model_id"], *rows[s]["fallback_model_ids"]], models, reach,
                                  frozenset())
                for s in _changed(cur)}
    assert problems and all(p == [] for p in problems.values()), {s: p for s, p in problems.items() if p}


def test_the_admin_routing_rules_would_refuse_a_vendor_rung_ahead_of_aws(cur):
    """The control case: the same check refuses the order rule S235 forbids (the OpenAI API row before an AWS model
    on a writer), so the test above is not vacuous."""
    from api.services.llm_route_control import chain_problems
    models, reach, _, _ = _registry(cur)
    cur.execute(_body())
    row = next(r for r in _rows(cur) if r["stage"] == "t9_write")
    found = chain_problems(row, [WRITER_RUNG, "sonnet-5-5", "sonnet-4-6"], models, reach, frozenset())
    assert {p["rule"] for p in found} & {"vendor_last_resort", "not_selectable"}, found


# ── an admin's chain, refusals ─────────────────────────────────────────────────────────────────────────────────────

def test_an_admin_chain_loses_the_rung_too_and_is_reported(cur):
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['sonnet-4-6', 'haiku-4-5', %s] "
                "WHERE stage = 't9_write'", (WRITER_RUNG,))
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['gpt-6-luna', %s] "
                "WHERE stage = 't10_judge'", (JUDGE_RUNG,))
    cur.execute(_body())
    assert _fallbacks(cur, "t9_write") == ["sonnet-4-6", "haiku-4-5"]
    assert _fallbacks(cur, "t10_judge") == ["gpt-6-luna"]
    assert "post-376 ones (the rule was applied all the same): t10_judge, t9_write" in " ".join(cur.connection.notices)


def test_a_writer_whose_only_fallback_was_the_rung_gets_its_primarys_older_sibling(cur):
    """The replayed migrations (and an admin's chain) can leave a writer on [<5.5 primary>, gpt-6-luna-openai]: the
    file puts the 366 sibling there instead of leaving the stage with no fallback."""
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY[%s] "
                "WHERE stage IN ('t8_angle_gen', 't2_flag_fix')", (WRITER_RUNG,))
    cur.execute(_body())
    assert _fallbacks(cur, "t8_angle_gen") == ["sonnet-4-6"]               # primary sonnet-5-5
    assert _fallbacks(cur, "t2_flag_fix") == ["haiku-4-5"]                 # primary haiku-5-5
    assert _fallbacks(cur, "t9_repair") == ["haiku-4-5"]                   # the post-376 chain: rung removed only
    assert "given their primary's older sibling: t2_flag_fix, t8_angle_gen" in " ".join(cur.connection.notices)


def test_a_route_left_with_no_fallback_and_no_sibling_refuses_the_file(cur):
    cur.execute("UPDATE shared.llm_role_config SET model_id = 'opus-5-5', fallback_model_ids = ARRAY[%s] "
                "WHERE stage = 't8_angle_gen'", (WRITER_RUNG,))
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException, match="left with no fallback .*: t8_angle_gen"):
        cur.execute(_body())
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_a_primary_the_file_would_remove_refuses_the_file(cur):
    cur.execute("UPDATE shared.llm_role_config SET model_id = %s, fallback_model_ids = ARRAY['gpt-6-luna'] "
                "WHERE stage = 't2_judge'", (JUDGE_RUNG,))
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException, match="PRIMARY is a rung this file removes .*: t2_judge"):
        cur.execute(_body())
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_a_writer_with_another_family_rung_refuses_the_file(cur):
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['gpt-6-luna', 'sonnet-4-6'] "
                "WHERE stage = 's1_generate'")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException, match="another family than their primary: "
                                                             r"s1_generate \(gpt-6-luna\)"):
        cur.execute(_body())
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_one_external_route_version_records_exactly_what_changed(cur):
    cur.execute("SELECT current_version AS v FROM shared.llm_route_settings WHERE singleton")
    before_version = cur.fetchone()["v"]
    cur.execute("SELECT snapshot FROM shared.llm_route_config_version WHERE version = %s", (before_version,))
    previous_snapshot = cur.fetchone()["snapshot"]
    pre = _live(cur)    # the live state the body starts from (earlier test files may leave deployment drift)
    cur.execute(_body())
    cur.execute("SELECT current_version AS v FROM shared.llm_route_settings WHERE singleton")
    assert cur.fetchone()["v"] == before_version + 1
    cur.execute("SELECT created_by, action, preset, snapshot, diff FROM shared.llm_route_config_version "
                "WHERE version = %s", (before_version + 1,))
    row = cur.fetchone()
    assert (row["created_by"], row["action"], row["preset"]) == (ACTOR, "external", None)
    live = _live(cur)
    assert route_versions.normalize(row["snapshot"]) == route_versions.normalize(live)
    assert route_versions.diff(row["snapshot"], live) == []
    assert row["diff"] == route_versions.diff(previous_snapshot, live)
    prev = route_versions.normalize(previous_snapshot)["routes"]
    moved = {c.get("stage") for c in row["diff"] if c["kind"] == "route"}
    # every decided stage whose chain in the version before still named the removed rung is in the diff
    rung = {**{s: WRITER_RUNG for s in WRITERS}, **{s: JUDGE_RUNG for s in INSTRUMENTS}}
    assert {s for s in _decided() if rung[s] in (route_versions.chain_of(prev.get(s)) or [])} <= moved
    # 383 moves no deployment: the version's provider order entries are exactly the unversioned drift found at apply
    # time (the 376 recipe records it too), none of them new
    orders = [c for c in row["diff"] if c["kind"] == "provider_order"]
    assert orders == [c for c in route_versions.diff(previous_snapshot, pre) if c["kind"] == "provider_order"]
    assert pre["priorities"] == live["priorities"]
