"""AA-784 (S252) — migration 286: the place-card draft (a3_place_card) moves from GPT-5.6 Luna (222) to GPT-6 Luna with
a GPT-5.6 Luna fallback; the Haiku check (a3_place_card_check) is untouched. Real Postgres.

conftest applies every migration (220 seeds both stages, 222 routes them, 170 seeds gpt-6-luna DISABLED, 286 enables it
and moves the draft). These tests run the file again directly, errors NOT swallowed: from the 222 row, twice
(idempotent), over a disabled gpt-6-luna (enabled), without the row, and the catalog guard. Every test restores the
post-286 state.
"""
import os
import sys

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "286_place_card_draft_gpt6_luna.sql")
STAGE = "a3_place_card"
POST_286 = ("gpt-6-luna", ["gpt-5.6-luna"], "validate", "openai", "acc3", True, None, 0)
# The check route after the replay: 222 set haiku -> sonnet, AA-1026 (migration 327) moved it to the Converse keys and
# migration 331 (AA-1026 I6) appended the acc2 fallback of each place card stage (another maker for each).
CHECK_ROUTE = ("haiku-5-5", ["sonnet-5-5", "llama4-scout"], "validate", "claude", "acc3", True, None, 0)
REPLAY_286 = ("gpt-6-luna", ["gpt-5.6-luna", "nova-lite"], "validate", "openai", "acc3", True, None, 0)


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SELECT bedrock_profile_ids::text, price_in_per_mtok, price_out_per_mtok FROM shared.llm_model_catalog "
              "WHERE model_key = 'gpt-6-luna'")
    catalog = c.fetchone()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        _restore(c, catalog)
        conn.close()


def _restore(c, catalog):
    """The post-286 state other tests see."""
    profiles, price_in, price_out = catalog
    c.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = %s::jsonb, price_in_per_mtok = %s, "
              "price_out_per_mtok = %s, api_style = 'converse' WHERE model_key = 'gpt-6-luna'",
              (profiles, price_in, price_out))
    c.execute("""INSERT INTO shared.llm_role_config
                     (stage, role, provider, model_id, account_route, is_active, fallback_model_ids,
                      shadow_model_id, shadow_sample_pct, updated_by)
                 VALUES (%s, 'validate', 'openai', 'gpt-5.6-luna', 'acc3', true, ARRAY['sonnet']::text[], NULL, 0,
                         'migration-222-aa772')
                 ON CONFLICT (stage) DO NOTHING""", (STAGE,))
    c.execute("DELETE FROM shared.schema_versions WHERE version = '286'")
    _run(c)


def _seed_222(c):
    c.execute("UPDATE shared.llm_role_config SET role = 'validate', provider = 'openai', model_id = 'gpt-5.6-luna', "
              "account_route = 'acc3', fallback_model_ids = ARRAY['sonnet']::text[], "
              "updated_by = 'migration-222-aa772' WHERE stage = %s", (STAGE,))
    c.execute("DELETE FROM shared.schema_versions WHERE version = '286'")


def _run(c):
    with open(MIGRATION, encoding="utf-8") as f:
        c.execute(f.read())


def _row(c, stage=STAGE):
    c.execute("SELECT model_id, fallback_model_ids, role, provider, account_route, is_active, shadow_model_id, "
              "shadow_sample_pct FROM shared.llm_role_config WHERE stage = %s", (stage,))
    return c.fetchone()


def _registered(c):
    c.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '286'")
    return c.fetchone()[0]


def _gpt6_enabled(c):
    c.execute("SELECT enabled, blocked_reason FROM shared.llm_model_catalog WHERE model_key = 'gpt-6-luna'")
    return c.fetchone()


def test_replay_state_is_post_286(cur):
    assert _row(cur) == REPLAY_286                              # 286's route, plus 331's acc2 fallback last
    assert _row(cur, "a3_place_card_check") == CHECK_ROUTE
    assert _gpt6_enabled(cur) == (True, None)
    assert _registered(cur) == 1


def test_moves_the_222_row_and_is_idempotent(cur):
    _seed_222(cur)
    assert _row(cur)[:2] == ("gpt-5.6-luna", ["sonnet"])
    _run(cur)
    assert _row(cur) == POST_286
    cur.execute("SELECT updated_by FROM shared.llm_role_config WHERE stage = %s", (STAGE,))
    assert cur.fetchone()[0] == "migration-286-aa784"
    _run(cur)                                                   # a re-run changes nothing
    assert _row(cur) == POST_286
    assert _row(cur, "a3_place_card_check") == CHECK_ROUTE        # the check route is never touched
    assert _registered(cur) == 1


def test_enables_a_disabled_gpt_6_luna(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET enabled = false, blocked_reason = 'test' "
                "WHERE model_key = 'gpt-6-luna'")
    _seed_222(cur)
    _run(cur)
    assert _gpt6_enabled(cur) == (True, None)
    assert _row(cur) == POST_286


def test_no_row_is_a_notice_not_an_error(cur):
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = %s", (STAGE,))
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '286'")
    _run(cur)
    assert _row(cur) is None and _registered(cur) == 1


@pytest.mark.parametrize("change", [
    ("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = bedrock_profile_ids - 'acc3' "
     "WHERE model_key = 'gpt-6-luna'"),
    "UPDATE shared.llm_model_catalog SET api_style = 'openai_chat' WHERE model_key = 'gpt-6-luna'",
    ("UPDATE shared.llm_model_catalog SET enabled = false, price_in_per_mtok = NULL "
     "WHERE model_key = 'gpt-6-luna'"),
])
def test_guard_refuses_an_unroutable_gpt_6_luna(cur, change):
    _seed_222(cur)
    cur.execute(change)
    with pytest.raises(psycopg2.errors.RaiseException, match="migration 286"):
        _run(cur)
    cur.execute("ROLLBACK")
    assert _row(cur)[:2] == ("gpt-5.6-luna", ["sonnet"])        # nothing moved
    assert _registered(cur) == 0
