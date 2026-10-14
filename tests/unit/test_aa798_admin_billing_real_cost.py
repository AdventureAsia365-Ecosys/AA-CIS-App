"""AA-798 (S235) — admin billing reads the real LLM cost and never invents a plan.

* ``GET /admin/billing`` returns the view's ``llm_cost_usd`` as-is (since migration 223 that is
  month-to-date ``SUM(shared.llm_call_log.cost_usd)``); no endpoint computes a second formula
  (the old ``pipeline_runs.cost_usd + 0.018 x versions`` estimate).
* An unknown tenant is a 404, not a $299 / 50-tour Starter plan.
* A tenant whose ``plan_id`` is NULL gets ``plan_attached: false`` and null quotas/price, in the
  admin billing view, the admin tenant list and the tenant's own ``/v1/billing``.

Mocked asyncpg; the real-SQL check of the view lives in tests/integration/test_aa798_usage_view.py.
"""
import inspect
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.routers import _billing, admin, admin_pipeline, admin_overview, admin_settings, v1_tours

_SECRET = "test-admin-secret"
TENANT_ID = str(uuid.uuid4())
MONTH = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _SECRET)


def _request(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    request = MagicMock()
    request.app.state.pool = pool
    return request


def _usage_row(**over):
    row = {
        "tenant_name": "Atlas & Hearth", "plan_tier": "growth", "billing_month": MONTH,
        "tours_quota_monthly": 200, "api_calls_quota_monthly": 20000,
        "price_usd_monthly": Decimal("799.00"),
        "tours_rewritten": 12, "api_calls_used": Decimal("340"),
        "quota_tours_pct": Decimal("6.0"), "quota_calls_pct": Decimal("1.7"),
        "tours_overage": 0, "overage_usd": Decimal("0.00"),
        "llm_cost_usd": Decimal("6.7812"),
        "overage_rate_usd_per_tour": Decimal("3.0000"),
    }
    row.update(over)
    return row


def _no_plan_row():
    # What the view yields for plan_id IS NULL: LEFT JOIN membership_plans gives NULL plan
    # columns, the CASE gives 0 %, GREATEST(0, n - NULL) gives 0, overage_usd is NULL.
    return _usage_row(tours_quota_monthly=None, api_calls_quota_monthly=None, price_usd_monthly=None,
                      overage_rate_usd_per_tour=None, quota_tours_pct=Decimal("0"),
                      quota_calls_pct=Decimal("0"), tours_overage=0, overage_usd=None)


async def _admin_billing(row, activity=()):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=list(activity))
    out = await admin_pipeline.get_admin_billing(_request(conn), x_admin_secret=_SECRET, tenant_id=TENANT_ID)
    return out, conn


class TestAdminBillingCost:
    @pytest.mark.asyncio
    async def test_llm_cost_is_the_view_column_unchanged(self):
        out, conn = await _admin_billing(_usage_row())
        assert out["llm_cost_usd"] == pytest.approx(6.7812)
        sql = conn.fetchrow.await_args.args[0]
        assert "v.llm_cost_usd" in sql and "shared.v_tenant_monthly_usage" in sql
        assert conn.fetchrow.await_args.args[1] == TENANT_ID
        assert out["plan_attached"] is True
        assert out["billing_month"] == "2026-10"
        assert out["price_usd_monthly"] == 799.0

    def test_no_second_cost_formula_in_billing_paths(self):
        # One definition: the view. No 0.018-per-version estimate, no pipeline_runs cost, anywhere
        # in the billing read paths.
        for fn in (admin_pipeline.get_admin_billing, v1_tours.get_my_billing):
            src = inspect.getsource(fn)
            assert "0.018" not in src
            assert "pipeline_runs" not in src
        assert "0.018" not in inspect.getsource(_billing)


