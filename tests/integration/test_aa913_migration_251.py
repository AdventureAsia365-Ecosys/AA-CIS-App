"""AA-913 — migration 251 (shared.alert_recipient) against real Postgres.

The SQL runs for real (lesson "a green unit test does not prove the query runs"):
- conftest applied every migration, so the table exists, 251 is registered and RLS is off;
- the CHECK constraints and the partial unique index hold (one active row per address; removed rows are history);
- logged in AS cis_app (the CIS runtime role, X1): SELECT / INSERT / UPDATE work, DELETE and TRUNCATE are refused;
  aa_app_user, tripplanner and booking hold nothing;
- re-applying the file is a no-op.
"""
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import CIS_APP_CI_PASSWORD, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "251_alert_recipient.sql")
PREFIX = "zz913-"
ACTOR = "admin:00000000-0000-0000-0000-000000000913"


def _sql() -> str:
    with open(MIGRATION) as f:
        return f.read()


def _connect(user=DB_USER, password=DB_PASS):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=password)


def _email(tag: str) -> str:
    return f"{PREFIX}{tag}-{uuid.uuid4().hex[:6]}@example.com"


@pytest.fixture
def su():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    yield cur
    cur.execute("DELETE FROM shared.alert_recipient WHERE email LIKE %s", (PREFIX + "%",))
    cur.close()
    c.close()


def _role_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return cur.fetchone() is not None


@pytest.fixture
def app(su):
    """A connection logged in AS cis_app. The replay skips CIS 244, so schema USAGE is granted here for the test
    only (and taken back) when cis_app lacks it. The table grant from 251 is restored the same way: conftest's
    x1_244_undo (run by the AA-860 modules that sort before this one) revokes every table in `shared` from
    cis_app, including this one."""
    if not _role_exists(su, "cis_app"):
        pytest.skip("role cis_app does not exist on this test DB (conftest creates it on a superuser replay)")
    su.execute("SELECT has_schema_privilege('cis_app', 'shared', 'USAGE')")
    granted = not su.fetchone()[0]
    if granted:
        su.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    su.execute("SELECT has_table_privilege('cis_app', 'shared.alert_recipient', 'SELECT, INSERT, UPDATE')")
    table_granted = not su.fetchone()[0]
    if table_granted:
        su.execute("GRANT SELECT, INSERT, UPDATE ON shared.alert_recipient TO cis_app")
    try:
        c = _connect("cis_app", CIS_APP_CI_PASSWORD)
    except psycopg2.OperationalError as e:
        if granted:
            su.execute("REVOKE USAGE ON SCHEMA shared FROM cis_app")
        if table_granted:
            su.execute("REVOKE SELECT, INSERT, UPDATE ON shared.alert_recipient FROM cis_app")
        pytest.skip(f"cannot log in as cis_app here: {e}")
    c.autocommit = True
    cur = c.cursor()
    try:
        yield cur
    finally:
        cur.close()
        c.close()
        if granted:
            su.execute("REVOKE USAGE ON SCHEMA shared FROM cis_app")
        if table_granted:
            su.execute("REVOKE SELECT, INSERT, UPDATE ON shared.alert_recipient FROM cis_app")


def _insert(cur, email, status="pending", **kw):
    cols = {"email": email, "status": status, "created_by": ACTOR, **kw}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    cur.execute(f"INSERT INTO shared.alert_recipient ({names}) VALUES ({marks}) RETURNING id", list(cols.values()))
    return cur.fetchone()[0]


# ── shape ────────────────────────────────────────────────────────────────────────────────────────

def test_table_exists_registered_and_rls_off(su):
    su.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'shared.alert_recipient'::regclass")
    assert su.fetchone() == (False, False)
    su.execute("SELECT description FROM shared.schema_versions WHERE version = '251'")
    row = su.fetchone()
    assert row and "AA-913" in row[0]
    su.execute("SELECT col_description('shared.alert_recipient'::regclass, attnum) FROM pg_attribute "
               "WHERE attrelid = 'shared.alert_recipient'::regclass AND attname = 'email'")
    assert su.fetchone()[0].startswith("pii:contact")


