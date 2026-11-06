"""AA-900 W0 — the widened release config fingerprint and the R1 freeze against real Postgres (every migration
applied by conftest). Each test runs in one transaction that is rolled back.

  * config_fp.collect reads every component it names on the live schema (no component "unavailable"), as the owner and
    as cis_app (the api / worker login: its grants cover every read);
  * a stage_model_params effort row of t9_write (migration 381's shape) moves t9_blog's config_fp and the reverify scan
    flags an active release with model_route; a row of an unrelated stage does not;
  * the reads run inside the caller's transaction without aborting it when a table is missing (savepoints);
  * r1_gate.records finds a passing writing-memory r1 run per writer, and mining.run_gate refuses at $0 without one.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.writing_memory import config_fp as cf  # noqa: E402
from services.writing_memory import mining, r1_gate  # noqa: E402
from services.writing_memory import releases as rel  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
READ_TABLES = ("llm_role_config", "llm_route_policy", "stage_model_params", "llm_model_catalog",
               "llm_model_deployment", "llm_route_settings", "writing_memory_release", "writing_rule",
               "tenant_brand_rules", "tenant_seo_config", "prompt_eval_runs", "eval_golden_set")


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _model_key(conn):
    return await conn.fetchval("SELECT model_key FROM shared.llm_model_catalog ORDER BY model_key LIMIT 1")


@pytest.mark.asyncio
async def test_collect_reads_every_component(conn):
    for writer in ("a1", "t2", "t9_blog", "t9_social"):
        fp = await cf.collect(conn, writer, env={})
        bad = [k for k, v in fp["parts"].items() if v == cf.UNAVAILABLE]
        assert bad == [], f"{writer}: {bad}"
    tenant = str(uuid.uuid4())
    fp = await cf.collect(conn, "t9_social", scope="tenant", tenant_id=tenant, env={})
    assert fp["parts"]["memory_items"] not in (None, cf.UNAVAILABLE) and fp["parts"]["brand_config"] is not None


@pytest.mark.asyncio
async def test_cis_app_can_read_every_component(conn):
    # other modules re-create roles or revoke from cis_app: grant the reads the Dev migrations grant, for this test
    await conn.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cis_app') THEN "
                       "CREATE ROLE cis_app; END IF; END $$")
    await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    for t in READ_TABLES:
        await conn.execute(f"GRANT SELECT ON shared.{t} TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")
    fp = await cf.collect(conn, "t9_blog", env={})
    await conn.execute("RESET ROLE")
    assert [k for k, v in fp["parts"].items() if v == cf.UNAVAILABLE] == []
    ok = await conn.fetchval("SELECT 1 AS ok")
    assert ok == 1                                   # the transaction is still usable


@pytest.mark.asyncio
async def test_an_effort_row_flags_the_active_release_and_an_unrelated_stage_does_not(conn):
    model = await _model_key(conn)
    for stage in ("t9_write", "a3_place_card_check"):
        await conn.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, is_active, "
                           "updated_at, updated_by, fallback_model_ids, shadow_sample_pct, pii_class) "
                           "VALUES ($1, 'writer', 'claude', $2, true, now(), 'test', '{}', 0, 'content') "
                           "ON CONFLICT (stage) DO NOTHING", stage, model)
    await conn.execute("DELETE FROM shared.stage_model_params WHERE stage IN ('t9_write', 'a3_place_card_check')")
    before = await rel.current_fingerprint(conn, "t9_blog")
    await conn.execute("INSERT INTO shared.stage_model_params (stage, model_key, effort, updated_by) "
                       "VALUES ('a3_place_card_check', $1, 'low', 'test')", model)
    unrelated = await rel.current_fingerprint(conn, "t9_blog")
    assert unrelated["config_fp"]["sha"] == before["config_fp"]["sha"]
    await conn.execute("INSERT INTO shared.stage_model_params (stage, model_key, effort, updated_by) "
                       "VALUES ('t9_write', $1, 'medium', 'test')", model)
    after = await rel.current_fingerprint(conn, "t9_blog")
    assert cf.reverify_components(before, after) == ["stage_params"]
    assert "t9_write" in cf.changed_stages(before, after)
    assert rel.reverify_reasons(before, after) == ["model_route"]


@pytest.mark.asyncio
async def test_a_failed_read_does_not_abort_the_callers_transaction(conn):
    await conn.execute("ALTER TABLE shared.llm_route_policy RENAME TO llm_route_policy_hidden_aa900")
    rows = await cf._rows(conn, "SELECT * FROM shared.llm_route_policy")
    assert rows == cf.UNAVAILABLE
    ok = await conn.fetchval("SELECT 1 AS ok")
    assert ok == 1


async def _r1_run(conn, writer, verdict="pass"):
    await conn.execute(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, regression_detected, "
        "details, triggered_by, run_kind, writer, tiers, fingerprint, stop_reason, verdict, finished_at) "
        "VALUES ('writing_memory', 'r1-test', 0, 0, false, '{}'::jsonb, 'test', 'r1', $1, '{}', '{}'::jsonb, "
        "'completed', $2, now())", writer, verdict)


@pytest.mark.asyncio
async def test_r1_records_and_the_mining_freeze(conn, monkeypatch):
    monkeypatch.delenv(r1_gate.R1_GATE_ENV, raising=False)
    await conn.execute("DELETE FROM shared.prompt_eval_runs WHERE pipeline = 'writing_memory' AND run_kind = 'r1'")
    assert await r1_gate.records(conn) == {}
    refusal, facts = await mining.run_gate(conn, "manual")
    assert refusal and "R1" in refusal and facts["r1"]["frozen"] == list(r1_gate.WRITERS)
    await _r1_run(conn, "t9_social", verdict="fail")
    assert await r1_gate.records(conn) == {}
    await _r1_run(conn, "t9_social")
    recs = await r1_gate.records(conn)
    assert list(recs) == ["t9_social"] and recs["t9_social"]["run_id"]
    refusal, facts = await mining.run_gate(conn, "manual")
    assert refusal is None or "R1" not in refusal
    assert facts["r1_writers"] == 1 and "t9_social" not in facts["r1"]["frozen"]
    assert await r1_gate.check(conn, "t9_social") is None
    assert (await r1_gate.check(conn, "t2"))[0] == "r1_missing"
