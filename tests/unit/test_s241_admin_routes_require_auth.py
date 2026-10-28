"""S241 (GĐ −1) — every route of the API must authenticate the caller, unless allowlisted as public.

Until S241, 27 routes under /admin answered without any check on the public API domain (25 GETs in
admin_job_runner / admin_llm_ops / admin_budgets / admin_photos / admin_segment_research, plus POST
/admin/segment-research/preview and POST /admin/upload-staging/{id}/decide). The admin BFF always
sent the secret, so the pages worked and nobody noticed. The S241 review then found /v1/rules and
/v1/social accepting ANY JWT (a tenant token included) as "admin".

This test makes those mistakes a red CI run instead of a live hole. It imports the real app and
walks EVERY entry of app.routes (not only /admin):

- APIRoute / APIWebSocketRoute: needs an auth dependency in its dependant tree (router-level or
  route-level `dependencies=[Depends(...)]`, or a `Depends(...)` parameter) — or, for HTTP routes, a
  strict inline staff check (see `_has_inline_staff_check`).
- Staff-only paths (STAFF_ONLY_PREFIXES: /admin, /v1/pipeline, /v1/rules, /v1/social) need a STAFF
  check. A tenant-or-staff dependency (a tenant JWT satisfies it) is accepted there only for the
  routes in ADMIN_TENANT_OR_STAFF.
- Included routers: FastAPI 0.110 copies an included router's routes into the parent, FastAPI >= 0.143
  keeps one `_IncludedRouter` entry (original router + include context). The scan expands that entry,
  carrying the include prefix and the `include_router(dependencies=...)` list into each route, so both
  versions are checked against the same effective path and dependencies (see `_walk`).
- Mount: recursed into when the mounted app is a router (include-level dependencies do NOT reach a
  mounted app, so none are carried); any other mounted ASGI app, and any plain Starlette Route /
  WebSocketRoute, fails unless listed in NON_API_ALLOWED with a reason.
- Anything else must be in ALLOWED_PUBLIC (or INLINE_REVIEWED) with a written reason.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, WebSocket
from fastapi import params as fastapi_params
from fastapi.dependencies.utils import get_parameterless_sub_dependant
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount

from api import main
from api.core import staff_authz, staff_secret
from api.main import app
from api.routers import (
    admin, admin_atoms, admin_pipeline, admin_settings, catalog_http, tenant_membership, v1_pipeline, v1_tours,
)

# Dependencies that admit staff only (the X-Admin-Secret held by the staff BFF).
STAFF_DEPENDENCIES = {
    admin.verify_admin_secret,                 # X-Admin-Secret (constant-time compare)
    admin_settings.verify_admin,               # same check, local copy in admin_settings
    admin_atoms._require_atoms_staff,          # X-Admin-Secret; logs + refuses a tenant Bearer
    v1_pipeline._require_admin,                # X-Admin-Secret; any JWT -> 403 (AA-799)
    staff_secret.require_staff_secret,         # same rule, shared by /v1/rules + /v1/social (S241)
    staff_authz.staff_policy,                  # AA-882: admin.verify_admin_secret, then the route POLICY
    # NOT staff_authz.staff_policy_if_staff: a no-op without the secret, so it is no auth check alone.
    main.require_admin_secret_for_docs,        # X-Admin-Secret on /docs, /redoc, /openapi.json (AA-577)
}
# Dependencies that a tenant JWT also satisfies.
TENANT_OR_STAFF_DEPENDENCIES = {
    admin_pipeline._resolve_brand_tenant_id,   # tenant JWT (own brand) or X-Admin-Secret (AA-424)
    v1_tours.get_tenant,                       # tenant portal routes
}
# Dependencies that admit a service caller only: an M2M client-credentials token of the aa-m2m pool, one pinned
# client and scope (AA-976 / AA-978, root ADR 0006b decision 7). Never accepted on a staff-only path.
M2M_DEPENDENCIES = {
    catalog_http.require_catalog_reader,       # /catalog/v1: CRM client + cis.catalog/read
    tenant_membership.require_tenant_reader,   # /tenant/membership/v1: CRM client + cis.tenant/read (AA-978)
    tenant_membership.require_tenant_activator,  # POST .../invitations/{id}/activate: + cis.tenant/activate (AA-979)
}
# Functions accepted as an inline staff check at the top of an endpoint body (identity, not name).
STAFF_INLINE_CHECKS = {admin.verify_admin_secret, admin_settings.verify_admin}

STAFF_ONLY_PREFIXES = ("/admin", "/v1/pipeline", "/v1/rules", "/v1/social")

# Staff-only paths where a tenant JWT is accepted on purpose. (METHOD, path) -> reason.
ADMIN_TENANT_OR_STAFF: dict[tuple[str, str], str] = {
    ("GET", "/admin/brand-identity"): "AA-424: tenant portal edits its own brand; JWT scoped to own tenant",
    ("POST", "/admin/brand-identity"): "AA-424: same",
    ("POST", "/admin/brand-identity/upload"): "AA-424: same",
    ("POST", "/admin/brand-identity/parse"): "AA-424: same",
}

# Routes that are public by design. (METHOD, path) -> reason.
ALLOWED_PUBLIC: dict[tuple[str, str], str] = {
    ("GET", "/health"): "ALB / deploy health check",
    ("GET", "/health/ready"): "AA-884 readiness probe (DB + schema version; 5 s in-process cache, no data)",
    ("POST", "/auth/tenant-login"): "login: trades a tenant API key for a JWT",
    ("POST", "/auth/admin-login"): "login: staff username + password",
    ("POST", "/auth/verify-tenant"): "verifies the JWT in the request body (portal middleware)",
    ("POST", "/auth/verify-admin"):
        "verifies the staff token in the request body (admin middleware): a WORKFORCE access token (C2 verifier, "
        "cis-* group, active directory row; AA-816 C3) or a legacy HS256 JWT (LEGACY_ADMIN_LOGIN_ENABLED)",
    ("POST", "/auth/booking-token"): "AA-Booking token exchange; authenticates by body credentials",
    ("POST", "/auth/tenant-session"):
        "AA-979: Content studio sign-in; authenticates by the partner-pool Bearer (own verifier, partner-web only; "
        "staff / EXTERNAL / M2M tokens 403), checked inline before any DB read",
    ("GET", "/auth/tenant-memberships"): "AA-979: same partner-pool Bearer check as /auth/tenant-session",
    ("GET", "/auth/.well-known/jwks.json"): "public signing keys",
    ("GET", "/v1/trip/{tour_id}"): "AA-482: public trip page data",
    ("GET", "/content/photos/{photo_id}"): "AA-708: public image redirect (TripPlanner, published pages)",
    ("GET", "/v1/angle-gate/goals"): "AA-449: static list of content goals",
}

# Routes whose auth is an inline check the scan cannot prove (it branches). (METHOD, path) -> reason.
INLINE_REVIEWED: dict[tuple[str, str], str] = {
    ("GET", "/v1/tours/{tour_id}/full"):
        "admin secret or verify_tenant_jwt, inline, before any DB access; 401 otherwise (reviewed S241)",
}

# Non-APIRoute entries (mounts, plain Starlette routes). path -> reason. Empty today.
NON_API_ALLOWED: dict[str, str] = {}


# ── Checker ──────────────────────────────────────────────────────────────────────────────────────

def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        if dep.call is not None:
            yield dep.call
        yield from _dependency_calls(dep)


def _is_staff_only(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in STAFF_ONLY_PREFIXES)


def _header_name(param: inspect.Parameter):
    """The HTTP header a parameter reads, or None when it is not a Header(...) parameter."""
    marker = param.default
    if not isinstance(marker, fastapi_params.Header):
        meta = getattr(param.annotation, "__metadata__", ())
        marker = next((m for m in meta if isinstance(m, fastapi_params.Header)), None)
        if marker is None:
            return None
    if marker.alias:
        return marker.alias.lower()
    name = param.name.replace("_", "-") if marker.convert_underscores else param.name
    return name.lower()


def _is_staff_inline_call(stmt: ast.stmt, endpoint, params, assigned: set) -> bool:
    """`<staff check>(<x-admin-secret header param>)`, resolved through the endpoint's globals."""
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    call = stmt.value
    if not isinstance(call.func, ast.Name):
        return False
    if endpoint.__globals__.get(call.func.id) not in STAFF_INLINE_CHECKS:
        return False
    if call.keywords or len(call.args) != 1 or not isinstance(call.args[0], ast.Name):
        return False
    arg = call.args[0].id
    return arg in params and arg not in assigned and _header_name(params[arg]) == "x-admin-secret"


