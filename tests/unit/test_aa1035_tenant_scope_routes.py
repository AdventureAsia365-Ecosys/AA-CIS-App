"""AA-1035 (R2 slice 1, root ADR 0019 d4) — CI guard: every staff route with a {tenant_id} path parameter runs
api/core/tenant_scope.require_tenant_scope.

ADR 0019 d4: "Every {tenant_id} route uses the require_tenant_scope dependency. A CI test checks that every such route
uses it." The scan walks the real app (the same walker as the S241 / AA-882 route tests), so a new route that forgets
``dependencies=TENANT_SCOPED`` fails here, before it can serve one tenant's data to a customer_success member of
another. It also fails when:
- a staff {tenant_id} route has no POLICY entry (an unlisted GET would fall back to the content.read rule, which is not
  a tenant decision);
- a route OUTSIDE the staff prefixes takes a {tenant_id} path parameter and is not one of the reviewed tenant-token
  routes below (those check the tenant of the token against the path in their own guard; a new one is a reviewed
  change of this list).
"""
from __future__ import annotations

from fastapi.routing import APIRoute

from api.core.staff_authz import POLICY, STAFF_PATH_PREFIXES
from api.core.tenant_scope import TENANT_SCOPED, require_tenant_scope
from tests.unit.test_s241_admin_routes_require_auth import (
    _dependency_calls,
    _inherited_calls,
    _served_routes,
    _walk,
)

# Tenant-facing routes with {tenant_id} in the path, outside the staff prefixes: their own guard ties the path tenant
# to the caller's tenant token or M2M client (reviewed list; method + path template).
TENANT_TOKEN_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/tenant/v1/tenants/{tenant_id}/account"),
    ("GET", "/tenant/membership/v1/tenants/{tenant_id}/members"),
    # AA-1036 R3 (tenant.addons.v1): the CRM's M2M client (scope cis.tenant/addons) reads an agency's add-ons and relays
    # its CRM owner's request; the CRM checks the owner role (api/routers/tenant_addons.py).
    ("GET", "/tenant/v1/tenants/{tenant_id}/add-ons"),
    ("POST", "/tenant/v1/tenants/{tenant_id}/add-on-requests"),
})


def _is_staff_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in STAFF_PATH_PREFIXES)


def _tenant_routes():
    from api.main import app

    for path, route, inherited in _walk(_served_routes(app)):
        if not isinstance(route, APIRoute) or "{tenant_id}" not in path:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            yield method, path, route, inherited


def _runs_scope(route, inherited, path) -> bool:
    calls = set(_dependency_calls(route.dependant)) | set(_inherited_calls(inherited, path))
    return require_tenant_scope in calls


def test_the_scan_sees_the_tenant_routes():
    staff = [(m, p) for m, p, _, _ in _tenant_routes() if _is_staff_path(p)]
    assert len(staff) >= 49, staff        # 47 before R2 + the three assignment routes - generate-key (AA-1036)
    assert ("GET", "/admin/tenants/{tenant_id}/usage") in staff
    assert ("PUT", "/admin/tenants/{tenant_id}/assignments/{admin_user_id}") in staff


def test_every_staff_tenant_route_runs_require_tenant_scope():
    missing = sorted(f"{m} {p} ({r.endpoint.__module__})" for m, p, r, inh in _tenant_routes()
                     if _is_staff_path(p) and not _runs_scope(r, inh, p))
    assert not missing, ("staff routes with {tenant_id} must declare dependencies=TENANT_SCOPED "
                         "(api/core/tenant_scope.py):\n" + "\n".join(missing))


def test_every_staff_tenant_route_has_a_policy_entry():
    missing = sorted(f"{m} {p}" for m, p, _, _ in _tenant_routes() if _is_staff_path(p) and (m, p) not in POLICY)
    assert not missing, missing


def test_tenant_routes_outside_the_staff_prefixes_are_the_reviewed_tenant_token_routes():
    found = {(m, p) for m, p, _, _ in _tenant_routes() if not _is_staff_path(p)}
    assert found == TENANT_TOKEN_ROUTES, sorted(found ^ TENANT_TOKEN_ROUTES)


def test_the_shared_dependency_list_is_the_scope_dependency_only():
    assert [d.dependency for d in TENANT_SCOPED] == [require_tenant_scope]
