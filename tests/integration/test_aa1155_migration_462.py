"""AA-1155 (S286) — migration 462: CIS custom staff roles, the business-owner marker, Booking custom grant codes, the
A10f / A10g E2E addresses and the contract view shared.v_staff_booking_grant, against real Postgres.

The SQL runs for real, errors NOT swallowed. The module fixture re-applies 462 twice (re-runnable; another module may
have put back an older admin_users_email_chk or staff_app_grant_role_chk). Then: the seed, the approve-class refusal,
the one-business-owner index, the custom-role CHECKs, the E2E rules, the view's rows and columns, and the grants (booking
reads the view, never the tables). Invented names and addresses only; every test rolls back."""
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
ISS = "https://cognito-idp.us-west-1.amazonaws.com/zz-pool"
CONTENT_LEAD_E2E = "e2e-cis-content-lead@e2e-mail.lumiguides.it.com"
MAKER_E2E = "e2e-custom-maker@e2e-mail.lumiguides.it.com"
CONTENT_SEED = {
    "content.read", "content.write", "review.act", "content.publish", "master.admin", "atoms.write", "photos.write",
    "a4.unpublish", "quality.write", "catalog.read", "tenant_content.read", "lab.read", "wave.run",
    "notifications.read", "reference_price.read", "learn.preview", "docs.read", "tenant.read",
}


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
    _apply(MIG_462)
    _apply(MIG_462)   # re-runnable
    yield


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def _staff(cur, role="content", email=None, *, sub=None, is_owner=False, business_owner=False, custom_role=None,
           active=True):
    staff_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO shared.admin_users (id, username, role, is_active, email, is_owner, is_business_owner, "
        "custom_role, cognito_iss, cognito_sub) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (staff_id, f"aa1155-{staff_id[:8]}", role, active, email, is_owner, business_owner, custom_role,
         ISS if sub else None, sub))
    return staff_id


def _grant(cur, staff_id, role):
    cur.execute("INSERT INTO shared.staff_app_grant (admin_user_id, app, role) VALUES (%s, 'booking', %s)",
                (staff_id, role))


def _refused(conn, sql, args=(), error=psycopg2.errors.CheckViolation):
    cur = conn.cursor()
    cur.execute("SAVEPOINT s")
    with pytest.raises(error):
        cur.execute(sql, args)
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_462_registered_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '462'")
    assert cur.fetchone()[0] == 1


def test_462_is_a_contract_file_without_a_directive():
    text = open(MIG_462, encoding="utf-8").read()
    assert text.splitlines()[0].startswith("-- Migration 462")
    assert "-- migrate:" not in text


def test_the_content_custom_role_is_seeded_exactly(conn):
    cur = conn.cursor()
    cur.execute("SELECT label, based_on::text AS based_on, archived FROM shared.staff_role WHERE code = 'custom_content'")
    assert cur.fetchone() == ("Content", "content_lead", False)
    cur.execute("SELECT capability FROM shared.staff_role_capability WHERE role_code = 'custom_content'")
    caps = {r[0] for r in cur.fetchall()}
    assert caps == CONTENT_SEED
    assert "content.settings.write" not in caps and "tenant.read" in caps


@pytest.mark.parametrize("capability", [
    "content.approve", "settings.approve", "pricing_policy.approve_finance", "staff.write", "staff.admin",
    "Content.Read", "content", "content.read; drop", "a.b.c.d.e",
])
def test_a_custom_role_refuses_approve_class_and_malformed_capabilities(conn, capability):
    _refused(conn, "INSERT INTO shared.staff_role_capability (role_code, capability, created_by) "
                   "VALUES ('custom_content', %s, 'test')", (capability,))


@pytest.mark.parametrize("code,based_on", [
    ("content", "content"), ("custom_", "content"), ("custom_Upper", "content"), ("custom_zz", "admin"),
    ("custom_zz", "viewer"), ("custom_zz", "reviewer"),
])
def test_a_custom_role_code_and_base_are_checked(conn, code, based_on):
    _refused(conn, "INSERT INTO shared.staff_role (code, label, based_on, created_by, updated_by) "
                   "VALUES (%s, 'Zz role', %s, 'test', 'test')", (code, based_on))


