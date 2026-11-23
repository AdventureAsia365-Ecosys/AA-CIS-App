"""AA-1160 (S289) — migration 475: shared.tenant_crm_agency (when the CRM last proved a tenant's agency, as CIS sees
it), its sighting window setting cis.tenant.crm_agency_seen_hours, and catalog.v_tenant_tripplanner_v1 appends the
agency's fallback route (contract tenant.entitlements.v1 1.11).

475's Part B is the catalog recipe and needs 357 + 377 (438 replaces the billing view too), 379 + 390 + 429 + 438, all
skipped by the replay (the replay runs 475's Part A only: the table). This module builds on the 245 / 246 module
fixtures (Dev after 244 + 245, db_migrator), applies 357, 377, 379, 390, 429, 438 and 475 as db_migrator (errors NOT
swallowed), 475 twice, and undoes them at teardown (DROP VIEW; the table and the setting row stay, the replay made
them).

  * the view's exact columns (438's first, then the three appended), owner catalog_owner, security_barrier,
    tripplanner only; neither tripplanner nor catalog_owner holds anything on the table (the view does not read it);
  * the table refuses an unknown source and a last sighting before the first, and goes with its tenant;
  * the setting row: int hours 2-168, default 3, platform, maker-checker, enforced, with its EN + VI guide;
  * as tripplanner, with TripPlanner's AA-1160 route SELECT: lead_channel keeps its 390 meaning (crm while crm.core is
    live) and the fallback columns carry the stored webhook / e-mail route whatever crm.core says (a marker row
    changes nothing); a channel without its target falls back to none.
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
M475 = os.path.join(HERE, "..", "..", "api", "migrations", "475_aa1160_tenant_crm_agency_marker.sql")
TP = "catalog.v_tenant_tripplanner_v1"
MARKER = "shared.tenant_crm_agency"
COLUMNS = t438.TP_COLUMNS + ", fallback_channel text, fallback_webhook_url text, fallback_email text"
# The route SELECT of AA-TripPlanner-Web backend/assembly/leads.py ROUTE_SQL after AA-1160 (column names, never *).
TP_ROUTE_SQL = (f"SELECT tenant_id, slug, name, kind, served, in_service, crm_core, lead_channel, lead_webhook_url, "
                f"lead_email, fallback_channel, fallback_webhook_url, fallback_email FROM {TP} "
                f"WHERE tenant_id = %s")


def undo_475(a):
    a.execute("DELETE FROM shared.schema_versions WHERE version = '475'")
    # The appended view columns go with 379's undo (DROP VIEW); the table and the setting row stay (the replay's
    # Part A made them).


@pytest.fixture(scope="module", autouse=True)
def applied_475(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()

    def undo():
        undo_475(a)
        t438.undo_438(a)
        t429.undo_429(a)
        t390.undo_390(a)
        t379.undo_379(a)
        t377.undo_377(a)
        t357.undo_357(a)

    try:
        undo()
        for path in (t357.M357, t377.M377, t379.M379, t390.M390, t429.M429, t438.M438, M475, M475):
            t323._apply(path)                                 # 475 twice: idempotent re-run
        yield
    finally:
        undo()
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


def _as(cur, role, sql, *args):
    cur.execute("SAVEPOINT zz_as")
    cur.execute(f"SET LOCAL ROLE {role}")
    try:
        cur.execute(sql, args)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT zz_as")


def _route(cur, tid):
    rows = _as(cur, "tripplanner", TP_ROUTE_SQL, tid)
    assert len(rows) == 1
    return rows[0]


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '475'")
    assert cur.fetchone()[0] == 1


def test_columns_owner_barrier_and_consumers(cur):
    cur.execute("SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum) "
                "AS c FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped",
                (TP,))
    assert cur.fetchone()[0] == COLUMNS
    cur.execute("SELECT pg_get_userbyid(relowner) AS o, 'security_barrier=true' = ANY(reloptions) AS b "
                "FROM pg_class WHERE oid = %s::regclass", (TP,))
    assert cur.fetchone() == ("catalog_owner", True)
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (["booking", "tripplanner", "aa_app_user", "cis_app", "crm_app"],))
    for (role,) in cur.fetchall():
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, TP))
        assert cur.fetchone()[0] is (role == "tripplanner"), role


def test_table_grants(cur):
    cur.execute("SELECT has_table_privilege('cis_app', %s, 'SELECT, INSERT, UPDATE, DELETE') AS app, "
                "has_table_privilege('tripplanner', %s, 'SELECT') AS tp, "
                "has_column_privilege('catalog_owner', %s, 'tenant_id', 'SELECT') AS id_read, "
                "has_column_privilege('catalog_owner', %s, 'last_seen_at', 'SELECT') AS seen_read, "
                "relrowsecurity AS rls FROM pg_class WHERE oid = %s::regclass",
                (MARKER, MARKER, MARKER, MARKER, MARKER))
    assert cur.fetchone() == (True, False, False, False, False)


def test_seen_window_setting(cur):
    cur.execute("SELECT app, area, value_type, unit, min_value::int AS lo, max_value::int AS hi, default_value, "
                "scope_kind, approval, enforced, guide ? 'en' AND guide ? 'vi' AS guided "
                "FROM shared.setting_definition WHERE key = 'cis.tenant.crm_agency_seen_hours'")
    assert cur.fetchone() == ("cis", "tenant", "int", "hours", 2, 168, 3, "platform", "maker_checker", True, True)


def test_table_checks_and_cascade(cur):
    tid, _slug = t379._tenant(cur, "Zz Marrowfield Journeys")
    for source, first, last in (("crm_guess", "now()", "now()"),
                                ("crm_pull", "now()", "now() - interval '1 hour'")):
        cur.execute("SAVEPOINT zz_bad")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(f"INSERT INTO {MARKER} (tenant_id, source, first_seen_at, last_seen_at) "
                        f"VALUES (%s, %s, {first}, {last})", (tid, source))
        cur.execute("ROLLBACK TO SAVEPOINT zz_bad")
    cur.execute(f"INSERT INTO {MARKER} (tenant_id, source) VALUES (%s, 'crm_onboarding') "
                f"RETURNING first_seen_at = last_seen_at AS same", (tid,))
    assert cur.fetchone()[0] is True
    # The row goes with its tenant (the tenant's own append-only guards make a real delete a saga, AA-839).
    cur.execute("SELECT confdeltype AS d, confrelid::regclass::text AS parent FROM pg_constraint "
                "WHERE conname = 'tenant_crm_agency_tenant_id_fkey'")
    assert cur.fetchone() == ("c", "shared.tenants")


def test_tripplanner_reads_marker_and_fallback(cur):
    crm_hook, _ = t379._tenant(cur, "Zz Willowmere Treks synthetic")
    t390._subscribe(cur, crm_hook, "crm", "crm.core")
    t390._site(cur, crm_hook, lead_channel="webhook", lead_webhook_url="https://willowmere.example.invalid/leads")
    row = _route(cur, crm_hook)
    # 390's meaning kept: crm while crm.core is live, its targets NULL; no CRM agency yet; the stored route as fallback
    assert (row["crm_core"], row["lead_channel"], row["lead_webhook_url"], row["lead_email"]) == (
        True, "crm", None, None)
    assert (row["fallback_channel"], row["fallback_webhook_url"], row["fallback_email"]) == (
        "webhook", "https://willowmere.example.invalid/leads", None)
    # A CRM sighting is CIS's own display input; the view (TripPlanner's route) does not change with it.
    cur.execute(f"INSERT INTO {MARKER} (tenant_id, source) VALUES (%s, 'crm_pull')", (crm_hook,))
    assert _route(cur, crm_hook) == row

    mail, _ = t379._tenant(cur, "Zz Saltmarsh Holidays synthetic")
    t390._site(cur, mail, lead_channel="email", lead_email="desk@saltmarsh.example.invalid")
    row = _route(cur, mail)
    assert (row["lead_channel"], row["lead_email"], row["fallback_channel"], row["fallback_email"],
            row["fallback_webhook_url"]) == (
        "email", "desk@saltmarsh.example.invalid", "email", "desk@saltmarsh.example.invalid", None)

    bare, _ = t379._tenant(cur, "Zz Thistledown Tours synthetic")
    t390._subscribe(cur, bare, "crm", "crm.core")
    row = _route(cur, bare)
    assert (row["lead_channel"], row["fallback_channel"], row["fallback_webhook_url"], row["fallback_email"]) == (
        "crm", "none", None, None)
