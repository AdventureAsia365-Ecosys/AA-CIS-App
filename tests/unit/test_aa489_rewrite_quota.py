"""AA-489 / AA-640 — per-tenant monthly rewrite quota (api/routers/v1_tours.py).

AA-640 (Nghiep, 25/09/2026): the quota comes from shared.membership_plans (the same number the
portal shows and billing uses), and a rewrite past it is ALLOWED and billed as overage — no 429
while the product is in trial.

Covers:
  1. plan quota read from membership_plans for the tenant's live plan_tier
  2. unknown plan / no tenant row falls back safely
  3. consume under, at and over the quota never raises; over-quota is logged
  4. GET /v1/quota is read-only and reports the membership_plans quota
  5. a tenant with no usage row yet reads as used=0
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import v1_tours

QUOTAS = {"starter": 50, "growth": 200, "business": 500, "enterprise": 999999, "internal": 999999}


def _make_conn(plan_tier="starter", used_count=None):
    conn = AsyncMock()

    async def _fetchrow(query, *args):
        assert "membership_plans" in query and "shared.tenants" in query
        return {"plan": plan_tier, "quota": QUOTAS.get(plan_tier, QUOTAS["starter"])}

    async def _fetchval(query, *args):
        if "INSERT INTO shared.tenant_rewrite_usage" in query:
            return used_count
        if "SELECT rewrite_count FROM shared.tenant_rewrite_usage" in query:
            return used_count
        raise AssertionError(f"unexpected query in test double: {query}")

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    return conn


@pytest.mark.asyncio
class TestRewriteQuota:
    async def test_plan_quota_comes_from_membership_plans(self):
        conn = _make_conn(plan_tier="growth")
        plan, limit = await v1_tours._get_tenant_plan_limit(conn, "tenant-1")
        assert (plan, limit) == ("growth", 200)  # not PLAN_LIMITS' old 500
        assert "tours_per_month" not in v1_tours.PLAN_LIMITS["growth"]

    async def test_unknown_plan_falls_back_to_starter(self):
        conn = _make_conn(plan_tier="some-future-tier")
        _, limit = await v1_tours._get_tenant_plan_limit(conn, "tenant-1")
        assert limit == 50

    async def test_missing_tenant_row_is_safe(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        assert await v1_tours._get_tenant_plan_limit(conn, "nope") == ("starter", 0)

    @pytest.mark.parametrize("used", [5, 50, 51, 400])
    async def test_consume_never_blocks(self, used):
        conn = _make_conn(plan_tier="starter", used_count=used)
        await v1_tours._check_and_consume_rewrite_quota(conn, "tenant-1")  # no raise, any count

    async def test_over_quota_is_logged(self):
        conn = _make_conn(plan_tier="starter", used_count=51)
        with patch.object(v1_tours, "logger") as log:
            await v1_tours._check_and_consume_rewrite_quota(conn, "tenant-1")
        log.info.assert_called_once()
        assert log.info.call_args.args[0] == "rewrite_over_quota"
        assert log.info.call_args.kwargs["quota"] == 50 and log.info.call_args.kwargs["used"] == 51

    async def test_within_quota_is_not_logged(self):
        conn = _make_conn(plan_tier="starter", used_count=50)
        with patch.object(v1_tours, "logger") as log:
            await v1_tours._check_and_consume_rewrite_quota(conn, "tenant-1")
        log.info.assert_not_called()

    async def test_get_quota_endpoint_shape(self):
        conn = _make_conn(plan_tier="business", used_count=42)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = False
        request = MagicMock()
        request.app.state.pool = pool

        result = await v1_tours.get_quota(request, tenant={"sub": "tenant-1"})

        assert result["plan_tier"] == "business"
        assert result["tours_per_month"] == 500
        assert result["rewrites_used"] == 42
        assert result["rewrites_remaining"] == 500 - 42
        assert "resets_at" in result
        for call in conn.fetchval.await_args_list:
            assert "INSERT" not in call.args[0]

    async def test_get_quota_no_usage_row_yet_returns_zero_used(self):
        conn = _make_conn(plan_tier="starter", used_count=None)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = False
        request = MagicMock()
        request.app.state.pool = pool

        result = await v1_tours.get_quota(request, tenant={"sub": "tenant-1"})

        assert result["rewrites_used"] == 0
        assert result["rewrites_remaining"] == 50
