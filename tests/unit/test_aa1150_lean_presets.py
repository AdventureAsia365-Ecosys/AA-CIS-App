"""AA-1150 (lean staff roles step 1): the Staff page's three people-role presets and the retired Booking role.

- ``business_owner`` = CIS admin + Booking admin, CRM roles untouched; ``content_ops`` ("Content + Product &
  operations") = CIS content_lead, NO Booking role and no CRM role (tech lead decision Q1, S285); "Administrator" is
  the existing ``platform_owner`` preset. One audit row with ``details.preset``; the owner flag never changes; the
  self-demote and last-owner guards apply to ``content_ops``.
- Booking ``content_creator`` is retired: the directory refuses to grant it (service 400, route 422), Sync deletes an
  old grant in the same audited transaction and removes ``booking-content_creator`` from the user, and the Pricing
  team preset treats such a grant as an empty slot.
"""
from __future__ import annotations

import pytest

from api.services import staff_directory as sd
from tests.unit.test_aa816_staff_directory_writes import (  # noqa: F401 — api, db, issuer and stubbed are fixtures
    NEW_EMAIL, _cis_but, _create, _expect, _expect_error, _group, _h, _user, api, db, issuer, stubbed,
)


def test_the_constants():
    assert sd.BOOKING_ROLES == ("admin", "sales", "contracting", "finance")
    assert sd.RETIRED_BOOKING_ROLES == ("content_creator",)
    assert sd.BOOKING_GROUPS[-1] == "booking-content_creator"   # still removed by Sync, after the assignable ones
    assert {sd.PRESET_PLATFORM_OWNER, sd.PRESET_PRICING_TEAM, sd.PRESET_BUSINESS_OWNER, sd.PRESET_CONTENT_OPS} == set(
        sd.PRESETS)


# ── Presets ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_business_owner_preset_gives_cis_and_booking_admin_and_keeps_crm(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content", booking="sales", crm=("support",))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-admin"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "cis-content"))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-admin"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "booking-sales"))
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="business_owner")
    assert result.status == sd.STATUS_OK
    assert db.row(uid)["role"] == "admin" and db.grants[(uid, "booking")] == "admin"
    assert db.crm_roles(uid) == ["support"] and db.row(uid)["is_owner"] is False
    assert db.audit[-1]["details"] == {"role": {"before": "content", "after": "admin"},
                                       "booking_role": {"before": "sales", "after": "admin"},
                                       "preset": "business_owner"}
    stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_content_ops_preset_gives_content_lead_and_removes_booking_and_crm(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content", booking="contracting", crm=("ops",))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-content_lead"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "cis-content"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "booking-contracting"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "crm-ops"))
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="content_ops")
    assert result.status == sd.STATUS_OK
    assert db.row(uid)["role"] == "content_lead" and (uid, "booking") not in db.grants and db.crm_roles(uid) == []
    assert db.audit[-1]["details"] == {"role": {"before": "content", "after": "content_lead"},
                                       "booking_role": {"before": "contracting", "after": None},
                                       "crm_roles": {"before": ["ops"], "after": []},
                                       "preset": "content_ops"}
    stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_content_ops_preset_is_a_no_op_when_in_place(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content_lead")
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-content_lead"))   # the add is idempotent
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="content_ops")
    assert result.status == sd.STATUS_OK and db.audit == []
    stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_content_ops_preset_never_demotes_oneself(db, stubbed):
    cognito, stub = stubbed
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=db.owner, preset="content_ops")
    assert (err.value.status, err.value.code) == (400, "self_demote")
    assert db.row(db.owner)["role"] == "admin" and db.audit == []
    stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_presets_are_sent_alone(db, stubbed):
    cognito, _ = stubbed
    uid = db.add(email=NEW_EMAIL, role="content")
    for preset in ("business_owner", "content_ops"):
        with pytest.raises(sd.StaffDirectoryError) as err:
            await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset=preset, booking_role="admin")
        assert (err.value.status, err.value.code) == (400, "preset_conflict")
    assert db.audit == []


