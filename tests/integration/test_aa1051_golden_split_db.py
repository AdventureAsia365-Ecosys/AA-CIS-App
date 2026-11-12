"""AA-1051 (Quality W5 code part) — the DEV / CAL / GATE split writers and readers on real Postgres as cis_app (SET
ROLE), with the INVENTED fixture of tests/unit/aa919_fake_golden.py. Each test rolls back.

Covers: the importer writes one split row per item (rule aa1051-split-v1) with the set and a re-run writes nothing;
the backfill (splits.run) DRY RUN reads only and its apply fills the items of a set imported before the table; the R2
store reads each item's side; the pre-registration runs query, the W5 spend query and the cost report queries run on
the real tables.

Needs CIS 399 (shared.eval_golden_split): the code PR ships after the migration PR (MIG-4 chain), so CI replays it.
A checkout without the file (the code branch before main has 399) skips this module."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X284_STANDIN_MARK, X284_PREDECESSORS  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402
from services.golden_set import derive, importer  # noqa: E402
from services.writer_proof import cost_report, spend, splits  # noqa: E402
from services.writing_memory import r2  # noqa: E402
from tests.unit.aa919_fake_golden import TENANT_A, TENANT_B, fake_fixture  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "399_eval_golden_split.sql")
pytestmark = pytest.mark.skipif(not os.path.exists(MIGRATION), reason="CIS 399 is not in this checkout yet")


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for version in X284_PREDECESSORS:
            await c.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                            "VALUES ($1, now(), $2) ON CONFLICT (version) DO NOTHING", version, X284_STANDIN_MARK)
        await c.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
        await c.execute(migration_body(284))                       # its grants (other modules revoke cis_app's)
        await c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.prompt_eval_runs TO cis_app")   # Dev (244)
        await c.execute("GRANT SELECT ON shared.tenants TO cis_app")
        await c.execute("GRANT SELECT, INSERT, DELETE ON shared.eval_golden_split TO cis_app")         # 399
        await c.execute("GRANT SELECT ON shared.llm_call_log TO cis_app")
        await c.execute("GRANT SELECT, INSERT ON shared.job TO cis_app")                               # Dev grant
        await c.execute("DO $$ BEGIN IF to_regclass('shared.writing_memory_release') IS NOT NULL THEN "
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON shared.writing_memory_release TO cis_app; END IF; "
                        "END $$")
        for tid in (TENANT_A, TENANT_B):
            await c.execute("""
                INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                                            posts_per_week)
                VALUES ($1::uuid, 'AA-1051 synthetic', $2, 'starter', 60, true, 3)
                ON CONFLICT (tenant_id) DO NOTHING""", tid, f"zz1051-{tid[:8]}")
        yield c
    finally:
        await tr.rollback()
        await c.close()


def _gs():
    gs = derive.build(fake_fixture("1.0.0"), name=f"zz1051_{uuid.uuid4().hex[:6]}", source_uri="file:fake.json",
                      source_sha256="f" * 64, imported_by="pytest")
    return gs, derive.baseline(gs)


async def _sides(c, set_id) -> dict:
    return {r["sample_id"]: (r["eval_split"], r["rule"]) for r in await c.fetch(
        "SELECT sample_id, eval_split, rule FROM shared.eval_golden_split WHERE set_id = $1::uuid", set_id)}


@pytest.mark.asyncio
async def test_import_writes_one_side_per_item_and_a_rerun_writes_nothing(conn):
    gs, b = _gs()
    await conn.execute("SET LOCAL ROLE cis_app")
    dry = await importer.run(gs, b, apply=False, conn=conn)
    assert dry["splits"]["rule"] == splits.RULE
    assert sum(sum(v.values()) for v in dry["splits"]["per_writer"].values()) == len(gs.items)
    rep = await importer.run(gs, b, apply=True, conn=conn)
    set_id = rep["db"]["set_id"]
    sides = await _sides(conn, set_id)
    assert len(sides) == len(gs.items) == rep["db"]["splits"]["inserted"]
    assert all(s == (splits.split_of(k), splits.RULE) for k, s in sides.items())
    again = await importer.run(gs, b, apply=True, conn=conn)
    assert again["db"]["action"] == "exists" and again["db"]["splits"]["inserted"] == 0


@pytest.mark.asyncio
async def test_backfill_dry_run_reads_only_and_apply_fills_the_set(conn):
    gs, b = _gs()
    await conn.execute("SET LOCAL ROLE cis_app")
    rep = await importer.run(gs, b, apply=True, conn=conn)
    set_id = rep["db"]["set_id"]
    await conn.execute("DELETE FROM shared.eval_golden_split WHERE set_id = $1::uuid", set_id)   # "before 399"
    name = gs.set_row["name"]
    dry = await splits.run(conn, name=name, version="1.0.0", apply=False, assigned_by="pytest")
    assert dry["summary"]["missing"] == len(gs.items) and not await _sides(conn, set_id)
    done = await splits.run(conn, name=name, version=None, apply=True, assigned_by="split_backfill:pytest")
    assert done["inserted"] == len(gs.items) and done["summary"]["missing"] == 0
    with pytest.raises(splits.SplitRefused):
        await splits.run(conn, name="zz_no_such_set", version=None, apply=False, assigned_by="pytest")


@pytest.mark.asyncio
async def test_r2_store_reads_the_side_and_the_w5_queries_run(conn):
    gs, b = _gs()
    await conn.execute("SET LOCAL ROLE cis_app")
    rep = await importer.run(gs, b, apply=True, conn=conn)
    set_id = rep["db"]["set_id"]

    class _Pool:
        def acquire(self):
            class _A:
                async def __aenter__(self_):
                    return conn

                async def __aexit__(self_, *a):
                    return False
            return _A()

    store = r2.PgStore(_Pool())
    writer = gs.items[0]["writer"]
    items = await store.items(set_id, writer, None)
    assert items and all(it["eval_split"] == splits.split_of(it["sample_id"]) for it in items)
    assert await store.prereg_runs("zz-no-runs") == []
    row = await conn.fetchrow(spend.SPEND_SQL, spend.W5_STAGE, "null")
    assert row["total"] >= 0 and row["workflow"] >= 0
    runs, ledger, results = await cost_report.load(conn, prereg_id="zz-no-runs", run_ids=[])
    assert runs == [] and ledger == [] and results == []
    base_run = rep["db"]["baseline_runs"][writer]["run_id"]
    runs, ledger, results = await cost_report.load(conn, prereg_id=None, run_ids=[base_run])
    assert [r["run_id"] for r in runs] == [base_run] and results and all(r["sample_id"] for r in results)
    assert cost_report.report(runs, ledger, results)["runs"][0]["ledger_rows"] == 0


@pytest.mark.asyncio
async def test_w5_job_reads_and_the_control_fingerprint(conn):
    """The spend gate's queue reads (queued / running W5 jobs with their caps, the jobs per arm of one
    pre-registration) and R2's control fingerprint read, on the real tables as cis_app."""
    gs, b = _gs()
    await conn.execute("SET LOCAL ROLE cis_app")
    rep = await importer.run(gs, b, apply=True, conn=conn)
    pid = f"zz-w5-{uuid.uuid4().hex[:6]}"
    rows = [("queued", {"prereg_id": pid, "prereg_workflow": "t9", "prereg_arm": "B", "cap_usd": 9.5}),
            ("running", {"prereg_id": "zz-other", "prereg_workflow": "null", "prereg_arm": "B", "cap_usd": 5}),
            ("failed", {"prereg_id": pid, "prereg_workflow": "t9", "prereg_arm": "B", "cap_usd": 9.5}),
            ("queued", {"writer": "t9_blog", "cap_usd": 2.0})]                     # not a W5 job
    for status, payload in rows:
        await conn.execute(
            "INSERT INTO shared.job (kind, payload, status, locked_by, locked_until) "
            "VALUES ($1, $2::jsonb, $3, CASE WHEN $3 = 'running' THEN 'pytest' END, "
            "CASE WHEN $3 = 'running' THEN now() + interval '1 minute' END)",
            spend.R2_JOB_KIND, json.dumps(payload), status)
    jobs = await spend.read_jobs(conn, pid)
    mine = [j for j in jobs["active"] if j["prereg_id"] in (pid, "zz-other")]
    assert sorted((j["prereg_id"], j["status"], j["cap_usd"]) for j in mine) == sorted([
        (pid, "queued", 9.5), ("zz-other", "running", 5.0)])
    assert all(j["prereg_id"] for j in jobs["active"]) and jobs["attempts"] == {"B": 2}
    assert spend.reserved(mine, "t9") == {"workflow": 9.5, "total": 14.5}

    class _Pool:
        def acquire(self):
            class _A:
                async def __aenter__(self_):
                    return conn

                async def __aexit__(self_, *a):
                    return False
            return _A()

    writer = gs.items[0]["writer"]
    base_run = rep["db"]["baseline_runs"][writer]["run_id"]
    fp = await r2.PgStore(_Pool()).run_fingerprint(base_run)
    assert isinstance(fp, dict) and fp.get("golden_set") and "config_fp" not in fp
    assert "no writer configuration" in r2.null_config_problems(fp, {"golden_set": fp["golden_set"]})[0]
    assert await r2.PgStore(_Pool()).run_fingerprint(str(uuid.uuid4())) is None
