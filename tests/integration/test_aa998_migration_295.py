"""AA-998 (S255) — migration 295 (the gateway data drops acc1: catalog profiles, account_route CHECK) against real
Postgres.

conftest applies every migration (169 / 259 seed acc1 profiles on haiku, sonnet, haiku-5-5, sonnet-5-5; 137 allows
account_route 'acc1'; 295 removes both). These tests run the file again directly, errors NOT swallowed: from the
pre-295 state, twice (idempotent), and each guard. Every test restores the post-295 state.
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "295_llm_acc3_only.sql")
SEEDED = ("haiku", "sonnet", "haiku-5-5", "sonnet-5-5")   # the rows 169 / 259 gave an acc1 profile
OLD_CHECK = "CHECK (account_route IN ('acc1', 'acc3') OR account_route IS NULL)"   # 137
STAGE = "zz_aa998_test_stage"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.llm_role_config WHERE stage = %s", (STAGE,))
        c.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = 'zz-aa998-only-acc1'")
        _run(c)                                 # back to the post-295 state for other tests
        conn.close()


def _run(c):
    c.execute(open(MIGRATION, encoding="utf-8").read())


def _profiles(c):
    c.execute("SELECT model_key, bedrock_profile_ids, updated_by FROM shared.llm_model_catalog "
              "WHERE model_key = ANY (%s)", (list(SEEDED),))
    return {k: (p, u) for k, p, u in c.fetchall()}


def _undo(c):
    """The pre-295 state: each seeded row has its acc1 profile (same id as acc3), the 137 CHECK."""
    c.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = bedrock_profile_ids || "
              "jsonb_build_object('acc1', bedrock_profile_ids ->> 'acc3'), updated_by = 'pre-295' "
              "WHERE model_key = ANY (%s) AND bedrock_profile_ids ? 'acc3'", (list(SEEDED),))
    c.execute("ALTER TABLE shared.llm_role_config DROP CONSTRAINT llm_role_config_account_route_check")
    c.execute(f"ALTER TABLE shared.llm_role_config ADD CONSTRAINT llm_role_config_account_route_check {OLD_CHECK}")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '295'")


def _insert_stage(c, account_route):
    c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, account_route, is_active, "
              "updated_at, updated_by) VALUES (%s, 'writer', 'claude', 'haiku', %s, false, now(), 'test')",
              (STAGE, account_route))


def test_post_replay_state_has_no_acc1(cur):
    cur.execute("SELECT model_key FROM shared.llm_model_catalog WHERE bedrock_profile_ids ? 'acc1'")
    assert cur.fetchall() == []
    cur.execute("SELECT count(*) FROM shared.llm_role_config WHERE account_route = 'acc1'")
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'llm_role_config_account_route_check'")
    definition = cur.fetchone()[0]
    assert "acc3" in definition and "acc1" not in definition
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '295'")
    assert cur.fetchone()


def test_check_refuses_acc1_and_allows_acc3_or_null(cur):
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_stage(cur, "acc1")
    cur.execute("ROLLBACK")
    for value in ("acc3", None):
        _insert_stage(cur, value)
        cur.execute("DELETE FROM shared.llm_role_config WHERE stage = %s", (STAGE,))


def test_removes_acc1_profiles_from_the_pre_295_state_and_is_idempotent(cur):
    _undo(cur)
    before = _profiles(cur)
    assert before and all("acc1" in p for p, _ in before.values())
    _run(cur)
    after = _profiles(cur)
    for key, (profiles, updated_by) in after.items():
        assert "acc1" not in profiles
        assert profiles == {k: v for k, v in before[key][0].items() if k != "acc1"}   # acc3 (and acc2) kept
        assert updated_by == "migration-295-aa998"
    cur.execute("UPDATE shared.llm_model_catalog SET updated_by = 'after-first-run' WHERE model_key = ANY (%s)",
                (list(SEEDED),))
    _run(cur)                                   # second run: no change, no error
    assert all(u == "after-first-run" for _, u in _profiles(cur).values())


def test_guard_refuses_a_row_whose_only_profile_is_acc1(cur):
    cur.execute("INSERT INTO shared.llm_model_catalog (model_key, label, vendor, provider, api_style, "
                "bedrock_profile_ids, callable_via, supports_temperature, enabled, updated_at, updated_by) "
                "VALUES ('zz-aa998-only-acc1', 'x', 'anthropic', 'bedrock', 'converse', '{\"acc1\": \"p\"}', "
                "'{llm_client}', false, false, now(), 'test')")
    with pytest.raises(psycopg2.Error, match="only profile is acc1: zz-aa998-only-acc1"):
        _run(cur)
    cur.execute("ROLLBACK")                     # the file's own BEGIN is still open after the raise


def test_guard_refuses_while_a_stage_routes_to_acc1(cur):
    _undo(cur)
    _insert_stage(cur, "acc1")
    with pytest.raises(psycopg2.Error, match=f"stages still routed to acc1: {STAGE}"):
        _run(cur)
    cur.execute("ROLLBACK")
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = %s", (STAGE,))
