"""AA-816 / AA-854 C3 — the Cognito staff lookup (api/core/staff_identity.py) against real Postgres.

shared.admin_users with migration 247 (email CHECK + unique lower(email), the (cognito_iss, cognito_sub) pair
CHECK + unique index). Proves the SQL, not the HTTP layer (unit tests cover that): the first sign-in binds the
identity to the active unbound row by email exactly once (a concurrent second bind matches no row), a row bound
to another identity or an inactive row is refused and never re-bound, last_login_at is written, the legacy
lookup returns the new directory columns, and resolve_staff maps groups to the role.
"""
import asyncio
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.core import staff_identity  # noqa: E402
from api.core.identity_verifier import Principal  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS"


def _principal(sub, email, groups=("cis-admin",)):
    return Principal(iss=ISS, sub=sub, client_id="3oqkumijh04cp81k3nlnub6qtv", groups=tuple(groups),
                     email=email, email_verified=None, username=sub)


@pytest_asyncio.fixture
async def db():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    ids = []
    staff_identity.clear_cache()

    async def add(role="admin", active=True, email=None, iss=None, sub=None):
        uid = uuid.uuid4()
        await p.execute(
            "INSERT INTO shared.admin_users (id, username, password_hash, role, is_active, email, display_name, "
            "cognito_iss, cognito_sub) VALUES ($1, $2, NULL, $3::shared.admin_role, $4, $5, 'Test Person', $6, $7)",
            uid, f"aa816c3-{uid.hex[:8]}", role, active, email, iss, sub,
        )
        ids.append(uid)
        return str(uid)

    async def get(uid):
        return await p.fetchrow("SELECT cognito_iss, cognito_sub, last_login_at FROM shared.admin_users "
                                "WHERE id = $1::uuid", uid)

    yield SimpleNamespace(pool=p, add=add, get=get)
    staff_identity.clear_cache()
    await p.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", ids)
    await p.close()


def _email():
    return f"aa816c3-{uuid.uuid4().hex[:10]}@adventure.asia"


@pytest.mark.asyncio
async def test_first_sign_in_binds_once_and_sets_last_login(db):
    email = _email()
    uid = await db.add(role="reviewer", email=email)
    sub = str(uuid.uuid4())
    identity = await staff_identity.resolve_staff(
        db.pool, _principal(sub, email.upper(), ["cis-reviewer"]), touch_login=True)
    assert (identity.admin_id, identity.role, identity.db_role) == (uid, "reviewer", "reviewer")
    assert identity.email == email and identity.display_name == "Test Person"
    row = await db.get(uid)
    assert (row["cognito_iss"], row["cognito_sub"]) == (ISS, sub)
    assert row["last_login_at"] is not None

    # Another Cognito identity with the same address (user deleted + recreated): never re-bound.
    staff_identity.clear_cache()
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.resolve_staff(db.pool, _principal(str(uuid.uuid4()), email))
    assert exc.value.status == 401
    assert (await db.get(uid))["cognito_sub"] == sub


@pytest.mark.asyncio
async def test_concurrent_first_sign_ins_bind_one_identity(db):
    email = _email()
    uid = await db.add(email=email)
    sub = str(uuid.uuid4())

    async def one():
        async with db.pool.acquire() as conn:
            return await staff_identity._load(conn, _principal(sub, email))

    rows = await asyncio.gather(one(), one(), one())
    assert {r.admin_id for r in rows} == {uid}
    assert (await db.get(uid))["cognito_sub"] == sub


@pytest.mark.asyncio
async def test_inactive_row_is_neither_bound_nor_returned(db):
    email = _email()
    uid = await db.add(email=email, active=False)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.resolve_staff(db.pool, _principal(str(uuid.uuid4()), email))
    assert exc.value.status == 401
    assert (await db.get(uid))["cognito_sub"] is None

    sub = str(uuid.uuid4())
    bound = await db.add(email=_email(), active=False, iss=ISS, sub=sub)
    with pytest.raises(staff_identity.StaffAuthError):
        await staff_identity.resolve_staff(db.pool, _principal(sub, None))
    assert (await db.get(bound))["last_login_at"] is None


@pytest.mark.asyncio
async def test_bound_row_is_found_by_iss_and_sub_and_group_decides_the_role(db):
    sub = str(uuid.uuid4())
    uid = await db.add(role="admin", email=_email(), iss=ISS, sub=sub)
    identity = await staff_identity.resolve_staff(db.pool, _principal(sub, None, ["cis-content", "booking-admin"]))
    assert (identity.admin_id, identity.role, identity.db_role) == (uid, "content", "admin")


@pytest.mark.asyncio
async def test_same_sub_from_another_issuer_is_another_identity(db):
    sub = str(uuid.uuid4())
    await db.add(email=_email(), iss=ISS, sub=sub)
    other = Principal(iss="https://cognito-idp.us-west-1.amazonaws.com/us-west-1_OTHERPOOL", sub=sub,
                      client_id="x", groups=("cis-admin",), email=None, email_verified=None, username=sub)
    with pytest.raises(staff_identity.StaffAuthError) as exc:
        await staff_identity.resolve_staff(db.pool, other)
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_legacy_lookup_returns_the_directory_columns(db):
    from api.main import _load_active_staff

    email = _email()
    uid = await db.add(role="content", email=email)
    row = await _load_active_staff(db.pool, uid)
    assert (row["email"], row["display_name"], row["role"]) == (email, "Test Person", "content")
