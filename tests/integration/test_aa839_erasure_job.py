"""AA-839 — the data_erasure job against real Postgres (every migration replayed by conftest).

The handler runs logged in AS cis_app (the CIS runtime role since X1 S18), with CIS 244 applied in the Dev order
the way test_x1_cis_app_privileges does, so grants, RLS parity policies and the privilege blockers are the ones the
worker meets on Dev. Seeding and the worker's own status updates use the superuser. Synthetic data only: each test
makes its own tenants (random ids) and its own traveller hash. Covers:
- the inventory is complete for the replayed schema (no tenant-key table without an entry) and every SQL the
  planner generates runs (counts on a tenant with no rows);
- a tenant DRY RUN counts the seeded rows, writes nothing and leaves another tenant alone;
- the apply is refused with the flag off, before the retention window, on a stale fingerprint or a blocker, on a
  DRY RUN already used, and on a tenant restored after the first checks; then runs in one transaction: deletes
  children-first (the quarter_plan <-> version cycle, the T2 version chain, owner_scope atoms, soft-keyed A3
  fingerprints), detaches the cost log, merges the daily rollup into the platform row, keeps the audit chain,
  writes the erasure audit row, and a DRY RUN afterwards finds 0;
- blockers: another tenant's rows (FK and self FK), a child job outside the erasure, a revoked privilege, and an
  unreadable foreign-owner table (one savepoint per check: the later checks still run);
- a traveller DRY RUN finds the address in a notification and a job payload by hash, the apply deletes / redacts
  them and strips the hash from every erasure job row; a verification DRY RUN and a failed job strip their own.
"""
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import psycopg2
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import (  # noqa: E402
    CIS_APP_CI_PASSWORD, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, x1_244_ci_shape, x1_244_ci_unshape,
    x1_244_undo,
)
from services.erasure import inventory as inv  # noqa: E402
from services.erasure import plan as pl  # noqa: E402
from services.jobs import erasure_job as ej  # noqa: E402
from shared.jobs import queue, registry  # noqa: E402
from shared.jobs.registry import JobContext, NonRetryable  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
APP_DSN = f"postgresql://cis_app:{CIS_APP_CI_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ON = {ej.APPLY_FLAG: "true"}
MIGRATION_244 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "244_x1_cis_app_runtime.sql")
# Registry tables the replayed test database may lack (migrations 001 / 020 and the acp_deliver page table are
# skipped or fail there), plus the legacy entries registered "absent on Dev". Missing is fine (nothing to erase);
# unregistered is not.
CI_ABSENT = {"gold_aa_internal.tenant_tour_versions", "acp_shared.acp_output_rules", "acp_deliver.tenant_tour_pages"} \
    | {t.table for t in inv.TENANT_TABLES if "absent on Dev" in t.reason}

# Live definition of gold_aa_internal.tenant_tour_versions (db-schema-reference.md, 22/10/2026): created by
# migration 001, which the replay skips, so this module builds it for its own tests and drops it afterwards
# (lesson S237: no global stand-in). With its FKs (tenant, published tour, self, job), RLS as on Dev, and the
# column review_queue.tenant_tour_version_id that migration 107 could not add without it.
TTV_LIVE_DDL = """
    CREATE TABLE gold_aa_internal.tenant_tour_versions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES shared.tenants (tenant_id),
        published_tour_id uuid NOT NULL REFERENCES gold_aa_internal.published_tours (id),
        version_number integer NOT NULL DEFAULT 1,
        parent_version_id uuid REFERENCES gold_aa_internal.tenant_tour_versions (id),
        rewritten_content jsonb NOT NULL DEFAULT '{}'::jsonb,
        status text NOT NULL DEFAULT 'pending',
        quality_score numeric(4,2),
        edit_source text NOT NULL DEFAULT 'ai_generated',
        rewrite_language text NOT NULL DEFAULT 'en',
        seo_mode text NOT NULL DEFAULT 'standard',
        edited_at timestamptz,
        edited_by_user text,
        created_at timestamptz NOT NULL DEFAULT now(),
        qa_status text NOT NULL DEFAULT 'pending',
        qa_repair_count smallint NOT NULL DEFAULT 0,
        qa_checked_at timestamptz,
        qa_auto_passed boolean NOT NULL DEFAULT false,
        job_id uuid REFERENCES shared.job (id)
    );
    ALTER TABLE gold_aa_internal.tenant_tour_versions ENABLE ROW LEVEL SECURITY;
    CREATE POLICY tenant_tour_versions_cis_app_all ON gold_aa_internal.tenant_tour_versions
        AS PERMISSIVE FOR ALL TO cis_app USING (true) WITH CHECK (true);
    GRANT SELECT, INSERT, UPDATE, DELETE ON gold_aa_internal.tenant_tour_versions TO cis_app;
    ALTER TABLE silver_aa_internal.review_queue ALTER COLUMN generated_content_id DROP NOT NULL,
        ADD COLUMN tenant_tour_version_id uuid REFERENCES gold_aa_internal.tenant_tour_versions (id);
"""


def _body_244_without_guards() -> str:
    sql = open(MIGRATION_244, encoding="utf-8").read()
    return "BEGIN;\n" + sql[sql.index("-- ── Guard 0"):sql.index("-- ── 4. Register")] + "\nCOMMIT;\n"


