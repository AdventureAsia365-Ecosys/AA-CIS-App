"""AA-816 — the legacy password login fails closed on a Cognito-only staff row.

Migration 247 makes shared.admin_users.password_hash nullable: a staff member created through the
Cognito directory has no password. Before 247, _verify_password called password_hash.encode() and caught
only ValueError / TypeError, so a NULL hash raised AttributeError and /auth/admin-login answered 500.
It must answer the same 401 as a wrong password, after a real (dummy) bcrypt compare.
"""
from contextlib import asynccontextmanager

import bcrypt
import pytest
from fastapi.testclient import TestClient

from api.main import app, get_pool
from api.routers import auth as auth_mod
from api.routers.auth import _DUMMY_BCRYPT_HASH, _verify_password

ADMIN_ID = "00000000-0000-0000-0000-000000000816"
PASSWORD = "correct horse battery staple"


class _Pool:
    """shared.admin_users as /auth/admin-login reads it (by username)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetchrow(self, sql, username):
        assert "FROM shared.admin_users WHERE username = $1" in sql
        return self.rows.get(username)


@pytest.fixture
def pool():
    fake = _Pool()
    app.dependency_overrides[get_pool] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def client(pool):
    return TestClient(app, raise_server_exceptions=False)


def _row(username, password_hash, active=True):
    return {"id": ADMIN_ID, "username": username, "password_hash": password_hash,
            "role": "admin", "is_active": active}


def _login(client, username, password=PASSWORD):
    return client.post("/auth/admin-login", json={"username": username, "password": password})


def test_dummy_hash_is_a_real_bcrypt_hash_that_never_matches():
    # A malformed dummy raises "Invalid salt" at once, which skips the compare and leaks timing.
    assert bcrypt.checkpw(b"anything", _DUMMY_BCRYPT_HASH) is False
    assert _DUMMY_BCRYPT_HASH.startswith(b"$2b$12$")


@pytest.mark.parametrize("stored", [None, ""])
def test_verify_password_fails_closed_without_a_hash(stored, monkeypatch):
    seen = []
    real = auth_mod.bcrypt.checkpw
    monkeypatch.setattr(auth_mod.bcrypt, "checkpw", lambda pw, h: seen.append(h) or real(pw, h))
    assert _verify_password(PASSWORD, stored) is False
    assert seen == [_DUMMY_BCRYPT_HASH]          # the compare still ran (constant timing)


def test_verify_password_still_accepts_a_real_hash():
    h = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
    assert _verify_password(PASSWORD, h) is True
    assert _verify_password("wrong", h) is False
    assert _verify_password(PASSWORD, "not-a-bcrypt-hash") is False


def test_login_with_null_hash_is_401_not_500(client, pool):
    pool.rows["cognito-only"] = _row("cognito-only", None)
    res = _login(client, "cognito-only")
    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Invalid username or password"


def test_null_hash_401_looks_like_a_wrong_password_and_an_unknown_user(client, pool):
    pool.rows["has-pw"] = _row("has-pw", bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode())
    pool.rows["cognito-only"] = _row("cognito-only", None)
    wrong = _login(client, "has-pw", "wrong")
    null = _login(client, "cognito-only")
    unknown = _login(client, "nobody")
    assert wrong.status_code == null.status_code == unknown.status_code == 401
    assert wrong.json() == null.json() == unknown.json()


def test_login_with_a_real_hash_still_issues_a_token(client, pool):
    pool.rows["has-pw"] = _row("has-pw", bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode())
    res = _login(client, "has-pw")
    assert res.status_code == 200, res.text
    assert res.json()["admin_id"] == ADMIN_ID
    assert res.json()["token"]
