"""AA-852 (X1 step S4) — migration 243 (owner-role registry grants + key guard) against real Postgres.

Lesson S236 (test_aa791_migration_231): conftest applies migrations as a SUPERUSER, which skips every
privilege check. On Dev, DB Migrate runs 243 as the RDS master, a plain role that owns schema shared and
shared.schema_versions. The module fixture undoes the superuser-applied 243, hands schema shared and the
registry to a NOSUPERUSER stand-in of the master, and applies the file as that role twice (idempotent),
errors NOT swallowed. Ownership goes back to the test user at teardown.

The runner stand-in is a member of bk_owner, tp_owner and cis_app, as rl-001 makes the Dev master.

The trigger is then exercised the way DB Migrate will run Booking / TripPlanner files after bk-014 /
tp-013: SET ROLE bk_owner / tp_owner (session user unchanged), INSERT … ON CONFLICT (version) DO NOTHING.
The guard refuses by default (review of 17/10): only members of the registry's owner insert any key, so a
runtime role reaching the registry through a bk_owner-owned simple view (booking.v_schema_versions after
bk-014: the base table is checked as the view owner, the trigger sees the caller) or through a stray
direct grant is refused too.
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


def _apply_as_runner(sql):
    runner = _connect(RUNNER)
    runner.autocommit = True
    try:
        runner.cursor().execute(sql)  # raises on any error
    finally:
        runner.close()


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
    # rl-001: the master is an INHERIT member of the new owner roles and of cis_app (for 244's guards).
    a.execute(f"GRANT bk_owner, tp_owner, cis_app TO {RUNNER}")
    # Dev: the master owns schema shared and shared.schema_versions (schema reference, 16/10).
    a.execute(f"ALTER SCHEMA shared OWNER TO {RUNNER}")
    a.execute(f"ALTER TABLE shared.schema_versions OWNER TO {RUNNER}")
    sql = open(MIGRATION, encoding="utf-8").read()
    _apply_as_runner(sql)
    _apply_as_runner(sql)             # idempotent re-run
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
                "has_schema_privilege(%s, 'shared', 'CREATE'), "
                "has_any_column_privilege(%s, 'shared.schema_versions', 'UPDATE'), "
                "has_table_privilege(%s, 'shared.schema_versions', 'SELECT WITH GRANT OPTION'), "
                "has_table_privilege(%s, 'shared.schema_versions', 'INSERT WITH GRANT OPTION')", (role,) * 8)
    assert cur.fetchone() == (True, False, False, False, False, False, False, False)


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
    with pytest.raises(psycopg2.errors.CheckViolation, match=f"{role} may not register '{foreign_key}'"):
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
def test_registry_owner_inserts_any_key(conn, key):
    # The master (and later cis_owner / db_migrator, members of cis_owner) is not restricted by the key guard.
    cur = _as(conn, RUNNER, REGISTER_SQL, (key,))
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = %s", (key,))
    assert cur.fetchone()[0] == 1


@pytest.mark.parametrize("caller", ["booking", "cis_app"])
@pytest.mark.parametrize("key", ["246", "bk-777", "tp-777", "rl-777"])
def test_runtime_role_through_owner_role_view_refused(conn, caller, key):
    # Review 17/10, problem 1: bk-014 makes bk_owner the owner of booking.v_schema_versions, a simple
    # auto-updatable view. An INSERT through it is checked on the base table as the view owner (bk_owner, which
    # holds INSERT), but inside the trigger current_user is the caller. A later stray GRANT … ON ALL TABLES or a
    # TABLES default ACL would hand the runtime INSERT on the view.
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA zz852_view; "
                "CREATE VIEW zz852_view.v_schema_versions AS SELECT * FROM shared.schema_versions; "
                "ALTER VIEW zz852_view.v_schema_versions OWNER TO bk_owner; "
                "GRANT USAGE ON SCHEMA zz852_view TO booking, cis_app; "
                "GRANT SELECT, INSERT ON zz852_view.v_schema_versions TO booking, cis_app")
    with pytest.raises(psycopg2.errors.CheckViolation, match=f"{caller} may not register '{key}'"):
        _as(conn, caller, "INSERT INTO zz852_view.v_schema_versions (version, applied_at, description) "
                          "VALUES (%s, now(), 'aa852 view test') ON CONFLICT (version) DO NOTHING", (key,))


@pytest.mark.parametrize("key", ["246", "bk-777", "tp-777"])
def test_non_owner_role_with_direct_insert_grant_refused(conn, key):
    # A stray direct grant (244 revokes cis_app's registry writes; this is the trigger backing that up).
    cur = conn.cursor()
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app; GRANT SELECT, INSERT ON shared.schema_versions TO cis_app")
    with pytest.raises(psycopg2.errors.CheckViolation, match=f"cis_app may not register '{key}'"):
        _as(conn, "cis_app", REGISTER_SQL, (key,))


def test_bk_owner_own_key_through_its_view(conn):
    # The legitimate path stays open: bk_owner itself (a file run by DB Migrate) may use its own prefix.
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA zz852_view; "
                "CREATE VIEW zz852_view.v_schema_versions AS SELECT * FROM shared.schema_versions; "
                "ALTER VIEW zz852_view.v_schema_versions OWNER TO bk_owner; "
                "GRANT USAGE ON SCHEMA zz852_view TO bk_owner")
    _as(conn, "bk_owner", "INSERT INTO zz852_view.v_schema_versions (version, applied_at, description) "
                          "VALUES ('bk-776', now(), 'aa852 view test')")
    cur = _as(conn, "bk_owner", "SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-776'")
    assert cur.fetchone()[0] == 1


def test_key_guard_function_not_executable_by_public(conn):
    # SEC-4: nothing granted to PUBLIC. A firing trigger does not check EXECUTE.
    cur = conn.cursor()
    cur.execute("SELECT has_function_privilege('public', 'shared.schema_versions_key_guard()', 'EXECUTE'), "
                "has_function_privilege('cis_app', 'shared.schema_versions_key_guard()', 'EXECUTE'), "
                "has_function_privilege('bk_owner', 'shared.schema_versions_key_guard()', 'EXECUTE')")
    assert cur.fetchone() == (False, False, False)


@pytest.mark.parametrize("extra_grant, revoke", [
    ("GRANT UPDATE (description) ON shared.schema_versions TO bk_owner",
     "REVOKE UPDATE (description) ON shared.schema_versions FROM bk_owner"),
    ("GRANT INSERT ON shared.schema_versions TO tp_owner WITH GRANT OPTION",
     "REVOKE GRANT OPTION FOR INSERT ON shared.schema_versions FROM tp_owner"),
    ("GRANT SELECT ON shared.schema_versions TO bk_owner WITH GRANT OPTION",
     "REVOKE GRANT OPTION FOR SELECT ON shared.schema_versions FROM bk_owner"),
    ("GRANT REFERENCES ON shared.schema_versions TO tp_owner",
     "REVOKE REFERENCES ON shared.schema_versions FROM tp_owner"),
])
def test_guard_1_refuses_more_than_read_and_insert(extra_grant, revoke):
    # Review 17/10, problem 4: column-level UPDATE and grant options passed the old guard 1. A grant option
    # would let bk_owner hand INSERT on to booking (the fail-open path of problem 1).
    _apply_as_runner(extra_grant)
    try:
        runner = _connect(RUNNER)
        runner.autocommit = True
        try:
            with pytest.raises(psycopg2.errors.RaiseException, match="migration 243: .* only read and insert"):
                runner.cursor().execute(open(MIGRATION, encoding="utf-8").read())
            runner.cursor().execute("ROLLBACK")
        finally:
            runner.close()
    finally:
        _apply_as_runner(revoke)


def test_one_before_insert_row_trigger_not_security_definer(conn):
    cur = conn.cursor()
    cur.execute("SELECT tgname, tgtype, tgenabled, tgfoid::regprocedure::text FROM pg_trigger "
                "WHERE tgrelid = 'shared.schema_versions'::regclass AND NOT tgisinternal")
    assert cur.fetchall() == [("schema_versions_key_guard", 7, "O", "shared.schema_versions_key_guard()")]
    cur.execute("SELECT prosecdef, pg_get_userbyid(proowner) FROM pg_proc "
                "WHERE oid = 'shared.schema_versions_key_guard()'::regprocedure")
    assert cur.fetchone() == (False, RUNNER)


def test_ci_roles_match_rl001(conn):
    # Review 17/10, problem 6: the CI stand-ins carry rl-001's attributes and membership chain, so a guard or
    # test on them (244's cis_app checks) behaves in CI as on Dev.
    cur = conn.cursor()
    cur.execute("SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolconnlimit "
                "FROM pg_roles WHERE rolname IN ('bk_owner', 'tp_owner', 'cis_app') ORDER BY rolname")
    assert cur.fetchall() == [("bk_owner", False, False, False, False, False, -1),
                              ("cis_app", True, False, False, False, False, 90),
                              ("tp_owner", False, False, False, False, False, -1)]
    # USAGE = inherits the role's privileges (PG 15 and 16; CI runs postgres:15, no inherit_option column).
    cur.execute("SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
                "WHERE m.member = %s::regrole AND pg_has_role(%s, m.roleid, 'USAGE') ORDER BY 1", (RUNNER, RUNNER))
    assert [row[0] for row in cur.fetchall()] == ["bk_owner", "cis_app", "tp_owner"]
