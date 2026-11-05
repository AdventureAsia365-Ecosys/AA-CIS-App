"""AA-1037 (root ADR 0019 d9 "Screens now") — Tenant 360 › Billing › Booking invoices on real Postgres, through the
real app: GET /admin/tenants/{tenant_id}/booking-billing.

``booking.v_tenant_billing_status`` (bk-042) is stood in by a table of its exact columns (as in
test_aa1037_tenant_billing_tick.py), created for this module and dropped after it. Covers: the answer (view row +
account + live subscriptions); billing.read (finance) on any tenant; customer_success on its book only (out of book:
403 "Tenant outside your scope", as for a tenant id that does not exist); support refused by the policy; no Booking
row -> booking null; the view gone -> available false. Calls carry the main admin secret + x-admin-role +
x-admin-user-id (the machine / CI path). Names are invented.
"""
import os
import sys
import uuid
from datetime import date

import asyncpg
import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = f"aa1037-it-{uuid.uuid4().hex}"   # a fresh value per run, never a fixed string
_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
# Copied from AA-Booking migrations/042_agency_billing_guards_status_view.sql (the view's columns and types).
BK042_COLUMNS = ("tenant_id uuid, status text, oldest_overdue_days integer, overdue_invoices integer, "
                 "overdue_amount jsonb, paid_through date, warn_after_days integer, suspend_after_days integer")


def _sql(name):
    with open(os.path.join(_MIG, name), encoding="utf-8") as f:
        return f.read()


@pytest_asyncio.fixture
async def world(monkeypatch):
    from api.main import app
    from api.routers import admin

    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    for name in ("362_admin_role_r2_staff_roles.sql", "363_staff_roles_reviewer_merge_finance_grant.sql",
                 "364_staff_tenant_assignment.sql"):
        await pool.execute(_sql(name))
    had_schema = await pool.fetchval("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'booking')")
    await pool.execute("CREATE SCHEMA IF NOT EXISTS booking")
    await pool.execute("DROP TABLE IF EXISTS booking.v_tenant_billing_status")
    await pool.execute(f"CREATE TABLE booking.v_tenant_billing_status ({BK042_COLUMNS})")
    tag = uuid.uuid4().hex[:8]
    t = {k: str(uuid.uuid4()) for k in ("mine", "other", "quiet")}
    for key, tid in t.items():
        await pool.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, kind) "
            "VALUES ($1, $2, $3, 'starter', 3, 'demo')",
            uuid.UUID(tid), f"Cobalt Ridge {key}", f"aa1037-{tag}-{key}")
    await pool.execute("UPDATE shared.tenants SET account_status = 'past_due' WHERE tenant_id = $1", uuid.UUID(t["mine"]))
    for key in ("mine", "other"):
        await pool.execute(
            "INSERT INTO booking.v_tenant_billing_status VALUES ($1, 'warn', 12, 2, $2::jsonb, $3, 7, 30)",
            uuid.UUID(t[key]), '{"USD": 240.50, "EUR": 80}', date(2026, 11, 30))
    s = {k: str(uuid.uuid4()) for k in ("admin", "finance", "csm", "support")}
    roles = {"admin": "admin", "finance": "finance", "csm": "customer_success", "support": "support"}
    for key, sid in s.items():
        await pool.execute(
            "INSERT INTO shared.admin_users (id, username, role, is_active, email) "
            "VALUES ($1, $2, $3::shared.admin_role, true, $4)",
            uuid.UUID(sid), f"aa1037-{tag}-{key}", roles[key], f"aa1037-{tag}-{key}@adventure.asia")
    await pool.execute(
        "INSERT INTO shared.staff_tenant_assignment (tenant_id, admin_user_id, kind, created_by) "
        "VALUES ($1, $2, 'owner', 'aa1037-it')", uuid.UUID(t["mine"]), uuid.UUID(s["csm"]))
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, pool, t, s, roles
    staff_ids = [uuid.UUID(v) for v in s.values()]
    await pool.execute("DROP TABLE IF EXISTS booking.v_tenant_billing_status")
    if not had_schema:
        await pool.execute("DROP SCHEMA IF EXISTS booking")
    await pool.execute("DELETE FROM shared.tenant_api_usage WHERE admin_user_id = ANY($1::uuid[])", staff_ids)
    await pool.execute("DELETE FROM shared.staff_tenant_assignment WHERE admin_user_id = ANY($1::uuid[])", staff_ids)
    await pool.execute("DELETE FROM shared.tenants WHERE slug LIKE $1", f"aa1037-{tag}-%")
    await pool.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", staff_ids)
    await pool.close()


def _as(s, roles, who):
    return {"X-Admin-Secret": SECRET, "x-admin-role": roles[who], "x-admin-user-id": s[who]}


def _url(tenant):
    return f"/admin/tenants/{tenant}/booking-billing"


@pytest.mark.asyncio
async def test_admin_reads_the_view_row_account_and_live_subscriptions(world):
    client, pool, t, s, roles = world
    res = await client.get(_url(t["mine"]), headers=_as(s, roles, "admin"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tenant_id"] == t["mine"] and body["available"] is True
    assert body["booking"] == {"status": "warn", "oldest_overdue_days": 12, "overdue_invoices": 2,
                               "overdue_amount": {"EUR": "80", "USD": "240.50"}, "paid_through": "2026-11-30",
                               "warn_after_days": 7, "suspend_after_days": 30}
    assert body["account"] == {"account_status": "past_due", "suspend_reason": None}
    live = await pool.fetch("SELECT product FROM shared.tenant_subscription WHERE tenant_id = $1 "
                            "AND status IN ('trialing', 'active', 'past_due') ORDER BY product", uuid.UUID(t["mine"]))
    assert [x["product"] for x in body["subscriptions"]] == [r["product"] for r in live]
    assert set(body) == {"tenant_id", "available", "booking", "account", "subscriptions"}


@pytest.mark.asyncio
async def test_finance_reads_any_tenant(world):
    client, _pool, t, s, roles = world
    for key in ("mine", "other"):
        res = await client.get(_url(t[key]), headers=_as(s, roles, "finance"))
        assert res.status_code == 200, (key, res.text)


@pytest.mark.asyncio
async def test_customer_success_reads_its_book_only(world):
    client, _pool, t, s, roles = world
    mine = await client.get(_url(t["mine"]), headers=_as(s, roles, "csm"))
    assert mine.status_code == 200, mine.text
    for other in (t["other"], str(uuid.uuid4())):
        res = await client.get(_url(other), headers=_as(s, roles, "csm"))
        assert res.status_code == 403 and res.json()["detail"] == "Tenant outside your scope", res.text


@pytest.mark.asyncio
async def test_support_is_refused(world):
    client, _pool, t, s, roles = world
    res = await client.get(_url(t["mine"]), headers=_as(s, roles, "support"))
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_without_the_secret_nothing_is_read(world):
    client, _pool, t, _s, _roles = world
    res = await client.get(_url(t["mine"]))
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_no_booking_row_and_view_gone(world):
    client, pool, t, s, roles = world
    quiet = await client.get(_url(t["quiet"]), headers=_as(s, roles, "admin"))
    assert quiet.status_code == 200 and quiet.json()["available"] is True and quiet.json()["booking"] is None
    await pool.execute("DROP TABLE booking.v_tenant_billing_status")
    gone = await client.get(_url(t["mine"]), headers=_as(s, roles, "admin"))
    assert gone.status_code == 200, gone.text
    assert gone.json()["available"] is False and gone.json()["booking"] is None
    assert gone.json()["account"]["account_status"] == "past_due"
