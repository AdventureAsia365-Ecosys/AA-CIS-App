"""AA-1024 lane gw-core (S263) — migrations 334 (route deadline per role), 335 (segment research stages), 336 (retire
three dead routes) and 337 (Llama 4 structured output in the prompt) against real Postgres.

conftest applies every migration, these four included. Other modules re-run earlier files (320, 324) or load route
snapshots, so a test never trusts the applied state: it sets up the state the file expects, replays the file's body
(BEGIN / COMMIT stripped) inside the fixture's transaction and checks the result. Every test rolls back what it
wrote. The traffic tests use the real stage keys (route names, not data).
"""
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

MIGRATIONS = Path(__file__).resolve().parents[2] / "api" / "migrations"
M334 = MIGRATIONS / "334_llm_route_deadlines.sql"
M335 = MIGRATIONS / "335_llm_segment_research_stages.sql"
M336 = MIGRATIONS / "336_llm_retire_dead_routes.sql"
M337 = MIGRATIONS / "337_llm_llama4_structured_prompt_mode.sql"
DEADLINE = {"writer": 1200, "judge": 900, "validate": 600, "embed": 120}
SEGMENT = ("segment_research", "segment_research_batch")
DEAD = ("s1_atom_writer", "tp_component_embed", "tp_extract")
LLAMA = ("llama4-maverick", "llama4-scout")


def _body(path: Path) -> str:
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


def _registered(c, version: str) -> bool:
    c.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = %s", (version,))
    return c.fetchone() is not None


# ── 334: route deadline per role ─────────────────────────────────────────────────────────────────────────────────

def _deadlines(c) -> dict:
    c.execute("SELECT stage, role, route_deadline_s AS deadline FROM shared.llm_role_config")
    return {r["stage"]: (r["role"], r["deadline"]) for r in c.fetchall()}


def test_334_every_row_gets_the_deadline_of_its_role(cur):
    assert _registered(cur, "334")
    cur.execute("UPDATE shared.llm_role_config SET route_deadline_s = NULL")
    cur.execute(_body(M334))
    rows = _deadlines(cur)
    assert rows and all(deadline == DEADLINE[role] for role, deadline in rows.values()), rows


def test_334_replay_fills_null_rows_and_keeps_a_value_already_set(cur):
    cur.execute("UPDATE shared.llm_role_config SET route_deadline_s = NULL")
    cur.execute("SELECT stage FROM shared.llm_role_config WHERE role = 'judge' ORDER BY stage LIMIT 1")
    kept = cur.fetchone()["stage"]
    cur.execute("UPDATE shared.llm_role_config SET route_deadline_s = 77 WHERE stage = %s", (kept,))
    cur.execute("SELECT updated_by AS who FROM shared.llm_role_config WHERE stage = %s", (kept,))
    who = cur.fetchone()["who"]
    cur.execute(_body(M334))
    rows = _deadlines(cur)
    assert rows.pop(kept) == ("judge", 77)
    assert all(deadline == DEADLINE[role] for role, deadline in rows.values())
    cur.execute("SELECT updated_by AS who FROM shared.llm_role_config WHERE stage = %s", (kept,))
    assert cur.fetchone()["who"] == who                     # provenance of the route is not touched
    cur.execute(_body(M334))                                 # a second run changes nothing
    assert _deadlines(cur)[kept] == ("judge", 77)


# ── 335: segment research stages ─────────────────────────────────────────────────────────────────────────────────

def _segment(c) -> dict:
    c.execute("SELECT stage, role, provider, model_id, account_route, is_active, fallback_model_ids AS fallbacks, "
              "shadow_sample_pct AS shadow, allowed_scopes AS scopes, route_deadline_s AS deadline, "
              "degrade_mode AS degrade FROM shared.llm_role_config WHERE stage = ANY (%s) ORDER BY stage",
              (list(SEGMENT),))
    return {r["stage"]: dict(r) for r in c.fetchall()}


def test_335_creates_the_two_writer_stages(cur):
    assert _registered(cur, "335")
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = ANY (%s)", (list(SEGMENT),))
    cur.execute(_body(M335))
    rows = _segment(cur)
    assert set(rows) == set(SEGMENT)
    for stage, row in rows.items():
        assert row == {"stage": stage, "role": "writer", "provider": "claude", "model_id": "haiku",
                       "account_route": "acc3", "is_active": True, "fallbacks": [], "shadow": 0,
                       "scopes": ["aws_only"], "deadline": 1200, "degrade": "queue_retry"}


def test_335_replay_recreates_a_missing_row_and_keeps_an_edited_one(cur):
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = 'segment_research_batch'")
    cur.execute("UPDATE shared.llm_role_config SET model_id = 'sonnet' WHERE stage = 'segment_research'")
    cur.execute(_body(M335))
    rows = _segment(cur)
    assert rows["segment_research_batch"]["model_id"] == "haiku"
    assert rows["segment_research"]["model_id"] == "sonnet"


