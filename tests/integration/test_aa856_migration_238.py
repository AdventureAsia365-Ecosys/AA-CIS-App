"""AA-856 — migration 238 (cohere-embed-v4 acc2 profile us. -> global.) against real Postgres.

conftest applies every migration (169 seeds the row with ``us.cohere.embed-v4:0``, 238 moves it).
These tests run the file again directly, errors NOT swallowed: from the `us.` state, twice
(idempotent), and the guard on an unexpected profile. The row is restored to the global profile.
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "238_cohere_embed_global_profile.sql")
US = "us.cohere.embed-v4:0"
GLOBAL = "global.cohere.embed-v4:0"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    try:
        yield c
    finally:
        _set_acc2(c, GLOBAL)
        conn.close()


def _set_acc2(c, profile):
    c.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = "
              "jsonb_set(bedrock_profile_ids, '{acc2}', to_jsonb(%s::text)) "
              "WHERE model_key = 'cohere-embed-v4'", (profile,))


def _row(c):
    c.execute("SELECT bedrock_profile_ids, updated_by FROM shared.llm_model_catalog "
              "WHERE model_key = 'cohere-embed-v4'")
    return c.fetchone()


def _run(c):
    c.execute(open(MIGRATION, encoding="utf-8").read())


def test_conftest_replay_left_the_row_on_the_global_profile(cur):
    profiles, _ = _row(cur)
    assert profiles["acc2"] == GLOBAL
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '238'")
    assert cur.fetchone()


def test_moves_us_to_global_and_is_idempotent(cur):
    _set_acc2(cur, US)
    cur.execute("UPDATE shared.llm_model_catalog SET updated_by = 'before-238' "
                "WHERE model_key = 'cohere-embed-v4'")
    _run(cur)
    profiles, by = _row(cur)
    assert profiles == {"acc2": GLOBAL} and by == "migration-238-aa856"
    cur.execute("UPDATE shared.llm_model_catalog SET updated_by = 'after-first-run' "
                "WHERE model_key = 'cohere-embed-v4'")
    _run(cur)                                   # second run: no change, no error
    profiles, by = _row(cur)
    assert profiles == {"acc2": GLOBAL} and by == "after-first-run"


def test_guard_refuses_an_unexpected_profile(cur):
    _set_acc2(cur, "eu.cohere.embed-v4:0")
    with pytest.raises(psycopg2.Error, match="238: cohere-embed-v4 acc2 profile is eu.cohere.embed-v4:0"):
        _run(cur)
    cur.execute("ROLLBACK")                     # the file's own BEGIN is still open after the raise
    profiles, _ = _row(cur)
    assert profiles["acc2"] == "eu.cohere.embed-v4:0"
