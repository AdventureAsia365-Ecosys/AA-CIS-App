"""AA-1035 A10d (S270, root ADR 0006a A10d) — the Dev-only read-only E2E customer_success identity.

e2e-cis-cs@e2e-mail.lumiguides.it.com, role customer_success, Dev only, read-only with NO write allow-list:

- api/core/e2e_staff.py: the constant, the Dev-only map, the helpers;
- the staff directory: address, scope (no other CIS role, no Booking grant, no CRM role), environment guard;
- staff_identity.resolve_staff: ``e2e_read_only`` True, ``e2e_allowlisted_writes`` False, 403 outside dev;
- the bearer middleware and staff_authz.denial_reason: every write is 403 ``e2e_read_only``, the six A10b
  allow-listed write routes included;
- the route-level A10b exceptions (view-as, studio invitations, sentence apply) and docs.owner_read never fire for it;
- the assignment route answers 409 for a Dev-only E2E row on a non-synthetic tenant, before any write;
- A10b is unchanged (regression).
No network, no AWS, no DB.
"""
from __future__ import annotations

import glob
import os
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Optional
from uuid import UUID

import pytest
import structlog.testing
from botocore.stub import Stubber
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from api.core import e2e_staff, staff_authz, staff_identity
from api.main import app as real_app
from api.middleware.staff_bearer import StaffBearerMiddleware
from api.routers import admin, admin_content_quality, admin_settings, admin_tenant360, admin_view_as
from api.services import staff_directory as sd
from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, StaffIssuer
from tests.unit.test_aa816_e2e_cis_admin import (  # noqa: F401 — issuer and directory are fixtures
    ADMIN_E2E, NOT_DEV, PIECE, SECRET, VERSION, CQDirectory, _admin_e2e_row, _admin_e2e_token, _set_env, directory,
    issuer,
)
from tests.unit.test_aa816_staff_directory_writes import POOL, StaffDB, _client, _create, _expect, _group

CS_E2E = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
CS_GROUP = "cis-customer_success"
TENANT = "3c2b1a00-1111-4222-8333-444455556666"
STAFF_ROW = "5d6e7f80-1111-4222-8333-444455556666"

# The six A10b allow-listed writes, as concrete paths (one per E2E_ALLOWED_WRITE_PATHS entry).
ALLOW_LISTED_PATHS = [
    f"/admin/content-quality/pieces/{PIECE}/sentences/apply",
    f"/admin/content-quality/tour-versions/{VERSION}/sentences/apply",
    f"/admin/tenants/{PIECE}/members/invitations",
    f"/admin/tenants/{PIECE}/members/{VERSION}/invitation/send",
    f"/admin/tenants/{PIECE}/view-as",
    f"/admin/view-as/sessions/{VERSION}/end",
]


# ── api/core/e2e_staff.py ────────────────────────────────────────────────────────────────────────

def test_constants_and_the_dev_only_map():
    assert e2e_staff.E2E_CIS_CS_TEST_STAFF_EMAIL == CS_E2E
    assert e2e_staff.E2E_CIS_CS_TEST_STAFF_ROLE == "customer_success"
    assert dict(e2e_staff.DEV_ONLY_E2E_STAFF_ROLES) == {ADMIN_E2E: "admin", CS_E2E: "customer_success"}
    with pytest.raises(TypeError):
        e2e_staff.DEV_ONLY_E2E_STAFF_ROLES["x@e2e-mail.lumiguides.it.com"] = "admin"  # type: ignore[index]
    # The live precheck finds the deployed address among the module's upper-case str constants.
    upper = {v for k, v in vars(e2e_staff).items() if k.isupper() and isinstance(v, str)}
    assert CS_E2E in upper
    assert sd.E2E_CIS_CS_TEST_STAFF_EMAIL == CS_E2E and sd.E2E_CIS_CS_TEST_STAFF_ROLE == "customer_success"


