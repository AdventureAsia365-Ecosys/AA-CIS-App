"""AA-1035 (S267 fix pass) against real Postgres, through the real app.

- The owner seat: support only sends a non-owner invitation again; customer_success invites, changes and removes
  editors and viewers of its book but never grants, changes, removes or re-sends the owner role (tenant.write only).
  The rows prove it: a refused call leaves shared.tenant_members unchanged.
- The shared tenant picker GET /admin/tenant-names (TENANT_NAMES_SQL): names only, scoped like the directory.
- finance opens Tenant 360 on /details: the header only (no tours, runs or brand rules).
- customer_success changes the RPM of its own tenant (tenant.book.write) but never the plan.
Calls carry the main admin secret + x-admin-role + x-admin-user-id (the machine / CI path, as
test_aa1035_tenant_scope_pg.py); the CRM relay of an invitation is replaced by a recorder (no network).
"""
import os
import sys
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _audit_cleanup import delete_audit_rows  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = f"aa1035-fix-it-{uuid.uuid4().hex}"   # a fresh value per run, never a fixed string
ADDRESS = "invitee@agency.example"
_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")


def _sql(name):
    with open(os.path.join(_MIG, name), encoding="utf-8") as f:
        return f.read()


@pytest_asyncio.fixture
async def world(monkeypatch):
    from api.main import app
    from api.routers import admin
    from api.services import crm_console

    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    relays = []

    async def fake_relay(method, path, authorization, *, json=None, params=None):
        relays.append(json)
        return 202, {"status": "pending", "expires_at": "2026-11-11T00:00:00Z"}

    monkeypatch.setattr(crm_console, "crm_ops_call", fake_relay)
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    for name in ("362_admin_role_r2_staff_roles.sql", "363_staff_roles_reviewer_merge_finance_grant.sql",
                 "364_staff_tenant_assignment.sql"):
        await pool.execute(_sql(name))
    # plan_id: the AA-798 baseline column update_tenant() reads (no-op once it exists, as test_aa797 / test_aa819)
    await pool.execute("ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS plan_id uuid")
    tag = uuid.uuid4().hex[:8]
    t = {k: uuid.uuid4() for k in ("mine", "pool", "other")}
    for key, tid in t.items():
        await pool.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, kind, segment) "
            "VALUES ($1, $2, $3, 'starter', 3, 'agency', 'high_touch')",
            tid, f"Lantern Cove {key}", f"aa1035f-{tag}-{key}")
    roles = {"admin": "admin", "csm": "customer_success", "support": "support", "finance": "finance",
             "lead": "content_lead", "writer": "content"}
    s = {k: str(uuid.uuid4()) for k in roles}
    for key, sid in s.items():
        await pool.execute(
            "INSERT INTO shared.admin_users (id, username, role, is_active, email) "
            "VALUES ($1, $2, $3::shared.admin_role, true, $4)",
            uuid.UUID(sid), f"aa1035f-{tag}-{key}", roles[key], f"aa1035f-{tag}-{key}@adventure.asia")
    # csm owns "mine" and support is its support contact; "pool" has no owner (support's pool); "other" is owned by
    # admin, so it is in neither the csm's book nor support's book or pool.
    for tenant, who, kind in ((t["mine"], "csm", "owner"), (t["mine"], "support", "support"),
                              (t["other"], "admin", "owner")):
        await pool.execute("INSERT INTO shared.staff_tenant_assignment (tenant_id, admin_user_id, kind, created_by) "
                           "VALUES ($1, $2, $3, 'test')", tenant, uuid.UUID(s[who]), kind)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, pool, {k: str(v) for k, v in t.items()}, s, roles, relays
    finally:
        staff_ids = [uuid.UUID(v) for v in s.values()]
        await pool.execute("DELETE FROM shared.tenant_api_usage WHERE admin_user_id = ANY($1::uuid[])", staff_ids)
        async with pool.acquire() as conn:
            for tid in t.values():
                await delete_audit_rows(conn, "tenant_id = $1", str(tid))
                async with conn.transaction():
                    await conn.execute("SET LOCAL session_replication_role = replica")
                    await conn.execute("DELETE FROM shared.staff_tenant_assignment WHERE tenant_id = $1", tid)
                    await conn.execute("DELETE FROM shared.tenant_membership_event WHERE tenant_id = $1", tid)
                    await conn.execute("DELETE FROM shared.tenant_members WHERE tenant_id = $1", tid)
                    await conn.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
        await pool.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", staff_ids)
        await pool.close()


def _as(s, roles, who):
    return {"X-Admin-Secret": SECRET, "x-admin-role": roles[who], "x-admin-user-id": s[who]}


