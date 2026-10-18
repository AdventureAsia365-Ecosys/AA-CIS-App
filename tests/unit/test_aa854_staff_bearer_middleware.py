"""AA-816 / AA-854 C3 — api/middleware/staff_bearer.py: a Cognito Bearer on a staff path sets the caller's
identity from the verified token, never from the client's headers.

Two harnesses:
- a small echo app (StaffBearerMiddleware around a BaseHTTPMiddleware stand-in for the rate limiter) that
  shows exactly which headers and contextvar a route sees;
- the real app (GET /admin/platform/staff, admin-only staff.read POLICY): a forged x-admin-role is
  overwritten, booking-only groups 403, an inactive row 401, an invalid Bearer 401 before the route, a
  legacy HS256 Bearer and secret-only requests unchanged, and the rate limiter's admin tracking sees the
  verified x-admin-user-id (the middleware wraps it).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from structlog.testing import capture_logs

from api.core import identity_config, staff_identity
from api.main import app as real_app
from api.middleware import rate_limit
from api.middleware.staff_bearer import StaffBearerMiddleware, is_staff_path
from api.routers import admin, admin_settings
from api.routers.auth import _create_admin_jwt, _create_jwt
from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, install_issuer

SECRET = "aa854-c3-test-admin-secret"
FORGED_ID = "99999999-9999-9999-9999-999999999999"


@pytest.fixture
def issuer():
    iss, _ = install_issuer()
    yield iss
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()


@pytest.fixture
def directory():
    return Directory()


# ── Echo app ───────────────────────────────────────────────────────────────────────────────────────

def _echo_app(directory) -> FastAPI:
    echo = FastAPI()
    seen: dict = {}

    async def stand_in_rate_limiter(request: Request, call_next):
        seen["rate_limiter_user"] = request.headers.get("x-admin-user-id")
        return await call_next(request)

    @echo.get("/admin/echo")
    @echo.get("/v1/pipeline/echo")
    @echo.get("/v1/tours/echo")
    async def _echo(request: Request):
        current = staff_identity.current_staff.get()
        return {
            "role": request.headers.get("x-admin-role"),
            "user": request.headers.get("x-admin-user-id"),
            "authorization": request.headers.get("authorization"),
            "roles_seen": request.headers.getlist("x-admin-role"),
            "current": current.admin_id if current else None,
            "state": getattr(getattr(request.state, "staff_identity", None), "admin_id", None),
            "rate_limiter_user": seen.get("rate_limiter_user"),
        }

    echo.add_middleware(BaseHTTPMiddleware, dispatch=stand_in_rate_limiter)
    echo.add_middleware(StaffBearerMiddleware)
    echo.state.pool = directory
    return echo


def test_cognito_bearer_overwrites_forged_identity_headers(issuer, directory):
    uid = directory.add(role="content", iss=ISS, sub=SUB)
    client = TestClient(_echo_app(directory))
    with capture_logs() as logs:
        res = client.get("/admin/echo", headers={
            "Authorization": f"Bearer {issuer.token(**{'cognito:groups': ['cis-content']})}",
            "x-admin-role": "admin", "x-admin-user-id": FORGED_ID,
        })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["role"] == "content" and body["roles_seen"] == ["content"]
    assert body["user"] == uid
    assert body["authorization"] is None              # downstream guards see a plain staff request
    assert body["current"] == uid and body["state"] == uid
    assert body["rate_limiter_user"] == uid           # wraps the rate limiter
    verified = [e for e in logs if e["event"] == "staff_bearer_verified"]
    assert verified and verified[0]["admin_id"] == uid and verified[0]["role"] == "content"


def test_contextvar_is_reset_after_the_request(issuer, directory):
    directory.add(iss=ISS, sub=SUB)
    client = TestClient(_echo_app(directory))
    client.get("/admin/echo", headers={"Authorization": f"Bearer {issuer.token()}"})
    assert staff_identity.current_staff.get() is None


def test_legacy_hs256_bearer_passes_through_untouched(issuer, directory):
    client = TestClient(_echo_app(directory))
    for token in (_create_admin_jwt(SUB, "u", "admin"), _create_jwt("t1", "Tenant", "starter")):
        res = client.get("/admin/echo", headers={
            "Authorization": f"Bearer {token}", "x-admin-role": "reviewer", "x-admin-user-id": FORGED_ID,
        })
        body = res.json()
        assert (body["role"], body["user"], body["authorization"]) == ("reviewer", FORGED_ID, f"Bearer {token}")
        assert body["current"] is None
    assert directory.sql == []


@pytest.mark.parametrize("headers", [
    {},                                                         # no Bearer
    {"Authorization": "Basic dXNlcjpwYXNz"},                    # not a Bearer
    {"Authorization": "Bearer not-a-jwt"},                      # not a JWT: left to the route's guard
])
def test_requests_without_a_cognito_bearer_are_unchanged(issuer, directory, headers):
    res = TestClient(_echo_app(directory)).get(
        "/v1/pipeline/echo", headers={**headers, "x-admin-role": "admin", "x-admin-user-id": FORGED_ID})
    assert res.json()["role"] == "admin" and res.json()["user"] == FORGED_ID
    assert directory.sql == []


def test_non_staff_paths_are_not_touched(issuer, directory):
    token = issuer.token(**{"cognito:groups": ["booking-admin"]})
    res = TestClient(_echo_app(directory)).get(
        "/v1/tours/echo", headers={"Authorization": f"Bearer {token}", "x-admin-role": "admin"})
    assert res.status_code == 200
    assert res.json()["authorization"] == f"Bearer {token}" and res.json()["role"] == "admin"


@pytest.mark.parametrize("path, staff", [
    ("/admin", True), ("/admin/x", True), ("/v1/pipeline/run", True), ("/v1/rules", True),
    ("/v1/social/batch-review", True), ("/administrator", False), ("/v1/tours", False), ("/auth/verify-admin", False),
])
def test_staff_path_matching(path, staff):
    assert is_staff_path(path) is staff


def test_invalid_cognito_bearer_is_401_and_never_reaches_the_route(issuer, directory):
    from tests.unit.test_identity_verifier import _rsa_key

    directory.add(iss=ISS, sub=SUB)
    with capture_logs() as logs:
        res = TestClient(_echo_app(directory)).get(
            "/admin/echo", headers={"Authorization": f"Bearer {issuer.token(key=_rsa_key())}"})
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"
    assert res.json() == {"detail": "Invalid staff token"}
    assert any(e["event"] == "staff_bearer_refused" and e["status"] == 401 for e in logs)
    assert directory.sql == []


def test_verifier_build_failure_is_503(directory, monkeypatch):
    from tests.unit.aa854_cognito_staff import StaffIssuer

    identity_config.reset_cis_staff_verifier()
    monkeypatch.setenv(identity_config.ENV_CIS_ADMIN_CLIENT_ID, "")
    monkeypatch.setenv(identity_config.ENV_WORKFORCE_ISSUER, "https://trailing-slash.example/")
    try:
        res = TestClient(_echo_app(directory)).get(
            "/admin/echo", headers={"Authorization": f"Bearer {StaffIssuer(ISS).token()}"})
    finally:
        identity_config.reset_cis_staff_verifier()
    assert res.status_code == 503


def test_missing_pool_is_503(issuer):
    echo = _echo_app(None)
    res = TestClient(echo).get("/admin/echo", headers={"Authorization": f"Bearer {issuer.token()}"})
    assert res.status_code == 503


# ── Real app ───────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def real(monkeypatch, directory, issuer):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)       # rate limiter tracking off unless a test sets it
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(real_app.state, "pool", directory, raising=False)
    return TestClient(real_app)


def _get_staff(client, token=None, **headers):
    h = {"X-Admin-Secret": SECRET, **headers}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return client.get("/admin/platform/staff", headers=h)


def test_real_app_forged_admin_header_is_overwritten_by_the_group_role(real, directory, issuer):
    uid = directory.add(role="content", iss=ISS, sub=SUB)
    with capture_logs() as logs:
        res = _get_staff(real, issuer.token(**{"cognito:groups": ["cis-content"]}),
                         **{"x-admin-role": "admin", "x-admin-user-id": FORGED_ID})
    assert res.status_code == 403
    assert res.json()["detail"] == "Role 'content' lacks the 'staff.read' permission"
    denied = [e for e in logs if e["event"] == "staff_policy_denied"]
    assert denied and denied[0]["admin_user"] == uid and denied[0]["role"] == "content"


def test_real_app_cis_admin_passes_with_a_forged_lower_role(real, directory, issuer):
    directory.add(role="admin", iss=ISS, sub=SUB)
    res = _get_staff(real, issuer.token(), **{"x-admin-role": "content"})
    assert res.status_code == 200, res.text
    assert res.json()["total"] == 1


def test_real_app_booking_only_groups_403(real, directory, issuer):
    directory.add(iss=ISS, sub=SUB)
    res = _get_staff(real, issuer.token(**{"cognito:groups": ["booking-admin"]}), **{"x-admin-role": "admin"})
    assert res.status_code == 403
    assert res.json()["detail"] == "No CIS staff group"


def test_real_app_inactive_row_401(real, directory, issuer):
    directory.add(active=False, iss=ISS, sub=SUB)
    assert _get_staff(real, issuer.token(), **{"x-admin-role": "admin"}).status_code == 401


def test_real_app_invalid_bearer_401_even_with_the_secret(real, directory, issuer):
    res = _get_staff(real, issuer.token(token_use="id"), **{"x-admin-role": "admin"})
    assert res.status_code == 401
    assert directory.sql == []


def test_real_app_cognito_bearer_still_needs_the_secret(real, directory, issuer):
    directory.add(iss=ISS, sub=SUB)
    res = real.get("/admin/platform/staff", headers={"Authorization": f"Bearer {issuer.token()}"})
    assert res.status_code == 403
    assert res.json()["detail"] == "Invalid admin secret"


def test_real_app_secret_only_and_legacy_bearer_unchanged(real, directory, issuer):
    directory.add()
    assert _get_staff(real, **{"x-admin-role": "admin"}).status_code == 200
    assert _get_staff(real, **{"x-admin-role": "content"}).status_code == 403
    legacy = _create_admin_jwt(SUB, "u", "content")
    assert _get_staff(real, legacy, **{"x-admin-role": "admin"}).status_code == 200
    assert directory.sql.count(staff_identity.SQL_BY_IDENTITY) == 0


def test_real_app_rate_limiter_tracks_the_verified_admin_id(real, directory, issuer, monkeypatch):
    uid = directory.add(iss=ISS, sub=SUB)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    track = AsyncMock()
    monkeypatch.setattr(rate_limit, "track_api_call", track)
    res = _get_staff(real, issuer.token(), **{"x-admin-user-id": FORGED_ID, "x-admin-role": "content"})
    assert res.status_code == 200, res.text
    assert track.await_args.kwargs["admin_user_id"] == uid


def test_middleware_is_registered_outside_the_rate_limiter():
    """Starlette: user_middleware[0] is the outermost layer."""
    names = [getattr(m.kwargs.get("dispatch"), "__name__", None) or m.cls.__name__ for m in real_app.user_middleware]
    assert names.index("StaffBearerMiddleware") < names.index("rate_limit_middleware")


# ── Review fixes: other issuers pass through; the dropped Authorization reaches real guards ───────

OTHER_ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_EXTERNALPOOL"


def test_non_workforce_issuer_bearer_passes_through_untouched(issuer, directory):
    """A JWT from another issuer (future EXTERNAL cis-portal token, CIS-minted Booking RS256) is not a staff
    token: the middleware leaves it, headers and all, to the route's own guard (review finding 2)."""
    token = issuer.token(iss=OTHER_ISS)
    res = TestClient(_echo_app(directory)).get(
        "/admin/echo", headers={"Authorization": f"Bearer {token}", "x-admin-role": "content"})
    assert res.status_code == 200
    body = res.json()
    assert body["authorization"] == f"Bearer {token}"
    assert body["role"] == "content" and body["current"] is None
    assert directory.sql == []


