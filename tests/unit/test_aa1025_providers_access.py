"""AA-1025 C1 follow-up — GET /admin/ai-gateway/access: which write controls of the AI providers page the caller may
use. The page hides a control the API would refuse; each answer is staff_authz.denial_reason on the control's own
write route, so this file checks that the routes are real POLICY routes and that the answers follow the API."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from api.core import staff_authz
from api.routers import admin_ai_gateway as router_mod

ALL_ACTIONS = set(router_mod.PAGE_WRITES)


def _principal(role: str, *, e2e_read_only: bool = False) -> staff_authz.StaffPrincipal:
    identity = SimpleNamespace(auth="cognito", is_owner=False, e2e_read_only=e2e_read_only)
    return staff_authz.principal_from_role(role, identity)


def test_every_page_write_is_a_policy_write_route():
    for key, (method, route) in router_mod.PAGE_WRITES.items():
        assert method not in ("GET", "HEAD"), key
        assert (method, route) in staff_authz.POLICY, f"{key}: {method} {route} is not a POLICY route"


def test_the_access_route_is_spend_read():
    assert staff_authz.POLICY[("GET", "/admin/ai-gateway/access")] == staff_authz.SPEND_READ


def test_admin_may_use_every_control():
    out = router_mod.page_access(_principal("admin"))
    assert out["read_only"] is False
    assert set(out["actions"]) == ALL_ACTIONS
    assert all(out["actions"].values())


def test_dev_e2e_admin_is_read_only_like_the_api():
    """The Dev-only E2E admin holds the admin role but every write answers 403 e2e_read_only: no control shows."""
    principal = _principal("admin", e2e_read_only=True)
    for method, route in router_mod.PAGE_WRITES.values():
        assert staff_authz.denial_reason(principal, method, route) is not None
    out = router_mod.page_access(principal)
    assert out == {"read_only": True, "tighten_only": False, "actions": {k: False for k in ALL_ACTIONS}}


def test_viewer_and_content_roles_get_no_control():
    for role in ("viewer", "content", "not-a-role"):
        out = router_mod.page_access(_principal(role))
        assert out["read_only"] is True, role
        assert not any(out["actions"].values()), role


def test_actions_follow_the_capability_of_each_route(monkeypatch):
    """A role that holds spend.write but not ai.admin or ops.write sees only the money controls."""
    perms = frozenset({staff_authz.SPEND_READ, staff_authz.SPEND_WRITE})
    monkeypatch.setitem(staff_authz.ROLE_PERMS, "money-only", perms)
    out = router_mod.page_access(_principal("money-only"))
    assert out["read_only"] is False
    assert {k for k, v in out["actions"].items() if v} == {"budgets", "jev_credit", "credit"}


def test_the_credit_view_writes_share_one_answer():
    """AA-1025 B5: the Credit view's add, delete and Check now all need what the `credit` action reports, and its
    threshold save is the connection PATCH that `connections` reports, so one answer per control is exact."""
    credit = staff_authz.POLICY[router_mod.PAGE_WRITES["credit"]]
    siblings = (("DELETE", "/admin/ai-gateway/credit/entries/{entry_id}"), ("POST", "/admin/ai-gateway/credit/check"))
    for route in siblings:
        assert staff_authz.POLICY[route] == credit, route
    assert router_mod.PAGE_WRITES["connections"] == ("PATCH", "/admin/llm-connections/{connection_key}")
    read_only = router_mod.page_access(_principal("admin", e2e_read_only=True))
    assert read_only["actions"]["credit"] is False


def test_route_returns_the_callers_access():
    out = asyncio.run(router_mod.get_access(principal=_principal("admin", e2e_read_only=True)))
    assert out["read_only"] is True


def test_tighten_only_follows_settings_write(monkeypatch):
    """S280 (AA-1096): a role with spend.write but not settings.write may only tighten caps; the page is told so it
    hides Remove and explains the rule, instead of letting the API answer 403. The administrator is not."""
    assert router_mod.page_access(_principal("admin"))["tighten_only"] is False
    perms = frozenset({staff_authz.SPEND_READ, staff_authz.SPEND_WRITE})
    monkeypatch.setitem(staff_authz.ROLE_PERMS, "money-only", perms)
    assert router_mod.page_access(_principal("money-only"))["tighten_only"] is True
