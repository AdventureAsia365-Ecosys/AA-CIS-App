"""AA-1049 W3 — the audit-tenant read of services/quality/trial_flags.py against real Postgres (migrations applied by
conftest): a synthetic tenant (kind demo) gets a trial switch, a real agency and the pilot agency do not, an unknown
tenant fails closed. Ids and names are invented and deleted at the end.
"""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.quality import trial_flags as tf  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
DEMO = "5e0d6c1a-2b3c-4d5e-8f60-aa1049000001"
AGENCY = "5e0d6c1a-2b3c-4d5e-8f60-aa1049000002"
PILOT = "5e0d6c1a-2b3c-4d5e-8f60-aa1049000003"
ALL = [DEMO, AGENCY, PILOT]


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    async with p.acquire() as c:
        await c.execute("DELETE FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])", ALL)
        await c.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, kind) "
            "VALUES ($1::uuid, 'Probe Lumen Trails', 'aa1049-lumen', 'starter', 3, 'demo')", DEMO)
        await c.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, kind) "
            "VALUES ($1::uuid, 'Probe Varn Journeys', 'aa1049-varn', 'starter', 3, 'agency')", AGENCY)
        has_pilot = await c.fetchval("SELECT count(*) FROM shared.tenants WHERE is_pilot")
        if not has_pilot:
            await c.execute(
                "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, kind, is_pilot, "
                "pilot_ends_at) VALUES ($1::uuid, 'Probe Zemlia Tours', 'aa1049-zemlia', 'starter', 3, 'agency', "
                "true, now() + interval '7 days')", PILOT)
    yield p
    async with p.acquire() as c:
        await c.execute("DELETE FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])", ALL)
    await p.close()


@pytest.mark.asyncio
async def test_the_audit_tenant_read_on_the_real_schema(pool):
    assert await tf.audit_tenant(pool, DEMO) == (True, "demo")
    assert await tf.audit_tenant(pool, AGENCY) == (False, "not an audit tenant")
    assert await tf.audit_tenant(pool, "5e0d6c1a-2b3c-4d5e-8f60-aa1049000009") == (False, "unknown tenant")
    async with pool.acquire() as c:
        pilot_here = await c.fetchval("SELECT count(*) AS n FROM shared.tenants WHERE tenant_id = $1::uuid", PILOT)
    if pilot_here:
        assert (await tf.audit_tenant(pool, PILOT))[0] is False


@pytest.mark.asyncio
async def test_a_trial_job_scope_on_the_real_schema(pool, monkeypatch):
    monkeypatch.delenv("T9_SEO_BUILDER", raising=False)
    payload = {"trial_flags": {"T9_SEO_BUILDER": "on"}}
    async with tf.scope(pool, "t9_write", payload, DEMO) as rec:
        assert tf.get("T9_SEO_BUILDER") == "on"
    assert rec["applied"] == {"T9_SEO_BUILDER": "on"}
    async with tf.scope(pool, "t9_write", payload, AGENCY) as rec:
        assert tf.get("T9_SEO_BUILDER") is None
    assert rec["applied"] == {} and rec["refused"] == ["T9_SEO_BUILDER: not an audit tenant"]
