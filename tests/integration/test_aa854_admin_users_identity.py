"""AA-816 / AA-854 (Cognito Phase 1) — migration 247 (staff directory identity columns +
shared.staff_app_grant) against real Postgres.

Lesson S236 (test_aa791_migration_231): conftest applies migrations as a SUPERUSER in CI, which skips
every privilege check. On Dev, DB Migrate runs 247 as the RDS master, a plain role that owns schema shared,
shared.admin_users and shared.schema_versions. The module fixture undoes the conftest-applied 247, hands
those three objects to a NOSUPERUSER stand-in of the master, and applies the file as that role twice
(idempotent), errors NOT swallowed. Ownership goes back to the test user at teardown. A local test user
without CREATEROLE cannot make the stand-in; the file is then applied as the test user itself (a plain
role that owns the objects, which is what the stand-in models).

Then: the constraints the directory relies on (email domain + lower case + unique, the (iss, sub) pair),
the grant table's CHECKs / PK / FK, the unchanged reach (same grants and RLS flags as admin_users), and the
legacy login on a Cognito-only row (NULL password_hash) answering 401, not 500.
"""
import os
import sys
import uuid

import asyncpg
import httpx
import psycopg2
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

RUNNER = "zz854_rds_master"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "247_admin_users_identity.sql")
DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS"
LEGACY_ID = "00000000-0000-0000-0000-000000000854"
NEW_COLUMNS = {
    "email": ("text", True),
    "display_name": ("text", True),
    "cognito_iss": ("text", True),
    "cognito_sub": ("text", True),
    "last_login_at": ("timestamp with time zone", True),
    "is_owner": ("boolean", False),
}


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


def _undo_247(a):
    a.execute("DROP TABLE IF EXISTS shared.staff_app_grant")
    a.execute("ALTER TABLE shared.admin_users "
              "DROP CONSTRAINT IF EXISTS admin_users_email_chk, "
              "DROP CONSTRAINT IF EXISTS admin_users_cognito_pair_chk, "
              "DROP COLUMN IF EXISTS email, DROP COLUMN IF EXISTS display_name, "
              "DROP COLUMN IF EXISTS cognito_iss, DROP COLUMN IF EXISTS cognito_sub, "
              "DROP COLUMN IF EXISTS last_login_at, DROP COLUMN IF EXISTS is_owner, "
              "ALTER COLUMN password_hash SET NOT NULL")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '247'")


def _drop_runner(a):
    a.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNNER,))
    if a.fetchone():
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER; DROP OWNED BY {RUNNER}; DROP ROLE {RUNNER}")


def _make_runner(a):
    """The NOSUPERUSER master stand-in, or None when the test user may not create roles (local DB)."""
    try:
        a.execute(f"CREATE ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS INHERIT", (DB_PASS,))
    except psycopg2.errors.InsufficientPrivilege:
        return None
    # Dev: the master owns schema shared, shared.admin_users and the registry (schema reference, 16/10).
    a.execute(f"ALTER SCHEMA shared OWNER TO {RUNNER}")
    a.execute(f"ALTER TABLE shared.admin_users OWNER TO {RUNNER}")
    a.execute(f"ALTER TABLE shared.schema_versions OWNER TO {RUNNER}")
    return RUNNER


def _apply(sql, user):
    c = _connect(user)
    c.autocommit = True
    try:
        c.cursor().execute(sql)  # raises on any error
    finally:
        c.close()


@pytest.fixture(scope="module", autouse=True)
def rds_like_247():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    _drop_runner(a)
    _undo_247(a)
    # A row that exists before 247: it must come out untouched (no owner flag, no identity).
    a.execute("DELETE FROM shared.admin_users WHERE id = %s", (LEGACY_ID,))
    a.execute("INSERT INTO shared.admin_users (id, username, password_hash, role, is_active) "
              "VALUES (%s, 'aa854-legacy', '$2b$12$legacyhash', 'admin', true)", (LEGACY_ID,))
    runner = _make_runner(a) or DB_USER
    sql = open(MIGRATION, encoding="utf-8").read()
    _apply(sql, runner)
    _apply(sql, runner)               # idempotent re-run
    try:
        yield runner
    finally:
        a.execute("DELETE FROM shared.admin_users WHERE id = %s", (LEGACY_ID,))
        _drop_runner(a)               # schema shared, the tables and the registry back to the test user
        admin.close()


@pytest.fixture
def conn():
    c = _connect()
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _add_staff(cur, **cols):
    cols = {"id": str(uuid.uuid4()), "username": f"aa854-{uuid.uuid4().hex[:8]}", "role": "reviewer", **cols}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    cur.execute(f"INSERT INTO shared.admin_users ({names}) VALUES ({marks})", list(cols.values()))
    return cols["id"]


def _refused(conn, sqlstate, fn):
    """fn(cursor) must fail with `sqlstate`; the transaction goes on (savepoint)."""
    cur = conn.cursor()
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.Error) as err:
        fn(cur)
    cur.execute("ROLLBACK TO SAVEPOINT s")
    assert err.value.pgcode == sqlstate, err.value


