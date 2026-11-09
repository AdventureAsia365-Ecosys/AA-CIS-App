"""AA-1028 R1 slice 3 without a database: plan validation, the Dev guard, the hourly tick's job and schedule, the
gateway's tenant-spend stages and its entitlement read with the pre-353 fallback, and the new admin routes (POLICY,
admin role only, error mapping). The database paths are in tests/integration/test_aa1028_slice3.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.staff_authz import POLICY, TENANT_READ, TENANT_WRITE
from services.tenant_account import dev_guard
from services.tenant_account import plans as pl
from shared import cost_guard
from shared.jobs import registry
from shared.llm_client import tenant_budget

TID = "7d4c6f0e-1b2a-4c3d-9e8f-0a1b2c3d4e5f"
CATALOGUE = {
    "content.rate_limit_rpm": {"feature_key": "content.rate_limit_rpm", "product": "content", "value_kind": "limit",
                               "unit": "rpm"},
    "content.rewrite": {"feature_key": "content.rewrite", "product": "content", "value_kind": "limit",
                        "unit": "count_month"},
    "content.social": {"feature_key": "content.social", "product": "content", "value_kind": "flag", "unit": None},
    "crm.core": {"feature_key": "crm.core", "product": "crm", "value_kind": "flag", "unit": None},
    "crm.seats": {"feature_key": "crm.seats", "product": "crm", "value_kind": "seats", "unit": "seats"},
    "ai.budget_usd_monthly": {"feature_key": "ai.budget_usd_monthly", "product": "platform", "value_kind": "limit",
                              "unit": "usd_month"},
}


# ── plan validation ─────────────────────────────────────────────────────────────────────────────────────────────

def test_draft_normalises_a_crm_plan():
    d = pl.draft({"name": "  Ferncliff CRM ", "price_usd_monthly": 9.5, "features": [
        {"feature_key": "crm.seats", "limit_value": "5"}, {"feature_key": "crm.core"}]},
        product="crm", tier=None, catalogue=CATALOGUE)
    assert d.name == "Ferncliff CRM" and d.price_unit == "seat" and str(d.price_usd_monthly) == "9.50"
    assert [f.feature_key for f in d.features] == ["crm.core", "crm.seats"]
    assert str(d.features[1].limit_value) == "5.00" and d.is_public is False


_TIERED = [{"feature_key": "content.rate_limit_rpm", "limit_value": 300},
           {"feature_key": "content.rewrite", "limit_value": 200},
           {"feature_key": "ai.budget_usd_monthly", "limit_value": 40}]


def _without(key, **values):
    return [{**f, **({"limit_value": values[f["feature_key"]]} if f["feature_key"] in values else {})}
            for f in _TIERED if f["feature_key"] != key]


def test_a_tiered_content_plan_with_its_enforced_limits():
    d = pl.draft({"name": "x", "features": _TIERED}, product="content", tier="growth", catalogue=CATALOGUE)
    assert [f.feature_key for f in d.features] == ["ai.budget_usd_monthly", "content.rate_limit_rpm",
                                                   "content.rewrite"]
    # 0 = no AI included (explicit); the rewrite quota may be "no limit" (the membership_plans fallback reads it)
    pl.draft({"name": "x", "features": _without(None, **{"ai.budget_usd_monthly": 0, "content.rewrite": None})},
             product="content", tier="growth", catalogue=CATALOGUE)
    # AA's own tier may leave the AI budget unlimited
    pl.draft({"name": "x", "features": _without(None, **{"ai.budget_usd_monthly": None})}, product="content",
             tier="internal", catalogue=CATALOGUE)
    # a content plan without a tier has no enforced set
    pl.draft({"name": "x", "features": []}, product="content", tier=None, catalogue=CATALOGUE)


@pytest.mark.parametrize("payload,product,tier,code", [
    ({"name": ""}, "crm", None, "invalid_name"),
    ({"name": "x", "price_unit": "month"}, "crm", None, "invalid_price_unit"),
    ({"name": "x", "seats_included": -1}, "crm", None, "invalid_seats"),
    ({"name": "x", "is_public": "yes"}, "crm", None, "invalid_is_public"),
    ({"name": "x", "price_usd_monthly": -1}, "crm", None, "invalid_number"),
    ({"name": "x", "price_usd_monthly": "1.005"}, "crm", None, "invalid_number"),
    ({"name": "x", "price_usd_monthly": True}, "crm", None, "invalid_number"),
    ({"name": "x", "price_usd_monthly": "NaN"}, "crm", None, "invalid_number"),
    ({"name": "x", "features": "crm.core"}, "crm", None, "invalid_features"),
    ({"name": "x", "features": [{"feature_key": "crm.core"}] * 2}, "crm", None, "invalid_features"),
    ({"name": "x", "features": [{"feature_key": "crm.nope"}]}, "crm", None, "unknown_feature"),
    ({"name": "x", "features": [{"feature_key": "content.social"}]}, "crm", None, "feature_product_mismatch"),
    ({"name": "x", "features": [{"feature_key": "crm.core", "limit_value": 1}]}, "crm", None, "flag_takes_no_limit"),
    ({"name": "x", "features": []}, "content", "growth", "rate_limit_required"),
    ({"name": "x", "features": [{"feature_key": "content.rate_limit_rpm", "limit_value": 0}]}, "content", "growth",
     "rate_limit_required"),
    # the limiter's column is an integer (S266 review: a plan RPM edit moves it)
    ({"name": "x", "features": [{"feature_key": "content.rate_limit_rpm", "limit_value": "300.5"}]}, "content",
     "growth", "rate_limit_whole"),
    ({"name": "x", "features": [{"feature_key": "content.rate_limit_rpm", "limit_value": "300.5"}]}, "content", None,
     "rate_limit_whole"),
    # S266 review of slice 3: a tiered content plan lists its enforced limits ("no row" = no limit)
    ({"name": "x", "features": _without("ai.budget_usd_monthly")}, "content", "growth", "enforced_limit_required"),
    ({"name": "x", "features": _without(None, **{"ai.budget_usd_monthly": None})}, "content", "growth",
     "enforced_limit_required"),
    ({"name": "x", "features": _without("content.rewrite")}, "content", "starter", "enforced_limit_required"),
    # platform keys only on content plans (the resolver reads them there, CIS 361)
    ({"name": "x", "features": [{"feature_key": "ai.budget_usd_monthly", "limit_value": 3}]}, "crm", None,
     "platform_key_content_only"),
    # bounds: counts and RPM are whole numbers their int4 readers hold; RPM <= RATE_LIMIT_RPM_MAX
    ({"name": "x", "features": _without(None, **{"content.rewrite": 10_000_000_000})}, "content", "growth",
     "invalid_number"),
    ({"name": "x", "features": _without(None, **{"content.rewrite": "12.5"})}, "content", "growth", "invalid_number"),
    ({"name": "x", "features": _without(None, **{"content.rate_limit_rpm": 10001})}, "content", "growth",
     "invalid_number"),
    ({"name": "x", "features": [{"feature_key": "crm.seats", "limit_value": 2**31}]}, "crm", None, "invalid_number"),
])
def test_draft_refuses(payload, product, tier, code):
    with pytest.raises(pl.PlanError) as e:
        pl.draft(payload, product=product, tier=tier, catalogue=CATALOGUE)
    assert e.value.code == code


def test_draft_edit_keeps_what_the_payload_leaves_out():
    base = {"name": "Old", "price_usd_monthly": "5.00", "price_unit": "seat", "seats_included": 2, "is_public": True,
            "notes": "n", "features": [{"feature_key": "crm.core", "limit_value": None}]}
    d = pl.draft({"name": "New"}, product="crm", tier=None, catalogue=CATALOGUE, base=base)
    assert (d.name, str(d.price_usd_monthly), d.seats_included, d.is_public, d.notes) == ("New", "5.00", 2, True, "n")
    assert [f.feature_key for f in d.features] == ["crm.core"]


@pytest.mark.asyncio
async def test_tier_rpm_refuses_unknown_tiers_without_a_query():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=300)
    assert await pl.tier_rate_limit_rpm(conn, "growth") == 300
    assert await pl.tier_rate_limit_rpm(conn, "platinum") is None and conn.fetchval.await_count == 1


@pytest.mark.asyncio
async def test_update_needs_a_version_and_never_moves_keys():
    for payload, code in (({}, "version_required"), ({"version": True}, "version_required"),
                          ({"version": 1, "tier": "growth"}, "immutable_field"),
                          ({"version": 1, "product": "crm"}, "immutable_field")):
        with pytest.raises(pl.PlanError) as e:
            await pl.update_plan(MagicMock(), "crm.core", payload, actor="admin:x")
        assert e.value.code == code


@pytest.mark.asyncio
async def test_follow_rpm_moves_only_on_a_content_rpm_change():
    def d(rpm):
        feats = () if rpm is None else (pl.PlanFeature(pl.RPM_KEY, pl.parse_decimal(rpm, "rpm", pl.LIMIT_MAX)),)
        return pl.PlanDraft(name="x", price_usd_monthly=None, price_unit="tenant", seats_included=None,
                            price_extra_seat_usd=None, is_public=False, notes=None, features=feats)

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"tenant_id": TID}, {"tenant_id": TID}])
    assert await pl._follow_rpm(conn, "content.growth", "content", d(300), d(450)) == 2
    assert conn.fetch.await_args.args == (pl.FOLLOW_RPM_SQL, "content.growth", 450, 300)
    for args in (("crm.core", "crm", d(300), d(450)), ("content.x", "content", d(None), d(450)),
                 ("content.x", "content", d(300), d(None)), ("content.x", "content", d(300), d(300))):
        conn.fetch.reset_mock()
        assert await pl._follow_rpm(conn, *args) == 0 and conn.fetch.await_count == 0
    # only tenants on this plan whose column is still the old default move
    assert "t.rate_limit_rpm = $3" in pl.FOLLOW_RPM_SQL and "s.plan_key = $1" in pl.FOLLOW_RPM_SQL


# ── Dev guard (root ADR 0019 d16) ───────────────────────────────────────────────────────────────────────────────

def test_dev_guard_matrix(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert dev_guard.default_kind() == "demo"
    assert dev_guard.refusal("demo", None) is None
    assert dev_guard.refusal("agency", None)[0] == "dev_real_tenant_refused"
    assert dev_guard.refusal("agency", 8) is None
    assert dev_guard.refusal("agency", 9)[0] == "invalid_pilot_weeks"
    assert dev_guard.refusal("demo", 2)[0] == "pilot_needs_agency"
    for env in ("prod", "test", "DEV", ""):
        monkeypatch.setenv("ENVIRONMENT", env)
        assert dev_guard.default_kind() == "agency" and dev_guard.refusal("agency", None) is None
        assert dev_guard.refusal("agency", 4)[0] == "pilot_dev_only"
    monkeypatch.delenv("ENVIRONMENT")
    assert dev_guard.default_kind() == "agency"


# ── the hourly tick ─────────────────────────────────────────────────────────────────────────────────────────────

def test_tick_kind_registered():
    from services import jobs  # noqa: F401  (registers every kind)
    from services.jobs import tenant_account_tick_job as job

    k = registry.get_kind(job.KIND)
    assert k is not None and k.concurrency == 1 and k.max_attempts == 2
    assert not k.deploy_blocking and not k.background


@pytest.mark.asyncio
async def test_enqueue_hourly_once_per_hour(monkeypatch):
    from services.jobs import tenant_account_tick_job as job

    now = datetime(2026, 11, 3, 14, 37, tzinfo=timezone.utc)
    assert job.schedule_key(now) == "tenant_account_tick:2026-11-03T14"
    enq = AsyncMock(return_value=("job-1", True))
    monkeypatch.setattr(job, "enqueue", enq)
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    assert await job.enqueue_hourly(pool, now=now) == ("job-1", True, "tenant_account_tick:2026-11-03T14")
    assert enq.await_args.kwargs == {"idempotency_key": "tenant_account_tick:2026-11-03T14",
                                     "created_by": job.SCHEDULER}
    pool.fetchval = AsyncMock(return_value=True)
    enq.reset_mock()
    assert (await job.enqueue_hourly(pool, now=now))[:2] == (None, False) and enq.await_count == 0


@pytest.mark.asyncio
async def test_sampler_part(monkeypatch):
    from api.core import ops_metrics_sampler as sampler
    from services.jobs import tenant_account_tick_job as job

    monkeypatch.setattr(job, "enqueue_hourly", AsyncMock(return_value=("job-9", True, "k")))
    out: dict = {}
    await sampler._tenant_account_tick_part(MagicMock(), {job.KIND: object()}, datetime.now(timezone.utc), out)
    assert out == {"tenant_account_tick": {"job_id": "job-9", "key": "k"}}
    out = {}
    await sampler._tenant_account_tick_part(MagicMock(), {}, datetime.now(timezone.utc), out)   # not registered
    monkeypatch.setenv(job.SWITCH_ENV, "off")
    await sampler._tenant_account_tick_part(MagicMock(), {job.KIND: object()}, datetime.now(timezone.utc), out)
    assert out == {}


@pytest.mark.asyncio
async def test_tick_job_runs_the_lifecycle(monkeypatch):
    from services.jobs import tenant_account_tick_job as job
    from services.tenant_account import lifecycle
    from shared.jobs.queue import Job

    result = {"trials": {"tenants": 1, "subscriptions": {"past_due": 1}, "accounts": {"past_due": 1}, "failed": 0},
              "expired_entitlements_synced": 2, "pilots_ended": []}
    monkeypatch.setattr(lifecycle, "tick", AsyncMock(return_value=result))
    ctx = registry.JobContext(MagicMock(), Job(id="j-7", kind=job.KIND, payload={"trigger": "schedule"},
                                               status="running", attempt=1, max_attempts=2, progress={},
                                               cost_usd=0.0))
    out = await job.run(ctx)
    assert out["trigger"] == "schedule" and out["expired_entitlements_synced"] == 2 and ctx.result == out
    assert lifecycle.actor_for("j-7") == "job:tenant_account_tick:j-7"


# ── the gateway's tenant AI budget ──────────────────────────────────────────────────────────────────────────────

def test_tenant_spend_stages():
    assert tenant_budget.is_tenant_spend_stage("t9_write") and tenant_budget.is_tenant_spend_stage("adhoc_aa1028")
    assert not tenant_budget.is_tenant_spend_stage("s1_generate")
    assert not tenant_budget.is_tenant_spend_stage(None) and not tenant_budget.is_tenant_spend_stage("")


def test_an_adhoc_call_of_a_tenant_past_its_budget_is_refused(monkeypatch):
    budget = cost_guard.TenantAIBudget(tenant_id=TID, monthly_usd=0.01, month_spent_usd=0.0102, alert_pct=80,
                                       approval_pct=100, hard_stop=True, month_calls=34)
    monkeypatch.setattr(tenant_budget, "loader_tenants", lambda: frozenset({TID}))
    monkeypatch.setattr(tenant_budget, "loader_state", lambda tid: budget)
    monkeypatch.setattr(tenant_budget, "_bound_tenant", lambda: TID)
    tenant_budget.invalidate()
    try:
        with pytest.raises(tenant_budget.TenantBudgetApprovalRequired):
            tenant_budget.check("adhoc_aa1028")
        assert tenant_budget.check("s1_generate") is budget           # a platform stage is never stopped
    finally:
        tenant_budget.invalidate()


class _Conn:
    """Answers the entitlement read, or raises UndefinedTableError for the SQL that names ``missing``."""

    def __init__(self, missing=(), row=None):
        self.missing, self.row, self.sql = missing, row, []

    def _check(self, sql):
        self.sql.append(sql)
        if any(m in sql for m in self.missing):
            raise type("UndefinedTableError", (Exception,), {})("missing")

    async def fetchrow(self, sql, *args):
        self._check(sql)
        return self.row

    async def fetch(self, sql, *args):
        self._check(sql)
        return [{"tenant_id": TID}]


def test_budget_reads_the_entitlement_then_falls_back():
    row = {"tenant_id": TID, "monthly_usd": 15, "alert_pct": 80, "approval_pct": 100, "hard_stop": True,
           "spent": 15.2, "calls": 40, "source": "plan"}
    c = _Conn(row=row)
    b = asyncio.run(cost_guard.tenant_budget_check(c, TID))
    assert b.needs_approval and "v_tenant_entitlement_resolved" in c.sql[0] and len(c.sql) == 1
    # before CIS 353 (no resolver view): the AA-1016 row alone
    c = _Conn(missing=("v_tenant_entitlement_resolved",), row=row)
    assert asyncio.run(cost_guard.tenant_budget_check(c, TID)) is not None and len(c.sql) == 2
    assert "v_tenant_entitlement_resolved" not in c.sql[1]
    assert asyncio.run(cost_guard.budgeted_tenants(c)) == frozenset({TID})
    # before 321: nothing
    c = _Conn(missing=("shared.",))
    assert asyncio.run(cost_guard.tenant_budget_check(c, TID)) is None
    assert asyncio.run(cost_guard.budgeted_tenants(c)) is None


def test_a_zero_budget_stops_the_first_call():
    # "0 = no AI" (S266 review): a 0 USD budget refuses before any spend
    b = cost_guard.TenantAIBudget(tenant_id=TID, monthly_usd=0.0, month_spent_usd=0.0, alert_pct=80,
                                  approval_pct=100, hard_stop=True)
    assert b.needs_approval and b.state == "approval"
    # ... whatever the AA-1016 row's hard_stop says (an override of 0 is the revocation path)
    off = cost_guard.TenantAIBudget(tenant_id=TID, monthly_usd=0.0, month_spent_usd=0.0, hard_stop=False,
                                    approval_pct=1000)
    assert off.stops and off.needs_approval and off.summary()["hard_stop"] is True
    # a budget above 0 keeps the row's hard_stop off (alert only)
    soft = cost_guard.TenantAIBudget(tenant_id=TID, monthly_usd=5.0, month_spent_usd=9.0, hard_stop=False)
    assert not soft.stops and not soft.needs_approval and soft.state == "alert"
    assert "x.monthly_usd = 0" in cost_guard._TENANT_BUDGET_SQL


def test_a_disabled_budget_override_is_a_zero_budget():
    # the resolver drops a disabled key; cost_guard must not read that as "no budget" (fail closed)
    assert "NOT o.enabled" in cost_guard._DISABLED_OVERRIDE
    assert "WHEN d.tenant_id IS NOT NULL THEN 0::numeric" in cost_guard._TENANT_BUDGET_SQL
    assert "d.tenant_id IS NOT NULL AS forced" in cost_guard._TENANT_BUDGET_SQL
    assert "x.forced OR x.monthly_usd = 0 OR x.row_hard_stop" in cost_guard._TENANT_BUDGET_SQL
    assert cost_guard._DISABLED_OVERRIDE.strip() in cost_guard.TENANT_BUDGET_TENANTS_SQL
    row = {"tenant_id": TID, "monthly_usd": 0, "alert_pct": 80, "approval_pct": 100, "hard_stop": True,
           "spent": 0, "calls": 0, "source": "override"}
    b = asyncio.run(cost_guard.tenant_budget_check(_Conn(row=row), TID))
    assert b.monthly_usd == 0.0 and b.needs_approval


def test_shadow_rows_do_not_count_towards_the_tenant_budget():
    # S266 re-review: the gateway's spend follows the Tenant 360 › Cost rule (AA's own A/B shadow cost is not the
    # tenant's), in both sub-selects of every budget read
    rule = """AND NOT coalesce(l.is_shadow, l.quality_signal @> '{"shadow": true}'::jsonb)"""
    assert cost_guard._SPEND_COLUMNS.count(rule) == 2
    for sql in (cost_guard._TENANT_BUDGET_SQL, cost_guard._LEGACY_TENANT_BUDGET_SQL):
        assert sql.count(rule) == 2


def test_plan_budget_defaults():
    assert (cost_guard.PLAN_ALERT_PCT, cost_guard.PLAN_APPROVAL_PCT) == (80, 100)
    assert "coalesce(b.hard_stop, true)" in cost_guard._TENANT_BUDGET_SQL


def test_no_resolved_key_falls_back_to_the_content_plan_budget():
    # S266 review: never "no budget" while the tenant is a customer (a trial past its end before CIS 361, an unpaid
    # or paused subscription): the content subscription plan's value, unless canceled
    assert "s.product = 'content' AND s.status <> 'canceled'" in cost_guard._PLAN_FALLBACK
    assert cost_guard._PLAN_FALLBACK.strip() in cost_guard.TENANT_BUDGET_TENANTS_SQL
    assert cost_guard._PLAN_FALLBACK.strip() in cost_guard._TENANT_BUDGET_SQL
    assert "ELSE p.limit_value END AS monthly_usd" in cost_guard._TENANT_BUDGET_SQL


# ── the admin routes ────────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "aa1028-s3-test-secret"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=True), conn


def _h(role="admin"):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "u-admin"}
    if role is not None:
        h["x-admin-role"] = role
    return h


def test_policy_entries():
    # AA-1035 R2: plans and a tenant's subscription read for customer_success too (book scoped; finance every tenant);
    # never support (S267 review: tenant.book.account.read, not tenant.book.read)
    desk = ("tenant.read", "tenant.book.account.read", "billing.read")
    assert POLICY[("GET", "/admin/plans")] == POLICY[("GET", "/admin/plans/features")] == desk
    for key in (("POST", "/admin/plans"), ("PATCH", "/admin/plans/{plan_key}"),
                ("POST", "/admin/plans/{plan_key}/retire"),
                ("PUT", "/admin/tenants/{tenant_id}/entitlement-overrides/{feature_key}"),
                ("DELETE", "/admin/tenants/{tenant_id}/entitlement-overrides/{feature_key}")):
        assert POLICY[key] == TENANT_WRITE
    assert POLICY[("GET", "/admin/tenants/{tenant_id}/subscription")] == desk


@pytest.mark.parametrize("role", ["content", None])
def test_writes_refuse_non_admin(client, role):
    c, _ = client
    assert c.post("/admin/plans", json={"plan_key": "crm.zz", "product": "crm", "name": "Z"},
                  headers=_h(role)).status_code == 403
    assert c.patch("/admin/plans/crm.core", json={"version": 1}, headers=_h(role)).status_code == 403
    assert c.put(f"/admin/tenants/{TID}/entitlement-overrides/crm.core", json={"reason": "r"},
                 headers=_h(role)).status_code == 403


def test_plan_routes_map_refusals(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(pl, "update_plan", AsyncMock(side_effect=pl.PlanError("version_conflict", "stale", 409)))
    resp = c.patch("/admin/plans/crm.core", json={"version": 1, "name": "Q"}, headers=_h())
    assert resp.status_code == 409 and resp.json()["detail"]["detail"] == "version_conflict"
    assert resp.headers["X-Error-Code"] == "version_conflict"

    upd = AsyncMock(return_value={"plan_key": "crm.core", "changed": ["name"]})
    monkeypatch.setattr(pl, "update_plan", upd)
    assert c.patch("/admin/plans/crm.core", json={"version": 3, "name": "Q"}, headers=_h()).status_code == 200
    assert upd.await_args.args[2] == {"version": 3, "name": "Q"} and upd.await_args.kwargs["actor"] == "admin:u-admin"

    create = AsyncMock(return_value={"plan_key": "crm.zz"})
    monkeypatch.setattr(pl, "create_plan", create)
    body = {"plan_key": "crm.zz", "product": "crm", "name": "Z", "features": [{"feature_key": "crm.core"}]}
    assert c.post("/admin/plans", json=body, headers=_h()).status_code == 201
    sent = create.await_args.args[1]
    assert "price_unit" not in sent and sent["features"] == [{"feature_key": "crm.core", "limit_value": None}]
    assert c.post("/admin/plans", json={**body, "product": "booking"}, headers=_h()).status_code == 422

    monkeypatch.setattr(pl, "list_plans", AsyncMock(return_value=[{"plan_key": "crm.core"}]))
    monkeypatch.setattr(pl, "assignable_tiers", AsyncMock(return_value=["starter"]))
    got = c.get("/admin/plans?product=crm", headers=_h()).json()
    assert got == {"plans": [{"plan_key": "crm.core"}], "assignable_tiers": ["starter"]}


def test_subscription_routes(client, monkeypatch):
    from services.tenant_account import subscription as sub

    c, _ = client
    monkeypatch.setattr(sub, "snapshot", AsyncMock(return_value=None))
    assert c.get(f"/admin/tenants/{TID}/subscription", headers=_h()).status_code == 404
    setter = AsyncMock(return_value={"feature_key": "crm.ai_assist", "applies": True})
    monkeypatch.setattr(sub, "set_override", setter)
    resp = c.put(f"/admin/tenants/{TID}/entitlement-overrides/crm.ai_assist", headers=_h(),
                 json={"enabled": False, "reason": "reviewing texts"})
    assert resp.status_code == 200 and setter.await_args.kwargs["enabled"] is False
    assert c.put(f"/admin/tenants/{TID}/entitlement-overrides/NotAKey", headers=_h(),
                 json={"reason": "r"}).status_code == 422
    monkeypatch.setattr(sub, "remove_override",
                        AsyncMock(side_effect=pl.PlanError("override_not_found", "none", 404)))
    resp = c.delete(f"/admin/tenants/{TID}/entitlement-overrides/crm.ai_assist", headers=_h())
    assert resp.status_code == 404 and resp.json()["detail"]["detail"] == "override_not_found"


# ── overrides no reader would enforce are refused (S266 review) ─────────────────────────────────────────────────

class _TxConn:
    """set_override's reads up to the refusal: the tenant lock and the feature row; no write may happen."""

    def __init__(self, feature):
        self.feature, self.writes = feature, []

    def transaction(self):
        class _T:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False
        return _T()

    async def execute(self, sql, *args):
        if "set_config" not in sql:
            self.writes.append(sql)

    async def fetchval(self, sql, *args):
        return "active"

    async def fetchrow(self, sql, *args):
        from services.tenant_account import subscription as sub

        assert sql == sub.FEATURE_SQL, sql[:60]
        return self.feature