def test_defaults(su):
    rid = _insert(su, _email("defaults"))
    su.execute("SELECT status, created_at IS NOT NULL, updated_at IS NOT NULL, removed_at FROM shared.alert_recipient "
               "WHERE id = %s", (rid,))
    assert su.fetchone() == ("pending", True, True, None)


def test_one_active_row_per_address(su):
    email = _email("dup")
    _insert(su, email)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert(su, email, status="confirmed")


def test_removed_rows_are_history_and_the_address_can_come_back(su):
    email = _email("again")
    _insert(su, email, status="removed", removed_reason="admin", removed_at="2026-10-20T00:00:00Z")
    _insert(su, email, status="removed", removed_reason="expired", removed_at="2026-10-20T01:00:00Z")
    _insert(su, email)   # a new active row next to two removed ones
    su.execute("SELECT count(*) FROM shared.alert_recipient WHERE email = %s", (email,))
    assert su.fetchone()[0] == 3


@pytest.mark.parametrize("email", [
    "Upper@Example.com",                       # not lower case
    "a@x.com,b@y.com",                         # two addresses
    "a@x.com b@y.com",
    "a@x.com;b@y.com",
    "no-at-sign.example.com",
    "two@@example.com",
    "a@b@example.com",
    "<a@example.com>",
    "a@nodot",
    "x" * 250 + "@example.com",                # > 254
])
def test_email_check_refuses(su, email):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(su, email)


@pytest.mark.parametrize("kw", [
    {"status": "bogus"},
    {"status": "removed"},                                               # no removed_at / reason
    {"status": "removed", "removed_at": "2026-10-20T00:00:00Z"},           # no reason
    {"status": "removed", "removed_reason": "admin"},                    # no removed_at
    {"status": "pending", "removed_reason": "admin"},                    # reason on an active row
    {"status": "removed", "removed_reason": "bored", "removed_at": "2026-10-20T00:00:00Z"},
    {"note": "n" * 81},
    {"last_seen_state": "maybe"},
])
def test_row_checks_refuse(su, kw):
    kw = dict(kw)
    status = kw.pop("status", "pending")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(su, _email("chk"), status=status, **kw)


# ── privileges ───────────────────────────────────────────────────────────────────────────────────

def test_cis_app_selects_inserts_updates(app):
    email = _email("app")
    rid = _insert(app, email)
    app.execute("UPDATE shared.alert_recipient SET status = 'confirmed', confirmed_at = now(), updated_at = now() "
                "WHERE id = %s", (rid,))
    assert app.rowcount == 1
    app.execute("SELECT status FROM shared.alert_recipient WHERE id = %s", (rid,))
    assert app.fetchone()[0] == "confirmed"


@pytest.mark.parametrize("sql", [
    "DELETE FROM shared.alert_recipient WHERE email LIKE 'zz913-%'",
    "TRUNCATE shared.alert_recipient",
])
def test_cis_app_cannot_delete_history(app, sql):
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        app.execute(sql)


@pytest.mark.parametrize("role", ["aa_app_user", "tripplanner", "booking"])
def test_other_app_roles_hold_nothing(su, role):
    if not _role_exists(su, role):
        pytest.skip(f"role {role} does not exist on this test DB")
    su.execute("SELECT has_table_privilege(%s, 'shared.alert_recipient', "
               "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')", (role,))
    assert su.fetchone()[0] is False


def test_public_holds_nothing(su):
    su.execute("SELECT count(*) FROM pg_class c, LATERAL aclexplode(c.relacl) x "
               "WHERE c.oid = 'shared.alert_recipient'::regclass AND x.grantee = 0")
    assert su.fetchone()[0] == 0


# ── re-apply ─────────────────────────────────────────────────────────────────────────────────────

def test_reapply_is_a_no_op(su):
    email = _email("keep")
    _insert(su, email)
    c = _connect()
    c.autocommit = True   # the file brings its own BEGIN … COMMIT
    try:
        c.cursor().execute(_sql())
    finally:
        c.close()
    su.execute("SELECT count(*) FROM shared.alert_recipient WHERE email = %s", (email,))
    assert su.fetchone()[0] == 1
    su.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '251'")
    assert su.fetchone()[0] == 1
