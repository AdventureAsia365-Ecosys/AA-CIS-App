"""AA-725 part 3 — route-decorator sanity, so a mis-bound route turns CI red instead of
reaching Dev and returning 422.

Regression context (S211, PR #559): a plain helper `_derive_run_display_status` was inserted
BETWEEN `@router.get(".../details")` and the real `async def get_tenant_details`. The decorator
bound to the helper instead — the real endpoint lost its route, and the path started returning
422 ("query status/tours_total/tours_passed required", the helper's own parameters). flake8 and
`tsc` did not catch it; only the live 422 did.

Two independent checks, both static (no DB, no running app needed):

1. AST — the function directly under a `@router.<m>(...)` / `@app.<m>(...)` route decorator is
   never a private helper (`_name`). An endpoint is never named with a leading underscore, so a
   helper sitting under the decorator is exactly the S211 mistake.
2. Live import — `from api.main import app` and call every parameterless GET route through
   `TestClient`. None may answer 422 from a leaked helper parameter (`field required` in query).
   This also fails loudly if importing the app or wiring a router breaks outright.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROUTERS = REPO / "api" / "routers"

# Decorator attribute names that register an HTTP route.
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
# The objects routes are registered on across the codebase.
_ROUTE_OWNERS = {"router", "app"}


def _is_route_decorator(dec: ast.expr) -> bool:
    # Matches @router.get(...), @app.post(...), @<something>_router.get(...), etc.
    if not isinstance(dec, ast.Call):
        return False
    func = dec.func
    if not isinstance(func, ast.Attribute) or func.attr not in _HTTP_METHODS:
        return False
    owner = func.value
    if isinstance(owner, ast.Name):
        return owner.id in _ROUTE_OWNERS or owner.id.endswith("router")
    return False


def _route_functions(path: Path):
    """Yield (lineno, func_name) for every function carrying an HTTP route decorator."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_route_decorator(d) for d in node.decorator_list):
            yield node.lineno, node.name


def test_no_route_decorator_binds_a_private_helper():
    """The S211 shape: a `_helper` ends up decorated as a route."""
    offenders = []
    for path in ROUTERS.rglob("*.py"):
        for lineno, name in _route_functions(path):
            if name.startswith("_"):
                offenders.append(f"{path.relative_to(REPO)}:{lineno} route bound to helper {name}()")
    assert not offenders, (
        "A route decorator is bound to a private helper — a non-route function was inserted "
        "between the decorator and the real endpoint (S211). Move the helper above the "
        "decorator:\n" + "\n".join(offenders)
    )


def test_parameterless_get_routes_do_not_422():
    """Call every GET route that needs no path/query/body arg and assert it never answers 422.

    A 422 on such a route means the handler is bound to the wrong function and FastAPI is asking
    for that function's leaked parameters — the exact S211 symptom.
    """
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    from api.main import app

    client = TestClient(app, raise_server_exceptions=False)
    checked = 0
    offenders = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        # Skip routes with path parameters (need a value) — the AST check covers those.
        if "{" in route.path:
            continue
        # Skip routes whose own signature declares required query params or a body: a 422 there is
        # legitimate, not a binding bug.
        required_params = [
            p for p in route.dependant.query_params if p.required
        ] + list(route.dependant.body_params)
        if required_params:
            continue
        resp = client.get(route.path)
        checked += 1
        if resp.status_code == 422:
            offenders.append(f"{route.path} -> 422 {resp.json()}")
    assert checked > 0, "no parameterless GET routes were exercised — the app wiring may be broken"
    assert not offenders, (
        "Parameterless GET routes answered 422 (mis-bound handler, S211):\n" + "\n".join(offenders)
    )