@pytest.mark.parametrize("email,dev_only,cs,admin_", [
    (CS_E2E, True, True, False),
    ("  E2E-CIS-CS@E2E-Mail.Lumiguides.IT.com ", True, True, False),
    (ADMIN_E2E, True, False, True),
    ("e2e@e2e-mail.lumiguides.it.com", False, False, False),
    ("e2e-crm-staff@e2e-mail.lumiguides.it.com", False, False, False),
    ("e2e-cis-cs2@e2e-mail.lumiguides.it.com", False, False, False),
    ("someone@adventure.asia", False, False, False),
    (None, False, False, False),
])
def test_helpers(email, dev_only, cs, admin_):
    assert e2e_staff.is_dev_only_e2e_staff(email) is dev_only
    assert e2e_staff.is_e2e_cis_cs(email) is cs
    assert e2e_staff.is_e2e_cis_admin(email) is admin_


def test_has_allowlisted_writes_needs_both_flags():
    assert e2e_staff.has_allowlisted_writes(SimpleNamespace(e2e_read_only=True, e2e_allowlisted_writes=True))
    assert not e2e_staff.has_allowlisted_writes(SimpleNamespace(e2e_read_only=True, e2e_allowlisted_writes=False))
    assert not e2e_staff.has_allowlisted_writes(SimpleNamespace(e2e_read_only=False, e2e_allowlisted_writes=True))
    assert not e2e_staff.has_allowlisted_writes(SimpleNamespace(e2e_read_only=True))
    assert not e2e_staff.has_allowlisted_writes(None)


# ── Staff directory ──────────────────────────────────────────────────────────────────────────────

def test_the_cs_address_is_a_staff_address():
    assert sd.require_staff_email(" E2E-CIS-CS@e2e-mail.lumiguides.it.com ") == CS_E2E
    assert sd.is_e2e_test_email(CS_E2E)


@pytest.mark.parametrize("email", [
    "e2e-cis-cs2@e2e-mail.lumiguides.it.com",
    "e2e-cis-cs@e2e-mail.lumiguides.it.com.evil.com",
    "e2e-cis-cs@lumiguides.it.com",
    "e2e-cis-cѕ@e2e-mail.lumiguides.it.com",
])
def test_look_alikes_are_refused(email):
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_staff_email(email)
    assert err.value.status == 400


def test_scope_accepts_customer_success_only():
    sd.require_e2e_test_scope(CS_E2E, "customer_success", None)


@pytest.mark.parametrize("role,booking,crm", [
    ("admin", None, ()), ("support", None, ()), ("content", None, ()), (None, None, ()),
    ("customer_success", "sales", ()), ("customer_success", "admin", ()), ("customer_success", "finance", ()),
    ("customer_success", "content_creator", ()), ("admin", "contracting", ()),
    ("customer_success", "contracting", ("ops",)),
    ("customer_success", None, ("ops",)), ("customer_success", None, ("platform_admin",)),
])
def test_scope_refuses_anything_else(role, booking, crm):
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_e2e_test_scope(CS_E2E, role, booking, crm)
    assert (err.value.status, err.value.code) == (400, "e2e_test_scope")
    assert "E2E customer success account" in err.value.detail and "customer_success" in err.value.detail


@pytest.mark.parametrize("value", NOT_DEV)
def test_environment_guard_refuses_outside_dev_with_its_own_name(monkeypatch, value):
    _set_env(monkeypatch, value)
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_dev_only_e2e_environment(CS_E2E)
    assert (err.value.status, err.value.code) == (403, "e2e_not_allowed_here")
    assert err.value.detail == "The E2E customer success account exists only on Dev"
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_dev_only_e2e_environment(ADMIN_E2E)
    assert err.value.detail == "The E2E CIS admin account exists only on Dev"   # A10b wording unchanged


@pytest.fixture
def stubbed():
    client = _client()
    with Stubber(client) as stub:
        yield sd.CognitoDirectory(client=client, pool_id=POOL), stub
        stub.assert_no_pending_responses()


@pytest.fixture
def db():
    staff_identity.clear_cache()
    d = StaffDB()
    d.owner = d.add(owner=True, email="owner@adventure.asia", username="owner", role="admin")
    yield d
    staff_identity.clear_cache()


