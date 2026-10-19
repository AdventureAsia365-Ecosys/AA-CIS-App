"""AA-816 (S243, option B) — migration 250 (TEMPORARY E2E test-mailbox exception in admin_users_email_chk)
against real Postgres.

The SQL runs for real (S231 lesson). The module fixture re-applies the file twice (idempotent), errors NOT
swallowed, as a NOSUPERUSER stand-in of the RDS master that owns shared.admin_users (Dev: DB Migrate runs as the
master today, as cis_owner after X1 245); a local test user without CREATEROLE applies it as itself. Re-applying
here also makes the module independent of test_aa854_admin_users_identity, which undoes and re-applies 247 (and so
leaves the CHECK without the exception).

Then: the exact address is accepted only as a reviewer that is not an owner and has no password; every other
non-company address is still refused; the 247 rule still holds for company addresses; the constraint is commented
as temporary; guard 0 refuses a runner that does not own the table; the removal SQL in the header restores 247.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

RUNNER = "zz250_rds_master"
OTHER = "zz250_not_owner"
E2E = "e2e@e2e-mail.lumiguides.it.com"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "250_admin_users_e2e_test_email.sql")
RESTORE_247 = (
    "ALTER TABLE shared.admin_users DROP CONSTRAINT IF EXISTS admin_users_email_chk, "
    "ADD CONSTRAINT admin_users_email_chk CHECK ("
    "email IS NULL OR (email = lower(email) AND email ~ '^[^@[:space:]]+@adventure[.]asia$'))"
)


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


def _sql() -> str:
    with open(MIGRATION, encoding="utf-8") as f:
        return f.read()


def _apply(sql, user=DB_USER):
    c = _connect(user)
    c.autocommit = True   # the file brings its own BEGIN … COMMIT
    try:
        c.cursor().execute(sql)   # raises on any error
    finally:
        c.close()


def _drop_role(cur, name):
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    if cur.fetchone():
        cur.execute(f"REASSIGN OWNED BY {name} TO CURRENT_USER; DROP OWNED BY {name}; DROP ROLE {name}")


def _owners(cur):
    cur.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'shared.admin_users'::regclass")
    table_owner = cur.fetchone()[0]
    cur.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'shared.schema_versions'::regclass")
    return table_owner, cur.fetchone()[0]


@pytest.fixture(scope="module", autouse=True)
def applied_250():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    _drop_role(a, RUNNER)
    a.execute("DELETE FROM shared.admin_users WHERE email = %s", (E2E,))
    # Start from the 247 rule (whatever ran before), so the file really replaces it.
    a.execute(RESTORE_247)
    a.execute("DELETE FROM shared.schema_versions WHERE version = '250'")
    table_owner, reg_owner = _owners(a)
    runner = None
    try:
        a.execute(f"CREATE ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS INHERIT", (DB_PASS,))
        runner = RUNNER
    except psycopg2.errors.InsufficientPrivilege:
        runner = None
    try:
        if runner:
            a.execute(f"GRANT USAGE ON SCHEMA shared TO {runner}")
            a.execute(f"ALTER TABLE shared.admin_users OWNER TO {runner}")
            a.execute(f"ALTER TABLE shared.schema_versions OWNER TO {runner}")
        _apply(_sql(), runner or DB_USER)
        _apply(_sql(), runner or DB_USER)   # idempotent re-run
        yield runner
    finally:
        if runner:
            a.execute(f'ALTER TABLE shared.admin_users OWNER TO "{table_owner}"')
            a.execute(f'ALTER TABLE shared.schema_versions OWNER TO "{reg_owner}"')
            a.execute(f"REVOKE USAGE ON SCHEMA shared FROM {runner}")
            _drop_role(a, runner)
        a.execute("DELETE FROM shared.admin_users WHERE email = %s", (E2E,))
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


def _add(cur, **cols):
    cols = {"id": str(uuid.uuid4()), "username": f"aa250-{uuid.uuid4().hex[:8]}", "role": "reviewer",
            "password_hash": None, **cols}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    cur.execute(f"INSERT INTO shared.admin_users ({names}) VALUES ({marks})", list(cols.values()))
    return cols["id"]


def _refused(conn, fn, sqlstate="23514"):
    cur = conn.cursor()
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.Error) as err:
        fn(cur)
    cur.execute("ROLLBACK TO SAVEPOINT s")
    assert err.value.pgcode == sqlstate, err.value


def test_250_registered_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*), max(description) FROM shared.schema_versions WHERE version = '250'")
    count, description = cur.fetchone()
    assert count == 1 and "E2E" in description and "temporary" in description


def test_the_e2e_reviewer_row_is_accepted(conn):
    cur = conn.cursor()
    uid = _add(cur, email=E2E, display_name="E2E Cognito", is_owner=False)
    cur.execute("SELECT email, role::text, is_owner, password_hash FROM shared.admin_users WHERE id = %s", (uid,))
    assert cur.fetchone() == (E2E, "reviewer", False, None)


@pytest.mark.parametrize("cols", [
    {"role": "admin"},
    {"role": "content"},
    {"role": "viewer"},
    {"is_owner": True},
    {"password_hash": "$2b$04$notarealhash"},
    {"email": E2E.upper()},
])
def test_the_e2e_address_with_a_wider_scope_is_refused(conn, cols):
    _refused(conn, lambda cur: _add(cur, **{"email": E2E, **cols}))


def test_the_e2e_row_can_not_be_promoted_later(conn):
    cur = conn.cursor()
    uid = _add(cur, email=E2E)
    _refused(conn, lambda c: c.execute("UPDATE shared.admin_users SET role = 'admin' WHERE id = %s", (uid,)))
    _refused(conn, lambda c: c.execute("UPDATE shared.admin_users SET is_owner = true WHERE id = %s", (uid,)))


@pytest.mark.parametrize("email", [
    "e2e2@e2e-mail.lumiguides.it.com",
    "x.e2e@e2e-mail.lumiguides.it.com",
    "e2e@lumiguides.it.com",
    "e2e@e2e-mail.lumiguides.it.com.evil.com",
    "e2e@x.e2e-mail.lumiguides.it.com",
    " e2e@e2e-mail.lumiguides.it.com",
    "x@example.com",
    "a@adventure.asia.evil.com",
    "a@sub.adventure.asia",
    "Nghiep@adventure.asia",
])
def test_every_other_non_company_address_is_still_refused(conn, email):
    _refused(conn, lambda cur: _add(cur, email=email))


def test_company_addresses_keep_every_role_and_flag(conn):
    cur = conn.cursor()
    _add(cur, email=f"a250-{uuid.uuid4().hex[:6]}@adventure.asia", role="admin", is_owner=True)
    _add(cur, email=f"b250-{uuid.uuid4().hex[:6]}@adventure.asia", role="content", password_hash="x")
    _add(cur, email=None, role="viewer", password_hash="x")


def test_the_constraint_is_commented_as_temporary(conn):
    cur = conn.cursor()
    cur.execute("SELECT obj_description(oid, 'pg_constraint'), convalidated FROM pg_constraint "
                "WHERE conrelid = 'shared.admin_users'::regclass AND conname = 'admin_users_email_chk'")
    comment, validated = cur.fetchone()
    assert validated and "TEMPORARY" in comment and E2E in comment


def test_guard_refuses_a_runner_that_does_not_own_the_table():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        try:
            a.execute(f"CREATE ROLE {OTHER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS INHERIT", (DB_PASS,))
        except psycopg2.errors.InsufficientPrivilege:
            pytest.skip("the test user may not create roles (local DB)")
        a.execute(f"GRANT USAGE ON SCHEMA shared TO {OTHER}")
        with pytest.raises(psycopg2.Error) as exc:
            _apply(_sql(), OTHER)
        assert "migration 250" in str(exc.value) and "ALTER TABLE needs the owner" in str(exc.value)
    finally:
        a.execute(f"REVOKE USAGE ON SCHEMA shared FROM {OTHER}")
        _drop_role(a, OTHER)
        admin.close()


def test_the_removal_sql_restores_247_and_refuses_while_the_row_exists(conn):
    """The header's removal step: with the E2E row still there the 247 CHECK can not come back (so the cleanup
    must delete the row first); without it, it can, and the address is refused again."""
    cur = conn.cursor()
    uid = _add(cur, email=E2E)
    _refused(conn, lambda c: c.execute(RESTORE_247))
    cur.execute("DELETE FROM shared.admin_users WHERE id = %s", (uid,))
    cur.execute(RESTORE_247)
    _refused(conn, lambda c: _add(c, email=E2E))   # rolled back with the test transaction
