"""AA-979 phase 2 automation (S252, root ADR 0006a A10b-1) — the Dev-only E2E CIS admin may call the two Content studio
invitation routes, on synthetic tenants only.

Covers api/core/e2e_staff.py (the two routes on the allow-list, the synthetic-tenant SQL, the E2E mailbox address
check) and the real app end to end through the bearer middleware, staff_policy and the routes of
api/routers/admin_tenant360.py: allowed on a synthetic tenant (row + audit + one relay, unchanged), refused 403
``e2e_read_only`` on a real tenant (is_canary FALSE or NULL), for an address outside the E2E mailbox domain and for the
``owner`` role, before any row, audit or relay; every other tenant write stays refused even on a synthetic tenant; a
normal admin is unaffected. No network, no AWS, no DB.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
import structlog.testing
from fastapi.testclient import TestClient

from api.core import e2e_staff, staff_identity
from api.main import app as real_app
from api.routers import admin, admin_settings
from api.services import crm_console
from services.tenant_membership import session as studio
from services.tenant_membership import writer
from tests.unit.aa854_cognito_staff import ISS, Directory
from tests.unit.test_aa816_e2e_cis_admin import (  # noqa: F401 — issuer is a fixture
    SECRET, _admin_e2e_row, _admin_e2e_token, _set_env, issuer,
)

SYNTHETIC = "3f6d2c1a-8b7e-4c5d-9a0b-1c2d3e4f5a6b"
REAL = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
NULL_FLAG = "5b4a3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d"
INVITEE = "e2e-test-studio-viewer@e2e-mail.lumiguides.it.com"
NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


# ── api/core/e2e_staff.py ────────────────────────────────────────────────────────────────────────

def test_the_invitation_routes_are_on_the_allow_list():
    assert e2e_staff.E2E_STUDIO_INVITATION_WRITES == {
        ("POST", "/admin/tenants/{tenant_id}/members/invitations"),
        ("POST", "/admin/tenants/{tenant_id}/members/{membership_id}/invitation/send"),
    }
    assert e2e_staff.E2E_STUDIO_INVITATION_WRITES <= e2e_staff.E2E_ALLOWED_WRITES
    assert e2e_staff.E2E_INVITE_ROLES == {"viewer", "editor"}
    assert e2e_staff.E2E_MAIL_DOMAIN == "e2e-mail.lumiguides.it.com"


def test_synthetic_sql_needs_an_explicit_true():
    # is_canary is nullable on Dev (migration 281): NULL must not count as synthetic.
    assert e2e_staff.SYNTHETIC_TENANT_SQL == "SELECT is_canary IS TRUE FROM shared.tenants WHERE tenant_id = $1"


class _Val:
    def __init__(self, value: Any) -> None:
        self.value, self.calls = value, []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return self.value


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), (None, False), (1, False), ("t", False)])
@pytest.mark.asyncio
async def test_is_synthetic_tenant(value, expected):
    conn = _Val(value)
    assert await e2e_staff.is_synthetic_tenant(conn, SYNTHETIC) is expected
    assert conn.calls == [(e2e_staff.SYNTHETIC_TENANT_SQL, (SYNTHETIC,))]


@pytest.mark.parametrize("email", [INVITEE, "e2e-test-studio-editor@e2e-mail.lumiguides.it.com",
                                   " E2E-Test-Studio-Viewer@E2E-Mail.Lumiguides.IT.com "])
def test_e2e_mailbox_addresses(email):
    assert e2e_staff.is_e2e_mailbox_address(email)


@pytest.mark.parametrize("email", [
    "someone@agency.example", "e2e@lumiguides.it.com", "x@sub.e2e-mail.lumiguides.it.com",
    "x@e2e-mail.lumiguides.it.com.", "x@e2e-mail.lumiguides.it.com.evil.example", "@e2e-mail.lumiguides.it.com",
    "x@y@e2e-mail.lumiguides.it.com", "e2e-mail.lumiguides.it.com", "x@e2e-mail.lumiguides.it.Kom", "", None, 5,
])
def test_other_addresses_are_refused(email):
    assert not e2e_staff.is_e2e_mailbox_address(email)


@pytest.mark.parametrize("method,path", [
    ("POST", f"/admin/tenants/{SYNTHETIC}/members/invitations"),
    ("POST", f"/admin/tenants/{SYNTHETIC}/members/{REAL}/invitation/send"),
])
def test_invitation_paths_are_allowed_writes(method, path):
    assert e2e_staff.is_allowed_e2e_write_path(method, path)


@pytest.mark.parametrize("method,path", [
    ("POST", f"/admin/tenants/{SYNTHETIC}/members/invitations/"),
    ("PUT", f"/admin/tenants/{SYNTHETIC}/members/invitations"),
    ("POST", "/admin/tenants/not-a-uuid/members/invitations"),
    ("POST", f"/admin/tenants/{SYNTHETIC}/members/{REAL}/invitation/send/x"),
    ("POST", f"/admin/tenants/{SYNTHETIC}/members/{REAL}/remove"),
    ("PATCH", f"/admin/tenants/{SYNTHETIC}/members/{REAL}"),
    ("POST", "/admin/tenants"),
    ("PATCH", f"/admin/tenants/{SYNTHETIC}"),
    ("POST", f"/admin/tenants/{SYNTHETIC}/generate-key"),
    ("POST", f"/admin/tenants/{SYNTHETIC}/offboard"),
    ("PUT", f"/admin/tenants/{SYNTHETIC}/config"),
])
def test_other_tenant_writes_are_not_allowed_writes(method, path):
    assert not e2e_staff.is_allowed_e2e_write_path(method, path)


# ── Real app ─────────────────────────────────────────────────────────────────────────────────────

class TenantDirectory(Directory):
    """The staff directory for the middleware, plus shared.tenants (three tenants: synthetic, real, flag NULL) and the
    membership writes the invitation makes (recorded, never executed)."""

    FLAGS = {SYNTHETIC: True, REAL: False, NULL_FLAG: None}

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, tuple]] = []
        self.request_audit: list[tuple] = []
        self.synthetic_checks = 0
        self.invited = {"membership_id": uuid.uuid4(), "cognito_iss": None, "cognito_sub": None,
                        "content_studio_role": "viewer", "status": "invited", "invitation_id": uuid.uuid4(),
                        "created_at": NOW, "created_by": "admin:x", "updated_at": NOW, "removed_at": None,
                        "removed_by": None}

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        if sql in (staff_identity.SQL_BY_IDENTITY, staff_identity.SQL_FIRST_LINK, staff_identity.SQL_BY_EMAIL):
            return await super().fetchrow(sql, *args)
        tid = str(args[0]) if args else None
        if sql == studio.TENANT_ACTIVE_SQL:
            return {"is_active": True, "cancelled_at": None} if tid in self.FLAGS else None
        if sql == studio.INVITE_SQL:
            self.writes.append(("invite", args))
            return dict(self.invited, content_studio_role=args[1], created_by=args[2])
        if "FROM shared.tenants" in sql:   # _require_tenant
            return {"tenant_id": args[0], "name": "T", "plan_tier": "starter"} if tid in self.FLAGS else None
        raise AssertionError(sql)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        assert sql == e2e_staff.SYNTHETIC_TENANT_SQL, sql
        self.synthetic_checks += 1
        return self.FLAGS.get(str(args[0]))   # TRUE / FALSE / NULL; an unknown tenant has no row (None)

    async def execute(self, sql: str, *args: Any) -> str:
        if "shared.tenant_api_usage" in sql:   # the usage middleware's request counter, not a tenant write
            return "INSERT 0 1"
        if "acp_shared.audit_log" in sql and "staff.request" in args:
            # AA-884 staff_audit: one row per staff write that passed the bearer cap (allowed or refused), unchanged.
            self.request_audit.append(args)
            return "INSERT 0 1"
        self.writes.append(("execute", (sql, *args)))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list:
        raise AssertionError(sql)


@pytest.fixture
def real(monkeypatch, issuer):  # noqa: F811
    _set_env(monkeypatch, "dev")
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    directory = TenantDirectory()
    admin_id = _admin_e2e_row(directory)
    monkeypatch.setattr(real_app.state, "pool", directory, raising=False)
    relays: list[tuple] = []

    async def fake_relay(method, path, authorization, *, json=None, params=None):
        relays.append((method, path, authorization, json))
        return 202, {"invitation_id": "x", "status": "pending", "expires_at": "2026-11-02T00:00:00Z"}

    monkeypatch.setattr(crm_console, "crm_ops_call", fake_relay)

    async def fake_list(conn, tenant_id):
        return [{"membership_id": str(directory.invited["membership_id"]), "status": "invited",
                 "invitation_id": str(directory.invited["invitation_id"])}]

    monkeypatch.setattr(writer, "list_members", fake_list)
    return TestClient(real_app), directory, relays, admin_id


def _headers(issuer) -> dict:  # noqa: F811
    return {"Authorization": f"Bearer {_admin_e2e_token(issuer)}"}


def _invite(client, issuer, tenant: str, email: str = INVITEE, role: str = "viewer"):  # noqa: F811
    return client.post(f"/admin/tenants/{tenant}/members/invitations",
                       json={"email": email, "content_studio_role": role, "locale": "en"}, headers=_headers(issuer))


def _resend(client, issuer, tenant: str, directory: TenantDirectory, email: str = INVITEE):  # noqa: F811
    return client.post(f"/admin/tenants/{tenant}/members/{directory.invited['membership_id']}/invitation/send",
                       json={"email": email, "locale": "en"}, headers=_headers(issuer))


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_invite_on_a_synthetic_tenant_is_allowed_and_audited(real, issuer, role):  # noqa: F811
    client, directory, relays, admin_id = real
    res = _invite(client, issuer, SYNTHETIC, role=role)
    assert res.status_code == 201, res.text
    assert res.json()["delivery"]["status"] == "sent" and res.json()["member"]["status"] == "invited"
    assert directory.synthetic_checks == 1
    # The row and the audit row are the normal ones (audit unchanged), with the A10b admin as the actor.
    (_, invite_args), = [w for w in directory.writes if w[0] == "invite"]
    assert invite_args[1] == role and invite_args[2] == f"admin:{admin_id}"
    audits = [w[1] for w in directory.writes if w[0] == "execute" and "acp_shared.audit_log" in w[1][0]]
    assert len(audits) == 1 and "tenant.member_invited" in audits[0] and f"admin:{admin_id}" in audits[0]
    # One relay, with the A10b session's own token (the CRM accepts any cis-admin OPS caller).
    (method, path, auth, payload), = relays
    assert (method, path) == ("POST", f"/tenants/{SYNTHETIC}/studio-invitations") and auth.startswith("Bearer ")
    assert payload["email"] == INVITEE
    (row,), = [directory.request_audit]
    assert f"admin:{admin_id}" in row and 201 in row


@pytest.mark.parametrize("tenant", [REAL, NULL_FLAG])
def test_invite_on_a_real_tenant_is_refused_before_any_write(real, issuer, tenant):  # noqa: F811
    client, directory, relays, _ = real
    with structlog.testing.capture_logs() as logs:
        res = _invite(client, issuer, tenant)
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert directory.writes == [] and relays == []
    assert any(e.get("event") == "tenant_member_invitation_e2e_refused" and e.get("reason") == "tenant_not_synthetic"
               for e in logs)
    # The refused call still leaves its AA-884 staff.request audit row, with the 403.
    (row,), = [directory.request_audit]
    assert 403 in row


def test_invite_of_an_unknown_tenant_is_404(real, issuer):  # noqa: F811
    client, directory, relays, _ = real
    res = _invite(client, issuer, str(uuid.uuid4()))
    assert res.status_code == 404 and directory.writes == [] and relays == []


@pytest.mark.parametrize("email,role,reason", [
    ("someone@agency.example", "viewer", "address"),
    ("x@sub.e2e-mail.lumiguides.it.com", "editor", "address"),
    (INVITEE, "owner", "role"),
])
def test_invite_outside_the_e2e_scope_is_refused_on_synthetic_too(real, issuer, email, role, reason):  # noqa: F811
    client, directory, relays, _ = real
    with structlog.testing.capture_logs() as logs:
        res = _invite(client, issuer, SYNTHETIC, email=email, role=role)
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert directory.writes == [] and relays == []
    assert any(e.get("event") == "tenant_member_invitation_e2e_refused" and e.get("reason") == reason for e in logs)
    # The address is never logged.
    assert all(email not in str(e) for e in logs)


def test_send_again_on_a_synthetic_tenant_is_allowed(real, issuer):  # noqa: F811
    client, directory, relays, _ = real
    res = _resend(client, issuer, SYNTHETIC, directory)
    assert res.status_code == 200, res.text
    assert res.json()["delivery"]["status"] == "sent" and len(relays) == 1
    assert relays[0][3]["cis_invitation_id"] == str(directory.invited["invitation_id"])


@pytest.mark.parametrize("tenant", [REAL, NULL_FLAG])
def test_send_again_on_a_real_tenant_is_refused(real, issuer, tenant):  # noqa: F811
    client, directory, relays, _ = real
    res = _resend(client, issuer, tenant, directory)
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert relays == []


def test_send_again_to_another_address_is_refused(real, issuer):  # noqa: F811
    client, directory, relays, _ = real
    res = _resend(client, issuer, SYNTHETIC, directory, email="someone@agency.example")
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"}) and relays == []


@pytest.mark.parametrize("method,path,body", [
    ("POST", f"/admin/tenants/{SYNTHETIC}/members/{REAL}/remove", None),
    ("PATCH", f"/admin/tenants/{SYNTHETIC}/members/{REAL}", {"content_studio_role": "owner"}),
    ("PATCH", f"/admin/tenants/{SYNTHETIC}", {"is_active": False}),
    ("POST", f"/admin/tenants/{SYNTHETIC}/generate-key", {}),
    ("POST", f"/admin/tenants/{SYNTHETIC}/offboard", {}),
    ("PUT", f"/admin/tenants/{SYNTHETIC}/config", {}),
    ("POST", "/admin/tenants", {"name": "x"}),
])
def test_every_other_tenant_write_stays_refused_on_a_synthetic_tenant(real, issuer, method, path, body):  # noqa: F811
    client, directory, relays, _ = real
    res = client.request(method, path, json=body, headers=_headers(issuer))
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert directory.writes == [] and relays == [] and directory.synthetic_checks == 0


def test_a_normal_admin_invites_on_a_real_tenant_as_before(real, issuer):  # noqa: F811
    client, directory, relays, _ = real
    directory.add(role="admin", email="staff@adventure.asia", iss=ISS, sub="another-sub")
    token = issuer.token(sub="another-sub", email="staff@adventure.asia", **{"cognito:groups": ["cis-admin"]})
    res = client.post(f"/admin/tenants/{REAL}/members/invitations", headers={"Authorization": f"Bearer {token}"},
                      json={"email": "new.owner@agency.example", "content_studio_role": "owner", "locale": "en"})
    assert res.status_code == 201, res.text
    assert directory.synthetic_checks == 0 and len(relays) == 1
