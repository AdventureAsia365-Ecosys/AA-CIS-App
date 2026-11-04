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
E2E = "e2e@e2e-mail.lumiguides.it.com"
NEW_ROLES = ("content_lead", "tenant_qa", "customer_success", "support", "finance", "ai_ops")


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
    merged = _staff(cur, role="reviewer", email=f"aa1035-{uuid.uuid4().hex[:6]}@adventure.asia")
    e2e = _staff(cur, role="reviewer", email=E2E)
    cur.execute("DELETE FROM shared.schema_versions WHERE version IN ('362', '363', '364')")
    for path in FILES:
        _apply(path)
    for path in FILES:          # re-runnable
        _apply(path)
    yield {"merged": merged, "e2e": e2e}
    cur.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa1035-%%'")
    cur.execute("DELETE FROM shared.tenants WHERE slug LIKE 'zephyr-%%'")
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


def test_362_has_no_expand_directive_and_364_has_one():
    heads = [open(p, encoding="utf-8").readline().strip() for p in FILES]
    assert heads[0].startswith("-- Migration 362") and heads[1].startswith("-- Migration 363")
    assert heads[2] == "-- migrate: expand"


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
    cur = conn.cursor()
    cur.execute(
        "SELECT has_table_privilege('cis_app', 'shared.staff_tenant_assignment', 'SELECT') AS a_sel, "
        "has_table_privilege('cis_app', 'shared.staff_tenant_assignment', 'DELETE') AS a_del, "
        "has_table_privilege('cis_app', 'shared.staff_tenant_assignment', 'UPDATE') AS a_upd, "
        "has_table_privilege('cis_app', 'shared.staff_view_as_session', 'UPDATE') AS v_upd, "
        "has_table_privilege('cis_app', 'shared.staff_view_as_session', 'DELETE') AS v_del")
    assert cur.fetchone() == (True, True, False, True, False)


def test_the_new_tables_are_in_the_erasure_inventory():
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from services.erasure.inventory import TENANT_TABLES

    names = {t.table for t in TENANT_TABLES}
    assert {"shared.staff_tenant_assignment", "shared.staff_view_as_session"} <= names
