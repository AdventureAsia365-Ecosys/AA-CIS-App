"""AA-1155 (review fix of the CIS custom-roles lane) — the staff directory reconcile and the sign-in drift log know the
custom-role groups.

Covers:
- cis-custom / booking-custom are read (ListUsersInGroup) like every staff group.
- A synced CIS custom holder (cis-custom alone, admin_users.custom_role set, role = based_on) and a synced Booking
  custom grant (booking-custom, staff_app_grant.role = a custom code) are no drift.
- A custom holder that kept a built-in cis-* group (it outranks cis-custom at sign-in) is cis_role drift; so is a
  custom row without cis-custom, a built-in row in cis-custom, and a Booking custom grant without booking-custom.
- A10f / A10g (root ADR 0006a) are known Dev E2E identities, only with their exact group sets and rows.
- resolve_staff logs staff_role_drift when a custom role row signs in through a built-in group.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

import api.main as main_mod
from api.core import e2e_staff, identity_config, staff_identity
from api.main import app, get_pool
from services import staff_reconcile as sr
from tests.unit.aa854_cognito_staff import ISS as TOKEN_ISS, SUB, Directory, install_issuer
from tests.unit.test_aa816_staff_reconcile import _clean_world, _row, _run, _run_e2e, _user

CUSTOM = "custom_trail_writer"
BK_CUSTOM = "custom_desk_ops"
A10F = e2e_staff.E2E_CIS_CONTENT_LEAD_TEST_STAFF_EMAIL
A10G = e2e_staff.E2E_CUSTOM_MAKER_TEST_STAFF_EMAIL


def _custom_world(*, cis_groups=("cis-custom",), booking_groups=("booking-custom",), custom_role=CUSTOM,
                  role="content_lead", booking=BK_CUSTOM):
    """The clean world plus one invented staff member holding a CIS custom role and a Booking custom grant."""
    users, groups, rows, grants = _clean_world()
    person = _user("river.stone@adventure.asia", "s-river")
    users.append(person)
    for g in (*cis_groups, *booking_groups):
        groups[g] = list(groups.get(g) or []) + [person]
    row = _row("river", role=role, email="river.stone@adventure.asia", sub="s-river", rid="id-river")
    row["custom_role"] = custom_role
    rows.append(row)
    if booking:
        grants.append({"admin_user_id": "id-river", "app": "booking", "role": booking})
    return users, groups, rows, grants


def _drift(report, kind):
    return [d for d in report["group_drift"] if d["kind"] == kind]


def test_the_custom_groups_are_staff_groups():
    assert "cis-custom" in sr.CIS_GROUPS and "booking-custom" in sr.BOOKING_GROUPS
    assert {"cis-custom", "booking-custom"} <= set(sr.STAFF_GROUPS)
    assert sr.CIS_CUSTOM_GROUP == staff_identity.CUSTOM_GROUP
    # the grant-table check keeps the built-in Booking roles only
    assert "custom" not in {r for _, r in sr.BOOKING_GROUP_ROLES}


def test_the_directory_rows_read_the_custom_role():
    assert "custom_role" in sr.ROWS_SQL


def test_a_synced_custom_holder_is_no_drift():
    report = _run(*_custom_world())
    assert report["group_drift"] == [], report["group_drift"]
    assert report["drift_total"] == 0


def test_a_built_in_group_left_beside_cis_custom_is_drift():
    # The remove step of the old group failed: cis-content_lead outranks cis-custom, so the person keeps content_lead.
    report = _run(*_custom_world(cis_groups=("cis-content_lead", "cis-custom")))
    assert _drift(report, "cis_role") == [{
        "username": "river", "email": "river.stone@adventure.asia", "kind": "cis_role", "directory": CUSTOM,
        "cognito": "content_lead", "cognito_groups": ["cis-content_lead", "cis-custom"]}]
    assert report["drift_total"] == 1


def test_a_custom_row_only_in_its_based_on_group_is_drift():
    report = _run(*_custom_world(cis_groups=("cis-content_lead",)))
    [entry] = _drift(report, "cis_role")
    assert (entry["directory"], entry["cognito"], entry["cognito_groups"]) == (CUSTOM, "content_lead",
                                                                               ["cis-content_lead"])


def test_a_built_in_row_in_cis_custom_is_drift():
    report = _run(*_custom_world(custom_role=None))
    [entry] = _drift(report, "cis_role")
    assert (entry["directory"], entry["cognito"], entry["cognito_groups"]) == ("content_lead", "custom",
                                                                               ["cis-custom"])


def test_a_booking_custom_grant_needs_booking_custom():
    report = _run(*_custom_world(booking_groups=("booking-sales",)))
    [entry] = _drift(report, "booking_role")
    assert (entry["directory"], entry["cognito"], entry["cognito_groups"]) == (BK_CUSTOM, "sales", ["booking-sales"])
    report = _run(*_custom_world(booking=None))
    [entry] = _drift(report, "booking_role")
    assert (entry["directory"], entry["cognito"], entry["cognito_groups"]) == (None, "custom", ["booking-custom"])


def test_a_built_in_booking_grant_in_booking_custom_is_drift():
    report = _run(*_custom_world(booking="sales"))
    [entry] = _drift(report, "booking_role")
    assert (entry["directory"], entry["cognito"]) == ("sales", "custom")


@pytest.mark.parametrize("grant, expected", [
    (None, []), ("admin", ["booking-admin"]), ("finance", ["booking-finance"]), (BK_CUSTOM, ["booking-custom"])])
def test_expected_booking_groups(grant, expected):
    assert sr.expected_booking_groups(grant) == expected


# ── A10f / A10g ───────────────────────────────────────────────────────────────────────────────────────────────────

def _a10_world(*, a10f_groups=("cis-content_lead",), a10g_groups=("cis-custom", "booking-custom"),
               a10g_custom=CUSTOM, a10f_custom=None):
    users, groups, rows, grants = _clean_world()
    lead = _user(A10F, "s-a10f", status="FORCE_CHANGE_PASSWORD")
    maker = _user(A10G, "s-a10g", status="FORCE_CHANGE_PASSWORD")
    users += [lead, maker]
    for g in a10f_groups:
        groups[g] = list(groups.get(g) or []) + [lead]
    for g in a10g_groups:
        groups[g] = list(groups.get(g) or []) + [maker]
    lead_row = _row("e2e-cis-content-lead", role="content_lead", email=A10F, sub="s-a10f", rid="id-a10f")
    lead_row["custom_role"] = a10f_custom
    maker_row = _row("e2e-custom-maker", role="content_lead", email=A10G, sub="s-a10g", rid="id-a10g")
    maker_row["custom_role"] = a10g_custom
    rows += [lead_row, maker_row]
    grants.append({"admin_user_id": "id-a10g", "app": "booking", "role": BK_CUSTOM})
    return users, groups, rows, grants


def test_a10f_and_a10g_are_known_exceptions_with_their_adr_clause():
    assert sr.KNOWN_E2E_STAFF[A10F].clause == "ADR 0006a A10f"
    assert sr.KNOWN_E2E_STAFF[A10F].groups == {"cis-content_lead"}
    assert sr.KNOWN_E2E_STAFF[A10F].directory_role == "content_lead"
    assert sr.KNOWN_E2E_STAFF[A10G].clause == "ADR 0006a A10g"
    assert sr.KNOWN_E2E_STAFF[A10G].groups == {"cis-custom", "booking-custom"}   # the ADR's one exact set
    assert sr.KNOWN_E2E_STAFF[A10G].directory_role == "content_lead"
    assert sr.KNOWN_E2E_STAFF[A10G].other_groups == ()
    assert "A10f" in sr.KNOWN_E2E_NOTE and "A10g" in sr.KNOWN_E2E_NOTE


def test_on_dev_a10f_and_a10g_are_listed_apart_and_not_drift():
    report = _run_e2e(*_a10_world())
    assert report["drift_total"] == 0, report
    known = {k["email"]: k for k in report["known_e2e_exceptions"]}
    assert set(known) == {A10F, A10G}
    assert known[A10F]["clause"] == "ADR 0006a A10f" and known[A10F]["exempt_from"] == []
    assert known[A10G]["groups"] == ["cis-custom", "booking-custom"]
    assert {p["email"] for p in report["pending_first_sign_in"]} == {A10F, A10G}


def test_off_dev_a10f_and_a10g_are_not_listed():
    report = _run_e2e(*_a10_world(), known_e2e=False)
    assert report["known_e2e_exceptions"] == [] and report["drift_total"] == 0


@pytest.mark.parametrize("kwargs, email", [
    ({"a10g_groups": ("cis-custom",)}, A10G),                # not the ADR's set: no booking-custom
    ({"a10g_groups": ("cis-content_lead", "cis-custom", "booking-custom")}, A10G),
    ({"a10g_custom": None}, A10G),                           # in cis-custom without a custom role
    ({"a10f_custom": CUSTOM}, A10F),                         # A10f never holds a custom role
    ({"a10f_groups": ("cis-content_lead", "booking-sales")}, A10F),
])
def test_an_a10f_or_a10g_off_its_exact_shape_is_not_a_known_exception(kwargs, email):
    report = _run_e2e(*_a10_world(**kwargs))
    assert email not in {k["email"] for k in report["known_e2e_exceptions"]}
    assert report["drift_total"] >= 1   # and it shows as drift


# ── sign-in: staff_role_drift ─────────────────────────────────────────────────────────────────────────────────────

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


def _verify(issuer, groups):
    return TestClient(app).post("/auth/verify-admin", json={"token": issuer.token(**{"cognito:groups": groups})})


def test_a_custom_row_signing_in_through_its_based_on_group_logs_drift(directory, issuer):
    directory.add_custom_role()
    uid = directory.add(role="content_lead", iss=TOKEN_ISS, sub=SUB, custom_role="custom_content")
    with capture_logs() as logs:
        res = _verify(issuer, ["cis-content_lead", "cis-custom"])
    assert res.status_code == 200 and res.json()["role"] == "content_lead"
    drift = [e for e in logs if e["event"] == "staff_role_drift"]
    assert drift == [{"event": "staff_role_drift", "log_level": "warning", "admin_id": uid,
                      "group_role": "content_lead", "db_role": "content_lead", "custom_role": "custom_content",
                      "sub": SUB}]


def test_a_custom_holder_signing_in_through_cis_custom_logs_no_drift(directory, issuer):
    directory.add_custom_role()
    directory.add(role="content_lead", iss=TOKEN_ISS, sub=SUB, custom_role="custom_content")
    with capture_logs() as logs:
        res = _verify(issuer, ["cis-custom"])
    assert res.status_code == 200, res.text
    assert not [e for e in logs if e["event"] == "staff_role_drift"]