async def _member(pool, tenant, role, *, active=False):
    """One membership row as the CIS writers would leave it (an invitation, or an active member with an identity)."""
    if active:
        return str(await pool.fetchval(
            "INSERT INTO shared.tenant_members (tenant_id, content_studio_role, status, cognito_iss, cognito_sub, "
            "created_by) VALUES ($1, $2, 'active', 'https://issuer.example/pool', $3, 'test') "
            "RETURNING membership_id", uuid.UUID(tenant), role, uuid.uuid4().hex))
    return str(await pool.fetchval(
        "INSERT INTO shared.tenant_members (tenant_id, content_studio_role, status, invitation_id, created_by) "
        "VALUES ($1, $2, 'invited', gen_random_uuid(), 'test') RETURNING membership_id", uuid.UUID(tenant), role))


async def _row(pool, mid):
    return await pool.fetchrow("SELECT content_studio_role, status FROM shared.tenant_members "
                               "WHERE membership_id = $1", uuid.UUID(mid))


def _owner_refused(res):
    return res.status_code == 403 and "owner role needs" in res.json()["detail"]


@pytest.mark.asyncio
async def test_support_only_sends_a_non_owner_invitation_again(world):
    client, pool, t, s, roles, relays = world
    editor_inv = await _member(pool, t["mine"], "editor")
    owner_inv = await _member(pool, t["mine"], "owner")
    editor = await _member(pool, t["mine"], "editor", active=True)
    h = _as(s, roles, "support")
    # AA-1035 close-out: support sends again to the address the CRM kept (no address in the body), never a new one
    sent = await client.post(f"/admin/tenants/{t['mine']}/members/{editor_inv}/invitation/send", headers=h, json={})
    assert sent.status_code == 200, sent.text
    assert len(relays) == 1
    readdressed = await client.post(f"/admin/tenants/{t['mine']}/members/{editor_inv}/invitation/send", headers=h,
                                    json={"email": ADDRESS})
    assert readdressed.status_code == 403 and "same address only" in readdressed.json()["detail"]
    assert _owner_refused(await client.post(f"/admin/tenants/{t['mine']}/members/{owner_inv}/invitation/send",
                                            headers=h, json={}))
    assert len(relays) == 1                                     # nothing relayed for the owner invitation
    before = await pool.fetchval("SELECT count(*) FROM shared.tenant_members WHERE tenant_id = $1",
                                 uuid.UUID(t["mine"]))
    for res in (
        await client.post(f"/admin/tenants/{t['mine']}/members/invitations", headers=h,
                          json={"email": ADDRESS, "content_studio_role": "owner"}),
        await client.post(f"/admin/tenants/{t['mine']}/members/invitations", headers=h,
                          json={"email": ADDRESS, "content_studio_role": "viewer"}),
        await client.patch(f"/admin/tenants/{t['mine']}/members/{editor}", headers=h,
                           json={"content_studio_role": "owner"}),
        await client.post(f"/admin/tenants/{t['mine']}/members/{editor}/remove", headers=h),
    ):
        assert res.status_code == 403 and res.json()["detail"].startswith("Role 'support' lacks"), res.text
    assert await pool.fetchval("SELECT count(*) FROM shared.tenant_members WHERE tenant_id = $1",
                               uuid.UUID(t["mine"])) == before
    assert dict(await _row(pool, editor)) == {"content_studio_role": "editor", "status": "active"}
    # outside its book and the pool: refused before the route
    other_inv = await _member(pool, t["other"], "editor")
    res = await client.post(f"/admin/tenants/{t['other']}/members/{other_inv}/invitation/send", headers=h,
                            json={"email": ADDRESS})
    assert res.status_code == 403 and res.json()["detail"] == "Tenant outside your scope"