@pytest.mark.asyncio
async def test_create_on_dev_inserts_a_customer_success_row_that_is_not_an_owner(db, stubbed, monkeypatch):
    _set_env(monkeypatch, "dev")
    cognito, stub = stubbed
    _expect(stub, "admin_create_user", _create(CS_E2E), {"User": {"Username": CS_E2E}})
    _expect(stub, "admin_add_user_to_group", _group(CS_E2E, CS_GROUP))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=CS_E2E, display_name="E2E customer success",
                                   role="customer_success", booking_role=None, send_invite=False)
    assert result.status == sd.STATUS_OK and result.created
    assert result.steps == ["create_user", f"add_group:{CS_GROUP}"]
    row = db.row(result.staff_id)
    assert (row["email"], row["role"], row["is_owner"], row["password_hash"]) == (CS_E2E, "customer_success",
                                                                                  False, None)
    assert db.actions() == [sd.StaffAudit.CREATED]


@pytest.mark.parametrize("kw", [{"booking_role": "sales"}, {"crm_roles": ["ops"]}, {"crm_roles": ["support"]},
                                {"role": "admin"}])
@pytest.mark.asyncio
async def test_create_with_a_booking_or_crm_role_is_refused(db, stubbed, monkeypatch, kw):
    _set_env(monkeypatch, "dev")
    cognito, _ = stubbed   # no stubbed response: any Cognito call would fail the test
    args = {"role": "customer_success", **kw}
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email=CS_E2E, display_name="E2E", **args)
    assert err.value.code == "e2e_test_scope" and len(db.rows) == 1 and db.audit == []


@pytest.mark.parametrize("value", [None, "test", "prod", "Dev "])
@pytest.mark.asyncio
async def test_create_outside_dev_is_refused_before_any_write(db, stubbed, monkeypatch, value):
    _set_env(monkeypatch, value)
    cognito, _ = stubbed
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email=CS_E2E, display_name="E2E",
                              role="customer_success")
    assert (err.value.status, err.value.code) == (403, "e2e_not_allowed_here")
    assert len(db.rows) == 1 and db.audit == []


# ── resolve_staff ────────────────────────────────────────────────────────────────────────────────

def _cs_row(directory: Directory, owner: bool = False) -> str:
    uid = directory.add(role="customer_success", email=CS_E2E, iss=ISS, sub=SUB, username="e2e-cis-cs")
    directory.row(uid)["is_owner"] = owner
    return uid


def _cs_token(issuer: StaffIssuer, groups=(CS_GROUP,), email: str = CS_E2E) -> str:
    return issuer.token(email=email, **{"cognito:groups": list(groups)})


@pytest.mark.asyncio
async def test_resolve_flags_the_cs_session_read_only_without_the_allow_list(issuer, directory, monkeypatch):
    _set_env(monkeypatch, "dev")
    uid = _cs_row(directory, owner=True)   # an owner flag could only come from a hand-edited row
    with structlog.testing.capture_logs() as logs:
        identity = await staff_identity.authenticate(directory, _cs_token(issuer))
    assert identity.admin_id == uid
    assert (identity.role, identity.e2e_read_only, identity.e2e_allowlisted_writes, identity.is_owner) == (
        "customer_success", True, False, False)
    assert any(e.get("event") == "staff_identity_e2e_read_only" and e.get("allowlisted_writes") is False
               for e in logs)


@pytest.mark.parametrize("value", NOT_DEV)
@pytest.mark.asyncio
async def test_resolve_refuses_the_cs_identity_outside_dev(issuer, directory, monkeypatch, value):
    _set_env(monkeypatch, value)
    _cs_row(directory)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _cs_token(issuer))
    assert (exc.value.status, exc.value.reason) == (403, "e2e_not_allowed_here")


@pytest.mark.asyncio
async def test_a_company_row_with_the_cs_token_email_is_capped_too(issuer, directory, monkeypatch):
    _set_env(monkeypatch, "dev")
    directory.add(role="customer_success", email="someone@adventure.asia", iss=ISS, sub=SUB)
    identity = await staff_identity.authenticate(directory, _cs_token(issuer))
    assert identity.e2e_read_only and not identity.e2e_allowlisted_writes


@pytest.mark.asyncio
async def test_a10b_keeps_its_allow_list(issuer, directory, monkeypatch):
    _set_env(monkeypatch, "dev")
    _admin_e2e_row(directory)
    identity = await staff_identity.authenticate(directory, _admin_e2e_token(issuer))
    assert (identity.role, identity.e2e_read_only, identity.e2e_allowlisted_writes) == ("admin", True, True)


