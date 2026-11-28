"""AA-1054 (S297) — migration 496: catalog.v_pilot_fence_v1, the pilot fence switch in force (CIS 495) as AA-Booking
reads it. Applied the way DB Migrate applies a cis/ file.

conftest skips 496 in the replay (catalog recipe, 245's owners) and replays 407 and 495. This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator), applies 496 as db_migrator under the
live-owner switch (errors NOT swallowed), twice. Teardown drops the view and the two policies, revokes the column
grants and unregisters 496.

  * the view: exact columns, owner catalog_owner, security_barrier, booking only; booking holds nothing on the two
    settings tables; catalog_owner holds column SELECT only and its new policies show it this one key only;
  * the switch both ways (as booking, rolled back): the default (off) as version 0; an approved true in force turns it
    on; a merely proposed or a future-dated value does not; a later approved false turns it off again;
  * fail closed: no definition row -> no row (the Booking reader then refuses the E2E identities);
  * the SELECT AA-Booking runs (backend/app/auth/pilot_fence.py FENCE_SQL) answers as booking.
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
M496 = os.path.join(HERE, "..", "..", "api", "migrations", "496_aa1054_pilot_fence_catalog_view.sql")
VIEW = "catalog.v_pilot_fence_v1"
KEY = "cis.platform.pilot_e2e_fence"
COLUMNS = ("fence_on", "version", "effective_from")
DEF_COLS = ("key", "app", "default_value", "created_at")
VAL_COLS = ("key", "tenant_id", "value", "version", "status", "effective_from")
REASON = "zz test: a reason of at least twenty characters"
# The SELECT of AA-Booking backend/app/auth/pilot_fence.py (column names, never *).
BK_FENCE_SQL = f"SELECT fence_on FROM {VIEW}"


def undo_496(a):
    a.execute(f"DROP VIEW IF EXISTS {VIEW}")
    a.execute("DROP POLICY IF EXISTS setting_definition_pilot_fence_read ON shared.setting_definition")
    a.execute("DROP POLICY IF EXISTS setting_value_pilot_fence_read ON shared.setting_value")
    a.execute("SELECT 1 FROM shared.schema_versions WHERE version = '415'")
    if a.fetchone() is None:                                  # 415's view needs the same column grants
        a.execute(f"REVOKE SELECT ({', '.join(DEF_COLS)}) ON shared.setting_definition FROM catalog_owner")
        a.execute(f"REVOKE SELECT ({', '.join(VAL_COLS)}) ON shared.setting_value FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '496'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_496(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_496(a)
        t323._apply(M496)
        t323._apply(M496)                                     # idempotent re-run
        yield
    finally:
        undo_496(a)
        admin.close()


@pytest.fixture
def cur():
    """A rolled-back transaction with RLS ENABLE + FORCE on both tables, as on Dev (CIS 407)."""
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


def _value(cur, value, *, version, approve=True, effective="now()"):
    cur.execute(f"INSERT INTO shared.setting_value (key, value, version, effective_from, proposed_by, reason) "
                f"VALUES (%s, %s::jsonb, %s, {effective}, 'admin:zz-a', %s) RETURNING id",
                (KEY, json.dumps(value), version, REASON))
    vid = cur.fetchone()[0]
    if approve:
        cur.execute("UPDATE shared.setting_value SET status = 'approved', decided_by = 'admin:zz-b', "
                    "decided_at = greatest(now(), proposed_at) WHERE id = %s", (vid,))
    return vid


def _rows(cur, role="booking"):
    cur.execute(f"SET LOCAL ROLE {role}")
    cur.execute(f"SELECT fence_on AS fence_on, version AS version FROM {VIEW}")
    rows = cur.fetchall()
    cur.execute("RESET ROLE")
    return rows


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '496'")
    assert cur.fetchone()[0] == 1


def test_columns_owner_barrier_and_consumers(cur):
    cur.execute("""SELECT a.attname FROM pg_attribute a
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
                (VIEW,))
    assert tuple(r[0] for r in cur.fetchall()) == COLUMNS
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
    for table in ("shared.setting_definition", "shared.setting_value"):
        for priv in ("SELECT", "INSERT", "UPDATE"):
            cur.execute("SELECT has_any_column_privilege('booking', %s, %s)", (table, priv))
            assert cur.fetchone()[0] is False, (table, priv)
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT')", (table,))
        assert cur.fetchone()[0] is False, table
    for col in ("reason", "proposed_by", "decided_by", "decision_note"):
        cur.execute("SELECT has_column_privilege('catalog_owner', 'shared.setting_value', %s, 'SELECT')", (col,))
        assert cur.fetchone()[0] is False, col


def test_catalog_owner_sees_only_this_key(cur):
    _value(cur, True, version=1)
    cur.execute("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason) "
                "VALUES ('cis.platform.admin_mfa_required', 'true'::jsonb, 1, 'admin:zz-a', %s)", (REASON,))
    cur.execute("SET LOCAL ROLE catalog_owner")
    cur.execute("SELECT count(key) AS n FROM shared.setting_definition WHERE app <> 'tripplanner' AND key <> %s",
                (KEY,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(key) AS n FROM shared.setting_value WHERE key NOT LIKE 'tripplanner.%%' AND key <> %s",
                (KEY,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(key) AS n FROM shared.setting_value WHERE key = %s", (KEY,))
    assert cur.fetchone()[0] == 1
    cur.execute("RESET ROLE")


def test_default_is_off_as_version_0(cur):
    assert _rows(cur) == [(False, 0)]


def test_an_approved_true_turns_it_on_and_a_later_false_off_again(cur):
    _value(cur, True, version=1)
    assert _rows(cur) == [(True, 1)]
    _value(cur, False, version=2, effective="now() + interval '1 day'")
    assert _rows(cur) == [(True, 1)]                          # approved, not in force yet
    _value(cur, False, version=3)
    assert _rows(cur) == [(False, 3)]


def test_a_proposal_does_not_count(cur):
    _value(cur, True, version=1, approve=False)
    assert _rows(cur) == [(False, 0)]


def test_a_future_value_does_not_count(cur):
    _value(cur, True, version=1, effective="now() + interval '1 day'")
    assert _rows(cur) == [(False, 0)]


def test_no_definition_means_no_row(cur):
    cur.execute("ALTER TABLE shared.setting_definition DISABLE TRIGGER USER")
    cur.execute("DELETE FROM shared.setting_definition WHERE key = %s", (KEY,))
    assert _rows(cur) == []


def test_the_booking_select_answers(cur):
    cur.execute("SET LOCAL ROLE booking")
    cur.execute(BK_FENCE_SQL)
    assert cur.fetchall() == [(False,)]
    cur.execute("RESET ROLE")
