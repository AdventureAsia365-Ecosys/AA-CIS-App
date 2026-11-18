"""AA-1126 l16 (S283) — on a real database: a job held by its tenant's AI budget reads back with the tenant-facing stop
code (the my-versions / version reads, v1_tours._job_statuses and the joined column), the 402 check reads the real
budget, and the hourly tick re-queues the held jobs once the tenant no longer needs approval while it keeps the held
ones of a tenant still over its allowance, of a suspended tenant and of a tenant whose budget is gone (those wait for
staff Retry). Invented tenants only; every row is removed after."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core import ai_allowance  # noqa: E402
from api.routers import v1_tours  # noqa: E402
from services.tenant_account import lifecycle  # noqa: E402
from shared import cost_guard  # noqa: E402
from shared.llm_client import tenant_budget  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = "3d9e4f50-6a1b-4c2d-8e3f-a1126000aa01"
OTHER = "4e0f5061-7b2c-4d3e-9f40-a1126000aa02"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for tid, slug in ((TENANT, "zz-aa1126-lunetta"), (OTHER, "zz-aa1126-corvalis")):
        await p.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                        "is_active) VALUES ($1::uuid, $2, $2, 'internal', 7, 60, true) ON CONFLICT DO NOTHING",
                        tid, slug)
    yield p
    await p.execute("DELETE FROM shared.llm_call_log WHERE stage = 'zz_aa1126'")
    await p.execute("DELETE FROM shared.job WHERE kind = 't9_write' AND payload->>'zz' = 'aa1126'")
    await p.execute("DELETE FROM shared.tenant_ai_budget WHERE tenant_id = ANY($1::uuid[])", [TENANT, OTHER])
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])", [TENANT, OTHER])
    await p.close()


async def _budget(pool, tenant, monthly, spent):
    await pool.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, "
                       "hard_stop, updated_by) VALUES ($1::uuid, $2, 80, 100, true, 'zz-aa1126') "
                       "ON CONFLICT (tenant_id) DO UPDATE SET monthly_usd = EXCLUDED.monthly_usd", tenant, monthly)
    await pool.execute("INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, cost_usd, quality_signal, "
                       "created_at) VALUES ($1::uuid, 'zz_aa1126', 'writer', 'nova-lite', $2, '{}'::jsonb, "
                       "now() - interval '5 minutes')", tenant, spent)


async def _held_job(pool, tenant) -> str:
    async with pool.acquire() as conn:
        budget = await cost_guard.tenant_budget_check(conn, tenant)
    error = str(tenant_budget.approval_required(budget))
    return await pool.fetchval(
        "INSERT INTO shared.job (kind, payload, status, attempt, max_attempts, error, finished_at) "
        "VALUES ('t9_write', $1::jsonb, 'stopped_budget', 1, 2, $2, now() - interval '1 hour') RETURNING id::text",
        json.dumps({"tenant_id": tenant, "zz": "aa1126"}), error)


@pytest.mark.asyncio
async def test_a_held_job_reads_back_with_the_quota_stop_code(pool):
    await _budget(pool, TENANT, 10, 12.0)
    held = await _held_job(pool, TENANT)
    failed = await pool.fetchval(
        "INSERT INTO shared.job (kind, payload, status, attempt, max_attempts, error, finished_at) "
        "VALUES ('t9_write', $1::jsonb, 'failed', 2, 2, 'zz model error', now()) RETURNING id::text",
        json.dumps({"tenant_id": TENANT, "zz": "aa1126"}))
    statuses = await v1_tours._job_statuses(pool, [held, failed, None])
    assert statuses[held] == ("stopped_budget", "ai_quota_exhausted")
    assert statuses[failed] == ("failed", None)
    # the joined form (flag on: shared.job joined into the version read) names the same column
    row = await pool.fetchrow(f"SELECT {v1_tours._JOB_STOP_COL} FROM shared.job j WHERE j.id = $1::uuid", held)
    assert row["job_stop_code"] == "ai_quota_exhausted"
    d = v1_tours._with_allowance({"job_stop_code": "ai_quota_exhausted"})
    assert d["allowance_resets_at"] and v1_tours._with_allowance({"job_stop_code": None})["allowance_resets_at"] is None


@pytest.mark.asyncio
async def test_the_402_check_reads_the_real_budget(pool):
    await _budget(pool, TENANT, 10, 3.0)
    async with pool.acquire() as conn:
        assert await ai_allowance.allowance_used(conn, TENANT) is False
    await _budget(pool, TENANT, 10, 9.0)                                           # 12 USD of 10
    async with pool.acquire() as conn:
        assert await ai_allowance.allowance_used(conn, TENANT) is True
        assert await ai_allowance.allowance_used(conn, OTHER) is False             # no budget row, no plan budget


@pytest.mark.asyncio
async def test_the_tick_requeues_only_the_tenants_no_longer_over(pool):
    await _budget(pool, TENANT, 10, 12.0)
    await _budget(pool, OTHER, 10, 12.0)
    mine, theirs = await _held_job(pool, TENANT), await _held_job(pool, OTHER)
    await pool.execute("UPDATE shared.tenant_ai_budget SET monthly_usd = 100 WHERE tenant_id = $1::uuid", TENANT)
    resumed = await lifecycle.resume_allowance_holds(pool)
    assert resumed >= 1
    status = {r["id"]: r["status"] for r in await pool.fetch(
        "SELECT id::text AS id, status FROM shared.job WHERE id = ANY($1::uuid[])", [mine, theirs])}
    assert status[mine] == "queued" and status[theirs] == "stopped_budget"


@pytest.mark.asyncio
async def test_the_tick_keeps_the_holds_of_a_suspended_tenant_and_of_a_removed_budget(pool):
    await _budget(pool, TENANT, 10, 12.0)
    await _budget(pool, OTHER, 10, 12.0)
    suspended, gone = await _held_job(pool, TENANT), await _held_job(pool, OTHER)
    await pool.execute("UPDATE shared.tenant_ai_budget SET monthly_usd = 100 WHERE tenant_id = $1::uuid", TENANT)
    await pool.execute("UPDATE shared.tenants SET account_status = 'suspended' WHERE tenant_id = $1::uuid", TENANT)
    await pool.execute("DELETE FROM shared.tenant_ai_budget WHERE tenant_id = $1::uuid", OTHER)
    assert await lifecycle.resume_allowance_holds(pool) == 0
    status = {r["id"]: r["status"] for r in await pool.fetch(
        "SELECT id::text AS id, status FROM shared.job WHERE id = ANY($1::uuid[])", [suspended, gone])}
    assert status == {suspended: "stopped_budget", gone: "stopped_budget"}
