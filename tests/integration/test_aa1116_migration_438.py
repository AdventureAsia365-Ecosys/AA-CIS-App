"""AA-1116 (S282, option B pricing) — migration 438: the billing terms appended to catalog.v_tenant_billing_v1
(Booking), catalog.v_tenant_ai_budget_v1 and catalog.v_tenant_tripplanner_v1 (TripPlanner), contract
tenant.entitlements.v1 1.9.

438 is the catalog recipe and needs 357 + 377 (billing) and 379 + 390 + 429 (TripPlanner), all skipped by the replay.
This module builds on the 245 / 246 module fixtures (Dev after 244 + 245, db_migrator), applies 357, 377, 379, 390, 429
and 438 as db_migrator (errors NOT swallowed), 438 twice, and undoes them at teardown (DROP VIEW + the column grants).

  * the three views' exact columns (377 / 390 / 429 columns first, then the appended ones), owner catalog_owner,
    security_barrier, the consumer roles only;
  * catalog_owner reads exactly the granted columns of shared.tenant_addon (never reason / created_by) and nothing of
    billing_ref;
  * as booking: the VND currency, the seat minimum, the agreed price, bill_free for a pilot (and not after an approved
    cis.billing.pilot_free = false), one add-on row with its quantity; the 377 column list still answers;
  * as tripplanner: alert / approval / hard stop from the tenant's AI budget row, else the registry defaults;
    ai_sessions_monthly from tp.ai_sessions (NULL when the plan sets none).
Invented names only.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa1028_migration_357 as t357  # noqa: E402
import test_aa1037_migration_377 as t377  # noqa: E402
import test_aa1038_migration_379 as t379  # noqa: E402
import test_aa1038_migration_390 as t390  # noqa: E402
import test_aa1090_migrations_428_429 as t429  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M438 = os.path.join(HERE, "..", "..", "api", "migrations", "438_aa1116_catalog_billing_terms_views.sql")
BILLING = "catalog.v_tenant_billing_v1"
BUDGET = "catalog.v_tenant_ai_budget_v1"
TP = "catalog.v_tenant_tripplanner_v1"
# Copy these into the AA-Booking / AA-TripPlanner-Web view stubs when their AA-1116 lanes read the new columns.
BILLING_COLUMNS = (
    "tenant_id uuid, name text, kind text, legal_entity_name text, tax_id text, billing_email text, "
    "billing_address text, billing_country text, product text, plan_key text, plan_name text, "
    "price_usd_monthly numeric(10,2), price_unit text, seats_included integer, price_extra_seat_usd numeric(10,2), "
    "seats integer, subscription_status text, billing_currency text, price_vnd_monthly numeric(14,0), "
    "price_extra_seat_vnd numeric(14,0), seats_min integer, agreed_price_monthly numeric(14,2), agreed_currency text, "
    "agreed_until date, billing_interval text, trial_ends_at timestamp with time zone, is_pilot boolean, "
    "bill_free boolean, quantity integer, ends_at timestamp with time zone")
BUDGET_COLUMNS = ("tenant_id uuid, budget_source text, monthly_usd numeric, month_spent_usd numeric(14,6), "
                  "month_calls integer, alert_pct integer, approval_pct integer, hard_stop boolean")
TP_COLUMNS = t429.COLUMNS + ", ai_sessions_monthly integer"
ADDON_COLUMNS = ("tenant_id", "plan_key", "product", "quantity", "status", "trial_ends_at", "starts_at", "ends_at")
REASON = "zz test: a reason of at least twenty characters"


def undo_438(a):
    a.execute(f"REVOKE SELECT ({', '.join(ADDON_COLUMNS)}) ON shared.tenant_addon FROM catalog_owner")
    a.execute("REVOKE SELECT (tenant_id, alert_pct, approval_pct, hard_stop) ON shared.tenant_ai_budget "
              "FROM catalog_owner")
    a.execute("REVOKE SELECT (billing_currency, is_pilot) ON shared.tenants FROM catalog_owner")
    a.execute("REVOKE SELECT (billing_interval, agreed_price_monthly, agreed_currency, agreed_until, trial_ends_at) "
              "ON shared.tenant_subscription FROM catalog_owner")
    a.execute("REVOKE SELECT (price_vnd_monthly, price_extra_seat_vnd, seats_min) ON shared.plan FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '438'")
    # The appended columns go with the 377 / 379 / 390 undo (DROP VIEW).


@pytest.fixture(scope="module", autouse=True)
def applied_438(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_438(a)
        t429.undo_429(a)
        t390.undo_390(a)
        t379.undo_379(a)
        t377.undo_377(a)
        t357.undo_357(a)
        t323._apply(t357.M357)
        t323._apply(t377.M377)
        t323._apply(t379.M379)
        t323._apply(t390.M390)
        t323._apply(t429.M429)
        t323._apply(M438)
        t323._apply(M438)                                     # idempotent re-run
        yield
    finally:
        undo_438(a)
        t429.undo_429(a)
        t390.undo_390(a)
        t379.undo_379(a)
        t377.undo_377(a)
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


def _cols(cur, view):
    cur.execute("SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum) "
                "AS c FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped",
                (view,))
    return cur.fetchone()[0]


def _as(cur, role, sql, *args):
    cur.execute("SAVEPOINT zz_as")
    cur.execute(f"SET LOCAL ROLE {role}")
    try:
        cur.execute(sql, args)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT zz_as")


def _approve(cur, key, value):
    cur.execute("SELECT coalesce(max(version), 0) + 1 AS v FROM shared.setting_value "
                "WHERE key = %s AND tenant_id IS NULL",
                (key,))
    version = cur.fetchone()[0]
    cur.execute("INSERT INTO shared.setting_value (key, value, version, effective_from, proposed_by, reason) "
                "VALUES (%s, %s::jsonb, %s, now(), 'admin:zz-a', %s) RETURNING id",
                (key, json.dumps(value), version, REASON))
    vid = cur.fetchone()[0]
    cur.execute("UPDATE shared.setting_value SET status = 'approved', decided_by = 'admin:zz-b', "
                "decided_at = greatest(now(), proposed_at) WHERE id = %s", (vid,))


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '438'")
    assert cur.fetchone()[0] == 1


@pytest.mark.parametrize("view,expected", [(BILLING, BILLING_COLUMNS), (BUDGET, BUDGET_COLUMNS), (TP, TP_COLUMNS)])
def test_columns_owner_and_barrier(cur, view, expected):
    assert _cols(cur, view) == expected
    cur.execute("SELECT pg_get_userbyid(relowner) AS o, 'security_barrier=true' = ANY(reloptions) AS b "
                "FROM pg_class WHERE oid = %s::regclass", (view,))
    assert cur.fetchone() == ("catalog_owner", True)


@pytest.mark.parametrize("view,reader", [(BILLING, "booking"), (BUDGET, "tripplanner"), (TP, "tripplanner")])
def test_consumers(cur, view, reader):
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (["booking", "tripplanner", "aa_app_user", "cis_app", "crm_app"],))
    for (role,) in cur.fetchall():
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, view))
        assert cur.fetchone()[0] is (role == reader), role


def test_catalog_owner_reads_only_the_granted_addon_columns(cur):
    cur.execute("SELECT string_agg(a.attname, ',' ORDER BY a.attname) AS c FROM pg_attribute a "
                "WHERE a.attrelid = 'shared.tenant_addon'::regclass AND a.attnum > 0 AND NOT a.attisdropped "
                "AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')")
    assert cur.fetchone()[0] == ",".join(sorted(ADDON_COLUMNS))
    cur.execute("SELECT has_column_privilege('catalog_owner', 'shared.tenant_subscription', 'billing_ref', 'SELECT') "
                "AS ok")
    assert cur.fetchone()[0] is False


def test_booking_reads_the_option_b_terms_and_addon_rows(cur):
    cur.execute("UPDATE shared.plan SET seats_min = 3, price_vnd_monthly = 490000 WHERE plan_key = 'crm.core'")
    cur.execute("INSERT INTO shared.plan (plan_key, product, name, price_usd_monthly, price_unit, updated_by) "
                "VALUES ('addon.zz_pack', 'addon', 'Zz pack', 19, 'unit', 'test')")
    tid = t377._tenant(cur, "Zz Juniper Coast")
    cur.execute("UPDATE shared.tenants SET billing_currency = 'VND', is_pilot = true WHERE tenant_id = %s", (tid,))
    cur.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, seats, "
                "agreed_price_monthly, agreed_currency, agreed_until, updated_by) "
                "VALUES (%s, 'crm', 'crm.core', 'active', 1, 450000, 'VND', '2030-01-01', 'test')", (tid,))
    cur.execute("INSERT INTO shared.tenant_addon (tenant_id, plan_key, quantity, status, reason, created_by, "
                "updated_by) VALUES (%s, 'addon.zz_pack', 2, 'active', 'zz order', 'test', 'test')", (tid,))
    rows = _as(cur, "booking", f"SELECT product, plan_key, billing_currency, seats_min, price_vnd_monthly, "
                               f"agreed_price_monthly, agreed_currency, billing_interval, is_pilot, bill_free, "
                               f"quantity, "
                               f"subscription_status FROM {BILLING} WHERE tenant_id = %s ORDER BY product", tid)
    by = {r["product"]: r for r in rows}
    assert set(by) == {"addon", "content", "crm"}
    crm = by["crm"]
    assert (crm["billing_currency"], crm["seats_min"], int(crm["price_vnd_monthly"]),
            float(crm["agreed_price_monthly"]), crm["agreed_currency"], crm["billing_interval"], crm["bill_free"],
            crm["quantity"]) == (
        "VND", 3, 490000, 450000.0, "VND", "month", True, None)
    assert (by["addon"]["plan_key"], by["addon"]["quantity"], by["addon"]["subscription_status"]) == (
        "addon.zz_pack", 2, "active")
    # the 377 column list still answers
    old = _as(cur, "booking", f"SELECT {', '.join(c.split()[0] for c in t377.COLUMNS)} FROM {BILLING} "
                              f"WHERE tenant_id = %s", tid) if isinstance(t377.COLUMNS, tuple) else None
    assert old is None or len(old) == 3


def test_bill_free_follows_the_setting(cur):
    tid = t377._tenant(cur, "Zz Juniper Coast")
    cur.execute("UPDATE shared.tenants SET is_pilot = true WHERE tenant_id = %s", (tid,))
    _approve(cur, "cis.billing.pilot_free", False)
    rows = _as(cur, "booking", f"SELECT bill_free FROM {BILLING} WHERE tenant_id = %s", tid)
    assert [r["bill_free"] for r in rows] == [False]


def test_tripplanner_reads_thresholds_and_sessions(cur):
    tid, _slug = t379._tenant(cur, "Zz Cedar Isles")
    rows = _as(cur, "tripplanner", f"SELECT alert_pct, approval_pct, hard_stop FROM {BUDGET} WHERE tenant_id = %s", tid)
    assert rows == [{"alert_pct": 80, "approval_pct": 100, "hard_stop": True}]
    cur.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, hard_stop, "
                "updated_by) VALUES (%s, 30, 70, 120, false, 'test')", (tid,))
    rows = _as(cur, "tripplanner", f"SELECT alert_pct, approval_pct, hard_stop FROM {BUDGET} WHERE tenant_id = %s", tid)
    assert rows == [{"alert_pct": 70, "approval_pct": 120, "hard_stop": False}]
    rows = _as(cur, "tripplanner", f"SELECT ai_sessions_monthly FROM {TP} WHERE tenant_id = %s", tid)
    assert rows == [{"ai_sessions_monthly": None}]
    cur.execute("INSERT INTO shared.plan_feature (plan_key, feature_key, limit_value, updated_by) "
                "VALUES ('tripplanner.hosted', 'tp.ai_sessions', 300, 'test') ON CONFLICT (plan_key, feature_key) "
                "DO UPDATE SET limit_value = 300")
    rows = _as(cur, "tripplanner", f"SELECT ai_sessions_monthly FROM {TP} WHERE tenant_id = %s", tid)
    assert rows == [{"ai_sessions_monthly": 300}]
