"""AA-911 decision D-1 (root ADR 0006a A11): a WORKFORCE token from the Preview-only ``cis-admin-preview`` client is
always the viewer, whatever ``cis-*`` group the user holds and whatever owner flag the row has.

Covers: api/core/identity_config (the preview client is accepted only when configured, and never equal to a
production client), api/core/staff_identity.resolve_staff (role viewer, is_owner False), the staff bearer middleware
(x-admin-role rewritten to viewer), staff_policy on the real app (writes 403 read-only, admin reads 403) and
POST /auth/verify-admin (the BFF gets role viewer). No network, no AWS, no DB.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import api.main as main_mod
from api.core import identity_config, staff_identity
from api.core.identity_verifier import IssuerConfig, MultiIssuerVerifier
from api.main import app as real_app
from api.main import get_pool
from api.middleware.staff_bearer import StaffBearerMiddleware
from api.routers import admin, admin_settings
from tests.unit.aa854_cognito_staff import BREAKGLASS, CIS_ADMIN, ISS, SUB, Directory, StaffIssuer
from tests.unit.test_identity_verifier import FakeFetcher

PREVIEW = "cisadminpreviewclient00001"
SECRET = "aa911-d1-test-admin-secret"


def _install(client_ids: frozenset[str]) -> StaffIssuer:
    issuer = StaffIssuer(ISS)
    cfg = IssuerConfig(issuer=ISS, client_ids=client_ids, name="workforce")
    identity_config._verifier = MultiIssuerVerifier([cfg], FakeFetcher(issuer))
    staff_identity.clear_cache()
    return issuer


@pytest.fixture
def issuer(monkeypatch):
    monkeypatch.setenv(identity_config.ENV_CIS_ADMIN_PREVIEW_CLIENT_ID, PREVIEW)
    monkeypatch.delenv("LEGACY_ADMIN_LOGIN_ENABLED", raising=False)
    iss = _install(frozenset({CIS_ADMIN, BREAKGLASS, PREVIEW}))
    main_mod._staff_state_cache.clear()
    yield iss
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()
    main_mod._staff_state_cache.clear()


@pytest.fixture
def directory():
    return Directory()


def _owner_admin(directory: Directory) -> str:
    uid = directory.add(role="admin", iss=ISS, sub=SUB)
    directory.row(uid)["is_owner"] = True
    return uid


def _preview_admin_token(issuer: StaffIssuer) -> str:
    return issuer.token(client_id=PREVIEW, **{"cognito:groups": ["cis-admin", "booking-admin"]})


# ── identity_config ──────────────────────────────────────────────────────────────────────────────

def test_preview_client_is_off_by_default(monkeypatch):
    monkeypatch.delenv(identity_config.ENV_CIS_ADMIN_PREVIEW_CLIENT_ID, raising=False)
    assert identity_config.DEV_CIS_ADMIN_PREVIEW_CLIENT_ID == "" or identity_config.cis_admin_preview_client_id()
    if not identity_config.cis_admin_preview_client_id():
        (cfg,) = identity_config.cis_staff_issuers()
        assert cfg.client_ids == frozenset({identity_config.cis_admin_client_id(),
                                            identity_config.breakglass_client_id()})
        assert not identity_config.is_preview_client("")
        assert not identity_config.is_preview_client(None)


def test_configured_preview_client_is_accepted_and_flagged(monkeypatch):
    monkeypatch.setenv(identity_config.ENV_CIS_ADMIN_PREVIEW_CLIENT_ID, PREVIEW)
    (cfg,) = identity_config.cis_staff_issuers()
    assert PREVIEW in cfg.client_ids
    assert identity_config.is_preview_client(PREVIEW)
    assert not identity_config.is_preview_client(identity_config.cis_admin_client_id())
    assert not identity_config.is_preview_client(identity_config.breakglass_client_id())


@pytest.mark.parametrize("same_as", ["cis-admin", "breakglass"])
def test_preview_client_equal_to_a_production_client_is_refused(monkeypatch, same_as):
    value = identity_config.cis_admin_client_id() if same_as == "cis-admin" else identity_config.breakglass_client_id()
    monkeypatch.setenv(identity_config.ENV_CIS_ADMIN_PREVIEW_CLIENT_ID, value)
    with pytest.raises(ValueError):
        identity_config.cis_staff_issuers()


# ── resolve_staff ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preview_token_with_admin_group_and_owner_row_is_a_viewer(issuer, directory):
    uid = _owner_admin(directory)
    identity = await staff_identity.authenticate(directory, _preview_admin_token(issuer))
    assert identity.admin_id == uid
    assert (identity.role, identity.is_owner, identity.preview) == ("viewer", False, True)
    assert identity.db_role == "admin"  # the row is untouched; only the session is capped


@pytest.mark.asyncio
async def test_same_user_on_the_production_client_keeps_admin_and_owner(issuer, directory):
    _owner_admin(directory)
    identity = await staff_identity.authenticate(directory, issuer.token(**{"cognito:groups": ["cis-admin"]}))
    assert (identity.role, identity.is_owner, identity.preview) == ("admin", True, False)


@pytest.mark.asyncio
async def test_preview_token_is_401_when_no_preview_client_is_configured(monkeypatch, directory):
    monkeypatch.delenv(identity_config.ENV_CIS_ADMIN_PREVIEW_CLIENT_ID, raising=False)
    issuer = _install(frozenset({CIS_ADMIN, BREAKGLASS}))
    try:
        _owner_admin(directory)
        with pytest.raises(staff_identity.StaffAuthError) as exc:
            await staff_identity.authenticate(directory, _preview_admin_token(issuer))
    finally:
        identity_config.reset_cis_staff_verifier()
        staff_identity.clear_cache()
    assert exc.value.status == 401
    assert directory.sql == []


# ── Bearer middleware: x-admin-role is the viewer ────────────────────────────────────────────────

def test_middleware_rewrites_the_role_to_viewer_over_a_forged_admin_header(issuer, directory):
    _owner_admin(directory)
    echo = FastAPI()

    @echo.get("/admin/echo")
    async def _echo(request: Request):
        return {"role": request.headers.get("x-admin-role"),
                "preview": staff_identity.current_staff.get().preview}

    echo.add_middleware(StaffBearerMiddleware)
    echo.state.pool = directory
    res = TestClient(echo).get("/admin/echo", headers={"Authorization": f"Bearer {_preview_admin_token(issuer)}",
                                                       "x-admin-role": "admin"})
    assert res.status_code == 200, res.text
    assert res.json() == {"role": "viewer", "preview": True}


# ── Real app: staff_policy refuses writes and admin reads ────────────────────────────────────────

@pytest.fixture
def real(monkeypatch, directory, issuer):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(real_app.state, "pool", directory, raising=False)
    return TestClient(real_app)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/admin/platform/staff", {"email": "new@adventure.asia", "display_name": "New", "role": "admin"}),
        ("POST", "/admin/platform/staff/00000000-0000-4000-8000-000000000001/disable", None),
        ("PATCH", "/admin/platform/staff/00000000-0000-4000-8000-000000000001/roles", {"role": "admin"}),
        ("POST", "/admin/waves/check", {}),
    ],
)
def test_real_app_preview_admin_writes_are_403_read_only(real, directory, issuer, method, path, body):
    _owner_admin(directory)
    res = real.request(method, path, json=body, headers={"Authorization": f"Bearer {_preview_admin_token(issuer)}",
                                                        "x-admin-role": "admin"})
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == "Role 'viewer' is read-only"


def test_real_app_preview_admin_cannot_read_the_staff_list(real, directory, issuer):
    _owner_admin(directory)
    res = real.get("/admin/platform/staff", headers={"Authorization": f"Bearer {_preview_admin_token(issuer)}"})
    assert res.status_code == 403
    assert res.json()["detail"] == "Role 'viewer' lacks the 'staff.read' permission"


def test_real_app_production_client_admin_still_reads_the_staff_list(real, directory, issuer):
    _owner_admin(directory)
    res = real.get("/admin/platform/staff",
                   headers={"Authorization": f"Bearer {issuer.token(**{'cognito:groups': ['cis-admin']})}"})
    assert res.status_code == 200, res.text


# ── /auth/verify-admin: the BFF gets the viewer ──────────────────────────────────────────────────

def test_verify_admin_returns_viewer_for_a_preview_token(issuer, directory):
    uid = _owner_admin(directory)
    real_app.dependency_overrides[get_pool] = lambda: directory
    try:
        res = TestClient(real_app).post("/auth/verify-admin", json={"token": _preview_admin_token(issuer)})
    finally:
        real_app.dependency_overrides.pop(get_pool, None)
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["admin_id"], body["role"], body["auth"]) == (uid, "viewer", "cognito")