def _has_inline_staff_check(endpoint) -> bool:
    """A staff check among the endpoint's top-level statements, before any await or return.

    The name must resolve (endpoint globals) to admin.verify_admin_secret or
    admin_settings.verify_admin, and the argument must be the endpoint's own X-Admin-Secret header
    parameter, not reassigned before the call. A check nested in an `if`/`try` does not count.
    """
    endpoint = inspect.unwrap(endpoint)
    params = inspect.signature(endpoint).parameters
    func = ast.parse(textwrap.dedent(inspect.getsource(endpoint))).body[0]
    assert isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)), endpoint
    assigned: set = set()
    for stmt in func.body:
        if _is_staff_inline_call(stmt, endpoint, params, assigned):
            return True
        if isinstance(stmt, ast.Return) or any(isinstance(n, ast.Await) for n in ast.walk(stmt)):
            return False
        assigned |= {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    return False


def _inherited_calls(inherited, path):
    """Callables of the include-level Depends(...) that wrap a route, sub-dependencies included."""
    for depends in inherited:
        sub = get_parameterless_sub_dependant(depends=depends, path=path)
        yield sub.call
        yield from _dependency_calls(sub)


def _check_endpoint(method, path, route, inherited, problems):
    key = (method, path)
    where = f"{method} {path} ({route.endpoint.__module__}.{route.endpoint.__name__})"
    if key in ALLOWED_PUBLIC or key in INLINE_REVIEWED:
        return
    calls = set(_dependency_calls(route.dependant)) | set(_inherited_calls(inherited, path))
    if calls & STAFF_DEPENDENCIES:
        return
    if isinstance(route, APIRoute) and _has_inline_staff_check(route.endpoint):
        return
    if calls & TENANT_OR_STAFF_DEPENDENCIES:
        if not _is_staff_only(path) or key in ADMIN_TENANT_OR_STAFF:
            return
        problems.append(f"{where}: staff-only path accepts a tenant JWT")
        return
    if calls & M2M_DEPENDENCIES:
        if not _is_staff_only(path):
            return
        problems.append(f"{where}: staff-only path accepts an M2M service token")
        return
    problems.append(f"{where}: no auth check")


def _is_included_router(route) -> bool:
    """FastAPI >= 0.143 `_IncludedRouter`: one entry per include_router() call (duck-typed, private)."""
    return hasattr(route, "original_router") and hasattr(route, "include_context")


def _served_routes(app_or_router) -> list:
    """The routes an app or router serves, low-priority ones (FastAPI >= 0.143 frontend routes) last."""
    router = getattr(app_or_router, "router", app_or_router)
    return [*router.routes, *getattr(router, "_low_priority_routes", [])]


def _walk(routes, prefix: str = "", inherited: tuple = ()):
    """Yield (full path, route, inherited Depends) for every entry reachable through `routes`.

    `inherited` holds the include_router(dependencies=...) of every enclosing _IncludedRouter. A
    route's own path already carries its APIRouter(prefix=...) and its dependant already carries the
    APIRouter(dependencies=...) on both FastAPI versions; an include context's prefix and dependencies
    already hold the parent router's (FastAPI's `_RouterIncludeContext.for_include`), so nesting
    only concatenates — exactly what `_RouterIncludeContext.combine` does at runtime.
    """
    for route in routes:
        if _is_included_router(route):
            ctx = route.include_context
            yield from _walk(_served_routes(route.original_router), prefix + ctx.prefix,
                             (*inherited, *ctx.dependencies))
            continue
        path = prefix + getattr(route, "path", "")
        if (isinstance(route, Mount) and path not in NON_API_ALLOWED
                and getattr(route.app, "routes", None) is not None):
            yield from _walk(_served_routes(route.app), path, ())
            continue
        yield path, route, inherited


def find_problems(routes) -> list[str]:
    """Every route reachable through `routes` that has no acceptable auth check."""
    problems: list[str] = []
    for path, route, inherited in _walk(routes):
        if isinstance(route, APIRoute):
            for method in sorted(route.methods):
                _check_endpoint(method, path, route, inherited, problems)
        elif isinstance(route, APIWebSocketRoute):
            _check_endpoint("WS", path, route, inherited, problems)
        elif path in NON_API_ALLOWED:
            continue
        else:
            problems.append(f"{type(route).__name__} {path}: not an APIRoute, not scanned — add auth or "
                            "a NON_API_ALLOWED entry with a reason")
    return problems


def _all_keys(routes):
    for path, route, _ in _walk(routes):
        if isinstance(route, APIRoute):
            yield from ((m, path) for m in route.methods)


# ── The real app ─────────────────────────────────────────────────────────────────────────────────

def test_scan_sees_the_whole_app():
    """Guard against the scan silently checking nothing (e.g. a renamed prefix)."""
    keys = set(_all_keys(_served_routes(app)))
    assert sum(1 for _, p in keys if p.startswith("/admin")) > 150
    assert sum(1 for _, p in keys if p.startswith("/v1/")) > 50


def test_every_route_requires_auth():
    problems = find_problems(_served_routes(app))
    assert not problems, (
        "These routes have no acceptable auth check (S241). Add the router's auth dependency — "
        "prefer dependencies=[Depends(verify_admin_secret)] on the APIRouter — or, for a route that "
        "must be public, an ALLOWED_PUBLIC entry with a reason:\n" + "\n".join(problems)
    )


def test_allowlist_entries_are_real_routes():
    known = set(_all_keys(_served_routes(app)))
    lists = {"ALLOWED_PUBLIC": ALLOWED_PUBLIC, "INLINE_REVIEWED": INLINE_REVIEWED,
             "ADMIN_TENANT_OR_STAFF": ADMIN_TENANT_OR_STAFF}
    stale = [(name, k) for name, entries in lists.items() for k in entries if k not in known]
    assert not stale, f"Allowlists name routes that no longer exist: {stale}"
    paths = {path for path, _, _ in _walk(_served_routes(app))}
    assert not [p for p in NON_API_ALLOWED if p not in paths]


# ── Self-tests of the checker ────────────────────────────────────────────────────────────────────

verify_admin_secret = admin.verify_admin_secret  # resolved by the inline rule through these globals
verify_admin = main.verify_admin                 # the POST /auth/verify-admin ENDPOINT, not a check


async def _late_check(request, x_admin_secret: str = Header(None)):
    await request.app.state.pool.fetch("SELECT 1")
    verify_admin_secret(x_admin_secret)


async def _nested_check(request, x_admin_secret: str = Header(None)):
    if request:
        verify_admin_secret(x_admin_secret)


async def _first_check(request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    return await request.app.state.pool.fetch("SELECT 1")


async def _wrong_function(request, x_admin_secret: str = Header(None)):
    verify_admin(request)  # same name as admin_settings.verify_admin, but the /auth endpoint
    return await request.app.state.pool.fetch("SELECT 1")


async def _query_param_secret(request, secret: str = None):
    verify_admin_secret(secret)  # a query parameter, not the X-Admin-Secret header
    return await request.app.state.pool.fetch("SELECT 1")


async def _reassigned_secret(request, x_admin_secret: str = Header(None)):
    x_admin_secret = admin.ADMIN_SECRET
    verify_admin_secret(x_admin_secret)
    return await request.app.state.pool.fetch("SELECT 1")


def test_inline_rule_accepts_only_a_real_staff_check_first():
    assert _has_inline_staff_check(_first_check)
    assert not _has_inline_staff_check(_late_check)
    assert not _has_inline_staff_check(_nested_check)
    assert not _has_inline_staff_check(_wrong_function)
    assert not _has_inline_staff_check(_query_param_secret)
    assert not _has_inline_staff_check(_reassigned_secret)


def test_checker_reports_mounts_websockets_and_tenant_auth_on_staff_paths():
    throwaway = FastAPI()
    sub = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @sub.get("/open")
    async def sub_open():
        return {}

    async def raw_asgi(scope, receive, send):  # e.g. a metrics exporter or StaticFiles
        pass

    @throwaway.websocket("/admin/ws/jobs")
    async def ws_jobs(websocket: WebSocket):
        await websocket.accept()

    @throwaway.get("/admin/tenant-ok-by-mistake")
    async def tenant_on_staff_path(tenant=Depends(v1_tours.get_tenant)):
        return tenant

    @throwaway.get("/internal/queue-dump")
    async def queue_dump():
        return []

    throwaway.mount("/admin/tools", sub)
    throwaway.mount("/admin/static", raw_asgi)

    report = "\n".join(find_problems(_served_routes(throwaway)))
    assert "WS /admin/ws/jobs" in report
    assert "GET /admin/tools/open" in report
    assert "Mount /admin/static" in report
    assert "GET /admin/tenant-ok-by-mistake" in report and "tenant JWT" in report
    assert "GET /internal/queue-dump" in report
    # FastAPI's own /docs etc. are plain Starlette routes: reported too.
    assert "Route /docs" in report


def test_checker_carries_include_prefix_and_dependencies(monkeypatch):
    """APIRouter(dependencies=...) and include_router(dependencies=...) count — also through a nested
    include — and the full prefixed path is what gets checked. Same verdict on FastAPI 0.110 (routes
    copied into the parent) and >= 0.143 (`_IncludedRouter`), and the verdict matches real requests.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    staff = [Depends(admin.verify_admin_secret)]
    throwaway = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    by_router = APIRouter(prefix="/admin/by-router", dependencies=staff)

    @by_router.get("/x")
    async def by_router_x():
        return {}

    by_include = APIRouter(prefix="/by-include")

    @by_include.get("/x")
    async def by_include_x():
        return {}

    leaf = APIRouter(prefix="/leaf")

    @leaf.get("/open")
    async def leaf_open():
        return {}

    outer = APIRouter(prefix="/outer")
    outer.include_router(leaf, prefix="/mid")
    tenant_only = APIRouter()

    @tenant_only.get("/y")
    async def tenant_y():
        return {}

    throwaway.include_router(by_router)
    throwaway.include_router(by_include, prefix="/admin", dependencies=staff)
    throwaway.include_router(outer, prefix="/admin/nest")
    throwaway.include_router(outer, prefix="/admin/nest-guarded", dependencies=staff)
    throwaway.include_router(tenant_only, prefix="/admin/t", dependencies=[Depends(v1_tours.get_tenant)])

    guarded = ["/admin/by-router/x", "/admin/by-include/x", "/admin/nest-guarded/outer/mid/leaf/open"]
    open_path = "/admin/nest/outer/mid/leaf/open"
    routes = _served_routes(throwaway)
    assert set(_all_keys(routes)) == {("GET", p) for p in [*guarded, open_path, "/admin/t/y"]}
    problems = find_problems(routes)
    assert sorted(problems) == sorted([
        f"GET {open_path} ({__name__}.leaf_open): no auth check",
        f"GET /admin/t/y ({__name__}.tenant_y): staff-only path accepts a tenant JWT",
    ])

    client = TestClient(throwaway)
    assert client.get(open_path).status_code == 200
    for path in guarded:
        assert client.get(path).status_code in (401, 403), path
        assert client.get(path, headers={"X-Admin-Secret": _SECRET}).status_code == 200, path


# ── Live checks through the real app: the S241 routes refuse an anonymous caller ─────────────

_SECRET = "s241-test-admin-secret"

# GETs confirmed open on the live API on 17/10/2026 (audit S241 §2), one or two per router.
PREVIOUSLY_OPEN = [
    "/admin/job-runner/summary",
    "/admin/job-runner/workers",
    "/admin/job-runner/jobs",
    "/admin/job-runner/retention",
    "/admin/llm-config",
    "/admin/llm-catalog",
    "/admin/llm-usage/calls",
    "/admin/budgets",
    "/admin/photos/summary",
    "/admin/cost-explorer",
    "/admin/dfs-balance",
    "/admin/segment-research/status",
    "/admin/unmapped-market-requests",
    "/admin/spend/daily",
]


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    # Only the router's module constant: with the ADMIN_SECRET env unset the rate-limit middleware
    # passes admin calls through untracked, so no DB write happens in the middleware.
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetchval = AsyncMock(return_value=0)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    # No lifespan (no `with`): nothing connects to Postgres or Redis.
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", PREVIOUSLY_OPEN)
def test_previously_open_get_refuses_without_secret(client, path):
    resp = client.get(path)
    assert resp.status_code in (401, 403), f"{path} -> {resp.status_code} without X-Admin-Secret"


@pytest.mark.parametrize("path", ["/admin/job-runner/summary", "/admin/budgets"])
def test_previously_open_get_refuses_a_wrong_secret(client, path):
    assert client.get(path, headers={"X-Admin-Secret": _SECRET + "x"}).status_code == 403


def test_segment_research_preview_refuses_without_secret(client):
    resp = client.post("/admin/segment-research/preview", json={"markets": ["Vietnam"]})
    assert resp.status_code == 403


def test_upload_staging_decide_refuses_without_secret(client):
    resp = client.post("/admin/upload-staging/00000000-0000-0000-0000-000000000000/decide",
                       json={"decision": "bypass"})
    assert resp.status_code == 403


def test_job_runner_summary_still_answers_with_secret(client):
    resp = client.get("/admin/job-runner/summary", headers={"X-Admin-Secret": _SECRET})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"] == [] and isinstance(body["kinds"], list)


def test_job_runner_workers_still_answers_with_secret(client, monkeypatch):
    from shared.jobs import queue

    async def fake_health(pool):
        return {"workers": [], "running": [], "queued": [], "alive_seconds": 90}

    monkeypatch.setattr(queue, "worker_health", fake_health)
    resp = client.get("/admin/job-runner/workers", headers={"X-Admin-Secret": _SECRET})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"workers": [], "running": [], "queued": [], "alive_seconds": 90}


def test_segment_research_status_still_answers_with_secret(client, monkeypatch):
    from api.routers import admin_segment_research

    async def fake_latest(pool, kind, statuses):
        return None

    monkeypatch.setattr(admin_segment_research.job_queue, "latest", fake_latest)
    resp = client.get("/admin/segment-research/status", headers={"X-Admin-Secret": _SECRET})
    assert resp.status_code == 200, resp.text
    assert resp.json()["running"] is False


def test_unconfigured_secret_is_still_503(client, monkeypatch):
    """Behaviour of verify_admin_secret is unchanged: no server secret -> 503, not open."""
    monkeypatch.setattr(admin, "ADMIN_SECRET", "")
    assert client.get("/admin/budgets").status_code == 503


# ── /v1/rules: staff only; a tenant JWT is refused (S241 review; /v1/social was removed by AA-850) ─────────────

_RULE_ID = "bbbbbbbb-0000-0000-0000-000000000001"
_V1_STAFF_ROUTES = [
    ("GET", "/v1/rules", None),
    ("PATCH", f"/v1/rules/{_RULE_ID}", {"active": False}),
]


@pytest.fixture
def v1_client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.routers import v1_rules

    # The router alone (no rate-limit middleware, which would look up the tenant in Redis/DB).
    # The DB pool raises if touched: the guard must refuse before any query runs.
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    v1_app = FastAPI()
    v1_app.include_router(v1_rules.router)
    v1_app.state.pool = MagicMock(acquire=MagicMock(side_effect=AssertionError("the DB must not be reached")))
    return TestClient(v1_app, raise_server_exceptions=True)


@pytest.mark.parametrize("method,path,body", _V1_STAFF_ROUTES)
def test_v1_staff_routes_refuse_a_tenant_jwt(v1_client, method, path, body):
    from api.routers.auth import _create_jwt

    token = _create_jwt("11111111-1111-1111-1111-111111111111", "Some Tenant", "starter")
    resp = v1_client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, f"{method} {path} -> {resp.status_code} with a tenant JWT"


@pytest.mark.parametrize("method,path,body", _V1_STAFF_ROUTES)
def test_v1_staff_routes_refuse_anonymous_and_a_wrong_secret(v1_client, method, path, body):
    assert v1_client.request(method, path, json=body).status_code == 401
    wrong = v1_client.request(method, path, json=body, headers={"X-Admin-Secret": _SECRET + "x"})
    assert wrong.status_code == 403


def test_v1_staff_routes_refuse_a_bad_jwt_with_401(v1_client):
    resp = v1_client.get("/v1/rules", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


@pytest.mark.parametrize("provided,expected,ok", [
    ("abc", "abc", True),
    ("abd", "abc", False),
    ("ab", "abc", False),
    (None, "abc", False),
    ("", "", False),
    ("abc", "", False),
    ("ăbc", "abc", False),  # non-ASCII header value: no TypeError from compare_digest
])
def test_secrets_equal(provided, expected, ok):
    from api.core.secret_compare import secrets_equal
    assert secrets_equal(provided, expected) is ok
