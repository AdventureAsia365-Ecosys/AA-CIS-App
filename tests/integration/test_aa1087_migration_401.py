"""AA-1087 (S278, root ADR 0006a A10e) — migration 401: catalog.v_tenant_account_v1 appends is_synthetic, so AA-Booking
holds its Dev-only E2E write identities to synthetic agencies. Applied the way DB Migrate applies a cis/ file.

conftest skips 357 and 401 in the replay (catalog recipe, 245's owners). This module builds on test_aa860_migration_246's
module fixture (Dev after 244 + 245, db_migrator), applies 357 then 401 as db_migrator under the live-owner switch
(errors NOT swallowed), 401 twice. Teardown drops the view and revokes the grants, so later modules see the replay
state.

  * the migration: registered once; 357's eight columns in order plus is_synthetic (boolean); owner catalog_owner,
    security_barrier, booking only; catalog_owner reads is_canary and still has only column SELECT on shared.tenants;
    booking still has no privilege on shared.tenants;
  * the view (as booking, rolled back): is_synthetic is TRUE only for is_canary TRUE and not the pilot (NULL, FALSE
    and the pilot read FALSE);
  * the SELECT AA-Booking runs for its E2E identities (backend/app/auth/e2e_staff.py) answers as booking, and the
    projection's SELECT (bk_tenant_pull) is unchanged.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa1028_migration_357 import BK_ACCOUNTS_SQL, COLUMNS as COLUMNS_357, M357, VIEW, undo_357  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M401 = os.path.join(HERE, "..", "..", "api", "migrations", "401_tenant_account_is_synthetic.sql")
COLUMNS = COLUMNS_357 + ("is_synthetic",)
# The SELECT of AA-Booking backend/app/auth/e2e_staff.py (one tenant, by id).
BK_SYNTHETIC_SQL = f"SELECT is_synthetic FROM {VIEW} WHERE tenant_id = %s"


def undo_401(a):
    a.execute("REVOKE SELECT (is_canary) ON shared.tenants FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '401'")


@pytest.fixture(scope="module", autouse=True)
def applied_401(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_401(a)
        undo_357(a)
        t323._apply(M357)
        t323._apply(M401)
        t323._apply(M401)                                     # idempotent re-run
        yield
    finally:
        undo_401(a)
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


def _tenant(cur, name, *, canary=None):
    tid = uuid.uuid4()
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                "is_active, is_canary) VALUES (%s, %s, %s, 'starter'::plan_tier_enum, 3, 60, true, %s)",
                (str(tid), name, f"zz401-{tid.hex[:12]}", canary))
    return str(tid)


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '401'")
    assert cur.fetchone()[0] == 1


def test_columns_are_357s_plus_is_synthetic(cur):
    cur.execute("""SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
                (VIEW,))
    rows = cur.fetchall()
    assert tuple(r[0] for r in rows) == COLUMNS
    assert rows[-1][1] == "boolean"


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


def test_catalog_owner_reads_is_canary_and_nothing_more_than_columns(cur):
    cur.execute("SELECT has_column_privilege('catalog_owner', 'shared.tenants', 'is_canary', 'SELECT')")
    assert cur.fetchone()[0] is True
    cur.execute("SELECT has_table_privilege('catalog_owner', 'shared.tenants', 'SELECT')")
    assert cur.fetchone()[0] is False
    for priv in ("INSERT", "UPDATE"):
        cur.execute("SELECT has_any_column_privilege('catalog_owner', 'shared.tenants', %s)", (priv,))
        assert cur.fetchone()[0] is False, priv


def test_booking_has_no_privilege_on_the_table(cur):
    for priv in ("SELECT", "UPDATE", "INSERT"):
        cur.execute("SELECT has_any_column_privilege('booking', 'shared.tenants', %s)", (priv,))
        assert cur.fetchone()[0] is False, priv


def test_is_synthetic_needs_an_explicit_canary_and_never_the_pilot(cur):
    # Dev's is_canary is nullable (CIS 281); the replay keeps 058's NOT NULL, so drop it here (rolled back).
    cur.execute("ALTER TABLE shared.tenants ALTER COLUMN is_canary DROP NOT NULL")
    synthetic = _tenant(cur, "Moonpetal Trails synthetic", canary=True)
    real = _tenant(cur, "Copperfern Journeys", canary=False)
    unset = _tenant(cur, "Ashgrove Voyages", canary=None)
    pilot = _tenant(cur, "Bramblewick Tours", canary=True)
    cur.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE is_pilot")   # one pilot only
    cur.execute("UPDATE shared.tenants SET is_pilot = true, pilot_ends_at = now() + interval '8 weeks' "
                "WHERE tenant_id = %s", (pilot,))
    cur.execute("SET LOCAL ROLE booking")
    got = {}
    for tid in (synthetic, real, unset, pilot):
        cur.execute(BK_SYNTHETIC_SQL, (tid,))
        got[tid] = cur.fetchone()[0]
    assert got == {synthetic: True, real: False, unset: False, pilot: False}


def test_the_projection_select_is_unchanged(cur):
    _tenant(cur, "Willowmere Isles synthetic", canary=True)
    cur.execute("SET LOCAL ROLE booking")
    cur.execute(BK_ACCOUNTS_SQL)
    assert [d[0] for d in cur.description] == ["tenant_id", "name", "in_service", "kind", "trade_book"]
