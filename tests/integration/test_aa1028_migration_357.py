"""AA-1028 R1 slice 2 (S266) — migration 357: catalog.v_tenant_account_v1, Booking's read of the tenant account
(the projection booking.b2b_partners, bk_tenant_pull), applied the way DB Migrate applies a cis/ file since X1 S22.

conftest skips 357 in the replay (catalog recipe, 245's owners). This module builds on test_aa860_migration_246's
module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships), applies 357 as db_migrator under the
live-owner switch (errors NOT swallowed), twice. Teardown drops the view and revokes the grants, so later modules see
the replay state.

  * the migration: registered once; the view's exact columns, owner catalog_owner, security_barrier, booking only;
    catalog_owner reads exactly six columns of shared.tenants (never the key hash or the bill-to fields) and has no
    other privilege on it; booking still has no privilege on shared.tenants;
  * the view (as booking, rolled back): one row per tenant with its name, status, in_service and trade_book; a
    suspension and an offboarding show in_service false; an offboarded tenant has no trade.book;
  * the SELECT AA-Booking runs (backend/app/b2b/tenant_pull.py ACCOUNTS_SQL) answers as booking.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M357 = os.path.join(HERE, "..", "..", "api", "migrations", "357_tenant_account_catalog_view.sql")
VIEW = "catalog.v_tenant_account_v1"
COLUMNS = ("tenant_id", "name", "account_status", "suspend_reason", "in_service", "kind", "is_pilot", "trade_book")
TENANT_COLUMNS = ("tenant_id", "name", "account_status", "suspend_reason", "kind", "is_pilot")
# The SELECT of AA-Booking backend/app/b2b/tenant_pull.py (column names, never *).
BK_ACCOUNTS_SQL = f"SELECT tenant_id, name, in_service, kind, trade_book FROM {VIEW} ORDER BY tenant_id"


def undo_357(a):
    a.execute(f"DROP VIEW IF EXISTS {VIEW}")
    a.execute(f"REVOKE SELECT ({', '.join(TENANT_COLUMNS)}) ON shared.tenants FROM catalog_owner")
    a.execute("REVOKE SELECT ON shared.v_tenant_entitlement_resolved FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '357'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_357(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_357(a)
        t323._apply(M357)
        t323._apply(M357)                                     # idempotent re-run
        yield
    finally:
        undo_357(a)
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


def _tenant(cur, name, *, tier="starter"):
    tid = uuid.uuid4()
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                "is_active) VALUES (%s, %s, %s, %s::plan_tier_enum, 3, 60, true)",
                (str(tid), name, f"zz357-{tid.hex[:12]}", tier))
    return str(tid)


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '357'")
    assert cur.fetchone()[0] == 1


def test_columns(cur):
    cur.execute("""SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                    AND NOT a.attisdropped ORDER BY a.attnum""", (VIEW,))
    assert tuple(r[0] for r in cur.fetchall()) == COLUMNS


def test_owner_barrier_and_consumers(cur):
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class WHERE oid = %s::regclass", (VIEW,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    cur.execute("SELECT has_table_privilege('booking', %s, 'SELECT')", (VIEW,))
    assert cur.fetchone()[0] is True
    for role in ("tripplanner", "aa_app_user", "cis_app", "crm_app"):
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,))
        if not cur.fetchone()[0]:
            continue
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, VIEW))
        assert cur.fetchone()[0] is False, role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_catalog_owner_reads_only_the_six_columns(cur):
    cur.execute("SELECT has_table_privilege('catalog_owner', 'shared.tenants', 'SELECT')")
    assert cur.fetchone()[0] is False
    cur.execute("""SELECT a.attname FROM pg_attribute a
                    WHERE a.attrelid = 'shared.tenants'::regclass AND a.attnum > 0 AND NOT a.attisdropped
                      AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""")
    assert {r[0] for r in cur.fetchall()} == set(TENANT_COLUMNS)
    for priv in ("INSERT", "UPDATE"):
        cur.execute("SELECT has_any_column_privilege('catalog_owner', 'shared.tenants', %s)", (priv,))
        assert cur.fetchone()[0] is False, priv


def test_booking_has_no_privilege_on_the_table(cur):
    for priv in ("SELECT", "UPDATE", "INSERT"):
        cur.execute("SELECT has_any_column_privilege('booking', 'shared.tenants', %s)", (priv,))
        assert cur.fetchone()[0] is False, priv


def test_view_follows_the_lifecycle(cur):
    live = _tenant(cur, "Zephyr Trails synthetic")
    held = _tenant(cur, "Larkspur Journeys synthetic")
    gone = _tenant(cur, "Quillfeather Tours synthetic")
    cur.execute("UPDATE shared.tenants SET is_active = false WHERE tenant_id = %s", (held,))
    cur.execute("UPDATE shared.tenants SET cancelled_at = now(), is_active = false WHERE tenant_id = %s", (gone,))
    cur.execute("SET LOCAL ROLE booking")
    rows = {r["tenant_id"]: r for r in _rows(cur, f"SELECT * FROM {VIEW} WHERE tenant_id = ANY(%s::uuid[])",
                                              [live, held, gone])}
    rows = {str(k): v for k, v in rows.items()}
    assert rows[live]["name"] == "Zephyr Trails synthetic" and rows[live]["in_service"] is True
    assert rows[live]["account_status"] == "active" and rows[live]["trade_book"] is True
    assert rows[live]["kind"] == "agency" and rows[live]["is_pilot"] is False
    assert rows[held]["account_status"] == "suspended" and rows[held]["in_service"] is False
    assert rows[held]["trade_book"] is True                      # a suspension leaves the subscription alone
    assert rows[gone]["account_status"] == "offboarding" and rows[gone]["in_service"] is False
    assert rows[gone]["trade_book"] is False                     # the bridge canceled the content subscription


def test_trade_book_follows_an_override(cur):
    tid = _tenant(cur, "Marigold Coast synthetic")
    cur.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, enabled, reason, granted_by) "
                "VALUES (%s, 'trade.book', false, 'no B2B booking yet', 'admin:test')", (tid,))
    cur.execute("SET LOCAL ROLE booking")
    assert _rows(cur, f"SELECT trade_book FROM {VIEW} WHERE tenant_id = %s", tid)[0]["trade_book"] is False


def test_trade_book_follows_the_trial_tick(cur):
    """CIS 361 (replacing 356's cut at trial_ends_at): an ended trial stays live until the hourly tenant_account_tick
    moves it to active or past_due (both live), so Booking keeps trade.book through the trial end; a canceled
    content subscription still ends it."""
    tid = _tenant(cur, "Saffron Bay synthetic")
    cur.execute("UPDATE shared.tenant_subscription SET status = 'trialing', trial_ends_at = now() + interval '2 days' "
                "WHERE tenant_id = %s AND product = 'content'", (tid,))
    assert _rows(cur, f"SELECT trade_book FROM {VIEW} WHERE tenant_id = %s", tid)[0]["trade_book"] is True
    cur.execute("UPDATE shared.tenant_subscription SET trial_ends_at = now() - interval '1 day' "
                "WHERE tenant_id = %s AND product = 'content'", (tid,))
    assert _rows(cur, f"SELECT trade_book FROM {VIEW} WHERE tenant_id = %s", tid)[0]["trade_book"] is True
    cur.execute("UPDATE shared.tenant_subscription SET status = 'canceled', canceled_at = now() "
                "WHERE tenant_id = %s AND product = 'content'", (tid,))
    cur.execute("SET LOCAL ROLE booking")
    assert _rows(cur, f"SELECT trade_book FROM {VIEW} WHERE tenant_id = %s", tid)[0]["trade_book"] is False


def test_booking_select_answers(cur):
    _tenant(cur, "Driftwood Isles synthetic")
    cur.execute("SET LOCAL ROLE booking")
    rows = _rows(cur, BK_ACCOUNTS_SQL)
    assert rows and set(rows[0]) == {"tenant_id", "name", "in_service", "kind", "trade_book"}
