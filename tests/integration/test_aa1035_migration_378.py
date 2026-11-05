"""AA-1035 A10d — migration 378: the Dev-only E2E customer_success address in admin_users_email_chk, real Postgres.

The SQL runs for real, errors NOT swallowed. The module fixture starts from the state CIS 375 left (the 375 CHECK),
applies 378 and re-applies it (re-runnable). Then: the A10d address is accepted only as customer_success, not an
owner, without a password; the A10 / A10b rules and the company domain rule are unchanged; no other address of the
mailbox domain is accepted; the guards hold; and the file is a contract file without a directive.
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
MIG_375 = os.path.join(_MIG, "375_admin_users_reviewer_alias_removed.sql")
MIG_378 = os.path.join(_MIG, "378_admin_users_e2e_cis_cs_email.sql")
CS = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
CIS_ADMIN = "e2e-cis-admin@e2e-mail.lumiguides.it.com"
CONTENT = "e2e@e2e-mail.lumiguides.it.com"


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


def _staff(cur, role="customer_success", email=None, is_owner=False, password_hash=None):
    staff_id = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.admin_users (id, username, role, is_active, email, is_owner, password_hash) "
                "VALUES (%s, %s, %s, true, %s, %s, %s)",
                (staff_id, f"aa378-{staff_id[:8]}", role, email, is_owner, password_hash))
    return staff_id


def _refused(conn, sql, args=()):
    cur = conn.cursor()
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(sql, args)
    cur.execute("ROLLBACK TO SAVEPOINT s")


def _constraint_def(cur):
    cur.execute("SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
                "WHERE conrelid = 'shared.admin_users'::regclass AND conname = 'admin_users_email_chk'")
    return cur.fetchone()[0]


@pytest.fixture(scope="module", autouse=True)
def applied():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    cur.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa378-%%' OR email = %s", (CS,))
    # The state 375 left (375's guard 0 refuses stray reviewer rows another module may have left; they are not ours).
    cur.execute("DELETE FROM shared.admin_users WHERE role = 'reviewer'")
    _apply(MIG_375)
    assert CS not in _constraint_def(cur)
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '378'")
    _apply(MIG_378)
    _apply(MIG_378)           # re-runnable
    yield
    cur.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa378-%%' OR email = %s", (CS,))
    c.close()


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def test_registers_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '378'")
    assert cur.fetchone()[0] == 1


def test_is_a_contract_file_without_a_directive():
    text = open(MIG_378, encoding="utf-8").read()
    assert text.splitlines()[0].startswith("-- Migration 378")
    assert "-- migrate:" not in text
    assert text.count("BEGIN;") == 1 and text.count("COMMIT;") == 1


def test_the_cs_address_is_accepted_as_customer_success_only(conn):
    cur = conn.cursor()
    staff = _staff(cur, role="customer_success", email=CS)
    for sql in ("UPDATE shared.admin_users SET role = 'admin' WHERE id = %s",
                "UPDATE shared.admin_users SET role = 'support' WHERE id = %s",
                "UPDATE shared.admin_users SET role = 'content' WHERE id = %s",
                "UPDATE shared.admin_users SET is_owner = true WHERE id = %s",
                "UPDATE shared.admin_users SET password_hash = 'x' WHERE id = %s"):
        _refused(conn, sql, (staff,))


@pytest.mark.parametrize("role", ["admin", "support", "content", "finance", "tenant_qa"])
def test_the_cs_address_is_refused_with_any_other_role(conn, role):
    cur = conn.cursor()
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _staff(cur, role=role, email=CS)
    cur.execute("ROLLBACK TO SAVEPOINT s")


@pytest.mark.parametrize("email", [
    "e2e-cis-cs2@e2e-mail.lumiguides.it.com",
    "E2E-CIS-CS@e2e-mail.lumiguides.it.com",
    "e2e-cis-cs@lumiguides.it.com",
    "someone@e2e-mail.lumiguides.it.com",
])
def test_no_other_mailbox_address_is_accepted(conn, email):
    cur = conn.cursor()
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _staff(cur, role="customer_success", email=email)
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_the_earlier_rules_are_unchanged(conn):
    cur = conn.cursor()
    _staff(cur, role="customer_success", email=f"aa378-{uuid.uuid4().hex[:6]}@adventure.asia")
    _staff(cur, role="admin", email=f"aa378-{uuid.uuid4().hex[:6]}@adventure.asia", is_owner=True)
    cur.execute("SAVEPOINT a")
    cis_admin = _staff(cur, role="admin", email=CIS_ADMIN)
    _refused(conn, "UPDATE shared.admin_users SET role = 'customer_success' WHERE id = %s", (cis_admin,))
    cur.execute("ROLLBACK TO SAVEPOINT a")
    cur.execute("SAVEPOINT b")
    content = _staff(cur, role="content", email=CONTENT)
    _refused(conn, "UPDATE shared.admin_users SET role = 'customer_success' WHERE id = %s", (content,))
    cur.execute("ROLLBACK TO SAVEPOINT b")
    _refused(conn, "INSERT INTO shared.admin_users (id, username, role, is_active, email) "
                   "VALUES (gen_random_uuid(), 'aa378-x', 'support', true, 'aa378-x@gmail.com')")


def test_the_constraint_definition_and_comment():
    c = _connect()
    try:
        cur = c.cursor()
        definition = _constraint_def(cur)
        cur.execute("SELECT obj_description(oid, 'pg_constraint') AS note FROM pg_constraint "
                    "WHERE conrelid = 'shared.admin_users'::regclass AND conname = 'admin_users_email_chk'")
        note = cur.fetchone()[0]
    finally:
        c.close()
    for needle in (CS, CIS_ADMIN, CONTENT, "'customer_success'", "adventure[.]asia"):
        assert needle in definition
    assert "reviewer" not in definition
    assert "378" in note and "A10d" in note and CS in note


def test_guard_0_refuses_a_runner_that_does_not_own_the_table():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    role = f"aa378_norole_{uuid.uuid4().hex[:6]}"
    try:
        cur.execute(f"CREATE ROLE {role} NOLOGIN")
        cur.execute(f"GRANT USAGE ON SCHEMA shared TO {role}")
        cur.execute(f"GRANT SELECT, INSERT ON shared.schema_versions TO {role}")
        sql = open(MIG_378, encoding="utf-8").read().replace("BEGIN;", f"BEGIN;\nSET LOCAL ROLE {role};", 1)
        with pytest.raises(psycopg2.errors.RaiseException, match="not by the runner"):
            cur.execute(sql)
        cur.execute("ROLLBACK")
    finally:
        cur.execute(f"REVOKE ALL ON shared.schema_versions FROM {role}")
        cur.execute(f"REVOKE ALL ON SCHEMA shared FROM {role}")
        cur.execute(f"DROP ROLE IF EXISTS {role}")
        c.close()