class TestNoInventedPlan:
    @pytest.mark.asyncio
    async def test_unknown_tenant_is_404_not_starter_defaults(self):
        with pytest.raises(HTTPException) as e:
            await _admin_billing(None)
        assert e.value.status_code == 404

    @pytest.mark.asyncio
    async def test_null_plan_gives_plan_attached_false_and_null_quotas(self):
        out, _ = await _admin_billing(_no_plan_row())
        assert out["plan_attached"] is False
        for k in _billing.PLAN_DERIVED_FIELDS:
            assert out[k] is None, k
        # usage and cost are still real numbers
        assert out["tours_rewritten"] == 12
        assert out["llm_cost_usd"] == pytest.approx(6.7812)

    def test_no_starter_literals_in_billing_paths(self):
        for fn in (admin_pipeline.get_admin_billing, v1_tours.get_my_billing):
            src = inspect.getsource(fn)
            assert not re.search(r"\b299\b", src), fn.__name__
            assert '"starter"' not in src, fn.__name__

    @pytest.mark.asyncio
    async def test_tenant_billing_null_plan_has_no_cost_and_no_invented_plan(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={k: v for k, v in _no_plan_row().items() if k != "llm_cost_usd"})
        conn.fetch = AsyncMock(side_effect=[[], [], []])
        conn.fetchval = AsyncMock(return_value=300)
        out = await v1_tours.get_my_billing(_request(conn), tenant={"sub": TENANT_ID})
        assert out["plan_attached"] is False
        assert out["price_usd_monthly"] is None and out["tours_quota_monthly"] is None
        assert "llm_cost_usd" not in out and "jev_cost_usd" not in out

    @pytest.mark.asyncio
    async def test_admin_tenant_list_null_plan(self):
        conn = AsyncMock()
        conn.fetch.return_value = [{
            "tenant_id": uuid.UUID(TENANT_ID), "name": "T", "slug": "t", "plan_tier": "growth",
            "posts_per_week": 3, "country": None, "rate_limit_rpm": 300, "created_at": MONTH,
            "is_active": True, "cancelled_at": None,
            "api_calls_used": 0, "quota_tours_pct": 0, "quota_calls_pct": 0, "tours_overage": 0,
            "overage_usd": 0, "llm_cost_usd": Decimal("1.2345"),
            "tours_quota_monthly": None, "api_calls_quota_monthly": None, "price_usd_monthly": None,
            "source_active": 0, "source_superseded": 0, "source_trashed": 0,
            "master_active": 0, "master_inactive": 0, "master_trashed": 0,
        }]
        out = await admin.list_tenants(_request(conn), x_admin_secret=_SECRET, include_inactive=False)
        t = out["tenants"][0]
        assert t["plan_attached"] is False
        assert t["plan"] == {"tours_quota_monthly": None, "api_calls_quota_monthly": None,
                             "price_usd_monthly": None}
        assert t["this_month"]["llm_cost_usd"] == pytest.approx(1.2345)
        sql = conn.fetch.call_args.args[0]
        assert "COALESCE(u.price_usd_monthly" not in sql
        assert "COALESCE(u.llm_cost_usd, 0)" in sql


class TestViewReadersShape:
    def test_readers_select_only_live_view_columns(self):
        # The app must work before AND after migration 223: read only columns both view shapes
        # (hand-made live view and 223) have. plan_attached is derived in Python, not a view column.
        live_cols = {
            "tenant_id", "tenant_name", "plan_tier", "slug", "tours_quota_monthly",
            "api_calls_quota_monthly", "price_usd_monthly", "overage_rate_usd_per_tour",
            "billing_month", "tours_rewritten", "api_calls_used", "quota_tours_pct",
            "quota_calls_pct", "tours_overage", "overage_usd", "llm_cost_usd",
        }
        sources = [inspect.getsource(admin_pipeline.get_admin_billing),
                   inspect.getsource(v1_tours.get_my_billing),
                   inspect.getsource(admin.list_tenants),
                   admin_overview._QUOTA_SQL,
                   inspect.getsource(admin_settings.get_settings)]
        for src in sources:
            for col in re.findall(r"\b[uv]\.([a-z_]+)", src):
                assert col in live_cols, col
            assert "plan_attached" not in re.findall(r"\b[uv]\.([a-z_]+)", src)
