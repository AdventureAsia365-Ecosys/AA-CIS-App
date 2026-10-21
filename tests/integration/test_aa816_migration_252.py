"""AA-816 (root ADR 0006a A10) — migration 252 (the temporary 250 E2E test-mailbox exception in
admin_users_email_chk is removed; the 247 rule is back) against real Postgres.

The SQL runs for real (S231 lesson), errors NOT swallowed, as a NOSUPERUSER LOGIN stand-in of the owner of
shared.admin_users (Dev: DB Migrate runs cis/ files as cis_owner); a local test user without CREATEROLE applies it
as itself. The module fixture first re-applies 250 (so 252 really replaces the exception, whatever ran before),
checks that 252 RAISEs while an E2E reviewer row exists and leaves the CHECK unchanged, then deletes the row and
applies 252 twice (idempotent).

Then: the old address and every variant of it are refused; company addresses keep every role and flag; the
constraint is commented; guard 0 refuses a runner that does not own the table. The module teardown leaves the
247 rule in place (the state the CI replay of every migration ends in).
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

RUNNER = "zz252_owner"
OTHER = "zz252_not_owner"
E2E = "e2e@e2e-mail.lumiguides.it.com"
MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
MIGRATION_250 = os.path.join(MIGRATIONS, "250_admin_users_e2e_test_email.sql")
MIGRATION_252 = os.path.join(MIGRATIONS, "252_admin_users_e2e_exception_removed.sql")
RULE_247 = "email ~ '^[^@[:space:]]+@adventure[.]asia$'::text"


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


def _constraint(cur):
    cur.execute("SELECT pg_get_constraintdef(oid), obj_description(oid, 'pg_constraint'), convalidated "
                "FROM pg_constraint WHERE conrelid = 'shared.admin_users'::regclass "
                "AND conname = 'admin_users_email_chk'")
    return cur.fetchone()


def _insert_e2e_row(cur) -> str:
    uid = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.admin_users (id, username, role, password_hash, email, is_owner) "
                "VALUES (%s, %s, 'reviewer', NULL, %s, false)", (uid, f"aa252-{uid[:8]}", E2E))
    return uid


# Filled by the module fixture: what 252 did while the E2E row still existed.
REFUSAL: dict = {}


@pytest.fixture(scope="module", autouse=True)
def applied_252():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    _drop_role(a, RUNNER)
    a.execute("DELETE FROM shared.admin_users WHERE lower(email) = %s", (E2E,))
    a.execute("DELETE FROM shared.schema_versions WHERE version = '252'")
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
        # Start from the 250 exception (the state Dev is in before the removal), with the E2E row present.
        _apply(_read(MIGRATION_250), runner or DB_USER)
        uid = _insert_e2e_row(a)
        before = _constraint(a)
        with pytest.raises(psycopg2.Error) as exc:
            _apply(_read(MIGRATION_252), runner or DB_USER)
        REFUSAL["message"] = str(exc.value)
        REFUSAL["unchanged"] = _constraint(a) == before
        a.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '252'")
        REFUSAL["registered"] = a.fetchone()[0]
        # The cleanup deletes the row first; then 252 applies, twice (idempotent).
        a.execute("DELETE FROM shared.admin_users WHERE id = %s", (uid,))
        _apply(_read(MIGRATION_252), runner or DB_USER)
        _apply(_read(MIGRATION_252), runner or DB_USER)
        yield runner
    finally:
        if runner:
            a.execute(f'ALTER TABLE shared.admin_users OWNER TO "{table_owner}"')
            a.execute(f'ALTER TABLE shared.schema_versions OWNER TO "{reg_owner}"')
            a.execute(f"REVOKE USAGE ON SCHEMA shared FROM {runner}")
            _drop_role(a, runner)
        a.execute("DELETE FROM shared.admin_users WHERE lower(email) = %s", (E2E,))
        # Whatever happened above, leave the 247 rule (what the CI replay of every migration ends in).
        a.execute(_read(MIGRATION_252))
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
    cols = {"id": str(uuid.uuid4()), "username": f"aa252-{uuid.uuid4().hex[:8]}", "role": "reviewer",
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


def test_252_raises_while_the_e2e_row_exists_and_changes_nothing():
    message = REFUSAL["message"]
    assert "migration 252" in message and "1 row(s) still use the old E2E test address" in message
    assert E2E not in message   # counts only, never an address
    assert REFUSAL["unchanged"] is True
    assert REFUSAL["registered"] == 0


def test_252_registered_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*), max(description) FROM shared.schema_versions WHERE version = '252'")
    count, description = cur.fetchone()
    assert count == 1 and "250 E2E exception is removed" in description


def test_the_constraint_is_the_247_rule_and_commented(conn):
    definition, comment, validated = _constraint(conn.cursor())
    assert validated
    assert RULE_247 in definition and "lower(email)" in definition
    assert "lumiguides" not in definition and "reviewer" not in definition
    assert comment == ("AA-816 (247, 252): lower-cased @adventure.asia only; the temporary 250 E2E exception "
                       "was removed by 252")


@pytest.mark.parametrize("cols", [
    {"email": E2E},
    {"email": E2E, "role": "admin"},
    {"email": E2E.upper()},
    {"email": "e2e+1@e2e-mail.lumiguides.it.com"},
    {"email": "e2e2@e2e-mail.lumiguides.it.com"},
    {"email": "e2e@lumiguides.it.com"},
    {"email": "e2e@x.e2e-mail.lumiguides.it.com"},
    {"email": "e2e@e2e-mail.lumiguides.it.com.evil.com"},
    {"email": " e2e@e2e-mail.lumiguides.it.com"},
    {"email": "x@example.com"},
    {"email": "a@adventure.asia.evil.com"},
    {"email": "a@sub.adventure.asia"},
    {"email": "Nghiep@adventure.asia"},
    {"email": " a@adventure.asia"},
])
def test_the_old_address_and_every_non_company_address_are_refused(conn, cols):
    _refused(conn, lambda cur: _add(cur, **cols))


def test_company_addresses_keep_every_role_and_flag(conn):
    cur = conn.cursor()
    _add(cur, email=f"a252-{uuid.uuid4().hex[:6]}@adventure.asia", role="admin", is_owner=True)
    _add(cur, email=f"b252-{uuid.uuid4().hex[:6]}@adventure.asia", role="content", password_hash="x")
    _add(cur, email="e2e-cognito-test@adventure.asia", role="reviewer")
    _add(cur, email=None, role="viewer", password_hash="x")


def test_guard_refuses_a_runner_that_does_not_own_the_table():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        a.execute(f"CREATE ROLE {OTHER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS INHERIT", (DB_PASS,))
    except psycopg2.errors.InsufficientPrivilege:
        admin.close()
        pytest.skip("the test user may not create roles (local DB)")
    try:
        a.execute(f"GRANT USAGE ON SCHEMA shared TO {OTHER}")
        with pytest.raises(psycopg2.Error) as exc:
            _apply(_read(MIGRATION_252), OTHER)
        assert "migration 252" in str(exc.value) and "ALTER TABLE needs the owner" in str(exc.value)
    finally:
        a.execute(f"REVOKE USAGE ON SCHEMA shared FROM {OTHER}")
        _drop_role(a, OTHER)
        admin.close()
