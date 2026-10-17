"""S241 (GĐ −1) — every /admin route must authenticate the caller.

Until S241, 27 routes under /admin answered without any check on the public API domain: 25 GETs
in admin_job_runner / admin_llm_ops / admin_budgets / admin_photos / admin_segment_research, plus
POST /admin/segment-research/preview (dry run) and POST /admin/upload-staging/{id}/decide (a write
that declared the X-Admin-Secret header but never verified it). The admin BFF always sent the
secret, so the pages worked and nobody noticed: those routers relied on the BFF alone.

This test makes that mistake a red CI run instead of a live hole. It imports the real app and, for
every APIRoute whose path starts with /admin, requires ONE of:

1. an auth dependency in the route's dependant tree (router-level or route-level
   `dependencies=[Depends(...)]`, or a `Depends(...)` parameter), or
2. an unconditional inline call to an auth function at the TOP LEVEL of the endpoint body, with no
   `await` and no `return` before it — so nothing touches the DB before the check runs. A check
   nested in an `if`/`try` does not count.

ALLOWED_PUBLIC lists the /admin routes that may stay without auth. It is empty: every /admin route
is staff-only. Add an entry only with a written reason.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.routing import APIRoute

from api.main import app
from api.routers import admin, admin_atoms, admin_pipeline, admin_settings

# Dependency callables that authenticate a caller.
AUTH_DEPENDENCIES = {
    admin.verify_admin_secret,                 # X-Admin-Secret (constant-time compare)
    admin_settings.verify_admin,               # same check, local copy in admin_settings
    admin_atoms._require_atoms_staff,          # X-Admin-Secret; logs + refuses a tenant Bearer
    admin_pipeline._resolve_brand_tenant_id,   # tenant JWT (own brand) or X-Admin-Secret (AA-424)
}
# Function names accepted as an inline check at the top of an endpoint body.
INLINE_AUTH_CALLS = {"verify_admin_secret", "verify_admin"}

# (METHOD, path) -> reason. Keep empty unless a route truly must be public.
ALLOWED_PUBLIC: dict[tuple[str, str], str] = {}


def _admin_routes():
    return [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/admin")]


def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        if dep.call is not None:
            yield dep.call
        yield from _dependency_calls(dep)


def _has_auth_dependency(route: APIRoute) -> bool:
    return any(call in AUTH_DEPENDENCIES for call in _dependency_calls(route.dependant))


def _is_auth_call(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id in INLINE_AUTH_CALLS
    )


def _has_inline_auth(route: APIRoute) -> bool:
    """An auth call among the endpoint's top-level statements, before any await or return."""
    source = textwrap.dedent(inspect.getsource(route.endpoint))
    func = ast.parse(source).body[0]
    assert isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)), route.path
    for stmt in func.body:
        if _is_auth_call(stmt):
            return True
        if isinstance(stmt, ast.Return) or any(isinstance(n, ast.Await) for n in ast.walk(stmt)):
            return False
    return False


def test_admin_routes_exist():
    """Guard against the scan silently checking nothing (e.g. a renamed prefix)."""
    assert len(_admin_routes()) > 150


def test_every_admin_route_requires_auth():
    open_routes = []
    for route in _admin_routes():
        for method in sorted(route.methods):
            if (method, route.path) in ALLOWED_PUBLIC:
                continue
            if _has_auth_dependency(route) or _has_inline_auth(route):
                continue
            open_routes.append(f"{method} {route.path} ({route.endpoint.__module__}.{route.endpoint.__name__})")
    assert not open_routes, (
        "These /admin routes have no auth check (S241). Add the router's auth dependency — "
        "prefer dependencies=[Depends(verify_admin_secret)] on the APIRouter:\n" + "\n".join(open_routes)
    )


def test_allowlist_entries_are_real_routes():
    known = {(m, r.path) for r in _admin_routes() for m in r.methods}
    stale = [k for k in ALLOWED_PUBLIC if k not in known]
    assert not stale, f"ALLOWED_PUBLIC names routes that no longer exist: {stale}"


def test_inline_check_after_an_await_does_not_count():
    """The inline rule itself: a check placed after DB access is not accepted."""
    async def late_check(request, x_admin_secret=None):
        await request.app.state.pool.fetch("SELECT 1")
        admin.verify_admin_secret(x_admin_secret)

    async def nested_check(request, x_admin_secret=None):
        if request:
            verify_admin_secret(x_admin_secret)  # noqa: F821 — parsed only, never called

    async def first_check(request, x_admin_secret=None):
        verify_admin_secret(x_admin_secret)  # noqa: F821 — parsed only, never called
        return await request.app.state.pool.fetch("SELECT 1")

    def fake(endpoint):
        return MagicMock(endpoint=endpoint, path="/admin/x")

    assert not _has_inline_auth(fake(late_check))
    assert not _has_inline_auth(fake(nested_check))
    assert _has_inline_auth(fake(first_check))


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
