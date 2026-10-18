"""AA-882 (S241 GĐ1 + GĐ3) — api/core/staff_authz.py: capabilities per role, policy per (method, route).

Covers:
- the decision table: every POLICY route x every principal (admin, reviewer, content, unknown role,
  machine) -> allowed or refused, machine only on MACHINE_ROUTES;
- the real app: a role is refused on every POLICY route whose capability it lacks before the endpoint
  (or the DB) runs; admin and the machine callers still reach their endpoints; the open reads stay
  open (GET /admin/llm-config for the S1 page, the CI smoke GETs without a role header);
- GĐ3: one sample route per router, per role, through the real app (tests/unit/test_aa882_gd3_*);
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
    AWAITING_ROUTE,
    CAPABILITIES,
    MACHINE,
    MACHINE_ROUTES,
    OWNER_PERMS,
    POLICY,
    ROLE_PERMS,
    STAFF_PATH_PREFIXES,
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
        "master.admin", "atoms.write", "photos.write", "a4.unpublish", "quality.write",
        "content.settings.write",
        "tenant.read", "tenant.write", "ops.read", "ops.write", "ai.admin", "spend.read",
        "spend.write", "staff.read", "audit.read",
    }


def test_staff_write_is_owner_only():
    assert OWNER_PERMS == {"staff.write"}
    assert not any("staff.write" in perms for perms in ROLE_PERMS.values())


def test_policy_names_known_capabilities_only():
    unknown = {cap for cap in POLICY.values() if cap not in CAPABILITIES}
    assert not unknown


def test_policy_and_machine_routes_are_staff_routes():
    for method, path in [*POLICY, *MACHINE_ROUTES]:
        assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert any(path.startswith(p + "/") for p in STAFF_PATH_PREFIXES), path
    assert all(path.startswith("/admin/") for _, path in MACHINE_ROUTES)


_CONTENT_WORK = ROLE_PERMS["reviewer"]
_ADMIN_ONLY_KEYS = sorted(k for k, cap in POLICY.items() if cap not in _CONTENT_WORK)
_CONTENT_WORK_KEYS = sorted(k for k, cap in POLICY.items() if cap in _CONTENT_WORK)


def test_policy_reads_are_admin_only_and_the_s1_reads_stay_open():
    """A GET is in POLICY only when its data is admin-only (GĐ3); the pages every staff role uses
    keep their reads (S1: llm-config, job status; Master Content: the aa_internal catalog)."""
    gets = {k: cap for k, cap in POLICY.items() if k[0] == "GET"}
    assert gets and all(cap not in _CONTENT_WORK for cap in gets.values())
    for open_read in ["/admin/llm-config", "/admin/job-runner/jobs/{job_id}", "/admin/review-queue",
                      "/admin/tenants/{tenant_id}/details", "/admin/notifications", "/admin/search"]:
        assert ("GET", open_read) not in POLICY, open_read


def test_staff_list_is_staff_read():
    """AA-883 (GĐ2): GET /admin/platform/staff needs staff.read (admin), never an open read."""
    assert POLICY[("GET", "/admin/platform/staff")] == "staff.read"
    assert POLICY[("GET", "/admin/platform/staff/{staff_id}")] == "staff.read"
    assert [r for r in ROLE_PERMS if "staff.read" in ROLE_PERMS[r]] == ["admin"]


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


@pytest.mark.parametrize("key", _ADMIN_ONLY_KEYS)
def test_reviewer_and_content_are_refused_every_admin_route(key):
    for role in ("reviewer", "content"):
        assert denial_reason(principal_from_role(role), *key) is not None


@pytest.mark.parametrize("key", _CONTENT_WORK_KEYS)
def test_every_staff_role_may_do_content_work(key):
    """Upload, S1, Review Queue, tour edits, the notification bell: unchanged for reviewer/content."""
    for role in _STAFF_ROLES:
        assert denial_reason(principal_from_role(role), *key) is None, (role, key)


def test_content_work_routes_are_the_every_staff_pages():
    """Guard against a typo that silently makes an admin route a content route (or the reverse)."""
    assert {p for _, p in _CONTENT_WORK_KEYS} == {
        "/admin/upload-url", "/admin/ingest-s3", "/admin/upload-staging/{staging_id}/decide",
        "/admin/raw-tours/{tour_id}/set-active", "/admin/raw-tours/{tour_id}",
        "/admin/run-tour", "/admin/run-tour-async", "/admin/s1-batch/submit", "/admin/s1/seo-prefetch",
        "/admin/review-queue/{review_id}/approve", "/admin/review-queue/{review_id}/reject",
        "/admin/review-queue/{review_id}/dismiss", "/admin/review-queue/{review_id}/supersede",
        "/admin/tours/{tour_id}/country", "/admin/tours/{tour_id}/raw",
        "/admin/tours/{tour_id}/generated/{content_id}",
        "/admin/tours/{tour_id}/generated/{content_id}/revalidate",
        "/admin/tours/{tour_id}/versions/{version_num}/promote",
        "/admin/tours/{tour_id}/trash", "/admin/tours/{tour_id}/restore",
        "/admin/notifications/read-all", "/admin/notifications/{notif_id}/read",
        "/v1/pipeline/run", "/v1/pipeline/review-queue/{review_id}/approve",
        "/v1/pipeline/review-queue/{review_id}/reject",
    }


@pytest.mark.parametrize("key", sorted(k for k in POLICY if POLICY[k] not in OWNER_PERMS))
def test_admin_may_call_every_gd1_write(key):
    assert denial_reason(principal_from_role("admin"), *key) is None


_OWNER_KEYS = sorted(k for k in POLICY if POLICY[k] in OWNER_PERMS)


def _cognito_identity(is_owner):
    from types import SimpleNamespace

    return SimpleNamespace(auth="cognito", is_owner=is_owner, admin_id="11111111-1111-1111-1111-111111111111")


def test_owner_routes_are_the_staff_directory_writes():
    assert _OWNER_KEYS and all(p.startswith("/admin/platform/staff") and m != "GET" for m, p in _OWNER_KEYS)


@pytest.mark.parametrize("key", _OWNER_KEYS)
def test_staff_write_needs_an_owner_on_a_cognito_session(key):
    """AA-816 C6: a legacy session (x-admin-role only) is refused even as admin; a Cognito admin who is not an
    owner is refused; a Cognito owner may write as admin only (the directory re-checks the row in its transaction)."""
    assert denial_reason(principal_from_role("admin"), *key) == staff_authz.STAFF_WRITE_NEEDS_COGNITO
    assert denial_reason(principal_from_role("admin", _cognito_identity(False)), *key) == \
        staff_authz.STAFF_WRITE_NEEDS_OWNER
    assert denial_reason(principal_from_role("admin", _cognito_identity(True)), *key) is None
    assert denial_reason(MACHINE, *key) is not None
    assert denial_reason(principal_from_role("tenant", _cognito_identity(True)), *key) == "Unknown staff role"
    for role in ("reviewer", "content"):
        assert denial_reason(principal_from_role(role, _cognito_identity(True)), *key) == \
            f"Role '{role}' lacks the 'staff.write' permission"


def test_owner_flag_never_comes_from_a_non_cognito_identity():
    from types import SimpleNamespace

    legacy = SimpleNamespace(auth="legacy", is_owner=True)
    p = principal_from_role("admin", legacy)
    assert p.auth is None and p.is_owner is False


@pytest.mark.parametrize("key", sorted(POLICY))
def test_machine_only_on_machine_routes(key):
    reason = denial_reason(MACHINE, *key)
    assert (reason is None) is (key in MACHINE_ROUTES), (key, reason)


@pytest.mark.parametrize("key", sorted(POLICY))
def test_unknown_role_is_refused(key):
    assert denial_reason(principal_from_role("tenant"), *key) == "Unknown staff role"


def test_routes_without_policy_are_open_reads():
    for principal in (MACHINE, principal_from_role("content"), principal_from_role("x")):
        assert denial_reason(principal, "GET", "/admin/llm-config") is None
    # A content write: every staff role, never the machine or an unknown role.
    assert denial_reason(principal_from_role("content"), "POST", "/admin/run-tour-async") is None
    assert denial_reason(MACHINE, "POST", "/admin/run-tour-async") is not None
    assert denial_reason(principal_from_role("x"), "POST", "/admin/run-tour-async") == "Unknown staff role"


# ── The real app ─────────────────────────────────────────────────────────────────────────────────

def _concrete(path: str) -> str:
    """A request path for a route template: every {param} gets a harmless sample value."""
    samples = {"job_id": "00000000-0000-0000-0000-000000000000", "topup_id": "1",
               "provider": "bedrock", "scope": "global", "tenant_id": "00000000-0000-0000-0000-000000000009",
               "rule_id": "00000000-0000-0000-0000-000000000008", "version_num": "1"}
    return re.sub(r"\{(\w+)\}", lambda m: samples.get(m.group(1), "aa882"), path)


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    # Same setup as test_s241_admin_routes_require_auth: the routers' secret constants only (the
    # rate-limit middleware then passes admin calls through untracked), and a pool that fails the
    # test if a refused request ever reaches the database.
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)  # read per call by the /v1 staff guards
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


_LIVE_POLICY = sorted(k for k in POLICY if k not in AWAITING_ROUTE)


@pytest.mark.parametrize("key", [k for k in _ADMIN_ONLY_KEYS if k not in AWAITING_ROUTE])
@pytest.mark.parametrize("role", ["reviewer", "content"])
def test_app_refuses_reviewer_and_content_on_admin_routes_before_the_endpoint(client, role, key):
    method, path = key
    resp = client.request(method, _concrete(path), json={}, headers=_headers(role))
    assert resp.status_code == 403, (role, key, resp.status_code, resp.text)
    assert resp.json()["detail"] == f"Role '{role}' lacks the '{POLICY[key]}' permission"


@pytest.mark.parametrize("key", _LIVE_POLICY)
@pytest.mark.parametrize("role", ["tenant", ""])
def test_app_refuses_an_unknown_role_before_the_endpoint(client, role, key):
    method, path = key
    resp = client.request(method, _concrete(path), json={}, headers=_headers(role))
    assert resp.status_code == 403, (role, key, resp.status_code, resp.text)


@pytest.mark.parametrize("key", [k for k in _LIVE_POLICY if k not in MACHINE_ROUTES])
def test_app_refuses_the_machine_off_its_allowlist(client, key):
    method, path = key
    resp = client.request(method, _concrete(path), json={}, headers=_headers(None))
    assert resp.status_code == 403, (key, resp.text)
    assert "machine" in resp.json()["detail"]


@pytest.mark.parametrize("key", _LIVE_POLICY)
def test_app_still_checks_the_secret_first(client, key):
    """Without the secret no role header helps: 403 "Invalid admin secret" on the staff_policy routers.
    On the staff_policy_if_staff ones the router's own guard answers, as before GĐ3: 401/403, or 422
    where an inline secret check sits after the body validation (admin_pipeline) — never the endpoint
    (the client fixture's pool fails the test if the DB is reached)."""
    method, path = key
    anon = client.request(method, _concrete(path), json={}, headers={"x-admin-role": "admin"})
    wrong = client.request(method, _concrete(path), json={},
                           headers={"X-Admin-Secret": _SECRET + "x", "x-admin-role": "admin"})
    assert anon.status_code in (401, 403, 422), (key, anon.status_code, anon.text)
    assert wrong.status_code in (403, 422), (key, wrong.status_code, wrong.text)
    if _runs_full_staff_policy(key):
        assert anon.status_code == wrong.status_code == 403
        assert anon.json()["detail"] == wrong.json()["detail"] == "Invalid admin secret"


def _runs_full_staff_policy(key) -> bool:
    from tests.unit.test_aa882_route_policy import _staff_routes
    from tests.unit.test_s241_admin_routes_require_auth import _dependency_calls

    for method, path, route, _ in _staff_routes():
        if (method, path) == key:
            return staff_authz.staff_policy in set(_dependency_calls(route.dependant))
    raise AssertionError(key)


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


def test_the_machine_policy_routes_are_the_scheduler_posts_and_the_overview():
    """The POLICY routes a role-less call can still reach: why every staff BFF must forward the role."""
    assert _MACHINE_POLICY_ROUTES == sorted([
        ("POST", "/admin/dfs-balance/check"), ("POST", "/admin/jev-canary/check"),
        ("POST", "/admin/jev/402-sweep"), ("POST", "/admin/job-runner/retention/schedule"),
        ("POST", "/admin/job-runner/staff-reconcile/schedule"),
        ("POST", "/admin/waves/check"), ("GET", "/admin/overview"),
    ])


def test_machine_routes_cover_every_role_less_caller_and_nothing_else():
    """Minimal allowlist: the CI deploy-dev.yml GETs and the ops-probe Lambda POSTs only."""
    assert set(MACHINE_ROUTES) == {
        ("GET", "/admin/job-runner/deploy-gate"), ("GET", "/admin/job-runner/summary"),
        ("GET", "/admin/job-runner/workers"), ("GET", "/admin/overview"),
        ("POST", "/admin/dfs-balance/check"), ("POST", "/admin/jev-canary/check"),
        ("POST", "/admin/jev/402-sweep"), ("POST", "/admin/waves/check"),
        ("POST", "/admin/job-runner/retention/schedule"),
        ("POST", "/admin/job-runner/staff-reconcile/schedule"),
    }


@pytest.mark.parametrize("key", _MACHINE_POLICY_ROUTES)
@pytest.mark.parametrize("role", ["reviewer", "content"])
def test_app_tenant_proxy_staff_branch_refuses_reviewer_and_content(client, role, key):
    """POST /api/tenant/admin/jev-canary/check (and the other machine-allowed POLICY POSTs) by a
    reviewer/content session: 403 before the endpoint (no paid Jev probe, no breaker reset)."""
    method, path = key
    resp = client.request(method, _concrete(path), json={}, headers=_tenant_proxy_headers(role))
    assert resp.status_code == 403, (role, key, resp.text)
    assert resp.json()["detail"] == f"Role '{role}' lacks the '{POLICY[key]}' permission"


def test_app_tenant_proxy_staff_branch_admin_reaches_retention_schedule(client, monkeypatch):
    from services.jobs import retention_job

    monkeypatch.setattr(retention_job, "schedule_all", AsyncMock(return_value={"enqueued": [], "skipped": []}))
    resp = client.post("/admin/job-runner/retention/schedule", headers=_tenant_proxy_headers("admin"))
    assert resp.status_code == 200, resp.text


_SECRET_BFFS = ["frontend/app/api/tenant/[...path]/route.ts", "frontend/app/api/admin/[...path]/route.ts",
                "frontend/app/api/pipeline/[...path]/route.ts", "frontend/app/api/tour-full/[id]/route.ts",
                "frontend/app/api/tenant/pipeline/brand-identity/route.ts"]


@pytest.mark.parametrize("bff", _SECRET_BFFS)
def test_staff_bffs_forward_the_verified_role(bff):
    """Every staff proxy that carries the admin secret must forward the role from requireAdmin(): a
    missing x-admin-role makes the backend see the machine principal (every write refused)."""
    src = (REPO / bff).read_text()
    assert re.search(r'(headers\["x-admin-role"\]\s*=|"x-admin-role":)\s*auth\.role\b', src), bff


def test_every_bff_that_sends_the_admin_secret_is_checked():
    """A new staff proxy (anything under frontend/app/api that reads ADMIN_SECRET) joins the list."""
    found = {str(p.relative_to(REPO)) for p in (REPO / "frontend" / "app" / "api").rglob("route.ts")
             if "process.env.ADMIN_SECRET" in p.read_text()}
    assert found == set(_SECRET_BFFS)


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


def test_staff_policy_fails_closed_without_a_matched_route(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request

    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    request = Request({"type": "http", "method": "PATCH", "path": "/admin/llm-config/x",
                       "headers": [], "query_string": b""})
    with pytest.raises(HTTPException) as exc:
        staff_authz.staff_policy(request, _SECRET, "admin")
    assert exc.value.status_code == 403
    assert exc.value.detail == "Route policy unavailable"
    with pytest.raises(HTTPException) as exc:
        staff_authz.staff_policy_if_staff(request, _SECRET, "admin")
    assert exc.value.status_code == 403


def test_staff_policy_if_staff_is_a_no_op_without_the_valid_secret(monkeypatch):
    """The router's own guard answers a request without the valid secret (tenant JWT, 401 rules)."""
    from starlette.requests import Request

    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    request = Request({"type": "http", "method": "POST", "path": "/admin/brand-identity",
                       "headers": [], "query_string": b""})
    for secret in (None, "", _SECRET + "x"):
        assert staff_authz.staff_policy_if_staff(request, secret, "reviewer") is None


def test_valid_secret_is_the_admin_module_or_the_environment(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_SECRET", "")
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    assert staff_authz._is_valid_secret(_SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    assert staff_authz._is_valid_secret(_SECRET)
    assert not staff_authz._is_valid_secret(_SECRET + "x")
    monkeypatch.setattr(admin, "ADMIN_SECRET", "")
    assert not staff_authz._is_valid_secret("")  # no configured secret never matches an empty header


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
    assert not [k for k in POLICY if k not in keys and k not in AWAITING_ROUTE], \
        "POLICY names routes that do not exist"
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


def test_frontend_capability_mirror_matches_role_perms():
    """AA-882 review: frontend/lib/rbac.ts ROLE_PERMS (UI hiding of admin-only controls) must name the
    same capabilities per role as the backend, and its Capability union must be the backend's
    role capabilities (staff.write is owner-only, never a role's)."""
    rbac = (REPO / "frontend" / "lib" / "rbac.ts").read_text()
    block = re.search(r"export const ROLE_PERMS[^=]*=\s*\{(.*?)\n\};", rbac, re.S)
    assert block, "ROLE_PERMS not found in frontend/lib/rbac.ts"
    fe = {role: set(re.findall(r'"([a-z0-9_.]+)"', caps))
          for role, caps in re.findall(r"^\s+(\w+):\s*\[(.*?)\]", block.group(1), re.S | re.M)}
    assert fe == {role: set(perms) for role, perms in ROLE_PERMS.items()}, fe
    union = set(re.findall(r'"([a-z0-9_.]+)"', re.search(
        r"export type Capability\s*=([^;]+);", rbac, re.S).group(1)))
    assert union == set().union(*ROLE_PERMS.values())
