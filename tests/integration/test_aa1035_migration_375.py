"""AA-1035 (R2 close-out) — migration 375: the reviewer alias is removed against real Postgres.

The SQL runs for real, errors NOT swallowed. The module fixture starts from the state CIS 363 left (the A10 E2E row as
'reviewer', the 363 CHECK, no alias CHECK), applies 375 and re-applies it (re-runnable). Then: the E2E row is content,
the E2E address is refused as reviewer, no row may be reviewer again, guard 0 refuses a company reviewer row written
after 363, and the file is a contract file without a directive.
"""
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
MIG_363 = os.path.join(_MIG, "363_staff_roles_reviewer_merge_finance_grant.sql")
MIG_375 = os.path.join(_MIG, "375_admin_users_reviewer_alias_removed.sql")
E2E = "e2e@e2e-mail.lumiguides.it.com"
DROP_ALIAS_CHK = "ALTER TABLE shared.admin_users DROP CONSTRAINT IF EXISTS admin_users_role_not_alias_chk"


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
                "VALUES (%s, %s, %s, true, %s)", (staff_id, f"aa375-{staff_id[:8]}", role, email))
    return staff_id


def _refused(conn, sql, args=()):
    cur = conn.cursor()
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(sql, args)
    cur.execute("ROLLBACK TO SAVEPOINT s")


@pytest.fixture(scope="module", autouse=True)
def applied():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    cur.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa375-%%' OR email = %s", (E2E,))
    # The state 363 left: no alias CHECK, the 363 email rule, the A10 E2E row still 'reviewer'. Other modules may have
    # left stray test reviewer rows behind; they are not this module's and 375's guard 0 would refuse them.
    cur.execute(DROP_ALIAS_CHK)
    cur.execute("DELETE FROM shared.admin_users WHERE role = 'reviewer'")
    _apply(MIG_363)
    e2e = _staff(cur, role="reviewer", email=E2E)
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '375'")
    _apply(MIG_375)
    _apply(MIG_375)           # re-runnable
    yield {"e2e": e2e}
    cur.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa375-%%'")
    c.close()


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def test_registers_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '375'")
    assert cur.fetchone()[0] == 1


def test_is_a_contract_file_without_a_directive():
    text = open(MIG_375, encoding="utf-8").read()
    assert text.splitlines()[0].startswith("-- Migration 375")
    assert "-- migrate:" not in text
    assert text.count("BEGIN;") == 1 and text.count("COMMIT;") == 1


def test_the_e2e_row_is_content(conn, applied):
    cur = conn.cursor()
    cur.execute("SELECT role::text FROM shared.admin_users WHERE id = %s", (applied["e2e"],))
    assert cur.fetchone()[0] == "content"


def test_no_row_may_be_reviewer_again(conn, applied):
    _refused(conn, "UPDATE shared.admin_users SET role = 'reviewer' WHERE id = %s", (applied["e2e"],))
    _refused(conn, "INSERT INTO shared.admin_users (id, username, role, is_active, email) "
                   "VALUES (gen_random_uuid(), 'aa375-company', 'reviewer', true, 'aa375-x@adventure.asia')")
    # content, and every other role, is still fine for a company address
    _staff(conn.cursor(), role="content", email=f"aa375-{uuid.uuid4().hex[:6]}@adventure.asia")
    _staff(conn.cursor(), role="tenant_qa", email=f"aa375-{uuid.uuid4().hex[:6]}@adventure.asia")


def test_the_e2e_address_keeps_its_narrow_scope(conn, applied):
    for sql in ("UPDATE shared.admin_users SET role = 'admin' WHERE id = %s",
                "UPDATE shared.admin_users SET is_owner = true WHERE id = %s",
                "UPDATE shared.admin_users SET password_hash = 'x' WHERE id = %s"):
        _refused(conn, sql, (applied["e2e"],))


def test_the_constraints_are_commented():
    c = _connect()
    try:
        cur = c.cursor()
        cur.execute("SELECT conname, obj_description(oid, 'pg_constraint') FROM pg_constraint "
                    "WHERE conrelid = 'shared.admin_users'::regclass "
                    "AND conname IN ('admin_users_email_chk', 'admin_users_role_not_alias_chk')")
        notes = dict(cur.fetchall())
    finally:
        c.close()
    assert "375" in notes["admin_users_email_chk"] and "as content" in notes["admin_users_email_chk"]
    assert "enum value" in notes["admin_users_role_not_alias_chk"]


def test_guard_0_refuses_a_company_reviewer_row():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    staff = None
    try:
        cur.execute(DROP_ALIAS_CHK)
        staff = _staff(cur, role="reviewer", email=f"aa375-{uuid.uuid4().hex[:6]}@adventure.asia")
        with pytest.raises(psycopg2.errors.RaiseException, match="other than the A10 E2E account"):
            _apply(MIG_375)
    finally:
        if staff:
            cur.execute("DELETE FROM shared.admin_users WHERE id = %s", (staff,))
        _apply(MIG_375)       # back to the applied state
        c.close()
