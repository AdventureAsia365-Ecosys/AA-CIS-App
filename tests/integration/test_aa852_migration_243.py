"""AA-852 (X1 step S4) — migration 243 (owner-role registry grants + key guard) against real Postgres.

Lesson S236 (test_aa791_migration_231): conftest applies migrations as a SUPERUSER, which skips every
privilege check. On Dev, DB Migrate runs 243 as the RDS master, a plain role that owns schema shared and
shared.schema_versions. The module fixture undoes the superuser-applied 243, hands schema shared and the
registry to a NOSUPERUSER stand-in of the master, and applies the file as that role twice (idempotent),
errors NOT swallowed. Ownership goes back to the test user at teardown.

The trigger is then exercised the way DB Migrate will run Booking / TripPlanner files after bk-014 /
tp-013: SET ROLE bk_owner / tp_owner (session user unchanged), INSERT … ON CONFLICT (version) DO NOTHING.
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

RUNNER = "zz852_rds_master"
OWNERS = ("bk_owner", "tp_owner")
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "243_x1_owner_role_grants.sql")
# AA-CIS-Infra db/run_migration.py CAN_REGISTER_SQL: what DB Migrate requires of the owner before it runs
# a switched file (else exit 8).
CAN_REGISTER_SQL = ("SELECT pg_catalog.has_schema_privilege(%s, 'shared', 'USAGE') "
                    "AND pg_catalog.has_table_privilege(%s, 'shared.schema_versions', 'INSERT')")
REGISTER_SQL = ("INSERT INTO shared.schema_versions (version, applied_at, description) "
                "VALUES (%s, now(), 'aa852 test') ON CONFLICT (version) DO NOTHING")


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


def _undo_243(a):
    a.execute("DROP TRIGGER IF EXISTS schema_versions_key_guard ON shared.schema_versions; "
              "DROP FUNCTION IF EXISTS shared.schema_versions_key_guard(); "
              "REVOKE ALL ON shared.schema_versions FROM bk_owner, tp_owner; "
              "REVOKE ALL ON SCHEMA shared FROM bk_owner, tp_owner; "
              "DELETE FROM shared.schema_versions WHERE version = '243'")


def _drop_runner(a):
    a.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNNER,))
    if a.fetchone():
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER; DROP OWNED BY {RUNNER}; DROP ROLE {RUNNER}")


@pytest.fixture(scope="module", autouse=True)
def rds_like_243():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    _drop_runner(a)
    _undo_243(a)
    a.execute(f"CREATE ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS INHERIT", (DB_PASS,))
    # Dev: the master owns schema shared and shared.schema_versions (schema reference, 16/10).
    a.execute(f"ALTER SCHEMA shared OWNER TO {RUNNER}")
    a.execute(f"ALTER TABLE shared.schema_versions OWNER TO {RUNNER}")
    runner = _connect(RUNNER)
    runner.autocommit = True
    try:
        sql = open(MIGRATION, encoding="utf-8").read()
        runner.cursor().execute(sql)  # raises on any error
        runner.cursor().execute(sql)  # idempotent re-run
    finally:
        runner.close()
    try:
        yield
    finally:
        _drop_runner(a)               # schema shared, the registry and the function back to the test user
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


def _as(conn, role, sql, params=None):
    cur = conn.cursor()
    cur.execute(f"SET LOCAL ROLE {role}")
    cur.execute(sql, params)
    return cur


def test_243_registered_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '243'")
    assert cur.fetchone()[0] == 1


@pytest.mark.parametrize("role", OWNERS)
def test_owner_roles_read_and_insert_only(conn, role):
    cur = conn.cursor()
    cur.execute(CAN_REGISTER_SQL, (role, role))
    assert cur.fetchone()[0] is True
    cur.execute("SELECT has_table_privilege(%s, 'shared.schema_versions', 'SELECT'), "
                "has_table_privilege(%s, 'shared.schema_versions', 'UPDATE'), "
                "has_table_privilege(%s, 'shared.schema_versions', 'DELETE'), "
                "has_table_privilege(%s, 'shared.schema_versions', 'TRUNCATE'), "
                "has_schema_privilege(%s, 'shared', 'CREATE')", (role,) * 5)
    assert cur.fetchone() == (True, False, False, False, False)


@pytest.mark.parametrize("role, own_key", [("bk_owner", "bk-999"), ("tp_owner", "tp-999")])
def test_owner_registers_its_own_key(conn, role, own_key):
    _as(conn, role, REGISTER_SQL, (own_key,))
    cur = _as(conn, role, "SELECT count(*) FROM shared.schema_versions WHERE version = %s", (own_key,))
    assert cur.fetchone()[0] == 1            # the read a view owned by bk_owner (booking.v_schema_versions) needs


@pytest.mark.parametrize("role, foreign_key", [
    ("bk_owner", "246"),        # the plan's case: a CIS key
    ("bk_owner", "tp-014"),
    ("bk_owner", "rl-002"),
    ("bk_owner", "BK-015"),     # LIKE is case-sensitive; keys are lower case (MIG-1)
    ("tp_owner", "246"),
    ("tp_owner", "bk-015"),
    ("tp_owner", "rl-002"),
])
def test_owner_cannot_register_another_owners_key(conn, role, foreign_key):
    with pytest.raises(psycopg2.errors.CheckViolation, match=f"{role} may register only"):
        _as(conn, role, REGISTER_SQL, (foreign_key,))


@pytest.mark.parametrize("role", OWNERS)
def test_wrong_key_raises_even_when_already_registered(conn, role):
    # BEFORE INSERT row triggers fire before the ON CONFLICT check: no silent DO NOTHING.
    with pytest.raises(psycopg2.errors.CheckViolation):
        _as(conn, role, REGISTER_SQL, ("243",))


@pytest.mark.parametrize("role", OWNERS)
def test_owner_cannot_rewrite_or_remove_rows(conn, role):
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        _as(conn, role, "UPDATE shared.schema_versions SET description = 'x' WHERE version = '243'")
    conn.rollback()
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        _as(conn, role, "DELETE FROM shared.schema_versions WHERE version = '243'")


@pytest.mark.parametrize("key", ["999", "bk-998", "tp-998", "rl-998"])
def test_other_inserters_unchanged(conn, key):
    # The master (and later cis_owner / db_migrator) is not restricted by the key guard.
    cur = _as(conn, RUNNER, REGISTER_SQL, (key,))
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = %s", (key,))
    assert cur.fetchone()[0] == 1


def test_one_before_insert_row_trigger_not_security_definer(conn):
    cur = conn.cursor()
    cur.execute("SELECT tgname, tgtype, tgenabled, tgfoid::regprocedure::text FROM pg_trigger "
                "WHERE tgrelid = 'shared.schema_versions'::regclass AND NOT tgisinternal")
    assert cur.fetchall() == [("schema_versions_key_guard", 7, "O", "shared.schema_versions_key_guard()")]
    cur.execute("SELECT prosecdef, pg_get_userbyid(proowner) FROM pg_proc "
                "WHERE oid = 'shared.schema_versions_key_guard()'::regprocedure")
    assert cur.fetchone() == (False, RUNNER)
