"""AA-816 A10b (S252, root ADR 0006a A10b) — the Dev-only E2E CIS admin.

Covers api/core/e2e_staff.py, the staff directory (address, scope, environment guard on every path that can create or
re-enable it; disable stays allowed everywhere), staff_identity.resolve_staff (``e2e_read_only``; 403
``e2e_not_allowed_here`` unless ENVIRONMENT is exactly ``dev``), the bearer middleware (every non-GET/HEAD/OPTIONS on a
staff path is 403 ``e2e_read_only`` before the route runs, EXCEPT the two content-quality sentence-apply routes),
staff_authz (the same rule by route template, defence in depth) and the content-quality apply routes on the real app
(only ``cut`` / ``manual``; the allow-listed routes really reach the handler). The A10 reviewer and every other staff
member are unchanged. No network, no AWS, no DB.
"""
from __future__ import annotations

import glob
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Any, Optional

import pytest
from botocore.stub import Stubber
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.core import e2e_staff, identity_config, staff_authz, staff_identity
from api.core.identity_verifier import IssuerConfig, MultiIssuerVerifier
from api.main import app as real_app
from api.middleware.staff_bearer import StaffBearerMiddleware
from api.routers import admin, admin_settings
from api.services import staff_directory as sd
from tests.unit.aa854_cognito_staff import BREAKGLASS, CIS_ADMIN, ISS, SUB, Directory, StaffIssuer
from tests.unit.test_aa816_staff_directory_writes import POOL, StaffDB, _client, _create, _expect, _group
from tests.unit.test_identity_verifier import FakeFetcher

ADMIN_E2E = "e2e-cis-admin@e2e-mail.lumiguides.it.com"
REVIEWER_E2E = "e2e@e2e-mail.lumiguides.it.com"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SECRET = "aa816-a10b-test-admin-secret"
PIECE = "0b5e9a52-6a43-4d55-9d5e-0f6c1b2a3c4d"
VERSION = "7f0c2d4e-1a2b-4c3d-8e9f-001122334455"
NOT_DEV = [None, "", "test", "prod", "production", "staging", "Dev", "DEV", " dev", "dev ", "development"]


def _set_env(monkeypatch, value: Optional[str]) -> None:
    if value is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", value)


# ── api/core/e2e_staff.py ────────────────────────────────────────────────────────────────────────

def test_constants():
    assert e2e_staff.E2E_CIS_ADMIN_TEST_STAFF_EMAIL == ADMIN_E2E
    assert e2e_staff.E2E_CIS_ADMIN_TEST_STAFF_ROLE == "admin"
    assert sd.E2E_CIS_ADMIN_TEST_STAFF_EMAIL == ADMIN_E2E and sd.E2E_CIS_ADMIN_TEST_STAFF_ROLE == "admin"
    assert e2e_staff.E2E_READ_ONLY == "e2e_read_only" and e2e_staff.E2E_NOT_ALLOWED_HERE == "e2e_not_allowed_here"
    assert e2e_staff.READ_METHODS == staff_authz.READ_METHODS
    assert e2e_staff.E2E_SENTENCE_MODES == {"cut", "manual"}


def test_the_write_allow_list_is_exactly_the_two_sentence_apply_routes():
    # Guard: a new entry must be a reviewed change of this test (S252 orchestrator decision).
    assert e2e_staff.E2E_ALLOWED_WRITES == {
        ("POST", "/admin/content-quality/pieces/{piece_id}/sentences/apply"),
        ("POST", "/admin/content-quality/tour-versions/{version_id}/sentences/apply"),
    }
    # Both are real routes of the app, with a POLICY entry, admin-only by capability.
    routes = {(m, r.path) for r in real_app.routes for m in getattr(r, "methods", ())}
    for method, route in e2e_staff.E2E_ALLOWED_WRITES:
        assert (method, route) in routes
        assert staff_authz.POLICY[(method, route)] in staff_authz.ROLE_PERMS["admin"]
    # The path regexes and the templates name the same routes.
    assert len(e2e_staff.E2E_ALLOWED_WRITE_PATHS) == len(e2e_staff.E2E_ALLOWED_WRITES)


@pytest.mark.parametrize("value,expected", [("dev", True)] + [(v, False) for v in NOT_DEV])
def test_dev_only_environment_is_an_exact_match(monkeypatch, value, expected):
    _set_env(monkeypatch, value)
    assert e2e_staff.dev_only_e2e_environment() is expected


