"""AA-1035 (R2 slice 1) — migrations 362 (admin_role values), 363 (reviewer merge, CHECK fixes) and 364 (staff tenant
assignment + view-as log) against real Postgres.

The SQL runs for real, errors NOT swallowed: the module fixture re-applies the three files in number order (each brings
its own BEGIN … COMMIT), so it also proves they are re-runnable. Another module (283's test) may have put back the 283
CHECK before this one runs; re-applying 363 restores the R2 rule.
"""
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
FILES = [
    os.path.join(_MIG, "362_admin_role_r2_staff_roles.sql"),
    os.path.join(_MIG, "363_staff_roles_reviewer_merge_finance_grant.sql"),
    os.path.join(_MIG, "364_staff_tenant_assignment.sql"),
]
# CIS 375 (the alias removed) forbids 'reviewer' rows; this module tests the state 363 left, so it drops that CHECK
# first and re-applies 375 at the end (tests/integration/test_aa1035_migration_375.py tests 375 itself).
MIG_375 = os.path.join(_MIG, "375_admin_users_reviewer_alias_removed.sql")
DROP_ALIAS_CHK = "ALTER TABLE shared.admin_users DROP CONSTRAINT IF EXISTS admin_users_role_not_alias_chk"
E2E = "e2e@e2e-mail.lumiguides.it.com"
NEW_ROLES = ("content_lead", "tenant_qa", "customer_success", "support", "finance", "ai_ops")
DEV_DEFAULT_ACL = "ALTER DEFAULT PRIVILEGES IN SCHEMA shared GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cis_app"
DEV_DEFAULT_ACL_UNDO = "ALTER DEFAULT PRIVILEGES IN SCHEMA shared REVOKE ALL ON TABLES FROM cis_app"


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


def _staff(cur, role="content", email=None):
    staff_id = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.admin_users (id, username, role, is_active, email) "
                "VALUES (%s, %s, %s, true, %s)", (staff_id, f"aa1035-{staff_id[:8]}", role, email))
    return staff_id


def _tenant(cur):
    tenant_id = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week) "
                "VALUES (%s, %s, %s, 'starter', 3)",
                (tenant_id, "Zephyr Lantern Tours", f"zephyr-{tenant_id[:8]}"))
    return tenant_id


@pytest.fixture(scope="module", autouse=True)
def applied():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    cur.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa1035-%%' OR email = %s", (E2E,))
    # Rows that 363 must merge (a company reviewer) and keep (the A10 E2E reviewer), written under the 283 rule.
    cur.execute("ALTER TABLE shared.admin_users DROP CONSTRAINT IF EXISTS admin_users_email_chk")
    cur.execute(DROP_ALIAS_CHK)
    merged = _staff(cur, role="reviewer", email=f"aa1035-{uuid.uuid4().hex[:6]}@adventure.asia")
    e2e = _staff(cur, role="reviewer", email=E2E)
    cur.execute("DELETE FROM shared.schema_versions WHERE version IN ('362', '363', '364')")
    # The Dev default ACL of the migration runner in shared (CIS 245: cis_app DML on every new table) applies when 364
    # creates its tables, so the narrowed grants are proven against it, not against an empty ACL (CI has no 245).
    cur.execute("DROP TABLE IF EXISTS shared.staff_view_as_session, shared.staff_tenant_assignment")
    cur.execute(DEV_DEFAULT_ACL)
    try:
        for path in FILES:
            _apply(path)
        for path in FILES:          # re-runnable
            _apply(path)
    finally:
        cur.execute(DEV_DEFAULT_ACL_UNDO)
    yield {"merged": merged, "e2e": e2e}
    cur.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa1035-%%'")
    cur.execute("DELETE FROM shared.tenants WHERE slug LIKE 'zephyr-%%'")
    _apply(MIG_375)   # back to the current rule (the E2E row, if any, becomes content)
    c.close()


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def test_each_file_registers_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT version, count(*) AS n FROM shared.schema_versions "
                "WHERE version IN ('362', '363', '364') GROUP BY version ORDER BY version")
    assert cur.fetchall() == [("362", 1), ("363", 1), ("364", 1)]


