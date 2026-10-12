"""AA-741 G1 — a tenant job's LLM rows carry the tenant, end to end on real Postgres.

The real Worker claims a job, binds its tenant from the payload, and a handler that logs a call
WITHOUT tenant_id= (as the S1 judge / brand audit / T10 judge / F10 embed sites do) writes a
shared.llm_call_log row with that tenant. A platform job (aa_internal) keeps tenant_id NULL.
"""
import asyncio
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import registry  # noqa: E402
from shared.jobs.worker import Worker  # noqa: E402
from shared.llm_client import call_log  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = "a7410001-0000-4000-8000-000000000001"
STAGE = "adhoc_aa741_g1"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    await p.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active)
        VALUES ($1::uuid, 'G1 Tenant', 'g1-tenant', 'internal', 7, 60, true)
        ON CONFLICT DO NOTHING
    """, TENANT)
    yield p
    await p.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1::uuid", TENANT)
    await p.close()


@pytest.fixture
def g1_kind(monkeypatch):
    monkeypatch.setattr(registry, "_KINDS", {})
    seen = {}

    @registry.job_kind("g1_job", concurrency=2)
    async def g1_job(ctx):
        # A thread (to_thread / LangGraph executor) sees the bound tenant too.
        seen.setdefault("thread_tenant", []).append(await asyncio.to_thread(call_log.current_tenant_id))
        await call_log.record_call_with_pool(            # no tenant_id= on purpose
            ctx.pool, stage=STAGE, role="judge", model="haiku-4-5", tokens_in=10,
            tokens_out=5, cost_usd=0.01, quality_signal={"source": "test"})

    return seen


async def _run_until_done(pool, job_ids):
    worker = Worker(pool, poll_seconds=0.1, heartbeat_seconds=0.2)
    runner = asyncio.create_task(worker.run())
    try:
        for _ in range(100):
            done = await pool.fetchval(
                "SELECT count(*) FROM shared.job WHERE id = ANY($1::uuid[]) AND status = 'succeeded'",
                job_ids)
            if done == len(job_ids):
                return
            await asyncio.sleep(0.1)
        raise AssertionError("jobs never succeeded")
    finally:
        await worker.shutdown()
        await runner


@pytest.mark.asyncio
async def test_tenant_job_rows_carry_the_tenant_platform_rows_stay_null(pool, g1_kind):
    t2, _ = await registry.enqueue(pool, "g1_job", {"tenant_id": TENANT, "version_id": "v"})
    t9, _ = await registry.enqueue(pool, "g1_job", {"request_id": "r", "context": {"tenant_id": TENANT}})
    s1, _ = await registry.enqueue(pool, "g1_job", {"tenant_id": call_log.PLATFORM_TENANT_ID})
    await _run_until_done(pool, [t2, t9, s1])

    rows = {str(r["job_id"]): r["tenant_id"] for r in await pool.fetch(
        "SELECT job_id, tenant_id::text FROM shared.llm_call_log WHERE stage = $1", STAGE)}
    assert rows == {t2: TENANT, t9: TENANT, s1: None}
    assert sorted(g1_kind["thread_tenant"], key=str) == sorted([TENANT, TENANT, None], key=str)
    assert call_log.current_tenant_id() is None          # nothing leaks outside the handlers
