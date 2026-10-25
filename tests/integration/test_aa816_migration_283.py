"""AA-816 (S252, A10b) — migration 283 (TEMPORARY Dev-only E2E CIS admin exception in admin_users_email_chk)
against real Postgres.

The SQL runs for real (S231 lesson). The module fixture starts from the 247 rule (whatever ran before), applies 250
then 283, and re-applies 283 (idempotent), errors NOT swallowed, as a NOSUPERUSER stand-in of the runner that owns
shared.admin_users (Dev: DB Migrate runs cis/ files as cis_owner after X1 245); a local test user without CREATEROLE
applies it as itself.

Then: the A10b address is accepted only as an admin that is not an owner and has no password; the A10 reviewer row
keeps exactly its 250 scope; every look-alike is refused; the 247 rule still holds for company addresses; the
constraint is commented as temporary for both addresses; guard 0 refuses a runner that does not own the table;
re-applying 250 after 283 (either order) leaves a working CHECK; the removal SQL restores 247 and refuses while a row
uses either address.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

RUNNER = "zz283_rds_master"
OTHER = "zz283_not_owner"
E2E = "e2e@e2e-mail.lumiguides.it.com"
ADMIN = "e2e-cis-admin@e2e-mail.lumiguides.it.com"
_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
MIGRATION = os.path.join(_MIG, "283_admin_users_e2e_cis_admin_email.sql")
MIGRATION_250 = os.path.join(_MIG, "250_admin_users_e2e_test_email.sql")
RESTORE_247 = (
    "ALTER TABLE shared.admin_users DROP CONSTRAINT IF EXISTS admin_users_email_chk, "
    "ADD CONSTRAINT admin_users_email_chk CHECK ("
    "email IS NULL OR (email = lower(email) AND email ~ '^[^@[:space:]]+@adventure[.]asia$'))"
)


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


def _read(path) -> str:
    with open(path, encoding="utf-8") as f:
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
def applied_283():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    _drop_role(a, RUNNER)
    a.execute("DELETE FROM shared.admin_users WHERE email IN (%s, %s)", (E2E, ADMIN))
    # Start from the 247 rule (whatever ran before), so 250 and then 283 really replace it.
    a.execute(RESTORE_247)
    a.execute("DELETE FROM shared.schema_versions WHERE version IN ('250', '283')")
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
        _apply(_read(MIGRATION_250), runner or DB_USER)
        _apply(_read(MIGRATION), runner or DB_USER)
        _apply(_read(MIGRATION), runner or DB_USER)   # idempotent re-run
        yield runner
    finally:
        if runner:
            a.execute(f'ALTER TABLE shared.admin_users OWNER TO "{table_owner}"')
            a.execute(f'ALTER TABLE shared.schema_versions OWNER TO "{reg_owner}"')
            a.execute(f"REVOKE USAGE ON SCHEMA shared FROM {runner}")
            _drop_role(a, runner)
        a.execute("DELETE FROM shared.admin_users WHERE email IN (%s, %s)", (E2E, ADMIN))
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
    cols = {"id": str(uuid.uuid4()), "username": f"aa283-{uuid.uuid4().hex[:8]}", "role": "admin",
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


def _constraint_def(cur) -> str:
    cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'shared.admin_users'::regclass AND conname = 'admin_users_email_chk'")
    return cur.fetchone()[0]


def test_283_registered_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*), max(description) FROM shared.schema_versions WHERE version = '283'")
    count, description = cur.fetchone()
    assert count == 1 and "A10b" in description and "temporary" in description


def test_the_e2e_cis_admin_row_is_accepted(conn):
    cur = conn.cursor()
    uid = _add(cur, email=ADMIN, display_name="E2E CIS admin", is_owner=False)
    cur.execute("SELECT email, role::text, is_owner, password_hash FROM shared.admin_users WHERE id = %s", (uid,))
    assert cur.fetchone() == (ADMIN, "admin", False, None)


@pytest.mark.parametrize("cols", [
    {"role": "reviewer"},
    {"role": "content"},
    {"role": "viewer"},
    {"is_owner": True},
    {"password_hash": "$2b$04$notarealhash"},
    {"email": ADMIN.upper()},
])
def test_the_admin_address_with_another_scope_is_refused(conn, cols):
    _refused(conn, lambda cur: _add(cur, **{"email": ADMIN, **cols}))


def test_the_admin_row_can_not_become_an_owner_or_get_a_password_later(conn):
    cur = conn.cursor()
    uid = _add(cur, email=ADMIN)
    _refused(conn, lambda c: c.execute("UPDATE shared.admin_users SET is_owner = true WHERE id = %s", (uid,)))
    _refused(conn, lambda c: c.execute("UPDATE shared.admin_users SET password_hash = 'x' WHERE id = %s", (uid,)))
    _refused(conn, lambda c: c.execute("UPDATE shared.admin_users SET role = 'reviewer' WHERE id = %s", (uid,)))


def test_the_a10_reviewer_keeps_exactly_its_250_scope(conn):
    cur = conn.cursor()
    _add(cur, email=E2E, role="reviewer")
    for cols in ({"role": "admin"}, {"role": "content"}, {"role": "viewer"}, {"role": "reviewer", "is_owner": True},
                 {"role": "reviewer", "password_hash": "x"}):
        _refused(conn, lambda c, cols=cols: _add(c, **{"email": E2E, **cols}))


@pytest.mark.parametrize("email", [
    "e2e-cis-admin2@e2e-mail.lumiguides.it.com",
    "e2e-cis-admin+x@e2e-mail.lumiguides.it.com",
    "e2e-admin@e2e-mail.lumiguides.it.com",
    "x.e2e-cis-admin@e2e-mail.lumiguides.it.com",
    "e2e-cis-admin@lumiguides.it.com",
    "e2e-cis-admin@x.e2e-mail.lumiguides.it.com",
    "e2e-cis-admin@e2e-mail.lumiguides.it.com.evil.com",
    " e2e-cis-admin@e2e-mail.lumiguides.it.com",
    "e2e-cis-admin@e2e-mail.lumiguides.it.com ",
    "e2e-cis-Kadmin@e2e-mail.lumiguides.it.com",      # Kelvin sign look-alike
    "e2e-cis-admin@e2e-mail.lumiguides.it.com​",      # zero-width space
    "e2e-cis-admin@adventure.asia.evil.com",
    "admin@example.com",
])
def test_every_look_alike_is_refused(conn, email):
    _refused(conn, lambda cur: _add(cur, email=email))


def test_company_addresses_keep_every_role_and_flag(conn):
    cur = conn.cursor()
    _add(cur, email=f"a283-{uuid.uuid4().hex[:6]}@adventure.asia", role="admin", is_owner=True)
    _add(cur, email=f"b283-{uuid.uuid4().hex[:6]}@adventure.asia", role="content", password_hash="x")
    _add(cur, email=None, role="viewer", password_hash="x")


def test_the_constraint_is_commented_as_temporary_for_both(conn):
    cur = conn.cursor()
    cur.execute("SELECT obj_description(oid, 'pg_constraint'), convalidated FROM pg_constraint "
                "WHERE conrelid = 'shared.admin_users'::regclass AND conname = 'admin_users_email_chk'")
    comment, validated = cur.fetchone()
    assert validated and comment.count("TEMPORARY") == 2 and E2E in comment and ADMIN in comment
    assert "Dev only" in comment


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
            _apply(_read(MIGRATION), OTHER)
        assert "migration 283" in str(exc.value) and "ALTER TABLE needs the owner" in str(exc.value)
    finally:
        a.execute(f"REVOKE USAGE ON SCHEMA shared FROM {OTHER}")
        _drop_role(a, OTHER)
        admin.close()


def test_reapplying_250_then_283_in_either_order_keeps_a_valid_check():
    """250's own test module re-applies 250 (which drops the A10b clause); a later 283 brings it back. Neither file
    fails on top of the other, and 283 last always gives the full rule."""
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        _apply(_read(MIGRATION))
        _apply(_read(MIGRATION_250))
        assert ADMIN not in _constraint_def(a)
        _apply(_read(MIGRATION))
        definition = _constraint_def(a)
        assert ADMIN in definition and E2E in definition
    finally:
        _apply(_read(MIGRATION))
        admin.close()


def test_the_removal_sql_restores_247_and_refuses_while_either_row_exists(conn):
    """The header's removal step: with either E2E row still there the 247 CHECK can not come back (so the cleanup
    must delete both rows first); without them, it can, and both addresses are refused again."""
    cur = conn.cursor()
    uid = _add(cur, email=ADMIN)
    _refused(conn, lambda c: c.execute(RESTORE_247))
    cur.execute("DELETE FROM shared.admin_users WHERE id = %s", (uid,))
    rid = _add(cur, email=E2E, role="reviewer")
    _refused(conn, lambda c: c.execute(RESTORE_247))
    cur.execute("DELETE FROM shared.admin_users WHERE id = %s", (rid,))
    cur.execute(RESTORE_247)
    _refused(conn, lambda c: _add(c, email=ADMIN))   # rolled back with the test transaction
    _refused(conn, lambda c: _add(c, email=E2E, role="reviewer"))