def test_admin_role_has_the_six_r2_values(conn):
    cur = conn.cursor()
    cur.execute("SELECT enumlabel FROM pg_enum WHERE enumtypid = 'shared.admin_role'::regtype")
    labels = {r[0] for r in cur.fetchall()}
    assert set(NEW_ROLES) | {"admin", "reviewer", "content", "viewer"} == labels


def test_the_three_files_are_contract_without_a_directive():
    # 364 needs REVOKE ALL (the Dev default ACL) and CREATE OR REPLACE, which the expand lint refuses (as CIS 353).
    for path, num in zip(FILES, ("362", "363", "364")):
        text = open(path, encoding="utf-8").read()
        assert text.splitlines()[0].startswith(f"-- Migration {num}")
        assert "-- migrate:" not in text


def test_reviewers_merge_into_content_but_the_e2e_row_stays(conn, applied):
    cur = conn.cursor()
    cur.execute("SELECT id::text, role::text FROM shared.admin_users WHERE id = ANY(%s::uuid[])",
                ([applied["merged"], applied["e2e"]],))
    roles = dict(cur.fetchall())
    assert roles == {applied["merged"]: "content", applied["e2e"]: "reviewer"}


def test_role_default_is_content(conn):
    cur = conn.cursor()
    cur.execute("SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = 'shared' AND table_name = 'admin_users' AND column_name = 'role'")
    assert cur.fetchone()[0].startswith("'content'")


@pytest.mark.parametrize("role", NEW_ROLES)
def test_company_rows_take_every_new_role(conn, role):
    _staff(conn.cursor(), role=role, email=f"aa1035-{uuid.uuid4().hex[:6]}@adventure.asia")


def test_the_e2e_address_may_be_content_after_the_alias(conn, applied):
    cur = conn.cursor()
    cur.execute("UPDATE shared.admin_users SET role = 'content' WHERE id = %s", (applied["e2e"],))


@pytest.mark.parametrize("role", ["admin", "customer_success", "finance"])
def test_the_e2e_address_keeps_its_narrow_scope(conn, applied, role):
    cur = conn.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.admin_users SET role = %s WHERE id = %s", (role, applied["e2e"]))


def test_the_booking_grant_takes_finance_and_refuses_others(conn):
    cur = conn.cursor()
    staff = _staff(cur)
    cur.execute("INSERT INTO shared.staff_app_grant (admin_user_id, app, role) VALUES (%s, 'booking', 'finance')",
                (staff,))
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.staff_app_grant SET role = 'accountant' WHERE admin_user_id = %s", (staff,))
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_one_owner_per_tenant(conn):
    cur = conn.cursor()
    tenant, a, b = _tenant(cur), _staff(cur), _staff(cur)
    cur.execute("INSERT INTO shared.staff_tenant_assignment (admin_user_id, tenant_id, kind, created_by) "
                "VALUES (%s, %s, 'owner', 'admin:test')", (a, tenant))
    cur.execute("INSERT INTO shared.staff_tenant_assignment (admin_user_id, tenant_id, kind, created_by) "
                "VALUES (%s, %s, 'backup', 'admin:test')", (b, tenant))
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("UPDATE shared.staff_tenant_assignment SET kind = 'owner' WHERE admin_user_id = %s", (b,))
    cur.execute("ROLLBACK TO SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.staff_tenant_assignment SET kind = 'manager' WHERE admin_user_id = %s", (b,))