@pytest.mark.parametrize("key,kind,enabled,limit,code", [
    ("content.rate_limit_rpm", "limit", True, "450", "use_rate_limit_patch"),
    ("ai.budget_usd_monthly", "limit", False, None, "limit_cannot_be_disabled"),
    ("content.rewrite", "limit", False, None, "limit_cannot_be_disabled"),
    ("crm.seats", "seats", False, None, "limit_cannot_be_disabled"),
    ("content.rewrite", "limit", True, None, "limit_required"),
])
def test_set_override_refuses_what_no_reader_enforces(key, kind, enabled, limit, code):
    from services.tenant_account import subscription as sub

    conn = _TxConn({"feature_key": key, "product": key.split(".")[0], "value_kind": kind})
    with pytest.raises(pl.PlanError) as e:
        asyncio.run(sub.set_override(conn, TID, key, enabled=enabled, limit_value=limit, reason="zz",
                                     expires_at=None, actor="admin:x"))
    assert e.value.code == code and e.value.status == 422 and conn.writes == []


@pytest.mark.parametrize("key,kind,unit,limit", [
    ("content.rewrite", "limit", "count_month", "3000000000"),     # past int4: the quota read would overflow
    ("content.rewrite", "limit", "count_month", "12.5"),
    ("crm.seats", "seats", "seats", str(2**31)),
])
def test_set_override_bounds_whole_number_limits(key, kind, unit, limit):
    from services.tenant_account import subscription as sub

    conn = _TxConn({"feature_key": key, "product": key.split(".")[0], "value_kind": kind, "unit": unit})
    with pytest.raises(pl.PlanError) as e:
        asyncio.run(sub.set_override(conn, TID, key, enabled=True, limit_value=limit, reason="zz",
                                     expires_at=None, actor="admin:x"))
    assert e.value.code == "invalid_number" and conn.writes == []


def test_bound_limit():
    D = pl.parse_decimal
    assert pl.bound_limit("ai.budget_usd_monthly", "usd_month", D("12.34", "x", pl.LIMIT_MAX)) == D("12.34", "x",
                                                                                                    pl.LIMIT_MAX)
    assert pl.bound_limit("content.rewrite", "count_month", None) is None
    assert pl.bound_limit(pl.RPM_KEY, "rpm", D(pl.RATE_LIMIT_RPM_MAX, "x", pl.LIMIT_MAX)) == pl.RATE_LIMIT_RPM_MAX
    with pytest.raises(pl.PlanError) as e:
        pl.bound_limit(pl.RPM_KEY, "rpm", D("1.5", "x", pl.LIMIT_MAX))
    assert e.value.code == "rate_limit_whole"
    from api.routers import admin

    assert admin.RATE_LIMIT_RPM_MAX == pl.RATE_LIMIT_RPM_MAX == 10000


def test_override_rules_cover_the_limiter_and_the_quota():
    from services.tenant_account import subscription as sub

    assert set(sub.NOT_OVERRIDABLE) == {pl.RPM_KEY} and sub.LIMIT_REQUIRED_KEYS == {"content.rewrite"}