@pytest.mark.parametrize("method,path", [
    ("POST", f"/admin/content-quality/pieces/{PIECE}/sentences/apply"),
    ("POST", f"/admin/content-quality/tour-versions/{VERSION}/sentences/apply"),
    ("POST", f"/admin/content-quality/pieces/{PIECE.upper()}/sentences/apply"),
])
def test_allowed_write_paths(method, path):
    assert e2e_staff.is_allowed_e2e_write_path(method, path)


@pytest.mark.parametrize("method,path", [
    ("POST", f"/admin/content-quality/pieces/{PIECE}/sentences/suggest"),            # the paid model call
    ("POST", f"/admin/content-quality/tour-versions/{VERSION}/sentences/suggest"),
    ("PATCH", f"/admin/content-quality/pieces/{PIECE}/sentences/apply"),
    ("PUT", f"/admin/content-quality/pieces/{PIECE}/sentences/apply"),
    ("DELETE", f"/admin/content-quality/pieces/{PIECE}/sentences/apply"),
    ("POST", f"/admin/content-quality/pieces/{PIECE}/sentences/apply/"),
    ("POST", f"/admin/content-quality/pieces/{PIECE}/sentences/apply/x"),
    ("POST", f"/x/admin/content-quality/pieces/{PIECE}/sentences/apply"),
    ("POST", "/admin/content-quality/pieces/not-a-uuid/sentences/apply"),
    ("POST", f"/admin/content-quality/pieces/{PIECE}/../../platform/staff/sentences/apply"),
    ("POST", f"/admin/content-quality/pieces/{PIECE}/label"),
    ("PATCH", f"/admin/content-quality/pieces/{PIECE}/signals/{VERSION}"),
    ("PATCH", f"/admin/content-quality/place-cards/{PIECE}"),
    ("POST", "/admin/content-quality/place-cards/build"),
    ("POST", "/admin/platform/staff"),
    ("POST", ""),
])
def test_everything_else_is_not_an_allowed_write(method, path):
    assert not e2e_staff.is_allowed_e2e_write_path(method, path)


# ── Staff directory ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [ADMIN_E2E, "  E2E-CIS-Admin@E2E-Mail.Lumiguides.IT.com "])
def test_the_admin_address_is_a_staff_address(value):
    assert sd.require_staff_email(value) == ADMIN_E2E
    assert sd.is_e2e_test_email(value)


@pytest.mark.parametrize("email", [
    "e2e-cis-admin2@e2e-mail.lumiguides.it.com",
    "e2e-cis-admin+x@e2e-mail.lumiguides.it.com",
    "e2e-admin@e2e-mail.lumiguides.it.com",
    "e2e-cis-admin@x.e2e-mail.lumiguides.it.com",
    "e2e-cis-admin@e2e-mail.lumiguides.it.com.evil.com",
    "e2e-cis-admin@lumiguides.it.com",
    "e2e-cis-ａdmin@e2e-mail.lumiguides.it.com",
    "e2e-cis-admin@e2e-mail.lumiguides.it.com​",
])
def test_look_alikes_are_refused(email):
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_staff_email(email)
    assert err.value.status == 400
    assert not sd.is_e2e_test_email(email)


def test_scope_accepts_admin_without_booking():
    sd.require_e2e_test_scope(ADMIN_E2E, "admin", None)


@pytest.mark.parametrize("role,booking", [("reviewer", None), ("content", None), ("viewer", None), (None, None),
                                          ("admin", "admin"), ("admin", "sales")])
def test_scope_refuses_anything_but_admin_without_booking(role, booking):
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_e2e_test_scope(ADMIN_E2E, role, booking)
    assert (err.value.status, err.value.code) == (400, "e2e_test_scope")
    assert "E2E CIS admin account" in err.value.detail and "admin" in err.value.detail


def test_the_a10_reviewer_scope_is_unchanged():
    sd.require_e2e_test_scope(REVIEWER_E2E, "reviewer", None)
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_e2e_test_scope(REVIEWER_E2E, "admin", None)   # e2e@ can not borrow the admin role
    assert err.value.code == "e2e_test_scope" and "reviewer" in err.value.detail


