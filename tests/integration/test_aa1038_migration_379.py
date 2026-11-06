"""AA-1038 R5 slice 2 (S271) — migration 379: shared.tenant_tripplanner_site, catalog.v_tenant_tripplanner_v1
(TripPlanner's read of its tenants, root ADR 0019 d10) and the demo tenant aa-tripplanner, applied the way DB Migrate
applies a cis/ file since X1 S22.

conftest skips 379 in the replay (catalog recipe, 245's owners). This module builds on test_aa860_migration_246's
module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships), applies 379 as db_migrator under the
live-owner switch (errors NOT swallowed), twice. Teardown drops the view and the table, revokes the grants and removes
the demo tenant's rows, so later modules see the replay state.

  * the migration: registered once; the view's exact columns and types, owner catalog_owner, security_barrier,
    tripplanner only; catalog_owner reads only the granted columns and writes nothing; tripplanner has no privilege on
    the base tables;
  * the demo tenant: one row, kind demo, the content.internal plan, a live TripPlanner subscription, served;
  * the view (as tripplanner, rolled back): a tenant without a TripPlanner subscription has no row; an agency with
    tripplanner.hosted is served with its theme and locale; a suspended one is not; kind internal never shows; the
    BO-8 badge flag follows an override; two synthetic agencies each see only their own settings by slug;
  * the theme CHECK refuses what is not a kit token -> #rrggbb object;
  * the SELECT AA-TripPlanner-Web runs (backend/browse/tenant_site.py TENANT_SQL) answers as tripplanner;
  * a bound agency that owns a live catalogue tour of its own reads only AA master tours through the catalog views
    (behaviour, as tripplanner, with a control that the GUC does add the agency's row at the table level).
Invented names only.
"""
import json
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)
from conftest import BATCH_ID  # noqa: E402

HERE = os.path.dirname(__file__)
M379 = os.path.join(HERE, "..", "..", "api", "migrations", "379_tenant_tripplanner_catalog_view.sql")
VIEW = "catalog.v_tenant_tripplanner_v1"
SITE = "shared.tenant_tripplanner_site"
DEMO = "00000000-0000-0000-0000-000000000002"
# Copied verbatim into AA-TripPlanner-Web backend/tests/test_aa1038_tenant_site_pg.py (CIS_379_COLUMNS).
COLUMNS = (
    "tenant_id uuid, slug text, name text, kind text, account_status text, in_service boolean, served boolean, "
    "tp_launcher boolean, tp_branding boolean, theme jsonb, locale text"
)
TENANT_COLUMNS = ("tenant_id", "slug", "name", "account_status", "kind")
SUB_COLUMNS = ("tenant_id", "product")
# The SELECT of AA-TripPlanner-Web backend/browse/tenant_site.py (column names, never *).
TP_TENANT_SQL = (f"SELECT tenant_id, slug, name, kind, in_service, served, tp_launcher, tp_branding, theme, locale "
                 f"FROM {VIEW} WHERE slug = %s")
# Tables that hold rows of the demo tenant after 379 (the triggers write them); removed at teardown.
DEMO_ROW_TABLES = ("shared.tenant_entitlement_override", "shared.tenant_entitlement", "shared.tenant_subscription",
                   "shared.tenant_account_event", "shared.tenant_membership_event")


