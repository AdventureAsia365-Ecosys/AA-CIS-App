"""AA-919 WM-19 / WM-21 — the R2 runner and the targeted shadow against real Postgres (the CIS 284 / 287 triggers),
logged in as cis_app, with the INVENTED golden fixture of tests/unit/aa919_fake_golden.py, a fake writer and a fake
judge (no model call). Each test rolls back.

Covers: migration 291 (the judge stage row, judge role, no shadow; a re-run is a no-op); an R2 run writes a finished
writing_memory r2 run, control rejudge rows that point at the baseline import's rows, candidate regenerate rows, and
the result trigger accepts every row (phase = split, same set, a control never regenerated); a seeded failing run stops
at tune with nothing in holdout; the run's spend is read from llm_call_log rows labelled with its run id; a shadow run
writes writing_memory_shadow rows owned by the input's tenant and a shadow run row.
"""
import asyncio  # noqa: F401 — pytest-asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import (DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X284_PREDECESSORS,  # noqa: E402
                      X284_STANDIN_MARK, X287_PREDECESSORS, X287_STANDIN_MARK)
from _rls_fixture_helpers import migration_body  # noqa: E402
from services.golden_set import derive, importer  # noqa: E402
from services.writing_memory import config as cfg  # noqa: E402
from services.writing_memory import r2, shadow  # noqa: E402
from services.writing_memory import releases as rel  # noqa: E402
from tests.unit.aa919_fake_golden import TENANT_A, TENANT_B, fake_fixture  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ADMIN = "11111111-1111-1111-1111-111111111111"