def test_assignments_go_with_the_tenant_and_the_person(conn):
    cur = conn.cursor()
    tenant, staff = _tenant(cur), _staff(cur)
    cur.execute("INSERT INTO shared.staff_tenant_assignment (admin_user_id, tenant_id, kind, created_by) "
                "VALUES (%s, %s, 'support', 'admin:test')", (staff, tenant))
    cur.execute("DELETE FROM shared.admin_users WHERE id = %s", (staff,))
    cur.execute("SELECT count(*) FROM shared.staff_tenant_assignment WHERE tenant_id = %s", (tenant,))
    assert cur.fetchone()[0] == 0


def test_view_as_window_is_at_most_60_minutes_and_needs_a_reason(conn):
    cur = conn.cursor()
    tenant, staff = _tenant(cur), _staff(cur)
    cur.execute("INSERT INTO shared.staff_view_as_session (admin_user_id, tenant_id, reason, expires_at) "
                "VALUES (%s, %s, 'ticket follow-up', now() + interval '60 minutes')", (staff, tenant))
    for reason, minutes in (("ticket follow-up", 61), ("  ", 10)):
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("INSERT INTO shared.staff_view_as_session (admin_user_id, tenant_id, reason, expires_at) "
                        "VALUES (%s, %s, %s, now() + make_interval(mins => %s))", (staff, tenant, reason, minutes))
        cur.execute("ROLLBACK TO SAVEPOINT s")
    # The log outlives nothing it should not: a staff row with a session can not be deleted (RESTRICT).
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("DELETE FROM shared.admin_users WHERE id = %s", (staff,))


def test_runtime_grants(conn):
    """Narrowed although the default ACL of the runner gave cis_app every DML right (fixture): REVOKE ALL first."""
    cur = conn.cursor()
    cur.execute(
        "SELECT has_table_privilege('cis_app', 'shared.staff_tenant_assignment', 'SELECT') AS a_sel, "
        "has_table_privilege('cis_app', 'shared.staff_tenant_assignment', 'INSERT') AS a_ins, "
        "has_table_privilege('cis_app', 'shared.staff_tenant_assignment', 'DELETE') AS a_del, "
        "has_table_privilege('cis_app', 'shared.staff_tenant_assignment', 'UPDATE') AS a_upd, "
        "has_table_privilege('cis_app', 'shared.staff_view_as_session', 'INSERT') AS v_ins, "
        "has_table_privilege('cis_app', 'shared.staff_view_as_session', 'UPDATE') AS v_upd, "
        "has_table_privilege('cis_app', 'shared.staff_view_as_session', 'DELETE') AS v_del, "
        "has_table_privilege('cis_app', 'shared.staff_view_as_session', 'TRUNCATE') AS v_trunc")
    assert cur.fetchone() == (True, True, True, False, True, True, True, False)
    cur.execute("SELECT count(*) FROM information_schema.role_table_grants WHERE table_schema = 'shared' "
                "AND table_name IN ('staff_tenant_assignment', 'staff_view_as_session') "
                "AND grantee IN ('PUBLIC', 'aa_app_user', 'catalog_owner')")
    assert cur.fetchone()[0] == 0


def _session(cur, staff, tenant):
    cur.execute("INSERT INTO shared.staff_view_as_session (admin_user_id, tenant_id, reason, expires_at) "
                "VALUES (%s, %s, 'invented ticket follow-up', now() + interval '30 minutes') RETURNING session_id",
                (staff, tenant))
    return cur.fetchone()[0]


def _as_cis_app(cur):
    """Run the rest of the transaction as cis_app (rolled back by the conn fixture). USAGE, the tenants read (Dev: CIS
    244) and the 353 helper are granted in the transaction: CI has no 244, and x1_244_undo revokes them."""
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    cur.execute("GRANT SELECT ON shared.tenants TO cis_app")
    cur.execute("GRANT EXECUTE ON FUNCTION shared.tenant_account_closed(uuid) TO cis_app")
    cur.execute("SET LOCAL ROLE cis_app")


