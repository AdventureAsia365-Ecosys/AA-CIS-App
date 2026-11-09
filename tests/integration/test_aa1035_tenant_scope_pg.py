"""AA-1035 (R2 slice 1, root ADR 0019 d4) — tenant scope against real Postgres, through the real app.

The isolation probe of d4 ("CSM A calling CSM B's tenant gets 403") on the CI database: an admin assigns staff to
invented tenants over the assignment routes (CIS 364), then a customer_success member reaches exactly its book, a
support member its book and the pool, the content role only the aa_internal tenant, and the Tenants directory lists
only what the caller may see. Calls carry the main admin secret + x-admin-role + x-admin-user-id (the machine / CI
path; a Cognito session carries the same identity from its verified token).
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

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = f"aa1035-it-{uuid.uuid4().hex}"   # a fresh value per run, never a fixed string
AA_INTERNAL = "00000000-0000-0000-0000-000000000001"
_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")


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
    # Another module may have reset the staff tables; the R2 batch is re-runnable.
    for name in ("362_admin_role_r2_staff_roles.sql", "363_staff_roles_reviewer_merge_finance_grant.sql",
                 "364_staff_tenant_assignment.sql"):
        await pool.execute(_sql(name))
    tag = uuid.uuid4().hex[:8]
    t = {k: str(uuid.uuid4()) for k in ("a", "b", "pool_small", "owned")}
    for key, tid in t.items():
        await pool.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, kind, segment) "
            "VALUES ($1, $2, $3, 'starter', 3, 'agency', $4)",
            uuid.UUID(tid), f"Quill Harbor {key}", f"aa1035-{tag}-{key.replace('_', '-')}",
            "tech_touch" if key == "pool_small" else "high_touch")
    s = {k: str(uuid.uuid4()) for k in ("admin", "csm", "csm_other", "support", "writer")}
    roles = {"admin": "admin", "csm": "customer_success", "csm_other": "customer_success", "support": "support",
             "writer": "content"}
    for key, sid in s.items():
        await pool.execute(
            "INSERT INTO shared.admin_users (id, username, role, is_active, email) "
            "VALUES ($1, $2, $3::shared.admin_role, true, $4)",
            uuid.UUID(sid), f"aa1035-{tag}-{key}", roles[key], f"aa1035-{tag}-{key.replace('_', '.')}@adventure.asia")
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, pool, t, s, roles
    staff_ids = [uuid.UUID(v) for v in s.values()]
    # the request logging of the staff calls (rate limiter usage rows) points at the staff rows
    await pool.execute("DELETE FROM shared.tenant_api_usage WHERE admin_user_id = ANY($1::uuid[])", staff_ids)
    await pool.execute("DELETE FROM shared.tenants WHERE slug LIKE $1", f"aa1035-{tag}-%")
    await pool.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", staff_ids)
    await pool.close()


def _as(s, roles, who):
    return {"X-Admin-Secret": SECRET, "x-admin-role": roles[who], "x-admin-user-id": s[who]}


async def _assign(client, s, roles, tenant, who, kind):
    return await client.put(f"/admin/tenants/{tenant}/assignments/{s[who]}", json={"kind": kind},
                            headers=_as(s, roles, "admin"))


@pytest.mark.asyncio
async def test_assignment_routes_one_owner_audit_and_idempotence(world):
    client, pool, t, s, roles = world
    res = await _assign(client, s, roles, t["a"], "csm", "owner")
    assert res.status_code == 200 and res.json()["changed"] is True, res.text
    again = await _assign(client, s, roles, t["a"], "csm", "owner")
    assert again.status_code == 200 and again.json()["changed"] is False
    second = await _assign(client, s, roles, t["a"], "csm_other", "owner")
    assert second.status_code == 409 and "already has an owner" in second.json()["detail"]
    backup = await _assign(client, s, roles, t["a"], "csm_other", "backup")
    assert backup.status_code == 200
    listed = await client.get(f"/admin/tenants/{t['a']}/assignments", headers=_as(s, roles, "csm"))
    assert listed.status_code == 200, listed.text
    assert [(a["admin_user_id"], a["kind"]) for a in listed.json()["assignments"]] == [
        (s["csm"], "owner"), (s["csm_other"], "backup")]
    actions = await pool.fetch("SELECT action FROM acp_shared.audit_log WHERE tenant_id = $1 "
                               "AND resource_type = 'staff_tenant_assignment' ORDER BY created_at", t["a"])
    assert [r["action"] for r in actions] == ["tenant.assignment_set", "tenant.assignment_set"]
    # Only tenant.assign (admin) changes assignments, even inside the caller's own book.
    refused = await client.put(f"/admin/tenants/{t['a']}/assignments/{s['csm']}", json={"kind": "backup"},
                               headers=_as(s, roles, "csm"))
    assert refused.status_code == 403
    gone = await client.delete(f"/admin/tenants/{t['a']}/assignments/{s['csm_other']}", headers=_as(s, roles, "admin"))
    assert gone.status_code == 200 and gone.json()["removed"] == "backup"
    missing = await client.delete(f"/admin/tenants/{t['a']}/assignments/{s['csm_other']}",
                                  headers=_as(s, roles, "admin"))
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_customer_success_reaches_its_book_only(world):
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["a"], "csm", "owner")).status_code == 200
    assert (await _assign(client, s, roles, t["owned"], "csm_other", "owner")).status_code == 200
    mine = await client.get(f"/admin/tenants/{t['a']}/members", headers=_as(s, roles, "csm"))
    assert mine.status_code == 200, mine.text
    for other in (t["b"], t["owned"], t["pool_small"], AA_INTERNAL):
        res = await client.get(f"/admin/tenants/{other}/members", headers=_as(s, roles, "csm"))
        assert res.status_code == 403 and res.json()["detail"] == "Tenant outside your scope", (other, res.text)
    # a tenant id that does not exist answers the same 403 (no probing)
    res = await client.get(f"/admin/tenants/{uuid.uuid4()}/members", headers=_as(s, roles, "csm"))
    assert res.status_code == 403
    # after the assignment goes, so does the access
    await client.delete(f"/admin/tenants/{t['a']}/assignments/{s['csm']}", headers=_as(s, roles, "admin"))
    res = await client.get(f"/admin/tenants/{t['a']}/members", headers=_as(s, roles, "csm"))
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_support_reaches_its_book_and_the_pool(world):
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["owned"], "csm", "owner")).status_code == 200
    assert (await _assign(client, s, roles, t["pool_small"], "csm_other", "owner")).status_code == 200
    ok = {t["b"]: "no owner", t["pool_small"]: "tech_touch segment"}
    for tenant, why in ok.items():
        res = await client.get(f"/admin/tenants/{tenant}/members", headers=_as(s, roles, "support"))
        assert res.status_code == 200, (why, res.text)
    res = await client.get(f"/admin/tenants/{t['owned']}/members", headers=_as(s, roles, "support"))
    assert res.status_code == 403
    assert (await _assign(client, s, roles, t["owned"], "support", "support")).status_code == 200
    res = await client.get(f"/admin/tenants/{t['owned']}/members", headers=_as(s, roles, "support"))
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_content_role_is_kept_on_the_aa_internal_tenant(world):
    client, pool, t, s, roles = world
    res = await client.get(f"/admin/tenants/{t['a']}/details", headers=_as(s, roles, "writer"))
    assert res.status_code == 403 and res.json()["detail"] == "Tenant outside your scope"
    res = await client.get(f"/admin/tenants/{t['a']}/members", headers=_as(s, roles, "writer"))
    assert res.status_code == 403 and res.json()["detail"].startswith("Role 'content' lacks")


@pytest.mark.asyncio
async def test_a_book_role_without_a_staff_id_is_refused(world):
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["a"], "csm", "owner")).status_code == 200
    res = await client.get(f"/admin/tenants/{t['a']}/members",
                           headers={"X-Admin-Secret": SECRET, "x-admin-role": "customer_success"})
    assert res.status_code == 403 and res.json()["detail"] == "Tenant scope needs a verified staff identity"


@pytest.mark.asyncio
async def test_support_never_sees_the_money_of_its_tenants(world):
    """S267 review: support (book + pool) reads the directory and the non-money Tenant 360 tabs only; the money
    columns are null for it, and plans, usage, cost, quality and the AI budget tile are refused."""
    client, pool, t, s, roles = world
    assert (await _assign(client, s, roles, t["a"], "csm", "owner")).status_code == 200
    assert (await _assign(client, s, roles, t["a"], "support", "support")).status_code == 200
    for path in (f"/admin/tenants/{t['a']}/usage", f"/admin/tenants/{t['a']}/cost",
                 f"/admin/tenants/{t['a']}/subscription", f"/admin/ai-gateway/tenant-budgets/{t['a']}"):
        res = await client.get(path, headers=_as(s, roles, "support"))
        assert res.status_code == 403, (path, res.text)
    assert (await client.get(f"/admin/tenants/{t['a']}/audit", headers=_as(s, roles, "support"))).status_code == 200
    # The directory and the details read shared.v_tenant_monthly_usage on its live columns, which the 007-shaped CI
    # view lacks until 223 (as test_aa797_tenant_admin); the column nulling is also unit-tested (test_aa798).
    has_live_view = await pool.fetchval(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'shared' "
        "AND table_name = 'v_tenant_monthly_usage' AND column_name = 'api_calls_used'")
    if not has_live_view:
        return
    rows = {r["tenant_id"]: r for r in
            (await client.get("/admin/tenants", headers=_as(s, roles, "support"))).json()["tenants"]}
    assert t["a"] in rows and t["b"] in rows          # its book and the pool
    for row in rows.values():
        assert row["plan"]["price_usd_monthly"] is None
        assert row["this_month"]["overage_usd"] is None and row["this_month"]["llm_cost_usd"] is None
    csm_rows = {r["tenant_id"]: r for r in
                (await client.get("/admin/tenants", headers=_as(s, roles, "csm"))).json()["tenants"]}
    assert set(csm_rows) == {t["a"]}
    assert isinstance(csm_rows[t["a"]]["this_month"]["llm_cost_usd"], float)
    details = await client.get(f"/admin/tenants/{t['a']}/details", headers=_as(s, roles, "support"))
    assert details.status_code == 200
    assert details.json()["summary"]["total_llm_cost_usd"] is None
    csm_details = await client.get(f"/admin/tenants/{t['a']}/details", headers=_as(s, roles, "csm"))
    assert isinstance(csm_details.json()["summary"]["total_llm_cost_usd"], float)
