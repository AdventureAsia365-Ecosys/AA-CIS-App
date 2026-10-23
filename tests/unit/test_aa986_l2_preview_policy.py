"""AA-986 L2 review: GET /admin/tours/{tour_id}/lifecycle-preview is admin-only (master.admin).

The dry run runs the real retire / trash / pause in a transaction it rolls back (row locks, shared.job updates,
shared.notifications inserts) and returns per-kind tenant derivative counts, so it needs the same capability as the
actions it previews. The viewer, the content roles and the machine caller (secret, no role header) are refused
before the endpoint reaches the database (the client fixture's pool fails the test otherwise).
"""
from __future__ import annotations

import pytest

from api.core.staff_authz import MACHINE, MASTER_ADMIN, POLICY, denial_reason, principal_from_role
from tests.unit.test_aa882_staff_authz import _concrete, _headers, client  # noqa: F401 (pytest fixture)

KEY = ("GET", "/admin/tours/{tour_id}/lifecycle-preview")


def test_preview_needs_master_admin_like_the_actions_it_previews():
    assert POLICY[KEY] == MASTER_ADMIN
    for action in ("trash", "deactivate"):
        assert POLICY[("PATCH", f"/admin/master/{{tour_id}}/{action}")] == MASTER_ADMIN


@pytest.mark.parametrize("role", ["viewer", "reviewer", "content"])
def test_preview_decision_refuses_non_admin_roles(role):
    assert denial_reason(principal_from_role(role), *KEY) == f"Role '{role}' lacks the 'master.admin' permission"
    assert denial_reason(principal_from_role("admin"), *KEY) is None


def test_preview_decision_refuses_the_machine():
    assert "machine" in denial_reason(MACHINE, *KEY)


@pytest.mark.parametrize("role", ["viewer", "reviewer", "content", None])
def test_app_refuses_the_preview_before_the_endpoint(client, role):  # noqa: F811
    path = _concrete(KEY[1]) + "?action=retire_source"
    resp = client.get(path, headers=_headers(role))
    assert resp.status_code == 403, (role, resp.status_code, resp.text)
