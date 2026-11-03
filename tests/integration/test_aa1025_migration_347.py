"""AA-1025 H1b (S265) — migration 347: catalog.v_llm_app_profile_v1, TripPlanner's read of its own app profile row
(shared.llm_app_profile, 345), applied the way DB Migrate applies a cis/ file since X1 S22.

conftest skips 323 and 347 in the replay (catalog recipe, 245's owners). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships), applies 323
(347's precondition) then 347 as db_migrator under the live-owner switch (errors NOT swallowed), 347 twice. Teardown
drops the views and revokes the column grants, so later modules see the replay state.

  * the migration: registered once; the view's exact columns, owner catalog_owner, security_barrier, tripplanner
    only; catalog_owner reads only the six listed columns of shared.llm_app_profile (never disabled_reason or
    updated_by) and has no other privilege on it; tripplanner still has no privilege on the table;
  * the view (as tripplanner, rolled back): exactly the tripplanner row, its switch and budget as stored; no row when
    the tripplanner row is missing; the other apps' rows never;
  * the SELECT TripPlanner runs (AA-TripPlanner-Web backend/shared/app_profile.py PROFILE_SQL) answers as tripplanner.
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M347 = os.path.join(HERE, "..", "..", "api", "migrations", "347_llm_app_profile_view_tripplanner.sql")
VIEW = "catalog.v_llm_app_profile_v1"
COLUMNS = ("app", "ai_enabled", "bulk_paused", "default_daily_budget_usd", "degrade_mode", "updated_at")
# The SELECT of AA-TripPlanner-Web backend/shared/app_profile.py (column names, never *).
TP_PROFILE_SQL = (f"SELECT {', '.join(COLUMNS)} FROM {VIEW} WHERE app = 'tripplanner'")


def undo_347(a):
    a.execute(f"DROP VIEW IF EXISTS {VIEW}")
    a.execute(f"REVOKE SELECT ({', '.join(COLUMNS)}) ON shared.llm_app_profile FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '347'")
    t323.undo_323(a)                                          # 347's precondition, applied by this module


@pytest.fixture(scope="module", autouse=True)
def applied_347(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_347(a)
        t323._apply(t323.MIGRATION)
        t323._apply(M347)
        t323._apply(M347)                                     # idempotent re-run
        yield
    finally:
        undo_347(a)
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


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '347'")
    assert cur.fetchone()[0] == 1


def test_columns(cur):
    cur.execute("""SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                    AND NOT a.attisdropped ORDER BY a.attnum""", (VIEW,))
    assert tuple(r[0] for r in cur.fetchall()) == COLUMNS


def test_owner_barrier_and_consumers(cur):
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class WHERE oid = %s::regclass", (VIEW,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    cur.execute("SELECT has_table_privilege('tripplanner', %s, 'SELECT')", (VIEW,))
    assert cur.fetchone()[0] is True
    for role in ("booking", "aa_app_user", "cis_app", "crm_app"):
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,))
        if not cur.fetchone()[0]:
            continue
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, VIEW))
        assert cur.fetchone()[0] is False, role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_catalog_owner_reads_only_the_six_columns(cur):
    cur.execute("SELECT has_table_privilege('catalog_owner', 'shared.llm_app_profile', 'SELECT')")
    assert cur.fetchone()[0] is False
    cur.execute("""SELECT a.attname FROM pg_attribute a
                    WHERE a.attrelid = 'shared.llm_app_profile'::regclass AND a.attnum > 0 AND NOT a.attisdropped
                      AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""")
    assert {r[0] for r in cur.fetchall()} == set(COLUMNS)
    for priv in ("INSERT", "UPDATE"):
        cur.execute("SELECT has_any_column_privilege('catalog_owner', 'shared.llm_app_profile', %s)", (priv,))
        assert cur.fetchone()[0] is False, priv
    cur.execute("SELECT has_table_privilege('catalog_owner', 'shared.llm_app_profile', 'DELETE')")
    assert cur.fetchone()[0] is False


def test_tripplanner_has_no_privilege_on_the_table(cur):
    for priv in ("SELECT", "UPDATE", "INSERT"):
        cur.execute("SELECT has_any_column_privilege('tripplanner', 'shared.llm_app_profile', %s)", (priv,))
        assert cur.fetchone()[0] is False, priv


def test_view_shows_the_tripplanner_row_only(cur):
    cur.execute("""UPDATE shared.llm_app_profile SET ai_enabled = false, default_daily_budget_usd = 1.2500,
                          disabled_reason = 'zz test switch' WHERE app = 'tripplanner'""")
    cur.execute("UPDATE shared.llm_app_profile SET ai_enabled = false, disabled_reason = 'zz' WHERE app = 'cis'")
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = _rows(cur, TP_PROFILE_SQL)
    assert len(rows) == 1
    row = rows[0]
    assert row["app"] == "tripplanner" and row["ai_enabled"] is False
    assert row["default_daily_budget_usd"] == Decimal("1.2500")
    assert row["degrade_mode"] == "basic_narration" and row["bulk_paused"] is False
    assert _rows(cur, f"SELECT count(*) AS n FROM {VIEW} WHERE app <> 'tripplanner'")[0]["n"] == 0


def test_no_row_when_the_tripplanner_row_is_missing(cur):
    cur.execute("DELETE FROM shared.llm_app_profile WHERE app = 'tripplanner'")
    cur.execute("SET LOCAL ROLE tripplanner")
    assert _rows(cur, TP_PROFILE_SQL) == []