@pytest.mark.parametrize("value", NOT_DEV)
def test_environment_guard_refuses_outside_dev(monkeypatch, value):
    _set_env(monkeypatch, value)
    with pytest.raises(sd.StaffDirectoryError) as err:
        sd.require_dev_only_e2e_environment(ADMIN_E2E)
    assert (err.value.status, err.value.code) == (403, "e2e_not_allowed_here")
    sd.require_dev_only_e2e_environment(REVIEWER_E2E)            # no-op for the A10 address
    sd.require_dev_only_e2e_environment("someone@adventure.asia")  # and for company addresses


def test_environment_guard_passes_on_dev(monkeypatch):
    _set_env(monkeypatch, "dev")
    sd.require_dev_only_e2e_environment(ADMIN_E2E)


@pytest.fixture
def stubbed():
    client = _client()
    with Stubber(client) as stub:
        yield sd.CognitoDirectory(client=client, pool_id=POOL), stub
        stub.assert_no_pending_responses()


@pytest.fixture
def db():
    staff_identity.clear_cache()
    d = StaffDB()
    d.owner = d.add(owner=True, email="owner@adventure.asia", username="owner", role="admin")
    yield d
    staff_identity.clear_cache()


@pytest.mark.asyncio
async def test_create_on_dev_inserts_an_admin_row_that_is_not_an_owner(db, stubbed, monkeypatch):
    _set_env(monkeypatch, "dev")
    cognito, stub = stubbed
    _expect(stub, "admin_create_user", _create(ADMIN_E2E), {"User": {"Username": ADMIN_E2E}})
    _expect(stub, "admin_add_user_to_group", _group(ADMIN_E2E, "cis-admin"))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=ADMIN_E2E, display_name="E2E CIS admin",
                                   role="admin", booking_role=None, send_invite=False)
    assert result.status == sd.STATUS_OK and result.created
    assert result.steps == ["create_user", "add_group:cis-admin"]
    row = db.row(result.staff_id)
    assert (row["email"], row["role"], row["is_owner"], row["password_hash"]) == (ADMIN_E2E, "admin", False, None)
    assert (result.staff_id, "booking") not in db.grants
    assert db.actions() == [sd.StaffAudit.CREATED]


@pytest.mark.parametrize("value", [None, "test", "prod", "Dev "])
@pytest.mark.asyncio
async def test_create_outside_dev_is_refused_before_any_write(db, stubbed, monkeypatch, value):
    _set_env(monkeypatch, value)
    cognito, _ = stubbed   # no stubbed response: any Cognito call would fail the test
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email=ADMIN_E2E, display_name="E2E CIS admin",
                              role="admin")
    assert (err.value.status, err.value.code) == (403, "e2e_not_allowed_here")
    assert len(db.rows) == 1 and db.audit == []


@pytest.mark.parametrize("role,booking", [("reviewer", None), ("content", None), ("admin", "sales")])
@pytest.mark.asyncio
async def test_create_with_another_scope_is_refused_on_dev_too(db, stubbed, monkeypatch, role, booking):
    _set_env(monkeypatch, "dev")
    cognito, _ = stubbed
    with pytest.raises(sd.StaffDirectoryError) as err:
        await sd.create_staff(db, cognito, actor_id=db.owner, email=ADMIN_E2E, display_name="E2E", role=role,
                              booking_role=booking)
    assert err.value.code == "e2e_test_scope" and len(db.rows) == 1 and db.audit == []


@pytest.mark.parametrize("op", ["enable", "sign_out", "sync", "roles"])
@pytest.mark.asyncio
async def test_every_path_that_can_re_enable_it_is_refused_outside_dev(db, stubbed, monkeypatch, op):
    _set_env(monkeypatch, "prod")
    cognito, _ = stubbed
    uid = db.add(email=ADMIN_E2E, role="admin", active=op != "sync")
    calls = {
        "enable": lambda: sd.enable_staff(db, cognito, actor_id=db.owner, staff_id=uid),
        "sign_out": lambda: sd.sign_out_staff(db, cognito, actor_id=db.owner, staff_id=uid),
        "sync": lambda: sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid),
        "roles": lambda: sd.change_roles(db, cognito, actor_id=db.owner, staff_id=uid, role="admin"),
    }
    if op == "sync":
        db.row(uid)["is_active"] = True   # sync of an active row creates / enables / adds groups
    if op == "enable":
        db.row(uid)["is_active"] = False
    with pytest.raises(sd.StaffDirectoryError) as err:
        await calls[op]()
    assert (err.value.status, err.value.code) == (403, "e2e_not_allowed_here")
    assert db.audit == []