def test_a_custom_holder_is_never_an_admin_and_needs_a_known_role(conn):
    cur = conn.cursor()
    _staff(cur, role="content_lead", custom_role="custom_content")
    _refused(conn, "INSERT INTO shared.admin_users (id, username, role, is_active, custom_role) "
                   "VALUES (gen_random_uuid(), 'aa1155-zz-admin', 'admin', true, 'custom_content')")
    _refused(conn, "INSERT INTO shared.admin_users (id, username, role, is_active, custom_role) "
                   "VALUES (gen_random_uuid(), 'aa1155-zz-unknown', 'content', true, 'custom_nowhere')",
             error=psycopg2.errors.ForeignKeyViolation)
    _refused(conn, "DELETE FROM shared.staff_role WHERE code = 'custom_content'",
             error=psycopg2.errors.ForeignKeyViolation)


def test_at_most_one_business_owner(conn):
    cur = conn.cursor()
    cur.execute("UPDATE shared.admin_users SET is_business_owner = false WHERE is_business_owner")
    _staff(cur, role="admin", email="zz-owner-one@adventure.asia", business_owner=True)
    _staff(cur, role="content", email="zz-owner-two@adventure.asia")
    _refused(conn, "UPDATE shared.admin_users SET is_business_owner = true WHERE email = 'zz-owner-two@adventure.asia'",
             error=psycopg2.errors.UniqueViolation)


def test_a_booking_grant_takes_built_in_and_custom_codes_only(conn):
    cur = conn.cursor()
    staff_id = _staff(cur, email="zz-grant@adventure.asia")
    _grant(cur, staff_id, "custom_product_ops_accounting")
    cur.execute("UPDATE shared.staff_app_grant SET role = 'finance' WHERE admin_user_id = %s", (staff_id,))
    for bad in ("custom", "custom_X", "product_ops", "approver"):
        _refused(conn, "UPDATE shared.staff_app_grant SET role = %s WHERE admin_user_id = %s", (bad, staff_id))


def test_the_a10f_and_a10g_addresses_keep_their_scope(conn):
    cur = conn.cursor()
    _staff(cur, role="content_lead", email=CONTENT_LEAD_E2E)
    _staff(cur, role="content_lead", email=MAKER_E2E, custom_role="custom_content")
    cur.execute("DELETE FROM shared.admin_users WHERE email IN (%s, %s)", (CONTENT_LEAD_E2E, MAKER_E2E))
    insert = ("INSERT INTO shared.admin_users (id, username, role, is_active, email, is_owner, is_business_owner, "
              "custom_role, password_hash) VALUES (gen_random_uuid(), %s, %s, true, %s, %s, %s, %s, %s)")
    for args in [
        ("aa1155-a", "admin", CONTENT_LEAD_E2E, False, False, None, None),
        ("aa1155-b", "content_lead", CONTENT_LEAD_E2E, False, False, "custom_content", None),
        ("aa1155-c", "content_lead", CONTENT_LEAD_E2E, True, False, None, None),
        ("aa1155-d", "content_lead", MAKER_E2E, False, True, None, None),
        ("aa1155-e", "content", MAKER_E2E, False, False, None, None),
        ("aa1155-f", "content_lead", MAKER_E2E, False, False, None, "x"),
        ("aa1155-g", "content_lead", "e2e-custom-maker2@e2e-mail.lumiguides.it.com", False, False, None, None),
        ("aa1155-h", "content", "e2e@e2e-mail.lumiguides.it.com", False, True, None, None),
    ]:
        _refused(conn, insert, args)


def test_the_view_has_the_four_key_columns_only(conn):
    cur = conn.cursor()
    cur.execute("SELECT attname FROM pg_attribute WHERE attrelid = 'shared.v_staff_booking_grant'::regclass "
                "AND attnum > 0 AND NOT attisdropped ORDER BY attnum")
    assert [r[0] for r in cur.fetchall()] == ["cognito_sub", "booking_role_code", "is_owner", "is_business_owner"]
    cur.execute("SELECT reloptions FROM pg_class WHERE oid = 'shared.v_staff_booking_grant'::regclass")
    assert "security_barrier=true" in cur.fetchone()[0]


