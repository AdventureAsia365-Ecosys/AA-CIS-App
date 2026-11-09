"""AA-835 (S251 live run) — migration 282: supply_content_writer moves from the legacy key 'sonnet' (278's seed) to
the catalog key 'sonnet-5-5', legacy 'sonnet' as the last fallback, against real Postgres.

conftest applies every migration (278's part A seeds the row, 259 adds the sonnet-5-5 catalog row, 282 moves the
stage). These tests run the file again directly, errors NOT swallowed: from the 278 seed, twice (idempotent), over
an admin-changed row (left alone), without the row, and the catalog guard. Every test restores the post-282 state.
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
                         "282_supply_writer_sonnet_5_5.sql")
STAGE = "supply_content_writer"
POST_282 = ("sonnet-5-5", ["sonnet"], "writer", "claude", "acc3", True, None, 0)
# AA-1026 (migration 327, later in the replay): the legacy 'sonnet' becomes its chain on Converse keys (sonnet-5-5 is
# already first, then haiku-5-5), the A-series writer ends on the OpenAI API model, and haiku-5-5 is dropped again: it
# would follow sonnet-5-5's Anthropic API deployment (T-3: outside AWS only after every AWS path of the route).
# AA-1026 (migration 366, S267): the family chain, Sonnet 4.6 and Haiku 4.5 on acc3 before the OpenAI API (the gateway
# runs it model first: Sonnet 5.5 on acc3, then on the Anthropic API, then Sonnet 4.6 ...). AA-1042 (migration 376,
# S269): Sonnet 4.6 gained its Anthropic API deployment, so Haiku 4.5 left the chain to keep six attempts. AA-900
# (migration 383, S271 W0, tech lead D7): no writer route keeps GPT-6 Luna on the OpenAI API.
POST_366 = ("sonnet-5-5", ["sonnet-4-6"], "writer", "claude", "acc3", True, None, 0)


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        _restore(c)
        conn.close()


def _restore(c):
    """The post-282, then post-366 state other tests see (the replay's end state: 327 then 366 rewrote the row; their
    files are not re-run here, their guards check the whole route table, which other tests' rows may differ from)."""
    c.execute("UPDATE shared.llm_model_catalog SET enabled = true, blocked_reason = NULL "
              "WHERE model_key = 'sonnet-5-5'")
    c.execute("""INSERT INTO shared.llm_role_config
                     (stage, role, provider, model_id, account_route, is_active, fallback_model_ids,
                      shadow_model_id, shadow_sample_pct, updated_by)
                 VALUES (%s, 'writer', 'claude', 'sonnet', 'acc3', true, ARRAY[]::text[], NULL, 0, 'migration-278')
                 ON CONFLICT (stage) DO NOTHING""", (STAGE,))
    _seed_278(c)
    _run(c)
    c.execute("UPDATE shared.llm_role_config SET fallback_model_ids = %s::text[], "
              "allowed_scopes = ARRAY['aws_only', 'vendor_direct'], updated_by = 'migration-383-aa900' "
              "WHERE stage = %s", (POST_366[1], STAGE))


def _seed_278(c):
    c.execute("UPDATE shared.llm_role_config SET model_id = 'sonnet', fallback_model_ids = ARRAY[]::text[], "
              "updated_by = 'migration-278' WHERE stage = %s", (STAGE,))
    c.execute("DELETE FROM shared.schema_versions WHERE version = '282'")


def _run(c):
    with open(MIGRATION, encoding="utf-8") as f:
        c.execute(f.read())


def _row(c):
    c.execute("SELECT model_id, fallback_model_ids, role, provider, account_route, is_active, shadow_model_id, "
              "shadow_sample_pct FROM shared.llm_role_config WHERE stage = %s", (STAGE,))
    return c.fetchone()


def _registered(c):
    c.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '282'")
    return c.fetchone()[0]


def test_replay_state_is_post_366(cur):
    assert _row(cur) == POST_366
    assert _registered(cur) == 1


def test_moves_the_278_seed_and_is_idempotent(cur):
    _seed_278(cur)
    assert _row(cur)[:2] == ("sonnet", [])
    _run(cur)
    assert _row(cur) == POST_282
    cur.execute("SELECT updated_by FROM shared.llm_role_config WHERE stage = %s", (STAGE,))
    assert cur.fetchone()[0] == "migration-282-aa835"
    _run(cur)                                                   # a re-run changes nothing
    assert _row(cur) == POST_282
    assert _registered(cur) == 1


@pytest.mark.parametrize("model_id,fallbacks", [("sonnet-5", []), ("sonnet", ["haiku"]), ("sonnet-5-5", [])])
def test_leaves_an_admin_changed_row_alone(cur, model_id, fallbacks):
    cur.execute("UPDATE shared.llm_role_config SET model_id = %s, fallback_model_ids = %s::text[], "
                "updated_by = 'admin:test' WHERE stage = %s", (model_id, fallbacks, STAGE))
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '282'")
    _run(cur)
    cur.execute("SELECT model_id, fallback_model_ids, updated_by FROM shared.llm_role_config WHERE stage = %s",
                (STAGE,))
    assert cur.fetchone() == (model_id, fallbacks, "admin:test")
    assert _registered(cur) == 1


def test_no_row_is_a_notice_not_an_error(cur):
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = %s", (STAGE,))
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '282'")
    _run(cur)
    assert _row(cur) is None and _registered(cur) == 1


@pytest.mark.parametrize("change", [
    "UPDATE shared.llm_model_catalog SET enabled = false, blocked_reason = 'test' WHERE model_key = 'sonnet-5-5'",
    ("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = bedrock_profile_ids - 'acc3' "
     "WHERE model_key = 'sonnet-5-5'"),
])
def test_guard_refuses_an_unroutable_sonnet_5_5(cur, change):
    cur.execute("SELECT bedrock_profile_ids::text FROM shared.llm_model_catalog WHERE model_key = 'sonnet-5-5'")
    profiles = cur.fetchone()[0]
    _seed_278(cur)
    cur.execute(change)
    try:
        with pytest.raises(psycopg2.errors.RaiseException, match="migration 282"):
            _run(cur)
        cur.execute("ROLLBACK")
        assert _row(cur)[:2] == ("sonnet", [])                 # nothing moved
        assert _registered(cur) == 0
    finally:
        cur.execute("ROLLBACK")
        cur.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = %s::jsonb "
                    "WHERE model_key = 'sonnet-5-5'", (profiles,))
