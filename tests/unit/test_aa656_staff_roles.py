"""AA-656 — every staff role gets a verifiable JWT.

/auth/verify-admin is what the Next.js middleware and the BFF proxies call to learn the caller's
role. It must accept the three staff roles (admin, reviewer, content) and keep rejecting tenant
tokens (same secret/alg, so a tenant JWT decodes fine and only the role whitelist stops it).
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers.auth import JWT_ALG, JWT_SECRET, STAFF_ROLES, _create_admin_jwt

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.mark.parametrize("role", ["admin", "reviewer", "content"])
def test_verify_admin_accepts_every_staff_role(client, role):
    token = _create_admin_jwt("00000000-0000-0000-0000-000000000001", f"u-{role}", role)
    res = client.post("/auth/verify-admin", json={"token": token})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == role


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
