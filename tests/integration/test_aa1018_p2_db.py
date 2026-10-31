"""AA-1018 part 2 / AA-1016 — the new reads and writes against a real Postgres (migrations applied by conftest, 320 to
322 included): the tenant budget queries, the approval wait and its re-queue on real shared.job rows, the
access_state / last_probe_at writes under cis_app's column grants, the stored failure domain and the route deadline
column. Invented tenant and stage names only."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import cost_guard  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.llm_client import breaker, invariant, role_config, tenant_budget  # noqa: E402
from services.jobs import llm_probe_job  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = "5e7a1d0c-3b2f-4c19-8a6d-2f1e0b9c8d71"
OTHER = "6f8b2e1d-4c30-4d2a-9b7e-3a2f1c0d9e82"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for tid, slug in ((TENANT, "zz-aa1016-varra"), (OTHER, "zz-aa1016-osk")):
        await p.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                        "is_active) VALUES ($1::uuid, $2, $2, 'internal', 7, 60, true) ON CONFLICT DO NOTHING",
                        tid, slug)
    await p.execute("DELETE FROM shared.job WHERE kind = 't9_write' AND error LIKE 'tenant_ai_budget_approval%'")
    yield p
    await p.execute("DELETE FROM shared.llm_call_log WHERE stage = 'zz_aa1016'")
    await p.execute("DELETE FROM shared.job WHERE kind = 't9_write' AND error LIKE 'tenant_ai_budget_approval%'")
    await p.execute("DELETE FROM shared.tenant_ai_budget WHERE tenant_id = ANY($1::uuid[])", [TENANT, OTHER])
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])", [TENANT, OTHER])
    await p.close()


async def _spend(pool, tenant, usd, ago=timedelta(minutes=5)):
    await pool.execute("INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, cost_usd, quality_signal, "
                       "created_at) VALUES ($1::uuid, 'zz_aa1016', 'writer', 'nova-lite', $2, '{}'::jsonb, "
                       "now() - $3::interval)", tenant, usd, ago)


async def _budget(pool, tenant, monthly, hard=True, alert=100, approval=150):
    await pool.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, "
                       "hard_stop, updated_by) VALUES ($1::uuid, $2, $3, $4, $5, 'zz-aa1016') "
                       "ON CONFLICT (tenant_id) DO UPDATE "
                       "SET monthly_usd = EXCLUDED.monthly_usd, hard_stop = EXCLUDED.hard_stop",
                       tenant, monthly, alert, approval, hard)


@pytest.mark.asyncio
async def test_the_tenant_budget_reads(pool):
    async with pool.acquire() as conn:
        assert TENANT not in await cost_guard.budgeted_tenants(conn)
        assert await cost_guard.tenant_budget_check(conn, TENANT) is None              # no row: no budget
        await _budget(pool, TENANT, 10)
        await _spend(pool, TENANT, 9.0)
        await _spend(pool, TENANT, 7.0)
        await _spend(pool, TENANT, 50.0, ago=timedelta(days=40))                         # an earlier month
        await _spend(pool, OTHER, 99.0)                                                  # another tenant
        assert TENANT in await cost_guard.budgeted_tenants(conn)
        b = await cost_guard.tenant_budget_check(conn, TENANT)
    if datetime.now(timezone.utc).day == 1 and datetime.now(timezone.utc).hour == 0:
        pytest.skip("the 5-minute-old rows may fall in the previous month")
    assert (b.month_spent_usd, b.month_calls, b.needs_approval) == (16.0, 2, True)


@pytest.mark.asyncio
async def test_the_approval_wait_and_its_requeue_on_real_jobs(pool):
    await _budget(pool, TENANT, 10)
    await _spend(pool, TENANT, 16.0)
    async with pool.acquire() as conn:
        budget = await cost_guard.tenant_budget_check(conn, TENANT)
    error = str(tenant_budget.approval_required(budget))
    job = await pool.fetchval(
        "INSERT INTO shared.job (kind, payload, status, attempt, max_attempts, error, finished_at) "
        "VALUES ('t9_write', $1::jsonb, 'stopped_budget', 1, 2, $2, now() - interval '1 hour') RETURNING id::text",
        json.dumps({"tenant_id": TENANT}), error)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    held = await queue.retry_by_reason(pool, queue.RETRY_REASON_TENANT_BUDGET, since=since, dry_run=False)
    assert held["matched"] == 1 and held["requeued"] == 0 and TENANT in held["tenants_waiting"]
    assert await pool.fetchval("SELECT status FROM shared.job WHERE id = $1::uuid", job) == "stopped_budget"
    await _budget(pool, TENANT, 40)                                                      # staff raised the budget
    out = await queue.retry_by_reason(pool, queue.RETRY_REASON_TENANT_BUDGET, since=since, dry_run=False)
    assert out["requeued_ids"] == [job]
    row = await pool.fetchrow("SELECT status, error FROM shared.job WHERE id = $1::uuid", job)
    assert row["status"] == "queued" and row["error"] is None
    # the LLM reason never takes a stopped_budget job
    llm = await queue.retry_by_reason(pool, since=since, dry_run=True)
    assert job not in [j["id"] for j in llm["jobs"]]


@pytest.mark.asyncio
async def test_cis_app_writes_access_state_and_the_probe_stamp(pool):
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            if not await conn.fetchval("SELECT has_schema_privilege('cis_app', 'shared', 'USAGE') AS ok"):
                await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")       # CI drops it (x1_244_undo)
            await conn.execute("SET LOCAL ROLE cis_app")
            await breaker._write_access_state(conn, "acc2", "ok")
            await conn.execute(llm_probe_job._STAMP_PROBE_SQL, ["acc2"])
            await conn.execute("RESET ROLE")
            row = await conn.fetchrow("SELECT access_state AS state, last_probe_at IS NOT NULL AS stamped "
                                      "FROM shared.llm_provider_connection WHERE connection_key = 'acc2'")
            assert row["state"] == "ok" and row["stamped"] is True
            await conn.execute("SET LOCAL ROLE cis_app")
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await breaker._write_access_state(conn, "acc2", "sleepy")
        finally:
            await tr.rollback()


@pytest.mark.asyncio
async def test_the_invariant_reads_the_stored_failure_domain(pool):
    async with pool.acquire() as conn:
        report = await invariant.read(conn)
        stored = {r["connection_key"]: r["failure_domain"] for r in await conn.fetch(
            "SELECT connection_key, failure_domain FROM shared.llm_provider_connection")}
    assert report["ready"] is True
    assert set(report["domains"]) <= {d for d in stored.values() if d}


@pytest.mark.asyncio
async def test_role_config_reads_route_deadline_s(pool):
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            stage = await conn.fetchval("SELECT stage FROM shared.llm_role_config WHERE is_active ORDER BY stage "
                                        "LIMIT 1")
            await conn.execute("UPDATE shared.llm_role_config SET route_deadline_s = 90 WHERE stage = $1", stage)
            row = await role_config._fetch_scoped(
                lambda sql: conn.fetchrow(sql, stage),
                lambda cols: (f"SELECT stage, role, provider, model_id, account_route, {role_config._ROUTE_COLUMNS}"
                              f"{cols} FROM shared.llm_role_config WHERE stage = $1 AND is_active"))
            assert role_config._row_to_config(row).route_deadline_s == 90
        finally:
            await tr.rollback()