def undo_379(a):
    a.execute(f"DROP VIEW IF EXISTS {VIEW}")
    a.execute(f"DROP TABLE IF EXISTS {SITE}")
    a.execute(f"REVOKE SELECT ({', '.join(TENANT_COLUMNS)}) ON shared.tenants FROM catalog_owner")
    a.execute(f"REVOKE SELECT ({', '.join(SUB_COLUMNS)}) ON shared.tenant_subscription FROM catalog_owner")
    a.execute("REVOKE SELECT ON shared.v_tenant_entitlement_resolved FROM catalog_owner")
    # The demo tenant's rows: the append-only guards refuse deleting events of a live tenant, so the triggers are off
    # for this superuser session only (replica role), as a test teardown, never a migration.
    a.execute("SET session_replication_role = replica")
    try:
        for table in DEMO_ROW_TABLES:
            a.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))
            if a.fetchone()[0]:
                a.execute(f"DELETE FROM {table} WHERE tenant_id = %s::uuid", (DEMO,))
        a.execute("DELETE FROM shared.tenants WHERE tenant_id = %s::uuid", (DEMO,))
    finally:
        a.execute("SET session_replication_role = DEFAULT")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '379'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_379(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_379(a)
        t323._apply(M379)
        t323._apply(M379)                                     # idempotent re-run
        yield
    finally:
        undo_379(a)
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


def _tenant(cur, name, *, tripplanner=True, kind="agency", tier="starter"):
    tid = uuid.uuid4()
    slug = f"zz379-{tid.hex[:12]}"
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                "is_active, kind) VALUES (%s, %s, %s, %s::plan_tier_enum, 3, 60, true, %s)",
                (str(tid), name, slug, tier, kind))
    if tripplanner:
        cur.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, updated_by) "
                    "VALUES (%s, 'tripplanner', 'tripplanner.hosted', 'active', 'test:379')", (str(tid),))
    return str(tid), slug


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '379'")
    assert cur.fetchone()[0] == 1


def test_columns_owner_barrier_and_consumers(cur):
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
        if not cur.fetchone()[0]:
            continue
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, VIEW))
        assert cur.fetchone()[0] is False, role


def test_catalog_owner_and_tripplanner_privileges(cur):
    cur.execute("""SELECT string_agg(a.attname, ',' ORDER BY a.attname) AS cols FROM pg_attribute a
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                      AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""", (SITE,))
    assert cur.fetchone()[0] == "locale,tenant_id,theme"
    for table in ("shared.tenants", "shared.tenant_subscription", SITE):
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT') AS ok", (table,))
        assert cur.fetchone()[0] is False, table
        for priv in ("SELECT", "INSERT", "UPDATE"):
            cur.execute("SELECT has_any_column_privilege('tripplanner', %s, %s) AS ok", (table, priv))
            assert cur.fetchone()[0] is False, (table, priv)
        for priv in ("INSERT", "UPDATE"):
            cur.execute("SELECT has_any_column_privilege('catalog_owner', %s, %s) AS ok", (table, priv))
            assert cur.fetchone()[0] is False, (table, priv)
    cur.execute("SELECT has_table_privilege('cis_app', %s, 'SELECT, INSERT, UPDATE, DELETE') AS ok", (SITE,))
    assert cur.fetchone()[0] is True
    cur.execute("SELECT has_table_privilege('aa_app_user', %s, 'SELECT') AS ok", (SITE,))
    assert cur.fetchone()[0] is False
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE') AS ok")
    assert cur.fetchone()[0] is False


def test_demo_tenant_is_seeded_once_and_served(cur):
    rows = _rows(cur, "SELECT tenant_id, slug, kind, plan_tier::text AS plan_tier, account_status FROM shared.tenants "
                      "WHERE tenant_id = %s::uuid OR slug = 'aa-tripplanner'", DEMO)
    assert len(rows) == 1
    assert rows[0]["slug"] == "aa-tripplanner" and rows[0]["kind"] == "demo" and rows[0]["plan_tier"] == "internal"
    subs = {r["product"]: r for r in _rows(cur, "SELECT product, plan_key, status FROM shared.tenant_subscription "
                                                "WHERE tenant_id = %s::uuid", DEMO)}
    assert subs["content"]["plan_key"] == "content.internal" and subs["content"]["status"] == "active"
    assert subs["tripplanner"]["plan_key"] == "tripplanner.hosted" and subs["tripplanner"]["status"] == "active"
    cur.execute("SET LOCAL ROLE tripplanner")
    demo = _rows(cur, TP_TENANT_SQL, "aa-tripplanner")
    assert len(demo) == 1
    assert str(demo[0]["tenant_id"]) == DEMO and demo[0]["served"] is True and demo[0]["in_service"] is True
    assert demo[0]["theme"] == {} and demo[0]["locale"] == "en" and demo[0]["tp_launcher"] is True
    assert demo[0]["tp_branding"] is False


