"""AA-1155 spec task T14 (root spec R5.7): the Staff page's people-role presets as data — api/services/staff_presets.py,
the directory's preset apply and the /admin/platform/presets routes.

The presets are rows (CIS 465, seeded with the four presets the code held); an is_owner user edits them, audited before
and after; the directory applies one by code. The code keeps only the guards: a preset never sets is_owner (no field
can hold it), the self-demote and last-owner checks, and an additive-only preset never replaces a held role (409
preset_would_replace). The behaviour tests of the four presets themselves stay in test_aa816 / test_aa1150 /
test_aa1091 (they now read the seeded rows). Invented codes and labels only."""
from __future__ import annotations

import dataclasses

import pytest

from api.core import staff_identity
from api.services import staff_directory as sd
from api.services import staff_presets as sp
from tests.unit.aa1155_editor_db import EditorDB
from tests.unit.aa1155_presets_seed import SEED_PRESETS
from tests.unit.test_aa816_staff_directory_writes import (  # noqa: F401 — api, issuer and stubbed are fixtures
    NEW_EMAIL, _expect, _group, _h, api, issuer, stubbed,
)

SEED = {p.code: p for p in SEED_PRESETS}


@pytest.fixture
def db():
    staff_identity.clear_cache()
    d = EditorDB()
    d.owner = d.add(owner=True, email="owner@adventure.asia", username="owner", role="admin")
    yield d
    staff_identity.clear_cache()


# ── plan(): what a preset changes ─────────────────────────────────────────────────────────────────

def test_a_preset_has_no_owner_field():
    assert not any("owner" in f.name for f in dataclasses.fields(sp.Preset))
    assert not any("owner" in f for f in sp.EDITABLE)


@pytest.mark.parametrize("code, held, expected", [
    ("platform_owner", ("content", "sales", ("support",)), ("admin", "admin", ("platform_admin", "support"))),
    ("business_owner", ("content", None, ("ops",)), ("admin", "admin", sp.KEEP)),
    ("content_ops", ("admin", "contracting", ("ops",)), ("content_lead", None, ())),
    ("pricing_team", ("viewer", None, ()), ("customer_success", "contracting", sp.KEEP)),
    ("pricing_team", ("content_lead", "admin", ()), (sp.KEEP, sp.KEEP, sp.KEEP)),          # rights kept
    ("pricing_team", ("customer_success", "content_creator", ()), (sp.KEEP, "contracting", sp.KEEP)),   # retired
])
def test_the_seeded_presets_plan(code, held, expected):
    out = sp.plan(SEED[code], *held)
    assert (out.role, out.booking, out.crm) == expected and out.lost == []


@pytest.mark.parametrize("held, lost", [
    (("finance", None), ["CIS finance"]),
    (("viewer", "sales"), ["Booking sales"]),
    (("custom_zz_role", "custom_zz_ops"), ["CIS custom_zz_role", "Booking custom_zz_ops"]),   # custom: never replaced
])
def test_an_additive_preset_never_replaces_a_held_role(held, lost):
    assert sp.plan(SEED["pricing_team"], *held).lost == lost


def test_a_preset_may_grant_custom_roles():
    p = sp.Preset(code="zz_maker", label="Invented maker", cis_role="custom_zz_role", booking_mode="set",
                  booking_role="custom_zz_ops", crm_mode="replace")
    out = sp.plan(p, "content", "sales", ("ops",))
    assert (out.role, out.booking, out.crm, out.lost) == ("custom_zz_role", "custom_zz_ops", (), [])


