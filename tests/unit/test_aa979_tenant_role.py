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


# ── Fix round 2: header shapes HTTPBearer accepts must not skip the check ────────────────────────────────────────

@pytest.mark.parametrize("header", [
    "bearer {t}",          # lower-case scheme (HTTPBearer compares scheme.lower())
    "BEARER {t}",
    "Bearer  {t}",         # two spaces (HTTPBearer strips the value)
    "Bearer {t} ",         # trailing space
    "Bearer \t{t}",
])
@pytest.mark.parametrize("method,path,role", [
    ("POST", "/v1/tours/pool/x/rewrite", "viewer"),
    ("PATCH", "/v1/content-writing/pieces/1", "viewer"),
    ("POST", "/admin/brand-identity", "viewer"),
    ("POST", "/v1/account/api-key", "editor"),
    ("DELETE", "/v1/integrations/facebook", "editor"),
    ("POST", "/v1/webhooks", "editor"),
])
def test_header_variants_are_refused_by_the_middleware(client, header, method, path, role):
    r = client.request(method, path, headers={"Authorization": header.format(t=_token(role))})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "studio_role_forbidden"


@pytest.mark.parametrize("header", ["bearer {t}", "Bearer  {t}"])
def test_header_variants_keep_allowed_calls(client, header):
    assert client.get("/v1/tours/pool", headers={"Authorization": header.format(t=_token("viewer"))}).status_code == 200
    r = client.post("/v1/tours/pool/x/rewrite", headers={"Authorization": header.format(t=_token("editor"))})
    assert r.status_code == 200
    r = client.post("/v1/account/api-key", headers={"Authorization": header.format(t=_token(None))})
    assert r.status_code == 200  # API-key token: unchanged


def test_any_tenant_token_in_the_header_is_checked(client):
    r = client.post("/v1/tours/x", headers={"Authorization": f"Token {_token('viewer')}"})
    assert r.status_code == 403  # fail closed even for a scheme HTTPBearer would not accept


def test_second_authorization_header_is_checked(client):
    r = client.post("/v1/tours/x", headers=[("Authorization", "Bearer junk"),
                                            ("Authorization", f"Bearer {_token('viewer')}")])
    assert r.status_code == 403


# ── The dependencies that accept the token enforce csr too (no middleware in this app) ────────────────────────

@pytest.fixture
def dep_client() -> TestClient:
    from fastapi import Depends

    from api.routers import admin_pipeline, v1_tours

    app = FastAPI()  # deliberately WITHOUT tenant_role_middleware

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "DELETE"])
    async def any_v1(path: str, tenant=Depends(v1_tours.get_tenant)):
        return {"ok": tenant["sub"]}

    @app.api_route("/admin/brand-identity", methods=["GET", "POST"])
    async def brand(tenant_id=Depends(admin_pipeline._resolve_brand_tenant_id)):
        return {"ok": tenant_id}
    return TestClient(app)


@pytest.mark.parametrize("header", ["Bearer {t}", "bearer {t}", "Bearer  {t}"])
@pytest.mark.parametrize("method,path,role,allowed", [
    ("POST", "/v1/tours/pool/x/rewrite", "viewer", False),
    ("GET", "/v1/tours/pool", "viewer", True),
    ("POST", "/v1/account/api-key", "editor", False),
    ("POST", "/v1/tours/pool/x/rewrite", "editor", True),
    ("POST", "/v1/account/api-key", "owner", True),
    ("POST", "/v1/account/api-key", None, True),
    ("POST", "/admin/brand-identity", "viewer", False),
    ("POST", "/admin/brand-identity", "editor", True),
])
def test_dependencies_enforce_csr(dep_client, header, method, path, role, allowed):
    from fastapi.security.utils import get_authorization_scheme_param

    token = _token(role)
    value = header.format(t=token)
    r = dep_client.request(method, path, headers={"Authorization": value})
    if get_authorization_scheme_param(value)[1] != token:
        # This FastAPI does not strip the value (older than the 0.161 pin): HTTPBearer hands the route " <t>", which
        # is not a JWT, so the route refuses it itself. Never a 200.
        assert r.status_code == 401, r.text
        return
    if allowed:
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == "studio_role_forbidden"


def test_dependency_direct_call_without_request_skips_the_check():
    from types import SimpleNamespace

    from api.routers import v1_tours
    assert v1_tours.get_tenant(SimpleNamespace(credentials=_token("viewer")))["csr"] == "viewer"