def test_rows_follow_the_subscription_and_the_account(cur):
    served, served_slug = _tenant(cur, "Quillmere Voyages synthetic")
    content_only, content_slug = _tenant(cur, "Bramblecourt Travel synthetic", tripplanner=False)
    paused, paused_slug = _tenant(cur, "Saltmarsh Routes synthetic")
    internal, internal_slug = _tenant(cur, "Inner Desk synthetic", kind="internal", tier="internal")
    cur.execute(f"INSERT INTO {SITE} (tenant_id, theme, locale, updated_by) VALUES (%s, %s::jsonb, 'vi', 'test:379')",
                (served, json.dumps({"accent": "#1a2b3c", "ink": "#0f0f0f"})))
    cur.execute("UPDATE shared.tenants SET account_status = 'suspended', suspend_reason = 'billing' "
                "WHERE tenant_id = %s", (paused,))
    cur.execute("SET LOCAL ROLE tripplanner")
    row = _rows(cur, TP_TENANT_SQL, served_slug)[0]
    assert row["served"] is True and row["kind"] == "agency" and row["name"] == "Quillmere Voyages synthetic"
    assert row["theme"] == {"accent": "#1a2b3c", "ink": "#0f0f0f"} and row["locale"] == "vi"
    assert _rows(cur, TP_TENANT_SQL, content_slug) == []
    assert _rows(cur, TP_TENANT_SQL, internal_slug) == []
    off = _rows(cur, TP_TENANT_SQL, paused_slug)[0]
    assert off["in_service"] is False and off["served"] is False
    assert _rows(cur, TP_TENANT_SQL, "zz379-no-such-slug") == []
    assert content_only and internal


def test_two_agencies_each_resolve_to_their_own_row(cur):
    # The isolation the /{slug} path rests on: a slug resolves to exactly its tenant, settings included.
    a, a_slug = _tenant(cur, "Larchfield Expeditions synthetic")
    b, b_slug = _tenant(cur, "Ottervale Journeys synthetic")
    for tid, colour in ((a, "#aa0000"), (b, "#00bb00")):
        cur.execute(f"INSERT INTO {SITE} (tenant_id, theme, updated_by) VALUES (%s, %s::jsonb, 'test:379')",
                    (tid, json.dumps({"accent": colour})))
    cur.execute("SET LOCAL ROLE tripplanner")
    ra, rb = _rows(cur, TP_TENANT_SQL, a_slug), _rows(cur, TP_TENANT_SQL, b_slug)
    assert [str(r["tenant_id"]) for r in ra] == [a] and [str(r["tenant_id"]) for r in rb] == [b]
    assert ra[0]["theme"] == {"accent": "#aa0000"} and rb[0]["theme"] == {"accent": "#00bb00"}
    # A tenant context set by TripPlanner (app.tenant_id, AA-1038 slice 1) does not filter the directory view.
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (a,))
    assert [str(r["tenant_id"]) for r in _rows(cur, TP_TENANT_SQL, b_slug)] == [b]


def test_branding_badge_follows_an_override(cur):
    tid, slug = _tenant(cur, "Fernhollow Trails synthetic")
    cur.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, enabled, reason, granted_by) "
                "VALUES (%s, 'tp.branding', true, 'synthetic test', 'test:379')", (tid,))
    cur.execute("SET LOCAL ROLE tripplanner")
    assert _rows(cur, TP_TENANT_SQL, slug)[0]["tp_branding"] is True