@pytest.mark.asyncio
async def test_a_mixed_identity_never_gets_the_allow_list(issuer, directory, monkeypatch):
    # The row is the A10b admin but the token's email is the cs address (or the other way): fail closed.
    _set_env(monkeypatch, "dev")
    _admin_e2e_row(directory)
    identity = await staff_identity.authenticate(directory, _cs_token(issuer, groups=("cis-admin",)))
    assert identity.e2e_read_only and not identity.e2e_allowlisted_writes


@pytest.mark.asyncio
async def test_other_staff_are_not_flagged(issuer, directory, monkeypatch):
    _set_env(monkeypatch, "dev")
    directory.add(role="customer_success", email="cs@adventure.asia", iss=ISS, sub=SUB)
    identity = await staff_identity.authenticate(directory, _cs_token(issuer, email="cs@adventure.asia"))
    assert (identity.e2e_read_only, identity.e2e_allowlisted_writes) == (False, False)


# ── Bearer middleware ────────────────────────────────────────────────────────────────────────────

def _echo_app(directory) -> tuple[FastAPI, list[str]]:
    reached: list[str] = []
    echo = FastAPI()

    @echo.api_route("/admin/{rest:path}", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
    async def _any(rest: str, request: Request):
        reached.append(f"{request.method} {rest}")
        return {"role": request.headers.get("x-admin-role")}

    echo.add_middleware(StaffBearerMiddleware)
    echo.state.pool = directory
    return echo, reached


@pytest.mark.parametrize("path", ALLOW_LISTED_PATHS)
def test_middleware_refuses_the_a10b_allow_listed_writes_to_cs(issuer, directory, monkeypatch, path):
    _set_env(monkeypatch, "dev")
    _cs_row(directory)
    app, reached = _echo_app(directory)
    with structlog.testing.capture_logs() as logs:
        res = TestClient(app).post(path, json={}, headers={"Authorization": f"Bearer {_cs_token(issuer)}"})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert reached == []
    assert any(e.get("event") == "staff_bearer_refused" and e.get("reason") == "e2e_read_only" for e in logs)


@pytest.mark.parametrize("method,path", [
    ("PUT", f"/admin/tenants/{TENANT}/assignments/{STAFF_ROW}"),
    ("DELETE", f"/admin/tenants/{TENANT}/assignments/{STAFF_ROW}"),
    ("POST", "/admin/platform/staff"),
    ("PATCH", f"/admin/tenants/{PIECE}/members/{VERSION}"),
    ("POST", f"/admin/content-quality/pieces/{PIECE}/sentences/suggest"),
])
def test_middleware_refuses_every_other_write(issuer, directory, monkeypatch, method, path):
    _set_env(monkeypatch, "dev")
    _cs_row(directory)
    app, reached = _echo_app(directory)
    res = TestClient(app).request(method, path, json={}, headers={"Authorization": f"Bearer {_cs_token(issuer)}"})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"}) and reached == []


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_middleware_lets_reads_through_as_customer_success(issuer, directory, monkeypatch, method):
    _set_env(monkeypatch, "dev")
    _cs_row(directory)
    app, reached = _echo_app(directory)
    res = TestClient(app).request(method, "/admin/tenants", headers={"Authorization": f"Bearer {_cs_token(issuer)}"})
    assert res.status_code == 200 and reached == [f"{method} tenants"]
    if method == "GET":
        assert res.json() == {"role": "customer_success"}


@pytest.mark.parametrize("path", ALLOW_LISTED_PATHS)
def test_middleware_still_lets_a10b_through_the_allow_list(issuer, directory, monkeypatch, path):
    _set_env(monkeypatch, "dev")
    _admin_e2e_row(directory)
    app, reached = _echo_app(directory)
    res = TestClient(app).post(path, json={}, headers={"Authorization": f"Bearer {_admin_e2e_token(issuer)}"})
    assert res.status_code == 200 and len(reached) == 1


# ── staff_authz ──────────────────────────────────────────────────────────────────────────────────

def _identity(*, allowlisted: bool) -> SimpleNamespace:
    return SimpleNamespace(auth="cognito", is_owner=False, e2e_read_only=True, e2e_allowlisted_writes=allowlisted)


def _cs_principal() -> staff_authz.StaffPrincipal:
    return staff_authz.principal_from_role("customer_success", _identity(allowlisted=False))


def test_principal_carries_both_flags():
    p = _cs_principal()
    assert (p.e2e_read_only, p.e2e_allowlisted_writes) == (True, False)
    a = staff_authz.principal_from_role("admin", _identity(allowlisted=True))
    assert (a.e2e_read_only, a.e2e_allowlisted_writes) == (True, True)
    # The allow-list flag alone, without the read-only cap, is never carried.
    lone = SimpleNamespace(auth="cognito", is_owner=False, e2e_read_only=False, e2e_allowlisted_writes=True)
    assert staff_authz.principal_from_role("admin", lone).e2e_allowlisted_writes is False


@pytest.mark.parametrize("method,route", sorted((m, r) for (m, r) in staff_authz.POLICY
                                                if m not in staff_authz.READ_METHODS))
def test_policy_refuses_every_write_route_to_cs(method, route):
    assert staff_authz.denial_reason(_cs_principal(), method, route) == "e2e_read_only"


@pytest.mark.parametrize("method,route", sorted(e2e_staff.E2E_ALLOWED_WRITES))
def test_policy_refuses_the_allow_listed_routes_even_with_the_admin_role(method, route):
    # The allow-list follows the A10b flag, not the role: an e2e_read_only admin without it is refused too.
    principal = staff_authz.principal_from_role("admin", _identity(allowlisted=False))
    assert staff_authz.denial_reason(principal, method, route) == "e2e_read_only"
    a10b = staff_authz.principal_from_role("admin", _identity(allowlisted=True))
    assert staff_authz.denial_reason(a10b, method, route) is None


def test_policy_reads_follow_the_customer_success_role():
    p = _cs_principal()
    assert staff_authz.denial_reason(p, "GET", "/admin/tenants") is None
    assert staff_authz.denial_reason(p, "GET", "/admin/platform/staff") is not None   # staff.read is admin-only


@pytest.mark.parametrize("role", ["customer_success", "admin"])
def test_docs_owner_read_is_not_granted(monkeypatch, role):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    p = staff_authz.principal_from_role(role, _identity(allowlisted=False))
    assert staff_authz.docs_owner_read_via(p) is None and not staff_authz.can_read_owner_docs(p)


# ── Route-level A10b exceptions never fire for cs ────────────────────────────────────────────────

class _Conn:
    """fetchval answers every synthetic-tenant read with ``synthetic``; any other SQL is recorded."""

    def __init__(self, synthetic: Optional[bool] = True) -> None:
        self.synthetic, self.sql = synthetic, []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        return self.synthetic if sql == e2e_staff.SYNTHETIC_TENANT_SQL else None


def _request(identity: Any) -> SimpleNamespace:
    return SimpleNamespace(scope={"state": {"staff_identity": identity}})


CS = SimpleNamespace(auth="cognito", is_owner=False, e2e_read_only=True, e2e_allowlisted_writes=False,
                     role="customer_success", admin_id=STAFF_ROW)
A10B = SimpleNamespace(auth="cognito", is_owner=False, e2e_read_only=True, e2e_allowlisted_writes=True,
                       role="admin", admin_id=STAFF_ROW)
INVITEE = "e2e-test-studio-viewer@e2e-mail.lumiguides.it.com"


@pytest.mark.asyncio
async def test_view_as_refuses_cs_even_on_a_synthetic_tenant():
    conn = _Conn(synthetic=True)
    with pytest.raises(HTTPException) as exc:
        await admin_view_as._refuse_e2e(_request(CS), conn, UUID(TENANT))
    assert (exc.value.status_code, exc.value.detail) == (403, "e2e_read_only")
    await admin_view_as._refuse_e2e(_request(A10B), conn, UUID(TENANT))   # A10b unchanged on a synthetic tenant
    with pytest.raises(HTTPException):
        await admin_view_as._refuse_e2e(_request(A10B), _Conn(synthetic=False), UUID(TENANT))


def test_view_as_session_list_stays_its_own_for_cs():
    principal = staff_authz.principal_from_role("customer_success", CS)
    assert admin_view_as._sees_every_session(principal, _request(CS)) is False


@pytest.mark.asyncio
async def test_studio_invitation_refuses_cs_whatever_the_tenant_address_or_role():
    with pytest.raises(HTTPException) as exc:
        await admin_tenant360._refuse_e2e_invitation(_request(CS), _Conn(True), UUID(TENANT), INVITEE, "viewer")
    assert (exc.value.status_code, exc.value.detail) == (403, "e2e_read_only")
    with pytest.raises(HTTPException):
        admin_tenant360._refuse_e2e_resend_role(_request(CS), UUID(TENANT), {"content_studio_role": "viewer"})
    # A10b unchanged.
    await admin_tenant360._refuse_e2e_invitation(_request(A10B), _Conn(True), UUID(TENANT), INVITEE, "viewer")
    admin_tenant360._refuse_e2e_resend_role(_request(A10B), UUID(TENANT), {"content_studio_role": "viewer"})


@pytest.mark.parametrize("mode", ["cut", "manual", "rewrite"])
def test_sentence_apply_refuses_cs_in_every_mode(mode):
    with pytest.raises(HTTPException) as exc:
        admin_content_quality._refuse_e2e_mode(_request(CS), mode)
    assert (exc.value.status_code, exc.value.detail) == (403, "e2e_read_only")


@pytest.mark.parametrize("mode,refused", [("cut", False), ("manual", False), ("rewrite", True)])
def test_sentence_apply_for_a10b_is_unchanged(mode, refused):
    if refused:
        with pytest.raises(HTTPException):
            admin_content_quality._refuse_e2e_mode(_request(A10B), mode)
    else:
        admin_content_quality._refuse_e2e_mode(_request(A10B), mode)


def test_members_access_offers_cs_no_write():
    out = admin_tenant360.members_access(staff_authz.principal_from_role("customer_success", CS),
                                         synthetic_tenant=True)
    assert out["read_only"] is True and not any(out["actions"].values()) and out["invite_roles"] == []


# ── Assignments: synthetic tenants only for a Dev-only E2E row ───────────────────────────────────

class _AssignConn:
    """The put_tenant_assignment reads: the tenant, the staff row, the synthetic flag. Every write is recorded."""

    def __init__(self, email: Optional[str], synthetic: Optional[bool], role: str = "customer_success") -> None:
        self.staff = {"id": STAFF_ROW, "is_active": True, "role": role, "email": email}
        self.synthetic, self.writes, self.reads = synthetic, [], []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if sql == admin_tenant360.ASSIGN_STAFF_SQL:
            return self.staff
        return {"tenant_id": TENANT, "name": "Quill Harbor", "plan_tier": "starter"}

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.reads.append(sql)
        return self.synthetic if sql == e2e_staff.SYNTHETIC_TENANT_SQL else None

    async def execute(self, sql: str, *args: Any) -> str:
        self.writes.append(sql)
        return "OK"


class _Pool:
    def __init__(self, conn: _AssignConn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def _put(monkeypatch, conn: _AssignConn) -> dict:
    monkeypatch.setattr(admin_tenant360, "verify_admin_secret", lambda secret: None)
    monkeypatch.setattr(admin_tenant360, "_admin_actor", lambda header: "admin:test")

    async def no_audit(*args: Any, **kwargs: Any) -> None:
        conn.writes.append("audit")

    monkeypatch.setattr(admin_tenant360, "write_audit_log", no_audit)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool(conn))))
    return await admin_tenant360.put_tenant_assignment(
        UUID(TENANT), UUID(STAFF_ROW), admin_tenant360.AssignmentBody(kind="backup"), request,
        x_admin_secret="x", x_admin_user_id=None)


