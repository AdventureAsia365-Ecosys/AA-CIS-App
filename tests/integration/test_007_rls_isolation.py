"""
tests/integration/test_007_rls_isolation.py

S7 Step 1 — Verify RLS isolation between 2 test tenants (migration 007).
Run AFTER applying 007_seed_test_tenants.sql.

Usage:
    pytest tests/integration/test_007_rls_isolation.py -v

Requires:
    CIS_TEST_DB_URL=postgresql://aa_cis_admin:***@<host>:5432/aa_cis_dev
    (or set via pytest.ini / .env)
"""

import os
import uuid
import pytest
import asyncpg

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_URL = os.environ.get(
    "CIS_TEST_DB_URL",
    "postgresql://aa_cis_admin:cistest@localhost:5432/aa_cis_dev"
)

TENANT_A_ID = "a1b2c3d4-0001-4000-8000-000000000001"
TENANT_B_ID = "a1b2c3d4-0002-4000-8000-000000000002"
TENANT_A_NAME = "WanderLux Travel"
TENANT_B_NAME = "ExploreAsia Co."


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
async def admin_conn():
    """DBA connection — bypasses RLS (BYPASSRLS role)."""
    conn = await asyncpg.connect(DB_URL)
    yield conn
    await conn.close()


@pytest.fixture(scope="module")
async def app_conn():
    """
    Application user connection — subject to RLS.
    aa_app_user has NO BYPASSRLS → RLS policies ARE enforced.
    Created by: migrations/007b_create_app_user.sql
    Password env: CIS_APP_DB_URL or derived from DB_URL + hardcoded test password.
    """
    app_url = os.environ.get(
        "CIS_APP_DB_URL",
        DB_URL.replace("aa_cis_admin", "aa_app_user")
    )
    # Replace password in URL with aa_app_user password
    # DB_URL format: postgresql://user:pass@host:port/db
    import re
    app_url = re.sub(r'(postgresql://aa_app_user:)[^@]+(@)', lambda m: m.group(1) + 'cisappuser2026' + m.group(2), app_url)
    conn = await asyncpg.connect(app_url)
    yield conn
    await conn.close()


async def set_tenant_context(conn, tenant_id: str):
    """Set RLS context for a transaction."""
    await conn.execute(f"SET LOCAL app.tenant_id = '{tenant_id}'")


# ---------------------------------------------------------------------------
# SECTION 1: Tenant Records Exist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_a_exists(admin_conn):
    """Tenant A (WanderLux) must exist in shared.tenants."""
    row = await admin_conn.fetchrow(
        "SELECT name, plan_tier, is_active FROM shared.tenants WHERE tenant_id = $1",
        uuid.UUID(TENANT_A_ID)
    )
    assert row is not None, f"Tenant A not found: {TENANT_A_ID}"
    assert row["name"] == TENANT_A_NAME
    assert row["plan_tier"] == "growth"
    assert row["is_active"] is True


@pytest.mark.asyncio
async def test_tenant_b_exists(admin_conn):
    """Tenant B (ExploreAsia) must exist in shared.tenants."""
    row = await admin_conn.fetchrow(
        "SELECT name, plan_tier, is_active FROM shared.tenants WHERE tenant_id = $1",
        uuid.UUID(TENANT_B_ID)
    )
    assert row is not None, f"Tenant B not found: {TENANT_B_ID}"
    assert row["name"] == TENANT_B_NAME
    assert row["plan_tier"] == "starter"
    assert row["is_active"] is True


# ---------------------------------------------------------------------------
# SECTION 2: Config Seeded Correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_a_brand_rules(admin_conn):
    """Tenant A brand rules seeded with luxury tone."""
    row = await admin_conn.fetchrow(
        "SELECT system_prompt, is_active FROM shared.tenant_brand_rules WHERE tenant_id = $1",
        uuid.UUID(TENANT_A_ID)
    )
    assert row is not None
    assert "luxury" in row["system_prompt"].lower() or "WanderLux" in row["system_prompt"]
    assert row["is_active"] is True


@pytest.mark.asyncio
async def test_tenant_b_brand_rules(admin_conn):
    """Tenant B brand rules seeded with adventure tone."""
    row = await admin_conn.fetchrow(
        "SELECT system_prompt, is_active FROM shared.tenant_brand_rules WHERE tenant_id = $1",
        uuid.UUID(TENANT_B_ID)
    )
    assert row is not None
    assert "adventure" in row["system_prompt"].lower() or "ExploreAsia" in row["system_prompt"]
    assert row["is_active"] is True


@pytest.mark.asyncio
async def test_tenant_a_seo_config(admin_conn):
    """Tenant A SEO config: dataforseo + US/UK/AU markets."""
    row = await admin_conn.fetchrow(
        "SELECT seo_provider, target_market FROM shared.tenant_seo_config WHERE tenant_id = $1",
        uuid.UUID(TENANT_A_ID)
    )
    assert row is not None
    assert row["seo_provider"] == "dataforseo"
    target = row["target_market"]
    assert "US" in target["countries"]


@pytest.mark.asyncio
async def test_tenant_b_export_config(admin_conn):
    """Tenant B export config: CSV format."""
    row = await admin_conn.fetchrow(
        "SELECT export_format FROM shared.tenant_export_config WHERE tenant_id = $1",
        uuid.UUID(TENANT_B_ID)
    )
    assert row is not None
    assert row["export_format"] == "csv"