@pytest.mark.parametrize("value", ["prod", None, "dev"])
@pytest.mark.asyncio
async def test_disable_stays_allowed_everywhere(db, stubbed, monkeypatch, value):
    _set_env(monkeypatch, value)
    cognito, stub = stubbed
    uid = db.add(email=ADMIN_E2E, role="admin")
    _expect(stub, "admin_disable_user", {"UserPoolId": POOL, "Username": ADMIN_E2E})
    _expect(stub, "admin_user_global_sign_out", {"UserPoolId": POOL, "Username": ADMIN_E2E})
    result = await sd.disable_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK and db.row(uid)["is_active"] is False


@pytest.mark.asyncio
async def test_sync_of_a_disabled_row_disables_outside_dev(db, stubbed, monkeypatch):
    _set_env(monkeypatch, "prod")
    cognito, stub = stubbed
    uid = db.add(email=ADMIN_E2E, role="admin", active=False)
    _expect(stub, "admin_disable_user", {"UserPoolId": POOL, "Username": ADMIN_E2E})
    _expect(stub, "admin_user_global_sign_out", {"UserPoolId": POOL, "Username": ADMIN_E2E})
    result = await sd.sync_staff(db, cognito, actor_id=db.owner, staff_id=uid)
    assert result.status == sd.STATUS_OK


@pytest.mark.asyncio
async def test_the_a10_reviewer_create_is_not_env_gated(db, stubbed, monkeypatch):
    _set_env(monkeypatch, "test")
    cognito, stub = stubbed
    _expect(stub, "admin_create_user", _create(REVIEWER_E2E), {"User": {"Username": REVIEWER_E2E}})
    _expect(stub, "admin_add_user_to_group", _group(REVIEWER_E2E, "cis-reviewer"))
    result = await sd.create_staff(db, cognito, actor_id=db.owner, email=REVIEWER_E2E, display_name="E2E",
                                   role="reviewer")
    assert result.status == sd.STATUS_OK


# ── resolve_staff ────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def issuer():
    iss = StaffIssuer(ISS)
    cfg = IssuerConfig(issuer=ISS, client_ids=frozenset({CIS_ADMIN, BREAKGLASS}), name="workforce")
    identity_config._verifier = MultiIssuerVerifier([cfg], FakeFetcher(iss))
    staff_identity.clear_cache()
    yield iss
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()


@pytest.fixture
def directory():
    return Directory()


def _admin_e2e_row(directory: Directory, owner: bool = False) -> str:
    uid = directory.add(role="admin", email=ADMIN_E2E, iss=ISS, sub=SUB, username="e2e-cis-admin")
    directory.row(uid)["is_owner"] = owner
    return uid


def _admin_e2e_token(issuer: StaffIssuer, groups=("cis-admin",)) -> str:
    return issuer.token(email=ADMIN_E2E, **{"cognito:groups": list(groups)})


@pytest.mark.asyncio
async def test_resolve_flags_the_admin_e2e_session_read_only_on_dev(issuer, directory, monkeypatch, capsys):
    _set_env(monkeypatch, "dev")
    uid = _admin_e2e_row(directory, owner=True)   # an owner flag could only come from a hand-edited row
    identity = await staff_identity.authenticate(directory, _admin_e2e_token(issuer))
    assert identity.admin_id == uid
    assert (identity.role, identity.e2e_read_only, identity.is_owner, identity.preview) == ("admin", True, False,
                                                                                          False)


@pytest.mark.asyncio
async def test_first_sign_in_by_email_is_flagged_too(issuer, directory, monkeypatch):
    _set_env(monkeypatch, "dev")
    directory.add(role="admin", email=ADMIN_E2E, username="e2e-cis-admin")   # not bound yet
    identity = await staff_identity.authenticate(directory, _admin_e2e_token(issuer))
    assert identity.e2e_read_only and directory.links == 1


@pytest.mark.parametrize("value", NOT_DEV)
@pytest.mark.asyncio
async def test_resolve_refuses_the_admin_e2e_outside_dev(issuer, directory, monkeypatch, value):
    _set_env(monkeypatch, value)
    _admin_e2e_row(directory)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.authenticate(directory, _admin_e2e_token(issuer))
    assert (exc.value.status, exc.value.detail, exc.value.reason) == (403, "e2e_not_allowed_here",
                                                                      "e2e_not_allowed_here")


