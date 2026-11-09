"""AA-741 (S271) — migration 381: an effort per stage x model in shared.stage_model_params for the blog writer t9_write
on Sonnet 5.5 (medium) and five Claude 5.5 validate stages (low). t2_generate and t8_angle_gen are left at their
default (no row) until a paid quality read backs a change.

conftest replays 381 like every file. The module re-applies the file itself (errors are not swallowed; a second run
is a no-op) and checks:
  * the six rows (no row for the other writers), effort only (max_tokens, min_output_tokens, temperature,
    reasoning_mode NULL), updated_by migration-381-aa741, registered once;
  * a row that exists with another value is kept and named in a NOTICE (ON CONFLICT DO NOTHING);
  * a listed stage with no route row (a replay where its stage migration did not apply) gets no row, NOTICE;
  * the guard refuses when t9_write has no route row: nothing changes (the file's transaction rolls back);
  * the gateway's read (stage_params.fetch_params) returns the effort for the writer stage.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

import psycopg2  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "381_stage_thinking_effort_writers.sql")
EXPECTED = {
    ("t9_write", "sonnet-5-5"): "medium",
    ("t9_faq_filter", "haiku-5-5"): "low",
    ("t9_faq_answer_fit", "sonnet-5-5"): "low",
    ("a3_place_card_check", "haiku-5-5"): "low",
    ("a3_place_days", "haiku-5-5"): "low",
    ("a3_place_dedupe_check", "haiku-5-5"): "low",
}
ACTOR = "migration-381-aa741"


def _sql() -> str:
    with open(MIGRATION, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def cur():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True
    k = c.cursor()
    k.execute("SELECT to_regclass('shared.stage_model_params') IS NOT NULL AS ok")
    if not k.fetchone()[0]:
        pytest.skip("shared.stage_model_params missing (migration 320 did not replay here)")
    k.execute("DELETE FROM shared.stage_model_params WHERE updated_by = %s", (ACTOR,))
    k.execute(_sql())                                         # errors raise here (conftest swallows its own run)
    k.execute(_sql())                                         # a second run is a no-op
    yield k
    k.execute("DELETE FROM shared.stage_model_params WHERE updated_by = %s", (ACTOR,))
    k.execute(_sql())                                         # leave the replayed state as conftest made it
    c.close()


def _routed(cur) -> dict:
    cur.execute("SELECT stage FROM shared.llm_role_config")
    stages = {r[0] for r in cur.fetchall()}
    return {k: v for k, v in EXPECTED.items() if k[0] in stages}


def test_the_blog_writer_is_routed_here(cur):
    assert "t9_write" in {k[0] for k in _routed(cur)}


def test_the_other_writers_keep_their_default(cur):
    # No quality evidence for an effort change on the tour and angle writers yet: no row, so they stay at high.
    cur.execute("SELECT count(*) AS n FROM shared.stage_model_params "
                "WHERE updated_by = %s AND stage IN ('t2_generate', 't8_angle_gen')", (ACTOR,))
    assert cur.fetchone()[0] == 0


def test_the_rows_set_effort_only(cur):
    cur.execute("SELECT stage, model_key, effort, max_tokens, min_output_tokens, temperature, reasoning_mode, "
                "notes FROM shared.stage_model_params WHERE updated_by = %s", (ACTOR,))
    rows = cur.fetchall()
    assert {(r[0], r[1]): r[2] for r in rows} == _routed(cur)
    for r in rows:
        assert r[3:7] == (None, None, None, None), r
        assert r[7] and "AA-741" in r[7]


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '381'")
    assert cur.fetchone()[0] == 1


def test_an_existing_row_is_kept_and_named(cur):
    conn = cur.connection
    cur.execute("DELETE FROM shared.stage_model_params WHERE stage = 't9_write' AND model_key = 'sonnet-5-5'")
    cur.execute("INSERT INTO shared.stage_model_params (stage, model_key, effort, updated_by) "
                "VALUES ('t9_write', 'sonnet-5-5', 'low', 'admin-test')")
    conn.notices.clear()
    cur.execute(_sql())
    cur.execute("SELECT effort, updated_by FROM shared.stage_model_params "
                "WHERE stage = 't9_write' AND model_key = 'sonnet-5-5'")
    assert cur.fetchone() == ("low", "admin-test")
    assert any("381" in n and "t9_write" in n and "kept" in n for n in conn.notices), conn.notices
    cur.execute("DELETE FROM shared.stage_model_params WHERE stage = 't9_write' AND model_key = 'sonnet-5-5'")
    cur.execute(_sql())
    cur.execute("SELECT effort, updated_by FROM shared.stage_model_params "
                "WHERE stage = 't9_write' AND model_key = 'sonnet-5-5'")
    assert cur.fetchone() == ("medium", ACTOR)


def test_an_unrouted_stage_is_skipped_and_named(cur):
    conn = cur.connection
    bad = _sql().replace("('t9_faq_filter', 'haiku-5-5'", "('t9_no_such_stage', 'haiku-5-5'")
    conn.notices.clear()
    cur.execute(bad)
    cur.execute("SELECT count(*) AS n FROM shared.stage_model_params WHERE stage = 't9_no_such_stage'")
    assert cur.fetchone()[0] == 0
    assert any("381" in n and "t9_no_such_stage" in n and "skipped" in n for n in conn.notices), conn.notices


def test_the_guard_refuses_without_a_t9_write_route(cur):
    bad = _sql().replace("('t9_write', 'sonnet-5-5'", "('t9_wrote', 'sonnet-5-5'")
    cur.execute("DELETE FROM shared.stage_model_params WHERE updated_by = %s", (ACTOR,))
    with pytest.raises(psycopg2.Error, match="t9_write"):
        cur.execute(bad)
    cur.execute("ROLLBACK")                                   # the failed file left its transaction open
    cur.execute("SELECT count(*) AS n FROM shared.stage_model_params WHERE updated_by = %s", (ACTOR,))
    assert cur.fetchone()[0] == 0
    cur.execute(_sql())


def test_the_gateway_reads_the_writer_effort(cur):
    import asyncpg

    from shared.llm_client import stage_params

    async def read():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            return await stage_params.fetch_params(conn)
        finally:
            await conn.close()

    params = asyncio.run(read())
    row = params[("t9_write", "sonnet-5-5")]
    assert row.effort == "medium" and row.reasoning_mode is None and row.max_tokens is None
