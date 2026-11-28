"""AA-1191 — migration 498: catalog.v_billing_currencies_v1, the enabled currencies as Booking reads them. Applied the
way DB Migrate applies a cis/ file.

conftest skips 498 in the replay (catalog recipe, 245's owners) and replays 435 + 497. This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator), applies 498 as db_migrator under the
live-owner switch (errors NOT swallowed), twice. Teardown drops the view and unregisters 498.

  * the view: exact columns, owner catalog_owner, security_barrier, booking only;
  * the row (as booking, rolled back): always one row, {USD} by default, the approved value in force (USD always first,
    no duplicate, list order kept), a proposal or a future-dated approval not served.
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
M498 = os.path.join(HERE, "..", "..", "api", "migrations", "498_aa1191_catalog_billing_currencies_view.sql")
VIEW = "catalog.v_billing_currencies_v1"
KEY = "cis.billing.enabled_currencies"
REASON = "zz test: a reason of at least twenty characters"


def undo_498(a):
    a.execute(f"DROP VIEW IF EXISTS {VIEW}")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '498'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_498(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_498(a)
        t323._apply(M498)
        t323._apply(M498)                                     # idempotent re-run
        yield
    finally:
        undo_498(a)
        admin.close()


@pytest.fixture
def cur():
    """A rolled-back transaction (as the 438 test: the value in force comes from 435's view, read as its owner)."""
    c = t245._connect()
    c.autocommit = False
    try:
        yield c.cursor()
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


def _rows(cur, role="booking"):
    cur.execute(f"SET LOCAL ROLE {role}")
    cur.execute(f"SELECT enabled_currencies, base_currency FROM {VIEW}")
    rows = cur.fetchall()
    cur.execute("RESET ROLE")
    return rows


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '498'")
    assert cur.fetchone()[0] == 1


def test_columns_owner_barrier_and_consumers(cur):
    cur.execute("""SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
                (VIEW,))
    assert cur.fetchall() == [("enabled_currencies", "text[]"), ("base_currency", "text")]
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


def test_usd_only_by_default(cur):
    assert _rows(cur) == [(["USD"], "USD")]


def test_the_approved_value_in_force_is_served_usd_first(cur):
    _value(cur, ["VND", "USD", "VND"], version=1)
    assert _rows(cur) == [(["USD", "VND"], "USD")]


def test_usd_stays_when_the_list_leaves_it_out(cur):
    _value(cur, ["VND"], version=1)
    assert _rows(cur) == [(["USD", "VND"], "USD")]


def test_a_proposal_is_not_served(cur):
    _value(cur, ["USD", "VND"], version=1, approve=False)
    assert _rows(cur) == [(["USD"], "USD")]


def test_a_future_dated_approval_is_not_served(cur):
    _value(cur, ["USD", "VND"], version=1, effective="now() + interval '1 day'")
    assert _rows(cur) == [(["USD"], "USD")]