# ── The directory applies a preset row ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_edited_preset_label_and_role_are_what_the_directory_applies(db, stubbed):
    cognito, stub = stubbed
    await sp.update_preset(db, actor_id=db.owner, code="business_owner",
                           fields={"label": "Invented owners", "booking_mode": "set", "booking_role": "finance"})
    uid = db.add(email=NEW_EMAIL, role="content")
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "cis-admin"))
    _expect(stub, "admin_remove_user_from_group", _group(NEW_EMAIL, "cis-content"))
    _expect(stub, "admin_add_user_to_group", _group(NEW_EMAIL, "booking-finance"))
    await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="business_owner")
    assert db.grants[(uid, "booking")] == "finance" and db.row(uid)["is_owner"] is False
    assert db.audit[-1]["details"]["preset"] == "business_owner"
    stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_an_archived_or_unknown_preset_is_refused(db, stubbed):
    cognito, _ = stubbed
    uid = db.add(email=NEW_EMAIL, role="content")
    db.presets["content_ops"]["archived"] = True
    for code in ("content_ops", "zz_missing"):
        with pytest.raises(sd.StaffDirectoryError) as err:
            await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset=code)
        assert (err.value.status, err.value.code) == (400, "preset")
    assert db.row(uid)["role"] == "content" and db.audit == []


@pytest.mark.asyncio
async def test_an_additive_preset_naming_the_label_refuses_before_any_write(db, stubbed):
    cognito, _ = stubbed
    uid = db.add(email=NEW_EMAIL, role="finance", booking="sales")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="pricing_team")
    assert (err.value.status, err.value.code) == (409, "preset_would_replace")
    assert "Pricing team" in err.value.detail and "CIS finance" in err.value.detail
    assert db.audit == [] and db.grants[(uid, "booking")] == "sales"


@pytest.mark.asyncio
async def test_a_preset_that_demotes_still_meets_the_self_demote_guard(db, stubbed):
    cognito, _ = stubbed
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=db.owner, preset="content_ops")
    assert (err.value.status, err.value.code) == (400, "self_demote")
    assert db.row(db.owner)["role"] == "admin" and db.row(db.owner)["is_owner"] is True


@pytest.mark.asyncio
async def test_a_preset_table_that_cannot_be_read_is_a_503(db, stubbed):
    import asyncpg

    cognito, _ = stubbed
    uid = db.add(email=NEW_EMAIL, role="content")

    async def broken(sql, *args):
        raise asyncpg.UndefinedTableError("relation shared.staff_role_preset does not exist")

    original = db.fetchrow

    async def fetchrow(sql, *args):
        if sql == sp.SQL_PRESET:
            return await broken(sql, *args)
        return await original(sql, *args)

    db.fetchrow = fetchrow
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="business_owner")
    assert (err.value.status, err.value.code) == (503, "presets_unavailable")


# ── The preset editor ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_a_preset_with_custom_roles_is_audited(db):
    db.add_custom_role("custom_zz_role", label="Invented", based_on="content_lead")
    db.add_booking_role("custom_zz_ops")
    preset = await sp.create_preset(db, actor_id=db.owner, fields={
        "code": "zz_maker", "label": "Invented maker", "cis_role": "custom_zz_role", "booking_mode": "set",
        "booking_role": "custom_zz_ops", "crm_mode": "replace", "crm_roles": [], "sort_order": 50})
    assert preset.cis_role == "custom_zz_role" and db.presets["zz_maker"]["booking_role"] == "custom_zz_ops"
    assert db.audit[-1]["action"] == sp.PRESET_CREATED and db.audit[-1]["resource_id"] == "zz_maker"
    assert db.audit[-1]["details"]["after"]["label"] == "Invented maker"
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sp.create_preset(db, actor_id=db.owner, fields={"code": "zz_maker", "label": "Again",
                                                              "cis_role": "content"})
    assert (err.value.status, err.value.code) == (409, "preset_exists")


