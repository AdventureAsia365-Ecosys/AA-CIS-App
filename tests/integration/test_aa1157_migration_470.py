"""AA-1157 (S288) — migration 470: shared.staff_view_as_session also logs view-as-a-staff-role sessions (tenant_id
NULL + view_role), against real Postgres.

The SQL runs for real, errors NOT swallowed. The module fixture applies 364, 462 and 470 (twice: re-runnable). Then:
exactly one kind per row (a tenant or a role, never both, never neither), the role code's format, the guard keeps a
staff-role row append-only (no UPDATE of view_role, no DELETE before its 24-month window, even as cis_app) while tenant
rows behave as before (the erasure deletes them once the tenant is offboarded). Invented ids only; every test rolls
back."""
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
MIG_364 = os.path.join(_MIG, "364_staff_tenant_assignment.sql")
MIG_462 = os.path.join(_MIG, "462_aa1155_custom_staff_roles.sql")
MIG_470 = os.path.join(_MIG, "470_aa1157_staff_view_as_role.sql")


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def _apply(path):
    c = _connect()
    c.autocommit = True   # the file brings its own BEGIN … COMMIT
    try:
        with open(path, encoding="utf-8") as f:
            c.cursor().execute(f.read())
    finally:
        c.close()


@pytest.fixture(scope="module", autouse=True)
def applied():
    _apply(MIG_364)
    _apply(MIG_462)
    _apply(MIG_470)
    _apply(MIG_470)   # re-runnable
    yield


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def _staff(cur):
    staff_id = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.admin_users (id, username, role, is_active, email) VALUES (%s, %s, 'admin', true, "
                "%s)", (staff_id, f"aa1157-{staff_id[:8]}", f"aa1157-{staff_id[:8]}@adventure.asia"))
    return staff_id


def _tenant(cur):
    tenant_id = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week) "
                "VALUES (%s, %s, %s, 'starter', 3)", (tenant_id, "Quillwater Tours", f"quill-{tenant_id[:8]}"))
    return tenant_id


def _role_session(cur, staff, role="custom_content", started="now()"):
    cur.execute("INSERT INTO shared.staff_view_as_session (admin_user_id, view_role, reason, started_at, expires_at) "
                f"VALUES (%s, %s, 'Check what this role sees', {started}, {started} + interval '30 minutes') "
                "RETURNING session_id", (staff, role))
    return cur.fetchone()[0]


def _as_cis_app(cur):
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    cur.execute("GRANT SELECT ON shared.tenants TO cis_app")
    cur.execute("GRANT EXECUTE ON FUNCTION shared.tenant_account_closed(uuid) TO cis_app")
    cur.execute("SET LOCAL ROLE cis_app")


