"""AA-979 P2b — Tenant 360 Content studio invitations (api/routers/admin_tenant360.py): the CIS row, the relay to the
CRM (crm.ops.v1 POST /tenants/{id}/studio-invitations with the staff member's own token), a failed delivery, send
again, staff policy, and that the e-mail is never stored or logged by CIS."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.core.staff_authz import denial_reason, principal_from_role
from api.routers import admin_tenant360 as t360
from api.services import crm_console
from services.tenant_membership import session as studio
from services.tenant_membership import writer

_SECRET = "test-admin-secret"
TENANT_ID = uuid.uuid4()
NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
EMAIL = "new.editor@agency.example"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _SECRET)


class _Tx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _member_row(**kw):
    row = {"membership_id": uuid.uuid4(), "cognito_iss": None, "cognito_sub": None, "content_studio_role": "editor",
           "status": "invited", "invitation_id": uuid.uuid4(), "created_at": NOW, "created_by": "admin:u1",
           "updated_at": NOW, "removed_at": None, "removed_by": None}
    row.update(kw)
    return row


def _conn(tenant_active=True, members=None):
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Tx())
    inserted = _member_row()

    async def fetchrow(sql, *args):
        if sql == studio.TENANT_ACTIVE_SQL:
            return {"is_active": tenant_active, "cancelled_at": None}
        if "FROM shared.tenants" in sql:
            return {"tenant_id": TENANT_ID, "name": "T", "plan_tier": "starter"}
        if sql == studio.INVITE_SQL:
            assert args[1] in ("owner", "editor", "viewer") and args[2].startswith("admin:")
            return inserted
        raise AssertionError(sql)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(return_value=members or [])
    conn.inserted = inserted
    return conn


def _request(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    request = MagicMock()
    request.app.state.pool = pool
    request.scope = {"state": {"staff_access_token": "staff.cognito.token"}}
    request.headers = {}
    return request


@pytest.fixture
def relay(monkeypatch):
    calls = []

    async def fake(method, path, authorization, *, json=None, params=None):
        calls.append((method, path, authorization, json))
        return relay.answer
    relay.answer = (202, {"invitation_id": "x", "status": "pending", "expires_at": "2026-11-02T00:00:00Z"})
    monkeypatch.setattr(crm_console, "crm_ops_call", fake)
    relay.calls = calls
    return relay


@pytest.mark.asyncio
async def test_invite_creates_the_row_then_relays_the_email_once(relay):
    conn = _conn()
    body = t360.MemberInvite(email=EMAIL, content_studio_role="editor", locale="vi")
    out = await t360.invite_tenant_member(TENANT_ID, body, _request(conn), _SECRET, "u1")
    assert out["member"]["status"] == "invited"
    assert out["delivery"] == {"status": "sent", "crm_status": 202, "expires_at": "2026-11-02T00:00:00Z"}
    (method, path, auth, payload), = relay.calls
    assert (method, path, auth) == ("POST", f"/tenants/{TENANT_ID}/studio-invitations", "Bearer staff.cognito.token")
    assert payload == {"email": EMAIL, "cis_invitation_id": str(conn.inserted["invitation_id"]), "locale": "vi"}
    # CIS stores no e-mail: neither the INSERT nor the audit row carries it.
    for c in conn.execute.call_args_list + conn.fetchrow.call_args_list:
        assert EMAIL not in json.dumps([str(a) for a in c.args])
    audit = [c.args for c in conn.execute.call_args_list if "acp_shared.audit_log" in c.args[0]]
    assert audit and audit[0][3] == "tenant.member_invited"


@pytest.mark.asyncio
async def test_failed_delivery_keeps_the_row_and_says_so(relay):
    relay.answer = (503, {"detail": "crm_unavailable", "message": "The partner CRM did not answer."})
    out = await t360.invite_tenant_member(TENANT_ID, t360.MemberInvite(email=EMAIL, content_studio_role="viewer"),
                                          _request(_conn()), _SECRET, "u1")
    assert out["member"]["status"] == "invited"
    assert out["delivery"]["status"] == "failed" and out["delivery"]["detail"] == "crm_unavailable"


@pytest.mark.asyncio
async def test_inactive_tenant_gets_no_invitation(relay):
    with pytest.raises(HTTPException) as exc:
        await t360.invite_tenant_member(TENANT_ID, t360.MemberInvite(email=EMAIL, content_studio_role="owner"),
                                        _request(_conn(tenant_active=False)), _SECRET, "u1")
    assert exc.value.status_code == 409
    assert relay.calls == []


@pytest.mark.asyncio
async def test_send_again_relays_the_same_invitation_id(relay):
    row = _member_row()
    conn = _conn(members=[row])
    out = await t360.resend_tenant_member_invitation(
        TENANT_ID, row["membership_id"], t360.MemberInviteResend(email=EMAIL), _request(conn), _SECRET)
    assert out["delivery"]["status"] == "sent"
    assert relay.calls[0][3]["cis_invitation_id"] == str(row["invitation_id"])


@pytest.mark.asyncio
async def test_send_again_refuses_an_active_member(relay):
    row = _member_row(status="active", cognito_iss="https://i", cognito_sub="s")
    with pytest.raises(HTTPException) as exc:
        await t360.resend_tenant_member_invitation(
            TENANT_ID, row["membership_id"], t360.MemberInviteResend(email=EMAIL), _request(_conn(members=[row])),
            _SECRET)
    assert exc.value.status_code == 409 and relay.calls == []


@pytest.mark.asyncio
async def test_wrong_secret_is_refused_before_anything(relay):
    with pytest.raises(HTTPException) as exc:
        await t360.invite_tenant_member(TENANT_ID, t360.MemberInvite(email=EMAIL, content_studio_role="editor"),
                                        _request(_conn()), "wrong", "u1")
    assert exc.value.status_code in (401, 403) and relay.calls == []


@pytest.mark.parametrize("payload", [
    {"email": "not-an-email", "content_studio_role": "editor"},
    {"email": EMAIL, "content_studio_role": "agent"},          # a CRM role: CIS never grants one
    {"email": EMAIL, "content_studio_role": "editor", "crm_role": "owner"},
    {"email": EMAIL, "content_studio_role": "editor", "locale": "fr"},
])
def test_body_rules(payload):
    with pytest.raises(ValidationError):
        t360.MemberInvite(**payload)


@pytest.mark.parametrize("path", ["/admin/tenants/{tenant_id}/members/invitations",
                                  "/admin/tenants/{tenant_id}/members/{membership_id}/invitation/send"])
def test_staff_policy_needs_tenant_write(path):
    assert denial_reason(principal_from_role("admin"), "POST", path) is None
    assert denial_reason(principal_from_role("viewer"), "POST", path) is not None
    assert denial_reason(principal_from_role("content"), "POST", path) is not None


def test_relay_allows_only_the_studio_invitation_path():
    tid = str(uuid.uuid4())
    assert crm_console.is_allowed("POST", f"/tenants/{tid}/studio-invitations")
    assert not crm_console.is_allowed("GET", f"/tenants/{tid}/studio-invitations")
    assert not crm_console.is_allowed("POST", f"/tenants/{tid}/studio-invitations/x")


def test_writer_staff_row_carries_the_invitation_id():
    row = _member_row()
    assert writer.staff_row(row)["invitation_id"] == str(row["invitation_id"])
