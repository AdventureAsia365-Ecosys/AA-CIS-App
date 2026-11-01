"""AA-816 (S242 C7) — staff directory reconcile: WORKFORCE pool vs shared.admin_users, report only.

Covers:
- Cognito reads: ListUsers / ListUsersInGroup paginated (botocore Stubber, exact params), a missing group is a
  finding, access denied is permanent; the module makes no other Cognito call (Stubber refuses any).
- each report category: (a) Cognito user without an active row, (b) active row unknown to Cognito,
  (c) CIS role / Booking grant / enabled drift, (d) status other than CONFIRMED, (e) cutover readiness,
  and the break-glass block (excluded from (a)/(c)/(d), checked on its own).
- the job: registered on the worker, runs end to end on a stubbed pool and a fake DB, logs counts only.
- the routes: health + on-demand run need staff.read, the daily schedule is a machine route keyed per day.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from botocore.stub import Stubber
from structlog.testing import capture_logs

from api.core.staff_authz import MACHINE_ROUTES, OPS_WRITE, POLICY, STAFF_READ
from services import staff_reconcile as sr
from shared.jobs import registry
from shared.jobs.queue import Job

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS"
POOL = "us-west-1_bCcFvbSOS"
BG = "admin@adventure.asia"
REPO = Path(__file__).resolve().parents[2]


def _user(email, sub, status="CONFIRMED", enabled=True, username=None):
    return {"Username": username or sub,
            "Attributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": email}],
            "UserStatus": status, "Enabled": enabled}


def _row(username, role="admin", active=True, email=None, sub=None, iss=ISS, rid=None):
    return {"id": rid or f"id-{username}", "username": username, "role": role, "is_active": active, "email": email,
            "cognito_iss": iss if sub else None, "cognito_sub": sub}


def _groups(**members):
    """{group: [raw users]} for every staff group; a kwarg value None = the group is missing."""
    out = {g: [] for g in sr.STAFF_GROUPS}
    for key, users in members.items():
        out[key.replace("_", "-", 1)] = users
    return out


def _client():
    return boto3.client("cognito-idp", region_name="us-west-1", aws_access_key_id="x", aws_secret_access_key="y")


# ── Cognito reads (Stubber) ───────────────────────────────────────────────────────────────────────

def test_list_users_follows_pagination_token():
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("list_users", {"Users": [_user("a@adventure.asia", "s-a")], "PaginationToken": "p2"},
                          {"UserPoolId": POOL, "Limit": 60})
        stub.add_response("list_users", {"Users": [_user("b@adventure.asia", "s-b")]},
                          {"UserPoolId": POOL, "Limit": 60, "PaginationToken": "p2"})
        users = sr.list_users(client, POOL)
        stub.assert_no_pending_responses()
    assert [u["Username"] for u in users] == ["s-a", "s-b"]


def test_list_group_follows_next_token_and_reports_a_missing_group():
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("list_users_in_group", {"Users": [_user("a@adventure.asia", "s-a")], "NextToken": "n2"},
                          {"UserPoolId": POOL, "GroupName": "cis-admin", "Limit": 60})
        stub.add_response("list_users_in_group", {"Users": [_user("b@adventure.asia", "s-b")]},
                          {"UserPoolId": POOL, "GroupName": "cis-admin", "Limit": 60, "NextToken": "n2"})
        stub.add_client_error("list_users_in_group", service_error_code="ResourceNotFoundException",
                              expected_params={"UserPoolId": POOL, "GroupName": "cis-content", "Limit": 60})
        assert len(sr.list_group(client, POOL, "cis-admin")) == 2
        assert sr.list_group(client, POOL, "cis-content") is None
        stub.assert_no_pending_responses()


def test_fetch_pool_lists_users_then_every_staff_group():
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("list_users", {"Users": [_user("a@adventure.asia", "s-a")]},
                          {"UserPoolId": POOL, "Limit": 60})
        for g in sr.STAFF_GROUPS:
            users = [_user("a@adventure.asia", "s-a")] if g == "cis-admin" else []
            stub.add_response("list_users_in_group", {"Users": users},
                              {"UserPoolId": POOL, "GroupName": g, "Limit": 60})
        data = sr.fetch_pool(client, POOL)
        stub.assert_no_pending_responses()
    assert len(data["users"]) == 1 and set(data["groups"]) == set(sr.STAFF_GROUPS)
    assert len(data["groups"]["cis-admin"]) == 1


def test_access_denied_is_a_permanent_read_error():
    client = _client()
    with Stubber(client) as stub:
        stub.add_client_error("list_users", service_error_code="AccessDeniedException",
                              expected_params={"UserPoolId": POOL, "Limit": 60})
        with pytest.raises(sr.CognitoReadError, match="AccessDeniedException"):
            sr.fetch_pool(client, POOL)


def test_throttling_is_left_to_the_job_retry():
    client = _client()
    with Stubber(client) as stub:
        stub.add_client_error("list_users", service_error_code="TooManyRequestsException",
                              expected_params={"UserPoolId": POOL, "Limit": 60})
        with pytest.raises(Exception) as exc:
            sr.fetch_pool(client, POOL)
    assert not isinstance(exc.value, sr.CognitoReadError)


def test_a_token_loop_stops(monkeypatch):
    monkeypatch.setattr(sr, "MAX_PAGES", 3)
    client = MagicMock()
    client.list_users.return_value = {"Users": [], "PaginationToken": "again"}
    with pytest.raises(sr.CognitoReadError, match="3 pages"):
        sr.list_users(client, POOL)


def test_only_the_two_list_calls_are_used():
    """The I3 grant allows Admin* writes too; this module must never use them (report only)."""
    src = (REPO / "services" / "staff_reconcile.py").read_text() + \
        (REPO / "services" / "jobs" / "staff_reconcile_job.py").read_text()
    calls = set(re.findall(r"client\.([a-z_]+)\(", src))
    assert calls == {"list_users", "list_users_in_group"}
    assert "admin_" not in "".join(calls)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", sr.ROWS_SQL + sr.GRANTS_SQL)


def test_pool_and_region_come_from_the_issuer():
    assert sr.pool_from_issuer(ISS) == (POOL, "us-west-1")
    with pytest.raises(ValueError):
        sr.pool_from_issuer("https://example.com/")


def test_breakglass_email_default_and_override(monkeypatch):
    monkeypatch.delenv(sr.ENV_BREAKGLASS_EMAIL, raising=False)
    assert sr.breakglass_email() == BG
    monkeypatch.setenv(sr.ENV_BREAKGLASS_EMAIL, " BreakGlass@Adventure.Asia ")
    assert sr.breakglass_email() == "breakglass@adventure.asia"


# ── the report (pure) ─────────────────────────────────────────────────────────────────────────────

NGHIEP = _user("nghieppham@adventure.asia", "s-nghiep")
BREAKGLASS = _user(BG, "s-bg", enabled=False)


def _clean_world():
    users = [NGHIEP, BREAKGLASS]
    groups = _groups(cis_admin=[NGHIEP, BREAKGLASS], booking_admin=[NGHIEP, BREAKGLASS])
    rows = [_row("nghiep", email="nghieppham@adventure.asia", sub="s-nghiep"),
            _row("admin", email=BG, sub="s-bg")]
    grants = [{"admin_user_id": "id-nghiep", "app": "booking", "role": "admin"}]
    return users, groups, rows, grants


def _run(users, groups, rows, grants):
    return sr.reconcile(users, groups, rows, grants, issuer=ISS, breakglass=BG)


def test_a_consistent_directory_reports_zero_drift():
    report = _run(*_clean_world())
    assert report["drift_total"] == 0, report
    assert report["readiness"]["ready"] is True
    assert report["breakglass"]["issues"] == [] and report["breakglass"]["linked"] is True
    assert report["groups"]["cis-admin"] == 2 and report["groups_missing"] == []


def test_a_cognito_user_without_an_active_row():
    users, groups, rows, grants = _clean_world()
    orphan = _user("ghost@adventure.asia", "s-ghost")
    gone = _user("left@adventure.asia", "s-left")                       # row inactive, Cognito still enabled
    done = _user("done@adventure.asia", "s-done", enabled=False)        # row inactive, Cognito disabled: fine
    recreated = _user("re@adventure.asia", "s-new")                     # row bound to an older sub
    users += [orphan, gone, done, recreated]
    groups["cis-reviewer"] = [orphan, gone, done, recreated]
    rows += [_row("left", role="reviewer", active=False, email="left@adventure.asia", sub="s-left"),
             _row("done", role="reviewer", active=False, email="done@adventure.asia", sub="s-done"),
             _row("re", role="reviewer", email="re@adventure.asia", sub="s-old")]
    report = _run(users, groups, rows, grants)
    reasons = {f["email"]: f["reason"] for f in report["cognito_without_row"]}
    assert reasons == {"ghost@adventure.asia": "no_row", "left@adventure.asia": "row_inactive",
                       "re@adventure.asia": "row_bound_to_other_sub"}
    assert report["counts"]["cognito_without_row"] == 3
    # the row of the re-created user points at a sub Cognito no longer has: (b) too
    assert [(u["username"], u["reason"]) for u in report["rows_unknown_to_cognito"]] == [("re", "unknown_sub")]


def test_an_unlinked_row_matches_by_email_and_is_not_an_orphan():
    users, groups, rows, grants = _clean_world()
    new = _user("thu@adventure.asia", "s-thu", status="FORCE_CHANGE_PASSWORD")
    users.append(new)
    groups["cis-content"] = [new]
    rows.append(_row("thu", role="content", email="thu@adventure.asia"))
    report = _run(users, groups, rows, grants)
    assert report["cognito_without_row"] == [] and report["group_drift"] == []
    assert report["readiness"]["missing_cognito_sub"] == ["thu"]
    # a new invitee who has not signed in yet is the normal state, not drift (review L1)
    assert [f["email"] for f in report["pending_first_sign_in"]] == ["thu@adventure.asia"]
    assert report["status_findings"] == [] and report["drift_total"] == 0


def test_an_active_row_unknown_to_cognito():
    users, groups, rows, grants = _clean_world()
    rows += [_row("deleted", role="reviewer", email="deleted@adventure.asia", sub="s-deleted"),
             _row("otherpool", role="reviewer", email="op@adventure.asia", sub="s-op",
                  iss="https://cognito-idp.us-west-1.amazonaws.com/us-west-1_OTHER"),
             _row("inactive", role="reviewer", active=False, email="in@adventure.asia", sub="s-in")]
    report = _run(users, groups, rows, grants)
    assert sorted((u["username"], u["reason"]) for u in report["rows_unknown_to_cognito"]) == [
        ("deleted", "unknown_sub"), ("otherpool", "other_issuer")]


def test_group_and_role_drift():
    users, groups, rows, grants = _clean_world()
    rev = _user("rev@adventure.asia", "s-rev")
    off = _user("off@adventure.asia", "s-off", enabled=False)
    users += [rev, off]
    # nghiep lost booking-admin and gained booking-sales; rev is cis-admin but a reviewer in the directory;
    # off is an active row whose Cognito user is disabled.
    groups["booking-admin"] = [BREAKGLASS]
    groups["booking-sales"] = [NGHIEP]
    groups["cis-admin"] = [NGHIEP, BREAKGLASS, rev]
    groups["cis-content"] = [off]
    rows += [_row("rev", role="reviewer", email="rev@adventure.asia", sub="s-rev"),
             _row("off", role="content", email="off@adventure.asia", sub="s-off")]
    report = _run(users, groups, rows, grants)
    got = sorted((d["username"], d["kind"], d["directory"], d["cognito"]) for d in report["group_drift"])
    assert got == [("nghiep", "booking_role", "admin", "sales"),
                   ("off", "cognito_disabled", "active", "disabled"),
                   ("rev", "cis_role", "reviewer", "admin")]
    assert report["counts"]["group_drift"] == 3


def test_booking_group_without_a_grant_and_no_cis_group_are_drift():
    users, groups, rows, grants = _clean_world()
    sales = _user("sales@adventure.asia", "s-sales")
    users.append(sales)
    groups["booking-sales"] = [sales]
    rows.append(_row("sales", role="content", email="sales@adventure.asia", sub="s-sales"))
    report = _run(users, groups, rows, grants)
    got = sorted((d["kind"], d["directory"], d["cognito"]) for d in report["group_drift"])
    assert got == [("booking_role", None, "sales"), ("cis_role", "content", None)]


def test_two_cis_groups_are_drift_even_when_the_highest_matches():
    users, groups, rows, grants = _clean_world()
    groups["cis-reviewer"] = [NGHIEP]
    report = _run(users, groups, rows, grants)
    assert [(d["kind"], d["cognito_groups"]) for d in report["group_drift"]] == [
        ("cis_role", ["cis-admin", "cis-reviewer"])]


def test_status_other_than_confirmed_is_reported_except_breakglass():
    users, groups, rows, grants = _clean_world()
    pending = _user("new@adventure.asia", "s-new", status="FORCE_CHANGE_PASSWORD")
    reset = _user("reset@adventure.asia", "s-reset", status="RESET_REQUIRED")
    unconfirmed = _user("unc@adventure.asia", "s-unc", status="UNCONFIRMED")
    users += [pending, reset, unconfirmed]
    groups["cis-content"] = [pending, reset, unconfirmed]
    rows += [_row("new", role="content", email="new@adventure.asia"),
             _row("reset", role="content", email="reset@adventure.asia", sub="s-reset"),
             _row("unc", role="content", email="unc@adventure.asia", sub="s-unc")]
    users[1] = _user(BG, "s-bg", status="FORCE_CHANGE_PASSWORD", enabled=False)   # break-glass: own block
    report = _run(users, groups, rows, grants)
    assert sorted((f["email"], f["status"]) for f in report["status_findings"]) == [
        ("reset@adventure.asia", "RESET_REQUIRED"), ("unc@adventure.asia", "UNCONFIRMED")]
    # FORCE_CHANGE_PASSWORD is listed apart and not counted (review L1)
    assert [(f["email"], f["row_username"]) for f in report["pending_first_sign_in"]] == [
        ("new@adventure.asia", "new")]
    assert report["counts"]["status_findings"] == 2 and report["counts"]["pending_first_sign_in"] == 1
    assert any("not CONFIRMED" in i for i in report["breakglass"]["issues"])
    assert report["drift_total"] == 2 + len(report["breakglass"]["issues"])


def test_status_skips_disabled_users_and_users_already_without_a_row():
    users, groups, rows, grants = _clean_world()
    offboarded = _user("off@adventure.asia", "s-off", status="RESET_REQUIRED", enabled=False)
    stray = _user("stray@adventure.asia", "s-stray", status="FORCE_CHANGE_PASSWORD")       # no row
    stuck = _user("stuck@adventure.asia", "s-stuck", status="RESET_REQUIRED")              # no row
    users += [offboarded, stray, stuck]
    rows.append(_row("off", role="content", active=False, email="off@adventure.asia", sub="s-off"))
    report = _run(users, groups, rows, grants)
    # offboarded: disabled + inactive row is consistent, never status drift
    assert report["status_findings"] == [] and report["pending_first_sign_in"] == []
    # no row: counted once, in (a), which carries the status
    assert sorted((f["email"], f["status"]) for f in report["cognito_without_row"]) == [
        ("stray@adventure.asia", "FORCE_CHANGE_PASSWORD"), ("stuck@adventure.asia", "RESET_REQUIRED")]
    assert report["drift_total"] == 2


def test_email_changed_on_one_side_is_drift():
    users, groups, rows, grants = _clean_world()
    rows[0] = _row("nghiep", email="old-nghiep@adventure.asia", sub="s-nghiep")
    report = _run(users, groups, rows, grants)
    assert [(d["username"], d["kind"], d["directory"], d["cognito"]) for d in report["group_drift"]] == [
        ("nghiep", "email", "old-nghiep@adventure.asia", "nghieppham@adventure.asia")]
    assert report["drift_total"] == 1
    # case and spaces are not drift
    rows[0] = _row("nghiep", email=" NghiepPham@Adventure.asia ", sub="s-nghiep")
    assert _run(users, groups, rows, grants)["drift_total"] == 0


def test_cutover_readiness_counts_and_usernames():
    users, groups, rows, grants = _clean_world()
    rows += [_row("legacy1", role="reviewer"), _row("legacy2", role="content"),
             _row("emailonly", role="content", email="eo@adventure.asia"),
             _row("gone", role="content", active=False)]
    report = _run(users, groups, rows, grants)
    r = report["readiness"]
    assert r["ready"] is False and r["active_rows"] == 5
    assert r["missing_email"] == ["legacy1", "legacy2"] and r["missing_email_count"] == 2
    assert r["missing_cognito_sub"] == ["emailonly", "legacy1", "legacy2"] and r["missing_cognito_sub_count"] == 3
    # readiness is not drift: legacy rows nobody created in Cognito yet are expected before cutover
    assert report["drift_total"] == 0


def test_breakglass_issues():
    users, groups, rows, grants = _clean_world()
    users[1] = _user(BG, "s-bg", enabled=True)
    groups["booking-admin"] = [NGHIEP]
    report = _run(users, groups, rows, grants)
    issues = report["breakglass"]["issues"]
    assert any("enabled" in i for i in issues) and any("booking-admin" in i for i in issues)
    assert report["counts"]["breakglass_issues"] == 2 and report["drift_total"] == 2
    assert report["cognito_without_row"] == [] and report["group_drift"] == []

    missing = _run([NGHIEP], _groups(cis_admin=[NGHIEP], booking_admin=[NGHIEP]), rows[:1], grants)
    assert missing["breakglass"]["present"] is False and missing["counts"]["breakglass_issues"] == 1


def test_a_missing_group_counts_as_drift():
    users, groups, rows, grants = _clean_world()
    groups["booking-contracting"] = None
    report = _run(users, groups, rows, grants)
    assert report["groups_missing"] == ["booking-contracting"] and report["drift_total"] == 1


def test_summary_fields_hold_counts_only():
    users, groups, rows, grants = _clean_world()
    users.append(_user("ghost@adventure.asia", "s-ghost"))
    fields = sr.summary_fields(_run(users, groups, rows, grants))
    assert fields["cognito_without_row"] == 1 and fields["ready"] is True
    assert all(isinstance(v, (int, bool)) for v in fields.values())
    assert "@" not in repr(fields)


def test_group_role_maps_match_sign_in_and_the_grant_table():
    from api.core.staff_identity import GROUP_ROLES

    assert sr.CIS_GROUPS == tuple(g for g, _ in GROUP_ROLES)
    mig = (REPO / "api" / "migrations" / "247_admin_users_identity.sql").read_text()
    check = re.search(r"staff_app_grant_role_chk CHECK \(role IN \(([^)]*)\)\)", mig).group(1)
    assert {r for _, r in sr.BOOKING_GROUP_ROLES} == set(re.findall(r"'([a-z_]+)'", check))
    assert all(g == f"booking-{r}" for g, r in sr.BOOKING_GROUP_ROLES)


# ── known E2E exceptions (root ADR 0006a A10 / A10a / A10b; AA-816 follow-up S253) ───────────────

E2E_REV = "e2e@e2e-mail.lumiguides.it.com"
E2E_BOOKING = "e2e-booking@e2e-mail.lumiguides.it.com"
E2E_CIS_ADMIN = "e2e-cis-admin@e2e-mail.lumiguides.it.com"
E2E_CRM_STAFF = "e2e-crm-staff@e2e-mail.lumiguides.it.com"


def _e2e_world():
    """The clean world plus the three Dev E2E identities exactly as the ADR allows them: A10 reviewer row, A10a only in
    Cognito (booking-admin, no CIS row), A10b admin row; all never signed in with a password."""
    users, groups, rows, grants = _clean_world()
    rev = _user(E2E_REV, "s-e2e", status="FORCE_CHANGE_PASSWORD")
    bk = _user(E2E_BOOKING, "s-e2e-bk", status="FORCE_CHANGE_PASSWORD")
    adm = _user(E2E_CIS_ADMIN, "s-e2e-adm", status="FORCE_CHANGE_PASSWORD")
    users += [rev, bk, adm]
    groups["cis-reviewer"] = [rev]
    groups["booking-admin"] = groups["booking-admin"] + [bk]
    groups["cis-admin"] = groups["cis-admin"] + [adm]
    rows += [_row("e2e", role="reviewer", email=E2E_REV, sub="s-e2e"),
             _row("e2e-cis-admin", role="admin", email=E2E_CIS_ADMIN, sub="s-e2e-adm")]
    return users, groups, rows, grants


def _run_e2e(users, groups, rows, grants, known_e2e=True):
    return sr.reconcile(users, groups, rows, grants, issuer=ISS, breakglass=BG, known_e2e=known_e2e)


def test_known_e2e_addresses_match_the_directory_and_the_a10b_module():
    from api.core import e2e_staff
    from api.services import staff_directory

    assert e2e_staff.E2E_REVIEWER_TEST_STAFF_EMAIL == staff_directory.E2E_TEST_STAFF_EMAIL
    assert sr.KNOWN_E2E_STAFF[E2E_REV].directory_role == staff_directory.E2E_TEST_STAFF_ROLE
    assert sr.KNOWN_E2E_STAFF[E2E_CIS_ADMIN].directory_role == staff_directory.E2E_CIS_ADMIN_TEST_STAFF_ROLE
    assert set(sr.KNOWN_E2E_STAFF) == {E2E_REV, E2E_BOOKING, E2E_CIS_ADMIN, E2E_CRM_STAFF}
    assert {e.clause for e in sr.KNOWN_E2E_STAFF.values()} == {"ADR 0006a A10", "ADR 0006a A10a", "ADR 0006a A10b",
                                                               "ADR 0006a A10c"}
    # A10c (AA-1019): exactly crm-ops + crm-support, never in the CIS directory (AA-CIS-Infra workforce.py).
    assert sr.KNOWN_E2E_STAFF[E2E_CRM_STAFF].groups == {"crm-ops", "crm-support"}
    assert sr.KNOWN_E2E_STAFF[E2E_CRM_STAFF].directory_role is None
    assert e2e_staff.E2E_CRM_STAFF_TEST_STAFF_EMAIL == E2E_CRM_STAFF
    with pytest.raises(TypeError):
        sr.KNOWN_E2E_STAFF["x@e2e-mail.lumiguides.it.com"] = sr.KNOWN_E2E_STAFF[E2E_REV]  # read-only


def test_on_dev_the_e2e_identities_are_listed_apart_and_not_drift():
    report = _run_e2e(*_e2e_world())
    assert report["drift_total"] == 0, report
    assert report["cognito_without_row"] == []
    got = {k["email"]: (k["clause"], k["exempt_from"], k["row_username"]) for k in report["known_e2e_exceptions"]}
    assert got == {E2E_REV: ("ADR 0006a A10", [], "e2e"),
                   E2E_BOOKING: ("ADR 0006a A10a", ["cognito_without_row"], None),
                   E2E_CIS_ADMIN: ("ADR 0006a A10b", [], "e2e-cis-admin")}
    assert report["counts"]["known_e2e_exceptions"] == 3 and report["known_e2e_enabled"] is True
    assert all(k["reason"] for k in report["known_e2e_exceptions"])
    assert sr.KNOWN_E2E_NOTE in report["notes"]
    # the two directory rows are still checked like any staff row: their pending status is listed as usual
    assert sorted(f["email"] for f in report["pending_first_sign_in"]) == [E2E_CIS_ADMIN, E2E_REV]


def test_off_dev_the_a10a_identity_is_drift_as_before():
    report = _run_e2e(*_e2e_world(), known_e2e=False)
    assert report["known_e2e_exceptions"] == [] and report["known_e2e_enabled"] is False
    assert report["counts"]["known_e2e_exceptions"] == 0
    assert [(f["email"], f["reason"]) for f in report["cognito_without_row"]] == [(E2E_BOOKING, "no_row")]
    assert report["drift_total"] == 1
    assert sr.KNOWN_E2E_NOTE not in report["notes"]


def test_the_default_is_off():
    users, groups, rows, grants = _e2e_world()
    report = sr.reconcile(users, groups, rows, grants, issuer=ISS, breakglass=BG)
    assert report["known_e2e_exceptions"] == [] and report["drift_total"] == 1


@pytest.mark.parametrize("value,enabled", [("dev", True), ("Dev", False), ("dev ", False), ("prod", False),
                                           ("stg", False), ("test", False), ("", False), (None, False)])
def test_known_e2e_is_on_only_where_environment_is_exactly_dev(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", value)
    assert sr.known_e2e_enabled() is enabled


@pytest.mark.parametrize("extra", ["cis-reviewer", "cis-admin", "booking-sales"])
def test_a10a_with_an_unexpected_group_stays_drift(extra):
    users, groups, rows, grants = _e2e_world()
    bk = next(u for u in users if u["Username"] == "s-e2e-bk")
    groups[extra] = groups[extra] + [bk]
    report = _run_e2e(users, groups, rows, grants)
    assert [(f["email"], f["reason"]) for f in report["cognito_without_row"]] == [(E2E_BOOKING, "no_row")]
    assert E2E_BOOKING not in {k["email"] for k in report["known_e2e_exceptions"]}
    assert report["counts"]["known_e2e_exceptions"] == 2 and report["drift_total"] == 1


def test_a10a_without_its_group_stays_drift():
    users, groups, rows, grants = _e2e_world()
    groups["booking-admin"] = [u for u in groups["booking-admin"] if u["Username"] != "s-e2e-bk"]
    report = _run_e2e(users, groups, rows, grants)
    assert [f["email"] for f in report["cognito_without_row"]] == [E2E_BOOKING]
    assert report["drift_total"] == 1


def test_a10b_with_another_group_or_a_booking_group_stays_drift():
    users, groups, rows, grants = _e2e_world()
    adm = next(u for u in users if u["Username"] == "s-e2e-adm")
    groups["booking-admin"] = groups["booking-admin"] + [adm]
    report = _run_e2e(users, groups, rows, grants)
    assert [(d["username"], d["kind"]) for d in report["group_drift"]] == [("e2e-cis-admin", "booking_role")]
    assert E2E_CIS_ADMIN not in {k["email"] for k in report["known_e2e_exceptions"]}
    assert report["drift_total"] == 1


def test_a10_with_cis_admin_instead_of_cis_reviewer_stays_drift():
    users, groups, rows, grants = _e2e_world()
    rev = next(u for u in users if u["Username"] == "s-e2e")
    groups["cis-reviewer"] = []
    groups["cis-admin"] = groups["cis-admin"] + [rev]
    report = _run_e2e(users, groups, rows, grants)
    assert [(d["username"], d["kind"], d["directory"], d["cognito"]) for d in report["group_drift"]] == [
        ("e2e", "cis_role", "reviewer", "admin")]
    assert E2E_REV not in {k["email"] for k in report["known_e2e_exceptions"]}


def test_a10_or_a10b_without_their_directory_row_stays_drift():
    users, groups, rows, grants = _e2e_world()
    rows = [r for r in rows if r["username"] not in ("e2e", "e2e-cis-admin")]
    report = _run_e2e(users, groups, rows, grants)
    assert sorted((f["email"], f["reason"]) for f in report["cognito_without_row"]) == [
        (E2E_CIS_ADMIN, "no_row"), (E2E_REV, "no_row")]
    assert [k["email"] for k in report["known_e2e_exceptions"]] == [E2E_BOOKING]
    assert report["drift_total"] == 2


def test_a10b_row_with_the_wrong_role_stays_drift():
    users, groups, rows, grants = _e2e_world()
    for r in rows:
        if r["username"] == "e2e-cis-admin":
            r["role"] = "reviewer"
    report = _run_e2e(users, groups, rows, grants)
    assert [(d["username"], d["kind"]) for d in report["group_drift"]] == [("e2e-cis-admin", "cis_role")]
    assert E2E_CIS_ADMIN not in {k["email"] for k in report["known_e2e_exceptions"]}


@pytest.mark.parametrize("email", [
    "e2e-booking@e2e-mail.lumiguides.it.com.evil",          # another domain
    "x.e2e-booking@e2e-mail.lumiguides.it.com",             # another local part
    "e2e-booking@sub.e2e-mail.lumiguides.it.com",           # a sub-domain
    "e2e-boo\u212aing@e2e-mail.lumiguides.it.com",          # KELVIN SIGN: lower() folds it to "k"
])
def test_only_the_exact_ascii_address_is_a_known_exception(email):
    users, groups, rows, grants = _clean_world()
    look = _user(email, "s-look")
    users.append(look)
    groups["booking-admin"] = groups["booking-admin"] + [look]
    report = _run_e2e(users, groups, rows, grants)
    assert report["known_e2e_exceptions"] == []
    assert [f["reason"] for f in report["cognito_without_row"]] == ["no_row"] and report["drift_total"] == 1


def test_a_disabled_a10a_identity_is_still_listed_with_its_state():
    users, groups, rows, grants = _e2e_world()
    for u in users:
        if u["Username"] == "s-e2e-bk":
            u["Enabled"] = False
    report = _run_e2e(users, groups, rows, grants)
    bk = next(k for k in report["known_e2e_exceptions"] if k["email"] == E2E_BOOKING)
    assert bk["enabled"] is False and bk["groups"] == ["booking-admin"] and report["drift_total"] == 0


def test_summary_fields_count_known_e2e_without_addresses():
    fields = sr.summary_fields(_run_e2e(*_e2e_world()))
    assert fields["known_e2e_exceptions"] == 3 and fields["drift_total"] == 0
    assert "@" not in repr(fields)


# ── the job ───────────────────────────────────────────────────────────────────────────────────────

class _Conn:
    """``crm``: the CRM grants once migration 330 is applied (AA-1031), or None before it."""

    def __init__(self, rows, grants, crm=None):
        self.rows, self.grants, self.crm = rows, grants, crm

    async def fetch(self, sql, *args):
        if sql == sr.CRM_GRANTS_SQL:
            assert self.crm is not None, "staff_crm_grant read before the table check"
            return self.crm
        return self.grants if "staff_app_grant" in sql else self.rows

    async def fetchval(self, sql, *args):
        assert sql == sr.CRM_TABLE_SQL
        return self.crm is not None


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def test_kind_is_registered_on_the_worker():
    from shared.jobs import worker

    worker.load_kinds()
    k = registry.kinds()["staff_directory_reconcile"]
    assert k.background and k.concurrency == 1 and k.max_attempts == 2 and not k.deploy_gate_exempt


@pytest.mark.asyncio
async def test_job_runs_end_to_end_and_logs_counts(monkeypatch):
    from services.jobs import staff_reconcile_job as job
    from shared.jobs import queue

    monkeypatch.setattr(queue, "set_progress", AsyncMock())
    monkeypatch.delenv("COGNITO_WORKFORCE_ISSUER", raising=False)
    users, groups, rows, grants = _clean_world()
    ghost = _user("ghost@adventure.asia", "s-ghost")
    client = _client()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire(_Conn(rows, grants)))
    with Stubber(client) as stub:
        stub.add_response("list_users", {"Users": users[:1], "PaginationToken": "p2"},
                          {"UserPoolId": POOL, "Limit": 60})
        stub.add_response("list_users", {"Users": users[1:] + [ghost]},
                          {"UserPoolId": POOL, "Limit": 60, "PaginationToken": "p2"})
        for g in sr.STAFF_GROUPS:
            stub.add_response("list_users_in_group", {"Users": groups[g]},
                              {"UserPoolId": POOL, "GroupName": g, "Limit": 60})
        ctx = registry.JobContext(pool, Job(id="job-1", kind=job.KIND, payload={"trigger": "schedule"},
                                            status="running", attempt=1, max_attempts=2, progress={}, cost_usd=0.0),
                                  {"cognito": client})
        with capture_logs() as logs:
            result = await job.run(ctx)
        stub.assert_no_pending_responses()
    assert result["trigger"] == "schedule" and result["pool_id"] == POOL and result["kind"] == job.KIND
    assert result["counts"]["cognito_without_row"] == 1 and result["drift_total"] == 1
    assert ctx.result is result
    line = next(e for e in logs if e["event"] == "staff_reconcile_summary")
    assert line["cognito_without_row"] == 1 and line["drift_total"] == 1 and line["ready"] is True
    assert "ghost" not in repr(line)


@pytest.mark.asyncio
@pytest.mark.parametrize("environment,drift,known", [("dev", 0, 1), ("prod", 1, 0)])
async def test_job_lists_the_a10a_identity_apart_only_on_dev(monkeypatch, environment, drift, known):
    from services.jobs import staff_reconcile_job as job
    from shared.jobs import queue

    monkeypatch.setattr(queue, "set_progress", AsyncMock())
    monkeypatch.delenv("COGNITO_WORKFORCE_ISSUER", raising=False)
    monkeypatch.setenv("ENVIRONMENT", environment)
    users, groups, rows, grants = _clean_world()
    bk = _user(E2E_BOOKING, "s-e2e-bk", status="FORCE_CHANGE_PASSWORD")
    groups["booking-admin"] = groups["booking-admin"] + [bk]
    client = _client()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire(_Conn(rows, grants)))
    with Stubber(client) as stub:
        stub.add_response("list_users", {"Users": users + [bk]}, {"UserPoolId": POOL, "Limit": 60})
        for g in sr.STAFF_GROUPS:
            stub.add_response("list_users_in_group", {"Users": groups[g]},
                              {"UserPoolId": POOL, "GroupName": g, "Limit": 60})
        ctx = registry.JobContext(pool, Job(id="job-3", kind=job.KIND, payload={"trigger": "manual"},
                                            status="running", attempt=1, max_attempts=2, progress={}, cost_usd=0.0),
                                  {"cognito": client})
        result = await job.run(ctx)
    assert result["drift_total"] == drift and result["counts"]["known_e2e_exceptions"] == known
    assert result["known_e2e_enabled"] is (environment == "dev")


@pytest.mark.asyncio
async def test_job_access_denied_is_non_retryable(monkeypatch):
    from services.jobs import staff_reconcile_job as job
    from shared.jobs import queue

    monkeypatch.setattr(queue, "set_progress", AsyncMock())
    client = _client()
    with Stubber(client) as stub:
        stub.add_client_error("list_users", service_error_code="AccessDeniedException")
        ctx = registry.JobContext(MagicMock(), Job(id="job-2", kind=job.KIND, payload={}, status="running", attempt=1,
                                                   max_attempts=2, progress={}, cost_usd=0.0), {"cognito": client})
        with pytest.raises(registry.NonRetryable):
            await job.run(ctx)


@pytest.mark.asyncio
async def test_scheduled_enqueue_is_keyed_per_day(monkeypatch):
    from services.jobs import staff_reconcile_job as job

    enq = AsyncMock(return_value=("j-1", True))
    monkeypatch.setattr(registry, "enqueue", enq)
    assert await job.enqueue_scheduled(MagicMock(), day="2026-10-18") == ("j-1", True)
    args, kw = enq.call_args
    assert args[1:] == (job.KIND, {"trigger": "schedule"})
    assert kw["idempotency_key"] == "staff_directory_reconcile:schedule:2026-10-18"


class _TxConn:
    """asyncpg connection stand-in for enqueue_manual: records SQL, answers the active-run query."""

    def __init__(self, active):
        self.active = active
        self.executed: list[tuple] = []
        self.in_tx = False

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.in_tx = True

            async def __aexit__(self, *exc):
                conn.in_tx = False
                return False

        return _Tx()

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetchval(self, sql, *args):
        assert self.in_tx and self.executed, "the active-run check must run under the lock"
        return self.active


class _TxPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_manual_enqueue_returns_the_active_run(monkeypatch):
    from services.jobs import staff_reconcile_job as job

    enq = AsyncMock(return_value=("j-new", True))
    monkeypatch.setattr(registry, "enqueue", enq)
    conn = _TxConn("j-active")
    assert await job.enqueue_manual(_TxPool(conn), requested_by="u1") == ("j-active", False)
    enq.assert_not_called()
    assert "pg_advisory_xact_lock" in conn.executed[0][0]

    conn = _TxConn(None)
    assert await job.enqueue_manual(_TxPool(conn), requested_by="u1") == ("j-new", True)
    # the insert goes through the locked connection, inside the transaction (review N1)
    assert enq.call_args.args[0] is conn
    assert enq.call_args.args[2] == {"trigger": "manual", "requested_by": "u1"}
    assert enq.call_args.kwargs["created_by"] == "admin:u1"


def _full_job():
    users, groups, rows, grants = _clean_world()
    users.append(_user("ghost@adventure.asia", "s-ghost"))
    report = _run(users, groups, rows, grants)
    report.update(kind="staff_directory_reconcile", trigger="manual", checked_at="2026-10-18T00:00:00+00:00")
    return {"id": "j-rec", "kind": "staff_directory_reconcile", "status": "succeeded",
            "payload": {"trigger": "manual", "requested_by": "u-admin"}, "progress": {"drift_total": 1},
            "result": report, "error": None, "created_at": None}


def test_redact_job_keeps_counts_only():
    from services.jobs import staff_reconcile_job as job

    out = job.redact_job(_full_job())
    assert out["result"]["redacted"] is True and out["result"]["drift_total"] == 1
    assert out["result"]["counts"]["cognito_without_row"] == 1 and out["result"]["ready"] is True
    assert set(out["result"]) == {"kind", "trigger", "checked_at", "drift_total", "counts", "ready", "redacted"}
    assert out["payload"] == {"trigger": "manual"}
    assert "@" not in repr(out)


@pytest.mark.asyncio
async def test_health_returns_latest_success_and_hides_older_failures():
    from services.jobs import staff_reconcile_job as job

    t = lambda h: datetime(2026, 10, 18, h, tzinfo=timezone.utc)  # noqa: E731
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[
        {"bucket": "failed", "id": "f", "status": "failed", "created_at": t(1), "started_at": t(1),
         "finished_at": t(1), "result": None, "error": "AccessDeniedException"},
        {"bucket": "succeeded", "id": "s", "status": "succeeded", "created_at": t(2), "started_at": t(2),
         "finished_at": t(2), "result": '{"drift_total": 0}', "error": None},
    ])
    out = await job.health(pool)
    assert out["latest"]["job_id"] == "s" and out["latest"]["result"] == {"drift_total": 0}
    assert out["last_failed"] is None and out["active"] is None


# ── routes ────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "aa816-c7-test-secret"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "pool", MagicMock(), raising=False)
    return TestClient(app, raise_server_exceptions=True)


def _h(role):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "u-admin"}
    if role is not None:
        h["x-admin-role"] = role
    return h


def test_policy_entries():
    assert POLICY[("GET", "/admin/platform/staff-directory/health")] == STAFF_READ
    assert POLICY[("POST", "/admin/platform/staff-directory/reconcile")] == STAFF_READ
    assert POLICY[("POST", "/admin/job-runner/staff-reconcile/schedule")] == OPS_WRITE
    assert ("POST", "/admin/job-runner/staff-reconcile/schedule") in MACHINE_ROUTES
    assert ("POST", "/admin/platform/staff-directory/reconcile") not in MACHINE_ROUTES


@pytest.mark.parametrize("method,path", [("GET", "/admin/platform/staff-directory/health"),
                                         ("POST", "/admin/platform/staff-directory/reconcile")])
@pytest.mark.parametrize("role", ["reviewer", "content", None])
def test_directory_routes_refuse_non_admin_and_machine(client, method, path, role):
    assert client.request(method, path, headers=_h(role)).status_code == 403


def test_health_and_reconcile_for_admin(client, monkeypatch):
    from services.jobs import staff_reconcile_job as job

    monkeypatch.setattr(job, "health", AsyncMock(return_value={"kind": job.KIND, "latest": None}))
    manual = AsyncMock(return_value=("j-9", True))
    monkeypatch.setattr(job, "enqueue_manual", manual)
    assert client.get("/admin/platform/staff-directory/health", headers=_h("admin")).json()["kind"] == job.KIND
    resp = client.post("/admin/platform/staff-directory/reconcile", headers=_h("admin"))
    assert resp.status_code == 200 and resp.json() == {"kind": job.KIND, "job_id": "j-9", "created": True}
    assert manual.call_args.kwargs["requested_by"] == "u-admin"


def test_schedule_is_a_machine_route_keyed_per_utc_day(client, monkeypatch):
    from services.jobs import staff_reconcile_job as job

    sched = AsyncMock(return_value=("j-1", False))
    monkeypatch.setattr(job, "enqueue_scheduled", sched)
    resp = client.post("/admin/job-runner/staff-reconcile/schedule", headers=_h(None))
    assert resp.status_code == 200, resp.text
    assert resp.json()["day"] == datetime.now(timezone.utc).date().isoformat() and resp.json()["created"] is False
    assert client.post("/admin/job-runner/staff-reconcile/schedule", headers=_h("reviewer")).status_code == 403
    assert client.post("/admin/job-runner/staff-reconcile/schedule",
                       headers={"x-admin-role": "admin"}).status_code == 403


@pytest.fixture
def job_reads(monkeypatch):
    """The job-runner reads backed by one reconcile job and one other job (review M1)."""
    from shared.jobs import queue

    other = {"id": "j-other", "kind": "s1_rewrite", "status": "succeeded", "payload": {"tour_id": "t1"},
             "progress": None, "result": {"ok": True}, "error": None, "created_at": None}
    monkeypatch.setattr(queue, "get", AsyncMock(side_effect=lambda pool, jid: _full_job()))
    monkeypatch.setattr(queue, "domain_links", AsyncMock(return_value=None))
    monkeypatch.setattr(queue, "list_jobs", AsyncMock(side_effect=lambda *a, **k: [_full_job(), dict(other)]))
    monkeypatch.setattr(queue, "count_jobs", AsyncMock(return_value=2))


@pytest.mark.parametrize("role", ["reviewer", "content", None])
def test_job_runner_reads_redact_the_report_without_staff_read(client, job_reads, role):
    one = client.get("/admin/job-runner/jobs/j-rec", headers=_h(role))
    assert one.status_code == 200, one.text
    assert one.json()["result"]["redacted"] is True and "cognito_without_row" not in one.json()["result"]
    assert "@" not in one.text and "requested_by" not in one.text
    listed = client.get("/admin/job-runner/jobs?kind=staff_directory_reconcile", headers=_h(role)).json()["jobs"]
    rec, other = listed
    assert rec["result"]["redacted"] is True and "breakglass" not in rec["result"]
    assert other["result"] == {"ok": True} and other["payload"] == {"tour_id": "t1"}   # other kinds untouched


def test_job_runner_reads_return_the_full_report_to_admin(client, job_reads):
    one = client.get("/admin/job-runner/jobs/j-rec", headers=_h("admin")).json()
    assert "redacted" not in one["result"]
    assert one["result"]["cognito_without_row"][0]["email"] == "ghost@adventure.asia"
    assert one["payload"]["requested_by"] == "u-admin"


# ── AA-1031: CRM roles (shared.staff_crm_grant, migration 330) vs the crm-* groups; A10c ────────────

def _crm_grant(uid: str, role: str) -> dict:
    return {"admin_user_id": uid, "app": "crm", "role": role}


def test_crm_group_map_matches_the_directory_writer_and_the_migration():
    from api.services import staff_directory

    assert dict((r, g) for g, r in sr.CRM_GROUP_ROLES) == dict(staff_directory.CRM_ROLE_GROUPS)
    mig = (REPO / "api" / "migrations" / "330_staff_crm_grant.sql").read_text()
    check = re.search(r"staff_crm_grant_role_chk CHECK \(role IN \(([^)]*)\)\)", mig).group(1)
    assert {r for _, r in sr.CRM_GROUP_ROLES} == set(re.findall(r"'([a-z_]+)'", check))
    assert set(sr.CRM_GROUPS) <= set(sr.STAFF_GROUPS)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", sr.CRM_GRANTS_SQL + sr.CRM_TABLE_SQL)


def test_crm_roles_in_step_with_the_groups_are_no_drift():
    users, groups, rows, grants = _clean_world()
    groups["crm-platform-admin"] = [NGHIEP]
    groups["crm-ops"] = [NGHIEP]
    grants += [_crm_grant("id-nghiep", "ops"), _crm_grant("id-nghiep", "platform_admin")]
    report = _run(users, groups, rows, grants)
    assert report["drift_total"] == 0, report
    assert report["crm_compared"] is True and sr.CRM_NOT_COMPARED_NOTE not in report["notes"]


def test_a_crm_group_given_by_hand_is_crm_role_drift():
    """S263: crm-platform-admin added by command, no CRM role in the directory -> a finding until the Staff page
    grants it (a sync would remove the group)."""
    users, groups, rows, grants = _clean_world()
    groups["crm-platform-admin"] = [NGHIEP]
    report = _run(users, groups, rows, grants)
    assert [(d["username"], d["kind"], d["directory"], d["cognito"], d["cognito_groups"])
            for d in report["group_drift"]] == [("nghiep", "crm_role", None, "platform_admin",
                                                 ["crm-platform-admin"])]
    assert report["drift_total"] == 1


def test_a_granted_crm_role_missing_in_cognito_is_drift():
    users, groups, rows, grants = _clean_world()
    groups["crm-support"] = [NGHIEP]
    grants += [_crm_grant("id-nghiep", "support"), _crm_grant("id-nghiep", "ops")]
    report = _run(users, groups, rows, grants)
    assert [(d["kind"], d["directory"], d["cognito"]) for d in report["group_drift"]] == [
        ("crm_role", "ops, support", "support")]


def test_before_migration_330_crm_groups_are_not_compared():
    users, groups, rows, grants = _clean_world()
    groups["crm-platform-admin"] = [NGHIEP]
    report = sr.reconcile(users, groups, rows, grants, issuer=ISS, breakglass=BG, crm_directory=False)
    assert report["group_drift"] == [] and report["drift_total"] == 0
    assert report["crm_compared"] is False and sr.CRM_NOT_COMPARED_NOTE in report["notes"]


def _a10c_world(extra_groups=()):
    users, groups, rows, grants = _e2e_world()
    crm = _user(E2E_CRM_STAFF, "s-e2e-crm", status="FORCE_CHANGE_PASSWORD")
    users.append(crm)
    for g in ("crm-ops", "crm-support") + tuple(extra_groups):
        groups[g] = groups[g] + [crm]
    return users, groups, rows, grants


def test_on_dev_the_a10c_identity_is_listed_apart_and_not_drift():
    report = _run_e2e(*_a10c_world())
    assert report["drift_total"] == 0, report
    a10c = next(k for k in report["known_e2e_exceptions"] if k["email"] == E2E_CRM_STAFF)
    assert (a10c["clause"], a10c["exempt_from"], a10c["groups"]) == (
        "ADR 0006a A10c", ["cognito_without_row"], ["crm-ops", "crm-support"])
    assert report["counts"]["known_e2e_exceptions"] == 4


@pytest.mark.parametrize("extra", ["crm-platform-admin", "cis-admin", "booking-admin"])
def test_a10c_with_an_unexpected_group_stays_drift(extra):
    report = _run_e2e(*_a10c_world([extra]))
    assert (E2E_CRM_STAFF, "no_row") in [(f["email"], f["reason"]) for f in report["cognito_without_row"]]
    assert E2E_CRM_STAFF not in {k["email"] for k in report["known_e2e_exceptions"]}


def test_off_dev_the_a10c_identity_is_drift():
    report = _run_e2e(*_a10c_world(), known_e2e=False)
    assert (E2E_CRM_STAFF, "no_row") in [(f["email"], f["reason"]) for f in report["cognito_without_row"]]


@pytest.mark.asyncio
async def test_load_directory_reads_crm_grants_only_when_the_table_exists():
    users, groups, rows, grants = _clean_world()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire(_Conn(rows, grants)))
    got_rows, got_grants, crm = await sr.load_directory_with_crm(pool)
    assert crm is False and got_grants == grants and got_rows == rows
    crm_rows = [_crm_grant("id-nghiep", "ops")]
    pool.acquire = MagicMock(return_value=_Acquire(_Conn(rows, grants, crm=crm_rows)))
    _, got_grants, crm = await sr.load_directory_with_crm(pool)
    assert crm is True and got_grants == grants + crm_rows
    assert await sr.load_directory(pool) == (rows, grants + crm_rows)


@pytest.mark.asyncio
async def test_job_passes_crm_directory_to_the_report(monkeypatch):
    from services.jobs import staff_reconcile_job as job
    from shared.jobs import queue

    monkeypatch.setattr(queue, "set_progress", AsyncMock())
    monkeypatch.delenv("COGNITO_WORKFORCE_ISSUER", raising=False)
    users, groups, rows, grants = _clean_world()
    groups["crm-platform-admin"] = [NGHIEP]
    client = _client()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire(_Conn(rows, grants, crm=[])))
    with Stubber(client) as stub:
        stub.add_response("list_users", {"Users": users}, {"UserPoolId": POOL, "Limit": 60})
        for g in sr.STAFF_GROUPS:
            stub.add_response("list_users_in_group", {"Users": groups[g]},
                              {"UserPoolId": POOL, "GroupName": g, "Limit": 60})
        ctx = registry.JobContext(pool, Job(id="job-4", kind=job.KIND, payload={"trigger": "manual"},
                                            status="running", attempt=1, max_attempts=2, progress={}, cost_usd=0.0),
                                  {"cognito": client})
        result = await job.run(ctx)
    assert result["crm_compared"] is True
    assert [d["kind"] for d in result["group_drift"]] == ["crm_role"]
