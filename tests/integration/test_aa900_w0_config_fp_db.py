"""AA-900 W0 — the widened release config fingerprint and the R1 freeze against real Postgres (every migration
applied by conftest). Each test runs in one transaction that is rolled back.

  * config_fp.collect reads every component it names on the live schema (no component "unavailable"), as the owner and
    as cis_app (the api / worker login: its grants cover every read);
  * a stage_model_params effort row of t9_write (migration 381's shape) moves t9_blog's config_fp and the reverify scan
    flags an active release with model_route; a row of an unrelated stage does not;
  * the reads run inside the caller's transaction without aborting it when a table is missing (savepoints);
  * r1_gate.records finds a passing writing-memory r1 run per writer, and mining.run_gate refuses at $0 without one;
  * activating a platform release flags the writer's active tenant release at once (config_fp memory_items: the
    tenant release renders after the platform one), with no separate scan;
  * the worker's scan compares mode_flags against the release's worker-run R2 evidence, never against an api draft.
"""
import json
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


# ── the scan after a release action, and mode_flags on the worker ─────────────────────────────────────────────────

ADMIN = "11111111-1111-1111-1111-111111111111"
SHA = "d" * 64


async def _golden(conn):
    await conn.execute("UPDATE shared.eval_golden_set SET status = 'superseded', superseded_at = now() "
                       "WHERE name = 'aa729' AND status = 'active'")
    return await conn.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, source_uri, source_sha256, seed, item_count, imported_by) "
        "VALUES ('aa729', '9.' || (floor(random() * 9000) + 1000)::int || '.' || (floor(random() * 9000) + 1000)::int, "
        "'file:zz.json', $1, 729, 0, 'pytest') RETURNING set_id", SHA)


async def _run(conn, writer, kind, verdict="pass", details=None, golden=None, release=None, fingerprint=None):
    return str(await conn.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by, "
        "run_kind, writer, golden_set_id, stop_reason, verdict, finished_at, release_id, fingerprint) "
        "VALUES ('writing_memory', 'zz900', 0, 0, $1::jsonb, 'pytest', $2, $3, $4, 'completed', $5, now(), $6::uuid, "
        "$7::jsonb) RETURNING id",
        json.dumps(details or {}), kind, writer, golden, verdict, release, json.dumps(fingerprint or {})))


async def _release0(conn, writer, tenant=None):
    golden = await _golden(conn)
    r0 = await _run(conn, writer, "r0_replay", details={"inputs": 20, "byte_diffs": 0})
    base = await _run(conn, writer, "baseline_import", "baseline", golden=golden)
    d = await rel.create_draft(conn, writer=writer, scope="tenant" if tenant else "platform", tenant_id=tenant,
                               created_by=f"admin:{ADMIN}")
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0, eval_run_id=base, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN, approval_ref="pytest")
    return await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")


async def _rules_release(conn, writer, *, r2_fingerprint=None):
    rule = await conn.fetchval(
        "INSERT INTO shared.writing_rule (kind, scope, writer, cluster_key, rule_text, status, approved_by, "
        "approved_at) VALUES ('writer_instruction', 'platform', $1, 'zz900-w', 'Open on the quiet lane.', "
        "'approved', 'x', now()) RETURNING rule_id::text", writer)
    d = await rel.create_draft(conn, writer=writer, rule_ids=[rule], created_by=f"admin:{ADMIN}")
    r2 = await _run(conn, writer, "r2", release=d["release_id"], details={"candidate": "release"},
                    fingerprint=r2_fingerprint)
    await rel.attach_evidence(conn, d["release_id"], eval_run_id=r2, actor=f"admin:{ADMIN}")
    sh = await _run(conn, writer, "shadow", release=d["release_id"])
    await rel.attach_evidence(conn, d["release_id"], shadow_run_id=sh, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN)
    return d


@pytest.fixture
def api_service():
    cf.set_service("api")
    yield
    cf.set_service("api")


@pytest.mark.asyncio
async def test_activating_a_platform_release_flags_the_tenant_release_at_once(conn, api_service):
    await _release0(conn, "t9_blog")
    tenant = str(uuid.uuid4())
    t0 = await _release0(conn, "t9_blog", tenant=tenant)
    assert (await rel.get_release(conn, t0["release_id"]))["reverify"] is False
    d = await _rules_release(conn, "t9_blog")
    await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")          # no separate scan call
    flagged = await rel.get_release(conn, t0["release_id"])
    assert flagged["reverify"] is True and flagged["reverify_reasons"] == ["prompt_version_mismatch"]
    details = await conn.fetchval(
        "SELECT details FROM acp_shared.audit_log WHERE resource_type = 'writing_memory_release' "
        "AND resource_id = $1 AND action = 'writing_memory.release_reverify_flagged' "
        "ORDER BY created_at DESC LIMIT 1", t0["release_id"])
    details = details if isinstance(details, dict) else json.loads(details)
    assert details["components"] == ["memory_items"]
    platform = await rel.get_release(conn, d["release_id"])
    assert platform["reverify"] is False                                       # its own config did not move


@pytest.mark.asyncio
async def test_the_worker_compares_mode_flags_with_the_worker_evidence_only(conn, api_service, monkeypatch):
    for k in cf.MODE_FLAGS["t9_blog"]:
        monkeypatch.delenv(k, raising=False)
    await _release0(conn, "t9_blog")
    # the R2 run ran in the worker with the worker's switch on
    cf.set_service(cf.WRITER_SERVICE)
    monkeypatch.setenv("T9_FAQ_LLM_FILTER", "on")
    r2_fp = await rel.current_fingerprint(conn, "t9_blog")
    # the draft, approval and activation happen in the api, where the switch is not set
    cf.set_service("api")
    monkeypatch.delenv("T9_FAQ_LLM_FILTER")
    d = await _rules_release(conn, "t9_blog", r2_fingerprint=r2_fp)
    await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")
    # the worker starts with the same env: nothing to flag
    cf.set_service(cf.WRITER_SERVICE)
    monkeypatch.setenv("T9_FAQ_LLM_FILTER", "on")
    assert await rel.reverify_scan(conn, actor="system:worker_start") == []
    # a deploy turns the worker switch off: the worker flags it against the R2 evidence
    monkeypatch.setenv("T9_FAQ_LLM_FILTER", "off")
    flagged = await rel.reverify_scan(conn, actor="system:worker_start")
    mine = [f for f in flagged if f["release_id"] == d["release_id"]]
    assert mine and mine[0]["reasons"] == ["model_route"] and mine[0]["components"] == ["mode_flags"]
