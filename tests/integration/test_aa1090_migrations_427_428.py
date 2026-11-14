"""AA-1090 TripPlanner R8 (S281) — migrations 427 and 428: the custom hosts of a tenant's TripPlanner site.

427 (shared.tenant_tripplanner_domain + the four custom-domain settings) is replayed by conftest like any file, so the
table exists here before this module runs. 428 (custom_domains appended to catalog.v_tenant_tripplanner_v1, catalog
recipe) is skipped by the replay; this module builds on the 390 module's fixtures (Dev after 244 + 245, db_migrator),
applies 379, 390 and 428 as db_migrator (errors NOT swallowed), 427 and 428 twice. Teardown drops what 428 added and
undoes 390 + 379, so later modules see the replay state.

  * 427: registered once; RLS off (rule a allowlist, like the site table); grants (cis_app only); the domain,
    status, certificate and target CHECKs refuse bad values; one host is one site (unique); the four settings exist
    with enforced = false until the code reads them;
  * 428: the view's exact columns and types, owner catalog_owner, security_barrier, tripplanner only; catalog_owner
    reads exactly tenant_id, domain, status of the domain table; tripplanner holds
    nothing on the table;
  * the view (as tripplanner): custom_domains lists a tenant's ACTIVE hosts only, sorted; another tenant's host never
    shows; the demo tenant has none; the SELECT AA-TripPlanner-Web runs for a Host (backend/browse/tenant_site.py
    HOST_SQL) answers the tenant of an active host and nothing for a pending one.
Invented names only (example.invalid hosts).
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa1038_migration_379 as t379  # noqa: E402
import test_aa1038_migration_390 as t390  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M427 = os.path.join(HERE, "..", "..", "api", "migrations", "427_aa1090_tenant_tripplanner_domain.sql")
M428 = os.path.join(HERE, "..", "..", "api", "migrations", "428_aa1090_tenant_tripplanner_custom_domains_view.sql")
VIEW = t379.VIEW
DOMAIN = "shared.tenant_tripplanner_domain"
DEMO = t379.DEMO
# Copied verbatim into AA-TripPlanner-Web backend/tests/test_aa1090_tenant_host_pg.py (CIS_428_COLUMNS).
COLUMNS = t390.COLUMNS + ", custom_domains text[]"
DOMAIN_COLUMNS = ("tenant_id", "domain", "status")
SETTINGS = ("cis.tripplanner.custom_domain_max_per_tenant", "cis.tripplanner.custom_domain_edge_max",
            "cis.tripplanner.custom_domain_validation_timeout_hours", "cis.tripplanner.custom_domain_poll_seconds")
# The SELECT of AA-TripPlanner-Web backend/browse/tenant_site.py HOST_SQL (column names, never *).
TP_HOST_SQL = (f"SELECT tenant_id, slug, name, kind, in_service, served, tp_launcher, tp_branding, theme, locale "
               f"FROM {VIEW} WHERE %s = ANY (custom_domains)")
# Built at run time: the repo's resource-ID check refuses an ARN with an account number in a public repo.
ACCOUNT = "0" * 12
ARN = f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/0a1b2c3d-0a1b-4c2d-8e3f-0a1b2c3d4e5f"


def undo_428(a):
    a.execute(f"REVOKE SELECT ({', '.join(DOMAIN_COLUMNS)}) ON {DOMAIN} FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '428'")
    # The appended view column goes with 379's undo (DROP VIEW).


@pytest.fixture(scope="module", autouse=True)
def applied_428(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_428(a)
        t390.undo_390(a)
        t379.undo_379(a)
        t323._apply(t379.M379)
        t323._apply(t390.M390)
        t323._apply(M427)                                     # replayed already: the re-run is a no-op
        t323._apply(M428)
        t323._apply(M428)                                     # idempotent re-run
        yield
    finally:
        undo_428(a)
        t390.undo_390(a)
        t379.undo_379(a)
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


_rows = t379._rows


def _tenant(cur, name, **kw):
    return t379._tenant(cur, name, **kw)


def _host(cur, tid, domain, status="active", **cols):
    cols.setdefault("updated_by", "test:427")
    names = ", ".join(["tenant_id", "domain", "status", *cols])
    marks = ", ".join(["%s"] * (3 + len(cols)))
    cur.execute(f"INSERT INTO {DOMAIN} ({names}) VALUES ({marks})", (tid, domain, status, *cols.values()))


# ── 427 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", ["427", "428"])
def test_registered_once(cur, version):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = %s", (version,))
    assert cur.fetchone()[0] == 1


def test_rls_off_like_the_other_control_plane_tables(cur):
    cur.execute("SELECT relrowsecurity AS on_ FROM pg_class WHERE oid = %s::regclass", (DOMAIN,))
    assert cur.fetchone()[0] is False
    cur.execute("SELECT count(*) AS n FROM pg_policies WHERE schemaname = 'shared' "
                "AND tablename = 'tenant_tripplanner_domain'")
    assert cur.fetchone()[0] == 0


def test_grants_cis_app_only(cur):
    cur.execute("SELECT has_table_privilege('cis_app', %s, 'SELECT, INSERT, UPDATE, DELETE') AS ok", (DOMAIN,))
    assert cur.fetchone()[0] is True
    for role in ("aa_app_user", "tripplanner", "public"):
        cur.execute("SELECT has_any_column_privilege(%s, %s, 'SELECT') AS ok", (role, DOMAIN))
        assert cur.fetchone()[0] is False, role


@pytest.mark.parametrize("domain", [
    "Trip.Quillmere.example.invalid", "trip..quillmere.invalid", "-trip.quillmere.invalid", "trip.quillmere.invalid.",
    "trip_x.quillmere.invalid", "https://trip.quillmere.invalid", "localhost", "10.0.0.1", "a.b",
    ("x" * 64) + ".quillmere.invalid",
])
def test_domain_check_refuses_what_is_not_a_host(cur, domain):
    tid, _ = _tenant(cur, "Quillmere Voyages synthetic")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _host(cur, tid, domain)


@pytest.mark.parametrize("cols", [
    {"status": "live"},
    {"certificate_arn": ARN.replace("us-east-1", "us-west-1")},
    {"cname_target": "quillmere.example.invalid"},
    {"failure_code": "Not A Code"},
    {"failure_message": "x" * 501},
])
def test_other_checks_refuse_bad_values(cur, cols):
    tid, _ = _tenant(cur, "Larchfield Expeditions synthetic")
    status = cols.pop("status", "failed")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _host(cur, tid, "trip.larchfield.example.invalid", status, **cols)


def test_a_valid_row_and_one_host_is_one_site(cur):
    a, _ = _tenant(cur, "Ottervale Journeys synthetic")
    b, _ = _tenant(cur, "Saltmarsh Routes synthetic")
    _host(cur, a, "trip.ottervale.example.invalid", "validating", certificate_arn=ARN,
          validation_name="_0a1b2c3d.trip.ottervale.example.invalid.",
          validation_value="_9f8e7d6c.acm-validations.aws.", cname_target="d111111abcdef8.cloudfront.net")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _host(cur, b, "trip.ottervale.example.invalid", "pending")


def test_the_four_settings_are_defined_not_enforced(cur):
    cur.execute("SELECT key, enforced, area FROM shared.setting_definition WHERE key = ANY(%s) ORDER BY key",
                (list(SETTINGS),))
    rows = cur.fetchall()
    assert sorted(r[0] for r in rows) == sorted(SETTINGS)
    assert all(r[1] is False and r[2] == "tripplanner" for r in rows)


# ── 428 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_view_columns_owner_barrier_and_consumers(cur):
    cur.execute("""SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
                     AS cols FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                      AND NOT a.attisdropped""", (VIEW,))
    assert cur.fetchone()[0] == COLUMNS
    cur.execute("SELECT pg_get_userbyid(relowner) AS o, reloptions AS r FROM pg_class WHERE oid = %s::regclass",
                (VIEW,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    cur.execute("SELECT has_table_privilege('tripplanner', %s, 'SELECT') AS ok", (VIEW,))
    assert cur.fetchone()[0] is True
    for role in ("booking", "aa_app_user", "cis_app", "crm_app"):
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) AS ok", (role,))
        if cur.fetchone()[0]:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, VIEW))
            assert cur.fetchone()[0] is False, role


def test_catalog_owner_reads_three_columns(cur):
    cur.execute("""SELECT string_agg(a.attname, ',' ORDER BY a.attname) AS cols FROM pg_attribute a
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                      AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""", (DOMAIN,))
    assert cur.fetchone()[0] == "domain,status,tenant_id"
    for priv in ("INSERT", "UPDATE"):
        cur.execute("SELECT has_any_column_privilege('catalog_owner', %s, %s) AS ok", (DOMAIN, priv))
        assert cur.fetchone()[0] is False, priv


def test_demo_tenant_has_no_custom_host(cur):
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute(f"SELECT custom_domains FROM {VIEW} WHERE tenant_id = %s", (DEMO,))
    assert cur.fetchone()[0] == []


def test_custom_domains_lists_active_hosts_sorted_per_tenant(cur):
    a, slug_a = _tenant(cur, "Moorgate Passage synthetic")
    b, _ = _tenant(cur, "Bramblecourt Travel synthetic")
    _host(cur, a, "trip.moorgate.example.invalid", "active")
    _host(cur, a, "go.moorgate.example.invalid", "active")
    for i, status in enumerate(("pending", "validating", "attaching", "removing", "failed")):
        _host(cur, a, f"s{i}.moorgate.example.invalid", status)
    _host(cur, b, "trip.bramblecourt.example.invalid", "active")
    cur.execute("SET LOCAL ROLE tripplanner")
    by = {r["tenant_id"]: r["custom_domains"] for r in _rows(
        cur, f"SELECT tenant_id::text AS tenant_id, custom_domains FROM {VIEW} WHERE tenant_id = ANY(%s::uuid[])",
        [a, b])}
    assert by[a] == ["go.moorgate.example.invalid", "trip.moorgate.example.invalid"]
    assert by[b] == ["trip.bramblecourt.example.invalid"]


def test_the_tripplanner_host_lookup(cur):
    tid, slug = _tenant(cur, "Inkwell Coast synthetic")
    _host(cur, tid, "trip.inkwell.example.invalid", "active")
    _host(cur, tid, "new.inkwell.example.invalid", "validating")
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = _rows(cur, TP_HOST_SQL, "trip.inkwell.example.invalid")
    assert len(rows) == 1 and rows[0]["slug"] == slug and rows[0]["served"] is True
    assert str(uuid.UUID(str(rows[0]["tenant_id"]))) == tid
    assert _rows(cur, TP_HOST_SQL, "new.inkwell.example.invalid") == []
    assert _rows(cur, TP_HOST_SQL, "unknown.example.invalid") == []


def test_390_reads_still_answer(cur):
    cur.execute("SET LOCAL ROLE tripplanner")
    row = _rows(cur, t390.TP_ROUTE_SQL, DEMO)[0]
    assert row["lead_channel"] == "none"
