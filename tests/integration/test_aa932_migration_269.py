"""AA-932 (partner CRM, card CRM-04) — migration 269 (crm_owner registry grants + key guard branch) on real Postgres.

Same method as test_aa852_migration_243 (lesson S236): conftest replays the migrations as a SUPERUSER, which skips
every privilege check. On Dev, DB Migrate runs 269 as cis_owner, a plain role that owns schema shared, the registry
and the key guard function (CIS 245). The module fixture hands those three objects to a NOSUPERUSER stand-in and
applies the file as that role twice (idempotent), errors NOT swallowed; teardown gives them back to the test user.

Then the trigger is exercised the way DB Migrate runs crm-NNN files: SET ROLE crm_owner (session user unchanged),
INSERT … ON CONFLICT (version) DO NOTHING. crm_owner registers crm- keys only; the 243 rules for every other role
stay (bk_owner bk-, tp_owner tp-, registry owner members any key, every other role none).
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

RUNNER = "zz932_cis_owner"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "269_crm_owner_registry_grant.sql")
# AA-CIS-Infra db/run_migration.py CAN_REGISTER_SQL: what DB Migrate requires of the owner before it runs a switched
# file (else exit 8).
CAN_REGISTER_SQL = ("SELECT pg_catalog.has_schema_privilege(%s, 'shared', 'USAGE') "
                    "AND pg_catalog.has_table_privilege(%s, 'shared.schema_versions', 'INSERT')")
REGISTER_SQL = ("INSERT INTO shared.schema_versions (version, applied_at, description) "
                "VALUES (%s, now(), 'aa932 test') ON CONFLICT (version) DO NOTHING")


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


def _apply_as_runner(sql):
    runner = _connect(RUNNER)
    runner.autocommit = True
    try:
        runner.cursor().execute(sql)  # raises on any error
    finally:
        runner.close()


def _drop_runner(a):
    a.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNNER,))
    if a.fetchone():
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER; DROP OWNED BY {RUNNER}; DROP ROLE {RUNNER}")


@pytest.fixture(scope="module", autouse=True)
def rds_like_269():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    _drop_runner(a)
    a.execute("SELECT to_regprocedure('shared.schema_versions_key_guard()') IS NOT NULL")
    if not a.fetchone()[0]:
        pytest.skip("CIS 243 (key guard) is not in this replay")
    a.execute(f"CREATE ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS INHERIT", (DB_PASS,))
    a.execute(f"GRANT bk_owner, tp_owner TO {RUNNER}")
    # Dev after 245: cis_owner owns schema shared, the registry and the guard function.
    a.execute(f"ALTER SCHEMA shared OWNER TO {RUNNER}")
    a.execute(f"ALTER TABLE shared.schema_versions OWNER TO {RUNNER}")
    a.execute(f"ALTER FUNCTION shared.schema_versions_key_guard() OWNER TO {RUNNER}")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '269'")
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


def test_269_registered_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '269'")
    assert cur.fetchone()[0] == 1


def test_crm_owner_reads_and_inserts_only(conn):
    cur = conn.cursor()
    cur.execute(CAN_REGISTER_SQL, ("crm_owner", "crm_owner"))
    assert cur.fetchone()[0] is True
    cur.execute("SELECT has_table_privilege('crm_owner', 'shared.schema_versions', 'SELECT'), "
                "has_table_privilege('crm_owner', 'shared.schema_versions', 'UPDATE'), "
                "has_table_privilege('crm_owner', 'shared.schema_versions', 'DELETE'), "
                "has_schema_privilege('crm_owner', 'shared', 'CREATE'), "
                "has_table_privilege('crm_owner', 'shared.schema_versions', 'INSERT WITH GRANT OPTION')")
    assert cur.fetchone() == (True, False, False, False, False)


def test_crm_owner_registers_its_own_key(conn):
    _as(conn, "crm_owner", REGISTER_SQL, ("crm-999",))
    cur = _as(conn, "crm_owner", "SELECT count(*) FROM shared.schema_versions WHERE version = 'crm-999'")
    assert cur.fetchone()[0] == 1


@pytest.mark.parametrize("role, foreign_key", [
    ("crm_owner", "270"),       # a CIS key
    ("crm_owner", "bk-099"),
    ("crm_owner", "tp-099"),
    ("crm_owner", "rl-099"),
    ("crm_owner", "CRM-099"),   # LIKE is case-sensitive; keys are lower case (MIG-1)
    ("crm_owner", "crmx-099"),  # 'crm-%' only, not every key starting with crm
    ("bk_owner", "crm-099"),    # the older owners cannot take the new prefix
    ("tp_owner", "crm-099"),
])
def test_owner_cannot_register_another_owners_key(conn, role, foreign_key):
    with pytest.raises(psycopg2.errors.CheckViolation, match=f"{role} may not register '{foreign_key}'"):
        _as(conn, role, REGISTER_SQL, (foreign_key,))


@pytest.mark.parametrize("role, own_key", [("bk_owner", "bk-998"), ("tp_owner", "tp-998")])
def test_243_owner_branches_unchanged(conn, role, own_key):
    _as(conn, role, REGISTER_SQL, (own_key,))
    cur = _as(conn, role, "SELECT count(*) FROM shared.schema_versions WHERE version = %s", (own_key,))
    assert cur.fetchone()[0] == 1


def test_crm_runtime_through_crm_owner_view_refused(conn):
    # crm_app (the CRM runtime) can never write the registry, even if a crm_owner-owned view exposed it: the
    # trigger sees the caller, not the view owner (the 243 review case, for the new owner).
    cur = conn.cursor()
    cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'zz932_crm_app') THEN "
                "CREATE ROLE zz932_crm_app NOLOGIN; END IF; END $$; "
                "CREATE SCHEMA zz932_view; "
                "CREATE VIEW zz932_view.v_schema_versions AS SELECT * FROM shared.schema_versions; "
                "ALTER VIEW zz932_view.v_schema_versions OWNER TO crm_owner; "
                "GRANT USAGE ON SCHEMA zz932_view TO zz932_crm_app; "
                "GRANT SELECT, INSERT ON zz932_view.v_schema_versions TO zz932_crm_app")
    with pytest.raises(psycopg2.errors.CheckViolation, match="zz932_crm_app may not register 'crm-777'"):
        _as(conn, "zz932_crm_app", "INSERT INTO zz932_view.v_schema_versions (version, applied_at, description) "
                                   "VALUES ('crm-777', now(), 'aa932 view test') ON CONFLICT (version) DO NOTHING")


def test_guard_stays_one_before_insert_trigger_not_security_definer(conn):
    cur = conn.cursor()
    cur.execute("SELECT tgname, tgtype, tgenabled, tgfoid::regprocedure::text FROM pg_trigger "
                "WHERE tgrelid = 'shared.schema_versions'::regclass AND NOT tgisinternal")
    assert cur.fetchall() == [("schema_versions_key_guard", 7, "O", "shared.schema_versions_key_guard()")]
    cur.execute("SELECT prosecdef, has_function_privilege('public', 'shared.schema_versions_key_guard()', 'EXECUTE') "
                "FROM pg_proc WHERE oid = 'shared.schema_versions_key_guard()'::regprocedure")
    assert cur.fetchone() == (False, False)


def test_guard_1_refuses_a_write_grant_to_crm_owner():
    _apply_as_runner("GRANT UPDATE (description) ON shared.schema_versions TO crm_owner")
    try:
        runner = _connect(RUNNER)
        runner.autocommit = True
        try:
            with pytest.raises(psycopg2.errors.RaiseException, match="migration 269: crm_owner holds column-level"):
                runner.cursor().execute(open(MIGRATION, encoding="utf-8").read())
            runner.cursor().execute("ROLLBACK")
        finally:
            runner.close()
    finally:
        _apply_as_runner("REVOKE UPDATE (description) ON shared.schema_versions FROM crm_owner")
