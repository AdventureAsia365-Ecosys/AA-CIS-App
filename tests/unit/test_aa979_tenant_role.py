"""AA-979 P2b — Content studio roles on tenant tokens (api/middleware/tenant_role.py): viewer read only, editor no
account writes, owner and API-key tokens unchanged; the middleware is wired into the real app."""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware import tenant_role
from api.routers.auth import _create_jwt, _create_partner_session_jwt, studio_role_of

TENANT = str(uuid.uuid4())


def _token(role: str | None) -> str:
    if role is None:
        return _create_jwt(TENANT, "T", "growth", client="portal")  # an API-key session
    token, _ = _create_partner_session_jwt(TENANT, "T", "growth", membership_id=str(uuid.uuid4()),
                                           content_studio_role=role)
    return token


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.middleware("http")(tenant_role.tenant_role_middleware)

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PATCH", "DELETE", "PUT"])
    async def any_v1(path: str):
        return {"ok": path}

    @app.api_route("/admin/brand-identity", methods=["GET", "POST"])
    async def brand():
        return {"ok": "brand"}

    @app.post("/auth/tenant-login")
    async def login():
        return {"ok": "login"}
    return TestClient(app)


def _call(client, method, path, role):
    return client.request(method, path, headers={"Authorization": f"Bearer {_token(role)}"})


@pytest.mark.parametrize("method,path,role,allowed", [
    ("GET", "/v1/tours/pool", "viewer", True),
    ("POST", "/v1/tours/pool/x/rewrite", "viewer", False),
    ("PATCH", "/v1/content-writing/pieces/1", "viewer", False),
    ("POST", "/admin/brand-identity", "viewer", False),
    ("GET", "/admin/brand-identity", "viewer", True),
    ("POST", "/v1/tours/pool/x/rewrite", "editor", True),
    ("POST", "/admin/brand-identity", "editor", True),
    ("POST", "/v1/integrations/wordpress", "editor", False),
    ("DELETE", "/v1/integrations/facebook", "editor", False),
    ("POST", "/v1/account/api-key", "editor", False),
    ("POST", "/v1/webhooks", "editor", False),
    ("GET", "/v1/account/members", "editor", True),
    ("POST", "/v1/integrations/wordpress", "owner", True),
    ("POST", "/v1/account/api-key", None, True),   # API-key token: owner behaviour, unchanged
    ("DELETE", "/v1/integrations/facebook", None, True),
    ("POST", "/v1/accounting", "editor", True),     # prefix match is per path segment
])
def test_role_matrix(client, method, path, role, allowed):
    r = _call(client, method, path, role)
    if allowed:
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 403
        assert r.json()["detail"] == "studio_role_forbidden"


def test_unknown_role_value_is_treated_as_viewer():
    assert studio_role_of({"csr": "superuser"}) == "viewer"
    assert studio_role_of({}) is None
    assert tenant_role.refusal("POST", "/v1/tours", "viewer") is True


def test_invalid_or_staff_bearer_passes_through_to_the_route(client):
    r = client.post("/v1/tours/x", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 200  # the route's own dependency answers (not this middleware)
    r = client.post("/auth/tenant-login", headers={"Authorization": f"Bearer {_token('viewer')}"})
    assert r.status_code == 200  # not a tenant path


def test_middleware_is_registered_on_the_real_app():
    from api.main import app
    names = [getattr(m, "kwargs", {}).get("dispatch") for m in app.user_middleware]
    assert tenant_role.tenant_role_middleware in names