@pytest.mark.asyncio
async def test_customer_success_never_touches_the_owner_seat(world):
    client, pool, t, s, roles, relays = world
    owner = await _member(pool, t["mine"], "owner", active=True)
    editor = await _member(pool, t["mine"], "editor", active=True)
    owner_inv = await _member(pool, t["mine"], "owner")
    h = _as(s, roles, "csm")
    count = "SELECT count(*) FROM shared.tenant_members WHERE tenant_id = $1"
    before = await pool.fetchval(count, uuid.UUID(t["mine"]))
    assert _owner_refused(await client.post(f"/admin/tenants/{t['mine']}/members/invitations", headers=h,
                                            json={"email": ADDRESS, "content_studio_role": "owner"}))
    assert await pool.fetchval(count, uuid.UUID(t["mine"])) == before and relays == []
    assert _owner_refused(await client.patch(f"/admin/tenants/{t['mine']}/members/{editor}", headers=h,
                                             json={"content_studio_role": "owner"}))
    assert _owner_refused(await client.patch(f"/admin/tenants/{t['mine']}/members/{owner}", headers=h,
                                             json={"content_studio_role": "viewer"}))
    assert _owner_refused(await client.post(f"/admin/tenants/{t['mine']}/members/{owner}/remove", headers=h))
    assert _owner_refused(await client.post(f"/admin/tenants/{t['mine']}/members/{owner_inv}/invitation/send",
                                            headers=h, json={"email": ADDRESS}))
    assert dict(await _row(pool, owner)) == {"content_studio_role": "owner", "status": "active"}
    assert dict(await _row(pool, editor)) == {"content_studio_role": "editor", "status": "active"}
    assert relays == []
    # the non-owner member actions work
    invited = await client.post(f"/admin/tenants/{t['mine']}/members/invitations", headers=h,
                                json={"email": ADDRESS, "content_studio_role": "viewer"})
    assert invited.status_code == 201, invited.text
    changed = await client.patch(f"/admin/tenants/{t['mine']}/members/{editor}", headers=h,
                                 json={"content_studio_role": "viewer"})
    assert changed.status_code == 200 and changed.json()["changed"] is True
    removed = await client.post(f"/admin/tenants/{t['mine']}/members/{editor}/remove", headers=h)
    assert removed.status_code == 200
    assert dict(await _row(pool, editor)) == {"content_studio_role": "viewer", "status": "removed"}
    # admin keeps the owner seat
    admin_h = _as(s, roles, "admin")
    res = await client.patch(f"/admin/tenants/{t['mine']}/members/{owner}", headers=admin_h,
                             json={"content_studio_role": "editor"})
    assert res.status_code == 200 and res.json()["member"]["content_studio_role"] == "editor"


@pytest.mark.asyncio
async def test_the_tenant_picker_is_names_only_and_scoped(world):
    client, pool, t, s, roles, relays = world
    lead = await client.get("/admin/tenant-names", headers=_as(s, roles, "lead"))
    assert lead.status_code == 200, lead.text
    rows = {r["tenant_id"]: r for r in lead.json()["tenants"]}
    assert set(t.values()) <= set(rows)
    assert set(rows[t["mine"]]) == {"tenant_id", "name", "slug", "status", "cancelled_at"}
    assert rows[t["mine"]]["name"] == "Lantern Cove mine" and rows[t["mine"]]["status"] == "active"
    csm = {r["tenant_id"] for r in (await client.get("/admin/tenant-names",
                                                     headers=_as(s, roles, "csm"))).json()["tenants"]}
    assert csm == {t["mine"]}
    support = {r["tenant_id"] for r in (await client.get("/admin/tenant-names",
                                                         headers=_as(s, roles, "support"))).json()["tenants"]}
    assert t["mine"] in support and t["pool"] in support and t["other"] not in support
    writer = await client.get("/admin/tenant-names", headers=_as(s, roles, "writer"))
    assert writer.status_code == 403


@pytest.mark.asyncio
async def test_finance_opens_tenant_360_on_the_header_and_csm_changes_rpm_not_the_plan(world):
    client, pool, t, s, roles, relays = world
    await pool.execute(
        "INSERT INTO shared.tenant_brand_rules (tenant_id, brand_name, system_prompt, version, is_active) "
        "VALUES ($1, 'Lantern', 'secret voice text', 1, true)", uuid.UUID(t["mine"]))
    try:
        has_live_view = await pool.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'shared' "
            "AND table_name = 'v_tenant_monthly_usage' AND column_name = 'api_calls_used'")
        if has_live_view:
            fin = await client.get(f"/admin/tenants/{t['mine']}/details", headers=_as(s, roles, "finance"))
            assert fin.status_code == 200, fin.text
            body = fin.json()
            assert body["rewritten_tours"] == [] and body["pipeline_runs"] == []
            assert body["brand_rules"]["system_prompt"] is None and body["brand_rules"]["version_count"] == 0
            assert isinstance(body["summary"]["total_llm_cost_usd"], float)
            csm = await client.get(f"/admin/tenants/{t['mine']}/details", headers=_as(s, roles, "csm"))
            assert csm.json()["brand_rules"]["system_prompt"] == "secret voice text"
        h = _as(s, roles, "csm")
        rpm = await client.patch(f"/admin/tenants/{t['mine']}", headers=h, json={"rate_limit_rpm": 42})
        assert rpm.status_code == 200, rpm.text
        assert await pool.fetchval("SELECT rate_limit_rpm FROM shared.tenants WHERE tenant_id = $1",
                                   uuid.UUID(t["mine"])) == 42
        plan = await client.patch(f"/admin/tenants/{t['mine']}", headers=h, json={"plan_tier": "growth"})
        assert plan.status_code == 403
        other = await client.patch(f"/admin/tenants/{t['other']}", headers=h, json={"rate_limit_rpm": 7})
        assert other.status_code == 403 and other.json()["detail"] == "Tenant outside your scope"
    finally:
        await pool.execute("DELETE FROM shared.tenant_brand_rules WHERE tenant_id = $1", uuid.UUID(t["mine"]))
