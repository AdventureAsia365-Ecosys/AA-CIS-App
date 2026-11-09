"""AA-1036 (R3) — migration 374 (shared.tenant_addon_request) against real Postgres.

The SQL runs for real, errors NOT swallowed: the module fixture drops the table, applies the file under the Dev default
ACL of the migration runner in shared (CIS 245), then applies it again (re-runnable). It proves the constraints (one
pending request per tenant and product, a decision is set once and whole, a decline needs a reason, an approval names
its plan), the narrowed grants and the FK cascade from shared.tenants.
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
FILE = os.path.join(_MIG, "374_tenant_addon_request.sql")
DEV_DEFAULT_ACL = "ALTER DEFAULT PRIVILEGES IN SCHEMA shared GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cis_app"
DEV_DEFAULT_ACL_UNDO = "ALTER DEFAULT PRIVILEGES IN SCHEMA shared REVOKE ALL ON TABLES FROM cis_app"


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def _apply():
    c = _connect()
    c.autocommit = True   # the file brings its own BEGIN … COMMIT
    try:
        with open(FILE, encoding="utf-8") as f:
            c.cursor().execute(f.read())
    finally:
        c.close()


def _tenant(cur):
    tenant_id = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week) "
                "VALUES (%s, %s, %s, 'starter', 3)",
                (tenant_id, "Quillwater Rambles", f"quillwater-{tenant_id[:8]}"))
    return tenant_id


@pytest.fixture(scope="module", autouse=True)
def applied():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '374'")
    cur.execute("DROP TABLE IF EXISTS shared.tenant_addon_request")
    cur.execute(DEV_DEFAULT_ACL)
    try:
        _apply()
        _apply()            # re-runnable
    finally:
        cur.execute(DEV_DEFAULT_ACL_UNDO)
    yield
    cur.execute("DELETE FROM shared.tenants WHERE slug LIKE 'quillwater-%%'")
    c.close()


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def _insert(cur, tenant_id, product="crm", **cols):
    base = {"tenant_id": tenant_id, "product": product, "requested_by": "membership:aa1036-test"}
    base.update(cols)
    names = ", ".join(base)
    marks = ", ".join(["%s"] * len(base))
    cur.execute(f"INSERT INTO shared.tenant_addon_request ({names}) VALUES ({marks}) RETURNING request_id",
                tuple(base.values()))
    return cur.fetchone()[0]


def test_registers_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '374'")
    assert cur.fetchone()[0] == 1


def test_the_file_is_contract_without_a_directive():
    with open(FILE, encoding="utf-8") as f:
        text = f.read()
    assert not [line for line in text.splitlines() if line.startswith("-- migrate:")]
    assert "Class: contract" in text


def test_one_pending_request_per_tenant_and_product(conn):
    cur = conn.cursor()
    tenant = _tenant(cur)
    _insert(cur, tenant, "crm")
    _insert(cur, tenant, "tripplanner")          # another product: allowed
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert(cur, tenant, "crm")
    cur.execute("ROLLBACK TO SAVEPOINT s")
    # Once decided, a new request for the same product is allowed.
    cur.execute("UPDATE shared.tenant_addon_request SET status = 'declined', decided_at = now(), "
                "decided_by = 'admin:x', decision_note = 'Not on the pilot yet' "
                "WHERE tenant_id = %s AND product = 'crm'", (tenant,))
    _insert(cur, tenant, "crm")


@pytest.mark.parametrize("cols", [
    {"product": "booking"},                                                    # not an add-on
    {"status": "approved"},                                                    # decided without decided_at / plan
    {"status": "declined", "decided_at": "now()", "decided_by": "admin:x"},   # decline without a reason
    {"note": ""},                                                              # empty note: NULL instead
    {"note": "x" * 501},
    {"requested_by": ""},
    {"decision_note": "a note on a pending row"},
])
def test_shape_constraints(conn, cols):
    cur = conn.cursor()
    tenant = _tenant(cur)
    product = cols.pop("product", "crm")
    if cols.get("decided_at") == "now()":
        cur.execute("SELECT now()")
        cols["decided_at"] = cur.fetchone()[0]
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(cur, tenant, product, **cols)


def test_an_approval_names_its_plan_and_job(conn):
    cur = conn.cursor()
    tenant = _tenant(cur)
    rid = _insert(cur, tenant)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):     # approved without a plan
        cur.execute("UPDATE shared.tenant_addon_request SET status = 'approved', decided_at = now(), "
                    "decided_by = 'admin:x' WHERE request_id = %s", (rid,))
    cur.execute("ROLLBACK TO SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):     # a job on a pending row
        cur.execute("UPDATE shared.tenant_addon_request SET provision_job_id = gen_random_uuid() "
                    "WHERE request_id = %s", (rid,))
    cur.execute("ROLLBACK TO SAVEPOINT s")
    cur.execute("UPDATE shared.tenant_addon_request SET status = 'approved', decided_at = now(), "
                "decided_by = 'admin:x', plan_key = 'crm.core', provision_job_id = gen_random_uuid() "
                "WHERE request_id = %s", (rid,))
    cur.execute("SELECT status, plan_key FROM shared.tenant_addon_request WHERE request_id = %s", (rid,))
    assert cur.fetchone() == ("approved", "crm.core")


def test_grants_are_narrowed_to_cis_app(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT has_table_privilege('cis_app', 'shared.tenant_addon_request', 'SELECT') AS s, "
        "has_table_privilege('cis_app', 'shared.tenant_addon_request', 'INSERT') AS i, "
        "has_table_privilege('cis_app', 'shared.tenant_addon_request', 'UPDATE') AS u, "
        "has_table_privilege('cis_app', 'shared.tenant_addon_request', 'DELETE') AS d, "
        "has_table_privilege('cis_app', 'shared.tenant_addon_request', 'TRUNCATE') AS t")
    assert cur.fetchone() == (True, True, True, True, False)
    cur.execute("SELECT grantee FROM information_schema.role_table_grants "
                "WHERE table_schema = 'shared' AND table_name = 'tenant_addon_request' "
                "AND grantee IN ('aa_app_user', 'catalog_owner', 'PUBLIC')")
    assert cur.fetchall() == []


def test_rows_go_with_the_tenant(conn):
    cur = conn.cursor()
    tenant = _tenant(cur)
    _insert(cur, tenant)
    cur.execute("DELETE FROM shared.tenants WHERE tenant_id = %s", (tenant,))
    cur.execute("SELECT count(*) FROM shared.tenant_addon_request WHERE tenant_id = %s", (tenant,))
    assert cur.fetchone()[0] == 0


def test_free_text_columns_are_tagged_pii(conn):
    cur = conn.cursor()
    cur.execute("SELECT a.attname, col_description(a.attrelid, a.attnum) AS d FROM pg_attribute a "
                "WHERE a.attrelid = 'shared.tenant_addon_request'::regclass AND a.attname IN ('note', 'decision_note')")
    rows = dict(cur.fetchall())
    assert rows["note"].startswith("pii:free_text") and rows["decision_note"].startswith("pii:free_text")