class _RoutePool(Directory):
    """Directory plus empty answers for the route's own queries (brand rules, raw_sources)."""

    def __init__(self) -> None:
        super().__init__()
        self.route_args: list = []

    async def fetchrow(self, sql, *args):
        if "tenant_brand_rules" in sql:
            self.route_args.append(args)
            return None
        return await super().fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        if "raw_sources" in sql:
            self.route_args.append(args)
            return []
        return await super().fetch(sql, *args)


@pytest.fixture
def route_pool(monkeypatch, issuer):
    pool = _RoutePool()
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(real_app.state, "pool", pool, raising=False)
    return pool


def test_real_app_brand_identity_cognito_bearer_is_a_staff_call(route_pool, issuer):
    """GET /admin/brand-identity reads a Bearer as a tenant JWT; with the Authorization header dropped after
    verify, a Cognito staff call is the secret-path staff call for the AA-internal tenant (not 401)."""
    route_pool.add(iss=ISS, sub=SUB)
    res = TestClient(real_app).get("/admin/brand-identity", headers={
        "Authorization": f"Bearer {issuer.token()}", "X-Admin-Secret": SECRET})
    assert res.status_code == 200, res.text
    assert res.json()["configured"] is False
    assert route_pool.route_args == [("00000000-0000-0000-0000-000000000001",)]


def test_real_app_brand_identity_other_issuer_meets_the_route_guard(route_pool, issuer):
    """A non-WORKFORCE iss reaches the route's tenant-JWT guard (401 from the route, not the staff verifier)."""
    route_pool.add(iss=ISS, sub=SUB)
    res = TestClient(real_app).get("/admin/brand-identity", headers={
        "Authorization": f"Bearer {issuer.token(iss=OTHER_ISS)}", "X-Admin-Secret": SECRET})
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid token"
    assert staff_identity.SQL_BY_IDENTITY not in route_pool.sql
    assert route_pool.route_args == []


def test_real_app_v1_pipeline_cognito_bearer_with_secret_is_200(route_pool, issuer, monkeypatch):
    route_pool.add(iss=ISS, sub=SUB)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    monkeypatch.setattr(rate_limit, "track_api_call", AsyncMock())
    res = TestClient(real_app).get("/v1/pipeline/sources", headers={
        "Authorization": f"Bearer {issuer.token()}", "X-Admin-Secret": SECRET})
    assert res.status_code == 200, res.text
    assert res.json()["sources"] == []
    assert route_pool.route_args and route_pool.route_args[0][0] == "00000000-0000-0000-0000-000000000001"
