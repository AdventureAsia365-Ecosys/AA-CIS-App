"""AA-1176 A10h — migration 473: the Dev-only E2E owner address in admin_users_email_chk, real Postgres.

The SQL runs for real, errors NOT swallowed. The module fixture starts from the state CIS 462 left (it re-applies 462,
whose CHECK another module may also have put back), applies 473 and re-applies it (re-runnable). Then: the A10h address
is accepted only as an admin without a custom role, never the business owner, without a password, with the owner marker
on or off; the earlier E2E rules and the company domain rule are unchanged; no other address of the mailbox domain is
accepted; and the file is a contract file without a directive. Invented rows only; every test rolls back.
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
MIG_462 = os.path.join(_MIG, "462_aa1155_custom_staff_roles.sql")
MIG_473 = os.path.join(_MIG, "473_aa1176_admin_users_e2e_owner_email.sql")
OWNER = "e2e-owner@e2e-mail.lumiguides.it.com"
CIS_ADMIN = "e2e-cis-admin@e2e-mail.lumiguides.it.com"
CS = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
CONTENT = "e2e@e2e-mail.lumiguides.it.com"
CONTENT_LEAD = "e2e-cis-content-lead@e2e-mail.lumiguides.it.com"
MAKER = "e2e-custom-maker@e2e-mail.lumiguides.it.com"


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


def _staff(cur, role="admin", email=None, *, is_owner=False, business_owner=False, custom_role=None,
           password_hash=None):
    staff_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO shared.admin_users (id, username, role, is_active, email, is_owner, is_business_owner, "
        "custom_role, password_hash) VALUES (%s, %s, %s, true, %s, %s, %s, %s, %s)",
        (staff_id, f"aa1176-{staff_id[:8]}", role, email, is_owner, business_owner, custom_role, password_hash))
    return staff_id


def _refused(conn, fn):
    cur = conn.cursor()
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        fn(cur)
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
    cur.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa1176-%%' OR email = %s", (OWNER,))
    _apply(MIG_462)                      # the 462 state (no A10h branch)
    assert OWNER not in _constraint_def(cur)
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '473'")
    _apply(MIG_473)
    _apply(MIG_473)                      # re-runnable
    yield
    cur.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa1176-%%' OR email = %s", (OWNER,))
    c.close()


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def test_registers_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '473'")
    assert cur.fetchone()[0] == 1


def test_is_a_contract_file_without_a_directive():
    text = open(MIG_473, encoding="utf-8").read()
    assert text.splitlines()[0].startswith("-- Migration 473")
    assert "-- migrate:" not in text
    assert text.count("BEGIN;") == 1 and text.count("COMMIT;") == 1


def test_the_462_branches_are_kept_byte_for_byte():
    """The 473 CHECK is the 462 CHECK plus one branch: every 462 line of the ADD CONSTRAINT body is in 473 unchanged."""
    def body(path):
        text = open(path, encoding="utf-8").read()
        start = text.index("ADD CONSTRAINT admin_users_email_chk CHECK (")
        return text[start:text.index(";", start)]
    old, new = body(MIG_462), body(MIG_473)
    assert new.startswith(old[:-1])     # 462 ends "IS NULL))": only the CHECK's own closing parenthesis moves
    assert new.count("OR (email = ") == old.count("OR (email = ") + 1


@pytest.mark.parametrize("is_owner", [True, False])
def test_the_owner_address_is_accepted_as_an_admin_with_or_without_the_owner_marker(conn, is_owner):
    cur = conn.cursor()
    staff = _staff(cur, role="admin", email=OWNER, is_owner=is_owner)
    cur.execute("UPDATE shared.admin_users SET is_owner = NOT is_owner WHERE id = %s", (staff,))
    cur.execute("SELECT is_owner FROM shared.admin_users WHERE id = %s", (staff,))
    assert cur.fetchone()[0] is (not is_owner)


def test_the_owner_address_never_becomes_the_business_owner_has_a_password_or_a_custom_role(conn):
    cur = conn.cursor()
    staff = _staff(cur, role="admin", email=OWNER, is_owner=True)
    for sql in ("UPDATE shared.admin_users SET is_business_owner = true WHERE id = %s",
                "UPDATE shared.admin_users SET password_hash = 'x' WHERE id = %s",
                "UPDATE shared.admin_users SET role = 'content_lead' WHERE id = %s",
                "UPDATE shared.admin_users SET role = 'content_lead', custom_role = 'custom_content' WHERE id = %s"):
        _refused(conn, lambda c, s=sql: c.execute(s, (staff,)))


@pytest.mark.parametrize("kwargs", [
    {"role": "admin", "business_owner": True},
    {"role": "admin", "business_owner": True, "is_owner": True},
    {"role": "admin", "password_hash": "x"},
    {"role": "content_lead"},
    {"role": "content_lead", "custom_role": "custom_content"},
    {"role": "customer_success"},
    {"role": "content"},
    {"role": "finance", "is_owner": True},
])
def test_the_owner_address_is_refused_outside_its_scope(conn, kwargs):
    _refused(conn, lambda c: _staff(c, email=OWNER, **kwargs))


@pytest.mark.parametrize("email", [
    "e2e-owner2@e2e-mail.lumiguides.it.com",
    "E2E-OWNER@e2e-mail.lumiguides.it.com",
    "e2e-owner@e2e-mail.lumiguides.it.com.",
    "e2e-owner@x.e2e-mail.lumiguides.it.com",
    "e2e-owner@lumiguides.it.com",
    "owner@e2e-mail.lumiguides.it.com",
])
def test_no_other_mailbox_address_is_accepted(conn, email):
    _refused(conn, lambda c: _staff(c, role="admin", email=email, is_owner=True))


@pytest.mark.parametrize("email,role", [(CIS_ADMIN, "admin"), (CS, "customer_success"), (CONTENT, "content"),
                                        (CONTENT_LEAD, "content_lead"), (MAKER, "content_lead")])
def test_the_other_e2e_addresses_still_never_hold_the_owner_marker(conn, email, role):
    _refused(conn, lambda c: _staff(c, role=role, email=email, is_owner=True))


def test_the_earlier_rules_are_unchanged(conn):
    cur = conn.cursor()
    _staff(cur, role="customer_success", email=f"aa1176-{uuid.uuid4().hex[:6]}@adventure.asia")
    _staff(cur, role="admin", email=f"aa1176-{uuid.uuid4().hex[:6]}@adventure.asia", is_owner=True)
    _staff(cur, role="admin", email=None)
    cur.execute("DELETE FROM shared.admin_users WHERE email IN (%s, %s)", (CIS_ADMIN, CS))
    _staff(cur, role="admin", email=CIS_ADMIN)
    _staff(cur, role="customer_success", email=CS)
    _refused(conn, lambda c: _staff(c, role="admin", email="someone@gmail.com"))
    _refused(conn, lambda c: _staff(c, role="admin", email="Upper@adventure.asia"))


def test_the_constraint_names_the_owner_branch_and_the_comment_says_temporary(conn):
    cur = conn.cursor()
    d = _constraint_def(cur)
    assert f"'{OWNER}'" in d and "reviewer" not in d and "adventure[.]asia" in d
    owner_branch = d.split(f"'{OWNER}'", 1)[1].split(" OR ", 1)[0]
    assert "NOT is_business_owner" in owner_branch and "password_hash IS NULL" in owner_branch
    assert "custom_role IS NULL" in owner_branch and "NOT is_owner" not in owner_branch
    cur.execute("SELECT obj_description(oid, 'pg_constraint') FROM pg_constraint "
                "WHERE conrelid = 'shared.admin_users'::regclass AND conname = 'admin_users_email_chk'")
    comment = cur.fetchone()[0]
    assert "TEMPORARY (473, A10h, Dev only by code)" in comment and "None the business owner" in comment