class OneConnPool:
    """PgStore asks a pool for connections; the test hands it its one transaction-bound connection."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for version in (*X284_PREDECESSORS, *X287_PREDECESSORS):
            mark = X287_STANDIN_MARK if version in X287_PREDECESSORS else X284_STANDIN_MARK
            await c.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                            "VALUES ($1, now(), $2) ON CONFLICT (version) DO NOTHING", version, mark)
        await c.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
        await c.execute(migration_body(284))
        await c.execute(migration_body(287))
        await c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.prompt_eval_runs TO cis_app")
        await c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.writing_rule TO cis_app")
        await c.execute("GRANT SELECT ON shared.tenants, shared.llm_role_config TO cis_app")
        await c.execute("GRANT SELECT, INSERT ON shared.llm_call_log TO cis_app")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _golden(conn):
    for tid in (TENANT_A, TENANT_B):
        await conn.execute("""
            INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active, posts_per_week)
            VALUES ($1::uuid, 'AA-919 synthetic', $2, 'starter', 60, true, 3) ON CONFLICT (tenant_id) DO NOTHING""",
                           tid, f"zz919-{tid[:8]}")
    await conn.execute("UPDATE shared.eval_golden_set SET status = 'superseded', superseded_at = now() "
                       "WHERE name = 'aa729' AND status = 'active'")
    version = f"9.{uuid.uuid4().int % 9000 + 1000}.{uuid.uuid4().int % 9000 + 1000}"
    gs = derive.build(fake_fixture(version), name="aa729", source_uri="file:fake.json", source_sha256="e" * 64,
                      imported_by="pytest")
    rep = await importer.run(gs, derive.baseline(gs), apply=True, conn=conn)
    assert rep["ok"], rep
    return await conn.fetchval("SELECT set_id::text FROM shared.eval_golden_set WHERE name = 'aa729' "
                               "AND status = 'active'")


async def _flush():
    return None


def _writer(fail=False):
    async def w(writer, inp, *, view, shared_inputs):
        text = "A calm invented rewrite of a pretend route."
        if fail:
            text += " [gate F9_cta_fact]"
        return {"name": inp["tour"]["name"], "summary": text}
    return w


def _judge(conn, spend_each=0.002):
    async def j(pool, *, writer, source_text, output_text, run_id, item_id, arm):
        # what judge_output writes: one llm_call_log row under the eval stage, with the run id (relabel)
        from shared.llm_client.call_log import _relabel
        stage, tenant, qs, piece, req = _relabel(cfg.EVAL_STAGE, None, {"arm": arm}, None, None)
        await conn.execute("INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal) "
                           "VALUES ($1, 'judge', 'fake', $2, $3::jsonb)", stage, spend_each, json.dumps(qs))
        return SimpleNamespace(verdict="pass", score=8.0, failure_codes=[], model="fake", judge_version="v-test",
                               cost_usd=spend_each)
    return j


@pytest.mark.asyncio
async def test_291_seeds_the_judge_stage_and_is_a_noop_on_rerun(conn):
    row = await conn.fetchrow("SELECT role, provider, model_id, shadow_model_id, shadow_sample_pct, is_active "
                              "FROM shared.llm_role_config WHERE stage = 'writing_memory_eval'")
    assert row is not None and row["role"] == "judge" and row["shadow_model_id"] is None
    assert row["shadow_sample_pct"] == 0 and row["is_active"]
    await conn.execute(migration_body(291))
    assert await conn.fetchval("SELECT count(*) FROM shared.llm_role_config WHERE stage = 'writing_memory_eval'") == 1


@pytest.mark.asyncio
async def test_r2_run_writes_rows_the_triggers_accept(conn):
    set_id = await _golden(conn)
    await conn.execute("SET LOCAL ROLE cis_app")
    store = r2.PgStore(OneConnPool(conn))
    out = await r2.run_r2(store, r2.R2Config(writer="a1"), writer_fn=_writer(), judge_fn=_judge(conn),
                          flush=_flush)
    run = await conn.fetchrow("SELECT * FROM shared.prompt_eval_runs WHERE id = $1::uuid", out.run_id)
    assert run["pipeline"] == "writing_memory" and run["run_kind"] == "r2" and run["writer"] == "a1"
    assert str(run["golden_set_id"]) == set_id and run["finished_at"] is not None
    assert run["verdict"] == out.verdict and run["stop_reason"] == out.stop_reason
    assert float(run["cost_cap_usd"]) == cfg.R2_RUN_CAP_USD
    assert float(run["cost_usd"]) == pytest.approx(out.cost_usd) and out.cost_usd > 0
    rows = await conn.fetch("SELECT r.arm, r.mode, r.phase, r.scored_result_id, s.run_id AS src_run, "
                            "s.mode AS src_mode, r.output IS NOT NULL AS has_output "
                            "FROM shared.eval_item_result r LEFT JOIN shared.eval_item_result s "
                            "ON s.result_id = r.scored_result_id WHERE r.run_id = $1::uuid", out.run_id)
    controls = [r for r in rows if r["arm"] == "control"]
    cands = [r for r in rows if r["arm"] == "candidate"]
    assert controls and cands
    baseline = await conn.fetchval("SELECT id FROM shared.prompt_eval_runs WHERE run_kind = 'baseline_import' "
                                   "AND writer = 'a1' AND golden_set_id = $1::uuid", set_id)
    assert all(r["mode"] == "rejudge" and r["src_run"] == baseline and r["src_mode"] == "score_only"
               and not r["has_output"] for r in controls)
    assert all(r["mode"] == "regenerate" and r["has_output"] for r in cands)
    assert {r["phase"] for r in cands} <= {"probe", "tune", "holdout"}


@pytest.mark.asyncio
async def test_seeded_failing_run_stops_at_tune(conn):
    await _golden(conn)
    await conn.execute("SET LOCAL ROLE cis_app")
    store = r2.PgStore(OneConnPool(conn))
    out = await r2.run_r2(store, r2.R2Config(writer="a1", seed_fault="qa_trace", cap_usd=1.0),
                          writer_fn=_writer(), judge_fn=_judge(conn), flush=_flush)
    assert (out.verdict, out.stop_reason, out.stopped_phase) == ("fail", "hard_stop", "tune")
    n = await conn.fetchval("SELECT count(*) FROM shared.eval_item_result WHERE run_id = $1::uuid "
                            "AND arm = 'candidate'", out.run_id)
    assert n == 1
    assert await conn.fetchval("SELECT count(*) FROM shared.eval_item_result WHERE run_id = $1::uuid "
                               "AND phase = 'holdout' AND arm = 'candidate'", out.run_id) == 0


@pytest.mark.asyncio
async def test_run_spend_counts_only_this_runs_labelled_rows(conn):
    store = r2.PgStore(OneConnPool(conn))
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rid = str(uuid.uuid4())
    for run_id, stage, usd in ((rid, cfg.EVAL_STAGE, 0.5), (rid, cfg.EVAL_STAGE, 0.25),
                               (str(uuid.uuid4()), cfg.EVAL_STAGE, 3.0), (rid, "s1_generate", 9.0)):
        await conn.execute("INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal) "
                           "VALUES ($1, 'writer', 'm', $2, $3::jsonb)", stage, usd, json.dumps({"eval_run": run_id}))
    await conn.execute("SET LOCAL ROLE cis_app")
    assert await store.run_spend(rid, since) == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_shadow_run_writes_rows_owned_by_the_input_tenant(conn):
    await _golden(conn)
    d = await rel.create_draft(conn, writer="t9_social", scope="tenant", tenant_id=TENANT_A,
                               created_by=f"admin:{ADMIN}")             # as the owner (the audit_log write)
    await conn.execute("SET LOCAL ROLE cis_app")

    def inp(i, tenant, probe=False):
        return {"input_source": "content_piece", "input_id": str(uuid.uuid4()), "tenant_id": tenant,
                "channel": "linkedin", "probe": probe,
                "input": {"writer_input": {"channel": "linkedin", "seed": f"Invented seed {i}.",
                                           "brand_pack": {"brand_name": f"Zzbrand {tenant[:4]}"}},
                          "tenant_id": tenant}}

    async def select(pool, release, *, n, channel, gates):
        return [inp(0, TENANT_A), inp(1, TENANT_A), inp(2, TENANT_B, probe=True)]

    async def writer(w, i, *, view, shared_inputs):
        return {"content_text": "A clean invented post."}

    pool = OneConnPool(conn)
    out = await shadow.run_shadow(pool, shadow.ShadowConfig(release_id=d["release_id"], force_size=2, cap_usd=1.0),
                                  writer_fn=writer, flush=_flush, select=select,
                                  packs_fn=lambda p, t: shadow.tenant_packs(p, set()))
    assert out["verdict"] == "pass", out
    rows = await conn.fetch("SELECT scope, tenant_id::text AS tenant_id, probe, verdict, channel, "
                            "release_id::text AS rid "
                            "FROM shared.writing_memory_shadow WHERE run_id = $1::uuid ORDER BY probe", out["run_id"])
    assert [(r["tenant_id"], r["probe"]) for r in rows] == [(TENANT_A, False), (TENANT_A, False), (TENANT_B, True)]
    assert all(r["scope"] == "tenant" and r["channel"] == "linkedin" and r["rid"] == d["release_id"] for r in rows)
    run = await conn.fetchrow("SELECT run_kind, writer, verdict, stop_reason, release_id::text AS rid "
                              "FROM shared.prompt_eval_runs WHERE id = $1::uuid", out["run_id"])
    assert (run["run_kind"], run["writer"], run["verdict"], run["rid"]) == ("shadow", "t9_social", "pass",
                                                                             d["release_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("writer,scope", [("a1", "platform"), ("t2", "tenant"), ("t9_blog", "tenant"),
                                          ("t9_social", "platform")])
async def test_shadow_input_queries_and_the_r2_estimate_run_read_only(conn, writer, scope):
    await _golden(conn)
    # as the test superuser: this checks the SQL (names, casts, filters), not grants (Dev's cis_app reads these tables;
    # this replay DB lacks some of Dev's grants)
    pool = OneConnPool(conn)
    release = {"writer": writer, "scope": scope, "tenant_id": TENANT_A if scope == "tenant" else None}
    async with conn.transaction(readonly=True):
        got = await shadow.select_inputs(pool, release, n=3, channel=None,
                                         gates=["F9_cta_fact"] if writer.startswith("t9") else [])
        est = await r2.estimate(pool, r2.R2Config(writer=writer, limit=4, cap_usd=2.0))
    assert isinstance(got, list)
    assert est["writer"] == writer and est["cap_usd"] == 2.0 and est["estimated_usd_max"] >= 0