@pytest.mark.asyncio
async def test_a_company_row_with_the_admin_e2e_token_email_is_capped_too(issuer, directory, monkeypatch):
    # Belt and braces: the cap follows the token's email as well as the row's.
    _set_env(monkeypatch, "dev")
    directory.add(role="admin", email="someone@adventure.asia", iss=ISS, sub=SUB)
    identity = await staff_identity.authenticate(directory, _admin_e2e_token(issuer))
    assert identity.e2e_read_only


@pytest.mark.asyncio
async def test_other_staff_are_not_flagged(issuer, directory, monkeypatch):
    _set_env(monkeypatch, "prod")
    directory.add(role="admin", email="staff@adventure.asia", iss=ISS, sub=SUB)
    identity = await staff_identity.authenticate(directory, issuer.token(**{"cognito:groups": ["cis-admin"]}))
    assert (identity.role, identity.e2e_read_only) == ("admin", False)


@pytest.mark.asyncio
async def test_the_a10_reviewer_is_not_flagged_and_not_env_gated(issuer, directory, monkeypatch):
    _set_env(monkeypatch, "test")
    directory.add(role="reviewer", email=REVIEWER_E2E, iss=ISS, sub=SUB)
    identity = await staff_identity.authenticate(
        directory, issuer.token(email=REVIEWER_E2E, **{"cognito:groups": ["cis-reviewer"]}))
    assert (identity.role, identity.e2e_read_only) == ("reviewer", False)


# ── Bearer middleware ────────────────────────────────────────────────────────────────────────────