def test_the_view_as_log_is_append_only(conn):
    cur = conn.cursor()
    tenant, staff = _tenant(cur), _staff(cur)
    sid = _session(cur, staff, tenant)
    _as_cis_app(cur)
    for sql in ("UPDATE shared.staff_view_as_session SET reason = 'rewritten reason' WHERE session_id = %s",
                "UPDATE shared.staff_view_as_session SET expires_at = expires_at - interval '1 minute' "
                "WHERE session_id = %s",
                "DELETE FROM shared.staff_view_as_session WHERE session_id = %s"):
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.RestrictViolation):
            cur.execute(sql, (sid,))
        cur.execute("ROLLBACK TO SAVEPOINT s")
    # Ending an open session is the one UPDATE; a second end is refused.
    cur.execute("UPDATE shared.staff_view_as_session SET ended_at = now(), end_reason = 'ended' WHERE session_id = %s",
                (sid,))
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RestrictViolation):
        cur.execute("UPDATE shared.staff_view_as_session SET end_reason = 'revoked' WHERE session_id = %s", (sid,))
    cur.execute("ROLLBACK TO SAVEPOINT s")
    cur.execute("RESET ROLE")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RestrictViolation):
        cur.execute("TRUNCATE shared.staff_view_as_session")
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_the_erasure_deletes_as_cis_app_once_the_tenant_is_offboarded(conn):
    """AA-839 runs `DELETE FROM <table> t WHERE (t.tenant_id = $1::uuid)` for every delete node as cis_app, rows or
    not (Postgres checks DELETE before it looks at rows): both tables must take it for an offboarded tenant."""
    cur = conn.cursor()
    tenant, staff = _tenant(cur), _staff(cur)
    _session(cur, staff, tenant)
    cur.execute("INSERT INTO shared.staff_tenant_assignment (admin_user_id, tenant_id, kind, created_by) "
                "VALUES (%s, %s, 'owner', 'admin:test')", (staff, tenant))
    cur.execute("UPDATE shared.tenants SET is_active = false, cancelled_at = now() WHERE tenant_id = %s", (tenant,))
    _as_cis_app(cur)
    for table in ("shared.staff_view_as_session", "shared.staff_tenant_assignment"):
        cur.execute(f"DELETE FROM {table} t WHERE (t.tenant_id = %s::uuid)", (tenant,))
        assert cur.rowcount == 1, table
    cur.execute("RESET ROLE")
    # The tenant row itself then goes (here as the superuser); the FK cascade finds nothing left.
    cur.execute("DELETE FROM shared.tenants WHERE tenant_id = %s", (tenant,))


def test_a_deleted_tenant_cascades_through_the_guard(conn):
    cur = conn.cursor()
    tenant, staff = _tenant(cur), _staff(cur)
    _session(cur, staff, tenant)
    cur.execute("DELETE FROM shared.tenants WHERE tenant_id = %s", (tenant,))
    cur.execute("SELECT count(*) FROM shared.staff_view_as_session WHERE tenant_id = %s", (tenant,))
    assert cur.fetchone()[0] == 0


def test_every_inventory_delete_table_is_deletable_by_cis_app(conn):
    """The erasure apply DELETEs every delete node even with 0 rows: a table without DELETE for cis_app fails it."""
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from services.erasure.inventory import TENANT_TABLES

    cur = conn.cursor()
    for table in ("shared.staff_tenant_assignment", "shared.staff_view_as_session"):
        entry = next(t for t in TENANT_TABLES if t.table == table)
        assert entry.action == "delete"
        cur.execute("SELECT has_table_privilege('cis_app', %s, 'DELETE') AS ok", (table,))
        assert cur.fetchone()[0] is True, table


def test_the_new_tables_are_in_the_erasure_inventory():
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from services.erasure.inventory import TENANT_TABLES

    names = {t.table for t in TENANT_TABLES}
    assert {"shared.staff_tenant_assignment", "shared.staff_view_as_session"} <= names
