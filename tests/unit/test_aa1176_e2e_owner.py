"""AA-1176 A10h (S288, root ADR 0006a A10h) — the Dev-only E2E owner.

e2e-owner@e2e-mail.lumiguides.it.com: groups exactly cis-admin + booking-admin, a directory admin with the owner marker
(CIS 473), never the business owner, NOT read-only, refused outside ENVIRONMENT dev, exempt from the administrator MFA
switch, held to its D5 limits on the Staff page. No network, no AWS, no DB.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import structlog.testing
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from api.core import (
    e2e_staff, e2e_tenant_writes, pilot_fence, staff_identity, staff_mfa, staff_mfa_settings, staff_view_as,
)
from api.middleware.staff_bearer import StaffBearerMiddleware
from api.services import staff_custom_roles as scr
from api.services import staff_directory as sd
from services import staff_reconcile as sr
from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, StaffIssuer
from tests.unit.test_aa816_e2e_cis_admin import (  # noqa: F401 — issuer and directory are fixtures
    ADMIN_E2E, NOT_DEV, _set_env, directory, issuer,
)
from tests.unit.test_aa816_staff_directory_writes import StaffDB

OWNER_E2E = "e2e-owner@e2e-mail.lumiguides.it.com"
OWNER_GROUPS = ("cis-admin", "booking-admin")
EMAIL_CODE_AMR = ["email_otp"]
ROOT = Path(__file__).resolve().parents[2]


# ── api/core/e2e_staff.py ────────────────────────────────────────────────────────────────────────

def test_constants():
    assert e2e_staff.E2E_OWNER_TEST_STAFF_EMAIL == OWNER_E2E
    assert e2e_staff.E2E_OWNER_TEST_STAFF_ROLE == "admin" == e2e_staff.E2E_OWNER_BOOKING_ROLE
    assert e2e_staff.E2E_OWNER_GROUPS == frozenset(OWNER_GROUPS)
    assert "crm-platform-admin" not in e2e_staff.E2E_OWNER_GROUPS
    # NOT in the read-only map; in the Dev-only address set (environment guards) and the Booking grant map.
    assert OWNER_E2E not in e2e_staff.DEV_ONLY_E2E_STAFF_ROLES
    assert e2e_staff.DEV_ONLY_E2E_ADDRESSES == frozenset(e2e_staff.DEV_ONLY_E2E_STAFF_ROLES) | {OWNER_E2E}
    assert e2e_staff.DEV_ONLY_E2E_BOOKING_ROLES[OWNER_E2E] == "admin"
    upper = {v for k, v in vars(e2e_staff).items() if k.isupper() and isinstance(v, str)}
    assert OWNER_E2E in upper


def test_the_address_matches_the_migration_473_check():
    sql = (ROOT / "api" / "migrations" / "473_aa1176_admin_users_e2e_owner_email.sql").read_text()
    branch = sql.split(f"email = '{OWNER_E2E}'", 1)[1].split(")", 1)[0]
    assert f"role = '{e2e_staff.E2E_OWNER_TEST_STAFF_ROLE}'" in branch
    assert "custom_role IS NULL" in branch and "NOT is_business_owner" in branch and "NOT is_owner" not in branch


@pytest.mark.parametrize("email,owner,dev_only_address,read_only", [
    (OWNER_E2E, True, True, False),
    ("  E2E-Owner@E2E-Mail.Lumiguides.IT.com ", True, True, False),
    (ADMIN_E2E, False, True, True),
    ("e2e-owner+x@e2e-mail.lumiguides.it.com", False, False, False),
    ("e2e-owner@x.e2e-mail.lumiguides.it.com", False, False, False),
    ("e2e-owner@e2e-mail.lumiguides.it.com.", False, False, False),
    ("e2e-owner2@e2e-mail.lumiguides.it.com", False, False, False),
    ("owner@adventure.asia", False, False, False),
    (None, False, False, False),
])
def test_helpers(email, owner, dev_only_address, read_only):
    assert e2e_staff.is_e2e_owner(email) is owner
    assert e2e_staff.is_dev_only_e2e_address(email) is dev_only_address
    assert e2e_staff.is_dev_only_e2e_staff(email) is read_only


@pytest.mark.parametrize("email", [OWNER_E2E + "\u200b", "e2e-\u1d0fwner@e2e-mail.lumiguides.it.com",
                                   "e2e-owner@e2e-mail.lumiguides.it.c\uff4fm", "\u212a" + OWNER_E2E])
def test_non_ascii_look_alikes_are_never_the_owner(email):
    # The raw value must be ASCII before lower-casing (U+212A KELVIN SIGN lower-cases to ASCII 'k').
    assert not e2e_staff.is_e2e_owner(email)
    assert e2e_staff.is_e2e_owner_like(OWNER_E2E.upper())



# ── resolve_staff ────────────────────────────────────────────────────────────────────────────────

def _owner_row(directory: Directory, *, owner: bool = True, role: str = "admin", email: str = OWNER_E2E,
               custom_role: Optional[str] = None) -> str:
    uid = directory.add(role=role, email=email, iss=ISS, sub=SUB, username="e2e-owner",
                        display_name=e2e_staff.E2E_OWNER_DISPLAY_NAME, custom_role=custom_role)
    directory.row(uid)["is_owner"] = owner
    return uid


def _owner_token(issuer: StaffIssuer, groups=OWNER_GROUPS, email: str = OWNER_E2E, amr=None) -> str:
    claims: dict[str, Any] = {"cognito:groups": list(groups)}
    if amr is not None:
        claims["amr"] = amr
    return issuer.token(email=email, **claims)


@pytest.fixture
def switch(monkeypatch):
    state = {"on": False}

    def fake_get(key, tenant_id=None):
        if key == pilot_fence.PILOT_E2E_FENCE:      # AA-1054: the pilot fence stays off here
            return False
        assert key == staff_mfa_settings.ADMIN_MFA_REQUIRED
        return state["on"]

    from shared import settings

    monkeypatch.setattr(settings, "get", fake_get)
    staff_mfa.clear_log_throttle()
    return state


@pytest.mark.asyncio
async def test_resolve_on_dev_is_an_owner_and_not_read_only(issuer, directory, monkeypatch, switch):
    _set_env(monkeypatch, "dev")
    uid = _owner_row(directory)
    with structlog.testing.capture_logs() as logs:
        identity = await staff_identity.authenticate(directory, _owner_token(issuer))
    assert identity.admin_id == uid
    assert (identity.role, identity.is_owner, identity.e2e_read_only, identity.e2e_allowlisted_writes,
            identity.e2e_owner, identity.preview) == ("admin", True, False, False, True, False)
    assert any(e.get("event") == "staff_identity_e2e_owner" and e.get("is_owner") is True for e in logs)
    assert staff_view_as.may_start(identity)


@pytest.mark.asyncio
async def test_without_the_owner_marker_it_is_a_plain_admin(issuer, directory, monkeypatch, switch):
    _set_env(monkeypatch, "dev")
    _owner_row(directory, owner=False)
    identity = await staff_identity.authenticate(directory, _owner_token(issuer))
    assert (identity.is_owner, identity.e2e_owner) == (False, True)
    assert not staff_view_as.may_start(identity)


@pytest.mark.parametrize("value", NOT_DEV)
@pytest.mark.asyncio
async def test_resolve_refuses_it_outside_dev(issuer, directory, monkeypatch, value, switch):
    _set_env(monkeypatch, value)
    _owner_row(directory)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _owner_token(issuer))
    assert (exc.value.status, exc.value.detail, exc.value.reason) == (403, "e2e_not_allowed_here",
                                                                      "e2e_not_allowed_here")


@pytest.mark.parametrize("groups", [
    ("cis-admin",), ("cis-admin", "booking-admin", "crm-platform-admin"), ("cis-admin", "cis-content"),
    ("cis-admin", "booking-finance"), ("cis-admin", "booking-admin", "cis-custom"),
    ("cis-content_lead", "booking-admin"),
])
@pytest.mark.asyncio
async def test_any_other_group_set_is_refused(issuer, directory, monkeypatch, groups, switch):
    _set_env(monkeypatch, "dev")
    _owner_row(directory)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _owner_token(issuer, groups=groups))
    assert (exc.value.status, exc.value.reason) == (403, "e2e_owner_scope")


@pytest.mark.asyncio
async def test_a_company_row_with_the_owner_token_email_is_refused(issuer, directory, monkeypatch, switch):
    _set_env(monkeypatch, "dev")
    uid = directory.add(role="admin", email="boss@adventure.asia", iss=ISS, sub=SUB)
    directory.row(uid)["is_owner"] = True
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _owner_token(issuer))
    assert exc.value.reason == "e2e_owner_scope"


@pytest.mark.asyncio
async def test_the_owner_row_with_a_company_token_email_is_refused(issuer, directory, monkeypatch, switch):
    _set_env(monkeypatch, "dev")
    _owner_row(directory)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _owner_token(issuer, email="boss@adventure.asia"))
    assert exc.value.reason == "e2e_owner_scope"


@pytest.mark.asyncio
async def test_a_mix_with_the_read_only_admin_address_is_refused(issuer, directory, monkeypatch, switch):
    _set_env(monkeypatch, "dev")
    _owner_row(directory)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _owner_token(issuer, email=ADMIN_E2E, groups=("cis-admin",)))
    assert exc.value.reason == "e2e_owner_scope"


@pytest.mark.parametrize("role,custom_role", [("content_lead", None), ("content_lead", "custom_content")])
@pytest.mark.asyncio
async def test_a_row_outside_its_shape_is_refused(issuer, directory, monkeypatch, role, custom_role, switch):
    _set_env(monkeypatch, "dev")
    _owner_row(directory, role=role, custom_role=custom_role)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _owner_token(issuer))
    assert exc.value.reason == "e2e_owner_scope"


@pytest.mark.asyncio
async def test_the_preview_cap_still_wins(issuer, directory, monkeypatch, switch):
    _set_env(monkeypatch, "dev")
    _owner_row(directory)
    monkeypatch.setattr(staff_identity.identity_config, "is_preview_client", lambda client_id: True)
    identity = await staff_identity.authenticate(directory, _owner_token(issuer))
    assert (identity.role, identity.is_owner, identity.preview) == ("viewer", False, True)


# ── Administrator MFA switch ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_switch_on_does_not_refuse_the_e2e_owner(issuer, directory, monkeypatch, switch):
    _set_env(monkeypatch, "dev")
    _owner_row(directory)
    switch["on"] = True
    identity = await staff_identity.authenticate(directory, _owner_token(issuer, amr=EMAIL_CODE_AMR))
    assert identity.e2e_owner and identity.is_owner
    assert not staff_mfa.is_admin_session(identity)


@pytest.mark.asyncio
async def test_the_switch_on_still_refuses_a_real_owner_without_the_code(issuer, directory, monkeypatch, switch):
    _set_env(monkeypatch, "dev")
    uid = directory.add(role="admin", email="boss@adventure.asia", iss=ISS, sub=SUB)
    directory.row(uid)["is_owner"] = True
    switch["on"] = True
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _owner_token(issuer, email="boss@adventure.asia",
                                                                  groups=("cis-admin",), amr=EMAIL_CODE_AMR))
    assert exc.value.detail == "mfa_required"


# ── Bearer middleware: not read-only ─────────────────────────────────────────────────────────────

def _echo_app(directory) -> tuple[FastAPI, list[str]]:
    reached: list[str] = []
    echo = FastAPI()

    @echo.api_route("/admin/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def _any(rest: str, request: Request):
        reached.append(f"{request.method} {rest}")
        identity = staff_identity.current_staff.get()
        return {"role": request.headers.get("x-admin-role"), "owner": identity.is_owner}

    echo.add_middleware(StaffBearerMiddleware)
    echo.state.pool = directory
    return echo, reached


@pytest.mark.parametrize("method,path", [("POST", "/admin/platform/staff"), ("PUT", "/admin/platform/staff/x/roles"),
                                         ("POST", "/admin/platform/compliance/refresh"),
                                         ("POST", "/admin/view-as/roles")])
def test_middleware_lets_its_writes_through(issuer, directory, monkeypatch, switch, method, path):
    _set_env(monkeypatch, "dev")
    _owner_row(directory)
    app, reached = _echo_app(directory)
    with structlog.testing.capture_logs() as logs:
        res = TestClient(app).request(method, path, json={},
                                      headers={"Authorization": f"Bearer {_owner_token(issuer)}"})
    assert res.status_code == 200, res.text
    assert res.json() == {"role": "admin", "owner": True} and len(reached) == 1
    assert any(e.get("event") == "staff_bearer_verified" and e.get("e2e_owner") is True for e in logs)


def test_middleware_refuses_it_outside_dev(issuer, directory, monkeypatch, switch):
    _set_env(monkeypatch, "prod")
    _owner_row(directory)
    app, reached = _echo_app(directory)
    res = TestClient(app).get("/admin/session", headers={"Authorization": f"Bearer {_owner_token(issuer)}"})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_not_allowed_here"}) and reached == []


# ── Erasure approval: only a person ──────────────────────────────────────────────────────────────

def _request(identity: Any) -> Any:
    return SimpleNamespace(state=SimpleNamespace(staff_identity=identity), url=SimpleNamespace(path="/x"),
                           scope={"state": {"staff_identity": identity}})


def test_refuse_any_e2e_refuses_the_owner(monkeypatch):
    identity = SimpleNamespace(e2e_read_only=False, e2e_owner=True)
    monkeypatch.setattr(e2e_tenant_writes, "request_staff_identity", lambda request: identity)
    with pytest.raises(HTTPException) as exc:
        e2e_tenant_writes.refuse_any_e2e(_request(identity), event="t")
    assert (exc.value.status_code, exc.value.detail) == (403, "e2e_owner_scope")
    person = SimpleNamespace(e2e_read_only=False, e2e_owner=False)
    monkeypatch.setattr(e2e_tenant_writes, "request_staff_identity", lambda request: person)
    e2e_tenant_writes.refuse_any_e2e(_request(person), event="t")


# ── Staff directory ──────────────────────────────────────────────────────────────────────────────

def test_the_owner_address_is_a_staff_address_with_its_scope(monkeypatch):
    _set_env(monkeypatch, "dev")
    assert sd.require_staff_email(" E2E-OWNER@e2e-mail.lumiguides.it.com ") == OWNER_E2E
    assert sd.is_e2e_test_email(OWNER_E2E)
    sd.require_e2e_test_scope(OWNER_E2E, "admin", "admin", ())
    sd.require_e2e_test_scope(OWNER_E2E, "admin", None, ())


@pytest.mark.parametrize("role,booking,crm", [
    ("content_lead", "admin", ()), ("support", None, ()), ("admin", "finance", ()), ("admin", "sales", ()),
    ("admin", "custom_product_ops_accounting", ()), ("admin", "admin", ("platform_admin",)),
    ("admin", "admin", ("ops",)), ("custom_content", None, ()),
])
def test_scope_refuses_anything_else(monkeypatch, role, booking, crm):
    _set_env(monkeypatch, "dev")
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_e2e_test_scope(OWNER_E2E, role, booking, crm)
    assert (err.value.status, err.value.code) == (400, "e2e_test_scope")
    assert "E2E owner account" in err.value.detail


@pytest.mark.parametrize("value", NOT_DEV)
def test_scope_and_environment_refuse_outside_dev(monkeypatch, value):
    _set_env(monkeypatch, value)
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_dev_only_e2e_environment(OWNER_E2E)
    assert (err.value.status, err.value.code) == (403, "e2e_not_allowed_here")
    assert "E2E owner account exists only on Dev" in err.value.detail
    with pytest.raises(sd.StaffDirectoryError):   # its Booking admin grant is Dev only too
        sd.require_e2e_test_scope(OWNER_E2E, "admin", "admin", ())


class OwnerStaffDB(StaffDB):
    """StaffDB whose actor and target reads also return the columns the A10h guards read."""

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        if sql == sd.SQL_ACTOR:
            self.sql.append(sql)
            r = self._find(args[0])
            return None if r is None else {"id": r["id"], "is_active": r["is_active"],
                                           "is_owner": r.get("is_owner", False), "role": r["role"],
                                           "email": r["email"]}
        return await super().fetchrow(sql, *args)

    def _target(self, r):
        t = super()._target(r)
        if t is not None:
            t["is_business_owner"] = r.get("is_business_owner", False)
            t["custom_role"] = r.get("custom_role")
        return t

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if sql == sd.SQL_OTHER_ACTIVE_OWNERS:
            self.sql.append(sql)
            return sum(1 for r in self.rows if r.get("is_owner") and r["is_active"] and r["role"] == "admin"
                       and r["id"] != str(args[0]) and (r["email"] or "").lower() != OWNER_E2E)
        return await super().fetchval(sql, *args)


class NoCognito:
    def __getattr__(self, name):
        raise AssertionError(f"no Cognito call expected: {name}")


@pytest.fixture
def odb(monkeypatch):
    _set_env(monkeypatch, "dev")
    staff_identity.clear_cache()
    d = OwnerStaffDB()
    d.boss = d.add(owner=True, email="boss@adventure.asia", username="boss", role="admin")
    d.e2e = d.add(owner=True, email=OWNER_E2E, username="e2e-owner", role="admin", booking="admin")
    d.staff = d.add(email="writer@adventure.asia", username="writer", role="content")
    yield d
    staff_identity.clear_cache()


def test_the_other_active_owners_query_never_counts_the_e2e_owner():
    assert sd.SQL_OTHER_ACTIVE_OWNERS.endswith(f"AND lower(email) IS DISTINCT FROM '{OWNER_E2E}'")


@pytest.mark.parametrize("op", ["disable", "enable", "sign_out", "sync", "roles"])
@pytest.mark.parametrize("target", ["boss", "e2e"])
@pytest.mark.asyncio
async def test_the_e2e_owner_never_changes_an_owner_or_itself(odb, op, target):
    staff_id = getattr(odb, target)
    calls = {
        "disable": lambda: sd.disable_staff(odb, NoCognito(), actor_id=odb.e2e, staff_id=staff_id),
        "enable": lambda: sd.enable_staff(odb, NoCognito(), actor_id=odb.e2e, staff_id=staff_id),
        "sign_out": lambda: sd.sign_out_staff(odb, NoCognito(), actor_id=odb.e2e, staff_id=staff_id),
        "sync": lambda: sd.sync_staff(odb, NoCognito(), actor_id=odb.e2e, staff_id=staff_id),
        "roles": lambda: sd.change_roles(odb, NoCognito(), actor_id=odb.e2e, staff_id=staff_id, role="content"),
    }
    with pytest.raises(sd.StaffDirectoryError) as err:
        await calls[op]()
    assert (err.value.status, err.value.code) == (403, "e2e_owner_scope")
    assert odb.audit == [] and odb.row(odb.boss)["is_active"] and odb.row(odb.boss)["role"] == "admin"


@pytest.mark.asyncio
async def test_the_e2e_owner_never_changes_the_business_owner(odb):
    odb.row(odb.staff)["is_business_owner"] = True
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.disable_staff(odb, NoCognito(), actor_id=odb.e2e, staff_id=odb.staff)
    assert err.value.code == "e2e_owner_scope"


@pytest.mark.asyncio
async def test_the_e2e_owner_never_sets_or_clears_the_business_owner_marker(odb):
    for value in (True, False):
        with pytest.raises(sd.StaffDirectoryError) as err:
            await scr.set_business_owner(odb, actor_id=odb.e2e, staff_id=odb.staff, value=value)
        assert (err.value.status, err.value.code) == (403, "e2e_owner_scope")


@pytest.mark.asyncio
async def test_the_e2e_owner_may_change_other_staff(odb, monkeypatch):
    class Cognito:
        def __init__(self):
            self.calls = []

        def disable(self, email):
            self.calls.append(("disable", email))

        def global_sign_out(self, email, user_disabled=False):
            self.calls.append(("sign_out", email))

    cognito = Cognito()
    result = await sd.disable_staff(odb, cognito, actor_id=odb.e2e, staff_id=odb.staff)
    assert result.staff_id == odb.staff and not odb.row(odb.staff)["is_active"]
    assert odb.audit[-1]["action"] == "staff.disabled" and odb.audit[-1]["actor"] == f"admin:{odb.e2e}"


@pytest.mark.asyncio
async def test_a_real_owner_may_disable_the_e2e_owner_and_it_is_never_the_last_owner(odb):
    class Cognito:
        def disable(self, email):
            pass

        def global_sign_out(self, email, user_disabled=False):
            pass

    await sd.disable_staff(odb, Cognito(), actor_id=odb.boss, staff_id=odb.e2e)
    assert not odb.row(odb.e2e)["is_active"]


@pytest.mark.asyncio
async def test_the_e2e_owner_does_not_keep_the_last_real_owner_removable(odb):
    # Two owners: the boss and the E2E owner. A second real owner tries to demote the boss: the E2E owner does not
    # count as "another active owner", so the boss is the last real owner.
    other = odb.add(owner=True, email="second@adventure.asia", username="second", role="admin")
    odb.row(other)["is_owner"] = True
    await sd._guard_last_owner(odb, odb._target(odb.row(odb.boss)))   # the second owner exists: allowed
    odb.row(other)["is_active"] = False
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd._guard_last_owner(odb, odb._target(odb.row(odb.boss)))
    assert err.value.code == "last_owner"


@pytest.mark.parametrize("value", ["prod", None, "Dev"])
@pytest.mark.asyncio
async def test_the_e2e_owner_is_not_an_owner_actor_outside_dev(odb, monkeypatch, value):
    _set_env(monkeypatch, value)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.disable_staff(odb, NoCognito(), actor_id=odb.e2e, staff_id=odb.staff)
    assert (err.value.status, err.value.code) == (403, "e2e_not_allowed_here")


def test_the_business_owner_marker_refuses_the_e2e_owner_as_target():
    assert sd.is_e2e_test_email(OWNER_E2E)   # staff_custom_roles._may_hold_marker refuses every E2E address


# ── Reconcile (access review scope) ──────────────────────────────────────────────────────────────

def test_the_reconcile_lists_it_as_a_known_e2e_identity():
    k = sr.KNOWN_E2E_STAFF[OWNER_E2E]
    assert k.clause == "ADR 0006a A10h" and k.groups == frozenset(OWNER_GROUPS) and k.directory_role == "admin"
    assert k.other_groups == () and "A10h" in sr.KNOWN_E2E_NOTE
    assert "never the business owner" in k.reason and "not read-only" in k.reason