def test_dependency_refuses_with_a_request_whatever_the_fastapi_version():
    from types import SimpleNamespace

    from fastapi import HTTPException

    from api.routers import admin_pipeline, v1_tours

    def req(method, path):
        return SimpleNamespace(method=method, url=SimpleNamespace(path=path))

    with pytest.raises(HTTPException) as exc:
        v1_tours.get_tenant(SimpleNamespace(credentials=_token("viewer")), req("POST", "/v1/tours/pool/x/rewrite"))
    assert (exc.value.status_code, exc.value.detail) == (403, "studio_role_forbidden")
    with pytest.raises(HTTPException) as exc:
        v1_tours.get_tenant(SimpleNamespace(credentials=_token("editor")), req("POST", "/v1/account/api-key"))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        admin_pipeline._resolve_brand_tenant_id(SimpleNamespace(credentials=_token("viewer")), None,
                                                req("POST", "/admin/brand-identity"))
    assert exc.value.status_code == 403
    assert v1_tours.get_tenant(SimpleNamespace(credentials=_token("viewer")), req("GET", "/v1/tours/pool"))["sub"]


# ── Read-only POSTs (S256 portal smoke: a viewer opening the brand page got 403 on the live CTA check) ─────────

@pytest.mark.parametrize("method,path,allowed", [
    ("POST", "/v1/content-writing/cta-check", True),     # allow-listed: checks the text, stores nothing
    ("PUT", "/v1/content-writing/cta-check", False),     # exact method, not "any method on the path"
    ("POST", "/v1/content-writing/cta-check/x", False),  # exact path, not a prefix
    ("POST", "/v1/content-writing/cta-checker", False),
    ("POST", "/v1/content-writing/requests/r1/write", False),
    ("PATCH", "/v1/content-writing/pieces/1", False),
])
def test_viewer_read_only_post_allow_list_is_exact(client, method, path, allowed):
    r = _call(client, method, path, "viewer")
    assert r.status_code == (200 if allowed else 403), r.text


def test_read_only_posts_are_only_known_no_write_routes():
    assert tenant_role.READ_ONLY_POSTS == frozenset({("POST", "/v1/content-writing/cta-check")})
    assert all(m not in tenant_role.READ_METHODS for m, _ in tenant_role.READ_ONLY_POSTS)


@pytest.fixture
def writing_client() -> TestClient:
    """The real content-writing router (real ``cta_check`` and ``get_tenant``) behind the real middleware."""
    from api.routers import v1_content_writing

    app = FastAPI()
    app.middleware("http")(tenant_role.tenant_role_middleware)
    app.include_router(v1_content_writing.router)
    return TestClient(app)


@pytest.mark.parametrize("role", ["viewer", "editor", "owner", None])
def test_every_studio_role_gets_200_on_cta_check(writing_client, role):
    r = writing_client.post("/v1/content-writing/cta-check", json={"cta": "Rated 4.9 on TripAdvisor"},
                            headers={"Authorization": f"Bearer {_token(role)}"})
    assert r.status_code == 200, r.text
    assert r.json()["cta_warning"]  # the real check ran (the route, not a stub)


def test_viewer_still_refused_on_a_real_content_write(writing_client):
    headers = {"Authorization": f"Bearer {_token('viewer')}"}
    r = writing_client.patch(f"/v1/content-writing/pieces/{uuid.uuid4()}", json={"content_text": "x"}, headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "studio_role_forbidden"
    r = writing_client.post(f"/v1/content-writing/requests/{uuid.uuid4()}/write", json={}, headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "studio_role_forbidden"


def test_dependency_side_check_allows_cta_check_for_a_viewer():
    from types import SimpleNamespace

    from fastapi import HTTPException

    from api.routers import v1_tours

    def req(method, path):
        return SimpleNamespace(method=method, url=SimpleNamespace(path=path))

    creds = SimpleNamespace(credentials=_token("viewer"))
    assert v1_tours.get_tenant(creds, req("POST", "/v1/content-writing/cta-check"))["csr"] == "viewer"
    with pytest.raises(HTTPException) as exc:
        v1_tours.get_tenant(creds, req("POST", "/v1/content-writing/requests/r1/write"))
    assert (exc.value.status_code, exc.value.detail) == (403, "studio_role_forbidden")