@pytest.fixture(scope="module", autouse=True)
def dev_shape():
    """CIS 244 in the Dev order (as test_x1_cis_app_privileges), then the live tenant_tour_versions; undone after."""
    admin = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    admin.autocommit = True
    a = admin.cursor()
    try:
        x1_244_ci_shape(a)
        a.execute(_body_244_without_guards())
        a.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                  "VALUES ('244', now(), 'test_aa839_erasure_job') ON CONFLICT (version) DO NOTHING")
        x1_244_ci_unshape(a)
        a.execute("ALTER ROLE cis_app LOGIN")                     # lesson 59: a TP test may leave it NOLOGIN
        a.execute(TTV_LIVE_DDL)
        yield
    finally:
        a.execute("DELETE FROM shared.job WHERE kind = 'data_erasure'")
        a.execute("ALTER TABLE silver_aa_internal.review_queue DROP COLUMN IF EXISTS tenant_tour_version_id")
        a.execute("DELETE FROM silver_aa_internal.review_queue WHERE generated_content_id IS NULL")
        a.execute("ALTER TABLE silver_aa_internal.review_queue ALTER COLUMN generated_content_id SET NOT NULL")
        a.execute("DROP TABLE IF EXISTS gold_aa_internal.tenant_tour_versions")
        x1_244_undo(a)
        admin.close()


@pytest_asyncio.fixture
async def pool():
    """(admin pool, cis_app pool): seeding and job status as the superuser, the handler as cis_app."""
    admin = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    app = await asyncpg.create_pool(APP_DSN, min_size=1, max_size=3)
    assert await app.fetchval("SELECT current_user") == "cis_app"
    yield admin, app
    await admin.execute("DELETE FROM shared.job WHERE kind = 'data_erasure'")
    await app.close()
    await admin.close()


class FakeS3:
    def list_objects_v2(self, **kw):
        return {"KeyCount": 2 if kw["Prefix"].startswith("brand-identity/") else 0, "IsTruncated": False}


class FakeSM:
    def describe_secret(self, SecretId):
        if SecretId.startswith("acp/cms/"):
            return {"Name": SecretId}
        err = Exception("not found")
        err.response = {"Error": {"Code": "ResourceNotFoundException"}}
        raise err


AWS = {"erasure_aws": {"s3": FakeS3(), "secretsmanager": FakeSM(), "bucket": "aa-cis-bronze-test"}}


async def _run(pools, payload, env=None, now=None, resources=None):
    """Runs the handler as the worker would (a real shared.job row, the handler on the cis_app pool), then marks
    the job like the worker (superuser pool), including the terminal hook of a failed job."""
    admin, app = pools
    job_id, _ = await queue.enqueue(admin, ej.KIND, payload)
    row = await admin.fetchrow("SELECT * FROM shared.job WHERE id = $1::uuid", job_id)
    ctx = JobContext(app, queue._to_job(row), resources=resources if resources is not None else AWS)
    try:
        result = await ej.run_erasure(ctx, env=env or {}, now=now)
    except Exception:
        await admin.execute("UPDATE shared.job SET status = 'failed', finished_at = now() WHERE id = $1::uuid",
                            job_id)
        await registry.run_terminal_hook(app, job_id)
        raise
    await admin.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now(), "
                        "result = coalesce(result, '{}'::jsonb) || $2::jsonb WHERE id = $1::uuid",
                        job_id, json.dumps(result, default=str))
    return job_id, result


