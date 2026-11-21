"""AA-1155 spec task T13 (root spec R5.6): the CIS role editor — api/services/staff_role_editor.py and the
/admin/platform/roles/cis routes.

The Booking editor's rules (AA-Booking app/auth/role_editor.py, task T6) on CIS: clone a built-in role (never admin,
viewer or reviewer), tick capabilities, relabel, archive; 400 on an approve-class, staff-directory or unknown
capability; owners only (403 for anyone else, before any write); one audit row with the role before and after. CIS adds
three guards because a CIS holder is refused when its role is empty or archived: no empty role (400), no archive while
an active account holds the role (409) or an active preset grants it (409). Invented role codes and labels only."""
from __future__ import annotations

import pytest

from api.core import staff_authz, staff_identity, staff_roles
from api.services import staff_directory as sd
from api.services import staff_role_editor as editor
from tests.unit.aa1155_editor_db import EditorDB
from tests.unit.test_aa816_staff_directory_writes import (  # noqa: F401 — api and issuer are fixtures
    NEW_EMAIL, _h, api, issuer,
)

CODE = "custom_zz_writer"


@pytest.fixture
def db():
    staff_identity.clear_cache()
    d = EditorDB()
    d.owner = d.add(owner=True, email="owner@adventure.asia", username="owner", role="admin")
    yield d
    staff_identity.clear_cache()


async def _create(db, **kw):
    args = {"code": CODE, "label": "Invented writers", "based_on": "content"} | kw
    return await editor.create_role(db, actor_id=db.owner, **args)


# ── The rules ──────────────────────────────────────────────────────────────────────────────────────

def test_bases_are_the_built_in_roles_but_admin_and_viewer():
    assert set(editor.bases()) == set(staff_authz.ROLE_PERMS) - {"admin", "viewer"}
    assert "reviewer" not in editor.bases()


def test_a_clone_never_holds_an_approval_or_the_staff_directory():
    for base in editor.bases():
        caps = editor.clone_of(base)
        assert caps <= staff_roles.custom_allowed()
        assert not any(staff_roles.is_approve_class(c) for c in caps)
        assert caps == staff_authz.ROLE_PERMS[base] & staff_roles.custom_allowed()


@pytest.mark.parametrize("caps, code", [
    (["content.read", "invented.capability"], "unknown_capability"),
    (["content.read", "settings.approve"], "approve_class_capability"),
    (["staff.write"], "approve_class_capability"),
    (["staff.admin"], "unknown_capability"),                 # a Booking name: CIS does not know it
    (["content.approve"], "unknown_capability"),             # unknown first, like the Booking editor
    ([], "no_capabilities"),
    ("content.read", "invalid_capabilities"),
    ([1, 2], "invalid_capabilities"),
])
def test_capabilities_are_checked(caps, code):
    with pytest.raises(sd.StaffDirectoryError) as err:
        editor.check_capabilities(caps)
    assert (err.value.status, err.value.code) == (400, code)


@pytest.mark.parametrize("fn, value, code", [
    (editor.check_code, "content", "invalid_role_code"),
    (editor.check_code, "custom_X", "invalid_role_code"),
    (editor.check_label, "  ", "invalid_label"),
    (editor.check_label, "x" * 81, "invalid_label"),
    (editor.check_label, "tab\there", "invalid_label"),
    (editor.check_base, "admin", "unknown_base_role"),
    (editor.check_base, "viewer", "unknown_base_role"),
    (editor.check_base, "reviewer", "unknown_base_role"),
    (editor.check_base, "custom_content", "unknown_base_role"),
])
def test_codes_labels_and_bases_are_checked(fn, value, code):
    with pytest.raises(sd.StaffDirectoryError) as err:
        fn(value)
    assert (err.value.status, err.value.code) == (400, code)


# ── The service ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_clones_the_base_and_is_audited(db):
    role = await _create(db)
    assert role["based_on"] == "content" and role["archived"] is False and role["holders"] == 0
    assert set(role["capabilities"]) == editor.clone_of("content")
    assert db.audit[-1]["action"] == editor.ROLE_CREATED
    assert db.audit[-1]["resource_type"] == "staff_role" and db.audit[-1]["resource_id"] == CODE
    assert db.audit[-1]["details"]["after"]["capabilities"] == sorted(editor.clone_of("content"))
    assert db.audit[-1]["actor"] == f"admin:{db.owner}"


