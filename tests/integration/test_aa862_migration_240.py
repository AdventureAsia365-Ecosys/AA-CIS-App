"""AA-862 — migration 240 (Cohere Embed v4 acc2 -> acc3 fallback rows and routes) against real Postgres.

conftest applies every migration (169/172/173/226 seed the base row and the four embed routes, 238
moves acc2 to the global profile, 240 adds the satellite rows and the fallback). These tests run the
file again directly, errors NOT swallowed: from the pre-240 state, twice (idempotent), and each guard.
Every test restores the post-240 state.
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "240_cohere_embed_satellite_fallback.sql")
STAGES = ("f10_embed", "a3_place_embed", "tp_search_embed", "tp_component_embed")
ACC3, ACC1 = "cohere-embed-v4-acc3", "cohere-embed-v4-acc1"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        _undo(c)
        _run(c)                                 # back to the post-240 state for other tests
        conn.close()


def _undo(c):
    c.execute("UPDATE shared.llm_role_config SET fallback_model_ids = '{}', account_route = NULL, "
              "model_id = 'cohere-embed-v4' WHERE stage = ANY (%s)", (list(STAGES),))
    c.execute("DELETE FROM shared.llm_model_catalog WHERE model_key IN (%s, %s)", (ACC3, ACC1))


def _run(c):
    c.execute(open(MIGRATION, encoding="utf-8").read())


def _catalog(c, key):
    c.execute("SELECT bedrock_profile_ids, enabled, blocked_reason, price_in_per_mtok, price_out_per_mtok, "
              "vendor, api_style, callable_via, updated_by FROM shared.llm_model_catalog "
              "WHERE model_key = %s", (key,))
    return c.fetchone()


def _routes(c):
    c.execute("SELECT stage, model_id, account_route, fallback_model_ids, updated_by "
              "FROM shared.llm_role_config WHERE stage = ANY (%s) ORDER BY stage", (list(STAGES),))
    return {r[0]: r[1:] for r in c.fetchall()}


def test_conftest_replay_left_the_post_240_state(cur):
    acc3, acc1 = _catalog(cur, ACC3), _catalog(cur, ACC1)
    assert acc3[0] == {"acc3": "global.cohere.embed-v4:0"} and acc3[1] is True and acc3[2] is None
    assert acc1 is None                         # acc1 is not used (Nghiệp, S239)
    routes = _routes(cur)
    assert set(routes) == set(STAGES)
    for model_id, account_route, fallbacks, _ in routes.values():
        assert (model_id, account_route, fallbacks) == ("cohere-embed-v4", None, [ACC3])
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '240'")
    assert cur.fetchone()


def test_copies_the_base_row_and_is_idempotent(cur):
    _undo(cur)
    _run(cur)
    base = _catalog(cur, "cohere-embed-v4")
    for key in (ACC3,):
        row = _catalog(cur, key)
        assert row[3:8] == base[3:8]             # prices, vendor, api_style, callable_via
        assert row[8] == "migration-240-aa862"
    assert all(r[3] == "migration-240-aa862" for r in _routes(cur).values())

    cur.execute("UPDATE shared.llm_role_config SET updated_by = 'after-first-run' WHERE stage = ANY (%s)",
                (list(STAGES),))
    cur.execute("UPDATE shared.llm_model_catalog SET updated_by = 'after-first-run' "
                "WHERE model_key = %s", (ACC3,))
    _run(cur)                                   # second run: no change, no error
    assert all(r[3] == "after-first-run" for r in _routes(cur).values())
    assert _catalog(cur, ACC3)[8] == "after-first-run"
    assert _catalog(cur, ACC1) is None


def test_keeps_an_admin_disable_of_the_acc3_row(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET enabled = false WHERE model_key = %s", (ACC3,))
    _run(cur)
    assert _catalog(cur, ACC3)[1] is False


def test_guard_refuses_a_hand_edited_route(cur):
    cur.execute("UPDATE shared.llm_role_config SET fallback_model_ids = %s WHERE stage = 'tp_search_embed'",
                ([ACC3, ACC1],))
    with pytest.raises(psycopg2.Error, match="240: route tp_search_embed"):
        _run(cur)
    cur.execute("ROLLBACK")                     # the file's own BEGIN is still open after the raise
    assert _routes(cur)["tp_search_embed"][2] == [ACC3, ACC1]


def test_guard_refuses_a_route_with_an_account_route(cur):
    cur.execute("UPDATE shared.llm_role_config SET account_route = 'acc3' WHERE stage = 'f10_embed'")
    with pytest.raises(psycopg2.Error, match="240: route f10_embed"):
        _run(cur)


def test_guard_refuses_a_satellite_row_on_the_wrong_account(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = "
                "'{\"acc2\": \"global.cohere.embed-v4:0\"}' WHERE model_key = %s", (ACC3,))
    with pytest.raises(psycopg2.Error, match="240: cohere-embed-v4-acc3 already exists"):
        _run(cur)


def test_guard_refuses_a_missing_base_row(cur):
    cur.execute("BEGIN")
    cur.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = 'cohere-embed-v4'")
    with pytest.raises(psycopg2.Error, match="240: shared.llm_model_catalog has no cohere-embed-v4 row"):
        cur.execute(open(MIGRATION, encoding="utf-8").read().replace("BEGIN;", "", 1))
    cur.execute("ROLLBACK")                     # restores the base row
    assert _catalog(cur, "cohere-embed-v4") is not None
