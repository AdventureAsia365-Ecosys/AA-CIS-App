"""AA-816 (S243, option B) — the TEMPORARY E2E test-mailbox exception in api/services/staff_directory.py.

Exactly one non-company address (E2E_TEST_STAFF_EMAIL) is a valid staff address, and only as a CIS reviewer with no
Booking grant: create, roles and sync refuse any wider scope for it before any DB write or Cognito call. Every other
off-domain address is still refused. The E2E helper's recipient and migration 250 name the same address.
"""
from __future__ import annotations

import os

import pytest

from api.core import staff_identity
from api.services import staff_directory as sd
from botocore.stub import Stubber

from tests.unit.test_aa816_staff_directory_writes import POOL, StaffDB, _client, _create, _expect, _group

E2E = "e2e@e2e-mail.lumiguides.it.com"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


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


# ── Validation ───────────────────────────────────────────────────────────────────────────────────

def test_the_constant_is_the_one_test_address():
    assert sd.E2E_TEST_STAFF_EMAIL == E2E and sd.E2E_TEST_STAFF_ROLE == "reviewer"


@pytest.mark.parametrize("value", [E2E, "  E2E@E2E-Mail.Lumiguides.IT.com  "])
def test_the_exact_address_is_accepted_normalised(value):
    assert sd.require_staff_email(value) == E2E
    assert sd.is_e2e_test_email(value)


@pytest.mark.parametrize("email", [
    "e2e2@e2e-mail.lumiguides.it.com",        # another local part on the same sub-domain
    "x.e2e@e2e-mail.lumiguides.it.com",
    "e2e+1@e2e-mail.lumiguides.it.com",       # plus addressing is a different address
    "e2e@lumiguides.it.com",                  # the apex
    "e2e@e2e-mail.lumiguides.it.com.evil.com",
    "e2e@x.e2e-mail.lumiguides.it.com",
    "e2e@e2e-mail.lumiguides.it.co",
    "e2e @e2e-mail.lumiguides.it.com",
    "someone@gmail.com",
])
def test_every_other_non_company_address_is_still_refused(email):
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_staff_email(email)
    assert (err.value.status, err.value.code) == (400, "email_domain")
    assert not sd.is_e2e_test_email(email)


def test_company_addresses_are_unchanged():
    assert sd.require_staff_email("New.Person@adventure.asia") == "new.person@adventure.asia"
    sd.require_e2e_test_scope("new.person@adventure.asia", "admin", "sales")   # no-op for other addresses


@pytest.mark.parametrize("role,booking", [("admin", None), ("content", None), ("viewer", None),
                                          ("reviewer", "sales"), ("reviewer", "admin"), (None, None)])
def test_scope_refuses_anything_but_reviewer_without_booking(role, booking):
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_e2e_test_scope(E2E, role, booking)
    assert (err.value.status, err.value.code) == (400, "e2e_test_scope")


def test_scope_accepts_reviewer_without_booking():
    sd.require_e2e_test_scope(E2E, "reviewer", None)


# ── create ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_the_e2e_reviewer(db, stubbed):
    cognito, stub = stubbed
    _expect(stub, "admin_create_user", _create(E2E), {"User": {"Username": E2E}})
    _expect(stub, "admin_add_user_to_group", _group(E2E, "cis-reviewer"))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=E2E, display_name="E2E Cognito",
                                   role="reviewer")
    assert result.status == sd.STATUS_OK and result.created
    assert result.steps == ["create_user", "add_group:cis-reviewer"]
    row = db.row(result.staff_id)
    assert (row["email"], row["role"], row["is_owner"], row["password_hash"]) == (E2E, "reviewer", False, None)
    assert (result.staff_id, "booking") not in db.grants
    assert db.actions() == [sd.StaffAudit.CREATED]


@pytest.mark.parametrize("role,booking", [("admin", None), ("content", None), ("reviewer", "sales")])
@pytest.mark.asyncio
async def test_create_with_a_wider_scope_is_refused_before_any_write(db, stubbed, role, booking):
    cognito, _ = stubbed   # no stubbed response: any Cognito call would fail the test
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email=E2E, display_name="E2E", role=role,
                              booking_role=booking)
    assert (err.value.status, err.value.code) == (400, "e2e_test_scope")
    assert len(db.rows) == 1 and db.audit == [] and db.grants == {}


# ── roles / sync ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [{"role": "admin"}, {"role": "content"}, {"booking_role": "sales"},
                                    {"role": "reviewer", "booking_role": "admin"}])
@pytest.mark.asyncio
async def test_roles_change_to_a_wider_scope_is_refused(db, stubbed, kwargs):
    cognito, _ = stubbed
    uid = db.add(email=E2E, role="reviewer")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, **kwargs)
    assert (err.value.status, err.value.code) == (400, "e2e_test_scope")
    assert db.row(uid)["role"] == "reviewer" and (uid, "booking") not in db.grants and db.audit == []


@pytest.mark.asyncio
async def test_roles_keeping_reviewer_and_no_booking_is_allowed(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=E2E, role="reviewer")
    _expect(stub, "admin_add_user_to_group", _group(E2E, "cis-reviewer"))
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, role="reviewer",
                                   booking_role=None)
    assert result.status == sd.STATUS_OK and db.audit == []   # nothing changed


@pytest.mark.asyncio
async def test_sync_refuses_a_drifted_e2e_row(db, stubbed):
    cognito, _ = stubbed
    uid = db.add(email=E2E, role="reviewer", booking="sales")   # a grant written outside the directory
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert err.value.code == "e2e_test_scope"


@pytest.mark.asyncio
async def test_disable_the_e2e_row_works_like_any_other(db, stubbed):
    cognito, stub = stubbed
    uid = db.add(email=E2E, role="reviewer")
    _expect(stub, "admin_disable_user", {"UserPoolId": POOL, "Username": E2E})
    _expect(stub, "admin_user_global_sign_out", {"UserPoolId": POOL, "Username": E2E})
    result = await sd.disable_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK and db.row(uid)["is_active"] is False


# ── The other copies of the address ──────────────────────────────────────────────────────────────

def test_e2e_helper_and_migration_250_name_the_same_address():
    with open(os.path.join(ROOT, "tests", "e2e", "helpers", "mailbox.ts"), encoding="utf-8") as f:
        assert f"export const E2E_COGNITO_EMAIL = '{E2E}';" in f.read()
    with open(os.path.join(ROOT, "api", "migrations", "250_admin_users_e2e_test_email.sql"), encoding="utf-8") as f:
        assert f"email = '{E2E}' AND role = 'reviewer'" in f.read()