@pytest.mark.asyncio
async def test_create_with_ticked_capabilities_and_a_duplicate(db):
    role = await _create(db, capabilities=["content.read", "tenant.read"])
    assert role["capabilities"] == ["content.read", "tenant.read"]
    with pytest.raises(sd.StaffDirectoryError) as err:
        await _create(db)
    assert (err.value.status, err.value.code) == (409, "role_exists")
    assert len(db.audit) == 1


@pytest.mark.asyncio
async def test_create_refuses_approve_class_before_any_write(db):
    with pytest.raises(sd.StaffDirectoryError) as err:
        await _create(db, capabilities=["content.read", "settings.approve"])
    assert (err.value.status, err.value.code) == (400, "approve_class_capability")
    assert CODE not in db.custom_roles and db.audit == [] and db.writes == 0


@pytest.mark.asyncio
async def test_only_an_owner_writes(db):
    other = db.add(email="lead@adventure.asia", username="lead", role="admin")   # admin, not an owner
    with pytest.raises(sd.StaffDirectoryError) as err:
        await editor.create_role(db, actor_id=other, code=CODE, label="X", based_on="content")
    assert (err.value.status, err.value.code) == (403, "not_owner")
    db.add_custom_role(CODE, label="Invented", based_on="content")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await editor.update_role(db, actor_id=other, code=CODE, label="Y")
    assert err.value.status == 403 and db.audit == [] and db.writes == 0


@pytest.mark.asyncio
async def test_update_ticks_capabilities_relabels_and_audits_before_and_after(db):
    db.add_custom_role(CODE, label="Invented", based_on="content", capabilities=("content.read", "tenant.read"))
    role = await editor.update_role(db, actor_id=db.owner, code=CODE, label="Invented editors",
                                    capabilities=["content.read", "content.write"])
    assert role["label"] == "Invented editors" and role["capabilities"] == ["content.read", "content.write"]
    details = db.audit[-1]["details"]
    assert db.audit[-1]["action"] == editor.ROLE_UPDATED
    assert details["before"]["capabilities"] == ["content.read", "tenant.read"]
    assert details["after"]["capabilities"] == ["content.read", "content.write"]
    assert details["before"]["label"] == "Invented" and details["after"]["label"] == "Invented editors"
    assert "+content.write" in details["summary"] and "-tenant.read" in details["summary"]


@pytest.mark.asyncio
async def test_update_refuses_bad_capabilities_and_unknown_roles(db):
    db.add_custom_role(CODE, label="Invented", based_on="content")
    for caps, code in ((["settings.approve"], "approve_class_capability"), (["nope.read"], "unknown_capability"),
                       ([], "no_capabilities")):
        with pytest.raises(sd.StaffDirectoryError) as err:
            await editor.update_role(db, actor_id=db.owner, code=CODE, capabilities=caps)
        assert (err.value.status, err.value.code) == (400, code)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await editor.update_role(db, actor_id=db.owner, code="custom_zz_missing", label="X")
    assert (err.value.status, err.value.code) == (404, "role_not_found")
    assert db.audit == []


@pytest.mark.asyncio
async def test_a_no_op_update_writes_nothing(db):
    db.add_custom_role(CODE, label="Invented", based_on="content", capabilities=("content.read",))
    role = await editor.update_role(db, actor_id=db.owner, code=CODE, label="Invented", capabilities=["content.read"],
                                    archived=False)
    assert role["label"] == "Invented" and db.audit == [] and db.writes == 0


@pytest.mark.asyncio
async def test_archive_and_restore_and_never_while_held(db):
    db.add_custom_role(CODE, label="Invented", based_on="content")
    holder = db.add(email=NEW_EMAIL, username="holder", role="content", custom_role=CODE)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await editor.update_role(db, actor_id=db.owner, code=CODE, archived=True, label="Renamed")
    assert (err.value.status, err.value.code) == (409, "role_in_use")
    assert db.custom_roles[CODE]["label"] == "Invented" and db.audit == []   # rolled back, label included
    db.row(holder)["is_active"] = False                                     # a disabled account holds nothing
    role = await editor.update_role(db, actor_id=db.owner, code=CODE, archived=True)
    assert role["archived"] is True and db.audit[-1]["details"]["summary"] == "archived"
    role = await editor.update_role(db, actor_id=db.owner, code=CODE, archived=False)
    assert role["archived"] is False and db.audit[-1]["details"]["summary"] == "restored"


