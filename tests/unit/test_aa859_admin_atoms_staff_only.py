"""AA-859 (S240) — /admin/atoms list, summary and PATCH are staff only.

The AA-431 tenant-JWT branch (`_resolve_atom_owner_scope`) is gone: 30 days of WAF logs showed no
Bearer caller of /admin/atoms*. Through a real FastAPI app, these check that:
  - a tenant Bearer JWT gets 403 on GET /admin/atoms, GET /admin/atoms/summary and PATCH
    /admin/atoms/{id}, never touches the DB pool, and logs one aa859_admin_atoms_tenant_refused;
  - a staff JWT or an invalid token as Bearer is refused the same way (a JWT never authorizes);
  - X-Admin-Secret is unchanged: 200, the same SQL as before with no owner_scope bind filter;
  - no credentials / a wrong secret / no secret configured keep verify_admin_secret's 403 / 503.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from api.routers import admin_atoms
from api.routers.auth import _create_admin_jwt, _create_jwt

ADMIN_SECRET = "aa859-j-test-admin-secret"
TENANT_ID = str(uuid.uuid4())
ATOM_ID = "atom_abc1234567"
REFUSED = "aa859_admin_atoms_tenant_refused"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    # verify_admin_secret reads admin.py's module global (captured at import), not the env.
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", ADMIN_SECRET)


def _conn():
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    conn.fetch.return_value = []
    conn.fetchrow.return_value = {"total": 0, "reviewed": 0}
    return conn


def _pool(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _client(conn=None):
    app = FastAPI()
    app.include_router(admin_atoms.router)
    app.state.pool = _pool(conn or _conn())
    return TestClient(app, raise_server_exceptions=False), app.state.pool


def _tenant_bearer(tenant_id=TENANT_ID):
    return {"Authorization": f"Bearer {_create_jwt(tenant_id, 'Tenant A', 'pro')}"}


# (method, path, json body) — the three routes that had the tenant branch.
CALLS = [
    pytest.param("GET", "/admin/atoms", None, id="GET /admin/atoms"),
    pytest.param("GET", "/admin/atoms/summary", None, id="GET /admin/atoms/summary"),
    pytest.param("PATCH", f"/admin/atoms/{ATOM_ID}", {"text": "edited"}, id="PATCH /admin/atoms/{id}"),
]


def _call(client, method, path, body, headers):
    return client.request(method, path, json=body, headers=headers)


def test_tenant_resolver_is_gone():
    assert not hasattr(admin_atoms, "_resolve_atom_owner_scope")


def test_every_admin_atoms_route_is_staff_gated():
    """Each of the five routes either carries _require_atoms_staff or checks the secret inline."""
    gated = {}
    for route in admin_atoms.router.routes:
        if not isinstance(route, APIRoute):
            continue
        deps = {d.call for d in route.dependant.dependencies}
        params = set(route.dependant.query_params + route.dependant.header_params)
        gated[(tuple(sorted(route.methods)), route.path)] = (
            admin_atoms._require_atoms_staff in deps
            or any(p.name == "x_admin_secret" for p in params)
        )
    assert len(gated) == 5
    assert all(gated.values()), gated


@pytest.mark.parametrize("method,path,body", CALLS)
def test_tenant_bearer_gets_403_and_never_reaches_the_db(method, path, body):
    client, pool = _client()
    with capture_logs() as logs:
        resp = _call(client, method, path, body, _tenant_bearer())
    assert resp.status_code == 403
    assert "tenant token" in resp.json()["detail"]
    pool.acquire.assert_not_called()
    refused = [entry for entry in logs if entry["event"] == REFUSED]
    assert len(refused) == 1
    assert refused[0]["tenant_id"] == TENANT_ID
    assert refused[0]["tenant_token_valid"] is True
    assert refused[0]["method"] == method
    assert refused[0]["path"] == path


@pytest.mark.parametrize("method,path,body", CALLS)
def test_tenant_bearer_with_wrong_secret_still_403(method, path, body):
    client, pool = _client()
    headers = {**_tenant_bearer(), "x-admin-secret": "wrong"}
    with capture_logs() as logs:
        resp = _call(client, method, path, body, headers)
    assert resp.status_code == 403
    pool.acquire.assert_not_called()
    assert [entry["event"] for entry in logs].count(REFUSED) == 1


@pytest.mark.parametrize("token_kind", ["staff_jwt", "garbage"])
def test_non_tenant_bearer_is_refused_too(token_kind):
    """A staff JWT (shares JWT_SECRET) or an invalid token never authorizes: 403, logged with no
    tenant id (the old branch gave 401 for an invalid token and 403 for a staff one)."""
    token = (_create_admin_jwt(str(uuid.uuid4()), "staff", "admin") if token_kind == "staff_jwt"
             else "not-a-real-jwt")
    client, pool = _client()
    with capture_logs() as logs:
        resp = client.get("/admin/atoms", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    pool.acquire.assert_not_called()
    refused = [entry for entry in logs if entry["event"] == REFUSED]
    assert len(refused) == 1
    assert refused[0]["tenant_id"] is None and refused[0]["tenant_token_valid"] is False


@pytest.mark.parametrize("method,path,body", CALLS)
def test_admin_secret_unchanged(method, path, body):
    conn = _conn()
    if method == "PATCH":
        conn.fetchrow.return_value = {
            "atom_id": ATOM_ID, "tour_id": uuid.uuid4(), "text": "edited", "deleted": False,
            "visual_potential": 2, "media": "{}", "created_at": None, "updated_at": None,
        }
    client, pool = _client(conn)
    with capture_logs() as logs:
        resp = _call(client, method, path, body, {"x-admin-secret": ADMIN_SECRET})
    assert resp.status_code == 200, resp.text
    pool.acquire.assert_called_once()
    assert REFUSED not in [entry["event"] for entry in logs]
    sql_calls = conn.fetch.call_args_list + conn.fetchrow.call_args_list + conn.fetchval.call_args_list
    assert sql_calls
    for call in sql_calls:
        assert "owner_scope = $" not in call.args[0]


def test_admin_secret_wins_over_a_bearer_header():
    """A valid admin secret is staff, whatever else the request carries (none seen in the WAF window)."""
    client, pool = _client()
    with capture_logs() as logs:
        resp = client.get("/admin/atoms/summary",
                          headers={**_tenant_bearer(), "x-admin-secret": ADMIN_SECRET})
    assert resp.status_code == 200
    pool.acquire.assert_called_once()
    assert REFUSED not in [entry["event"] for entry in logs]


@pytest.mark.parametrize("method,path,body", CALLS)
def test_no_credentials_or_wrong_secret_403_without_refusal_log(method, path, body):
    client, pool = _client()
    with capture_logs() as logs:
        none = _call(client, method, path, body, {})
        wrong = _call(client, method, path, body, {"x-admin-secret": "wrong"})
    assert none.status_code == 403 and wrong.status_code == 403
    assert none.json()["detail"] == "Invalid admin secret"
    pool.acquire.assert_not_called()
    assert REFUSED not in [entry["event"] for entry in logs]


def test_admin_secret_not_configured_is_503_but_a_tenant_still_gets_403(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", "")
    client, _pool_ = _client()
    assert client.get("/admin/atoms", headers={"x-admin-secret": "x"}).status_code == 503
    assert client.get("/admin/atoms", headers=_tenant_bearer()).status_code == 403


def test_owner_scope_class_filter_still_works_for_staff():
    conn = _conn()
    client, _pool_ = _client(conn)
    resp = client.get("/admin/atoms?owner_scope_class=legacy", headers={"x-admin-secret": ADMIN_SECRET})
    assert resp.status_code == 200
    assert "ta.owner_scope != 'platform'" in conn.fetch.call_args.args[0]