def _refused(cur, exc, sql, args):
    cur.execute("SAVEPOINT s")
    with pytest.raises(exc):
        cur.execute(sql, args)
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_registers_once_and_is_contract_without_a_directive(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '470'")
    assert cur.fetchone()[0] == 1
    text = open(MIG_470, encoding="utf-8").read()
    assert text.splitlines()[0].startswith("-- Migration 470")
    assert "migrate:" not in text.splitlines()[0]


def test_columns_and_checks(conn):
    cur = conn.cursor()
    cur.execute("SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name = 'staff_view_as_session' AND column_name IN ('tenant_id', 'view_role') "
                "ORDER BY column_name")
    assert cur.fetchall() == [("tenant_id", "YES"), ("view_role", "YES")]


def test_exactly_one_kind_per_row(conn):
    cur = conn.cursor()
    staff, tenant = _staff(cur), _tenant(cur)
    _role_session(cur, staff)                                 # a role row: fine
    cur.execute("INSERT INTO shared.staff_view_as_session (admin_user_id, tenant_id, reason, expires_at) "
                "VALUES (%s, %s, 'ticket follow-up', now() + interval '10 minutes')", (staff, tenant))   # tenant row
    _refused(cur, psycopg2.errors.CheckViolation,
             "INSERT INTO shared.staff_view_as_session (admin_user_id, tenant_id, view_role, reason, expires_at) "
             "VALUES (%s, %s, 'content', 'both kinds', now() + interval '10 minutes')", (staff, tenant))
    _refused(cur, psycopg2.errors.CheckViolation,
             "INSERT INTO shared.staff_view_as_session (admin_user_id, reason, expires_at) "
             "VALUES (%s, 'neither kind', now() + interval '10 minutes')", (staff,))


@pytest.mark.parametrize("bad", ["Admin", "x", "custom-content", "1content", "a" * 64])
def test_view_role_format(conn, bad):
    cur = conn.cursor()
    staff = _staff(cur)
    _refused(cur, psycopg2.errors.CheckViolation,
             "INSERT INTO shared.staff_view_as_session (admin_user_id, view_role, reason, expires_at) "
             "VALUES (%s, %s, 'bad code', now() + interval '10 minutes')", (staff, bad))


def test_the_existing_rules_hold_for_role_rows(conn):
    cur = conn.cursor()
    staff = _staff(cur)
    for reason, minutes in (("Check what this role sees", 61), ("  ", 10)):
        _refused(cur, psycopg2.errors.CheckViolation,
                 "INSERT INTO shared.staff_view_as_session (admin_user_id, view_role, reason, expires_at) "
                 "VALUES (%s, 'content', %s, now() + make_interval(mins => %s))", (staff, reason, minutes))


def test_a_role_row_is_append_only_as_cis_app(conn):
    cur = conn.cursor()
    staff = _staff(cur)
    sid = _role_session(cur, staff)
    _as_cis_app(cur)
    for sql in ("UPDATE shared.staff_view_as_session SET view_role = 'admin' WHERE session_id = %s",
                "UPDATE shared.staff_view_as_session SET view_role = 'admin', ended_at = now(), end_reason = 'ended' "
                "WHERE session_id = %s",
                "UPDATE shared.staff_view_as_session SET tenant_id = gen_random_uuid(), view_role = NULL "
                "WHERE session_id = %s",
                "DELETE FROM shared.staff_view_as_session WHERE session_id = %s"):
        _refused(cur, psycopg2.errors.RestrictViolation, sql, (sid,))
    # ending it is the one UPDATE; once ended (but recent) it still cannot be deleted
    cur.execute("UPDATE shared.staff_view_as_session SET ended_at = now(), end_reason = 'ended' WHERE session_id = %s",
                (sid,))
    _refused(cur, psycopg2.errors.RestrictViolation,
             "DELETE FROM shared.staff_view_as_session WHERE session_id = %s", (sid,))
    # the erasure's statement shape (by tenant id) never reaches a role row
    cur.execute("DELETE FROM shared.staff_view_as_session t WHERE (t.tenant_id = %s::uuid)", (str(uuid.uuid4()),))
    assert cur.rowcount == 0


def test_an_ended_role_row_past_24_months_may_be_purged(conn):
    cur = conn.cursor()
    staff = _staff(cur)
    old = _role_session(cur, staff, started="now() - interval '25 months'")
    open_old = _role_session(cur, staff, started="now() - interval '26 months'")
    cur.execute("UPDATE shared.staff_view_as_session SET ended_at = started_at + interval '5 minutes', "
                "end_reason = 'ended' WHERE session_id = %s", (old,))
    _as_cis_app(cur)
    cur.execute("DELETE FROM shared.staff_view_as_session WHERE session_id = %s", (old,))
    assert cur.rowcount == 1
    # never ended (a crash before any close): kept, it is still evidence
    _refused(cur, psycopg2.errors.RestrictViolation,
             "DELETE FROM shared.staff_view_as_session WHERE session_id = %s", (open_old,))


def test_tenant_rows_behave_as_before(conn):
    cur = conn.cursor()
    staff, tenant = _staff(cur), _tenant(cur)
    cur.execute("INSERT INTO shared.staff_view_as_session (admin_user_id, tenant_id, reason, expires_at) "
                "VALUES (%s, %s, 'ticket follow-up', now() + interval '10 minutes') RETURNING session_id",
                (staff, tenant))
    sid = cur.fetchone()[0]
    _as_cis_app(cur)
    _refused(cur, psycopg2.errors.RestrictViolation,
             "DELETE FROM shared.staff_view_as_session WHERE session_id = %s", (sid,))
    _refused(cur, psycopg2.errors.RestrictViolation,
             "UPDATE shared.staff_view_as_session SET view_role = 'content' WHERE session_id = %s", (sid,))
    cur.execute("RESET ROLE")
    cur.execute("UPDATE shared.tenants SET is_active = false, cancelled_at = now() WHERE tenant_id = %s", (tenant,))
    _as_cis_app(cur)
    cur.execute("DELETE FROM shared.staff_view_as_session t WHERE (t.tenant_id = %s::uuid)", (tenant,))
    assert cur.rowcount == 1


def test_truncate_stays_refused(conn):
    cur = conn.cursor()
    _refused(cur, psycopg2.errors.RestrictViolation, "TRUNCATE shared.staff_view_as_session", None)


def test_guard_1_probe_runs_with_a_staff_row_and_leaves_nothing():
    """Guard 1 probes the guard with a throwaway staff-role row when the directory has a row; the probe is rolled
    back, so a re-apply leaves no session behind."""
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    staff = _staff(cur)
    try:
        cur.execute("SELECT count(*) FROM shared.staff_view_as_session")
        before = cur.fetchone()[0]
        _apply(MIG_470)
        cur.execute("SELECT count(*) FROM shared.staff_view_as_session")
        assert cur.fetchone()[0] == before
    finally:
        cur.execute("DELETE FROM shared.admin_users WHERE id = %s", (staff,))
        c.close()