# ── 336: retire three dead routes ────────────────────────────────────────────────────────────────────────────────

def _switches(c) -> dict:
    c.execute("SELECT stage, is_active, disabled_reason AS reason FROM shared.llm_role_config")
    return {r["stage"]: (r["is_active"], r["reason"]) for r in c.fetchall()}


def _ledger(c, stage: str, days_ago: int) -> None:
    c.execute("INSERT INTO shared.llm_call_log (stage, role, model, account, quality_signal, created_at) "
              "VALUES (%s, 'writer', 'sonnet', 'acc3', '{}'::jsonb, now() - make_interval(days => %s))",
              (stage, days_ago))


def test_336_the_three_dead_routes_are_off_with_a_reason(cur):
    assert _registered(cur, "336")
    t10_before = _switches(cur).get("t10_claims")
    cur.execute(_body(M336))
    rows = _switches(cur)
    for stage in DEAD:
        active, reason = rows[stage]
        assert active is False and reason and "Retired" in reason
    assert t10_before is not None and rows["t10_claims"] == t10_before     # live validate stage, untouched


def test_336_replay_switches_off_and_leaves_every_other_stage(cur):
    cur.execute("UPDATE shared.llm_role_config SET is_active = true, disabled_reason = NULL WHERE stage = ANY (%s)",
                (list(DEAD),))
    before = _switches(cur)
    _ledger(cur, "tp_extract", 45)                            # older than 30 days: history, not a caller
    cur.execute(_body(M336))
    after = _switches(cur)
    assert {s: v for s, v in after.items() if s not in DEAD} == {s: v for s, v in before.items() if s not in DEAD}
    assert all(after[s][0] is False for s in DEAD)
    cur.execute("SELECT count(*) AS n FROM shared.llm_role_config WHERE stage = ANY (%s) "
                "AND updated_by = 'migration-336-aa1024'", (list(DEAD),))
    assert cur.fetchone()["n"] == 3


def test_336_refuses_a_stage_with_recent_ledger_traffic(cur):
    cur.execute("UPDATE shared.llm_role_config SET is_active = true, disabled_reason = NULL WHERE stage = ANY (%s)",
                (list(DEAD),))
    _ledger(cur, "s1_atom_writer", 3)
    with pytest.raises(psycopg2.errors.RaiseException, match="still has ledger traffic"):
        cur.execute(_body(M336))


def test_336_a_missing_row_is_not_an_error(cur):
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = 'tp_extract'")
    cur.execute("UPDATE shared.llm_role_config SET is_active = true, disabled_reason = NULL WHERE stage = ANY (%s)",
                (list(DEAD),))
    cur.execute(_body(M336))
    rows = _switches(cur)
    assert "tp_extract" not in rows and rows["s1_atom_writer"][0] is False


# ── 337: Llama 4 asked in the prompt ─────────────────────────────────────────────────────────────────────────────

def _caps(c) -> dict:
    c.execute("SELECT model_key, capabilities AS caps FROM shared.llm_model_catalog WHERE model_key = ANY (%s)",
              (list(LLAMA),))
    return {r["model_key"]: r["caps"] for r in c.fetchall()}


def test_337_both_llama_rows_state_the_prompt_mode(cur):
    assert _registered(cur, "337")
    cur.execute(_body(M337))
    for caps in _caps(cur).values():
        assert caps["forced_tool_choice"] is False and caps["structured_output"] == "prompt"


def test_337_replay_from_the_rows_324_seeds_and_no_other_row_changes(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = '{}'::jsonb WHERE model_key = ANY (%s)",
                (list(LLAMA),))
    cur.execute("SELECT model_key, capabilities AS caps FROM shared.llm_model_catalog WHERE NOT (model_key = ANY (%s))",
                (list(LLAMA),))
    others = {r["model_key"]: r["caps"] for r in cur.fetchall()}
    cur.execute(_body(M337))
    assert _caps(cur) == {k: {"forced_tool_choice": False, "structured_output": "prompt"} for k in LLAMA}
    cur.execute("SELECT model_key, capabilities AS caps FROM shared.llm_model_catalog WHERE NOT (model_key = ANY (%s))",
                (list(LLAMA),))
    assert {r["model_key"]: r["caps"] for r in cur.fetchall()} == others


def test_337_a_stated_mode_is_kept(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = '{\"structured_output\": \"auto\"}'::jsonb "
                "WHERE model_key = 'llama4-maverick'")
    cur.execute(_body(M337))
    assert _caps(cur)["llama4-maverick"] == {"structured_output": "auto", "forced_tool_choice": False}


def test_337_guard_refuses_an_unknown_stated_mode(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = '{\"structured_output\": \"grammar\"}'::jsonb "
                "WHERE model_key = 'llama4-scout'")
    with pytest.raises(psycopg2.errors.RaiseException, match="known structured_output"):
        cur.execute(_body(M337))