@pytest.mark.asyncio
async def test_content_ops_needs_the_crm_table(db, stubbed):
    cognito, _ = stubbed
    db.crm_table = False
    uid = db.add(email=NEW_EMAIL, role="content")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="content_ops")
    assert (err.value.status, err.value.code) == (503, "crm_unavailable")


# ── Retired content_creator ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_content_creator_is_never_granted_again(db, stubbed):
    cognito, _ = stubbed
    uid = db.add(email=NEW_EMAIL, role="content")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, booking_role="content_creator")
    assert (err.value.status, err.value.code) == (400, "booking_role")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email="another@adventure.asia", display_name="A",
                              role="content", booking_role="content_creator")
    assert err.value.code == "booking_role"
    assert db.audit == []


@pytest.mark.asyncio
async def test_sync_retires_a_content_creator_grant_and_removes_its_group(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content", booking="content_creator")
    _expect_error(stub, "admin_create_user", "UsernameExistsException", _create())
    _expect(stub, "admin_enable_user", _user(NEW_EMAIL))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-content"))
    for g in _cis_but("cis-content") + sd.BOOKING_GROUPS + sd.CRM_GROUPS:   # booking-content_creator included
        _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, g))
    result = await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK and "remove_group:booking-content_creator" in result.steps
    assert (uid, "booking") not in db.grants
    assert db.actions() == [sd.StaffAudit.ROLES_CHANGED, sd.StaffAudit.COGNITO_SYNCED]
    assert db.audit[0]["details"] == {"booking_role": {"before": "content_creator", "after": None},
                                      "retired_booking_role": "content_creator"}
    stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_sync_of_a_disabled_row_also_retires_the_grant(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content", booking="content_creator", active=False)
    _expect(stub, "admin_disable_user", _user(NEW_EMAIL))
    _expect(stub, "admin_user_global_sign_out", _user(NEW_EMAIL))
    result = await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK and (uid, "booking") not in db.grants
    assert db.actions()[0] == sd.StaffAudit.ROLES_CHANGED


@pytest.mark.asyncio
async def test_a_role_change_removes_the_retired_group(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="content", booking="content_creator")
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-sales"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "booking-content_creator"))
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, booking_role="sales")
    assert result.steps == ["add_group:booking-sales", "remove_group:booking-content_creator"]
    assert db.grants[(uid, "booking")] == "sales"


@pytest.mark.asyncio
async def test_pricing_team_preset_fills_a_retired_grant(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=NEW_EMAIL, role="customer_success", booking="content_creator")
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-contracting"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "booking-content_creator"))
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="pricing_team")
    assert result.status == sd.STATUS_OK and db.grants[(uid, "booking")] == "contracting"
    assert db.audit[-1]["details"] == {"booking_role": {"before": "content_creator", "after": "contracting"},
                                       "preset": "pricing_team"}


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────

def test_route_applies_the_lean_presets_and_refuses_content_creator(api, db):
    client, stub, token = api
    uid = db.add(email=NEW_EMAIL, role="content")
    for op, g in (("admin_add_user_to_group", "cis-content_lead"), ("admin_remove_user_from_group", "cis-content")):
        _expect(stub, op, _group(NEW_EMAIL, g))
    resp = client.patch(f"/admin/platform/staff/{uid}/roles", headers=_h(token), json={"preset": "content_ops"})
    assert resp.status_code == 200, resp.text
    staff = resp.json()["staff"]
    assert (staff["role"], staff["booking_role"], staff["crm_roles"], staff["is_owner"]) == (
        "content_lead", None, [], False)
    for op, g in (("admin_add_user_to_group", "cis-admin"), ("admin_remove_user_from_group", "cis-content_lead"),
                  ("admin_add_user_to_group", "booking-admin")):
        _expect(stub, op, _group(NEW_EMAIL, g))
    resp = client.patch(f"/admin/platform/staff/{uid}/roles", headers=_h(token), json={"preset": "business_owner"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["staff"]["role"], resp.json()["staff"]["booking_role"]) == ("admin", "admin")
    resp = client.patch(f"/admin/platform/staff/{uid}/roles", headers=_h(token),
                        json={"booking_role": "content_creator"})
    assert resp.status_code == 422, resp.text
    stub.assert_no_pending_responses()
