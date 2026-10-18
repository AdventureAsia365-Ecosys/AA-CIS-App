"""AA-680 — the staff lookup behind POST /auth/booking-token runs against real Postgres.

shared.admin_users comes from migration 074 (+ 218 for the 'content' role, + 247 for the Cognito identity
columns and a nullable password_hash). The lookup must bind a uuid parameter, cast the role enum to text,
return nothing for a deactivated user, and return a Cognito-only row (NULL password_hash) like any other.
"""
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def db():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    ids = []

    async def add(role, active, password_hash="x"):
        uid = uuid.uuid4()
        await p.execute(
            "INSERT INTO shared.admin_users (id, username, password_hash, role, is_active) "
            "VALUES ($1, $2, $3, $4::shared.admin_role, $5)",
            uid, f"aa680-{uid.hex[:8]}", password_hash, role, active,
        )
        ids.append(uid)
        return str(uid)

    yield SimpleNamespace(pool=p, add_staff=add)
    await p.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", ids)
    await p.close()


@pytest.mark.asyncio
async def test_active_staff_row_is_returned(db):
    from api.main import _load_active_staff
    uid = await db.add_staff("content", True)
    row = await _load_active_staff(db.pool, uid)
    assert row is not None
    assert row["id"] == uid
    assert row["role"] == "content"
    assert row["username"].startswith("aa680-")


@pytest.mark.asyncio
async def test_inactive_staff_returns_none(db):
    from api.main import _load_active_staff
    uid = await db.add_staff("admin", False)
    assert await _load_active_staff(db.pool, uid) is None


@pytest.mark.asyncio
async def test_unknown_or_malformed_id_returns_none(db):
    from api.main import _load_active_staff
    assert await _load_active_staff(db.pool, str(uuid.uuid4())) is None
    assert await _load_active_staff(db.pool, "not-a-uuid") is None
    assert await _load_active_staff(db.pool, None) is None


@pytest.mark.asyncio
async def test_cognito_only_staff_row_is_returned(db):
    """AA-816 (247): a staff member created through the Cognito directory has no password_hash."""
    from api.main import _load_active_staff
    uid = await db.add_staff("reviewer", True, password_hash=None)
    row = await _load_active_staff(db.pool, uid)
    assert row is not None
    assert row["role"] == "reviewer"
