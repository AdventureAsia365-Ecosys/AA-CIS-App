"""S279 thresholds lane L5 (AA-1096) — migration 415: catalog.v_app_settings_v1, the TripPlanner settings in force as
TripPlanner reads them. Applied the way DB Migrate applies a cis/ file.

conftest skips 415 in the replay (catalog recipe, 245's owners) and replays 407 and 414. This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator), applies 415 as db_migrator under the
live-owner switch (errors NOT swallowed), twice. Teardown drops the view and the two policies, revokes the column
grants and unregisters 415.

  * the view: exact columns, owner catalog_owner, security_barrier, tripplanner only; catalog_owner holds column SELECT
    only and its policies show it the TripPlanner rows only;
  * the rows (as tripplanner, rolled back): one platform row per key (the default as version 0, then the approved value
    in force; a future-dated or merely proposed value is not served), one row per tenant override in force;
  * isolation: tripplanner sees no CIS key, and a role granted the view by mistake (booking here) sees no row.
Invented values only.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M415 = os.path.join(HERE, "..", "..", "api", "migrations", "415_app_settings_catalog_view.sql")
VIEW = "catalog.v_app_settings_v1"
COLUMNS = ("app", "key", "value", "version", "tenant_id", "effective_from")
DEF_COLS = ("key", "app", "default_value", "created_at")
VAL_COLS = ("key", "tenant_id", "value", "version", "status", "effective_from")
TENANT = "00000000-0000-4000-8000-0000000a1096"
TKEY = "tripplanner.leads.tenant_daily_cap"          # platform+tenant, default 200
PKEY = "tripplanner.rate.narrate_per_hour"           # platform, default 10
REASON = "zz test: a reason of at least twenty characters"


def undo_415(a):
    a.execute(f"DROP VIEW IF EXISTS {VIEW}")
    a.execute("DROP POLICY IF EXISTS setting_definition_catalog_owner_read ON shared.setting_definition")
    a.execute("DROP POLICY IF EXISTS setting_value_catalog_owner_read ON shared.setting_value")
    a.execute(f"REVOKE SELECT ({', '.join(DEF_COLS)}) ON shared.setting_definition FROM catalog_owner")
    a.execute(f"REVOKE SELECT ({', '.join(VAL_COLS)}) ON shared.setting_value FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '415'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_415(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_415(a)
        t323._apply(M415)
        t323._apply(M415)                                     # idempotent re-run
        yield
    finally:
        undo_415(a)
        admin.close()


@pytest.fixture
def cur():
    """A rolled-back transaction with RLS ENABLE + FORCE on both tables, as on Dev (CIS 407): the replay's X1 shaping
    may have taken it off, and the view must answer through catalog_owner's policies, not around them."""
    c = t245._connect()
    c.autocommit = False
    try:
        k = c.cursor()
        for table in ("shared.setting_definition", "shared.setting_value"):
            k.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY")
        yield k
    finally:
        c.rollback()
        c.close()


def _value(cur, key, value, *, version, tenant=None, approve=True, effective="now()"):
    cur.execute(f"INSERT INTO shared.setting_value (key, tenant_id, value, version, effective_from, proposed_by, "
                f"reason) VALUES (%s, %s::uuid, %s::jsonb, %s, {effective}, 'admin:zz-a', %s) RETURNING id",
                (key, tenant, json.dumps(value), version, REASON))
    vid = cur.fetchone()[0]
    if approve:
        cur.execute("UPDATE shared.setting_value SET status = 'approved', decided_by = 'admin:zz-b', "
                    "decided_at = greatest(now(), proposed_at) WHERE id = %s", (vid,))
    return vid


def _tenant(cur):
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) "
                "VALUES (%s::uuid, 'Zz Lantern Trails', 'zz-lantern-trails', 3)", (TENANT,))


def _rows(cur, role="tripplanner"):
    cur.execute(f"SET LOCAL ROLE {role}")
    cur.execute(f"SELECT app, key, value::text, version, tenant_id::text FROM {VIEW}")
    rows = cur.fetchall()
    cur.execute("RESET ROLE")
    return rows


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '415'")
    assert cur.fetchone()[0] == 1


def test_columns_owner_barrier_and_consumers(cur):
    cur.execute("""SELECT a.attname FROM pg_attribute a
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
                (VIEW,))
    assert tuple(r[0] for r in cur.fetchall()) == COLUMNS
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
    for table in ("shared.setting_definition", "shared.setting_value"):
        cur.execute("SELECT has_any_column_privilege('tripplanner', %s, 'SELECT')", (table,))
        assert cur.fetchone()[0] is False, table
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT')", (table,))
        assert cur.fetchone()[0] is False, table
    for col in ("reason", "proposed_by", "decided_by", "decision_note"):
        cur.execute("SELECT has_column_privilege('catalog_owner', 'shared.setting_value', %s, 'SELECT')", (col,))
        assert cur.fetchone()[0] is False, col


def test_defaults_are_served_as_version_0_one_platform_row_per_key(cur):
    rows = _rows(cur)
    cur.execute("SELECT count(*) FROM shared.setting_definition WHERE app = 'tripplanner'")
    assert len(rows) == cur.fetchone()[0] == 17
    by_key = {r[1]: r for r in rows}
    assert by_key[PKEY][2:] == ("10", 0, None) and by_key[TKEY][2:] == ("200", 0, None)
    assert {r[0] for r in rows} == {"tripplanner"}


def test_the_approved_value_in_force_wins_not_a_proposal_or_a_future_one(cur):
    _value(cur, PKEY, 12, version=1)
    _value(cur, PKEY, 14, version=2, effective="now() + interval '1 day'")    # approved, not in force yet
    _value(cur, "tripplanner.rate.search_per_minute", 45, version=1, approve=False)   # a proposal only
    by_key = {r[1]: r for r in _rows(cur) if r[4] is None}
    assert by_key[PKEY][2:4] == ("12", 1)
    assert by_key["tripplanner.rate.search_per_minute"][2:4] == ("30", 0)


def test_a_tenant_override_is_its_own_row(cur):
    _tenant(cur)
    _value(cur, TKEY, 300, version=1)
    _value(cur, TKEY, 25, version=1, tenant=TENANT)
    rows = [r for r in _rows(cur) if r[1] == TKEY]
    assert sorted(((r[4], r[2], r[3]) for r in rows), key=str) == sorted([(None, "300", 1), (TENANT, "25", 1)],
                                                                         key=str)


def test_tripplanner_never_sees_another_apps_rows(cur):
    _value(cur, "cis.publish.daily_limit_default", 21, version=1)
    assert all(r[1].startswith("tripplanner.") for r in _rows(cur))
    cur.execute("SET LOCAL ROLE catalog_owner")
    cur.execute("SELECT count(key) FROM shared.setting_definition WHERE app <> 'tripplanner'")
    assert cur.fetchone()[0] == 0                                       # its policy shows TripPlanner rows only
    cur.execute("SELECT count(key) FROM shared.setting_value WHERE key NOT LIKE 'tripplanner.%'")
    assert cur.fetchone()[0] == 0
    cur.execute("RESET ROLE")


def test_a_role_granted_the_view_by_mistake_sees_nothing(cur):
    cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booking')")
    if not cur.fetchone()[0]:
        pytest.skip("no booking role in this database")
    cur.execute(f"GRANT SELECT ON {VIEW} TO booking")                  # rolled back with the test
    assert _rows(cur, "booking") == []