# ── Registration and shape ───────────────────────────────────────────────────────────────────────

def test_247_registered_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '247'")
    assert cur.fetchone()[0] == 1


def test_new_columns_types_and_nullability(conn):
    cur = conn.cursor()
    cur.execute("SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull "
                "FROM pg_attribute a WHERE a.attrelid = 'shared.admin_users'::regclass "
                "AND a.attnum > 0 AND NOT a.attisdropped")
    cols = {name: (typ, nullable) for name, typ, nullable in cur.fetchall()}
    for name, want in NEW_COLUMNS.items():
        assert cols.get(name) == want, name
    assert cols["password_hash"][1] is True           # nullable now
    assert cols["role"][1] is False                   # stays NOT NULL (S241 Q3)
    cur.execute("SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef "
                "WHERE adrelid = 'shared.admin_users'::regclass "
                "AND adnum = (SELECT attnum FROM pg_attribute WHERE attrelid = 'shared.admin_users'::regclass "
                "AND attname = 'is_owner')")
    assert cur.fetchone()[0] == "false"


def test_existing_row_is_untouched(conn):
    cur = conn.cursor()
    cur.execute("SELECT password_hash, is_owner, email, display_name, cognito_iss, cognito_sub, last_login_at "
                "FROM shared.admin_users WHERE id = %s", (LEGACY_ID,))
    assert cur.fetchone() == ("$2b$12$legacyhash", False, None, None, None, None, None)


# ── admin_users constraints ──────────────────────────────────────────────────────────────────────

def test_cognito_only_row_without_password(conn):
    cur = conn.cursor()
    uid = _add_staff(cur, password_hash=None, email="nghieppham@adventure.asia", display_name="Nghiep Pham",
                     cognito_iss=ISS, cognito_sub=str(uuid.uuid4()))
    cur.execute("SELECT password_hash, is_owner FROM shared.admin_users WHERE id = %s", (uid,))
    assert cur.fetchone() == (None, False)


@pytest.mark.parametrize("email", [
    "x@example.com",                      # another domain
    "Nghiep@adventure.asia",              # not lower-cased
    "a@adventure.asia.evil.com",          # domain suffix trick
    "a@xadventure.asia",                  # look-alike domain
    "a@sub.adventure.asia",               # sub-domain
    "a b@adventure.asia",                 # whitespace
    "@adventure.asia",                    # empty local part
    "a@b@adventure.asia",                 # two @
])
def test_email_check_refuses(conn, email):
    _refused(conn, "23514", lambda cur: _add_staff(cur, password_hash="x", email=email))


def test_email_unique_case_insensitive(conn):
    cur = conn.cursor()
    _add_staff(cur, password_hash="x", email="same@adventure.asia")
    _refused(conn, "23505", lambda c: _add_staff(c, password_hash="x", email="same@adventure.asia"))
    # The index is on lower(email): even a row that bypassed the lower-case CHECK could not clash.
    cur.execute("SELECT pg_get_indexdef('shared.admin_users_email_lower_key'::regclass)")
    assert "lower(email)" in cur.fetchone()[0]


def test_cognito_pair_check(conn):
    sub = str(uuid.uuid4())
    _refused(conn, "23514", lambda c: _add_staff(c, password_hash="x", cognito_iss=ISS))
    _refused(conn, "23514", lambda c: _add_staff(c, password_hash="x", cognito_sub=sub))
    _refused(conn, "23514", lambda c: _add_staff(c, password_hash="x", cognito_iss="http://evil", cognito_sub=sub))
    _refused(conn, "23514", lambda c: _add_staff(c, password_hash="x", cognito_iss=ISS, cognito_sub=""))
    cur = conn.cursor()
    _add_staff(cur, password_hash="x", cognito_iss=ISS, cognito_sub=sub)


def test_cognito_identity_unique_on_the_pair(conn):
    cur = conn.cursor()
    sub = str(uuid.uuid4())
    _add_staff(cur, password_hash=None, cognito_iss=ISS, cognito_sub=sub)
    _refused(conn, "23505", lambda c: _add_staff(c, password_hash=None, cognito_iss=ISS, cognito_sub=sub))
    # The same sub from another issuer (a rebuilt or production pool) is another identity (ADR 0006 §9).
    _add_staff(cur, password_hash=None, cognito_iss=ISS + "-prod", cognito_sub=sub)


def test_many_rows_without_identity_or_email(conn):
    cur = conn.cursor()
    for _ in range(3):
        _add_staff(cur, password_hash="x")            # NULLs never clash in the unique indexes


# ── shared.staff_app_grant ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["admin", "sales", "contracting", "content_creator"])
def test_grant_accepts_every_booking_role(conn, role):
    cur = conn.cursor()
    uid = _add_staff(cur, password_hash="x")
    cur.execute("INSERT INTO shared.staff_app_grant (admin_user_id, app, role) VALUES (%s, 'booking', %s) "
                "RETURNING created_at IS NOT NULL AND updated_at IS NOT NULL", (uid, role))
    assert cur.fetchone()[0] is True