def _echo_app(directory) -> tuple[FastAPI, list[str]]:
    reached: list[str] = []
    echo = FastAPI()

    @echo.api_route("/admin/{rest:path}", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
    async def _any(rest: str, request: Request):
        reached.append(f"{request.method} {rest}")
        identity = staff_identity.current_staff.get()
        return {"role": request.headers.get("x-admin-role"), "e2e_read_only": identity.e2e_read_only}

    echo.add_middleware(StaffBearerMiddleware)
    echo.state.pool = directory
    return echo, reached


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_middleware_lets_reads_through_as_admin(issuer, directory, monkeypatch, method):
    _set_env(monkeypatch, "dev")
    _admin_e2e_row(directory)
    app, reached = _echo_app(directory)
    res = TestClient(app).request(method, "/admin/content-quality/pieces",
                                  headers={"Authorization": f"Bearer {_admin_e2e_token(issuer)}"})
    assert res.status_code == 200, res.text
    assert reached == [f"{method} content-quality/pieces"]
    if method == "GET":
        assert res.json() == {"role": "admin", "e2e_read_only": True}


@pytest.mark.parametrize("method,path", [
    ("POST", "/admin/platform/staff"),
    ("PATCH", "/admin/platform/staff/00000000-0000-4000-8000-000000000001/roles"),
    ("POST", f"/admin/content-quality/pieces/{PIECE}/sentences/suggest"),
    ("PATCH", f"/admin/content-quality/pieces/{PIECE}/signals/{VERSION}"),
    ("PATCH", f"/admin/content-quality/place-cards/{PIECE}"),
    ("POST", "/admin/content-quality/place-cards/build"),
    ("PUT", "/admin/notifications/read"),
    ("DELETE", "/admin/anything/1"),
    ("POST", f"/admin/content-quality/pieces/{PIECE}/sentences/apply/"),
    ("PATCH", f"/admin/content-quality/pieces/{PIECE}/sentences/apply"),
    ("POST", "/admin/waves/check"),
])
def test_middleware_refuses_every_other_write_before_the_route(issuer, directory, monkeypatch, capsys, method, path):
    _set_env(monkeypatch, "dev")
    _admin_e2e_row(directory)
    app, reached = _echo_app(directory)
    res = TestClient(app).request(method, path, headers={"Authorization": f"Bearer {_admin_e2e_token(issuer)}"})
    assert res.status_code == 403
    assert res.json() == {"detail": "e2e_read_only"}
    assert reached == []
    out = capsys.readouterr().out
    assert "staff_bearer_refused" in out and "e2e_read_only" in out


@pytest.mark.parametrize("path", [f"/admin/content-quality/pieces/{PIECE}/sentences/apply",
                                  f"/admin/content-quality/tour-versions/{VERSION}/sentences/apply"])
def test_middleware_lets_the_sentence_apply_routes_through(issuer, directory, monkeypatch, path):
    _set_env(monkeypatch, "dev")
    _admin_e2e_row(directory)
    app, reached = _echo_app(directory)
    res = TestClient(app).post(path, json={}, headers={"Authorization": f"Bearer {_admin_e2e_token(issuer)}"})
    assert res.status_code == 200, res.text
    assert res.json() == {"role": "admin", "e2e_read_only": True} and len(reached) == 1


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_middleware_leaves_other_admins_writes_alone(issuer, directory, monkeypatch, method):
    _set_env(monkeypatch, "dev")
    directory.add(role="admin", email="staff@adventure.asia", iss=ISS, sub=SUB)
    app, reached = _echo_app(directory)
    token = issuer.token(**{"cognito:groups": ["cis-admin"]})
    res = TestClient(app).request(method, "/admin/platform/staff", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200 and res.json()["e2e_read_only"] is False and reached


def test_middleware_answers_403_not_allowed_here_outside_dev(issuer, directory, monkeypatch):
    _set_env(monkeypatch, "prod")
    _admin_e2e_row(directory)
    app, reached = _echo_app(directory)
    res = TestClient(app).get("/admin/content-quality/pieces",
                              headers={"Authorization": f"Bearer {_admin_e2e_token(issuer)}"})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_not_allowed_here"}) and reached == []


# ── staff_authz (defence in depth) ───────────────────────────────────────────────────────────────

class _Identity:
    auth = "cognito"
    is_owner = False

    def __init__(self, e2e: bool) -> None:
        self.e2e_read_only = e2e


def test_principal_carries_the_flag():
    assert staff_authz.principal_from_role("admin", _Identity(True)).e2e_read_only is True
    assert staff_authz.principal_from_role("admin", _Identity(False)).e2e_read_only is False
    assert staff_authz.principal_from_role("admin", None).e2e_read_only is False


@pytest.mark.parametrize("method,route", [(m, r) for (m, r) in staff_authz.POLICY
                                          if m not in staff_authz.READ_METHODS
                                          and (m, r) not in e2e_staff.E2E_ALLOWED_WRITES])
def test_policy_refuses_every_other_write_route(method, route):
    principal = staff_authz.principal_from_role("admin", _Identity(True))
    assert staff_authz.denial_reason(principal, method, route) == "e2e_read_only"


@pytest.mark.parametrize("method,route", sorted(e2e_staff.E2E_ALLOWED_WRITES))
def test_policy_allows_the_sentence_apply_routes(method, route):
    principal = staff_authz.principal_from_role("admin", _Identity(True))
    assert staff_authz.denial_reason(principal, method, route) is None


def test_policy_reads_follow_the_admin_role():
    principal = staff_authz.principal_from_role("admin", _Identity(True))
    assert staff_authz.denial_reason(principal, "GET", "/admin/content-quality/pieces") is None
    assert staff_authz.denial_reason(principal, "GET", "/admin/platform/staff") is None   # staff.read (admin)


def test_policy_for_a_normal_admin_is_unchanged():
    principal = staff_authz.principal_from_role("admin", _Identity(False))
    assert staff_authz.denial_reason(principal, "POST", "/admin/content-quality/place-cards/build") is None


# ── Real app: the content-quality routes ─────────────────────────────────────────────────────────

class CQDirectory(Directory):
    """The staff directory for the middleware, plus a content-quality store with no piece / version (404)."""

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        if sql in (staff_identity.SQL_BY_IDENTITY, staff_identity.SQL_FIRST_LINK, staff_identity.SQL_BY_EMAIL):
            return await super().fetchrow(sql, *args)
        self.sql.append(sql)
        return None


@pytest.fixture
def real(monkeypatch, issuer):
    _set_env(monkeypatch, "dev")
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    directory = CQDirectory()
    _admin_e2e_row(directory)
    monkeypatch.setattr(real_app.state, "pool", directory, raising=False)
    return TestClient(real_app), directory


def _apply_body(mode: str, **extra) -> dict:
    return {"start": 0, "end": 5, "text": "Hello", "replacement": "" if mode == "cut" else "Hi there",
            "mode": mode, **extra}


@pytest.mark.parametrize("path,extra,missing", [
    (f"/admin/content-quality/pieces/{PIECE}/sentences/apply", {}, "Piece not found"),
    (f"/admin/content-quality/tour-versions/{VERSION}/sentences/apply", {"field": "overview"}, "Version not found"),
])
@pytest.mark.parametrize("mode", ["cut", "manual"])
def test_real_app_cut_and_manual_reach_the_handler(real, issuer, path, extra, missing, mode):
    client, directory = real
    res = client.post(path, json=_apply_body(mode, **extra),
                      headers={"Authorization": f"Bearer {_admin_e2e_token(issuer)}", "x-admin-role": "viewer"})
    # Past the bearer cap, staff_policy, the admin-role check and the mode check: the handler looked the object up.
    assert (res.status_code, res.json()["detail"]) == (404, missing), res.text


@pytest.mark.parametrize("path,extra", [
    (f"/admin/content-quality/pieces/{PIECE}/sentences/apply", {}),
    (f"/admin/content-quality/tour-versions/{VERSION}/sentences/apply", {"field": "overview"}),
])
def test_real_app_rewrite_mode_is_refused(real, issuer, path, extra):
    client, directory = real
    res = client.post(path, json=_apply_body("rewrite", **extra),
                      headers={"Authorization": f"Bearer {_admin_e2e_token(issuer)}"})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert not any("content_piece" in s or "tenant_tour_versions" in s for s in directory.sql)


@pytest.mark.parametrize("method,path,body", [
    ("POST", f"/admin/content-quality/pieces/{PIECE}/sentences/suggest",
     {"start": 0, "end": 5, "text": "Hello", "problem": "x"}),
    ("POST", f"/admin/content-quality/pieces/{PIECE}/label", {"label": "as_is"}),
    ("PATCH", f"/admin/content-quality/place-cards/{PIECE}", {"status": "approved"}),
    ("POST", "/admin/platform/staff", {"email": "new@adventure.asia", "display_name": "New", "role": "admin"}),
    ("POST", "/admin/waves/check", {}),
])
def test_real_app_other_writes_are_403_e2e_read_only(real, issuer, method, path, body):
    client, _ = real
    res = client.request(method, path, json=body, headers={"Authorization": f"Bearer {_admin_e2e_token(issuer)}"})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})


