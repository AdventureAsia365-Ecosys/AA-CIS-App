"""AA-1031 (S264 live check F3) — GET /admin/tenants/{id}/members/access: which member write controls the caller may use
on one tenant. The Members tab of Tenant 360 and the agency drawer's "also invite to the Content studio" box hide a
control the API would refuse; each answer is staff_authz.denial_reason on the control's own write route, plus the
invitation routes' own refusal of the Dev-only E2E CIS admin (synthetic tenant only, viewer / editor only).

Pure checks of api/routers/admin_tenant360.members_access, and the real app end to end (bearer middleware,
staff_policy, the route) with the AA-979 A10b harness. No network, no AWS, no DB.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from api.core import e2e_staff, staff_authz
from api.routers import admin_tenant360 as router_mod
from tests.unit.aa854_cognito_staff import ISS
from tests.unit.test_aa816_e2e_cis_admin import issuer  # noqa: F401 — fixture
from tests.unit.test_aa979_a10b_studio_invites import (  # noqa: F401 — real is a fixture
    NULL_FLAG, REAL, SYNTHETIC, _headers, real,
)

ALL_ACTIONS = {"invite", "send_again", "change_role", "remove"}
ALL_ROLES = ["owner", "editor", "viewer"]


def _principal(role: str, *, e2e_read_only: bool = False) -> staff_authz.StaffPrincipal:
    # e2e_read_only models the A10b admin (the one identity with allow-listed writes, AA-1035 A10d).
    identity = SimpleNamespace(auth="cognito", is_owner=False, e2e_read_only=e2e_read_only,
                               e2e_allowlisted_writes=e2e_read_only)
    return staff_authz.principal_from_role(role, identity)


# ── pure ─────────────────────────────────────────────────────────────────────────────────────────

def test_every_member_write_is_a_policy_write_route():
    assert set(router_mod.MEMBER_WRITES) == ALL_ACTIONS
    for key, (method, route) in router_mod.MEMBER_WRITES.items():
        assert method not in ("GET", "HEAD"), key
        # AA-1035 R2: also the tenant desk roles' tenant.member.write (scoped to their tenants); the send-again also
        # support's tenant.member.resend (S267 fix pass; an owner invitation needs tenant.write in the route)
        expected = (staff_authz.TENANT_WRITE, staff_authz.TENANT_MEMBER_WRITE)
        if key == "send_again":
            expected += (staff_authz.TENANT_MEMBER_RESEND,)
        assert staff_authz.POLICY[(method, route)] == expected, key


def test_the_access_route_is_tenant_read():
    assert staff_authz.POLICY[("GET", "/admin/tenants/{tenant_id}/members/access")] == (staff_authz.TENANT_READ,
                                                                                         staff_authz.TENANT_BOOK_READ)


def test_the_invitation_controls_are_the_e2e_allow_listed_routes():
    """The two controls the E2E session may use on a synthetic tenant are exactly its allow-listed invitation routes."""
    invitations = {router_mod.MEMBER_WRITES["invite"], router_mod.MEMBER_WRITES["send_again"]}
    assert invitations == set(e2e_staff.E2E_STUDIO_INVITATION_WRITES)


@pytest.mark.parametrize("synthetic", [True, False])
def test_admin_may_use_every_control(synthetic):
    out = router_mod.members_access(_principal("admin"), synthetic_tenant=synthetic)
    assert out == {"read_only": False, "actions": dict.fromkeys(ALL_ACTIONS, True), "invite_roles": ALL_ROLES,
                   "owner_writes": True, "send_again_to": {"same_address": True, "new_address": True}}


def test_e2e_admin_on_a_real_tenant_is_read_only_like_the_api():
    out = router_mod.members_access(_principal("admin", e2e_read_only=True), synthetic_tenant=False)
    assert out == {"read_only": True, "actions": dict.fromkeys(ALL_ACTIONS, False), "invite_roles": [],
                   "owner_writes": False, "send_again_to": {"same_address": False, "new_address": False}}


def test_e2e_admin_on_a_synthetic_tenant_may_only_invite_viewer_or_editor():
    principal = _principal("admin", e2e_read_only=True)
    out = router_mod.members_access(principal, synthetic_tenant=True)
    assert out["read_only"] is False
    assert out["actions"] == {"invite": True, "send_again": True, "change_role": False, "remove": False}
    assert out["invite_roles"] == ["editor", "viewer"]
    assert set(out["invite_roles"]) == e2e_staff.E2E_INVITE_ROLES
    # AA-1035 close-out: the E2E session names an E2E mailbox address, never the CRM's kept one
    assert out["send_again_to"] == {"same_address": False, "new_address": True}
    # change role and remove follow denial_reason: refused for the E2E session even on a synthetic tenant.
    for key in ("change_role", "remove"):
        assert staff_authz.denial_reason(principal, *router_mod.MEMBER_WRITES[key]) == e2e_staff.E2E_READ_ONLY


@pytest.mark.parametrize("role", ["viewer", "content", "not-a-role"])
def test_other_roles_get_no_control(role):
    out = router_mod.members_access(_principal(role), synthetic_tenant=True)
    assert out == {"read_only": True, "actions": dict.fromkeys(ALL_ACTIONS, False), "invite_roles": [],
                   "owner_writes": False, "send_again_to": {"same_address": False, "new_address": False}}


def test_the_machine_gets_no_control():
    out = router_mod.members_access(staff_authz.MACHINE, synthetic_tenant=False)
    assert out["read_only"] is True and out["invite_roles"] == []


# ── real app ─────────────────────────────────────────────────────────────────────────────────────

def _access(client, tenant: str, headers: dict):
    return client.get(f"/admin/tenants/{tenant}/members/access", headers=headers)


@pytest.mark.parametrize("tenant", [REAL, NULL_FLAG])
def test_route_e2e_admin_on_a_real_tenant(real, issuer, tenant):  # noqa: F811
    client, directory, relays, _ = real
    res = _access(client, tenant, _headers(issuer))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tenant_id"] == tenant
    assert body["read_only"] is True and not any(body["actions"].values()) and body["invite_roles"] == []
    assert directory.synthetic_checks == 1
    assert directory.writes == [] and relays == []


def test_route_e2e_admin_on_a_synthetic_tenant(real, issuer):  # noqa: F811
    client, directory, _, _ = real
    res = _access(client, SYNTHETIC, _headers(issuer))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["actions"] == {"invite": True, "send_again": True, "change_role": False, "remove": False}
    assert body["invite_roles"] == ["editor", "viewer"] and body["read_only"] is False


def test_route_normal_admin_sees_every_control_without_the_synthetic_read(real, issuer):  # noqa: F811
    client, directory, _, _ = real
    directory.add(role="admin", email="staff@adventure.asia", iss=ISS, sub="another-sub")
    token = issuer.token(sub="another-sub", email="staff@adventure.asia", **{"cognito:groups": ["cis-admin"]})
    res = _access(client, REAL, {"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    assert res.json() == {"tenant_id": REAL, "read_only": False, "actions": dict.fromkeys(ALL_ACTIONS, True),
                          "invite_roles": ALL_ROLES, "owner_writes": True,
                          "send_again_to": {"same_address": True, "new_address": True}}
    assert directory.synthetic_checks == 0


def test_route_unknown_tenant_is_404(real, issuer):  # noqa: F811
    client, _, _, _ = real
    res = _access(client, str(uuid.uuid4()), _headers(issuer))
    assert res.status_code == 404


def test_route_is_in_the_openapi_document():
    from api.main import app

    assert "/admin/tenants/{tenant_id}/members/access" in app.openapi()["paths"]
