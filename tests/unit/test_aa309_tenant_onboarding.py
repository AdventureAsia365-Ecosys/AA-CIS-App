"""AA-309 [N1] — tenant onboarding endpoints (api/routers/admin.py).

Mocks the asyncpg pool per the pool.acquire() convention established in
test_aa300_admin_atoms.py — no live DB, no LLM. Auth is exercised against the
real verify_admin_secret() from the same module.

AA-472 (Hướng B): seed-atoms/assign-angle/mirror endpoints and their tests are removed —
portfolio seeding was never required, and angle was found to be the wrong concept at the
tenant level.

AA-473: Gate A removed entirely (ADR-2026-038 §0.2) -- create_tenant() now inserts
is_active=true directly, no approval step, no acp_shared.tenant_onboarding table. All
Gate A-specific test classes (TestCreateTenantIsInactive, TestGateAApprove, TestGateAStatus,
TestListTenantsPending, TestUpdateTenantGateAGuard) removed with the code they tested;
TestCreateTenantIsActive added below in their place.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers import admin
from services.acp_produce.brand import fetch_brand_rubric_text
from services.content_generation.brand_standards import AA_BRAND_IDENTITY_PROMPT

_TEST_SECRET = "test-admin-secret"

TENANT_ID = uuid.uuid4()
PLAN_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)


def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


class _TxnCM:
    """conn.transaction() is a sync method returning an async context manager --
    AsyncMock's default auto-mocking makes it a coroutine instead, which breaks
    `async with conn.transaction():`. Same fix as test_aa361_atom_usage.py /
    test_aa367_packets.py."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _make_conn():
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxnCM())
    return conn


class TestCreateTenantPostsPerWeek:
    """AA-384: posts_per_week is now a free, caller-supplied value (1-14), not derived from
    plan_tier — POSTS_PER_WEEK_BY_PLAN_TIER was removed entirely."""

    def test_posts_per_week_required(self):
        with pytest.raises(Exception):
            admin.CreateTenantRequest(name="Test Agency", slug="test-agency")

    def test_posts_per_week_out_of_range_rejected(self):
        with pytest.raises(Exception):
            admin.CreateTenantRequest(name="Test Agency", slug="test-agency", posts_per_week=15)
        with pytest.raises(Exception):
            admin.CreateTenantRequest(name="Test Agency", slug="test-agency", posts_per_week=0)

    def test_posts_per_week_need_not_match_any_tier(self):
        """The whole point of AA-384: 4 doesn't match any old tier value (1/3/5/7) and must still
        be accepted."""
        body = admin.CreateTenantRequest(name="Test Agency", slug="test-agency", posts_per_week=4)
        assert body.posts_per_week == 4


class TestCreateTenantIsActive:
    """AA-473: Gate A removed -- a new tenant is active immediately, no onboarding row."""

    @pytest.mark.asyncio
    async def test_new_tenant_is_active_true(self):
        conn = _make_conn()
        # slug-existing check -> None, plan_id lookup (AA-797) -> PLAN_ID, INSERT RETURNING ->
        # TENANT_ID, has_rules check -> None (no existing brand_rules row for this brand-new tenant)
        conn.fetchval.side_effect = [None, PLAN_ID, TENANT_ID, None]
        pool = _make_pool(conn)
        request = _make_request(pool)

        body = admin.CreateTenantRequest(name="Test Agency", slug="test-agency", posts_per_week=4)
        result = await admin.create_tenant(body, request, x_admin_secret=_TEST_SECRET, x_admin_role="admin")

        assert result.is_active is True
        assert result.posts_per_week == 4
        insert_args = conn.fetchval.call_args_list[2][0]
        assert "VALUES ($1, $2, $3::plan_tier_enum, $4, $5, $6, $7, true)" in insert_args[0]
        assert insert_args[4] == PLAN_ID  # AA-797: plan_id is set on create
        assert insert_args[5] == 4  # posts_per_week bound param


class TestCreateTenantSeedsFindableBrandRules:
    """AA-471: create_tenant()'s placeholder tenant_brand_rules row must be seeded with
    brand_name='default', not body.name -- every real reader (fetch_brand_rubric_text() in
    services/acp_produce/brand.py, _resolve_brand_rule()'s no-brand_name branch in
    admin_pipeline.py, the AA-198/AA-129 multi-brand convention since migration 044) queries
    `WHERE brand_name = 'default'`. Before this fix, brand_name was seeded as the tenant's own
    company name, so the row existed but could never be found by any real reader -- every
    tenant onboarded through this endpoint silently fell through to the generic
    AA_BRAND_IDENTITY_PROMPT fallback. Root cause + evidence: docs/implementation-notes/
    AA-471.md."""

    @pytest.mark.asyncio
    async def test_brand_rules_insert_uses_default_not_tenant_name(self):
        conn = _make_conn()
        conn.fetchval.side_effect = [None, PLAN_ID, TENANT_ID, None]
        pool = _make_pool(conn)
        request = _make_request(pool)

        body = admin.CreateTenantRequest(
            name="WanderLux Travel", slug="wanderlux-travel", posts_per_week=4,
        )
        await admin.create_tenant(body, request, x_admin_secret=_TEST_SECRET, x_admin_role="admin")

        brand_rules_calls = [
            c for c in conn.execute.call_args_list if "tenant_brand_rules" in c.args[0]
        ]
        assert len(brand_rules_calls) == 1
        _, tenant_id_param, brand_name_param = brand_rules_calls[0].args
        assert tenant_id_param == TENANT_ID
        assert brand_name_param == "default"
        assert brand_name_param != body.name  # the exact AA-471 regression this guards against

    @pytest.mark.asyncio
    async def test_seeded_row_is_actually_findable_by_fetch_brand_rubric_text(self):
        """Chains the write through the real reader, not just 'some brand_name got written':
        simulates the real DB now holding exactly the row create_tenant() wrote, then runs
        fetch_brand_rubric_text()'s own real WHERE clause against it -- confirms both halves
        (write + read) agree, the way the live AA-471 verify confirmed against the real DB."""
        conn = _make_conn()
        conn.fetchval.side_effect = [None, PLAN_ID, TENANT_ID, None]
        pool = _make_pool(conn)
        request = _make_request(pool)

        body = admin.CreateTenantRequest(
            name="WanderLux Travel", slug="wanderlux-travel", posts_per_week=4,
        )
        await admin.create_tenant(body, request, x_admin_secret=_TEST_SECRET, x_admin_role="admin")

        brand_rules_calls = [
            c for c in conn.execute.call_args_list if "tenant_brand_rules" in c.args[0]
        ]
        _, seeded_tenant_id, seeded_brand_name = brand_rules_calls[0].args

        async def _fake_fetchrow(query, tenant_id_param):
            assert "brand_name = 'default'" in query  # the real reader's actual filter
            row_matches = seeded_brand_name == "default" and tenant_id_param == str(seeded_tenant_id)
            if row_matches:
                return {
                    "system_prompt": "You are writing for WanderLux Travel.",
                    "style_guide": None,
                    "forbidden_words": [],
                    "good_examples": None,
                }
            return None

        db = AsyncMock()
        db.fetchrow.side_effect = _fake_fetchrow

        result = await fetch_brand_rubric_text(db, str(seeded_tenant_id))

        assert result != AA_BRAND_IDENTITY_PROMPT
        assert "You are writing for WanderLux Travel." in result
