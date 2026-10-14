"""AA-797 (S235) — admin tenant writes against real Postgres: create sets plan_id, PATCH body
suspends/resumes and changes plan with audit rows, offboarded is terminal (409), offboard cancels
only QUEUED jobs, generate-key is audited, /usage returns 200 from shared.tenant_api_usage.

shared.membership_plans and shared.tenants.plan_id exist live but in no repo migration yet (the
AA-798 baseline, migration 223, adds them). Until then this fixture creates both with
IF NOT EXISTS, using the live column list from docs/architecture/db-schema-reference.md, so the
test runs on the 007-shaped CI DB; once 223 lands the DDL is a no-op. Same for a two-column
stand-in of gold_aa_internal.tenant_tour_versions (only migration 001 creates it; the test DB
skips 001).
"""
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SLUG = f"aa797-{uuid.uuid4().hex[:8]}"
HDR = {"x_admin_secret": "s", "x_admin_user_id": "admin-797", "x_admin_role": "admin"}


@pytest_asyncio.fixture
async def ctx(monkeypatch):
    from api.routers import admin
    monkeypatch.setattr(admin, "verify_admin_secret", lambda _s: None)
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    await p.execute("""
        CREATE TABLE IF NOT EXISTS shared.membership_plans (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_name text NOT NULL,
            tours_quota_monthly integer NOT NULL,
            api_calls_quota_monthly integer NOT NULL,
            price_usd_monthly numeric NOT NULL,
            overage_rate_usd_per_tour numeric NOT NULL,
            overage_rate_usd_per_1k_calls numeric NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now())""")
    await p.execute("ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS plan_id uuid")
    # gold_aa_internal.tenant_tour_versions is created only by migration 001, which the test DB
    # skips. /usage counts T2 versions from it, so stand in the two columns it reads (dropped
    # again below if this test created it).
    made_ttv = not await p.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NOT NULL")
    if made_ttv:
        await p.execute("CREATE SCHEMA IF NOT EXISTS gold_aa_internal")
        await p.execute("CREATE TABLE gold_aa_internal.tenant_tour_versions "
                        "(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, "
                        "published_tour_id uuid NOT NULL)")
    added_plans, plans = [], {}
    for name, quota in (("starter", 10), ("business", 200)):
        pid = await p.fetchval(
            "SELECT id FROM shared.membership_plans WHERE plan_name = $1 AND is_active LIMIT 1", name)
        if pid is None:
            pid = await p.fetchval("""
                INSERT INTO shared.membership_plans (plan_name, tours_quota_monthly, api_calls_quota_monthly,
                    price_usd_monthly, overage_rate_usd_per_tour, overage_rate_usd_per_1k_calls)
                VALUES ($1, $2, 1000, 99, 1, 1) RETURNING id""", name, quota)
            added_plans.append(pid)
        plans[name] = pid
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=p)))
    state = {"tenant": None}
    yield SimpleNamespace(admin=admin, pool=p, request=request, plans=plans, state=state)
    tid = state["tenant"]
    if tid:
        await p.execute("DELETE FROM acp_shared.audit_log WHERE tenant_id = $1", tid)
        await p.execute("DELETE FROM shared.job WHERE payload->>'tenant_id' = $1", tid)
        await p.execute("DELETE FROM shared.tenant_api_usage WHERE tenant_id = $1::uuid", tid)
        await p.execute("DELETE FROM shared.tenant_brand_rules WHERE tenant_id = $1::uuid", tid)
        await p.execute("DELETE FROM acp_shared.acp_quota_ledger WHERE tenant_id = $1::uuid", tid)
        await p.execute("DELETE FROM shared.jev_tenant_allowlist WHERE tenant_id = $1::uuid", tid)
        await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1::uuid", tid)
    if made_ttv:
        await p.execute("DROP TABLE gold_aa_internal.tenant_tour_versions")
    for pid in added_plans:
        await p.execute("DELETE FROM shared.membership_plans WHERE id = $1", pid)
    await p.close()


async def _list_status(admin, pool, req, tid, include_inactive):
    """list_tenants LEFT JOINs shared.v_tenant_monthly_usage on its LIVE columns, which the
    007-shaped CI view does not have until AA-798's migration 223 (lesson S231: check the
    column exists before relying on it). Returns None when the list cannot run here; the
    status derivation is also unit-tested."""
    has_live_view = await pool.fetchval(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'shared' "
        "AND table_name = 'v_tenant_monthly_usage' AND column_name = 'api_calls_used'")
    if not has_live_view:
        return None
    listed = await admin.list_tenants(req, x_admin_secret="s", include_inactive=include_inactive)
    return [t["status"] for t in listed["tenants"] if t["tenant_id"] == tid]


async def _audit(pool, tid, action):
    return await pool.fetch(
        "SELECT actor, details FROM acp_shared.audit_log WHERE tenant_id = $1 AND action = $2", tid, action)