@pytest.mark.parametrize("email,role", [(CS_E2E, "customer_success"), (ADMIN_E2E, "admin")])
@pytest.mark.parametrize("synthetic", [False, None])
@pytest.mark.asyncio
async def test_a_dev_only_e2e_row_on_a_real_tenant_is_409_and_writes_nothing(monkeypatch, email, role, synthetic):
    conn = _AssignConn(email, synthetic, role=role)
    with pytest.raises(HTTPException) as exc:
        await _put(monkeypatch, conn)
    assert (exc.value.status_code, exc.value.detail) == (409, admin_tenant360.E2E_REAL_TENANT_REFUSED)
    assert conn.writes == []


@pytest.mark.asyncio
async def test_a_dev_only_e2e_row_on_a_synthetic_tenant_is_assigned(monkeypatch):
    conn = _AssignConn(CS_E2E, True)
    out = await _put(monkeypatch, conn)
    assert out["changed"] is True and any("INSERT INTO shared.staff_tenant_assignment" in w for w in conn.writes)


@pytest.mark.asyncio
async def test_a_company_row_is_not_asked_about_the_tenant(monkeypatch):
    conn = _AssignConn("cs@adventure.asia", False)
    out = await _put(monkeypatch, conn)
    assert out["changed"] is True and e2e_staff.SYNTHETIC_TENANT_SQL not in conn.reads


