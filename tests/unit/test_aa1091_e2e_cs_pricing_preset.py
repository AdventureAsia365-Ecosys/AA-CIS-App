"""AA-1091 (S279, root ADR 0006a A10e-2): the A10d customer_success E2E account may hold the "Pricing team" preset's
Booking role ``contracting``, on Dev only, so it is the maker of the AA-1091 live check with synthetic prices.

The staff directory accepts exactly that one Booking grant for exactly that address, only where ``ENVIRONMENT`` is
exactly ``dev``; every other E2E address keeps "no Booking role"; the preset gives it and an owner removes it; the CIS
session stays read-only with no write allow-list whatever its Booking group.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.core import e2e_staff, staff_identity
from api.services import staff_directory as sd
from services import staff_reconcile as sr
from tests.unit.test_aa1035_a10d_e2e_cs import (  # noqa: F401 — db, stubbed, issuer and directory are fixtures
    CS_E2E, CS_GROUP, _cs_row, _cs_token, _echo_app, db, directory, issuer, stubbed,
)
from tests.unit.test_aa816_e2e_cis_admin import ADMIN_E2E, NOT_DEV, _set_env
from tests.unit.test_aa816_staff_directory_writes import _expect, _group
from tests.unit.test_aa816_staff_reconcile import (
    E2E_BK_CHECKER, E2E_BK_MAKER, E2E_BK_STAFF, E2E_BOOKING, E2E_CIS_ADMIN, E2E_CRM_STAFF, E2E_REV, _e2e_world, _row,
    _run_e2e, _user,
)

CONTRACTING_GROUP = "booking-contracting"
CONTENT_E2E = "e2e@e2e-mail.lumiguides.it.com"
PRICING_CHECKER = "e2e-booking-pricing-checker@e2e-mail.lumiguides.it.com"


def test_the_constants():
    assert e2e_staff.E2E_CIS_CS_PRICING_BOOKING_ROLE == "contracting" == sd.PRICING_BOOKING_ROLE
    assert e2e_staff.E2E_BOOKING_PRICING_CHECKER_TEST_STAFF_EMAIL == (
        "e2e-booking-pricing-checker@e2e-mail.lumiguides.it.com")
    # The checker is never a CIS directory address.
    assert not sd.is_e2e_test_email(e2e_staff.E2E_BOOKING_PRICING_CHECKER_TEST_STAFF_EMAIL)
    assert dict(e2e_staff.DEV_ONLY_E2E_BOOKING_ROLES) == {CS_E2E: "contracting"}


def test_scope_accepts_contracting_for_the_cs_account_on_dev(monkeypatch):
    _set_env(monkeypatch, "dev")
    sd.require_e2e_test_scope(CS_E2E, "customer_success", "contracting")
    sd.require_e2e_test_scope(f"  {CS_E2E.upper()} ", "customer_success", "contracting")
    sd.require_e2e_test_scope(CS_E2E, "customer_success", None)


@pytest.mark.parametrize("value", NOT_DEV)
def test_scope_refuses_contracting_outside_dev(monkeypatch, value):
    _set_env(monkeypatch, value)
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_e2e_test_scope(CS_E2E, "customer_success", "contracting")
    assert (err.value.status, err.value.code) == (400, "e2e_test_scope")
    assert "no Booking role other than contracting (Dev only)" in err.value.detail


@pytest.mark.parametrize("email,role", [(CONTENT_E2E, "content"), (ADMIN_E2E, "admin")])
def test_the_other_e2e_accounts_still_hold_no_booking_role(monkeypatch, email, role):
    _set_env(monkeypatch, "dev")
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_e2e_test_scope(email, role, "contracting")
    assert err.value.code == "e2e_test_scope" and "no Booking role and no CRM role" in err.value.detail


@pytest.mark.asyncio
async def test_the_pricing_preset_on_dev_gives_the_cs_account_contracting_and_removing_it_works(db, stubbed,
                                                                                                monkeypatch):
    _set_env(monkeypatch, "dev")
    cognito, stub = stubbed
    uid = db.add(email=CS_E2E, role="customer_success", username="e2e-cis-cs")
    _expect(stub, "admin_add_user_to_group", _group(CS_E2E, CONTRACTING_GROUP))
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="pricing_team")
    assert result.status == sd.STATUS_OK
    assert db.row(uid)["role"] == "customer_success" and db.grants[(uid, "booking")] == "contracting"
    assert db.audit[-1]["details"] == {"booking_role": {"before": None, "after": "contracting"},
                                       "preset": "pricing_team"}
    # Cleanup after the live check: the owner removes the Booking grant; the CIS role stays customer_success (A10d).
    _expect(stub, "admin_remove_user_from_group", _group(CS_E2E, CONTRACTING_GROUP))
    result = await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, booking_role=None)
    assert result.status == sd.STATUS_OK and (uid, "booking") not in db.grants
    assert db.row(uid)["role"] == "customer_success"
    stub.assert_no_pending_responses()


@pytest.mark.parametrize("value", [None, "test", "prod", "Dev "])
@pytest.mark.asyncio
async def test_the_pricing_preset_outside_dev_is_refused_before_any_write(db, stubbed, monkeypatch, value):
    _set_env(monkeypatch, value)
    cognito, _ = stubbed   # no stubbed response: any Cognito call would fail the test
    uid = db.add(email=CS_E2E, role="customer_success", username="e2e-cis-cs")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, preset="pricing_team")
    assert err.value.code in ("e2e_test_scope", "e2e_not_allowed_here")
    assert (uid, "booking") not in db.grants and db.audit == []


@pytest.mark.parametrize("booking", ["sales", "admin", "finance"])
@pytest.mark.asyncio
async def test_any_other_booking_role_for_the_cs_account_is_refused_on_dev(db, stubbed, monkeypatch, booking):
    _set_env(monkeypatch, "dev")
    cognito, _ = stubbed
    uid = db.add(email=CS_E2E, role="customer_success", username="e2e-cis-cs")
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, booking_role=booking)
    assert err.value.code == "e2e_test_scope"
    assert (uid, "booking") not in db.grants and db.audit == []


@pytest.mark.asyncio
async def test_the_cis_session_with_the_preset_stays_read_only_without_the_allow_list(issuer, directory,
                                                                                       monkeypatch):
    _set_env(monkeypatch, "dev")
    _cs_row(directory)
    staff_identity.clear_cache()
    identity = await staff_identity.authenticate(directory, _cs_token(issuer, groups=(CS_GROUP, CONTRACTING_GROUP)))
    assert (identity.role, identity.e2e_read_only, identity.e2e_allowlisted_writes, identity.is_owner) == (
        "customer_success", True, False, False)


def test_the_cis_middleware_still_refuses_its_writes(issuer, directory, monkeypatch):
    _set_env(monkeypatch, "dev")
    _cs_row(directory)
    app, reached = _echo_app(directory)
    token = _cs_token(issuer, groups=(CS_GROUP, CONTRACTING_GROUP))
    res = TestClient(app).post("/admin/platform/staff", json={}, headers={"Authorization": f"Bearer {token}"})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"}) and reached == []


# ── the staff directory reconcile (services/staff_reconcile.py) ──────────────────────────────────

def test_the_known_e2e_list_is_exactly_the_reviewed_one():
    assert set(sr.KNOWN_E2E_STAFF) == {E2E_REV, E2E_BOOKING, E2E_CIS_ADMIN, E2E_CRM_STAFF, CS_E2E, E2E_BK_STAFF,
                                       E2E_BK_MAKER, E2E_BK_CHECKER, PRICING_CHECKER}
    assert {e.clause for e in sr.KNOWN_E2E_STAFF.values()} == {
        "ADR 0006a A10", "ADR 0006a A10a", "ADR 0006a A10b", "ADR 0006a A10c", "ADR 0006a A10d", "ADR 0006a A10e",
        "ADR 0006a A10e-2"}
    # The pricing checker: booking-contracting only, never in the CIS directory. The A10d account's one other group
    # set is the Pricing team preset's; no other identity has a second group set.
    assert sr.KNOWN_E2E_STAFF[PRICING_CHECKER].groups == {"booking-contracting"}
    assert sr.KNOWN_E2E_STAFF[PRICING_CHECKER].directory_role is None
    assert sr.KNOWN_E2E_STAFF[PRICING_CHECKER].clause == "ADR 0006a A10e-2"
    assert {e: k.other_groups for e, k in sr.KNOWN_E2E_STAFF.items() if k.other_groups} == {
        CS_E2E: (frozenset({"cis-customer_success", "booking-contracting"}),)}


def _with_cs(extra_group=None, booking=None):
    users, groups, rows, grants = _e2e_world()
    cs = _user(CS_E2E, "s-e2e-cs")
    users.append(cs)
    for g in [CS_GROUP] + ([extra_group] if extra_group else []):
        groups[g] = list(groups.get(g, [])) + [cs]
    rows.append(_row("e2e-cis-cs", role="customer_success", email=CS_E2E, sub="s-e2e-cs"))
    if booking:
        grants.append({"admin_user_id": "id-e2e-cis-cs", "app": "booking", "role": booking})
    return users, groups, rows, grants


def test_on_dev_the_cs_account_with_the_pricing_preset_is_listed_apart_and_not_drift():
    report = _run_e2e(*_with_cs(CONTRACTING_GROUP, "contracting"))
    assert report["drift_total"] == 0, report
    got = {k["email"]: (k["clause"], k["row_username"]) for k in report["known_e2e_exceptions"]}
    assert got[CS_E2E] == ("ADR 0006a A10d", "e2e-cis-cs")


@pytest.mark.parametrize("group,booking", [("booking-admin", "admin"), ("booking-sales", "sales")])
def test_the_cs_account_with_another_booking_group_is_not_a_known_exception(group, booking):
    report = _run_e2e(*_with_cs(group, booking))
    assert CS_E2E not in {k["email"] for k in report["known_e2e_exceptions"]}


def test_on_dev_the_pricing_checker_is_listed_apart_only_with_exactly_contracting():
    users, groups, rows, grants = _e2e_world()
    chk = _user(PRICING_CHECKER, "s-e2e-bk-pc", status="FORCE_CHANGE_PASSWORD")
    users.append(chk)
    groups[CONTRACTING_GROUP] = list(groups.get(CONTRACTING_GROUP, [])) + [chk]
    report = _run_e2e(users, groups, rows, grants)
    got = {k["email"]: (k["clause"], k["exempt_from"]) for k in report["known_e2e_exceptions"]}
    assert got[PRICING_CHECKER] == ("ADR 0006a A10e-2", ["cognito_without_row"])
    assert report["drift_total"] == 0, report
    groups["booking-admin"] = list(groups["booking-admin"]) + [chk]
    report = _run_e2e(users, groups, rows, grants)
    assert PRICING_CHECKER not in {k["email"] for k in report["known_e2e_exceptions"]}
    assert (PRICING_CHECKER, "no_row") in {(f["email"], f["reason"]) for f in report["cognito_without_row"]}