@pytest.mark.asyncio
async def test_tenant_lifecycle_end_to_end(ctx):
    admin, pool, req = ctx.admin, ctx.pool, ctx.request
    A = admin.AdminTenantAuditAction

    # create -> plan_id set, onboard audited with the real admin
    created = await admin.create_tenant(
        admin.CreateTenantRequest(name="AA-797 test", slug=SLUG, plan_tier="starter", posts_per_week=3),
        req, **HDR)
    tid = ctx.state["tenant"] = created.tenant_id
    row = await pool.fetchrow("SELECT plan_id, is_active FROM shared.tenants WHERE tenant_id = $1::uuid", tid)
    assert row["plan_id"] == ctx.plans["starter"] and row["is_active"] is True
    assert [r["actor"] for r in await _audit(pool, tid, A.ONBOARD)] == ["admin:admin-797"]

    # suspend, then resume: DB really changes; one audit row each with before/after
    await admin.update_tenant(uuid.UUID(tid), admin.UpdateTenantRequest(is_active=False), req, **HDR)
    assert await pool.fetchval("SELECT is_active FROM shared.tenants WHERE tenant_id = $1::uuid", tid) is False
    assert await _list_status(admin, pool, req, tid, False) in (None, [])
    assert await _list_status(admin, pool, req, tid, True) in (None, ["suspended"])
    await admin.update_tenant(uuid.UUID(tid), admin.UpdateTenantRequest(is_active=True), req, **HDR)
    assert await pool.fetchval("SELECT is_active FROM shared.tenants WHERE tenant_id = $1::uuid", tid) is True
    status_rows = await _audit(pool, tid, A.STATUS_CHANGED)
    assert len(status_rows) == 2

    # plan change: plan_tier, plan_id and rpm together
    await admin.update_tenant(uuid.UUID(tid), admin.UpdateTenantRequest(plan_tier="business"), req, **HDR)
    row = await pool.fetchrow(
        "SELECT plan_tier::text AS tier, plan_id, rate_limit_rpm FROM shared.tenants WHERE tenant_id = $1::uuid", tid)
    assert (row["tier"], row["plan_id"], row["rate_limit_rpm"]) == ("business", ctx.plans["business"], 1000)
    assert len(await _audit(pool, tid, A.PLAN_CHANGED)) == 1

    # key rotation audited
    await admin.generate_api_key(uuid.UUID(tid), req, **HDR)
    assert len(await _audit(pool, tid, A.KEY_ROTATED)) == 1

    # /usage: 200 from tenant_api_usage (the view's 007 column names are no longer read)
    await pool.executemany(
        "INSERT INTO shared.tenant_api_usage (tenant_id, endpoint, method, status_code, response_ms) "
        "VALUES ($1::uuid, '/v1/quota', 'GET', $2, 20)", [(tid, 200), (tid, 200), (tid, 429)])
    usage = await admin.get_tenant_usage(uuid.UUID(tid), req, x_admin_secret="s", months=3)
    assert usage["limits"]["tours_per_month"] == 200 and usage["t2_versions"] == 0
    [m] = usage["monthly_usage"]
    assert (m["total_calls"], m["successful_calls"], m["rate_limited_calls"]) == (3, 2, 1)

    # offboard: queued job cancelled, running job untouched, key revoked, audit row
    queued = await pool.fetchval(
        "INSERT INTO shared.job (kind, payload) VALUES ('aa797_test', jsonb_build_object('tenant_id', $1::text)) "
        "RETURNING id", tid)
    running = await pool.fetchval(
        "INSERT INTO shared.job (kind, payload, status) VALUES ('aa797_test', "
        "jsonb_build_object('tenant_id', $1::text), 'running') RETURNING id", tid)
    out = await admin.offboard_tenant(tid, admin.OffboardRequest(reason="aa797 test"), req, **HDR)
    assert out["cancelled_jobs"] == 1
    assert await pool.fetchval("SELECT status FROM shared.job WHERE id = $1", queued) == "cancelled"
    r = await pool.fetchrow("SELECT status, cancel_requested FROM shared.job WHERE id = $1", running)
    assert r["status"] == "running" and r["cancel_requested"] is False
    t = await pool.fetchrow("SELECT is_active, api_key_hash, cancelled_at FROM shared.tenants "
                            "WHERE tenant_id = $1::uuid", tid)
    assert t["is_active"] is False and t["api_key_hash"].startswith("REVOKED_") and t["cancelled_at"]
    assert [a["actor"] for a in await _audit(pool, tid, A.OFFBOARD)] == ["admin:admin-797"]

    # offboarded is terminal
    for call in (
        admin.update_tenant(uuid.UUID(tid), admin.UpdateTenantRequest(is_active=True), req, **HDR),
        admin.generate_api_key(uuid.UUID(tid), req, **HDR),
        admin.offboard_tenant(tid, admin.OffboardRequest(reason="again"), req, **HDR),
    ):
        with pytest.raises(HTTPException) as e:
            await call
        assert e.value.status_code == 409
    assert await _list_status(admin, pool, req, tid, True) in (None, ["offboarded"])