# ── Real app ─────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def real(monkeypatch, issuer):
    _set_env(monkeypatch, "dev")
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    directory = CQDirectory()
    _cs_row(directory)
    monkeypatch.setattr(real_app.state, "pool", directory, raising=False)
    return TestClient(real_app), directory


@pytest.mark.parametrize("path,body", [
    (f"/admin/content-quality/pieces/{PIECE}/sentences/apply",
     {"start": 0, "end": 5, "text": "Hello", "replacement": "", "mode": "cut"}),
    (f"/admin/tenants/{TENANT}/members/invitations",
     {"email": INVITEE, "content_studio_role": "viewer", "locale": "en"}),
    (f"/admin/tenants/{TENANT}/view-as", {"reason": "Support ticket", "minutes": 15}),
    (f"/admin/view-as/sessions/{VERSION}/end", {}),
])
def test_real_app_the_allow_listed_writes_are_403_for_cs(real, issuer, path, body):
    client, directory = real
    res = client.post(path, json=body, headers={"Authorization": f"Bearer {_cs_token(issuer)}"})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    # refused before any route query: only the identity lookup ran
    assert [q for q in directory.sql if "shared.admin_users" not in q] == []


def test_real_app_assignment_write_is_403_for_cs(real, issuer):
    client, _ = real
    res = client.put(f"/admin/tenants/{TENANT}/assignments/{STAFF_ROW}", json={"kind": "backup"},
                     headers={"Authorization": f"Bearer {_cs_token(issuer)}"})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})