async def _tenant(pools, offboarded_days=None) -> str:
    admin, _ = pools
    tid = str(uuid.uuid4())
    await admin.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active, posts_per_week,
                                    cancelled_at)
        VALUES ($1::uuid, 'AA-839 synthetic', $2, 'starter', 60, $3, 3,
                CASE WHEN $4::int IS NULL THEN NULL ELSE now() - make_interval(days => $4::int) END)""",
                        tid, f"aa839-{tid[:8]}", offboarded_days is None, offboarded_days)
    return tid


async def _seed(pools, tid: str, stage: str) -> dict:
    """One row (or a few) per kind of table: content/config (delete), FK cycle (quarter plan), cost log (detach),
    daily rollup (merge), API usage, finished job, audit row (keep), and the review-round tables: a T2 version chain
    with its review row and A3 fingerprint, owner_scope atoms on an AA master tour, a retired pipeline_jobs row."""
    admin, _ = pools
    await admin.execute("INSERT INTO acp_shared.tenant_config (tenant_id, markets, channels) "
                        "VALUES ($1::uuid, ARRAY['VN'], ARRAY['blog'])", tid)
    plan_id, version_id = str(uuid.uuid4()), str(uuid.uuid4())
    await admin.execute("INSERT INTO acp_shared.quarter_plan (plan_id, tenant_id, year, quarter) "
                        "VALUES ($1::uuid, $2::uuid, 2026, 4)", plan_id, tid)
    await admin.execute("""INSERT INTO acp_shared.quarter_plan_version
                           (version_id, plan_id, version_no, payload, source, approval_status)
                           VALUES ($1::uuid, $2::uuid, 1, '{}'::jsonb, 'standard', 'pending')""", version_id, plan_id)
    await admin.execute("UPDATE acp_shared.quarter_plan SET current_version_id = $2::uuid WHERE plan_id = $1::uuid",
                        plan_id, version_id)
    await admin.execute("""INSERT INTO shared.notifications (tenant_id, actor_type, event_type, payload, target_roles)
                           VALUES ($1::uuid, 'system', 'aa839.test', '{"n": 1}'::jsonb, ARRAY['admin'])""", tid)
    await admin.execute("INSERT INTO shared.unmapped_market_requests (tenant_id, country_code) "
                        "VALUES ($1::uuid, 'ZZ')", tid)
    await admin.execute("""INSERT INTO shared.tenant_api_usage (id, tenant_id, endpoint, method, status_code, called_at,
                                                                actor_type)
                           VALUES (gen_random_uuid(), $1::uuid, '/v1/x', 'GET', 200, now(), 'tenant')""", tid)
    piece = str(uuid.uuid4())
    await admin.execute("""
        INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal,
                                         content_piece_id, created_at)
        VALUES ($1::uuid, $2, 'writer', 'm1', 10, 1, 0.5, '{}'::jsonb, $3::uuid, now()),
               ($1::uuid, $2, 'writer', 'm1', 10, 1, 0.25, '{}'::jsonb, NULL, now())""", tid, stage, piece)
    for tenant, calls in ((tid, 3), (None, 5)):     # the platform (NULL) row of the same key exists already
        await admin.execute("""
            INSERT INTO shared.llm_call_daily (day, app, tenant_id, stage, role, model, provider, account, is_shadow,
                                               calls, fallback_calls, tokens_in, tokens_out, cost_usd, updated_at)
            VALUES (DATE '2026-01-01', 'cis', $1::uuid, $2, 'writer', 'm1', 'bedrock', 'acc3', false,
                    $3, 0, 100, 10, 1.5, now())""", tenant, stage, calls)
    job_id, _ = await queue.enqueue(admin, "t9_write", {"tenant_id": tid, "piece_id": piece})
    await admin.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now() WHERE id = $1::uuid", job_id)
    await admin.execute("""INSERT INTO acp_shared.audit_log (id, tenant_id, actor, action, resource_type, resource_id,
                                                            details, created_at)
                           VALUES (gen_random_uuid(), $1::text, 'admin:t', 'agency.offboard', 'tenant', $1::text,
                                   '{}'::jsonb, now())""", tid)
    # T2: published tour -> version chain v1 <- v2 (v2.parent = v1, v1.job = the tenant's finished job)
    pub = await admin.fetchval("""
        INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, aa_name, tenant_id)
        VALUES (gen_random_uuid(), gen_random_uuid(), 'AA-839', $1::uuid) RETURNING id""", tid)
    v1 = await admin.fetchval("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, "
                              "job_id) VALUES ($1::uuid, $2, $3::uuid) RETURNING id", tid, pub, job_id)
    v2 = await admin.fetchval("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, "
                              "version_number, parent_version_id) VALUES ($1::uuid, $2, 2, $3) RETURNING id",
                              tid, pub, v1)
    await admin.execute("INSERT INTO acp_contract.atomize_day_fingerprint (tenant_tour_version_id, day_number, "
                        "fingerprint_hash, atomized_at) VALUES ($1, 1, 'h', now())", v1)
    # raw tours: the tenant's own (with a platform atom on it) and an AA master tour holding one atom the tenant owns
    own, master = str(uuid.uuid4()), str(uuid.uuid4())
    for tour, tenant in ((own, tid), (master, inv.PLATFORM_TENANT_ID)):
        await admin.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name) "
                            "VALUES ($1::uuid, $2::uuid, gen_random_uuid(), 'AA-839')", tour, tenant)
    for atom, tour, scope in ((f"aa839-{tid[:8]}-own", master, tid), (f"aa839-{tid[:8]}-plat", master, "platform"),
                              (f"aa839-{tid[:8]}-ontour", own, "platform")):
        await admin.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text) "
                            "VALUES ($1, $2::uuid, $3, 'atom')", atom, tour, scope)
    await admin.execute("INSERT INTO silver_aa_internal.review_queue (tour_id, tenant_id, tenant_tour_version_id) "
                        "VALUES ($1::uuid, $2::uuid, $3)", own, tid, v2)
    await admin.execute("INSERT INTO shared.pipeline_jobs (request, tenant) VALUES ('{}'::jsonb, $1), "
                        "('{}'::jsonb, $2)", tid, f"aa839-{tid[:8]}")
    return {"plan_id": plan_id, "version_id": version_id, "job_id": job_id, "v1": str(v1), "v2": str(v2),
            "master": master, "own_tour": own}


def _rows(result) -> dict:
    return {(r["table"], r["action"]): r["rows"] for r in result["rows"]}


# ── inventory completeness + every generated SQL runs ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_inventory_covers_every_tenant_table_and_the_plan_sql_runs(pool):
    _, app = pool
    async with app.acquire() as conn:
        catalog = await pl.load_catalog(conn)
        plan = pl.plan_tenant(inv.Registry(), catalog)
        assert plan.unregistered == [], (
            f"CIS tables with a tenant key and no entry in services/erasure/inventory.py: {plan.unregistered}")
        assert set(plan.missing) <= CI_ABSENT - {"gold_aa_internal.tenant_tour_versions"}, plan.missing
        assert {"acp_contract.tour_atoms", "shared.pipeline_jobs", "acp_contract.atomize_day_fingerprint"} <= \
            set(catalog.tenant_keys)                              # the widened guard sees them
        assert not [c for c in plan.checks if c.kind in ("unsupported_cycle", "too_deep")]
        assert plan.order[-1] == "shared.tenants"
        assert "acp_shared.quarter_plan_version" in plan.nodes          # reached through plan_id
        assert {"self_reference"} <= {c.kind for c in plan.checks}      # job.parent_job_id, ttv.parent_version_id
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            out = await pl.count_tenant(conn, plan, str(uuid.uuid4()))   # every count / check SQL executes
    assert all(r["rows"] == 0 for r in out["rows"])
    assert not [b for b in out["blockers"] if b["rows"] is None], out["blockers"]


# ── tenant ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_dry_run_counts_and_writes_nothing(pool):
    admin, _ = pool
    tid, other = await _tenant(pool, offboarded_days=40), await _tenant(pool)
    stage = f"adhoc_aa839_{uuid.uuid4().hex[:8]}"
    await _seed(pool, tid, stage)
    await _seed(pool, other, stage + "_o")
    _, res = await _run(pool, {"subject": "tenant", "tenant": tid})
    rows = _rows(res)
    assert res["mode"] == "dry_run" and not res["blocked"], res["blockers"]
    assert rows[("shared.tenants", "delete")] == 1
    assert rows[("acp_shared.tenant_config", "delete")] == 1
    assert rows[("acp_shared.quarter_plan", "delete")] == 1
    assert rows[("acp_shared.quarter_plan_version", "cascade")] == 1
    assert rows[("acp_shared.quarter_plan", "null_first:current_version_id")] == 1
    assert rows[("shared.llm_call_log", "detach")] == 2
    assert rows[("shared.llm_call_daily", "merge")] == 1
    assert rows[("shared.job", "delete")] == 1
    assert rows[("acp_shared.audit_log", "keep")] == 1
    # review round: T2 chain, its review row and A3 fingerprint, owner_scope + tour-path atoms, retired run-tour jobs
    assert rows[("gold_aa_internal.tenant_tour_versions", "delete")] == 2
    assert rows[("silver_aa_internal.review_queue", "delete")] == 1
    assert rows[("acp_contract.atomize_day_fingerprint", "delete")] == 1
    assert rows[("acp_contract.tour_atoms", "delete")] == 2            # owned on the master tour + on its own tour
    assert rows[("shared.pipeline_jobs", "delete")] == 2               # tenant id and slug
    assert res["tenant_state"] == "offboarded" and res["unregistered"] == []
    assert res["s3"][1] == {"prefix": f"bronze:brand-identity/{tid}/", "objects": 2}
    assert {s["name"]: s["exists"] for s in res["secrets"]} == {f"acp/cms/{tid}": True, f"acp/social/{tid}": False}
    assert any(i["owner"] == "AA-Partner (crm)" for i in res["checklist"])
    # nothing written
    assert await admin.fetchval("SELECT count(*) FROM shared.tenants WHERE tenant_id = $1::uuid", tid) == 1
    assert await admin.fetchval("SELECT count(*) FROM shared.llm_call_log WHERE tenant_id = $1::uuid", tid) == 2


@pytest.mark.asyncio
async def test_tenant_dry_run_lists_s3_keys_outside_the_tenant_prefixes(pool):
    admin, _ = pool
    tid = await _tenant(pool, offboarded_days=40)
    await admin.execute("UPDATE shared.tenants SET last_brand_brief_s3_key = $2 WHERE tenant_id = $1::uuid",
                        tid, f"s3://aa-cis-bronze-test/brand-identity/{tid}/brief.json")       # inside: not listed
    await admin.execute("INSERT INTO gold_aa_internal.content_exports (tenant_id, export_id, format, s3_path, status) "
                        "VALUES ($1::uuid, gen_random_uuid(), 'json', $2, 'done')", tid, f"exports/{tid}/e1.json")
    _, res = await _run(pool, {"subject": "tenant", "tenant": tid})
    refs = {r["column"]: r for r in res["s3_refs"]}
    assert refs["shared.tenants.last_brand_brief_s3_key"]["outside_keys"] == []
    exp = refs["gold_aa_internal.content_exports.s3_path"]
    assert exp["outside_keys"] == [f"exports/{tid}/e1.json"] and exp["outside_dirs"] == [f"exports/{tid}/"]
    assert "silver_aa_internal.raw_sources.s3_path" in refs and not res["blocked"], res["blockers"]


@pytest.mark.asyncio
async def test_tenant_apply_refusals(pool):
    admin, _ = pool
    tid = await _tenant(pool, offboarded_days=3)
    await _seed(pool, tid, f"adhoc_aa839_{uuid.uuid4().hex[:8]}")
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    apply = {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
             "approved_fingerprint": dry["fingerprint"], "approved_by": "Nghiep", "snapshot_id": "dsar-test-pre"}
    with pytest.raises(NonRetryable, match=ej.APPLY_FLAG):
        await _run(pool, apply)                                   # flag off (the default everywhere)
    with pytest.raises(NonRetryable, match="less than 30 days"):
        await _run(pool, apply, env=ON)                           # offboarded 3 days ago
    env = dict(ON, ERASURE_TENANT_RETENTION_DAYS="0")
    extra = await admin.fetchval("INSERT INTO shared.unmapped_market_requests (tenant_id, country_code) "
                                 "VALUES ($1::uuid, 'ZY') RETURNING id", tid)   # counts change after the approval
    with pytest.raises(NonRetryable, match="counts changed"):
        await _run(pool, apply, env=env)
    # that apply claimed the DRY RUN: even with the counts back, the same approval cannot be used again
    await admin.execute("DELETE FROM shared.unmapped_market_requests WHERE id = $1", extra)
    with pytest.raises(NonRetryable, match="already used by apply job"):
        await _run(pool, apply, env=env)
    assert await admin.fetchval("SELECT count(*) FROM shared.tenants WHERE tenant_id = $1::uuid", tid) == 1


@pytest.mark.asyncio
async def test_a_restore_after_the_first_checks_stops_the_apply_inside_the_transaction(pool, monkeypatch):
    admin, _ = pool
    tid = await _tenant(pool, offboarded_days=40)
    await _seed(pool, tid, f"adhoc_aa839_{uuid.uuid4().hex[:8]}")
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    # the restore lands after apply_refusals read the (still offboarded) row and before the transaction; it writes
    # no audit row here, so the counts (and the fingerprint) stay the same: only the locked re-check can stop it.
    # AA-978 (CIS 276): a real restore also writes a tenant.unlocked feed event, which alone changes the counts; the
    # feed trigger is off for this one statement so the test still reaches the locked re-check.
    stale = dict(await admin.fetchrow("SELECT tenant_id, is_active, cancelled_at FROM shared.tenants "
                                      "WHERE tenant_id = $1::uuid", tid))
    async with admin.acquire() as c, c.transaction():
        await c.execute("ALTER TABLE shared.tenants DISABLE TRIGGER tenants_membership_feed_upd")
        await c.execute("UPDATE shared.tenants SET cancelled_at = NULL, is_active = true WHERE tenant_id = $1::uuid",
                        tid)
        await c.execute("ALTER TABLE shared.tenants ENABLE TRIGGER tenants_membership_feed_upd")
    real = ej.apply_refusals
    monkeypatch.setattr(ej, "apply_refusals",
                        lambda payload, dry_job, tenant_row, now, env=None: real(payload, dry_job, stale, now, env))
    with pytest.raises(NonRetryable, match="inside the transaction: the tenant is not offboarded"):
        await _run(pool, {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
                          "approved_fingerprint": dry["fingerprint"], "approved_by": "N", "snapshot_id": "s"}, env=ON)
    assert await admin.fetchval("SELECT count(*) FROM gold_aa_internal.tenant_tour_versions "
                                "WHERE tenant_id = $1::uuid", tid) == 2


@pytest.mark.asyncio
async def test_tenant_apply_erases_children_first_and_leaves_other_tenants(pool):
    admin, _ = pool
    tid, other = await _tenant(pool, offboarded_days=40), await _tenant(pool)
    stage = f"adhoc_aa839_{uuid.uuid4().hex[:8]}"
    seeded = await _seed(pool, tid, stage)
    await _seed(pool, other, stage + "_o")
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    assert not dry["blocked"], dry["blockers"]
    _, res = await _run(pool, {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
                               "approved_fingerprint": dry["fingerprint"], "approved_by": "Nghiep",
                               "snapshot_id": "dsar-test-pre", "actor": "admin:test"}, env=ON)
    assert res["mode"] == "apply" and res["verified"] is True, res.get("verified_left")
    assert await admin.fetchval("SELECT count(*) FROM shared.tenants WHERE tenant_id = $1::uuid", tid) == 0
    assert await admin.fetchval("SELECT count(*) FROM acp_shared.quarter_plan_version WHERE version_id = $1::uuid",
                                seeded["version_id"]) == 0
    assert await admin.fetchval("SELECT count(*) FROM shared.job WHERE id = $1::uuid", seeded["job_id"]) == 0
    # review round: the T2 chain, its review row and fingerprint, the atoms the tenant owned, the retired jobs
    assert await admin.fetchval("SELECT count(*) FROM gold_aa_internal.tenant_tour_versions "
                                "WHERE tenant_id = $1::uuid", tid) == 0
    assert await admin.fetchval("SELECT count(*) FROM acp_contract.atomize_day_fingerprint "
                                "WHERE tenant_tour_version_id = $1::uuid", seeded["v1"]) == 0
    atoms = {r["atom_id"] for r in await admin.fetch("SELECT atom_id FROM acp_contract.tour_atoms "
                                                     "WHERE atom_id LIKE $1", f"aa839-{tid[:8]}-%")}
    assert atoms == {f"aa839-{tid[:8]}-plat"}                       # the master tour's platform atom stays
    assert await admin.fetchval("SELECT count(*) FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid",
                                seeded["master"]) == 1
    assert await admin.fetchval("SELECT count(*) FROM shared.pipeline_jobs WHERE tenant IN ($1, $2)",
                                tid, f"aa839-{tid[:8]}") == 0
    # cost history kept without the tenant link
    log = await admin.fetch("SELECT tenant_id, content_piece_id FROM shared.llm_call_log WHERE stage = $1", stage)
    assert len(log) == 2 and all(r["tenant_id"] is None and r["content_piece_id"] is None for r in log)
    daily = await admin.fetch("SELECT tenant_id, calls FROM shared.llm_call_daily WHERE stage = $1", stage)
    assert [(r["tenant_id"], r["calls"]) for r in daily] == [(None, 8)]
    # audit chain kept + one erasure row, counts only
    actions = [r["action"] for r in await admin.fetch(
        "SELECT action FROM acp_shared.audit_log WHERE tenant_id = $1::text ORDER BY created_at", tid)]
    assert actions == ["agency.offboard", "tenant.erased"]
    details = await admin.fetchval("SELECT details FROM acp_shared.audit_log WHERE tenant_id = $1::text "
                                   "AND action = 'tenant.erased'", tid)
    details = json.loads(details) if isinstance(details, str) else details
    assert details["dry_run_job_id"] == dry_id and details["snapshot_id"] == "dsar-test-pre"
    # the other tenant is untouched
    assert await admin.fetchval("SELECT count(*) FROM acp_shared.quarter_plan WHERE tenant_id = $1::uuid", other) == 1
    assert await admin.fetchval("SELECT count(*) FROM shared.llm_call_log WHERE tenant_id = $1::uuid", other) == 2
    assert await admin.fetchval("SELECT count(*) FROM gold_aa_internal.tenant_tour_versions "
                                "WHERE tenant_id = $1::uuid", other) == 2
    # independent verification: a new DRY RUN finds nothing but the kept audit rows
    _, again = await _run(pool, {"subject": "tenant", "tenant": tid})
    left = {k: v for k, v in _rows(again).items() if v}
    assert left == {("acp_shared.audit_log", "keep"): 2}, left
    assert again["tenant_state"] is None


@pytest.mark.asyncio
async def test_a_row_of_another_tenant_pointing_at_erased_rows_blocks(pool):
    admin, _ = pool
    tid, other = await _tenant(pool, offboarded_days=40), await _tenant(pool)
    seeded = await _seed(pool, tid, f"adhoc_aa839_{uuid.uuid4().hex[:8]}")
    o = await _seed(pool, other, f"adhoc_aa839_{uuid.uuid4().hex[:8]}_o")
    await admin.execute("INSERT INTO acp_shared.quarter_plan (plan_id, tenant_id, year, quarter, current_version_id) "
                        "VALUES (gen_random_uuid(), $1::uuid, 2026, 3, $2::uuid)", other, seeded["version_id"])
    # review round: another tenant's review row and T2 version (self FK) point at the tenant's versions, and a child
    # job without a tenant in its payload points at the tenant's finished job (shared.job self FK)
    await admin.execute("INSERT INTO silver_aa_internal.review_queue (tour_id, tenant_id, tenant_tour_version_id) "
                        "VALUES ($1::uuid, $2::uuid, $3::uuid)", o["own_tour"], other, seeded["v1"])
    await admin.execute("UPDATE gold_aa_internal.tenant_tour_versions SET parent_version_id = $2::uuid "
                        "WHERE id = $1::uuid", o["v1"], seeded["v2"])
    child, _ = await queue.enqueue(admin, "t9_write", {"note": "no tenant"})
    await admin.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now(), parent_job_id = $2::uuid "
                        "WHERE id = $1::uuid", child, seeded["job_id"])
    q_id, _ = await queue.enqueue(admin, "t9_write", {"tenant_id": tid})          # still queued
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    kinds = {(b["kind"], b["table"]) for b in dry["blockers"]}
    assert ("cross_tenant", "acp_shared.quarter_plan") in kinds and ("live_jobs", "shared.job") in kinds
    assert ("cross_tenant", "silver_aa_internal.review_queue") in kinds
    assert ("self_reference", "gold_aa_internal.tenant_tour_versions") in kinds
    assert ("self_reference", "shared.job") in kinds
    assert not [b for b in dry["blockers"] if b["rows"] is None], dry["blockers"]
    with pytest.raises(NonRetryable, match="blockers"):
        await _run(pool, {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
                          "approved_fingerprint": dry["fingerprint"], "approved_by": "N", "snapshot_id": "s"}, env=ON)
    await admin.execute("DELETE FROM shared.job WHERE id = ANY($1::uuid[])", [q_id, child])


@pytest.mark.asyncio
async def test_a_revoked_privilege_and_an_unreadable_foreign_table_are_blockers(pool):
    """As cis_app: DELETE revoked on a table with rows -> `privilege` blocker; another owner's table with an FK into
    shared.tenants that cis_app cannot read -> `foreign_owner` "not readable", and every later check still runs (one
    savepoint per check, no aborted transaction)."""
    admin, _ = pool
    tid = await _tenant(pool, offboarded_days=40)
    await _seed(pool, tid, f"adhoc_aa839_{uuid.uuid4().hex[:8]}")
    made_schema = await admin.fetchval("SELECT to_regnamespace('booking') IS NULL")
    if made_schema:
        await admin.execute("CREATE SCHEMA booking")
    await admin.execute("CREATE TABLE booking.aa839_ref (id uuid PRIMARY KEY, "
                        "tenant_id uuid REFERENCES shared.tenants (tenant_id))")
    await admin.execute("REVOKE DELETE ON shared.unmapped_market_requests FROM cis_app")
    try:
        _, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    finally:
        await admin.execute("GRANT DELETE ON shared.unmapped_market_requests TO cis_app")
        await admin.execute("DROP TABLE booking.aa839_ref")
        if made_schema:
            await admin.execute("DROP SCHEMA booking")
    by = {(b["kind"], b["table"]): b for b in dry["blockers"]}
    assert ("privilege", "shared.unmapped_market_requests") in by
    assert "not readable" in by[("foreign_owner", "booking.aa839_ref")]["detail"]
    unreadable = [b for b in dry["blockers"] if b["rows"] is None]
    assert [b["table"] for b in unreadable] == ["booking.aa839_ref"], unreadable     # nothing mislabelled after it
    assert dry["checks_passed"], "the checks after the failed one must still run"


# ── traveller ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_traveller_dry_run_then_apply(pool):
    admin, _ = pool
    address = f"Traveller.{uuid.uuid4().hex[:8]}@Example.com"
    h = hashlib.sha256(address.strip().lower().encode()).hexdigest()
    tid = await _tenant(pool)
    notif = await admin.fetchval(
        """INSERT INTO shared.notifications (tenant_id, actor_type, event_type, payload, target_roles)
           VALUES ($1::uuid, 'system', 'aa839.test', jsonb_build_object('from', 'Name <' || $2 || '>'),
                   ARRAY['admin']) RETURNING id""", tid, address)
    bad_job, _ = await queue.enqueue(admin, "t9_write", {"tenant_id": tid, "note": f"reply to {address.lower()}"})
    await admin.execute("UPDATE shared.job SET status = 'failed', finished_at = now() WHERE id = $1::uuid", bad_job)
    other = await admin.fetchval(
        """INSERT INTO shared.notifications (tenant_id, actor_type, event_type, payload, target_roles)
           VALUES ($1::uuid, 'system', 'aa839.test', '{"from": "someone.else@example.com"}'::jsonb, ARRAY['admin'])
           RETURNING id""", tid)

    first_id, _ = await _run(pool, {"subject": "traveller", "email_sha256": h})     # an earlier, superseded DRY RUN
    dry_id, dry = await _run(pool, {"subject": "traveller", "email_sha256": h})
    rows = {r["table"]: r for r in dry["rows"]}
    assert rows["shared.notifications:payload"]["ids"] == [str(notif)]
    assert rows["shared.job:payload"]["ids"] == [bad_job] and rows["shared.job:payload"]["action"] == "redact"
    assert rows["shared.admin_users:email"]["rows"] == 0 and not dry["blocked"]
    assert address.lower() not in json.dumps(dry) and address not in json.dumps(dry)
    assert any(i["owner"] == "AA-Booking" for i in dry["checklist"])

    _, res = await _run(pool, {"subject": "traveller", "email_sha256": h, "mode": "apply", "dry_run_job_id": dry_id,
                               "approved_fingerprint": dry["fingerprint"], "approved_by": "Nghiep",
                               "snapshot_id": "dsar-test-pre"}, env=ON)
    assert res["verified"] is True
    assert await admin.fetchval("SELECT count(*) FROM shared.notifications WHERE id = $1", notif) == 0
    assert await admin.fetchval("SELECT count(*) FROM shared.notifications WHERE id = $1", other) == 1
    payload = await admin.fetchval("SELECT payload FROM shared.job WHERE id = $1::uuid", bad_job)
    assert (json.loads(payload) if isinstance(payload, str) else payload) == {"redacted": "AA-839"}
    # the hash is gone from every erasure job row, the superseded DRY RUN included
    assert await admin.fetchval("SELECT count(*) FROM shared.job WHERE payload::text LIKE '%' || $1 || '%'", h) == 0
    stripped = await admin.fetchval("SELECT payload ->> 'email_sha256' FROM shared.job WHERE id = $1::uuid", first_id)
    assert stripped == ej.STRIPPED
    # the DRY RUN salts went with the hash: the fingerprints left in the rows cannot be matched to an address
    assert dry["subject_salt"] and dry["fingerprint_version"] == pl.FINGERPRINT_VERSION
    assert await admin.fetchval("SELECT count(*) FROM shared.job WHERE kind = $1 AND result ? 'subject_salt' "
                                "AND id = ANY($2::uuid[])", ej.KIND, [first_id, dry_id]) == 0
    audit = await admin.fetchval("SELECT details::text FROM acp_shared.audit_log WHERE action = 'dsar.cis_erased' "
                                 "AND details ->> 'dry_run_job_id' = $1", dry_id)
    assert audit and h not in audit
    # the verification DRY RUN finds nothing and leaves no copy of the hash behind
    ver_id, again = await _run(pool, {"subject": "traveller", "email_sha256": h, "verify": True})
    assert all(r["rows"] == 0 for r in again["rows"]) and again["hash_stripped"] is True
    assert await admin.fetchval("SELECT payload ->> 'email_sha256' FROM shared.job WHERE id = $1::uuid",
                                ver_id) == ej.STRIPPED_VERIFY
    # ... and no salt either: neither in what it returns nor in its stored row (the worker writes the result after
    # the strip, so a salt in the result would outlive the hash and make the fingerprint testable again)
    assert "subject_salt" not in again
    assert await admin.fetchval("SELECT result ? 'subject_salt' FROM shared.job WHERE id = $1::uuid", ver_id) is False
    assert await admin.fetchval("SELECT count(*) FROM shared.job WHERE kind = $1 AND payload ->> 'email_sha256' "
                                "LIKE 'stripped%' AND result ? 'subject_salt'", ej.KIND) == 0


@pytest.mark.asyncio
async def test_a_failed_traveller_job_keeps_no_hash(pool):
    admin, _ = pool
    h = hashlib.sha256(f"x{uuid.uuid4().hex}@example.com".encode()).hexdigest()
    dry_id, dry = await _run(pool, {"subject": "traveller", "email_sha256": h})
    with pytest.raises(NonRetryable, match=ej.APPLY_FLAG):
        await _run(pool, {"subject": "traveller", "email_sha256": h, "mode": "apply", "dry_run_job_id": dry_id,
                          "approved_fingerprint": dry["fingerprint"], "approved_by": "N", "snapshot_id": "s"})
    failed = await admin.fetchrow("SELECT id::text AS id, payload ->> 'email_sha256' AS h FROM shared.job "
                                  "WHERE kind = $1 AND status = 'failed' AND payload ->> 'dry_run_job_id' = $2",
                                  ej.KIND, dry_id)
    assert failed["h"] == "stripped-on-failed"


@pytest.mark.asyncio
async def test_same_counts_for_two_tenants_give_two_fingerprints(pool, monkeypatch):
    """AA-839 review: the fingerprint names the subject. Two bare offboarded tenants have identical counts; an
    approval of tenant A's fingerprint does not apply tenant B (v1 fingerprints were equal here). A tenant DRY RUN
    draws no salt (only a traveller fingerprint needs one)."""
    def no_salt(*_a, **_k):
        raise AssertionError("a tenant DRY RUN must not draw a salt")
    monkeypatch.setattr(ej.secrets, "token_hex", no_salt)
    a, b = await _tenant(pool, offboarded_days=40), await _tenant(pool, offboarded_days=40)
    _, dry_a = await _run(pool, {"subject": "tenant", "tenant": a})
    dry_b_id, dry_b = await _run(pool, {"subject": "tenant", "tenant": b})
    assert _rows(dry_a) == _rows(dry_b) and dry_a["fingerprint"] != dry_b["fingerprint"]
    # a tenant fingerprint is keyed by the tenant id alone: no salt is drawn or stored
    assert "subject_salt" not in dry_a and "subject_salt" not in dry_b
    assert dry_b["fingerprint"] == pl.fingerprint("tenant", b, dry_b["rows"])
    admin, _ = pool
    assert await admin.fetchval("SELECT result ? 'subject_salt' FROM shared.job WHERE id = $1::uuid",
                                dry_b_id) is False
    with pytest.raises(NonRetryable, match="approved_fingerprint differs"):
        await _run(pool, {"subject": "tenant", "tenant": b, "mode": "apply", "dry_run_job_id": dry_b_id,
                          "approved_fingerprint": dry_a["fingerprint"], "approved_by": "N", "snapshot_id": "s"},
                   env=dict(ON, ERASURE_TENANT_RETENTION_DAYS="0"))
    assert await admin.fetchval("SELECT count(*) FROM shared.tenants WHERE tenant_id = $1::uuid", b) == 1


@pytest.mark.asyncio
async def test_a_v1_dry_run_is_refused_and_not_claimed(pool):
    """AA-839 review: a DRY RUN made before fingerprint v2 (no fingerprint_version) cannot be applied, and the
    refusal happens before the claim, so the DRY RUN row carries no used_by_apply."""
    admin, _ = pool
    tid = await _tenant(pool, offboarded_days=40)
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    await admin.execute("UPDATE shared.job SET result = result - 'fingerprint_version' WHERE id = $1::uuid", dry_id)
    with pytest.raises(NonRetryable, match="predates the subject-bound fingerprint"):
        await _run(pool, {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
                          "approved_fingerprint": dry["fingerprint"], "approved_by": "N", "snapshot_id": "s"},
                   env=dict(ON, ERASURE_TENANT_RETENTION_DAYS="0"))
    assert await admin.fetchval("SELECT result ? 'used_by_apply' FROM shared.job WHERE id = $1::uuid",
                                dry_id) is False
    assert await admin.fetchval("SELECT count(*) FROM shared.tenants WHERE tenant_id = $1::uuid", tid) == 1


@pytest.mark.asyncio
async def test_strip_hash_leaves_a_non_object_result_alone(pool):
    """AA-839 review: `jsonb - key` raises on a scalar; the strip must not fail (and so must not leave the hash)
    on a job row whose result is not an object."""
    admin, app = pool
    h = hashlib.sha256(f"s{uuid.uuid4().hex}@example.com".encode()).hexdigest()
    job_id, _ = await queue.enqueue(admin, ej.KIND, {"subject": "traveller", "email_sha256": h, "mode": "dry_run"})
    await admin.execute("UPDATE shared.job SET result = '\"done\"'::jsonb WHERE id = $1::uuid", job_id)
    async with app.acquire() as conn:
        await ej._strip_hash(conn, [job_id], h, ej.STRIPPED)
    row = await admin.fetchrow("SELECT payload ->> 'email_sha256' AS h, result::text AS r FROM shared.job "
                               "WHERE id = $1::uuid", job_id)
    assert row["h"] == ej.STRIPPED and row["r"] == '"done"'


@pytest.mark.asyncio
async def test_now_is_checked_against_the_dry_run_age(pool):
    tid = await _tenant(pool, offboarded_days=40)
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    with pytest.raises(NonRetryable, match="older than"):
        await _run(pool, {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
                          "approved_fingerprint": dry["fingerprint"], "approved_by": "N", "snapshot_id": "s"},
                   env=ON, now=later)
