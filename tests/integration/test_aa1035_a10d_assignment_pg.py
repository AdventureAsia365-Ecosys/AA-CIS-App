"""AA-1035 A10d (root ADR 0006a A10d) — the Dev-only E2E customer_success row joins synthetic tenants only, real Postgres.

Through the real app with the admin header path (main secret + x-admin-role admin): PUT
/admin/tenants/{id}/assignments/{staff} answers 409 for the A10d row on a tenant that is not synthetic
(``shared.tenants.is_canary`` not TRUE) and writes no shared.staff_tenant_assignment row; on a synthetic tenant it
assigns as usual. A company customer_success row is unaffected. Invented tenants and staff only.
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
SECRET = f"aa1035-a10d-it-{uuid.uuid4().hex}"   # a fresh value per run, never a fixed string
CS_E2E = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
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
    # Another module may have reset the staff tables or the CHECK; the R2 batch and 378 are re-runnable.
    await pool.execute("DELETE FROM shared.admin_users WHERE role = 'reviewer'")
    for name in ("362_admin_role_r2_staff_roles.sql", "363_staff_roles_reviewer_merge_finance_grant.sql",
                 "364_staff_tenant_assignment.sql", "375_admin_users_reviewer_alias_removed.sql",
                 "378_admin_users_e2e_cis_cs_email.sql"):
        await pool.execute(_sql(name))
    await pool.execute("DELETE FROM shared.admin_users WHERE email = $1", CS_E2E)
    tag = uuid.uuid4().hex[:8]
    t = {"synthetic": str(uuid.uuid4()), "real": str(uuid.uuid4())}
    for key, tid in t.items():
        await pool.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, kind, segment, is_canary) "
            "VALUES ($1, $2, $3, 'starter', 3, 'agency', 'high_touch', $4)",
            uuid.UUID(tid), f"Lantern Cove {key}", f"aa1035d-{tag}-{key}", key == "synthetic")
    s = {"admin": str(uuid.uuid4()), "e2e_cs": str(uuid.uuid4()), "company_cs": str(uuid.uuid4())}
    rows = {"admin": ("admin", f"aa1035d-{tag}-admin@adventure.asia"), "e2e_cs": ("customer_success", CS_E2E),
            "company_cs": ("customer_success", f"aa1035d-{tag}-cs@adventure.asia")}
    for key, sid in s.items():
        role, email = rows[key]
        await pool.execute(
            "INSERT INTO shared.admin_users (id, username, role, is_active, email) "
            "VALUES ($1, $2, $3::shared.admin_role, true, $4)", uuid.UUID(sid), f"aa1035d-{tag}-{key}", role, email)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, pool, t, s
    staff_ids = [uuid.UUID(v) for v in s.values()]
    await pool.execute("DELETE FROM shared.staff_tenant_assignment WHERE admin_user_id = ANY($1::uuid[])", staff_ids)
    await pool.execute("DELETE FROM shared.tenant_api_usage WHERE admin_user_id = ANY($1::uuid[])", staff_ids)
    await pool.execute("DELETE FROM shared.tenants WHERE slug LIKE $1", f"aa1035d-{tag}-%")
    await pool.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", staff_ids)
    await pool.close()


def _admin(s):
    return {"X-Admin-Secret": SECRET, "x-admin-role": "admin", "x-admin-user-id": s["admin"]}


async def _rows(pool, staff_id):
    rows = await pool.fetch("SELECT tenant_id::text AS tenant_id, kind FROM shared.staff_tenant_assignment "
                            "WHERE admin_user_id = $1", uuid.UUID(staff_id))
    return sorted((r["tenant_id"], r["kind"]) for r in rows)


@pytest.mark.asyncio
async def test_the_e2e_cs_row_joins_synthetic_tenants_only(world):
    client, pool, t, s = world
    refused = await client.put(f"/admin/tenants/{t['real']}/assignments/{s['e2e_cs']}", json={"kind": "backup"},
                               headers=_admin(s))
    assert refused.status_code == 409, refused.text
    assert "synthetic tenants only" in refused.json()["detail"]
    assert await _rows(pool, s["e2e_cs"]) == []

    ok = await client.put(f"/admin/tenants/{t['synthetic']}/assignments/{s['e2e_cs']}", json={"kind": "backup"},
                          headers=_admin(s))
    assert ok.status_code == 200, ok.text
    assert await _rows(pool, s["e2e_cs"]) == [(t["synthetic"], "backup")]


@pytest.mark.asyncio
async def test_a_company_cs_row_is_unaffected(world):
    client, pool, t, s = world
    res = await client.put(f"/admin/tenants/{t['real']}/assignments/{s['company_cs']}", json={"kind": "backup"},
                           headers=_admin(s))
    assert res.status_code == 200, res.text
    assert await _rows(pool, s["company_cs"]) == [(t["real"], "backup")]