# ---------------------------------------------------------------------------
# SECTION 3: RLS Isolation — Core Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rls_tenant_a_sees_only_own_pipeline_runs(app_conn):
    """
    Critical RLS test:
    When app_conn sets context = tenant_A, it MUST only see tenant_A's pipeline_runs.
    Tenant B's run (batch_exploreasia_test_001) must NOT be visible.
    """
    async with app_conn.transaction():
        await set_tenant_context(app_conn, TENANT_A_ID)
        rows = await app_conn.fetch("SELECT batch_id, tenant_id FROM shared.pipeline_runs")

        tenant_ids_visible = {str(r["tenant_id"]) for r in rows}
        assert TENANT_A_ID in tenant_ids_visible, "Tenant A cannot see its own runs"
        assert TENANT_B_ID not in tenant_ids_visible, \
            "CRITICAL: Tenant A can see Tenant B's pipeline_runs — RLS BROKEN"

        batch_ids = [r["batch_id"] for r in rows]
        assert "batch_wanderlux_test_001" in batch_ids
        assert "batch_exploreasia_test_001" not in batch_ids


@pytest.mark.asyncio
async def test_rls_tenant_b_sees_only_own_pipeline_runs(app_conn):
    """
    Critical RLS test:
    When app_conn sets context = tenant_B, it MUST only see tenant_B's pipeline_runs.
    Tenant A's run (batch_wanderlux_test_001) must NOT be visible.
    """
    async with app_conn.transaction():
        await set_tenant_context(app_conn, TENANT_B_ID)
        rows = await app_conn.fetch("SELECT batch_id, tenant_id FROM shared.pipeline_runs")

        tenant_ids_visible = {str(r["tenant_id"]) for r in rows}
        assert TENANT_B_ID in tenant_ids_visible, "Tenant B cannot see its own runs"
        assert TENANT_A_ID not in tenant_ids_visible, \
            "CRITICAL: Tenant B can see Tenant A's pipeline_runs — RLS BROKEN"

        batch_ids = [r["batch_id"] for r in rows]
        assert "batch_exploreasia_test_001" in batch_ids
        assert "batch_wanderlux_test_001" not in batch_ids


@pytest.mark.asyncio
async def test_rls_no_context_sees_no_rows(app_conn):
    """
    Without SET LOCAL app.tenant_id, app_user should see 0 rows.
    This is the fail-safe: no context = no data.
    """
    # Do NOT call set_tenant_context — test bare connection
    rows = await app_conn.fetch("SELECT COUNT(*) AS cnt FROM shared.pipeline_runs")
    count = rows[0]["cnt"]
    # RLS with USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    # When setting returns NULL/empty → no rows match → count = 0
    assert count == 0, \
        f"CRITICAL: app_user sees {count} rows without tenant context — RLS misconfigured"


@pytest.mark.asyncio
async def test_rls_admin_bypasses_rls(admin_conn):
    """
    DBA (aa_cis_admin) must see ALL pipeline_runs regardless of context.
    This confirms BYPASSRLS is working correctly for admin.
    """
    rows = await admin_conn.fetch("SELECT batch_id FROM shared.pipeline_runs")
    batch_ids = [r["batch_id"] for r in rows]
    assert "batch_wanderlux_test_001" in batch_ids
    assert "batch_exploreasia_test_001" in batch_ids


# ---------------------------------------------------------------------------
# SECTION 4: Silver Schemas Exist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_silver_schema_tenant_a_exists(admin_conn):
    """Silver schema for Tenant A must have been created."""
    row = await admin_conn.fetchrow(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'silver_a1b2c3d4_0001'"
    )
    assert row is not None, "Silver schema for Tenant A (silver_a1b2c3d4_0001) not found"


@pytest.mark.asyncio
async def test_silver_schema_tenant_b_exists(admin_conn):
    """Silver schema for Tenant B must have been created."""
    row = await admin_conn.fetchrow(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'silver_a1b2c3d4_0002'"
    )
    assert row is not None, "Silver schema for Tenant B (silver_a1b2c3d4_0002) not found"


@pytest.mark.asyncio
async def test_gold_schema_tenant_a_exists(admin_conn):
    """Gold schema for Tenant A must have been created."""
    row = await admin_conn.fetchrow(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'gold_a1b2c3d4_0001'"
    )
    assert row is not None, "Gold schema for Tenant A (gold_a1b2c3d4_0001) not found"


@pytest.mark.asyncio
async def test_gold_schema_tenant_b_exists(admin_conn):
    """Gold schema for Tenant B must have been created."""
    row = await admin_conn.fetchrow(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'gold_a1b2c3d4_0002'"
    )
    assert row is not None, "Gold schema for Tenant B (gold_a1b2c3d4_0002) not found"


# ---------------------------------------------------------------------------
# SECTION 5: No tenant API key column
# ---------------------------------------------------------------------------
#
# AA-1036 (root ADR 0019 d6): tenant API keys were removed; CIS 368 cleared shared.tenants.api_key_hash and CIS 369
# dropped it. The two tests that checked each seed tenant held a SHA-256 key hash (AA-433) are replaced by this one.

@pytest.mark.asyncio
async def test_tenants_have_no_api_key_column(admin_conn):
    """shared.tenants carries no tenant API key column any more (CIS 369)."""
    n = await admin_conn.fetchval(
        "SELECT count(*) AS n FROM information_schema.columns "
        "WHERE table_schema = 'shared' AND table_name = 'tenants' AND column_name = 'api_key_hash'"
    )
    assert n == 0

