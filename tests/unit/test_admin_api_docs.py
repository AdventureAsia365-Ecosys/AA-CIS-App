"""AA-577 follow-up — GET /admin/openapi.json (api/routers/admin_api_docs.py), read by the admin page
/admin/api-docs through the BFF. Admin secret + x-admin-role "admin" required, local/CI included."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.routers import admin

SECRET = "the-real-secret"
PATH = "/admin/openapi.json"


@pytest.fixture
def client():
    # No lifespan (no `with`): the route needs no DB / Redis.
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _secret():
    with patch.object(admin, "ADMIN_SECRET", SECRET):
        yield


def test_missing_secret_403(client):
    assert client.get(PATH, headers={"x-admin-role": "admin"}).status_code == 403


def test_wrong_secret_403(client):
    res = client.get(PATH, headers={"X-Admin-Secret": "wrong-secret", "x-admin-role": "admin"})
    assert res.status_code == 403


def test_right_secret_but_not_admin_role_403(client):
    """Reviewer/content staff share the backend secret; the role keeps them out."""
    for role in (None, "reviewer", "content", "tenant"):
        headers = {"X-Admin-Secret": SECRET}
        if role:
            headers["x-admin-role"] = role
        assert client.get(PATH, headers=headers).status_code == 403, role


def test_secret_not_configured_503(client):
    with patch.object(admin, "ADMIN_SECRET", ""):
        res = client.get(PATH, headers={"X-Admin-Secret": "", "x-admin-role": "admin"})
    assert res.status_code == 503


def test_right_secret_and_admin_role_returns_the_schema(client):
    res = client.get(PATH, headers={"X-Admin-Secret": SECRET, "x-admin-role": "admin"})
    assert res.status_code == 200
    body = res.json()
    assert "openapi" in body
    assert body["info"]["title"] == main.app.title
    assert "paths" in body and body["paths"]
    # Same schema the AA-577 /openapi.json route serves, and the route itself stays hidden.
    assert body == main.app.openapi()
    assert PATH not in body["paths"]


def test_aa577_docs_routes_unchanged():
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"} <= paths
    assert main.app.openapi_url is None


def test_declared_before_any_get_route_that_matches_it():
    """FastAPI matches in declaration order: no earlier GET route may capture /admin/openapi.json."""
    first = None
    for r in main.app.routes:
        regex = getattr(r, "path_regex", None)
        if regex is None or "GET" not in (getattr(r, "methods", None) or set()):
            continue
        if regex.match(PATH):
            first = r
            break
    assert first is not None
    assert first.path == PATH
    assert first.endpoint.__module__ == "api.routers.admin_api_docs"
