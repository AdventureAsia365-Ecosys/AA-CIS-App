"""AA-656 — every staff role gets a verifiable JWT.

/auth/verify-admin is what the Next.js middleware and the BFF proxies call to learn the caller's
role. It must accept the three staff roles (admin, reviewer, content) and keep rejecting tenant
tokens (same secret/alg, so a tenant JWT decodes fine and only the role whitelist stops it).
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

import api.main as main_mod
from api.main import app, get_pool
from api.routers.auth import JWT_ALG, JWT_SECRET, STAFF_ROLES, _create_admin_jwt

REPO = Path(__file__).resolve().parents[2]
ADMIN_ID = "00000000-0000-0000-0000-000000000001"


class _Pool:
    """shared.admin_users as /auth/verify-admin reads it: id -> (username, role, is_active)."""

    def __init__(self):
        self.users: dict[str, tuple[str, str, bool]] = {}
        self.reads = 0

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetchrow(self, sql, uid):
        self.reads += 1
        user = self.users.get(str(uid))
        if not user or not user[2]:
            return None
        return {"id": str(uid), "username": user[0], "role": user[1]}


@pytest.fixture
def pool():
    fake = _Pool()
    main_mod._staff_state_cache.clear()
    app.dependency_overrides[get_pool] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_pool, None)
    main_mod._staff_state_cache.clear()


@pytest.fixture
def client(pool):
    return TestClient(app)


@pytest.mark.parametrize("role", ["admin", "reviewer", "content"])
def test_verify_admin_accepts_every_staff_role(client, pool, role):
    pool.users[ADMIN_ID] = (f"u-{role}", role, True)
    token = _create_admin_jwt(ADMIN_ID, f"u-{role}", role)
    res = client.post("/auth/verify-admin", json={"token": token})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == role


def test_verify_admin_refuses_a_deactivated_user(client, pool):
    """AA-882 review: a deactivated account stops at the next verify, not at the 24 h token expiry."""
    pool.users[ADMIN_ID] = ("u-admin", "admin", False)
    token = _create_admin_jwt(ADMIN_ID, "u-admin", "admin")
    res = client.post("/auth/verify-admin", json={"token": token})
    assert res.status_code == 401
    assert res.json()["detail"] == "Staff user is inactive or unknown"


def test_verify_admin_refuses_an_unknown_user(client, pool):
    token = _create_admin_jwt(ADMIN_ID, "gone", "admin")
    assert client.post("/auth/verify-admin", json={"token": token}).status_code == 401


def test_verify_admin_returns_the_db_role_not_the_jwt_claim(client, pool):
    """An admin demoted to reviewer keeps an admin JWT for 24 h; the BFF forwards the DB role."""
    pool.users[ADMIN_ID] = ("u-admin", "reviewer", True)
    token = _create_admin_jwt(ADMIN_ID, "u-admin", "admin")
    res = client.post("/auth/verify-admin", json={"token": token})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "reviewer"


def test_verify_admin_caches_the_staff_row_briefly(client, pool, monkeypatch):
    pool.users[ADMIN_ID] = ("u-admin", "admin", True)
    token = _create_admin_jwt(ADMIN_ID, "u-admin", "admin")
    for _ in range(3):
        assert client.post("/auth/verify-admin", json={"token": token}).status_code == 200
    assert pool.reads == 1
    # Past the TTL the row is read again and a deactivation takes effect.
    pool.users[ADMIN_ID] = ("u-admin", "admin", False)
    clock = main_mod.time.monotonic() + main_mod._STAFF_STATE_TTL_S + 1
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: clock)
    assert client.post("/auth/verify-admin", json={"token": token}).status_code == 401
    assert pool.reads == 2


def test_verify_admin_rejects_tenant_token(client):
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"sub": "t1", "tenant_id": "t1", "role": "tenant", "iat": now, "exp": now + timedelta(hours=1)},
        JWT_SECRET, algorithm=JWT_ALG,
    )
    res = client.post("/auth/verify-admin", json={"token": token})
    assert res.status_code == 401


def test_verify_admin_rejects_unknown_role(client):
    token = _create_admin_jwt("00000000-0000-0000-0000-000000000001", "x", "superuser")
    assert client.post("/auth/verify-admin", json={"token": token}).status_code == 401


def test_staff_roles_match_enum_and_frontend_matrix():
    # The backend tuple, the DB enum (migration 074 + 218) and frontend/lib/rbac.ts must agree.
    assert STAFF_ROLES == ("admin", "reviewer", "content")
    mig = (REPO / "api/migrations/218_admin_role_content.sql").read_text()
    assert "ADD VALUE IF NOT EXISTS 'content'" in mig
    rbac = (REPO / "frontend/lib/rbac.ts").read_text()
    assert 'STAFF_ROLES: readonly StaffRole[] = ["admin", "reviewer", "content"]' in rbac


def test_content_password_login_is_gone():
    route = (REPO / "frontend/app/api/auth/login/route.ts").read_text()
    assert "process.env.CONTENT_PASSWORD" not in route
