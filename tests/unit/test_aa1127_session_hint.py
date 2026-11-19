"""AA-1127 C3 — GET /admin/session: the admin shell's read-only hint. Every staff role may ask; the answer follows the
same rules the API applies to writes (a read-only role, a Dev-only E2E identity), so the shell hides what the API
would refuse anyway."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from api.core import staff_authz
from api.routers import admin_session as router_mod


def _principal(role: str, *, e2e_read_only: bool = False) -> staff_authz.StaffPrincipal:
    identity = SimpleNamespace(auth="cognito", is_owner=False, e2e_read_only=e2e_read_only)
    return staff_authz.principal_from_role(role, identity)


def test_route_is_open_to_every_staff_role():
    capability = staff_authz.POLICY[("GET", "/admin/session")]
    assert capability == staff_authz.NOTIFICATIONS_READ
    for role, perms in staff_authz.ROLE_PERMS.items():
        assert capability in perms, role
        assert staff_authz.denial_reason(_principal(role), "GET", "/admin/session") is None, role


@pytest.mark.parametrize("role", ["admin", "content_lead", "content", "tenant_qa", "customer_success", "support",
                                  "finance", "ai_ops"])
def test_a_writing_role_is_not_read_only(role):
    hint = router_mod.session_hint(_principal(role))
    assert hint.read_only is False and hint.reason is None and hint.role == role


def test_viewer_is_read_only_by_role():
    hint = router_mod.session_hint(_principal("viewer"))
    assert hint.read_only is True and hint.reason == "role"


def test_dev_e2e_admin_is_read_only_like_the_api():
    """The Dev E2E admin holds the admin role, but the API refuses its writes (403 e2e_read_only)."""
    principal = _principal("admin", e2e_read_only=True)
    assert staff_authz.denial_reason(principal, "POST", "/admin/writing-memory/run") is not None
    hint = router_mod.session_hint(principal)
    assert hint.read_only is True and hint.reason == "e2e" and hint.role == "admin"


def test_route_returns_the_callers_hint():
    out = asyncio.run(router_mod.get_session(principal=_principal("viewer")))
    assert out.model_dump() == {"role": "viewer", "read_only": True, "reason": "role"}
