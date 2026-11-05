"""AA-1035 R2 close-out (S269) against real Postgres:

- tenant.membership.v1 1.3 getStaffTenantScope reads (api/core/tenant_scope.desk_scope_of_identity): a staff row by
  WORKFORCE (iss, sub), its assignments (CIS 364) and the pool rule; an inactive or unknown identity reaches nothing;
- a Tenant QA piece read of customer_success looks the piece's tenant up first (an unknown piece is a 404). The
  list SQL is not run here: the CI schema of acp_shared.content_piece lacks columns the live table has (channel,
  angle_gate_option_id), so the lists are covered by the unit tests' SQL assertions.

Invented names only (fixture text guard).
"""
import os
import sys
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = f"aa1035-r2c-it-{uuid.uuid4().hex}"   # a fresh value per run, never a fixed string
WORKFORCE = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_zz1035itpool"
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
    for name in ("362_admin_role_r2_staff_roles.sql", "363_staff_roles_reviewer_merge_finance_grant.sql",
                 "364_staff_tenant_assignment.sql"):
        await pool.execute(_sql(name))
    tag = uuid.uuid4().hex[:8]
    t = {k: str(uuid.uuid4()) for k in ("mine", "other", "pooled")}
    for key, tid in t.items():
        await pool.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, kind, segment) "
            "VALUES ($1, $2, $3, 'starter', 3, 'agency', $4)",
            uuid.UUID(tid), f"Lantern Bay {key}", f"aa1035r2c-{tag}-{key}",
            "tech_touch" if key == "pooled" else "high_touch")
    s = {k: str(uuid.uuid4()) for k in ("csm", "owner_of_other", "gone")}
    for key, sid in s.items():
        await pool.execute(
            "INSERT INTO shared.admin_users (id, username, role, is_active, email, cognito_iss, cognito_sub) "
            "VALUES ($1, $2, 'customer_success'::shared.admin_role, $3, $4, $5, $6)",
            uuid.UUID(sid), f"aa1035r2c-{tag}-{key}", key != "gone", f"aa1035r2c-{tag}-{key}@adventure.asia",
            WORKFORCE, f"sub-{tag}-{key}")
    await pool.execute("INSERT INTO shared.staff_tenant_assignment (admin_user_id, tenant_id, kind, created_by) "
                       "VALUES ($1, $2, 'owner', 'test'), ($3, $4, 'owner', 'test'), ($5, $2, 'backup', 'test')",
                       uuid.UUID(s["csm"]), uuid.UUID(t["mine"]), uuid.UUID(s["owner_of_other"]),
                       uuid.UUID(t["other"]), uuid.UUID(s["gone"]))
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, pool, t, s, tag
    staff_ids = [uuid.UUID(v) for v in s.values()]
    await pool.execute("DELETE FROM shared.tenant_api_usage WHERE admin_user_id = ANY($1::uuid[])", staff_ids)
    await pool.execute("DELETE FROM shared.tenants WHERE slug LIKE $1", f"aa1035r2c-{tag}-%")
    await pool.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", staff_ids)
    await pool.close()


@pytest.mark.asyncio
async def test_the_desk_scope_of_an_identity_on_real_tables(world):
    from api.core import tenant_scope

    _, pool, t, _, tag = world
    async with pool.acquire() as conn:
        scope, ids = await tenant_scope.desk_scope_of_identity(conn, WORKFORCE, f"sub-{tag}-csm", "customer_success")
        assert (scope, ids) == ("book", [t["mine"]])
        scope, ids = await tenant_scope.desk_scope_of_identity(conn, WORKFORCE, f"sub-{tag}-csm", "support")
        assert scope == "book_pool" and t["mine"] in ids and t["pooled"] in ids and t["other"] not in ids
        # an inactive staff row and an unknown identity reach nothing
        assert await tenant_scope.desk_scope_of_identity(conn, WORKFORCE, f"sub-{tag}-gone", "support") == ("none", [])
        assert await tenant_scope.desk_scope_of_identity(conn, WORKFORCE, "nobody", "customer_success") == ("none", [])


def _as(role, staff_id):
    return {"X-Admin-Secret": SECRET, "x-admin-role": role, "x-admin-user-id": staff_id}


@pytest.mark.asyncio
async def test_an_unknown_piece_is_a_404_for_the_book_role(world):
    client, _, _, s, _ = world
    res = await client.get(f"/admin/content-quality/pieces/{uuid.uuid4()}", headers=_as("customer_success", s["csm"]))
    assert res.status_code == 404