@pytest.mark.parametrize("fields, code", [
    ({"cis_role": "viewer"}, "cis_role"),
    ({"cis_role": "custom_zz_gone"}, "custom_role"),                  # not an active CIS custom role
    ({"booking_mode": "set", "booking_role": "custom_zz_gone"}, "booking_role"),
    ({"booking_mode": "set"}, "booking_mode"),
    ({"booking_mode": "clear", "booking_role": "sales"}, "booking_mode"),
    ({"crm_mode": "add", "crm_roles": []}, "crm_mode"),
    ({"crm_mode": "keep", "crm_roles": ["ops"]}, "crm_mode"),
    ({"crm_mode": "replace", "crm_roles": ["boss"]}, "crm_role"),
    ({"additive_only": True, "booking_mode": "clear"}, "additive_conflict"),
    ({"additive_only": False, "cis_keep_roles": ["admin"]}, "additive_conflict"),
    ({"additive_only": True, "cis_keep_roles": ["superuser"]}, "invalid_cis_keep_roles"),
    ({"cis_role": None, "booking_mode": "keep", "crm_mode": "keep"}, "empty"),
    ({"label": " "}, "invalid_label"),
    ({"description": "line\nbreak"}, "invalid_description"),
    ({"sort_order": 5000}, "sort_order"),
    ({"is_owner": True}, "preset_field"),                             # a preset never sets the owner flag
])
@pytest.mark.asyncio
async def test_the_editor_refuses_bad_presets_before_any_write(db, fields, code):
    before = dict(db.presets["content_ops"])
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sp.update_preset(db, actor_id=db.owner, code="content_ops", fields=fields)
    assert (err.value.status, err.value.code) == (400, code)
    assert db.presets["content_ops"] == before and db.audit == []


@pytest.mark.asyncio
async def test_update_is_audited_before_and_after_and_a_no_op_writes_nothing(db):
    after = await sp.update_preset(db, actor_id=db.owner, code="pricing_team",
                                   fields={"label": "Invented pricing", "archived": True})
    assert after.label == "Invented pricing" and after.archived is True
    details = db.audit[-1]["details"]
    assert db.audit[-1]["action"] == sp.PRESET_UPDATED
    assert details["before"]["label"] == "Pricing team" and details["after"]["label"] == "Invented pricing"
    assert details["before"]["archived"] is False and details["after"]["archived"] is True
    n = len(db.audit)
    await sp.update_preset(db, actor_id=db.owner, code="pricing_team", fields={"label": "Invented pricing"})
    assert len(db.audit) == n
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sp.update_preset(db, actor_id=db.owner, code="zz_missing", fields={"label": "X"})
    assert (err.value.status, err.value.code) == (404, "preset_not_found")


async def _dead_role_preset(db):
    """A preset naming a CIS and a Booking custom role, both archived since (the Booking one in Booking)."""
    db.add_custom_role("custom_zz_role", label="Invented", based_on="content_lead")
    db.add_booking_role("custom_zz_ops")
    await sp.create_preset(db, actor_id=db.owner, fields={
        "code": "zz_dead", "label": "Invented dead", "cis_role": "custom_zz_role", "booking_mode": "set",
        "booking_role": "custom_zz_ops"})
    db.custom_roles["custom_zz_role"]["archived"] = True
    db.booking_custom["custom_zz_ops"]["archived"] = True


@pytest.mark.parametrize("fields", [
    {"archived": True},
    {"label": "Invented retired"},
    {"description": "Invented note", "sort_order": 7},
    {"booking_mode": "clear", "booking_role": None},   # the CIS role is not re-checked: its field did not change
])
@pytest.mark.asyncio
async def test_a_preset_whose_custom_role_was_archived_can_still_be_retired(db, fields):
    await _dead_role_preset(db)
    after = await sp.update_preset(db, actor_id=db.owner, code="zz_dead", fields=fields)
    assert db.audit[-1]["action"] == sp.PRESET_UPDATED and db.audit[-1]["resource_id"] == "zz_dead"
    assert all(getattr(after, k) == v for k, v in fields.items())


@pytest.mark.parametrize("fields, code", [
    ({"cis_role": "custom_zz_role2"}, "custom_role"),                       # a changed role is re-checked
    ({"booking_role": "custom_zz_gone"}, "booking_role"),
])
@pytest.mark.asyncio
async def test_a_changed_or_restored_preset_role_must_be_live(db, fields, code):
    await _dead_role_preset(db)
    db.add_custom_role("custom_zz_role2", label="Invented two", archived=True)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sp.update_preset(db, actor_id=db.owner, code="zz_dead", fields=fields)
    assert (err.value.status, err.value.code) == (400, code)
    await sp.update_preset(db, actor_id=db.owner, code="zz_dead", fields={"archived": True})
    with pytest.raises(sd.StaffDirectoryError) as err:                      # restoring re-checks every role
        await sp.update_preset(db, actor_id=db.owner, code="zz_dead", fields={"archived": False})
    assert (err.value.status, err.value.code) == (400, "custom_role")
    assert db.presets["zz_dead"]["archived"] is True


