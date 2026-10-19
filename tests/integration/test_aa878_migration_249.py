"""AA-878 — migration 249 (shared.admin_role gains the read-only 'viewer') against real Postgres.

The SQL runs for real (S231 lesson), and the parts a superuser would hide are modelled:
- conftest applied every migration, so the enum lists the four values in order and 249 is registered;
- the file, applied again as a NOSUPERUSER stand-in of the RDS master that owns the type (Dev: DB Migrate runs as
  the master today, as cis_owner after X1 245), is a no-op, errors NOT swallowed; ownership goes back at teardown.
  A local test user without CREATEROLE can not make the stand-in; the file is then applied as the test user;
- ADD VALUE inside the file's own BEGIN … COMMIT really adds the value to a type that lacks it (a copy of the type
  in a scratch schema, the same SQL with the type name swapped), and the value is usable once committed;
- guard 0 refuses a runner that does not own the type (nothing changes);
- after commit a 'viewer' row can be stored and read back (the E2E account's shape: legacy password row, no email,
  not an owner).
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

RUNNER = "zz878_rds_master"
OTHER = "zz878_not_owner"
SCRATCH = "zz878"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "249_admin_role_viewer.sql")
VIEWER_ID = "00000000-0000-0000-0000-000000000878"


def _sql() -> str:
    with open(MIGRATION) as f:
        return f.read()


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


def _apply(sql, user=DB_USER):
    c = _connect(user)
    c.autocommit = True   # the file brings its own BEGIN … COMMIT
    try:
        c.cursor().execute(sql)   # raises on any error
    finally:
        c.close()


def _labels(cur, regtype="shared.admin_role"):
    cur.execute("SELECT enumlabel FROM pg_enum WHERE enumtypid = %s::regtype ORDER BY enumsortorder", (regtype,))
    return [r[0] for r in cur.fetchall()]


def _drop_role(cur, name):
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    if cur.fetchone():
        cur.execute(f"REASSIGN OWNED BY {name} TO CURRENT_USER; DROP OWNED BY {name}; DROP ROLE {name}")


@pytest.fixture
def admin():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    yield cur
    cur.execute("DELETE FROM shared.admin_users WHERE id = %s", (VIEWER_ID,))
    cur.execute(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE")
    cur.close()
    c.close()


def test_enum_lists_the_four_roles_and_249_is_registered(admin):
    assert _labels(admin) == ["admin", "reviewer", "content", "viewer"]
    admin.execute("SELECT description FROM shared.schema_versions WHERE version = '249'")
    row = admin.fetchone()
    assert row and "viewer" in row[0]


def test_reapply_as_the_type_owner_is_a_no_op(admin):
    admin.execute("SELECT pg_get_userbyid(typowner) FROM pg_type WHERE oid = 'shared.admin_role'::regtype")
    type_owner = admin.fetchone()[0]
    admin.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'shared.schema_versions'::regclass")
    reg_owner = admin.fetchone()[0]
    runner = None
    try:
        try:
            admin.execute(f"CREATE ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS INHERIT", (DB_PASS,))
            runner = RUNNER
        except psycopg2.errors.InsufficientPrivilege:
            runner = None
        if runner:
            admin.execute(f"GRANT USAGE ON SCHEMA shared TO {runner}")
            admin.execute(f"ALTER TYPE shared.admin_role OWNER TO {runner}")
            admin.execute(f"ALTER TABLE shared.schema_versions OWNER TO {runner}")
        for _ in range(2):
            _apply(_sql(), runner or DB_USER)
        assert _labels(admin) == ["admin", "reviewer", "content", "viewer"]
        admin.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '249'")
        assert admin.fetchone()[0] == 1
    finally:
        if runner:
            admin.execute(f'ALTER TYPE shared.admin_role OWNER TO "{type_owner}"')
            admin.execute(f'ALTER TABLE shared.schema_versions OWNER TO "{reg_owner}"')
            admin.execute(f"REVOKE USAGE ON SCHEMA shared FROM {runner}")
            _drop_role(admin, runner)


def test_add_value_in_the_files_transaction_adds_it_to_a_type_without_it(admin):
    """The real statement order of 249 (guard, ADD VALUE, register, COMMIT) on a copy of the pre-249 type."""
    admin.execute(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE")
    admin.execute(f"CREATE SCHEMA {SCRATCH}")
    admin.execute(f"CREATE TYPE {SCRATCH}.admin_role AS ENUM ('admin', 'reviewer', 'content')")
    admin.execute(f"CREATE TABLE {SCRATCH}.schema_versions (LIKE shared.schema_versions INCLUDING ALL)")
    sql = _sql().replace("shared.admin_role", f"{SCRATCH}.admin_role").replace(
        "shared.schema_versions", f"{SCRATCH}.schema_versions")
    assert "shared." not in sql.split("BEGIN;", 1)[1]
    _apply(sql)
    assert _labels(admin, f"{SCRATCH}.admin_role") == ["admin", "reviewer", "content", "viewer"]
    admin.execute(f"SELECT 'viewer'::{SCRATCH}.admin_role::text")   # usable once committed
    assert admin.fetchone()[0] == "viewer"
    admin.execute(f"SELECT version FROM {SCRATCH}.schema_versions")
    assert [r[0] for r in admin.fetchall()] == ["249"]


def test_guard_refuses_a_runner_that_does_not_own_the_type(admin):
    try:
        admin.execute(f"CREATE ROLE {OTHER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS INHERIT", (DB_PASS,))
    except psycopg2.errors.InsufficientPrivilege:
        pytest.skip("the test user may not create roles (local DB)")
    try:
        admin.execute(f"GRANT USAGE ON SCHEMA shared TO {OTHER}")
        with pytest.raises(psycopg2.Error) as exc:
            _apply(_sql(), OTHER)
        assert "migration 249" in str(exc.value) and "ALTER TYPE needs the owner" in str(exc.value)
        assert _labels(admin) == ["admin", "reviewer", "content", "viewer"]
    finally:
        admin.execute(f"REVOKE USAGE ON SCHEMA shared FROM {OTHER}")
        _drop_role(admin, OTHER)


def test_a_viewer_row_is_stored_like_the_e2e_account(admin):
    admin.execute(
        "INSERT INTO shared.admin_users (id, username, password_hash, role) "
        "VALUES (%s, 'e2e-aa878-viewer-test', '$2b$04$notarealhashnotarealhashnotarealhashnotarealhashnotar', "
        "'viewer')", (VIEWER_ID,))
    admin.execute("SELECT role::text, is_active, is_owner, email FROM shared.admin_users WHERE id = %s", (VIEWER_ID,))
    assert admin.fetchone() == ("viewer", True, False, None)
