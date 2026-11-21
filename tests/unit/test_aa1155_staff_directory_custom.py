"""AA-1155 spec task T9 (R5.3, R3.3) — the Staff page writes custom roles and the business-owner marker.

- R5.3: a CIS custom role code sets admin_users.custom_role (role = its based_on) and puts the person in cis-custom; a
  Booking custom role code is the Booking grant and the group booking-custom; Sync makes the pool match; switching
  back to a built-in role clears the custom role and removes the custom group. Unknown or archived codes are 400.
- R3.3: the business-owner marker: owners only, at most one holder, never an E2E address, audited before and after.
- The roles catalogue (GET /admin/platform/roles) lists every app's built-in and custom roles, no person.
- A10f / A10g directory rules.

The in-memory directory and the Cognito Stubber of tests/unit/test_aa816_staff_directory_writes.py; invented names.
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg
import pytest

from api.core import e2e_staff, staff_identity, staff_roles
from api.services import staff_custom_roles as scr
from api.services import staff_directory as sd
from tests.unit.test_aa816_staff_directory_writes import (  # noqa: F401 - fixtures
    NEW_EMAIL, StaffDB, _cis_but, _expect, _group, _user, stubbed,
)

MAKER = e2e_staff.E2E_CUSTOM_MAKER_TEST_STAFF_EMAIL
LEAD = e2e_staff.E2E_CIS_CONTENT_LEAD_TEST_STAFF_EMAIL


class CustomStaffDB(StaffDB):
    """StaffDB plus the CIS 462 columns (custom_role, is_business_owner) and the Booking custom roles view
    (``booking_custom``: code -> row; None plays an unreadable view)."""

    def __init__(self) -> None:
        super().__init__()
        self.booking_custom: Optional[dict[str, dict[str, Any]]] = {}

    def _target(self, r):
        out = super()._target(r)
        if out is not None:
            out |= {"custom_role": r.get("custom_role"), "is_business_owner": r.get("is_business_owner", False)}
        return out

    async def fetch(self, sql, *args):
        if sql == staff_roles.SQL_BOOKING_ROLE:
            if self.booking_custom is None:
                raise asyncpg.InsufficientPrivilegeError("permission denied for view v_staff_role_capability")
            role = self.booking_custom.get(args[0])
            return [] if role is None else [
                {"role_code": args[0], "label": role["label"], "based_on": role.get("based_on", "contracting"),
                 "archived": role.get("archived", False), "capability": c}
                for c in role.get("capabilities", ["supply.read"])]
        return await super().fetch(sql, *args)

    async def fetchval(self, sql, *args):
        if sql == scr.SQL_BUSINESS_OWNER_HOLDER:
            return next((r["id"] for r in self.rows if r.get("is_business_owner") and r["id"] != str(args[0])), None)
        out = await super().fetchval(sql, *args)
        if sql == sd.SQL_INSERT:
            self._find(out)["custom_role"] = args[4]
        return out

    async def execute(self, sql, *args):
        if sql == scr.SQL_SET_BUSINESS_OWNER:
            self._find(args[0])["is_business_owner"] = args[1]
            return "UPDATE 1"
        out = await super().execute(sql, *args)
        if sql == sd.SQL_SET_ROLE:
            self._find(args[0])["custom_role"] = args[2]
        return out


@pytest.fixture
def db():
    staff_identity.clear_cache()
    d = CustomStaffDB()
    d.owner = d.add(owner=True, email="owner@adventure.asia", username="owner", role="admin")
    yield d
    staff_identity.clear_cache()


@pytest.fixture
def roles(db):
    db.add_custom_role("custom_content", label="Content", based_on="content_lead",
                       capabilities=sorted(staff_roles.content_seed_capabilities()))
    db.add_custom_role("custom_old", label="Old desk", archived=True)
    db.booking_custom = {"custom_product_ops": {"label": "Product, ops & accounting"},
                         "custom_gone": {"label": "Gone", "archived": True}}
    return db


# ── R5.3: CIS custom role ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_custom_cis_role_keeps_based_on_and_moves_the_person_to_cis_custom(roles, stubbed):
    cognito, stub = stubbed
    uid = roles.add(email=NEW_EMAIL, role="content")
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-custom"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "cis-content"))
    result = await sd.change_roles(roles, cognito, actor_id=roles.owner, staff_id=uid, role="custom_content")
    assert result.status == sd.STATUS_OK
    row = roles.row(uid)
    assert (row["role"], row["custom_role"]) == ("content_lead", "custom_content")
    assert roles.audit[-1]["details"] == {"role": {"before": "content", "after": "custom_content"}}


@pytest.mark.asyncio
async def test_back_to_a_built_in_role_clears_the_custom_role_and_its_group(roles, stubbed):
    cognito, stub = stubbed
    uid = roles.add(email=NEW_EMAIL, role="content_lead", custom_role="custom_content")
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-content"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "cis-custom"))
    await sd.change_roles(roles, cognito, actor_id=roles.owner, staff_id=uid, role="content")
    row = roles.row(uid)
    assert (row["role"], row["custom_role"]) == ("content", None)
    assert roles.audit[-1]["details"]["role"] == {"before": "custom_content", "after": "content"}


@pytest.mark.asyncio
async def test_one_custom_role_to_another_keeps_cis_custom(roles, stubbed):
    cognito, stub = stubbed
    roles.add_custom_role("custom_desk_two", label="Desk two", based_on="content",
                          capabilities=["content.read", "content.write"])
    uid = roles.add(email=NEW_EMAIL, role="content_lead", custom_role="custom_content")
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-custom"))      # added again, never removed
    await sd.change_roles(roles, cognito, actor_id=roles.owner, staff_id=uid, role="custom_desk_two")
    assert roles.row(uid)["custom_role"] == "custom_desk_two" and roles.row(uid)["role"] == "content"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["custom_old", "custom_never"])
async def test_an_archived_or_unknown_custom_role_is_400_and_changes_nothing(roles, stubbed, code):
    cognito, _ = stubbed
    uid = roles.add(email=NEW_EMAIL, role="content")
    with pytest.raises(sd.StaffDirectoryError) as e:
        await sd.change_roles(roles, cognito, actor_id=roles.owner, staff_id=uid, role=code)
    assert (e.value.status, e.value.code) == (400, "custom_role")
    assert roles.row(uid)["role"] == "content" and roles.audit == []


def test_a_custom_code_passes_the_shape_check_and_anything_else_is_400():
    assert sd.require_cis_role("custom_content") == "custom_content"
    assert sd.require_booking_role("custom_product_ops") == "custom_product_ops"
    for bad in ("custom_", "viewer", "Custom_x"):
        with pytest.raises(sd.StaffDirectoryError):
            sd.require_cis_role(bad)
    assert sd.cis_group("custom_content") == "cis-custom" and sd.booking_group("custom_x1") == "booking-custom"
    assert "cis-custom" in sd.CIS_GROUPS and "booking-custom" in sd.BOOKING_GROUPS


# ── R5.3: Booking custom role ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_booking_custom_role_is_the_grant_and_booking_custom(roles, stubbed):
    cognito, stub = stubbed
    uid = roles.add(email=NEW_EMAIL, role="content", booking="sales")
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-custom"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "booking-sales"))
    await sd.change_roles(roles, cognito, actor_id=roles.owner, staff_id=uid, booking_role="custom_product_ops")
    assert roles.grants[(uid, "booking")] == "custom_product_ops"


@pytest.mark.asyncio
async def test_a_booking_custom_role_unknown_archived_or_unreadable_is_refused(roles, stubbed):
    cognito, _ = stubbed
    uid = roles.add(email=NEW_EMAIL, role="content")
    for code in ("custom_gone", "custom_missing"):
        with pytest.raises(sd.StaffDirectoryError) as e:
            await sd.change_roles(roles, cognito, actor_id=roles.owner, staff_id=uid, booking_role=code)
        assert (e.value.status, e.value.code) == (400, "booking_role")
    roles.booking_custom = None
    with pytest.raises(sd.StaffDirectoryError) as e:
        await sd.change_roles(roles, cognito, actor_id=roles.owner, staff_id=uid, booking_role="custom_product_ops")
    assert (e.value.status, e.value.code) == (503, "booking_roles_unavailable")
    assert (uid, "booking") not in roles.grants


@pytest.mark.asyncio
async def test_sync_puts_a_custom_holder_in_both_custom_groups(roles, stubbed):
    cognito, stub = stubbed
    uid = roles.add(email=NEW_EMAIL, role="content_lead", custom_role="custom_content", booking="custom_product_ops")
    _expect(stub, "admin_create_user", {"UserPoolId": "us-west-1_TESTPOOL", "Username": NEW_EMAIL,
                                        "MessageAction": "SUPPRESS",
                                        "UserAttributes": [{"Name": "email", "Value": NEW_EMAIL},
                                                           {"Name": "email_verified", "Value": "true"}]})
    _expect(stub, "admin_enable_user", _user(NEW_EMAIL))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-custom"))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-custom"))
    for g in _cis_but("cis-custom"):
        _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, g))
    for g in sd.BOOKING_GROUPS:
        if g != "booking-custom":
            _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, g))
    for g in sd.CRM_GROUPS:
        _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, g))
    result = await sd.sync_staff(roles, cognito, actor_id=roles.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK


@pytest.mark.asyncio
async def test_create_with_custom_roles_writes_the_code_and_the_custom_groups(roles, stubbed):
    cognito, stub = stubbed
    _expect(stub, "admin_create_user", {"UserPoolId": "us-west-1_TESTPOOL", "Username": NEW_EMAIL,
                                        "MessageAction": "SUPPRESS",
                                        "UserAttributes": [{"Name": "email", "Value": NEW_EMAIL},
                                                           {"Name": "email_verified", "Value": "true"}]})
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-custom"))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-custom"))
    result = await sd.create_staff(roles, cognito, actor_id=roles.owner, email=NEW_EMAIL, display_name="New Person",
                                   role="custom_content", booking_role="custom_product_ops")
    row = roles.row(result.staff_id)
    assert (row["role"], row["custom_role"]) == ("content_lead", "custom_content")
    assert roles.grants[(result.staff_id, "booking")] == "custom_product_ops"


# ── R3.3: the business-owner marker ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_owner_sets_and_clears_the_marker_with_audit(roles):
    uid = roles.add(email=NEW_EMAIL, role="admin")
    await scr.set_business_owner(roles, actor_id=roles.owner, staff_id=uid, value=True)
    assert roles.row(uid)["is_business_owner"] is True
    assert roles.audit[-1]["action"] == scr.BUSINESS_OWNER_CHANGED
    assert roles.audit[-1]["details"] == {"is_business_owner": {"before": False, "after": True}}
    await scr.set_business_owner(roles, actor_id=roles.owner, staff_id=uid, value=False)
    assert roles.row(uid)["is_business_owner"] is False
    assert roles.audit[-1]["details"] == {"is_business_owner": {"before": True, "after": False}}
    n = len(roles.audit)
    await scr.set_business_owner(roles, actor_id=roles.owner, staff_id=uid, value=False)   # no change, no row
    assert len(roles.audit) == n


@pytest.mark.asyncio
async def test_a_second_business_owner_is_409(roles):
    first = roles.add(email="first.person@adventure.asia", role="admin")
    second = roles.add(email="second.person@adventure.asia", role="finance")
    await scr.set_business_owner(roles, actor_id=roles.owner, staff_id=first, value=True)
    with pytest.raises(sd.StaffDirectoryError) as e:
        await scr.set_business_owner(roles, actor_id=roles.owner, staff_id=second, value=True)
    assert (e.value.status, e.value.code) == (409, "business_owner_taken")
    assert not roles.row(second).get("is_business_owner")


@pytest.mark.asyncio
async def test_only_an_owner_sets_the_marker(roles):
    other = roles.add(email="other.admin@adventure.asia", role="admin")
    uid = roles.add(email=NEW_EMAIL, role="admin")
    with pytest.raises(sd.StaffDirectoryError) as e:
        await scr.set_business_owner(roles, actor_id=other, staff_id=uid, value=True)
    assert (e.value.status, e.value.code) == (403, "not_owner")
    assert roles.audit == []


@pytest.mark.asyncio
async def test_never_an_e2e_address_nor_a_disabled_account(roles, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    e2e = roles.add(email=LEAD, role="content_lead")
    with pytest.raises(sd.StaffDirectoryError) as e:
        await scr.set_business_owner(roles, actor_id=roles.owner, staff_id=e2e, value=True)
    assert e.value.code == "e2e_test_scope"
    off = roles.add(email=NEW_EMAIL, role="admin", active=False)
    with pytest.raises(sd.StaffDirectoryError) as e:
        await scr.set_business_owner(roles, actor_id=roles.owner, staff_id=off, value=True)
    assert e.value.code == "inactive"


# ── A10f / A10g ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_a10f_may_hold_exactly_content_lead():
    sd.require_e2e_test_scope(LEAD, "content_lead", None)
    for role, booking, crm in (("content", None, ()), ("custom_content", None, ()),
                               ("content_lead", "sales", ()), ("content_lead", None, ("ops",))):
        with pytest.raises(sd.StaffDirectoryError) as e:
            sd.require_e2e_test_scope(LEAD, role, booking, crm)
        assert e.value.code == "e2e_test_scope"


def test_a10g_may_hold_a_custom_role_based_on_content_lead(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    sd.require_e2e_test_scope(MAKER, "custom_content", "custom_product_ops", based_on="content_lead")
    for role, booking, based_on in (("content_lead", None, None), ("custom_content", "sales", "content_lead"),
                                    ("custom_desk", None, "content")):
        with pytest.raises(sd.StaffDirectoryError):
            sd.require_e2e_test_scope(MAKER, role, booking, based_on=based_on)


def test_a10f_and_a10g_exist_only_on_dev(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    for email in (LEAD, MAKER):
        with pytest.raises(sd.StaffDirectoryError) as e:
            sd.require_dev_only_e2e_environment(email)
        assert e.value.code == e2e_staff.E2E_NOT_ALLOWED_HERE
        assert sd.is_e2e_test_email(email)


# ── R5.3 / R5.4: the roles catalogue ──────────────────────────────────────────────────────────────────────────────

class _CatalogueConn:
    def __init__(self, cis=None, booking=None):
        self.cis, self.booking = cis, booking

    async def fetch(self, sql, *args):
        import asyncpg

        if sql == staff_roles.SQL_CIS_ROLES:
            if self.cis is None:
                raise asyncpg.UndefinedTableError("relation shared.staff_role does not exist")
            return self.cis
        if sql == staff_roles.SQL_BOOKING_ROLES:
            if self.booking is None:
                raise asyncpg.InsufficientPrivilegeError("permission denied")
            return self.booking
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_the_catalogue_lists_every_app_with_built_in_and_custom_roles():
    cis = [{"code": "custom_content", "label": "Content", "based_on": "content_lead", "archived": False,
            "capabilities": ["content.read", "made.up", "tenant.read"]}]
    booking = [{"role_code": "custom_product_ops", "label": "Product, ops & accounting", "based_on": "contracting",
                "archived": False, "capability": c} for c in ("supply.read", "bill.write")]
    out = await staff_roles.roles_catalogue(_CatalogueConn(cis, booking))
    assert [r["code"] for r in out["cis"]["builtin"]][:2] == ["admin", "content_lead"]
    assert {r["code"]: r["assignable"] for r in out["cis"]["builtin"]}["viewer"] is False
    assert out["cis"]["custom"] == [{"code": "custom_content", "label": "Content", "based_on": "content_lead",
                                     "archived": False, "capabilities": ["content.read", "tenant.read"]}]
    assert out["booking"]["custom"][0]["capabilities"] == ["bill.write", "supply.read"]
    assert [r["code"] for r in out["booking"]["builtin"]] == list(sd.BOOKING_ROLES)
    assert [g["code"] for g in out["crm"]["groups"]] == list(sd.CRM_ROLES)
    assert out["tripplanner"] == {"staff_roles": False}
    assert out["cis"]["custom_available"] and out["booking"]["custom_available"]


@pytest.mark.asyncio
async def test_the_catalogue_says_which_custom_list_is_unavailable():
    out = await staff_roles.roles_catalogue(_CatalogueConn(None, None))
    assert out["cis"]["custom"] == [] and out["cis"]["custom_available"] is False
    assert out["booking"]["custom_available"] is False
    assert out["cis"]["builtin"]


def test_the_built_in_labels_cover_every_role_and_carry_no_person_or_issue_id():
    from api.core.staff_authz import ROLE_PERMS

    assert set(staff_roles.CIS_ROLE_LABELS) == set(ROLE_PERMS)
    assert set(staff_roles.BOOKING_ROLE_LABELS) == set(sd.BOOKING_ROLES)
    assert set(staff_roles.CRM_GROUP_LABELS) == set(sd.CRM_ROLES)
    import re
    for label in [*staff_roles.CIS_ROLE_LABELS.values(), *staff_roles.BOOKING_ROLE_LABELS.values(),
                  *staff_roles.CRM_GROUP_LABELS.values()]:
        assert not re.search(r"AA-\d+|e2e", label, re.I)