@pytest.mark.parametrize("bad", [
    [], {"accent": "red"}, {"accent": "#ABCDEF"}, {"accent": 1}, {"Accent": "#abcdef"},
    {"accent": "#abcdef; x: y"}, {"k" * 41: "#abcdef"},
])
def test_theme_check_refuses_what_is_not_a_kit_colour(cur, bad):
    tid, _ = _tenant(cur, "Pebblewick Tours synthetic")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(f"INSERT INTO {SITE} (tenant_id, theme, updated_by) VALUES (%s, %s::jsonb, 'test:379')",
                    (tid, json.dumps(bad)))
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_locale_check(cur):
    tid, _ = _tenant(cur, "Moorgate Passage synthetic")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(f"INSERT INTO {SITE} (tenant_id, locale, updated_by) VALUES (%s, 'fr', 'test:379')", (tid,))


MASTER = "00000000-0000-0000-0000-000000000001"


def _catalogue_tour(cur, tenant_id, name):
    """One live tour (raw_tours + published_tours) owned by `tenant_id`, inserted as the superuser test login."""
    tour_id = str(uuid.uuid4())
    cur.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
                "inclusions, exclusions, activities, source_group_id, source_status) VALUES (%s, %s, %s, %s, "
                "'Vietnam', '3 Days', 'Guide', 'Flights', '[\"kayaking\"]'::jsonb, %s, 'active')",
                (tour_id, tenant_id, BATCH_ID, name, str(uuid.uuid4())))
    cur.execute("INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
                "aa_summary, aa_highlights, aa_itineraries, master_status) VALUES (%s, %s, %s, %s, 'Summary', "
                "'[\"Karsts\"]'::jsonb, 'Day 1 — Arrive', 'active')",
                (tour_id, str(uuid.uuid4()), tenant_id, name))
    return tour_id


def test_a_bound_agency_reads_aa_master_catalogue_rows_only(cur):
    # The contract's rule "a bound tenant reads AA master catalogue rows only", by behaviour: a synthetic agency that
    # holds a TripPlanner subscription AND owns a live catalogue tour of its own; TripPlanner (role tripplanner) binds
    # it (app.tenant_id, AA-1038 slice 1) and reads the catalog views. Only the AA master tour comes back.
    agency, _slug = _tenant(cur, "Quillmere Voyages synthetic")
    tag = uuid.uuid4().hex[:8]
    master_tour = _catalogue_tour(cur, MASTER, f"Master tour {tag}")
    agency_tour = _catalogue_tour(cur, agency, f"Agency tour {tag}")
    # The views TripPlanner may read that carry a tour id (v_tour, v_tour_component, v_tour_stop, v_tour_place, ...).
    cur.execute("SELECT c.relname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'catalog' AND c.relkind = 'v' "
                "AND a.attname = 'tour_id' AND NOT a.attisdropped "
                "AND has_table_privilege('tripplanner', c.oid, 'SELECT') ORDER BY 1")
    tour_views = [r[0] for r in cur.fetchall()]
    assert "v_tour" in tour_views, tour_views

    # Control: the GUC really adds the agency's row at the table level (tenant_isolation is TO PUBLIC), so the result
    # below is the views' doing, not a missing row.
    cur.execute("SET LOCAL ROLE catalog_owner")
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (agency,))
    cur.execute("SELECT tour_id::text FROM gold_aa_internal.published_tours WHERE tour_id IN (%s, %s)",
                (master_tour, agency_tour))
    assert {r[0] for r in cur.fetchall()} == {master_tour, agency_tour}
    cur.execute("RESET ROLE")

    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (agency,))
    cur.execute("SELECT tour_id::text FROM catalog.v_tour WHERE tour_id IN (%s, %s)", (master_tour, agency_tour))
    assert {r[0] for r in cur.fetchall()} == {master_tour}
    for view in tour_views:
        cur.execute(f"SELECT count(*) FROM catalog.{view} WHERE tour_id = %s", (agency_tour,))
        assert cur.fetchone()[0] == 0, view
    # The same with no tenant bound (the bare path): still the master tour only.
    cur.execute("SELECT set_config('app.tenant_id', '', true)")
    cur.execute("SELECT tour_id::text FROM catalog.v_tour WHERE tour_id IN (%s, %s)", (master_tour, agency_tour))
    assert {r[0] for r in cur.fetchall()} == {master_tour}