def test_grant_checks_pk_and_fk(conn):
    cur = conn.cursor()
    uid = _add_staff(cur, password_hash="x")
    ins = "INSERT INTO shared.staff_app_grant (admin_user_id, app, role) VALUES (%s, %s, %s)"
    _refused(conn, "23514", lambda c: c.execute(ins, (uid, "cis", "admin")))          # app outside the list
    _refused(conn, "23514", lambda c: c.execute(ins, (uid, "booking", "owner")))      # role outside the list
    _refused(conn, "23514", lambda c: c.execute(ins, (uid, "booking", "Admin")))      # case matters
    _refused(conn, "23503", lambda c: c.execute(ins, (str(uuid.uuid4()), "booking", "admin")))  # unknown user
    _refused(conn, "23502", lambda c: c.execute(ins, (uid, "booking", None)))
    cur.execute(ins, (uid, "booking", "sales"))
    _refused(conn, "23505", lambda c: c.execute(ins, (uid, "booking", "admin")))      # one role per app


def test_grant_goes_with_the_person(conn):
    cur = conn.cursor()
    uid = _add_staff(cur, password_hash="x")
    cur.execute("INSERT INTO shared.staff_app_grant (admin_user_id, app, role) VALUES (%s, 'booking', 'admin')",
                (uid,))
    cur.execute("DELETE FROM shared.admin_users WHERE id = %s", (uid,))
    cur.execute("SELECT count(*) FROM shared.staff_app_grant WHERE admin_user_id = %s", (uid,))
    assert cur.fetchone()[0] == 0


def test_grant_table_is_never_more_reachable_than_the_directory(conn, rds_like_247):
    """Dev: neither table has a non-owner grant (schema reference 16/10). This DB also has the harness's
    app_user grants on the directory (conftest GRANT … ON ALL TABLES IN SCHEMA shared, made before the
    fixture re-created the grant table), so the check is the guard's: a subset, not equality."""
    cur = conn.cursor()
    acl = ("SELECT pg_get_userbyid(x.grantee), x.privilege_type, x.is_grantable "
           "FROM pg_class c, LATERAL aclexplode(c.relacl) x "
           "WHERE c.oid = %s::regclass AND x.grantee <> c.relowner")
    cur.execute(acl, ("shared.admin_users",))
    directory = set(cur.fetchall())
    cur.execute(acl, ("shared.staff_app_grant",))
    assert set(cur.fetchall()) <= directory
    cur.execute("SELECT relname, relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid IN ('shared.admin_users'::regclass, 'shared.staff_app_grant'::regclass) ORDER BY 1")
    (_, au_rls, au_force, au_owner), (_, sag_rls, sag_force, sag_owner) = cur.fetchall()
    assert (sag_rls, sag_force) == (au_rls, au_force) == (False, False)
    assert sag_owner == au_owner == rds_like_247       # created by the runner, like the directory
    # The other apps' runtime roles and the tenant role cannot read the grant table.
    for role in ("aa_app_user", "booking", "tripplanner", "bk_owner", "tp_owner"):
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone():
            cur.execute("SELECT has_table_privilege(%s, 'shared.staff_app_grant', 'SELECT')", (role,))
            assert cur.fetchone()[0] is False, role


# ── The legacy login on a Cognito-only row (real DB, real app) ───────────────────────────────────

@pytest_asyncio.fixture
async def app_on_db():
    from api.main import app, get_pool
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    app.dependency_overrides[get_pool] = lambda: pool
    ids = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, pool, ids
    finally:
        app.dependency_overrides.pop(get_pool, None)
        await pool.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", ids)
        await pool.close()


@pytest.mark.asyncio
async def test_legacy_login_on_a_cognito_only_row_is_401(app_on_db):
    client, pool, ids = app_on_db
    uid = uuid.uuid4()
    username = f"aa854-cognito-{uid.hex[:8]}"
    await pool.execute(
        "INSERT INTO shared.admin_users "
        "(id, username, password_hash, role, is_active, email, cognito_iss, cognito_sub) "
        "VALUES ($1, $2, NULL, 'admin', true, $3, $4, $5)",
        uid, username, f"{username}@adventure.asia", ISS, str(uuid.uuid4()))
    ids.append(uid)
    res = await client.post("/auth/admin-login", json={"username": username, "password": "anything"})
    assert res.status_code == 401, res.text
    assert res.json()["detail"] == "Invalid username or password"


@pytest.mark.asyncio
async def test_staff_lookup_returns_a_cognito_only_row(app_on_db):
    """The verify-admin / booking-token lookup reads id, username, role only: a NULL hash does not matter."""
    from api.main import _load_active_staff
    _client, pool, ids = app_on_db
    uid = uuid.uuid4()
    await pool.execute(
        "INSERT INTO shared.admin_users (id, username, password_hash, role, is_active) "
        "VALUES ($1, $2, NULL, 'content', true)", uid, f"aa854-{uid.hex[:8]}")
    ids.append(uid)
    row = await _load_active_staff(pool, str(uid))
    assert row is not None and row["role"] == "content"
