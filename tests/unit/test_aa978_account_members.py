"""AA-978 follow-up — GET /v1/account/members, the tenant portal's Account › Members page.

- the tenant id comes only from the verified token (bound in the one query); a malformed sub is 403
- removed rows are left out by the SQL; active rows first, then invitations
- a row shows the fingerprint, never the raw (iss, sub), and never a staff actor, invitation id or CRM role
- a staff token is refused by get_tenant (403); no token is 401
- 503 while CIS 276 is not applied
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.routers import v1_account_members as mod
from services.tenant_membership import portal
from services.tenant_membership.writer import fingerprint

TENANT = str(uuid.uuid4())
T0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
ISS = "https://issuer.example.test/pool"


def _row(status="active", role="editor", sub="sub-1"):
    return {
        "membership_id": uuid.uuid4(), "cognito_iss": ISS if status == "active" else None,
        "cognito_sub": sub if status == "active" else None, "content_studio_role": role, "status": status,
        "created_at": T0, "updated_at": T0,
        # columns the tenant query does not select; a row that carried them must still not leak them
        "created_by": "staff:abc", "removed_by": None, "invitation_id": uuid.uuid4(),
    }


def _request(rows=None, exc=None):
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=rows or [])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = False
    request = MagicMock()
    request.app.state.pool = pool
    return request, conn


@pytest.mark.asyncio
async def test_tenant_id_comes_from_the_token_only():
    request, conn = _request([])
    await mod.list_members(request, tenant={"sub": TENANT, "role": "tenant"})
    sql, bound = conn.fetch.await_args.args
    assert bound == TENANT
    assert "WHERE tenant_id = $1::uuid AND status <> 'removed'" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", [None, "", "not-a-uuid", 42])
async def test_a_malformed_sub_is_403(sub):
    request, conn = _request([])
    with pytest.raises(HTTPException) as exc:
        await mod.list_members(request, tenant={"sub": sub, "role": "tenant"})
    assert exc.value.status_code == 403
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_rows_show_a_fingerprint_role_status_and_dates_only():
    active, invited = _row(), _row(status="invited", role="viewer")
    request, _ = _request([active, invited])
    body = await mod.list_members(request, tenant={"sub": TENANT, "role": "tenant"})
    assert (body["total"], body["active"], body["invited"]) == (2, 1, 1)
    first, second = body["data"]
    assert set(first) == {"membership_id", "member_ref", "role", "status", "added_at", "updated_at"}
    assert first["member_ref"] == fingerprint(ISS, "sub-1") and len(first["member_ref"]) == 12
    assert second["member_ref"] is None and second["status"] == "invited"
    flat = repr(body)
    assert "sub-1" not in flat and ISS not in flat and "staff:abc" not in flat
    mod.AccountMembers.model_validate(body)  # the response model accepts the shape


def test_the_query_orders_active_first_and_selects_no_actor_columns():
    sql = " ".join(portal.TENANT_MEMBERS_SQL.split())
    assert sql.index("ORDER BY (status = 'invited')") > 0
    for col in ("created_by", "removed_by", "invitation_id", "removed_at"):
        assert col not in sql


@pytest.mark.asyncio
async def test_503_while_migration_276_is_missing():
    request, _ = _request(exc=asyncpg.UndefinedTableError("relation does not exist"))
    with pytest.raises(HTTPException) as exc:
        await mod.list_members(request, tenant={"sub": TENANT, "role": "tenant"})
    assert exc.value.status_code == 503


def test_the_route_needs_a_tenant_token(monkeypatch):
    from fastapi import FastAPI

    from api.routers import auth

    app = FastAPI()
    app.include_router(mod.router)
    request, _ = _request([])
    app.state.pool = request.app.state.pool
    client = TestClient(app)
    assert client.get("/v1/account/members").status_code in (401, 403)  # no bearer
    monkeypatch.setattr(auth, "verify_jwt", lambda _t: {"sub": TENANT, "role": "admin"})
    staff = client.get("/v1/account/members", headers={"Authorization": "Bearer staff"})
    assert staff.status_code == 403  # a valid staff token is not a tenant
    monkeypatch.setattr(auth, "verify_jwt", lambda _t: {"sub": TENANT, "role": "tenant"})
    ok = client.get("/v1/account/members", headers={"Authorization": "Bearer tenant"})
    assert ok.status_code == 200 and ok.json() == {"data": [], "total": 0, "active": 0, "invited": 0}
