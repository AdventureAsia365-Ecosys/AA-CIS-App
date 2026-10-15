"""AA-840 — migration 236: acp_shared.tenant_config.channels accepts the 8 Slate Channels, and the
Slate reads that row (no row = every Channel), against real Postgres.

Before 236 the CHECK allowed only blog/facebook/tiktok/email, so the admin Planning config form
failed in the DB for linkedin/instagram/landing_page/ads although the API accepted them.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = uuid.UUID("84084084-0840-4840-8840-000000000840")


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    await p.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                                    posts_per_week, api_key_hash)
        VALUES ($1::uuid, 'AA-840 test', 'aa840-test', 'starter', 60, true, 3, 'h')
        ON CONFLICT DO NOTHING""", TENANT)
    await p.execute("DELETE FROM acp_shared.tenant_config WHERE tenant_id = $1::uuid", TENANT)
    yield p
    await p.execute("DELETE FROM acp_shared.tenant_config WHERE tenant_id = $1::uuid", TENANT)
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1::uuid", TENANT)
    await p.close()


@pytest.mark.asyncio
async def test_migration_236_registered(pool):
    assert await pool.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '236'") == 1


@pytest.mark.asyncio
async def test_all_eight_channels_save(pool):
    from services.acp_planning.tenant_config import fetch_tenant_planning_config, save_tenant_planning_config
    from services.acp_shared.slate import CHANNEL_BARS

    await save_tenant_planning_config(TENANT, ["US"], list(CHANNEL_BARS), 3, pool)
    cfg = await fetch_tenant_planning_config(TENANT, pool)
    assert cfg.channels == list(CHANNEL_BARS)


@pytest.mark.asyncio
async def test_unknown_channel_still_rejected(pool):
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO acp_shared.tenant_config (tenant_id, markets, channels) VALUES ($1::uuid, '{US}', $2)",
            TENANT, ["blog", "pinterest"])


@pytest.mark.asyncio
async def test_slate_reads_the_row(pool):
    from services.acp_planning.tenant_config import save_tenant_planning_config
    from services.acp_shared.slate import CHANNEL_BARS, _tenant_enabled_channels

    async with pool.acquire() as conn:
        assert await _tenant_enabled_channels(TENANT, conn) == list(CHANNEL_BARS)
    await save_tenant_planning_config(TENANT, ["US"], ["linkedin", "blog"], 3, pool)
    async with pool.acquire() as conn:
        assert await _tenant_enabled_channels(TENANT, conn) == ["blog", "linkedin"]
