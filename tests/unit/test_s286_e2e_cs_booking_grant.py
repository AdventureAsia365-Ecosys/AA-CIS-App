"""S286 live check F3 (AA-1150): why the A10d account ``e2e-cis-cs@`` lost ``booking-contracting``, pinned.

Finding: on 14/11/2026 13:21Z the CIS task role called AdminRemoveUserFromGroup(booking-contracting) for the A10d
address, so the one-sign-in spec's Booking hop was refused (``reason=forbidden``). The suspected path, the staff
directory reconcile or Sync stripping a fixed-set E2E identity, is not it:

- the reconcile (``services/staff_reconcile.py`` + its job) is report only: it never calls a Cognito write;
- Sync makes the pool match the directory row, and keeps ``booking-contracting`` while the row holds the Pricing team
  grant (root ADR 0006a A10e-2);
- the removal was the A10e-2 revoke step itself: after the AA-1091 run the owner set the account's Booking role back
  to None (``change_roles(booking_role=None)``, one audited ``roles_changed`` row), which removes the group
  (tests/unit/test_aa1091_e2e_cs_pricing_preset.py). The account's resting state has no Booking role.

A ``booking-contracting`` added in Cognito alone (no directory grant) is drift: the reconcile reports it and the next
Sync removes it, so the only lasting way back is the preset on the Staff page. A real staff member's extra group is
still removed by Sync.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from api.services import staff_directory as sd
from services import staff_reconcile as sr
from tests.unit.test_aa1035_a10d_e2e_cs import (  # noqa: F401 — db and stubbed are fixtures
    CS_E2E, CS_GROUP, db, stubbed,
)
from tests.unit.test_aa816_e2e_cis_admin import _set_env
from tests.unit.test_aa816_staff_directory_writes import _cis_but, _create, _expect, _expect_error, _group, _user
from tests.unit.test_aa816_staff_reconcile import _e2e_world, _row, _run_e2e
from tests.unit.test_aa816_staff_reconcile import _user as _pool_user

CONTRACTING = "booking-contracting"
REPO = Path(__file__).resolve().parents[2]
# Every Cognito write the staff directory makes (CognitoDirectory), as boto3 method names.
COGNITO_WRITES = {"admin_create_user", "admin_add_user_to_group", "admin_remove_user_from_group",
                  "admin_disable_user", "admin_enable_user", "admin_user_global_sign_out"}


def _names(path: Path) -> set[str]:
    """Every attribute and function name a module uses, and the snake_case of every Cognito operation string."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("Admin"):
            out.add(sd._snake(node.value))
    return out


@pytest.mark.parametrize("rel", ["services/staff_reconcile.py", "services/jobs/staff_reconcile_job.py"])
def test_the_reconcile_never_writes_to_cognito(rel):
    names = _names(REPO / rel)
    assert not names & COGNITO_WRITES, rel
    # nor through the staff directory's writer
    assert not names & {"CognitoDirectory", "get_cognito", "sync_staff", "change_roles", "remove_from_group"}, rel


def _sync_steps(stub, email, cis_group, wanted):
    _expect_error(stub, "admin_create_user", "UsernameExistsException", _create(email))
    _expect(stub, "admin_enable_user", _user(email))
    for g in wanted:
        _expect(stub, "admin_add_user_to_group", _group(email, g))
    for g in _cis_but(cis_group) + tuple(g for g in sd.BOOKING_GROUPS if g not in wanted) + sd.CRM_GROUPS:
        _expect(stub, "admin_remove_user_from_group", _group(email, g))


@pytest.mark.asyncio
async def test_sync_keeps_booking_contracting_while_the_e2e_row_holds_the_pricing_grant(
        db, stubbed, monkeypatch):  # noqa: F811
    _set_env(monkeypatch, "dev")
    cognito, stub = stubbed
    uid = db.add(email=CS_E2E, role="customer_success", username="e2e-cis-cs", booking="contracting")
    _sync_steps(stub, CS_E2E, CS_GROUP, [CS_GROUP, CONTRACTING])   # stub order: never a remove of contracting
    result = await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK
    assert f"remove_group:{CONTRACTING}" not in result.steps and f"add_group:{CONTRACTING}" in result.steps
    assert db.grants[(uid, "booking")] == "contracting"


@pytest.mark.asyncio
async def test_sync_removes_booking_contracting_from_the_e2e_account_at_rest(db, stubbed, monkeypatch):  # noqa: F811
    # The resting state (the owner removed the preset): the row has no Booking grant, so the group goes.
    _set_env(monkeypatch, "dev")
    cognito, stub = stubbed
    uid = db.add(email=CS_E2E, role="customer_success", username="e2e-cis-cs")
    _sync_steps(stub, CS_E2E, CS_GROUP, [CS_GROUP])
    result = await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK and f"remove_group:{CONTRACTING}" in result.steps


@pytest.mark.asyncio
async def test_sync_still_strips_a_real_staff_members_extra_booking_group(db, stubbed):  # noqa: F811
    email = "ops.lead@example.com"
    cognito, stub = stubbed
    uid = db.add(email=email, role="customer_success", username="ops-lead")
    _sync_steps(stub, email, CS_GROUP, [CS_GROUP])
    result = await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK and f"remove_group:{CONTRACTING}" in result.steps


def _cs_world(groups_held, booking_grant):
    users, groups, rows, grants = _e2e_world()
    cs = _pool_user(CS_E2E, "s-e2e-cs")
    users.append(cs)
    for g in groups_held:
        groups[g] = list(groups.get(g, [])) + [cs]
    rows.append(_row("e2e-cis-cs", role="customer_success", email=CS_E2E, sub="s-e2e-cs"))
    if booking_grant:
        grants.append({"admin_user_id": "id-e2e-cis-cs", "app": "booking", "role": booking_grant})
    return users, groups, rows, grants


def test_the_reconcile_reports_a_cognito_only_contracting_as_drift():
    # booking-contracting re-added in Cognito by hand, the directory row still without the grant: drift, not a known
    # exception, so the Staff page shows it and Sync would remove it.
    report = _run_e2e(*_cs_world([CS_GROUP, CONTRACTING], None))
    assert {(d["email"], d["kind"], d["directory"], d["cognito"]) for d in report["group_drift"]} == {
        (CS_E2E, "booking_role", None, "contracting")}
    assert CS_E2E not in {k["email"] for k in report["known_e2e_exceptions"]}


@pytest.mark.parametrize("held,grant", [([CS_GROUP], None), ([CS_GROUP, CONTRACTING], "contracting")])
def test_the_reconcile_accepts_both_a10d_states_when_the_row_agrees(held, grant):
    report = _run_e2e(*_cs_world(held, grant))
    assert report["drift_total"] == 0, report
    assert CS_E2E in {k["email"] for k in report["known_e2e_exceptions"]}
    assert sr.KNOWN_E2E_STAFF[CS_E2E].allows(set(held))
