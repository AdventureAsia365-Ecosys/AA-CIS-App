"""AA-1093 part 3 — per-tenant CIS backup and restore drill against real Postgres (every migration replayed).

The job and the drill run logged in AS cis_app (CIS 244 in the Dev order, as test_aa839_erasure_job does), so the
grants and RLS are the ones the worker and the API container meet on Dev. Seeding and the worker's own status updates
use the superuser. The bucket is a local directory (DirStore). Synthetic data only, invented names. Covers:
- the table catalogue on the replayed schema: nothing unregistered, and every export SQL runs (a tenant with no rows);
- a backup holds the tenant's rows only (never another tenant's, never the platform's atoms on an AA master tour),
  its manifest counts equal the live counts, every file matches SHA256SUMS;
- the drill restores every table into pg_temp with the live types, manifest = restored = live, every row identical
  to its live row, writes one tenant.restore_drill audit row (counts only) and leaves the live tables untouched;
- tampering (a table file, SHA256SUMS) fails the drill; an unknown tenant, the platform tenant and a tenant without
  a backup are refused; the size cap skips tables whole and the drill then reports an incomplete backup (exit 3).
"""
import gzip
import json
import os
import sys
import uuid

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
from services.jobs import tenant_backup_job as tbj  # noqa: E402
from services.tenant_backup import catalogue as cat  # noqa: E402
from services.tenant_backup import export, restore_drill  # noqa: E402
from services.tenant_backup.store import DirStore  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs.registry import JobContext, NonRetryable  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
APP_DSN = f"postgresql://cis_app:{CIS_APP_CI_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATION_244 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "244_x1_cis_app_runtime.sql")


def _body_244_without_guards() -> str:
    sql = open(MIGRATION_244, encoding="utf-8").read()
    return "BEGIN;\n" + sql[sql.index("-- ── Guard 0"):sql.index("-- ── 4. Register")] + "\nCOMMIT;\n"


@pytest.fixture(scope="module", autouse=True)
def dev_shape():
    """CIS 244 in the Dev order (as test_aa839_erasure_job), undone after."""
    admin = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    admin.autocommit = True
    a = admin.cursor()
    try:
        x1_244_ci_shape(a)
        a.execute(_body_244_without_guards())
        a.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                  "VALUES ('244', now(), 'test_aa1093_tenant_backup') ON CONFLICT (version) DO NOTHING")
        x1_244_ci_unshape(a)
        a.execute("ALTER ROLE cis_app LOGIN")
        yield
    finally:
        a.execute("DELETE FROM shared.job WHERE kind = 'tenant_backup'")
        x1_244_undo(a)
        admin.close()


@pytest_asyncio.fixture
async def pools():
    admin = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    app = await asyncpg.create_pool(APP_DSN, min_size=1, max_size=3)
    assert await app.fetchval("SELECT current_user") == "cis_app"
    yield admin, app
    await app.close()
    await admin.close()


@pytest.fixture
def store(tmp_path):
    return DirStore(str(tmp_path / "gold"))


