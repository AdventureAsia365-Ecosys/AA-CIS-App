"""AA-978 — Tenant 360 Members routes (api/routers/admin_tenant360.py): staff policy, body rules, the writer's
outcomes mapped to HTTP, and the audit row. Mocked asyncpg (the test_aa766_tenant360.py shape); the SQL itself runs
in tests/integration/test_aa978_membership_service.py."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.core.staff_authz import denial_reason, principal_from_role
from api.routers import admin_tenant360 as t360
from services.acp_shared.audit_log import AdminTenantAuditAction
from services.tenant_membership import writer

_SECRET = "test-admin-secret"
TENANT_ID = uuid.uuid4()
MEMBER_ID = uuid.uuid4()
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
ROUTES = (("GET", "/admin/tenants/{tenant_id}/members"),
          ("PATCH", "/admin/tenants/{tenant_id}/members/{membership_id}"),
          ("POST", "/admin/tenants/{tenant_id}/members/{membership_id}/remove"))


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _SECRET)


class _TxnCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _member_row(**kw):
    row = {"membership_id": MEMBER_ID, "cognito_iss": "https://issuer.example/pool", "cognito_sub": "sub-1",
           "content_studio_role": "editor", "status": "active", "invitation_id": None, "created_at": NOW,
           "created_by": "seed:AA-978", "updated_at": NOW, "removed_at": None, "removed_by": None}
    row.update(kw)
    return row


def _conn(locked=None, updated=None):
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxnCM())
    tenant = {"tenant_id": TENANT_ID, "name": "T", "plan_tier": "starter"}

    async def fetchrow(sql, *args):
        if "FROM shared.tenants" in sql:
            return tenant
        if sql == writer.LOCK_SQL:
            return locked
        return updated
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    return conn


def _request(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    request = MagicMock()
    request.app.state.pool = pool
    return request


def _audit_rows(conn):
    out = []
    for c in conn.execute.call_args_list:
        if "acp_shared.audit_log" in c.args[0]:
            _sql, _tid, actor, action, _rtype, _rid, details = c.args
            out.append((action, actor, json.loads(details)))
    return out


@pytest.mark.parametrize("role", ["reviewer", "content", "viewer"])
def test_only_tenant_admins_reach_the_members_routes(role):
    for method, route in ROUTES:
        assert denial_reason(principal_from_role(role), method, route) is not None, (role, method)
    for method, route in ROUTES:
        assert denial_reason(principal_from_role("admin"), method, route) is None, method


def test_the_body_carries_the_content_studio_role_only():
    assert t360.MemberRoleChange(content_studio_role="owner").content_studio_role == "owner"
    for bad in ({"content_studio_role": "agent"}, {"content_studio_role": "owner", "crm_role": "owner"}, {}):
        with pytest.raises(ValidationError):
            t360.MemberRoleChange(**bad)


@pytest.mark.asyncio
async def test_role_change_audits_and_returns_a_fingerprint():
    conn = _conn(locked=_member_row(), updated=_member_row(content_studio_role="owner"))
    out = await t360.patch_tenant_member(TENANT_ID, MEMBER_ID, t360.MemberRoleChange(content_studio_role="owner"),
                                         _request(conn), x_admin_secret=_SECRET, x_admin_user_id="u-1")
    assert out["changed"] is True and out["member"]["content_studio_role"] == "owner"
    assert out["member"]["identity_fingerprint"] == writer.fingerprint("https://issuer.example/pool", "sub-1")
    assert "sub-1" not in json.dumps(out)
    assert _audit_rows(conn) == [(AdminTenantAuditAction.MEMBER_ROLE_CHANGED, "admin:u-1",
                                  {"before": {"content_studio_role": "editor"},
                                   "after": {"content_studio_role": "owner"}})]


@pytest.mark.asyncio
async def test_same_role_is_a_noop_without_audit():
    conn = _conn(locked=_member_row())
    out = await t360.patch_tenant_member(TENANT_ID, MEMBER_ID, t360.MemberRoleChange(content_studio_role="editor"),
                                         _request(conn), x_admin_secret=_SECRET, x_admin_user_id="u-1")
    assert out["changed"] is False and _audit_rows(conn) == []


@pytest.mark.asyncio
async def test_remove_audits_and_a_second_remove_is_a_noop():
    conn = _conn(locked=_member_row(), updated=_member_row(status="removed", removed_at=NOW, removed_by="admin:u-1"))
    out = await t360.remove_tenant_member(TENANT_ID, MEMBER_ID, _request(conn), x_admin_secret=_SECRET,
                                          x_admin_user_id="u-1")
    assert out["changed"] is True and out["member"]["status"] == "removed"
    assert [a[0] for a in _audit_rows(conn)] == [AdminTenantAuditAction.MEMBER_REMOVED]
    conn = _conn(locked=_member_row(status="removed", removed_at=NOW, removed_by="admin:u-1"))
    out = await t360.remove_tenant_member(TENANT_ID, MEMBER_ID, _request(conn), x_admin_secret=_SECRET,
                                          x_admin_user_id="u-1")
    assert out["changed"] is False and _audit_rows(conn) == []


@pytest.mark.asyncio
async def test_unknown_member_is_404_and_a_removed_member_is_409():
    with pytest.raises(HTTPException) as e:
        await t360.remove_tenant_member(TENANT_ID, MEMBER_ID, _request(_conn(locked=None)), x_admin_secret=_SECRET,
                                        x_admin_user_id="u-1")
    assert e.value.status_code == 404
    conn = _conn(locked=_member_row(status="removed", removed_at=NOW, removed_by="admin:u-1"))
    with pytest.raises(HTTPException) as e:
        await t360.patch_tenant_member(TENANT_ID, MEMBER_ID, t360.MemberRoleChange(content_studio_role="owner"),
                                       _request(conn), x_admin_secret=_SECRET, x_admin_user_id="u-1")
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_before_migration_276_the_routes_answer_503():
    conn = _conn()
    conn.fetch = AsyncMock(side_effect=asyncpg.UndefinedTableError("relation does not exist"))
    with pytest.raises(HTTPException) as e:
        await t360.get_tenant_members(TENANT_ID, _request(conn), x_admin_secret=_SECRET)
    assert e.value.status_code == 503


@pytest.mark.asyncio
async def test_list_has_no_raw_identity():
    conn = _conn()
    conn.fetch = AsyncMock(return_value=[_member_row(), _member_row(status="invited", cognito_iss=None,
                                                                    cognito_sub=None, invitation_id=uuid.uuid4())])
    out = await t360.get_tenant_members(TENANT_ID, _request(conn), x_admin_secret=_SECRET)
    assert [m["identity_fingerprint"] is None for m in out["members"]] == [False, True]
    assert "sub-1" not in json.dumps(out) and "issuer.example" not in json.dumps(out)
    assert "CRM" in out["note"]