def test_real_app_verify_admin_returns_admin_for_the_bff(real, issuer):
    from api.main import get_pool

    client, directory = real
    real_app.dependency_overrides[get_pool] = lambda: directory
    try:
        res = client.post("/auth/verify-admin", json={"token": _admin_e2e_token(issuer)})
    finally:
        real_app.dependency_overrides.pop(get_pool, None)
    assert res.status_code == 200, res.text
    assert (res.json()["role"], res.json()["auth"]) == ("admin", "cognito")


# ── The other copies of the address ──────────────────────────────────────────────────────────────

def test_e2e_helper_and_migration_name_the_same_address():
    with open(os.path.join(ROOT, "tests", "e2e", "helpers", "mailbox.ts"), encoding="utf-8") as f:
        text = f.read()
    assert f"export const E2E_CIS_ADMIN_EMAIL = '{ADMIN_E2E}';" in text
    assert "export const MAILBOX_ADMIN_PREFIX = 'inbound-admin/';" in text
    (migration,) = glob.glob(os.path.join(ROOT, "api", "migrations", "*_admin_users_e2e_cis_admin_email.sql"))
    with open(migration, encoding="utf-8") as f:
        assert f"email = '{ADMIN_E2E}' AND role = 'admin' AND NOT is_owner" in f.read()


def test_no_workflow_sets_the_admin_evidence_switch():
    # The admin-evidence Playwright project is agent-only (local, MFA admin role); CI could not read the mailbox prefix
    # anyway. This keeps the intent explicit.
    for path in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.y*ml")):
        with open(path, encoding="utf-8") as f:
            assert not re.search(r"\bE2EADMIN\b", f.read()), path


def test_playwright_admin_evidence_project_is_opt_in_and_ignored_by_chromium():
    with open(os.path.join(ROOT, "playwright.config.ts"), encoding="utf-8") as f:
        cfg = f.read()
    assert "name: 'admin-evidence'" in cfg
    assert re.search(r"testIgnore:[^\n]*admin-evidence", cfg)
    with open(os.path.join(ROOT, "tests", "e2e", "admin-evidence", "admin-pages.spec.ts"), encoding="utf-8") as f:
        spec = f.read()
    assert "process.env.E2EADMIN !== '1'" in spec


def test_json_detail_values_are_stable():
    # The BFF and the agent's helper match on these strings.
    assert json.dumps({"detail": e2e_staff.E2E_READ_ONLY}) == '{"detail": "e2e_read_only"}'
