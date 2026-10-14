"""AA-766 (S236) — Tenant 360: RPM override on PATCH /admin/tenants/{id} (audited), and the three
admin-only read endpoints (cost / quality / markets) in api/routers/admin_tenant360.py.
Mocked asyncpg, same pool/conn shape as test_aa797_tenant_admin.py."""
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.routers import admin
from api.routers import admin_tenant360 as t360
from services.acp_shared.audit_log import AdminTenantAuditAction

_SECRET = "test-admin-secret"
TENANT_ID = uuid.uuid4()
PLAN_ID = uuid.uuid4()
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _SECRET)


class _TxnCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _conn():
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxnCM())
    return conn


def _request(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    request = MagicMock()
    request.app.state.pool = pool
    request.app.state.redis = None
    return request


def _audit_rows(conn):
    out = []
    for c in conn.execute.call_args_list:
        if "acp_shared.audit_log" in c.args[0]:
            _sql, _tid, actor, action, _rtype, _rid, details = c.args
            out.append((action, actor, json.loads(details)))
    return out


def _before(**kw):
    row = {"plan_tier": "starter", "plan_id": PLAN_ID, "rate_limit_rpm": 60,
           "is_active": True, "cancelled_at": None}
    row.update(kw)
    return row


# ── PATCH rate_limit_rpm ──────────────────────────────────────────────────────────────────────

class TestRateLimitOverride:
    async def _patch(self, conn, body, role="admin"):
        return await admin.update_tenant(
            TENANT_ID, admin.UpdateTenantRequest(**body), _request(conn),
            x_admin_secret=_SECRET, x_admin_user_id="u-1", x_admin_role=role,
        )

    @pytest.mark.asyncio
    async def test_rpm_alone_updates_and_audits(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        out = await self._patch(conn, {"rate_limit_rpm": 120})
        assert out["changed"] == ["rate_limit"] and out["rate_limit_rpm"] == 120 and out["plan_tier"] == "starter"
        upd = [c for c in conn.execute.call_args_list if "SET rate_limit_rpm = $2" in c.args[0]]
        assert len(upd) == 1 and upd[0].args[1:] == (TENANT_ID, 120)
        assert _audit_rows(conn) == [(AdminTenantAuditAction.RATE_LIMIT_CHANGED, "admin:u-1",
                                      {"before": {"rate_limit_rpm": 60}, "after": {"rate_limit_rpm": 120}})]

    @pytest.mark.asyncio
    async def test_same_rpm_is_a_no_op(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        out = await self._patch(conn, {"rate_limit_rpm": 60})
        assert out["status"] == "unchanged" and _audit_rows(conn) == []

    @pytest.mark.asyncio
    async def test_rpm_with_plan_change_wins_over_plan_default(self):
        new_plan = uuid.uuid4()
        conn = _conn()
        conn.fetchrow.return_value = _before()
        conn.fetchval.return_value = new_plan
        out = await self._patch(conn, {"plan_tier": "business", "rate_limit_rpm": 500})
        # the override is folded into the plan write: one UPDATE, one audit row
        assert out["changed"] == ["plan"]
        upd = [c for c in conn.execute.call_args_list if "plan_id = $3" in c.args[0]]
        assert upd[0].args[1:] == (TENANT_ID, "business", new_plan, 500)
        [(action, _actor, details)] = _audit_rows(conn)
        assert action == AdminTenantAuditAction.PLAN_CHANGED and details["after"]["rate_limit_rpm"] == 500

    @pytest.mark.asyncio
    async def test_plan_change_alone_reports_plan_default_rpm(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        conn.fetchval.return_value = uuid.uuid4()
        out = await self._patch(conn, {"plan_tier": "growth"})
        assert out["plan_tier"] == "growth" and out["rate_limit_rpm"] == admin.PLAN_LIMITS["growth"]["rpm"]

    @pytest.mark.asyncio
    async def test_rpm_needs_admin_role(self):
        with pytest.raises(HTTPException) as e:
            await self._patch(_conn(), {"rate_limit_rpm": 120}, role="reviewer")
        assert e.value.status_code == 403

    def test_rpm_bounds_validated(self):
        for bad in (0, -5, admin.RATE_LIMIT_RPM_MAX + 1):
            with pytest.raises(ValidationError):
                admin.UpdateTenantRequest(rate_limit_rpm=bad)


# ── pure helpers ──────────────────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_channel_kind(self):
        assert t360.channel_kind("blog") == "blog"
        for c in ("facebook", "tiktok", "instagram", "linkedin"):
            assert t360.channel_kind(c) == "social"
        for c in ("email", "landing_page", "ads", "unknown", None):
            assert t360.channel_kind(c) == "other"

    def test_piece_cost_by_kind_rolls_up(self):
        rows = [{"channel": "blog", "pieces": 4, "cost_usd": Decimal("2.0")},
                {"channel": "facebook", "pieces": 3, "cost_usd": Decimal("0.6")},
                {"channel": "instagram", "pieces": 1, "cost_usd": Decimal("0.3")}]
        out = t360.piece_cost_by_kind(rows)
        assert out == [
            {"kind": "blog", "pieces": 4, "cost_usd": 2.0, "cost_per_piece_usd": 0.5},
            {"kind": "social", "pieces": 4, "cost_usd": 0.9, "cost_per_piece_usd": 0.225},
        ]

    def test_margin_with_plan(self):
        m = t360.margin_block({"price_usd_monthly": 100.0, "overage_usd": 20.0}, 30.0)
        assert m["revenue_usd"] == 120.0 and m["margin_usd"] == 90.0 and m["margin_pct"] == 75.0
        assert m["plan_attached"] is True

    def test_margin_without_plan_invents_nothing(self):
        for usage in (None, {"price_usd_monthly": None, "overage_usd": None}):
            m = t360.margin_block(usage, 5.0)
            assert m["plan_attached"] is False
            assert m["revenue_usd"] is None and m["margin_usd"] is None and m["margin_pct"] is None
            assert m["llm_cost_usd"] == 5.0

    def test_margin_zero_price_plan_has_no_pct(self):
        m = t360.margin_block({"price_usd_monthly": 0.0, "overage_usd": 0.0}, 1.0)
        assert m["margin_usd"] == -1.0 and m["margin_pct"] is None

    def test_edit_rate(self):
        e = t360.edit_rate_block({"ai_versions": 10, "tenant_edits": 3, "tours_generated": 8, "tours_edited": 2})
        assert e["edit_rate"] == 0.25 and e["edits_per_version"] == 0.3
        assert t360.edit_rate_block(None)["edit_rate"] is None

    def test_seo_market_codes(self):
        codes, unsupported, defaulted = t360.seo_market_codes(json.dumps({"countries": ["DE", "AE", "UK"]}))
        assert set(codes) == {"DE", "UK"} and unsupported == ["AE"] and defaulted is False
        codes, unsupported, defaulted = t360.seo_market_codes(None)
        assert codes == ["US"] and unsupported == [] and defaulted is True
        codes, unsupported, defaulted = t360.seo_market_codes({"countries": ["AE"]})
        assert codes == ["US"] and unsupported == ["AE"] and defaulted is True


# ── routes ────────────────────────────────────────────────────────────────────────────────────

def _tenant_row():
    return {"tenant_id": TENANT_ID, "name": "Atlas", "plan_tier": "growth"}


class TestCostRoute:
    @pytest.mark.asyncio
    async def test_cost_breakdown_and_margin(self):
        conn = _conn()
        usage = {"tenant_name": "Atlas", "plan_tier": "growth", "billing_month": NOW.date(),
                 "tours_quota_monthly": 50, "api_calls_quota_monthly": 1000,
                 "price_usd_monthly": Decimal("99"), "tours_rewritten": 4, "api_calls_used": 10,
                 "quota_tours_pct": 8, "quota_calls_pct": 1, "tours_overage": 0,
                 "overage_usd": Decimal("0"), "llm_cost_usd": Decimal("3.0"),
                 "overage_rate_usd_per_tour": Decimal("1")}
        conn.fetchrow.side_effect = [_tenant_row(), usage]
        conn.fetch.side_effect = [
            [{"stage": "t9_write", "calls": 5, "cost_usd": Decimal("1.5")},
             {"stage": "t2_generate", "calls": 4, "cost_usd": Decimal("1.0")},
             {"stage": "t10_judge", "calls": 5, "cost_usd": Decimal("0.5")}],
            [{"channel": "blog", "pieces": 2, "cost_usd": Decimal("1.2")},
             {"channel": "facebook", "pieces": 3, "cost_usd": Decimal("0.6")}],
        ]
        out = await t360.get_tenant_cost(TENANT_ID, _request(conn), x_admin_secret=_SECRET)
        assert out["total_cost_usd"] == 3.0
        assert out["per_tour"] == {"t2_cost_usd": 1.0, "tours_rewritten": 4, "cost_per_tour_usd": 0.25}
        assert out["unattributed_to_piece_usd"] == 1.2
        assert [k["kind"] for k in out["by_kind"]] == ["blog", "social"]
        assert out["margin"]["revenue_usd"] == 99.0 and out["margin"]["margin_usd"] == 96.0
        assert out["billing_month"] == "2026-10"
        # every cost query excludes shadow rows and is scoped to this tenant + month
        for c in conn.fetch.call_args_list:
            assert '{"shadow": true}' in c.args[0] and "date_trunc('month', now())" in c.args[0]
            assert c.args[1] == TENANT_ID

    @pytest.mark.asyncio
    async def test_unknown_tenant_is_404(self):
        conn = _conn()
        conn.fetchrow.return_value = None
        with pytest.raises(HTTPException) as e:
            await t360.get_tenant_cost(TENANT_ID, _request(conn), x_admin_secret=_SECRET)
        assert e.value.status_code == 404

    @pytest.mark.asyncio
    async def test_needs_admin_secret(self):
        with pytest.raises(HTTPException) as e:
            await t360.get_tenant_cost(TENANT_ID, _request(_conn()), x_admin_secret="wrong")
        assert e.value.status_code == 403


class TestQualityRoute:
    @pytest.mark.asyncio
    async def test_gates_pieces_and_edits(self):
        conn = _conn()
        conn.fetchrow.side_effect = [
            _tenant_row(),
            {"ai_versions": 4, "tenant_edits": 1, "tours_generated": 4, "tours_edited": 1},
        ]
        conn.fetch.side_effect = [
            [{"status": "approved", "n": 8}, {"status": "held", "n": 2}, {"status": "failed", "n": 1}],
            [{"gate": "F9_cta_fact", "evaluated": 10, "passed": 7, "failed_blocking": 2,
              "failed_warn": 1, "held_pieces": 2}],
            [{"held_reason": "F9_cta_fact", "n": 2}],
        ]
        out = await t360.get_tenant_quality(TENANT_ID, _request(conn), days=30, x_admin_secret=_SECRET)
        assert out["pieces"]["shipped"] == 10 and out["pieces"]["held_rate"] == 0.2
        assert out["gates"][0]["pass_rate"] == 0.7 and out["gates"][0]["held_pieces"] == 2
        assert out["held_reasons"] == [{"held_reason": "F9_cta_fact", "count": 2}]
        assert out["edits"]["edit_rate"] == 0.25
        assert all(c.args[1:] == (TENANT_ID, 30) for c in conn.fetch.call_args_list)


class TestMarketsRoute:
    @pytest.mark.asyncio
    async def test_coverage_per_seo_market(self):
        conn = _conn()
        conn.fetchrow.return_value = _tenant_row()
        conn.fetchval.side_effect = [json.dumps({"countries": ["UK", "AE"], "declared": ["UK", "UAE"]}),
                                     ["US"]]
        conn.fetch.side_effect = [
            [{"market": "UK", "places_in_scope": 10, "researched": 6, "fresh": 5, "last_researched": NOW}],
            [{"country_code": "AE", "first_seen_at": NOW, "requested_at": NOW, "resolved_at": None}],
        ]
        out = await t360.get_tenant_markets(TENANT_ID, _request(conn), x_admin_secret=_SECRET)
        assert out["seo"]["markets"] == ["UK"] and out["seo"]["unsupported"] == ["AE"]
        assert out["coverage"] == [{"market": "UK", "places_in_scope": 10, "researched": 6, "fresh": 5,
                                    "fresh_pct": 50.0, "last_researched": NOW.isoformat()}]
        assert out["planning_markets"] == ["US"]
        assert out["unmapped_requests"][0]["country_code"] == "AE"
        cov_call = conn.fetch.call_args_list[0]
        assert cov_call.args[1:] == (TENANT_ID, False, ["UK"], t360.FRESH_DAYS)
        # an admin page view never records an unmapped request (only reads the table)
        assert not any("INSERT" in c.args[0] for c in conn.execute.call_args_list)

    @pytest.mark.asyncio
    async def test_aa_internal_scope_flag(self):
        conn = _conn()
        conn.fetchrow.return_value = _tenant_row()
        conn.fetchval.side_effect = [None, None]
        conn.fetch.side_effect = [[], []]
        internal = uuid.UUID(t360.AA_INTERNAL_TENANT)
        out = await t360.get_tenant_markets(internal, _request(conn), x_admin_secret=_SECRET)
        assert conn.fetch.call_args_list[0].args[2] is True
        assert out["seo"]["defaulted_to_us"] is True and out["seo"]["configured"] is False
        assert out["coverage"][0]["market"] == "US" and out["coverage"][0]["fresh_pct"] is None
        assert out["planning_markets"] is None


def test_routes_registered_on_app():
    from api.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    for suffix in ("cost", "quality", "markets"):
        assert f"/admin/tenants/{{tenant_id}}/{suffix}" in paths
