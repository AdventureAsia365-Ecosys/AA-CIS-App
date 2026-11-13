"""AA-1093 lane L2 — migration 410: catalog.v_tenant_erasure_v1 (contract tenant.erasure.v1), the erasure requests as
AA-Booking and TripPlanner read them. Applied the way DB Migrate applies a cis/ file.

conftest skips 410 in the replay (catalog recipe, 245's owners). This module builds on test_aa860_migration_246's module
fixture (Dev after 244 + 245, db_migrator), applies 410 as db_migrator under the live-owner switch (errors NOT
swallowed), twice. Teardown drops the view, revokes the column grants and unregisters 410.

  * the view: exact columns, owner catalog_owner, security_barrier, booking and tripplanner only; catalog_owner holds
    column SELECT only (never the approver);
  * the rows (as booking, rolled back): one per request and app (the contract's ErasureRequestRow), the app's own
    not_before and step status (active, failed, done, waiting), the approved plan hash of the CURRENT round only and
    only in phase apply / verify.
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
M410 = os.path.join(HERE, "..", "..", "api", "migrations", "410_tenant_erasure_catalog_view.sql")
VIEW = "catalog.v_tenant_erasure_v1"
COLUMNS = ("request_id", "tenant_id", "phase", "round", "opened_at", "approval_expires_at", "is_pilot", "is_synthetic",
           "app", "step_status", "not_before", "approved_plan_hash", "updated_at")
REQ_COLS = ("request_id", "tenant_id", "phase", "round", "opened_at", "approval_expires_at", "is_pilot", "is_synthetic",
            "not_before_booking", "not_before_tripplanner", "round_apps", "done_apps", "updated_at")
STEP_COLS = ("request_id", "round", "app", "mode", "status", "approved_plan_hash")


def undo_410(a):
    a.execute(f"DROP VIEW IF EXISTS {VIEW}")
    a.execute(f"REVOKE SELECT ({', '.join(REQ_COLS)}) ON shared.tenant_erasure_request FROM catalog_owner")
    a.execute(f"REVOKE SELECT ({', '.join(STEP_COLS)}) ON shared.tenant_erasure_step FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '410'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_410(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_410(a)
        t323._apply(M410)
        t323._apply(M410)                                     # idempotent re-run
        yield
    finally:
        undo_410(a)
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


def _request(cur, *, phase="dry_run", round_=1, round_apps="{}", done_apps="{}") -> str:
    rid = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.tenant_erasure_request (request_id, tenant_id, mode, phase, round, opened_at, "
                "is_synthetic, not_before_cis, not_before_booking, not_before_tripplanner, not_before_crm, round_apps, "
                "done_apps, created_by) VALUES (%s, %s, 'drill', %s, %s, '2026-11-01T00:00:00Z', true, "
                "'2026-12-01T00:00:00Z', '2026-12-02T00:00:00Z', '2026-12-03T00:00:00Z', '2027-01-30T00:00:00Z', "
                "%s::text[], %s::text[], 'admin:pytest')",
                (rid, str(uuid.uuid4()), phase, round_, round_apps, done_apps))
    return rid


def _step(cur, rid, app, *, round_=1, approved=None, status="ok"):
    cur.execute("INSERT INTO shared.tenant_erasure_step (request_id, round, app, mode, status, plan_hash, "
                "approved_plan_hash) VALUES (%s, %s, %s, 'dry_run', %s, %s, %s)",
                (rid, round_, app, status, "c" * 64, approved))


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '410'")
    assert cur.fetchone()[0] == 1


def test_columns_owner_barrier_and_consumers(cur):
    cur.execute("""SELECT a.attname FROM pg_attribute a
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
                (VIEW,))
    assert tuple(r[0] for r in cur.fetchall()) == COLUMNS
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class WHERE oid = %s::regclass", (VIEW,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    for role in ("booking", "tripplanner"):
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, VIEW))
        assert cur.fetchone()[0] is True, role
    for role in ("aa_app_user", "cis_app", "crm_app"):
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,))
        if not cur.fetchone()[0]:
            continue
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, VIEW))
        assert cur.fetchone()[0] is False, role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_catalog_owner_reads_columns_only(cur):
    for table in ("shared.tenant_erasure_request", "shared.tenant_erasure_step"):
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT')", (table,))
        assert cur.fetchone()[0] is False, table
    for col in ("approved_by", "snapshot_id", "blockers", "receipt", "combined_fingerprint"):
        cur.execute("SELECT has_column_privilege('catalog_owner', 'shared.tenant_erasure_request', %s, 'SELECT')",
                    (col,))
        assert cur.fetchone()[0] is False, col


def test_rows_per_app_step_status_and_the_current_round_approval(cur):
    rid = _request(cur, phase="apply", round_=2, round_apps="{booking,tripplanner}", done_apps="{cis}")
    _step(cur, rid, "booking", round_=1, approved="d" * 64)       # an older round: never served
    _step(cur, rid, "booking", round_=2, approved="e" * 64)
    _step(cur, rid, "tripplanner", round_=2, approved=None, status="refused")
    waiting = _request(cur, phase="dry_run", round_apps="{}", done_apps="{cis,booking,tripplanner}")
    early = _request(cur, phase="dry_run", round_apps="{cis,booking}")
    _step(cur, early, "booking", approved="f" * 64)               # dry_run: never served before the approval
    cur.execute("SET LOCAL ROLE booking")
    cur.execute(f"SELECT request_id::text, app, phase, round, step_status, not_before::text, approved_plan_hash, "
                f"is_synthetic FROM {VIEW} WHERE request_id = ANY(%s::uuid[])", ([rid, waiting, early],))
    got = {(r[0], r[1]): r[2:] for r in cur.fetchall()}
    assert len(got) == 6
    assert got[(rid, "booking")][:3] == ("apply", 2, "active") and got[(rid, "booking")][4] == "e" * 64
    assert got[(rid, "booking")][3].startswith("2026-12-02") and got[(rid, "booking")][5] is True
    assert got[(rid, "tripplanner")][2] == "failed" and got[(rid, "tripplanner")][3].startswith("2026-12-03")
    assert got[(waiting, "booking")][2] == "done" and got[(waiting, "tripplanner")][2] == "done"
    assert got[(early, "booking")][2] == "active" and got[(early, "booking")][4] is None
    assert got[(early, "tripplanner")][2] == "waiting"


def test_tripplanner_reads_the_view(cur):
    _request(cur)
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute(f"SELECT count(*) FROM {VIEW} WHERE app = 'tripplanner'")
    assert cur.fetchone()[0] >= 1
