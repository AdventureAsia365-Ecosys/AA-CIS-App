"""AA-1116 (S283, contract tenant.entitlements.v1 1.10) — migrations 443 and 444: the quote discount of each
subscription line, stored on shared.tenant_subscription (443, replayed by the conftest) and appended to Booking's
catalog.v_tenant_billing_v1 (444, the catalog recipe, skipped by the replay like 438).

This module builds on test_aa1116_migration_438's chain (Dev after 244 + 245, db_migrator; 357, 377, 379, 390, 429 and
438 applied as db_migrator, errors NOT swallowed), applies 443 again (idempotent) and 444 twice, and undoes 444 then the
438 chain at teardown.

  * 443: the two columns (0 / NULL by default), the CHECK (0-100, the kind exactly when the share is not 0, founding
    or combo only); writing the share adds no account event (the update trigger ignores it);
  * 444: the view's exact columns (438's 28 in their order, then discount_pct, discount_kind), owner catalog_owner,
    security_barrier, booking only; booking reads the share of each line; the 438 column list still answers.
Invented names only.
"""
import os
import sys

import psycopg2
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
import test_aa1116_migration_438 as t438  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M443 = os.path.join(HERE, "..", "..", "api", "migrations", "443_aa1116_subscription_quote_discount.sql")
M444 = os.path.join(HERE, "..", "..", "api", "migrations", "444_aa1116_catalog_billing_discount.sql")
BILLING = t438.BILLING
# Copy this into the AA-Booking view stub when its drafts read the discount (contract 1.10).
BILLING_COLUMNS = t438.BILLING_COLUMNS + ", discount_pct numeric(5,2), discount_kind text"


def undo_444(a):
    a.execute("REVOKE SELECT (quote_discount_pct, quote_discount_kind) ON shared.tenant_subscription "
              "FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '444'")
    # The appended columns go with the 377 undo (DROP VIEW).


@pytest.fixture(scope="module", autouse=True)
def applied_444(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    chain = (t357.M357, t377.M377, t379.M379, t390.M390, t429.M429, t438.M438)
    try:
        undo_444(a)
        t438.undo_438(a)
        t429.undo_429(a)
        t390.undo_390(a)
        t379.undo_379(a)
        t377.undo_377(a)
        t357.undo_357(a)
        for m in chain:
            t323._apply(m)
        t323._apply(M443)                                     # replayed by the conftest already: idempotent
        t323._apply(M444)
        t323._apply(M444)                                     # idempotent re-run
        yield
    finally:
        undo_444(a)
        t438.undo_438(a)
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


def _sub(cur, label="Zz Bramblewick Tours"):
    tid = t377._tenant(cur, label)
    cur.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, seats, updated_by) "
                "VALUES (%s, 'crm', 'crm.core', 'active', 3, 'test')", (tid,))
    return tid


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = ANY(%s)", (["443", "444"],))
    assert cur.fetchone()[0] == 2


def test_443_defaults_and_check(cur):
    tid = _sub(cur)
    cur.execute("SELECT quote_discount_pct, quote_discount_kind FROM shared.tenant_subscription "
                "WHERE tenant_id = %s AND product = 'crm'", (tid,))
    pct, kind = cur.fetchone()
    assert float(pct) == 0 and kind is None
    for pct, kind in ((10, None), (0, "combo"), (101, "combo"), (-1, "founding"), (10, "loyalty")):
        cur.execute("SAVEPOINT zz")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE shared.tenant_subscription SET quote_discount_pct = %s, quote_discount_kind = %s "
                        "WHERE tenant_id = %s AND product = 'crm'", (pct, kind, tid))
        cur.execute("ROLLBACK TO SAVEPOINT zz")


def test_writing_the_share_adds_no_account_event(cur):
    tid = _sub(cur)
    cur.execute("SELECT count(*) AS n FROM shared.tenant_account_event WHERE tenant_id = %s", (tid,))
    before = cur.fetchone()[0]
    cur.execute("UPDATE shared.tenant_subscription SET quote_discount_pct = 10, quote_discount_kind = 'combo' "
                "WHERE tenant_id = %s AND product = 'crm'", (tid,))
    cur.execute("SELECT count(*) AS n FROM shared.tenant_account_event WHERE tenant_id = %s", (tid,))
    assert cur.fetchone()[0] == before


def test_444_columns_owner_and_barrier(cur):
    assert t438._cols(cur, BILLING) == BILLING_COLUMNS
    cur.execute("SELECT pg_get_userbyid(relowner) AS o, 'security_barrier=true' = ANY(reloptions) AS b "
                "FROM pg_class WHERE oid = %s::regclass", (BILLING,))
    assert cur.fetchone() == ("catalog_owner", True)


def test_444_booking_only(cur):
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (["booking", "tripplanner", "aa_app_user", "cis_app", "crm_app"],))
    for (role,) in cur.fetchall():
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, BILLING))
        assert cur.fetchone()[0] is (role == "booking"), role


def test_booking_reads_the_discount_of_each_line(cur):
    tid = _sub(cur)
    cur.execute("UPDATE shared.tenant_subscription SET quote_discount_pct = 15, quote_discount_kind = 'founding' "
                "WHERE tenant_id = %s AND product = 'crm'", (tid,))
    rows = t438._as(cur, "booking", f"SELECT product, discount_pct, discount_kind FROM {BILLING} "
                                    f"WHERE tenant_id = %s ORDER BY product", tid)
    by = {r["product"]: r for r in rows}
    assert set(by) == {"content", "crm"}
    assert (float(by["crm"]["discount_pct"]), by["crm"]["discount_kind"]) == (15.0, "founding")
    assert (float(by["content"]["discount_pct"]), by["content"]["discount_kind"]) == (0.0, None)
    # a reader of the 438 columns keeps its rows
    names = ", ".join(c.split()[0] for c in t438.BILLING_COLUMNS.split(", "))
    assert len(t438._as(cur, "booking", f"SELECT {names} FROM {BILLING} WHERE tenant_id = %s", tid)) == 2