def test_the_view_lists_active_identities_with_a_grant_or_a_marker(conn):
    cur = conn.cursor()
    cur.execute("UPDATE shared.admin_users SET is_business_owner = false WHERE is_business_owner")
    custom = _staff(cur, role="content_lead", email="zz-custom@adventure.asia", sub="zz-sub-custom",
                    custom_role="custom_content")
    _grant(cur, custom, "custom_product_ops_accounting")
    built_in = _staff(cur, email="zz-sales@adventure.asia", sub="zz-sub-sales")
    _grant(cur, built_in, "sales")
    _staff(cur, role="admin", email="zz-bo@adventure.asia", sub="zz-sub-bo", business_owner=True)
    _staff(cur, role="admin", email="zz-owner@adventure.asia", sub="zz-sub-owner", is_owner=True)
    _staff(cur, email="zz-none@adventure.asia", sub="zz-sub-none")                 # no grant, no marker
    gone = _staff(cur, email="zz-gone@adventure.asia", sub="zz-sub-gone", active=False)
    _grant(cur, gone, "sales")
    no_cognito = _staff(cur, email="zz-legacy@adventure.asia")
    _grant(cur, no_cognito, "sales")
    cur.execute("SELECT cognito_sub, booking_role_code, is_owner, is_business_owner FROM shared.v_staff_booking_grant "
                "WHERE cognito_sub LIKE 'zz-sub-%%' ORDER BY cognito_sub")
    assert cur.fetchall() == [
        ("zz-sub-bo", None, False, True),
        ("zz-sub-custom", "custom_product_ops_accounting", False, False),
        ("zz-sub-owner", None, True, False),
        ("zz-sub-sales", "sales", False, False),
    ]


def test_booking_reads_the_view_and_never_the_tables(conn):
    cur = conn.cursor()
    cur.execute("SELECT has_schema_privilege('booking', 'shared', 'USAGE') AS ok")
    assert cur.fetchone()[0] is True
    checks = {
        ("shared.v_staff_booking_grant", "SELECT"): True,
        ("shared.v_staff_booking_grant", "INSERT"): False,
        ("shared.admin_users", "SELECT"): False,
        ("shared.staff_app_grant", "SELECT"): False,
        ("shared.staff_role", "SELECT"): False,
        ("shared.staff_role_capability", "SELECT"): False,
    }
    for (obj, priv), expected in checks.items():
        cur.execute("SELECT has_table_privilege('booking', %s, %s) AS ok", (obj, priv))
        assert cur.fetchone()[0] is expected, (obj, priv)
    for role in ("tripplanner", "aa_app_user"):
        cur.execute("SELECT has_table_privilege(%s, 'shared.v_staff_booking_grant', 'SELECT') AS ok", (role,))
        assert cur.fetchone()[0] is False, role


def test_booking_reads_the_view_through_its_own_role(conn):
    cur = conn.cursor()
    staff_id = _staff(cur, role="content_lead", email="zz-role@adventure.asia", sub="zz-sub-role",
                      custom_role="custom_content")
    _grant(cur, staff_id, "custom_product_ops_accounting")
    cur.execute("SET LOCAL ROLE booking")
    cur.execute("SELECT booking_role_code FROM shared.v_staff_booking_grant WHERE cognito_sub = 'zz-sub-role'")
    assert cur.fetchone()[0] == "custom_product_ops_accounting"
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("SELECT email FROM shared.admin_users LIMIT 1")
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_cis_app_reads_the_role_tables(conn):
    cur = conn.cursor()
    for obj in ("shared.staff_role", "shared.staff_role_capability"):
        cur.execute("SELECT has_table_privilege('cis_app', %s, 'SELECT') AS ok", (obj,))
        assert cur.fetchone()[0] is True, obj
