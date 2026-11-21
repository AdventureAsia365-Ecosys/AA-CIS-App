"""AA-1155 spec task T8 (R5.1, R5.2, R2.3) — CIS custom staff roles in staff_identity / staff_authz.

- R2.3: the seed of CIS 462 (custom_content "Content") equals the code's rule (content_lead minus
  content.settings.write, plus tenant.read): this test fails when the migration and ROLE_PERMS disagree.
- R5.1: a cis-custom holder is authorised from the custom role's capabilities and refused (403
  custom_role_unavailable) when the role or its capabilities cannot be read; built-in holders never read the tables.
- R5.2: /auth/verify-admin returns the caller's capability list (built-in and custom).
- The route matrix of the custom role (every POLICY entry) and the tenant scope it derives from its capabilities.
- A10f / A10g (root ADR 0006a): Dev only, read-only.

No database, no network: the fake directory of tests/unit/aa854_cognito_staff.py answers the SQL.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as main_mod
from api.core import e2e_staff, identity_config, staff_authz, staff_identity, staff_roles, tenant_scope
from api.core.staff_authz import POLICY, ROLE_PERMS, StaffPrincipal, denial_reason, policy_capabilities
from api.main import app, get_pool
from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, install_issuer

MIGRATION_462 = Path(__file__).resolve().parents[2] / "api" / "migrations" / "462_aa1155_custom_staff_roles.sql"
CUSTOM_GROUPS = ["cis-custom", "booking-custom"]


@pytest.fixture
def directory():
    d = Directory()
    app.dependency_overrides[get_pool] = lambda: d
    yield d
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def issuer(monkeypatch):
    monkeypatch.delenv("LEGACY_ADMIN_LOGIN_ENABLED", raising=False)
    iss, _ = install_issuer()
    main_mod._staff_state_cache.clear()
    yield iss
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()


@pytest.fixture
def client(directory, issuer):
    return TestClient(app)


def _verify(client, token):
    return client.post("/auth/verify-admin", json={"token": token})


# ── R2.3: the seed and the code agree ─────────────────────────────────────────────────────────────────────────────

def _seed_of_462() -> set[str]:
    text = MIGRATION_462.read_text(encoding="utf-8")
    block = text.split("INSERT INTO shared.staff_role_capability", 1)[1].split("ON CONFLICT", 1)[0]
    return set(re.findall(r"'([a-z0-9_]+(?:\.[a-z0-9_]+)+)'", block))


def test_the_462_content_seed_equals_content_lead_minus_settings_plus_tenant_read():
    expected = staff_roles.content_seed_capabilities()
    assert _seed_of_462() == set(expected)
    assert len(expected) == 18
    assert "content.settings.write" not in expected and "tenant.read" in expected
    # Seeded under the code the code uses, based on content_lead (CIS 462).
    text = MIGRATION_462.read_text(encoding="utf-8")
    assert f"('{staff_roles.CONTENT_ROLE_CODE}', '{staff_roles.CONTENT_ROLE_LABEL}', " \
           f"'{staff_roles.CONTENT_ROLE_BASED_ON}'," in text


def test_the_seed_holds_only_capabilities_a_custom_role_may_hold():
    seed = staff_roles.content_seed_capabilities()
    assert seed <= staff_roles.custom_allowed()
    assert not any(staff_roles.is_approve_class(c) for c in seed)


# ── Capability rules ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cap", ["settings.approve", "pricing.approve", "x.approve_rate", "staff.write", "staff.admin"])
def test_the_approve_class_is_recognised(cap):
    assert staff_roles.is_approve_class(cap)


@pytest.mark.parametrize("cap", ["content.read", "settings.write", "approvers.read", "content.publish"])
def test_other_capabilities_are_not_the_approve_class(cap):
    assert not staff_roles.is_approve_class(cap)


def test_custom_capabilities_keep_only_known_and_allowed_names():
    got = staff_roles.custom_capabilities(["content.read", "made.up", "settings.approve", "staff.write", 7])
    assert got == frozenset({"content.read"})
    assert "staff.write" not in staff_roles.custom_allowed()
    assert staff_roles.custom_allowed() <= staff_authz.CAPABILITIES


@pytest.mark.parametrize("code,ok", [("custom_content", True), ("custom_a1", True), ("content", False),
                                     ("custom_", False), ("Custom_x", False), ("custom_x-y", False)])
def test_custom_code_pattern(code, ok):
    assert staff_roles.is_custom_code(code) is ok


# ── R5.1 / R5.2: verify-admin ─────────────────────────────────────────────────────────────────────────────────────

def test_a_custom_holder_gets_its_role_capabilities_and_label(client, directory, issuer):
    directory.add_custom_role(capabilities=sorted(staff_roles.content_seed_capabilities()))
    uid = directory.add(role="content_lead", iss=ISS, sub=SUB, custom_role="custom_content")
    res = _verify(client, issuer.token(**{"cognito:groups": CUSTOM_GROUPS}))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["admin_id"] == uid
    assert body["role"] == "custom_content"
    assert body["custom_role_label"] == "Content"
    assert body["capabilities"] == sorted(staff_roles.content_seed_capabilities())
    assert "content.settings.write" not in body["capabilities"]   # never the base role's (content_lead) rights


@pytest.mark.parametrize("case", ["no_custom_role", "unknown", "archived", "empty", "unreadable"])
def test_a_custom_holder_is_refused_when_its_role_cannot_be_used(client, directory, issuer, case):
    if case == "archived":
        directory.add_custom_role(archived=True)
    elif case == "empty":
        directory.add_custom_role(capabilities=["made.up", "settings.approve"])   # nothing the code allows
    elif case in ("no_custom_role", "unreadable"):
        directory.add_custom_role()
    directory.fail_roles = case == "unreadable"
    custom = None if case == "no_custom_role" else "custom_content"
    if case == "unknown":
        custom = "custom_gone"
    directory.add(role="content_lead", iss=ISS, sub=SUB, custom_role=custom)
    res = _verify(client, issuer.token(**{"cognito:groups": ["cis-custom"]}))
    assert res.status_code == 403
    assert res.json()["detail"] == staff_roles.CUSTOM_ROLE_REFUSED


def test_an_admin_signs_in_while_the_role_tables_are_unreadable(client, directory, issuer):
    directory.fail_roles = True
    directory.add(role="admin", iss=ISS, sub=SUB)
    res = _verify(client, issuer.token(**{"cognito:groups": ["cis-admin"]}))
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "admin"
    assert not any(sql == staff_roles.SQL_CIS_ROLE for sql in directory.sql)   # built-in holders never read them


def test_a_built_in_group_outranks_cis_custom(client, directory, issuer):
    directory.add_custom_role()
    directory.add(role="content_lead", iss=ISS, sub=SUB, custom_role="custom_content")
    res = _verify(client, issuer.token(**{"cognito:groups": ["cis-content", "cis-custom"]}))
    assert res.status_code == 200
    assert res.json()["role"] == "content"
    assert res.json()["capabilities"] == sorted(ROLE_PERMS["content"])


def test_booking_custom_alone_is_no_cis_role(client, directory, issuer):
    directory.add_custom_role()
    directory.add(role="content_lead", iss=ISS, sub=SUB, custom_role="custom_content")
    res = _verify(client, issuer.token(**{"cognito:groups": ["booking-custom"]}))
    assert res.status_code == 403
    assert res.json()["detail"] == "No CIS staff group"


# ── staff_authz: the principal and the route matrix ─────────────────────────────────────────────────────────────

class _Identity:
    def __init__(self, role, caps, auth="cognito"):
        self.role, self.capabilities, self.auth = role, frozenset(caps), auth
        self.is_owner = False
        self.e2e_read_only = False
        self.e2e_allowlisted_writes = False
        self.admin_id = "00000000-0000-4000-8000-0000000000aa"


SEED = staff_roles.content_seed_capabilities()


def test_a_custom_principal_takes_its_capabilities_from_the_verified_identity():
    p = staff_authz.principal_from_role("custom_content", _Identity("custom_content", SEED))
    assert p.perms == SEED and p.auth == "cognito"
    assert denial_reason(p, "GET", "/admin/review-queue") is None
    assert denial_reason(p, "PATCH", "/admin/settings/seo") is not None   # content.settings.write


@pytest.mark.parametrize("identity", [None, "legacy", "other_role"])
def test_a_forged_custom_role_header_holds_nothing(identity):
    ident = (None if identity is None
             else _Identity("custom_content", SEED, auth="legacy") if identity == "legacy"
             else _Identity("custom_other", SEED))
    p = staff_authz.principal_from_role("custom_content", ident)
    assert p.perms == frozenset()
    assert denial_reason(p, "GET", "/admin/review-queue") == "Unknown staff role"
    assert denial_reason(p, "PATCH", "/admin/settings/seo") == "Unknown staff role"


def test_role_perms_reads_the_custom_role_of_this_request_only():
    assert staff_authz.role_perms("custom_content") == frozenset()
    token = staff_identity.current_staff.set(_Identity("custom_content", SEED))
    try:
        assert staff_authz.role_perms("custom_content") == SEED
        assert staff_authz.has_perm("custom_content", "tenant.read")
        assert not staff_authz.has_perm("custom_content", "content.settings.write")
        assert staff_authz.role_perms("custom_other") == frozenset()
        assert staff_authz.is_staff_role("custom_content") and not staff_authz.is_staff_role("custom_other")
    finally:
        staff_identity.current_staff.reset(token)
    assert staff_authz.role_perms("content_lead") == ROLE_PERMS["content_lead"]


def _allowed(principal: StaffPrincipal, method: str, route: str) -> bool:
    return denial_reason(principal, method, route) is None


def test_the_custom_content_role_reaches_exactly_what_its_capabilities_open():
    """Every POLICY route: allowed iff one of its capabilities is in the seed (owner-only staff.write never). Against
    content_lead the only differences are the content.settings.write routes (refused) and the tenant.read routes
    (opened, Q7)."""
    custom = StaffPrincipal(kind="staff", role="custom_content", perms=SEED, auth="cognito")
    lead = staff_authz.principal_from_role("content_lead")
    for (method, route), entry in POLICY.items():
        caps = set(policy_capabilities(entry))
        want = bool(caps & SEED) and not caps <= staff_authz.OWNER_PERMS
        assert _allowed(custom, method, route) is want, (method, route, entry)
        if _allowed(custom, method, route) != _allowed(lead, method, route):
            assert caps & {"content.settings.write", "tenant.read"}, (method, route, entry)


def test_an_unlisted_get_needs_content_read_for_a_custom_role_too():
    no_read = StaffPrincipal(kind="staff", role="custom_desk", perms=frozenset({"tenant.book.read"}),
                             auth="cognito")
    assert denial_reason(no_read, "GET", "/admin/not-in-policy") is not None
    reader = StaffPrincipal(kind="staff", role="custom_content", perms=SEED, auth="cognito")
    assert denial_reason(reader, "GET", "/admin/not-in-policy") is None


@pytest.mark.parametrize("caps,scope", [
    ({"content.read", "tenant.read"}, tenant_scope.SCOPE_ALL),
    ({"tenant_content.read"}, tenant_scope.SCOPE_ALL),
    ({"tenant.book.read"}, tenant_scope.SCOPE_BOOK),
    ({"content.read"}, tenant_scope.SCOPE_INTERNAL),
    ({"notifications.read"}, None),
])
def test_a_custom_role_scope_comes_from_its_capabilities(caps, scope):
    p = StaffPrincipal(kind="staff", role="custom_x1", perms=frozenset(caps), auth="cognito")
    assert tenant_scope.principal_scope(p) == scope
    assert tenant_scope.principal_scope(staff_authz.principal_from_role("content_lead")) == tenant_scope.SCOPE_ALL


# ── A10f / A10g ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email,groups,custom", [
    (e2e_staff.E2E_CIS_CONTENT_LEAD_TEST_STAFF_EMAIL, ["cis-content_lead"], None),
    (e2e_staff.E2E_CUSTOM_MAKER_TEST_STAFF_EMAIL, CUSTOM_GROUPS, "custom_content"),
])
def test_a10f_and_a10g_are_dev_only_and_read_only(client, directory, issuer, monkeypatch, email, groups, custom):
    directory.add_custom_role()
    directory.add(role="content_lead", email=email, iss=ISS, sub=SUB, custom_role=custom)
    token = issuer.token(**{"cognito:groups": groups, "email": email})
    monkeypatch.setenv("ENVIRONMENT", "prod")
    res = _verify(client, token)
    assert res.status_code == 403 and res.json()["detail"] == e2e_staff.E2E_NOT_ALLOWED_HERE
    staff_identity.clear_cache()
    monkeypatch.setenv("ENVIRONMENT", "dev")
    res = _verify(client, token)
    assert res.status_code == 200, res.text
    assert res.json()["role"] == (custom or "content_lead")
    assert e2e_staff.is_dev_only_e2e_staff(email)
    assert not e2e_staff.is_e2e_cis_admin(email)    # no write allow-list (A10b only)


def test_the_a10g_booking_grant_is_a_custom_role_on_dev_only(monkeypatch):
    maker = e2e_staff.E2E_CUSTOM_MAKER_TEST_STAFF_EMAIL
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert e2e_staff.e2e_booking_role_allowed(maker, "custom_product_ops")
    assert e2e_staff.e2e_booking_role_allowed(maker, None)
    assert not e2e_staff.e2e_booking_role_allowed(maker, "contracting")
    assert not e2e_staff.e2e_booking_role_allowed(e2e_staff.E2E_CIS_CONTENT_LEAD_TEST_STAFF_EMAIL, "custom_x1")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert not e2e_staff.e2e_booking_role_allowed(maker, "custom_product_ops")