@pytest.mark.asyncio
async def test_an_unreadable_booking_view_does_not_block_a_relabel_or_archive(db):
    db.add_booking_role("custom_zz_ops")
    await sp.create_preset(db, actor_id=db.owner, fields={
        "code": "zz_ops", "label": "Invented ops", "booking_mode": "set", "booking_role": "custom_zz_ops"})
    db.booking_unreadable = True
    await sp.update_preset(db, actor_id=db.owner, code="zz_ops", fields={"label": "Invented ops two"})
    await sp.update_preset(db, actor_id=db.owner, code="zz_ops", fields={"archived": True})
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sp.update_preset(db, actor_id=db.owner, code="zz_ops", fields={"archived": False})
    assert (err.value.status, err.value.code) == (503, "booking_roles_unavailable")


@pytest.mark.asyncio
async def test_only_an_owner_edits_presets(db):
    other = db.add(email="lead@adventure.asia", username="lead", role="admin")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sp.update_preset(db, actor_id=other, code="content_ops", fields={"label": "X"})
    assert (err.value.status, err.value.code) == (403, "not_owner")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sp.create_preset(db, actor_id=other, fields={"code": "zz_x", "label": "X", "cis_role": "content"})
    assert err.value.status == 403 and db.audit == [] and "zz_x" not in db.presets


# ── The routes ─────────────────────────────────────────────────────────────────────────────────────

def test_the_preset_routes(api, db):
    client, _, token = api
    resp = client.get("/admin/platform/presets", headers=_h(token))
    body = resp.json()
    assert resp.status_code == 200 and body["available"] is True and body["can_edit"] is True
    assert [p["code"] for p in body["presets"]] == ["platform_owner", "business_owner", "content_ops", "pricing_team"]
    for path, method, payload, status in (
            ("/admin/platform/presets/content_ops", "patch", {"label": "Invented content"}, 200),
            ("/admin/platform/presets/content_ops", "patch", {"is_owner": True}, 422),
            ("/admin/platform/presets/content_ops", "patch", {"crm_mode": "drop"}, 422),
            ("/admin/platform/presets/content_ops", "patch", {"booking_mode": "set"}, 400),
            ("/admin/platform/presets/content_ops", "patch", {"cis_role": "viewer"}, 422),
            ("/admin/platform/presets", "post", {"code": "zz_new", "label": "Invented", "cis_role": "support"}, 201),
            ("/admin/platform/presets", "post", {"code": "zz_new", "label": "Invented", "cis_role": "support"}, 409),
            ("/admin/platform/presets", "post", {"code": "zz_bad", "label": "Invented", "is_owner": True}, 422)):
        resp = getattr(client, method)(path, headers=_h(token), json=payload)
        assert resp.status_code == status, (path, payload, resp.text)
    assert db.presets["content_ops"]["label"] == "Invented content" and db.presets["zz_new"]["cis_role"] == "support"


def test_a_non_owner_reads_presets_and_cannot_edit(api, db, issuer):
    client, _, _ = api
    sub = "cccccccc-0000-4000-8000-00000000000c"
    db.add(email="other.admin@adventure.asia", username="other", role="admin", iss=issuer.iss, sub=sub)
    other = issuer.token(sub=sub, email="other.admin@adventure.asia", **{"cognito:groups": ["cis-admin"]})
    resp = client.get("/admin/platform/presets", headers=_h(other))
    assert resp.status_code == 200 and resp.json()["can_edit"] is False
    resp = client.patch("/admin/platform/presets/content_ops", headers=_h(other), json={"label": "X"})
    assert resp.status_code == 403 and db.audit == []
