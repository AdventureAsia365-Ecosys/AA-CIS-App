"""AA-1036 R3 slice 3 (root ADR 0019 d6) — tenant API keys are gone.

- ``POST /auth/tenant-login`` answers 410 Gone with a short JSON body naming the partner sign-in, for ANY body (none,
  empty, a key-shaped value, not JSON), on the live app (api/main.py) and on the unmounted /auth router alike; it never
  reads the database and never writes an audit row.
- No route issues, rotates, shows or verifies a tenant key: no ``generate-key`` route, no ``/v1/account/api-key``,
  no ``TenantApiKey`` security scheme, the create / restore answers carry no ``api_key``.
- A source guard over the production code (api/, services/, shared/, worker/; migrations excluded): no code reads
  or writes ``shared.tenants.api_key_hash``, no key hashing / verifying helper exists, nothing reads an ``X-API-Key``
  header for a tenant, and the legacy tenant-token minter ``_create_jwt`` is called by tests only. Comments and
  docstrings may still name the retired pieces (history); code and string literals may not.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import main
from api.routers import admin, auth

REPO = Path(__file__).resolve().parents[2]
PROD_DIRS = ("api", "services", "shared", "worker")

GONE_BODY = {
    "detail": "tenant_api_key_removed",
    "message": ("Tenant API keys were removed. Sign in to the Content studio with your partner account "
                "(the agency web sign-in)."),
    "sign_in": "partner",
}

# identifiers of the retired key code; none may come back in production code
FORBIDDEN_NAMES = {
    "verify_tenant_api_key", "_hash_api_key", "generate_api_key", "GenerateKeyResponse", "api_key_facts",
    "AccountApiKey", "sync_test_tenant_key", "TenantLoginRequest", "TenantLoginResponse",
}
# substrings no production string literal may carry (a docstring may, as history)
FORBIDDEN_LITERALS = ("api_key_hash", "generate-key", "e2e-tenant", "X-API-Key", "/v1/account/api-key")


# ── POST /auth/tenant-login is 410 Gone ──────────────────────────────────────────────────────────────────────────────

BODIES = [
    {},
    {"json": {}},
    {"json": {"api_key": "x" * 40}},
    {"json": {"api_key": "x" * 40, "client": "portal"}},
    {"json": {"api_key": ""}},
    {"json": [1, 2]},
    {"content": b"not json", "headers": {"Content-Type": "application/json"}},
    {"content": b"api_key=abc", "headers": {"Content-Type": "application/x-www-form-urlencoded"}},
]


@pytest.fixture
def live_client(monkeypatch):
    """The real app. A pool that fails on use proves the route never touches the database."""
    pool = MagicMock()
    pool.acquire.side_effect = AssertionError("POST /auth/tenant-login must not read the database")
    monkeypatch.setattr(main, "pool", pool, raising=False)
    monkeypatch.setattr(main.app.state, "redis", None, raising=False)
    return TestClient(main.app, raise_server_exceptions=True)


@pytest.mark.parametrize("kw", BODIES)
def test_live_tenant_login_is_gone_for_any_body(live_client, kw):
    res = live_client.post("/auth/tenant-login", **kw)
    assert res.status_code == 410, res.text
    assert res.json() == GONE_BODY


def test_unmounted_auth_router_answers_the_same():
    app = FastAPI()
    app.include_router(auth.router)
    c = TestClient(app)
    for kw in BODIES:
        res = c.post("/auth/tenant-login", **kw)
        assert res.status_code == 410 and res.json() == GONE_BODY


def test_gone_body_names_the_partner_sign_in():
    assert auth.TENANT_LOGIN_GONE == GONE_BODY
    assert "partner" in auth.TENANT_LOGIN_GONE["message"]


def test_the_two_handlers_take_no_body():
    for fn in (main.tenant_login, auth.tenant_login):
        assert list(fn.__code__.co_varnames[:fn.__code__.co_argcount]) == []


# ── no route issues, shows or verifies a key ─────────────────────────────────────────────────────────────────────────

def test_openapi_has_no_key_route_or_scheme():
    doc = main.app.openapi()
    paths = doc["paths"]
    assert not [p for p in paths if "generate-key" in p or p.endswith("/api-key")]
    op = paths["/auth/tenant-login"]["post"]
    assert "requestBody" not in op and op.get("deprecated") is True
    assert set(op["responses"]) == {"410"}
    assert "TenantApiKey" not in doc.get("components", {}).get("securitySchemes", {})


def test_no_live_route_path_carries_a_key():
    for route in main.app.routes:
        path = getattr(route, "path", "")
        assert "generate-key" not in path and not path.endswith("/api-key"), path


def test_create_and_restore_answers_carry_no_key():
    assert "api_key" not in admin.CreateTenantResponse.model_fields
    assert "api_key" not in admin.RestoreTenantResponse.model_fields


# ── source guard ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _prod_files():
    for d in PROD_DIRS:
        for p in sorted((REPO / d).rglob("*.py")):
            rel = p.relative_to(REPO).as_posix()
            if rel.startswith("api/migrations/") or "/tests/" in rel:
                continue
            yield rel, p


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _findings(rel: str, source: str) -> list[str]:
    tree = ast.parse(source, filename=rel)
    docs = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.alias):
            name = node.asname or node.name.rsplit(".", 1)[-1]
        if name in FORBIDDEN_NAMES:
            found.append(f"{rel}:{getattr(node, 'lineno', '?')} name {name}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            for bad in FORBIDDEN_LITERALS:
                if bad in node.value:
                    found.append(f"{rel}:{node.lineno} literal {bad!r}")
        if isinstance(node, ast.Call):
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if called == "_create_jwt":
                found.append(f"{rel}:{node.lineno} calls _create_jwt (tests only)")
    return found


def test_no_production_code_issues_or_verifies_a_tenant_key():
    findings = []
    for rel, p in _prod_files():
        findings += _findings(rel, p.read_text(encoding="utf-8"))
    assert findings == []


def test_the_retired_modules_are_gone():
    for rel in ("api/lambda_authorizer", "services/acp_shared/e2e_tenant_key_sync.py"):
        assert not (REPO / rel).exists(), rel


@pytest.mark.parametrize("snippet,expected", [
    ('x = "UPDATE shared.tenants SET api_key_hash = $1"', "literal 'api_key_hash'"),
    ("def verify_tenant_api_key(): pass", "name verify_tenant_api_key"),
    ("from api.routers.auth import _hash_api_key", "name _hash_api_key"),
    ('h = request.headers.get("X-API-Key")', "literal 'X-API-Key'"),
    ("t = _create_jwt('a', 'b', 'c')", "calls _create_jwt"),
    ('@router.post("/tenants/{tenant_id}/generate-key")\ndef f(): pass', "literal 'generate-key'"),
])
def test_the_guard_catches_each_pattern(snippet, expected):
    assert any(expected in f for f in _findings("api/example.py", snippet))


def test_the_guard_lets_history_in_comments_and_docstrings():
    snippet = ('"""verify_tenant_api_key and api_key_hash were removed (AA-1036)."""\n'
               "# POST /admin/tenants/{id}/generate-key was removed\n"
               "def f():\n    \"\"\"the X-API-Key header is not read\"\"\"\n    return 1\n")
    assert _findings("api/example.py", snippet) == []