def test_real_app_verify_admin_returns_customer_success_for_the_bff(real, issuer):
    from api.main import get_pool

    client, directory = real
    real_app.dependency_overrides[get_pool] = lambda: directory
    try:
        res = client.post("/auth/verify-admin", json={"token": _cs_token(issuer)})
    finally:
        real_app.dependency_overrides.pop(get_pool, None)
    assert res.status_code == 200, res.text
    assert (res.json()["role"], res.json()["auth"]) == ("customer_success", "cognito")


# ── The other copies of the address ──────────────────────────────────────────────────────────────

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_e2e_helper_and_migration_name_the_same_address():
    with open(os.path.join(ROOT, "tests", "e2e", "helpers", "mailbox.ts"), encoding="utf-8") as f:
        assert f"export const E2E_CIS_CS_EMAIL = '{CS_E2E}';" in f.read()
    (migration,) = glob.glob(os.path.join(ROOT, "api", "migrations", "*_admin_users_e2e_cis_cs_email.sql"))
    with open(migration, encoding="utf-8") as f:
        assert f"email = '{CS_E2E}' AND role = 'customer_success' AND NOT is_owner" in f.read()


def test_the_cs_playwright_specs_are_opt_in():
    for name in ("cs-scope.spec.ts", "viewas-banner.spec.ts"):
        with open(os.path.join(ROOT, "tests", "e2e", "admin-evidence", name), encoding="utf-8") as f:
            spec = f.read()
        assert re.search(r"process\.env\.(E2ECS|E2EADMIN) !== '1'", spec), name
    for path in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.y*ml")):
        with open(path, encoding="utf-8") as f:
            assert not re.search(r"\bE2ECS\b", f.read()), path
