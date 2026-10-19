"""AA-741 (S229) — POST /admin/tenants/{id}/offboard against real Postgres.

It never worked: the audit_log INSERT bound $1 to both tenant_id and resource_id, and Postgres
raised AmbiguousParameterError ("inconsistent types deduced for parameter $1") — invisible with a
mocked connection. Also asserts the offboarded tenant leaves the Jev allow-list.
"""
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from _audit_cleanup import delete_audit_rows  # AA-884: audit_log is append-only (CIS 248)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = str(uuid.uuid4())


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    slug = f"t-{TENANT[:8]}"
    await p.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                                    posts_per_week, api_key_hash)
        VALUES ($1::uuid, 'Offboard Test', $2, 'starter', 60, true, 3, 'h')
        ON CONFLICT DO NOTHING""", TENANT, slug)
    await p.execute("""
        INSERT INTO shared.jev_tenant_allowlist (tenant_id, reason, added_by)
        VALUES ($1::uuid, 'test', 'test') ON CONFLICT DO NOTHING""", TENANT)
    yield p
    await delete_audit_rows(p, "tenant_id = $1", TENANT)
    await p.execute("DELETE FROM shared.jev_tenant_allowlist WHERE tenant_id = $1::uuid", TENANT)
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1::uuid", TENANT)
    await p.close()


@pytest.mark.asyncio
async def test_offboard_revokes_key_writes_audit_and_leaves_allowlist(pool, monkeypatch):
    from api.routers import admin
    monkeypatch.setattr(admin, "verify_admin_secret", lambda _s: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    out = await admin.offboard_tenant(TENANT, admin.OffboardRequest(reason="test"), request, "s",
                                      x_admin_role="admin")  # AA-797: tenant writes are admin-only
    assert out["status"] == "offboarded"

    t = await pool.fetchrow("SELECT is_active, api_key_hash, cancelled_at FROM shared.tenants "
                            "WHERE tenant_id = $1::uuid", TENANT)
    assert t["is_active"] is False and t["api_key_hash"].startswith("REVOKED_") and t["cancelled_at"]
    a = await pool.fetchrow("SELECT action, resource_id FROM acp_shared.audit_log "
                            "WHERE tenant_id = $1 AND action = 'agency.offboard'", TENANT)
    assert a and a["resource_id"] == TENANT
    assert await pool.fetchval("SELECT count(*) FROM shared.jev_tenant_allowlist "
                               "WHERE tenant_id = $1::uuid", TENANT) == 0
