"""AA-882 (S241 GĐ1) — api/core/staff_authz.py: capabilities per role, policy per (method, route).

Covers:
- the decision table: every POLICY route x every principal (admin, reviewer, content, unknown role,
  machine) -> allowed or refused, machine only on MACHINE_ROUTES;
- the real app: reviewer/content/machine are refused on every POLICY route before the endpoint (or
  the DB) runs; admin and the machine callers still reach their endpoints; the reads stay open
  (GET /admin/llm-config for the S1 page, the CI smoke GETs without a role header);
- the allowlists stay true: POLICY/MACHINE_ROUTES name real routes, the CI deploy workflow calls
  only machine routes, and the staff roles match frontend/lib/rbac.ts.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core import staff_authz
from api.core.staff_authz import (
    CAPABILITIES,
    MACHINE,
    MACHINE_ROUTES,
    OWNER_PERMS,
    POLICY,
    POLICY_READ_PREFIX,
    ROLE_PERMS,
    denial_reason,
    principal_from_role,
)
from api.main import app
from api.routers import admin, admin_settings
from tests.unit.test_s241_admin_routes_require_auth import _served_routes, _walk

REPO = Path(__file__).resolve().parents[2]
_SECRET = "aa882-test-admin-secret"
_STAFF_ROLES = ("admin", "reviewer", "content")


# ── The capability map ───────────────────────────────────────────────────────────────────────────

def test_role_perms_match_the_s241_matrix():
    """§5.1 of the S241 admin-console design: content work for every staff role, the rest admin."""
    content_work = {"content.read", "content.write", "review.act"}
    assert set(ROLE_PERMS) == set(_STAFF_ROLES)
    assert ROLE_PERMS["reviewer"] == content_work
    assert ROLE_PERMS["content"] == content_work
    assert ROLE_PERMS["admin"] == content_work | {
        "master.admin", "atoms.write", "photos.write", "a4.unpublish", "content.settings.write",
        "tenant.read", "tenant.write", "ops.read", "ops.write", "ai.admin", "spend.read",
        "spend.write", "staff.read", "audit.read",
    }


def test_staff_write_is_owner_only():
    assert OWNER_PERMS == {"staff.write"}
    assert not any("staff.write" in perms for perms in ROLE_PERMS.values())


def test_policy_names_known_capabilities_only():
    unknown = {cap for cap in POLICY.values() if cap not in CAPABILITIES}
    assert not unknown


def test_policy_and_machine_routes_are_admin_routes():
    for method, path in [*POLICY, *MACHINE_ROUTES]:
        assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert path.startswith("/admin/")


def test_gd1_leaves_reads_open():
    """GĐ1 narrows writes only (S1 needs GET /admin/llm-config). The one exception is AA-883 (GĐ2):
    the staff directory GETs under /admin/platform/ are admin-only from day one."""
    assert not [k for k in POLICY if k[0] == "GET" and not k[1].startswith(POLICY_READ_PREFIX)]
    assert ("GET", "/admin/llm-config") not in POLICY


# ── Principal ────────────────────────────────────────────────────────────────────────────────────

def test_no_role_header_is_the_machine():
    assert principal_from_role(None) is MACHINE
    assert MACHINE.is_machine and MACHINE.perms == frozenset()


@pytest.mark.parametrize("header", ["", "  ", "tenant", "Admin", "root", "admin,reviewer"])
def test_a_bad_role_header_is_never_the_machine(header):
    p = principal_from_role(header)
    assert not p.is_machine
    assert p.perms == frozenset()


@pytest.mark.parametrize("role", _STAFF_ROLES)
def test_staff_role_gets_its_perms(role):
    p = principal_from_role(role)
    assert p.kind == "staff" and p.role == role and p.perms == ROLE_PERMS[role]


# ── Decision table: every POLICY route x every principal ────────────────────────────────────────

@pytest.mark.parametrize("key", sorted(POLICY))
@pytest.mark.parametrize("role", _STAFF_ROLES)
def test_staff_decision_per_role_and_route(role, key):
    method, path = key
    allowed = POLICY[key] in ROLE_PERMS[role]
    reason = denial_reason(principal_from_role(role), method, path)
    assert (reason is None) is allowed, (role, key, reason)


@pytest.mark.parametrize("key", sorted(POLICY))
def test_reviewer_and_content_are_refused_every_gd1_write(key):
    """Every GĐ1 route is an admin-level write."""
    for role in ("reviewer", "content"):
        assert denial_reason(principal_from_role(role), *key) is not None


@pytest.mark.parametrize("key", sorted(POLICY))
def test_admin_may_call_every_gd1_write(key):
    assert denial_reason(principal_from_role("admin"), *key) is None


@pytest.mark.parametrize("key", sorted(POLICY))
def test_machine_only_on_machine_routes(key):
    reason = denial_reason(MACHINE, *key)
    assert (reason is None) is (key in MACHINE_ROUTES), (key, reason)


@pytest.mark.parametrize("key", sorted(POLICY))
def test_unknown_role_is_refused(key):
    assert denial_reason(principal_from_role("tenant"), *key) == "Unknown staff role"


def test_routes_without_policy_are_not_narrowed_in_gd1():
    for principal in (MACHINE, principal_from_role("content"), principal_from_role("x")):
        assert denial_reason(principal, "GET", "/admin/llm-config") is None
        assert denial_reason(principal, "POST", "/admin/run-tour-async") is None


# ── The real app ─────────────────────────────────────────────────────────────────────────────────

def _concrete(path: str) -> str:
    """A request path for a route template: every {param} gets a harmless sample value."""
    samples = {"job_id": "00000000-0000-0000-0000-000000000000", "topup_id": "1",
               "provider": "bedrock", "scope": "global"}
    return re.sub(r"\{(\w+)\}", lambda m: samples.get(m.group(1), "aa882"), path)


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    # Same setup as test_s241_admin_routes_require_auth: the routers' secret constants only (the
    # rate-limit middleware then passes admin calls through untracked), and a pool that fails the
    # test if a refused request ever reaches the database.
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=AssertionError("the DB must not be reached"))
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        setattr(pool, name, AsyncMock(side_effect=AssertionError("the DB must not be reached")))
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=True)


def _headers(role):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "aa882-test"}
    if role is not None:
        h["x-admin-role"] = role
    return h


@pytest.mark.parametrize("key", sorted(POLICY))
@pytest.mark.parametrize("role", ["reviewer", "content", "tenant", ""])
def test_app_refuses_non_admin_roles_before_the_endpoint(client, role, key):
    method, path = key
    resp = client.request(method, _concrete(path), json={}, headers=_headers(role))
    assert resp.status_code == 403, (role, key, resp.status_code, resp.text)


@pytest.mark.parametrize("key", sorted(k for k in POLICY if k not in MACHINE_ROUTES))
def test_app_refuses_the_machine_off_its_allowlist(client, key):
    method, path = key
    resp = client.request(method, _concrete(path), json={}, headers=_headers(None))
    assert resp.status_code == 403, (key, resp.text)
    assert "machine" in resp.json()["detail"]


@pytest.mark.parametrize("key", sorted(POLICY))
def test_app_still_checks_the_secret_first(client, key):
    method, path = key
    anon = client.request(method, _concrete(path), json={}, headers={"x-admin-role": "admin"})
    wrong = client.request(method, _concrete(path), json={},
                           headers={"X-Admin-Secret": _SECRET + "x", "x-admin-role": "admin"})
    assert anon.status_code == 403 and wrong.status_code == 403
    assert anon.json()["detail"] == wrong.json()["detail"] == "Invalid admin secret"


def test_app_admin_reaches_patch_llm_config(client, monkeypatch):
    from api.routers import admin_llm_ops

    monkeypatch.setattr(admin_llm_ops, "list_stage_configs", AsyncMock(return_value=[]))
    resp = client.patch("/admin/llm-config/no-such-stage", json={"model_id": "haiku"},
                        headers=_headers("admin"))
    assert resp.status_code == 404, resp.text  # past the policy, into the endpoint
    reviewer = client.patch("/admin/llm-config/no-such-stage", json={"model_id": "haiku"},
                            headers=_headers("reviewer"))
    assert reviewer.status_code == 403
    assert reviewer.json()["detail"] == "Role 'reviewer' lacks the 'ai.admin' permission"


def test_app_admin_reaches_put_budget(client):
    resp = client.put("/admin/budgets/no-such-provider/global", json={}, headers=_headers("admin"))
    assert resp.status_code == 422, resp.text  # the endpoint's own validation, before any DB call


def test_app_admin_reaches_retention_run(client):
    resp = client.post("/admin/job-runner/retention/run", json={"kind": "no-such-kind"},
                       headers=_headers("admin"))
    assert resp.status_code == 422, resp.text


def test_app_machine_reaches_retention_schedule(client, monkeypatch):
    """The ops-probe Lambda sends the secret without a role: it must keep working (S241 R2)."""
    from services.jobs import retention_job

    monkeypatch.setattr(retention_job, "schedule_all", AsyncMock(return_value={"enqueued": [], "skipped": []}))
    resp = client.post("/admin/job-runner/retention/schedule", headers=_headers(None))
    assert resp.status_code == 200, resp.text
    assert resp.json()["enqueued"] == []
    # A content user through the BFF (role forwarded) is still refused.
    assert client.post("/admin/job-runner/retention/schedule",
                       headers=_headers("content")).status_code == 403


def _tenant_proxy_headers(role):
    """What frontend/app/api/tenant/[...path]/route.ts sends upstream for a staff session."""
    return {"x-admin-secret": _SECRET, "x-admin-user-id": "aa882-test", "x-admin-role": role}


_MACHINE_POLICY_ROUTES = sorted(k for k in POLICY if k in MACHINE_ROUTES)


def test_the_machine_policy_routes_are_the_four_scheduler_posts():
    """The routes a role-less call could still reach: why the tenant proxy must forward the role."""
    assert _MACHINE_POLICY_ROUTES == sorted([
        ("POST", "/admin/dfs-balance/check"), ("POST", "/admin/jev-canary/check"),
        ("POST", "/admin/jev/402-sweep"), ("POST", "/admin/job-runner/retention/schedule"),
    ])


@pytest.mark.parametrize("key", _MACHINE_POLICY_ROUTES)
@pytest.mark.parametrize("role", ["reviewer", "content"])
def test_app_tenant_proxy_staff_branch_refuses_reviewer_and_content(client, role, key):
    """POST /api/tenant/admin/jev-canary/check (and the other machine-allowed POLICY POSTs) by a
    reviewer/content session: 403 before the endpoint (no paid Jev probe, no breaker reset)."""
    method, path = key
    resp = client.request(method, _concrete(path), json={}, headers=_tenant_proxy_headers(role))
    assert resp.status_code == 403, (role, key, resp.text)
    assert resp.json()["detail"] == f"Role '{role}' lacks the 'ops.write' permission"


def test_app_tenant_proxy_staff_branch_admin_reaches_retention_schedule(client, monkeypatch):
    from services.jobs import retention_job

    monkeypatch.setattr(retention_job, "schedule_all", AsyncMock(return_value={"enqueued": [], "skipped": []}))
    resp = client.post("/admin/job-runner/retention/schedule", headers=_tenant_proxy_headers("admin"))
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("bff", ["frontend/app/api/tenant/[...path]/route.ts",
                                 "frontend/app/api/admin/[...path]/route.ts"])
def test_bff_proxies_to_any_backend_path_forward_the_verified_role(bff):
    """Both staff proxies that carry the admin secret to an arbitrary backend path must forward the
    role from requireAdmin(): a missing x-admin-role makes the backend see the machine principal."""
    src = (REPO / bff).read_text()
    assert re.search(r'(headers\["x-admin-role"\]\s*=|"x-admin-role":)\s*auth\.role\b', src), bff


@pytest.mark.parametrize("role", [*_STAFF_ROLES, None])
def test_app_get_llm_config_stays_open_to_every_staff_role_and_the_machine(client, monkeypatch, role):
    """S1 (reviewer/content) reads the stage configs; the policy must not touch that GET (S241 R3)."""
    from api.routers import admin_llm_ops

    monkeypatch.setattr(admin_llm_ops, "list_stage_configs", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin_llm_ops, "_load_catalog", AsyncMock(return_value=[]))
    resp = client.get("/admin/llm-config", headers=_headers(role))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"stages": []}


@pytest.mark.parametrize("role", [*_STAFF_ROLES, None])
def test_app_ci_smoke_gets_stay_open(client, monkeypatch, role):
    """The deploy preflight + smoke call these with the secret and no role (S241 R2)."""
    from shared.jobs import queue

    monkeypatch.setattr(queue, "active_count", AsyncMock(return_value={"active": 0, "queued": 0, "running": 0}))
    monkeypatch.setattr(queue, "worker_health", AsyncMock(
        return_value={"workers": [], "running": [], "queued": [], "alive_seconds": 90}))
    gate = client.get("/admin/job-runner/deploy-gate", headers=_headers(role))
    assert gate.status_code == 200, gate.text
    assert gate.json()["safe_to_deploy"] is True
    assert client.get("/admin/job-runner/workers", headers=_headers(role)).status_code == 200


def test_staff_policy_fails_closed_without_a_matched_route():
    from fastapi import HTTPException
    from starlette.requests import Request

    request = Request({"type": "http", "method": "PATCH", "path": "/admin/llm-config/x",
                       "headers": [], "query_string": b""})
    with pytest.raises(HTTPException) as exc:
        staff_authz.staff_policy(request, None, "admin")
    assert exc.value.status_code == 403


# ── Allowlists stay true ─────────────────────────────────────────────────────────────────────────

def _app_keys():
    from fastapi.routing import APIRoute

    keys = set()
    for path, route, _ in _walk(_served_routes(app)):
        if isinstance(route, APIRoute):
            keys |= {(m, path) for m in route.methods}
    return keys


def test_policy_and_machine_routes_exist():
    keys = _app_keys()
    assert not [k for k in POLICY if k not in keys], "POLICY names routes that do not exist"
    assert not [k for k in MACHINE_ROUTES if k not in keys], "MACHINE_ROUTES names routes that do not exist"


def test_ci_deploy_workflow_calls_only_machine_routes():
    """deploy-dev.yml curls /admin with the secret and no x-admin-role: each path must be a machine
    GET, or the next deploy's preflight / smoke gets 403 once GĐ3 puts the GETs under POLICY."""
    workflow = (REPO / ".github" / "workflows" / "deploy-dev.yml").read_text()
    paths = set(re.findall(r"(/admin/[A-Za-z0-9_\-/]+)", workflow))
    assert paths, "the scan found no /admin path in deploy-dev.yml"
    machine_gets = {p for m, p in MACHINE_ROUTES if m == "GET"}
    assert paths <= machine_gets, f"not machine routes: {sorted(paths - machine_gets)}"
    assert "x-admin-role" not in workflow.lower()


def _ts_string_list(source: str, pattern: str) -> set[str]:
    match = re.search(pattern, source, re.S)
    assert match, pattern
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


def test_staff_roles_match_frontend_rbac():
    """ROLE_PERMS and frontend/lib/rbac.ts must name the same staff roles (the admin_role enum)."""
    rbac = (REPO / "frontend" / "lib" / "rbac.ts").read_text()
    union = _ts_string_list(rbac, r"export type StaffRole\s*=\s*([^;]+);")
    staff_roles = _ts_string_list(rbac, r"export const STAFF_ROLES[^=]*=\s*\[([^\]]*)\]")
    labels = set(re.findall(r"^\s+(\w+):\s*\"", re.search(
        r"export const ROLE_LABEL[^{]*\{([^}]*)\}", rbac, re.S).group(1), re.M))
    assert union == staff_roles == labels == set(ROLE_PERMS), (union, staff_roles, labels)