@pytest.mark.asyncio
async def test_no_archive_while_an_active_preset_grants_the_role(db):
    from api.services import staff_presets as sp   # noqa: PLC0415

    db.add_custom_role(CODE, label="Invented", based_on="content")
    await sp.create_preset(db, actor_id=db.owner, fields={"code": "zz_writers", "label": "Invented preset",
                                                          "cis_role": CODE})
    await sp.create_preset(db, actor_id=db.owner, fields={      # a keep list naming the role does not block
        "code": "zz_keeper", "label": "Invented keeper", "cis_role": "content", "additive_only": True,
        "cis_keep_roles": [CODE]})
    n = len(db.audit)
    with pytest.raises(sd.StaffDirectoryError) as err:
        await editor.update_role(db, actor_id=db.owner, code=CODE, archived=True, label="Renamed")
    assert (err.value.status, err.value.code) == (409, "role_in_use_by_preset")
    assert "Invented preset" in err.value.detail and "Invented keeper" not in err.value.detail
    assert db.custom_roles[CODE]["label"] == "Invented" and len(db.audit) == n
    await sp.update_preset(db, actor_id=db.owner, code="zz_writers", fields={"archived": True})
    role = await editor.update_role(db, actor_id=db.owner, code=CODE, archived=True)
    assert role["archived"] is True


@pytest.mark.asyncio
async def test_the_editor_view(db):
    db.add_custom_role(CODE, label="Invented", based_on="content")
    db.add(email=NEW_EMAIL, username="holder", role="content", custom_role=CODE)
    view = await editor.editor_view(db, can_edit=True)
    assert view["can_edit"] is True and view["custom_available"] is True
    assert [r["code"] for r in view["custom"]] == [CODE] and view["custom"][0]["holders"] == 1
    clonable = {r["code"] for r in view["builtin"] if r["clonable"]}
    assert clonable == set(editor.bases())
    caps = {c["name"]: c["assignable"] for c in view["capabilities"]}
    assert set(caps) == staff_authz.CAPABILITIES
    assert caps["staff.write"] is False and caps["content.read"] is True
    assert {c for c, ok in caps.items() if ok} == staff_roles.custom_allowed()


# ── The routes ─────────────────────────────────────────────────────────────────────────────────────

def test_the_routes_create_edit_and_refuse(api, db):
    client, _, token = api
    resp = client.get("/admin/platform/roles/cis", headers=_h(token))
    assert resp.status_code == 200 and resp.json()["can_edit"] is True
    resp = client.post("/admin/platform/roles/cis", headers=_h(token),
                       json={"code": CODE, "label": "Invented writers", "based_on": "content_lead"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["based_on"] == "content_lead"
    for body, status in (({"capabilities": ["content.read", "settings.approve"]}, 400),
                         ({"capabilities": ["content.read", "made.up"]}, 400),
                         ({"is_owner": True}, 422),
                         ({"label": "Renamed"}, 200),
                         ({"archived": True}, 200)):
        resp = client.patch(f"/admin/platform/roles/cis/{CODE}", headers=_h(token), json=body)
        assert resp.status_code == status, (body, resp.text)
    assert db.custom_roles[CODE]["archived"] is True and db.custom_roles[CODE]["label"] == "Renamed"
    resp = client.post("/admin/platform/roles/cis", headers=_h(token),
                       json={"code": "custom_zz_two", "label": "Two", "based_on": "admin"})
    assert resp.status_code == 400


def test_a_non_owner_or_a_legacy_session_cannot_write(api, db, issuer):
    client, _, _ = api
    sub = "bbbbbbbb-0000-4000-8000-00000000000b"
    db.add(email="other.admin@adventure.asia", username="other", role="admin", iss=issuer.iss, sub=sub)
    other = issuer.token(sub=sub, email="other.admin@adventure.asia", **{"cognito:groups": ["cis-admin"]})
    body = {"code": CODE, "label": "Invented", "based_on": "content"}
    assert client.post("/admin/platform/roles/cis", headers=_h(other), json=body).status_code == 403
    assert client.post("/admin/platform/roles/cis", headers=_h(), json=body).status_code == 403   # secret only
    resp = client.get("/admin/platform/roles/cis", headers=_h(other))
    assert resp.status_code == 200 and resp.json()["can_edit"] is False
    assert CODE not in db.custom_roles and db.audit == []