async def _tenant(admin) -> str:
    tid = str(uuid.uuid4())
    await admin.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active, posts_per_week)
        VALUES ($1::uuid, 'Lanternfold Travel (synthetic)', $2, 'starter', 60, true, 3)""", tid, f"zz1093-{tid[:8]}")
    return tid


async def _seed(admin, tid: str) -> dict:
    """Rows of the tenant in exported tables (config, a quarter plan and its version, a bell, a market request, its
    own raw tour with a platform atom on it, an atom it owns on an AA master tour, a published row) and in excluded
    ones (cost log, audit row); the AA master tour also holds a platform atom that must never be backed up."""
    await admin.execute("INSERT INTO acp_shared.tenant_config (tenant_id, markets, channels) "
                        "VALUES ($1::uuid, ARRAY['VN'], ARRAY['blog'])", tid)
    plan_id = str(uuid.uuid4())
    await admin.execute("INSERT INTO acp_shared.quarter_plan (plan_id, tenant_id, year, quarter) "
                        "VALUES ($1::uuid, $2::uuid, 2026, 4)", plan_id, tid)
    await admin.execute("""INSERT INTO acp_shared.quarter_plan_version
                           (version_id, plan_id, version_no, payload, source, approval_status)
                           VALUES (gen_random_uuid(), $1::uuid, 1, '{"weeks": [1, 2]}'::jsonb, 'standard',
                                   'pending')""", plan_id)
    await admin.execute("""INSERT INTO shared.notifications (tenant_id, actor_type, event_type, payload, target_roles)
                           VALUES ($1::uuid, 'system', 'zz1093.test', '{"n": 1}'::jsonb, ARRAY['admin'])""", tid)
    await admin.execute("INSERT INTO shared.unmapped_market_requests (tenant_id, country_code) VALUES ($1::uuid, 'ZZ')",
                        tid)
    own, master = str(uuid.uuid4()), str(uuid.uuid4())
    for tour, tenant, name in ((own, tid, "Mistral Ridge loop"), (master, inv.PLATFORM_TENANT_ID, "Brinmoor harbour")):
        await admin.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name) "
                            "VALUES ($1::uuid, $2::uuid, gen_random_uuid(), $3)", tour, tenant, name)
    plat_atom = f"zz1093-{tid[:8]}-plat"
    for atom, tour, scope in ((f"zz1093-{tid[:8]}-own", master, tid), (plat_atom, master, "platform"),
                              (f"zz1093-{tid[:8]}-ontour", own, "platform")):
        await admin.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text) "
                            "VALUES ($1, $2::uuid, $3, 'A slow morning walk.')", atom, tour, scope)
    await admin.execute("""INSERT INTO gold_aa_internal.published_tours
                           (tour_id, generated_content_id, aa_name, tenant_id)
                           VALUES ($1::uuid, gen_random_uuid(), 'Mistral Ridge loop', $2::uuid)""", own, tid)
    await admin.execute("""INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, tokens_in, tokens_out,
                                                            cost_usd, quality_signal, created_at)
                           VALUES ($1::uuid, 'zz1093', 'writer', 'm1', 10, 1, 0.5, '{}'::jsonb, now())""", tid)
    await admin.execute("""INSERT INTO acp_shared.audit_log (id, tenant_id, actor, action, resource_type, resource_id,
                                                            details, created_at)
                           VALUES (gen_random_uuid(), $1::text, 'admin:t', 'agency.onboard', 'tenant', $1::text,
                                   '{}'::jsonb, now())""", tid)
    return {"plat_atom": plat_atom, "master": master}


async def _backup(pools, store, tid, max_bytes=export.DEFAULT_MAX_BYTES) -> tuple[str, dict]:
    """The job as the worker runs it: a real shared.job row, the handler on the cis_app pool, then succeeded."""
    admin, app = pools
    job_id, _ = await queue.enqueue(admin, tbj.KIND, {"tenant_id": tid, "trigger": "manual"})
    row = await admin.fetchrow("SELECT * FROM shared.job WHERE id = $1::uuid", job_id)
    ctx = JobContext(app, queue._to_job(row), resources={"tenant_backup_store": store})
    os.environ["TENANT_BACKUP_MAX_BYTES"] = str(max_bytes)
    try:
        result = await tbj.run(ctx)
    finally:
        os.environ.pop("TENANT_BACKUP_MAX_BYTES", None)
    await admin.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now(), result = $2::jsonb "
                        "WHERE id = $1::uuid", job_id, json.dumps(result, default=str))
    return job_id, result


async def _live_fingerprint(admin, tid: str) -> dict:
    """md5 of every live row the backup selects for the tenant, per table (the drill must not change one)."""
    catalog = await pl.load_catalog(admin)
    bp = cat.backup_plan(pl.plan_tenant(inv.Registry(), catalog))
    out = {}
    for t in bp.tables:
        out[t.table] = await admin.fetchval(
            f"SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '|' ORDER BY to_jsonb(t)::text), '')) "
            f"FROM {pl.qname(t.table)} t WHERE {t.where}", tid)
    return out


def _lines(store: DirStore, prefix: str, rel: str) -> list[str]:
    with gzip.open(os.path.join(store.root, *(prefix + rel).split("/")), "rt", encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln]


# ── catalogue on the replayed schema ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_catalogue_is_complete_and_every_export_sql_runs(pools, store):
    admin, _ = pools
    catalog = await pl.load_catalog(admin)
    bp = cat.backup_plan(pl.plan_tenant(inv.Registry(), catalog))
    assert bp.unregistered == []
    tid = await _tenant(admin)
    _, result = await _backup(pools, store, tid)
    assert result["complete"] is True and result["skipped"] == []
    # the tenant row and what its insert triggers write (subscription, entitlements, feed events), nothing else
    assert result["tables"] == len(bp.tables) and result["rows"] >= 1


# ── backup + drill round trip ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backup_holds_the_tenant_rows_only_and_the_drill_restores_them(pools, store):
    admin, app = pools
    tid, other = await _tenant(admin), await _tenant(admin)
    seeded = await _seed(admin, tid)
    await _seed(admin, other)
    job_id, result = await _backup(pools, store, tid)
    prefix = cat.object_prefix(tid, job_id)
    assert result["prefix"] == prefix and result["complete"] is True

    sums = export.parse_sums(open(os.path.join(store.root, *(prefix + "SHA256SUMS").split("/"))).read())
    manifest = json.load(open(os.path.join(store.root, *(prefix + "manifest.json").split("/"))))
    assert manifest["tenant_id"] == tid and manifest["backup_id"] == job_id and manifest["format"] == cat.FORMAT
    excluded = {e["table"] for e in manifest["excluded"]}
    assert {"shared.llm_call_log", "acp_shared.audit_log", "shared.job"} <= excluded
    by_table = {e["table"]: e for e in manifest["tables"]}
    expect = {"shared.tenants": 1, "acp_shared.tenant_config": 1, "acp_shared.quarter_plan": 1,
              "acp_shared.quarter_plan_version": 1, "shared.notifications": 1, "shared.unmapped_market_requests": 1,
              "silver_aa_internal.raw_tours": 1, "acp_contract.tour_atoms": 2, "gold_aa_internal.published_tours": 1}
    for table, n in expect.items():
        assert by_table[table]["rows"] == n, table
    for e in manifest["tables"]:
        assert sums[e["file"]] == e["sha256"]
        lines = _lines(store, prefix, e["file"])
        assert len(lines) == e["rows"]
        blob = "\n".join(lines)
        assert other not in blob, e["table"]                          # never another tenant's row
        assert seeded["plat_atom"] not in blob                        # never the platform atoms of the AA catalogue
    assert manifest["restore_order"].index("shared.tenants") < manifest["restore_order"].index(
        "acp_shared.tenant_config")

    before = await _live_fingerprint(admin, tid)
    audits = await admin.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE action = 'tenant.restore_drill'")
    report = await restore_drill.drill(app, store, tid)
    assert report["ok"] is True and report["complete"] is True and restore_drill.exit_code(report) == 0
    assert report["backup_id"] == job_id and report["checksums_verified"] == len(manifest["tables"]) + 1
    assert report["mismatched_tables"] == [] and report["live_differs"] == []
    for r in report["tables"]:
        assert r["manifest_rows"] == r["restored_rows"] == r["live_rows"], r
        if r["live_pk_matched"] is not None:
            assert r["live_pk_matched"] == r["live_identical"] == r["restored_rows"], r
    # nothing of the drill outlives it: live rows unchanged, no temporary table left, one audit row (counts only)
    assert await _live_fingerprint(admin, tid) == before
    async with app.acquire() as c:
        assert await c.fetchval("SELECT count(*) AS n FROM pg_class WHERE relname LIKE 'rd\\_%' "
                                "AND relpersistence = 't'") == 0
    assert await admin.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE action = 'tenant.restore_drill'") \
        == audits + 1
    row = await admin.fetchrow("SELECT tenant_id, resource_id, details FROM acp_shared.audit_log "
                               "WHERE action = 'tenant.restore_drill' AND tenant_id = $1 AND resource_id = $2",
                               tid, str(job_id))
    details = json.loads(row["details"]) if isinstance(row["details"], str) else row["details"]
    assert row["tenant_id"] == tid and row["resource_id"] == job_id
    assert set(details) == {"backup_id", "reason", "ok", "complete", "tables", "manifest_rows", "restored_rows",
                            "checksums_verified", "mismatched_tables", "live_differs"}
    assert details["reason"] == "ok"
    # the report holds ids, names, counts and digests only: no seeded value
    text = json.dumps(report, default=str)
    assert "Mistral" not in text and "slow morning" not in text and "Lanternfold" not in text


@pytest.mark.asyncio
async def test_the_drill_finds_a_changed_live_tenant(pools, store):
    admin, app = pools
    tid = await _tenant(admin)
    await _seed(admin, tid)
    await _backup(pools, store, tid)
    await admin.execute("INSERT INTO shared.notifications (tenant_id, actor_type, event_type, payload, target_roles) "
                        "VALUES ($1::uuid, 'system', 'zz1093.later', '{}'::jsonb, ARRAY['admin'])", tid)
    await admin.execute("UPDATE acp_shared.tenant_config SET markets = ARRAY['TH'] WHERE tenant_id = $1::uuid", tid)
    report = await restore_drill.drill(app, store, tid)
    assert report["ok"] is True                                       # the backup itself still restores
    assert report["live_differs"] == ["shared.notifications"]
    cfg = next(r for r in report["tables"] if r["table"] == "acp_shared.tenant_config")
    assert cfg["live_pk_matched"] == 1 and cfg["live_identical"] == 0


# ── tampering, refusals, size cap ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_changed_file_or_checksum_list_fails_the_drill(pools, store):
    admin, app = pools
    tid = await _tenant(admin)
    await _seed(admin, tid)
    job_id, _ = await _backup(pools, store, tid)
    prefix = cat.object_prefix(tid, job_id)
    target = os.path.join(store.root, *(prefix + cat.table_file("shared.notifications")).split("/"))
    with gzip.open(target, "wb") as f:
        f.write(b'{"note": "changed"}\n')
    with pytest.raises(restore_drill.DrillError) as e:
        await restore_drill.drill(app, store, tid, job_id)
    assert e.value.code == "checksum_mismatch"
    sums = os.path.join(store.root, *(prefix + "SHA256SUMS").split("/"))
    with open(sums, "a") as f:
        f.write("0" * 64 + "  tables/x.jsonl.gz\n")
    with pytest.raises(restore_drill.DrillError) as e:
        await restore_drill.drill(app, store, tid, job_id)
    assert e.value.code == "sums_digest_mismatch"
    os.remove(sums)
    with pytest.raises(restore_drill.DrillError) as e:
        await restore_drill.drill(app, store, tid, job_id)
    assert e.value.code == "object_missing"


@pytest.mark.asyncio
async def test_refusals(pools, store):
    admin, app = pools
    ghost = str(uuid.uuid4())
    with pytest.raises(NonRetryable, match="no_tenant"):
        await _backup(pools, store, ghost)
    with pytest.raises(NonRetryable, match="platform_tenant"):
        await _backup(pools, store, inv.PLATFORM_TENANT_ID)
    for tenant, code in ((ghost, "no_tenant"), (inv.PLATFORM_TENANT_ID, "platform_tenant"), ("x", "bad_tenant_id")):
        with pytest.raises(restore_drill.DrillError) as e:
            await restore_drill.drill(app, store, tenant)
        assert e.value.code == code
    tid, other = await _tenant(admin), await _tenant(admin)
    with pytest.raises(restore_drill.DrillError) as e:
        await restore_drill.drill(app, store, tid)
    assert e.value.code == "no_backup"
    job_other, _ = await _backup(pools, store, other)
    with pytest.raises(restore_drill.DrillError) as e:
        await restore_drill.drill(app, store, tid, job_other)
    assert e.value.code == "backup_of_another_tenant"


@pytest.mark.asyncio
async def test_size_cap_skips_whole_tables_and_the_backup_is_incomplete(pools, store):
    admin, app = pools
    tid = await _tenant(admin)
    await _seed(admin, tid)
    job_id, result = await _backup(pools, store, tid, max_bytes=1)
    assert result["complete"] is False
    skipped = {s["table"]: s for s in result["skipped"]}
    assert skipped["acp_contract.tour_atoms"] == {"table": "acp_contract.tour_atoms", "rows": 2, "reason": "size_cap"}
    assert "shared.tenants" in skipped
    report = await restore_drill.drill(app, store, tid, job_id)
    assert report["ok"] is True and report["complete"] is False and restore_drill.exit_code(report) == 3
    assert {s["table"] for s in report["skipped"]} == set(skipped)
    assert all(r["restored_rows"] == 0 for r in report["tables"])
