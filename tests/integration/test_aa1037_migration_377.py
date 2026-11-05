"""AA-1037 R4 slice 1 (S269) — migration 377: catalog.v_tenant_billing_v1, Booking's read of the subscriptions, plan
prices and bill-to fields it invoices an agency for (root ADR 0019 d9), applied the way DB Migrate applies a cis/
file since X1 S22.

conftest skips 357 and 377 in the replay (catalog recipe, 245's owners). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships), applies 357 and
377 as db_migrator under the live-owner switch (errors NOT swallowed), 377 twice. Teardown drops both views and revokes
the grants, so later modules see the replay state.

  * the migration: registered once; the exact columns and types; owner catalog_owner, security_barrier, booking only;
    catalog_owner reads only the granted columns (never tenant_subscription.billing_ref) and writes nothing; booking
    has no privilege on the base tables;
  * the view (as booking, rolled back): one row per tenant and subscription with the plan price; a tenant without a
    subscription has one row with NULL subscription columns;
  * the SELECTs AA-Booking runs (backend/app/agency_billing/cis.py) answer as booking.
Invented names only.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa1028_migration_357 as t357  # noqa: E402
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M377 = os.path.join(HERE, "..", "..", "api", "migrations", "377_tenant_billing_catalog_view.sql")
VIEW = "catalog.v_tenant_billing_v1"
# Copied verbatim into AA-Booking backend/tests/integration/test_aa1037_agency_billing.py (CIS_377_COLUMNS).
COLUMNS = (
    "tenant_id uuid, name text, kind text, legal_entity_name text, tax_id text, billing_email text, "
    "billing_address text, billing_country text, product text, plan_key text, plan_name text, "
    "price_usd_monthly numeric(10,2), price_unit text, seats_included integer, price_extra_seat_usd numeric(10,2), "
    "seats integer, subscription_status text"
)
TENANT_COLUMNS = ("legal_entity_name", "tax_id", "billing_email", "billing_address", "billing_country")
SUB_COLUMNS = ("tenant_id", "product", "plan_key", "status", "seats")
PLAN_COLUMNS = ("plan_key", "name", "price_usd_monthly", "price_unit", "seats_included", "price_extra_seat_usd")
# The SELECTs of AA-Booking backend/app/agency_billing/cis.py (column names, never *).
BK_SUBS_SQL = (
    "SELECT tenant_id, name, kind, legal_entity_name, tax_id, billing_email, billing_address, billing_country, "
    f"product, plan_key, plan_name, price_usd_monthly, price_unit, seats_included, price_extra_seat_usd, seats, "
    f"subscription_status FROM {VIEW} WHERE subscription_status = ANY(%s::text[]) ORDER BY tenant_id, product"
)
BK_BILL_TO_SQL = (f"SELECT legal_entity_name, tax_id, billing_email, billing_address, billing_country FROM {VIEW} "
                  "WHERE tenant_id = %s::uuid LIMIT 1")


def undo_377(a):
    a.execute(f"DROP VIEW IF EXISTS {VIEW}")
    a.execute(f"REVOKE SELECT ({', '.join(TENANT_COLUMNS)}) ON shared.tenants FROM catalog_owner")
    a.execute(f"REVOKE SELECT ({', '.join(SUB_COLUMNS)}) ON shared.tenant_subscription FROM catalog_owner")
    a.execute(f"REVOKE SELECT ({', '.join(PLAN_COLUMNS)}) ON shared.plan FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '377'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_377(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_377(a)
        t357.undo_357(a)
        t323._apply(t357.M357)
        t323._apply(M377)
        t323._apply(M377)                                     # idempotent re-run
        yield
    finally:
        undo_377(a)
        t357.undo_357(a)
        admin.close()


@pytest.fixture
def cur():
    c = t245._connect()
    c.autocommit = False
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


def _rows(cur, sql, *args):
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _tenant(cur, name):
    tid = uuid.uuid4()
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                "is_active, legal_entity_name, tax_id, billing_country) VALUES (%s, %s, %s, 'starter'::plan_tier_enum, "
                "3, 60, true, %s, 'TX-377', 'TH')",
                (str(tid), name, f"zz377-{tid.hex[:12]}", f"{name} Co"))
    return str(tid)


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '377'")
    assert cur.fetchone()[0] == 1


def test_columns_owner_barrier_and_consumers(cur):
    cur.execute("""SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
                     AS cols FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                      AND NOT a.attisdropped""", (VIEW,))
    assert cur.fetchone()[0] == COLUMNS
    cur.execute("SELECT pg_get_userbyid(relowner) AS o, reloptions AS r FROM pg_class WHERE oid = %s::regclass",
                (VIEW,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    cur.execute("SELECT has_table_privilege('booking', %s, 'SELECT') AS ok", (VIEW,))
    assert cur.fetchone()[0] is True
    for role in ("tripplanner", "aa_app_user", "cis_app", "crm_app"):
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) AS ok", (role,))
        if not cur.fetchone()[0]:
            continue
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, VIEW))
        assert cur.fetchone()[0] is False, role


def test_catalog_owner_and_booking_privileges(cur):
    cur.execute("SELECT has_column_privilege('catalog_owner', 'shared.tenant_subscription', 'billing_ref', 'SELECT')"
                " AS ok")
    assert cur.fetchone()[0] is False
    for table in ("shared.tenants", "shared.tenant_subscription", "shared.plan"):
        for priv in ("INSERT", "UPDATE"):
            cur.execute("SELECT has_any_column_privilege('catalog_owner', %s, %s) AS ok", (table, priv))
            assert cur.fetchone()[0] is False, (table, priv)
        cur.execute("SELECT has_any_column_privilege('booking', %s, 'SELECT') AS ok", (table,))
        assert cur.fetchone()[0] is False, table
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE') AS ok")
    assert cur.fetchone()[0] is False


def test_rows_per_subscription_and_booking_reads(cur):
    with_sub = _tenant(cur, "Zephyr Trails synthetic")
    bare = _tenant(cur, "Larkspur Journeys synthetic")
    cur.execute("DELETE FROM shared.tenant_subscription WHERE tenant_id = %s", (bare,))
    cur.execute("SET LOCAL ROLE booking")
    rows = _rows(cur, f"SELECT * FROM {VIEW} WHERE tenant_id = %s::uuid", with_sub)
    assert rows and all(r["product"] for r in rows)
    content = next(r for r in rows if r["product"] == "content")
    assert content["plan_key"].startswith("content.") and content["subscription_status"] in ("active", "trialing")
    assert content["legal_entity_name"] == "Zephyr Trails synthetic Co" and content["tax_id"] == "TX-377"
    lone = _rows(cur, f"SELECT * FROM {VIEW} WHERE tenant_id = %s::uuid", bare)
    assert len(lone) == 1 and lone[0]["product"] is None and lone[0]["subscription_status"] is None
    subs = _rows(cur, BK_SUBS_SQL, ["active", "past_due", "trialing"])
    assert any(str(r["tenant_id"]) == with_sub for r in subs)
    assert _rows(cur, BK_BILL_TO_SQL, with_sub)[0]["billing_country"] == "TH"
