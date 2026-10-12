"""AA-741 (S229) — saving the Brand Identity form writes the brand row AND the tenant markets the
pipeline reads (shared.tenant_seo_config.target_market), against real Postgres."""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = str(uuid.uuid4())


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    await p.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                                    posts_per_week, api_key_hash)
        VALUES ($1::uuid, 'Brand Test', $2, 'starter', 60, true, 3, 'h')""", TENANT, f"b-{TENANT[:8]}")
    yield p
    await p.execute("DELETE FROM shared.tenant_seo_config WHERE tenant_id = $1::uuid", TENANT)
    await p.execute("DELETE FROM shared.tenant_brand_rules WHERE tenant_id = $1::uuid", TENANT)
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1::uuid", TENANT)
    await p.close()


@pytest.mark.asyncio
async def test_brand_save_syncs_markets_and_resave_updates_them(pool):
    from api.routers import admin_pipeline
    from shared.services.tenant_config_service import TenantConfigService
    from services.seo_intelligence.seed_builder import resolve_buyer_markets

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    style = "Use active verbs. " * 60
    await admin_pipeline.update_brand_identity(
        admin_pipeline.BrandIdentityUpdate(style_guide=style, target_markets=["Europe", "Singapore"]),
        req, tenant_id=TENANT)
    row = await pool.fetchrow("SELECT style_guide FROM shared.tenant_brand_rules "
                              "WHERE tenant_id = $1::uuid AND is_active", TENANT)
    assert row["style_guide"] == style.strip()              # kept whole, not cut to 300

    async with pool.acquire() as conn:
        cfg = await TenantConfigService(conn).get_seo_config(TENANT)
    assert cfg.target_market["countries"] == ["UK", "DE", "FR", "NL", "SG"]
    assert [m[0] for m in resolve_buyer_markets(cfg.target_market)][0] == 2826   # UK first

    await admin_pipeline.update_brand_identity(
        admin_pipeline.BrandIdentityUpdate(target_markets=["Australia"]), req, tenant_id=TENANT)
    tm = await pool.fetchval("SELECT target_market FROM shared.tenant_seo_config "
                             "WHERE tenant_id = $1::uuid", TENANT)
    assert json.loads(tm)["countries"] == ["AU"]
    assert await pool.fetchval("SELECT count(*) FROM shared.tenant_seo_config "
                               "WHERE tenant_id = $1::uuid", TENANT) == 1
