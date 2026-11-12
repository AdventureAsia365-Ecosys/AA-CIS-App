"""AA-1094 (S278 audit M20, root ADR 0019 d10) — migration 404: catalog.v_tenant_tour_v1, the live AA tours an agency
tenant has publishable text for (approved by it, its own edit, or AI text whose QA passed), for TripPlanner's /{slug}
pages (contract catalog.tenant_tour.v1).

conftest skips 404 (catalog recipe; it needs 359's catalog.v_tour and 379, both skipped too). Each test runs in ONE
transaction on the RDS-like state of test_aa860_migration_245 (module fixture), the way test_aa1040_migration_359 builds
it: 273's replayed column is dropped first, 253 / 256 / 257 / 258 / 273 / 289 / 359 and 379 are applied as cis_owner
(DB Migrate's live-owner switch), the CI stand-in of tenant_tour_versions gets the live version_number and qa_status
columns, 404 is
applied (twice: re-runnable), the checks run, and the transaction is rolled back.

Checks: registered once; the view's exact columns, owner catalog_owner, security_barrier, tripplanner only; the
catalog_owner grants and the policy; as tripplanner: a tenant sees only its own publishable versions of LIVE AA tours,
its own (approved or edited) text before newer AI text, then the latest one per catalogue key (also across an AA source
re-upload); the agency's edit as the portal writes it (pending, tenant_edit) is served; needs_review, QA-escalated,
pending AI, failed and rejected versions, a paused AA tour, a suspended tenant and a tenant without TripPlanner are
absent; a blank title falls back to AA's; a writer-JSON day list itinerary reads as "Day N" blocks; the SELECT
AA-TripPlanner-Web runs answers as tripplanner. Invented names and places only.
"""
import json
import os
import sys
import uuid

import psycopg2.extensions
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only)
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa990_migration_289 as t289  # noqa: E402  (helpers: _apply, _cols, migration_255)
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa990_migration_289 import migration_255  # noqa: E402,F401  (module autouse fixture)

BEFORE = t289.BEFORE + (t289.MIGRATION, "359_catalog_tour_text_updated_at.sql",
                        "379_tenant_tripplanner_catalog_view.sql")
MIGRATION = "404_tenant_tour_catalog_view.sql"
VIEW = "catalog.v_tenant_tour_v1"
TTV = "gold_aa_internal.tenant_tour_versions"
# Copied verbatim into AA-TripPlanner-Web backend/tests/test_aa1038_tenant_site_pg.py (CIS_404_COLUMNS).
COLUMNS = (
    "tenant_id uuid, source_system text, source_id text, tour_id uuid, slug text, title text, subtitle text, "
    "summary text, highlights jsonb, itinerary_text text, seo_title text, seo_meta text, country text, "
    "duration_raw text, duration_days integer, best_time_to_go text, cover_photo_path text, photo_count bigint, "
    "version_id uuid, edit_source text, text_updated_at timestamp with time zone, human_edited boolean, "
    "content_hash text"
)
# The columns AA-TripPlanner-Web backend/browse/tenant_tours.py selects (COLUMNS; never *), keyed by tenant as it is.
TP_CARDS_SQL = f"""SELECT t.source_system, t.source_id, t.tour_id::text AS tour_id, t.slug, t.title, t.subtitle,
                          t.summary, t.highlights, t.itinerary_text, t.seo_title, t.seo_meta, t.country,
                          t.duration_raw, t.duration_days, t.best_time_to_go, t.cover_photo_path, t.photo_count,
                          t.version_id::text AS version_id, t.text_updated_at, t.human_edited, t.content_hash
                     FROM {VIEW} t WHERE t.tenant_id = %s::uuid ORDER BY t.title"""
GRANTED = "created_at,edit_source,id,published_tour_id,rewritten_content,status,tenant_id,version_number"


class _Cur(psycopg2.extensions.cursor):
    """Carries the seeded tenants and tours."""


def _as(c, role, sql, *args):
    c.execute(f"SET LOCAL ROLE {role}")
    c.execute(sql, args)
    cols = [d[0] for d in c.description]
    out = [dict(zip(cols, r)) for r in c.fetchall()]
    c.execute("RESET ROLE")
    return out


def _live_ttv(c):
    """The CI stand-in of tenant_tour_versions (conftest TTV_STANDIN_DDL) has no version_number and no qa_status; Dev's
    table has both (NOT NULL; qa_status default 'pending', 107). Added inside the test transaction, rolled back after.
    RLS on as on Dev (244 enables it)."""
    c.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (TTV,))
    assert c.fetchone()[0], "the RDS-like state must have tenant_tour_versions (x1_244_ci_shape)"
    c.execute(f"ALTER TABLE {TTV} ADD COLUMN IF NOT EXISTS version_number integer NOT NULL DEFAULT 1")
    c.execute(f"ALTER TABLE {TTV} ADD COLUMN IF NOT EXISTS qa_status text NOT NULL DEFAULT 'pending'")
    c.execute(f"ALTER TABLE {TTV} ENABLE ROW LEVEL SECURITY")


def _tenant(c, name, *, tripplanner=True, status="active", kind="agency"):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
              "is_active, kind) VALUES (%s, %s, %s, 'starter'::plan_tier_enum, 3, 60, true, %s)",
              (tid, name, f"zz404-{tid[:12]}", kind))
    if tripplanner:
        c.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, updated_by) "
                  "VALUES (%s, 'tripplanner', 'tripplanner.hosted', 'active', 'test:404')", (tid,))
    if status != "active":
        c.execute("UPDATE shared.tenants SET account_status = %s WHERE tenant_id = %s::uuid", (status, tid))
    return tid


def _published_id(c, tour_id):
    c.execute("SELECT id::text AS id FROM gold_aa_internal.published_tours WHERE tour_id = %s::uuid", (tour_id,))
    return c.fetchone()[0]


def _version(c, tenant, tour_id, *, n, status="ai_generated", text=None, edit_source="ai_generated", age_min=0,
             qa_status="passed"):
    content = text if text is not None else {
        "name": f"{n}: invented tenant title", "subtitle": "Invented subtitle", "summary": "Invented summary",
        "highlights": ["Invented highlight"], "itineraries": "Day 1 — Arrive\nInvented day.",
        "seo_title": "Invented seo title", "seo_meta": "Invented meta", "trip_type": "", "status": "done"}
    vid = str(uuid.uuid4())
    c.execute(f"""INSERT INTO {TTV} (id, tenant_id, published_tour_id, version_number, rewritten_content, status,
                                     edit_source, created_at, qa_status)
                  VALUES (%s, %s, %s::uuid, %s, %s::jsonb, %s, %s, now() - make_interval(mins => %s), %s)""",
              (vid, tenant, _published_id(c, tour_id), n, json.dumps(content), status, edit_source, age_min,
               qa_status))
    return vid


def _source_id(c, tour_id):
    c.execute("SELECT source_key AS k FROM silver_aa_internal.raw_tours WHERE tour_id = %s::uuid", (tour_id,))
    return c.fetchone()[0]


@pytest.fixture
def cur(rds_like_245):  # noqa: F811
    conn = t245._connect()
    conn.autocommit = False
    c = conn.cursor(cursor_factory=_Cur)
    try:
        c.execute("ALTER TABLE gold_aa_internal.published_tours DROP COLUMN IF EXISTS updated_at")   # Dev before 273
        c.execute("SELECT 1 FROM pg_attribute WHERE attrelid = 'geo.tour_place'::regclass AND attname = 'embedding'")
        if not c.fetchone():
            c.execute("ALTER TABLE geo.tour_place ADD COLUMN IF NOT EXISTS embedding float8[]")
        for name in BEFORE:
            t289._apply(c, name)
        _live_ttv(c)
        tag = uuid.uuid4().hex[:8]
        c.tour_a, c.group_a = t228._tour(c, name=f"Invented Ridge Trek {tag}", country="Laos")
        c.tour_b, _ = t228._tour(c, name=f"Invented Lake Loop {tag}", country="Laos")
        c.paused, _ = t228._tour(c, name=f"Invented Paused Walk {tag}", country="Laos", master_status="inactive")
        c.agency = _tenant(c, "Quillmere Voyages (invented)")
        c.other = _tenant(c, "Ottervale Journeys (invented)")
        c.suspended = _tenant(c, "Saltmarsh Routes (invented)", status="suspended")
        c.no_tp = _tenant(c, "Brindle Paths (invented)", tripplanner=False)
        t289._apply(c, MIGRATION)
        t289._apply(c, MIGRATION)                                  # idempotent re-run
        yield c
    finally:
        conn.rollback()
        conn.close()


def _rows(c, tenant):
    return _as(c, "tripplanner", TP_CARDS_SQL, tenant)


def test_registered_owned_barrier_and_tripplanner_only(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '404'")
    assert cur.fetchone()[0] == 1
    cur.execute("""SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
                     AS cols FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                      AND NOT a.attisdropped""", (VIEW,))
    assert cur.fetchone()[0] == COLUMNS
    cur.execute("SELECT pg_get_userbyid(relowner) AS o, reloptions AS r FROM pg_class WHERE oid = %s::regclass",
                (VIEW,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    for role, allowed in (("tripplanner", True), ("booking", False), ("aa_app_user", False), ("cis_app", False)):
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, VIEW))
        assert cur.fetchone()[0] is allowed, role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE') AS ok")
    assert cur.fetchone()[0] is False


def test_catalog_owner_reads_eight_columns_through_one_policy(cur):
    cur.execute("""SELECT string_agg(a.attname, ',' ORDER BY a.attname) AS cols FROM pg_attribute a
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                      AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""", (TTV,))
    assert cur.fetchone()[0] == GRANTED
    cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT') AS ok", (TTV,))
    assert cur.fetchone()[0] is False
    cur.execute("SELECT has_any_column_privilege('tripplanner', %s, 'SELECT') AS ok", (TTV,))
    assert cur.fetchone()[0] is False
    cur.execute("""SELECT cmd, roles::text[] AS roles, qual FROM pg_policies
                    WHERE schemaname = 'gold_aa_internal' AND tablename = 'tenant_tour_versions'
                      AND policyname = 'catalog_owner_tenant_text_read'""")
    cmd, roles, qual = cur.fetchone()
    assert cmd == "SELECT" and roles == ["catalog_owner"]
    assert "approved" in qual and "tenant_edit" in qual and "qa_status" in qual and "passed" in qual
    assert "needs_review" not in qual and "rejected" not in qual and "escalated" not in qual


def _served(c, tenant):
    return _as(c, "tripplanner", f"SELECT source_id, version_id::text AS v, title, summary, highlights, edit_source, "
                                 f"human_edited, country, tour_id::text AS tour_id FROM {VIEW} "
                                 "WHERE tenant_id = %s::uuid ORDER BY source_id", tenant)


def test_approved_text_is_never_replaced_by_newer_ai_text(cur):
    approved = _version(cur, cur.agency, cur.tour_a, n=1, status="approved", age_min=30, qa_status="escalated")
    _version(cur, cur.agency, cur.tour_a, n=2, status="ai_generated", age_min=5)     # newer, QA passed, unreviewed
    _version(cur, cur.agency, cur.tour_a, n=3, status="needs_review", age_min=2)      # newer, below the line
    _version(cur, cur.agency, cur.tour_a, n=4, status="rejected", age_min=1)          # refused: never shown
    _version(cur, cur.agency, cur.paused, n=1, status="approved")                     # AA paused it: not listed
    (row,) = _served(cur, cur.agency)
    assert row["source_id"] == _source_id(cur, cur.tour_a) and row["v"] == approved
    assert row["title"] == "1: invented tenant title" and row["summary"] == "Invented summary"
    assert row["tour_id"] == cur.tour_a and row["country"] == "Laos"
    assert row["highlights"] == ["Invented highlight"] and row["human_edited"] is False
    (tp,) = _rows(cur, cur.agency)                                                     # TripPlanner's own SELECT
    assert tp["version_id"] == approved


def test_the_agency_edit_as_the_portal_writes_it_is_served(cur):
    # The edit route inserts the agency's edit as status pending, edit_source tenant_edit, qa_status left at its default.
    _version(cur, cur.agency, cur.tour_a, n=1, status="ai_generated", age_min=30)
    edit = _version(cur, cur.agency, cur.tour_a, n=2, status="pending", edit_source="tenant_edit", age_min=10,
                    qa_status="pending", text={"name": "Invented agency title", "summary": "Invented agency summary"})
    _version(cur, cur.agency, cur.tour_a, n=3, status="ai_generated", age_min=1)       # a later AI re-rewrite
    (row,) = _served(cur, cur.agency)
    assert row["v"] == edit and row["title"] == "Invented agency title" and row["human_edited"] is True
    assert row["edit_source"] == "tenant_edit"


def test_latest_ai_text_with_qa_passed_is_served_when_the_agency_has_none(cur):
    _version(cur, cur.agency, cur.tour_a, n=1, status="ai_generated", age_min=30)
    newest = _version(cur, cur.agency, cur.tour_a, n=2, status="ai_generated", age_min=5)
    (row,) = _served(cur, cur.agency)
    assert row["v"] == newest and row["title"] == "2: invented tenant title" and row["human_edited"] is False


def test_unpublishable_versions_are_not_listed(cur):
    _version(cur, cur.agency, cur.tour_a, n=1, status="needs_review")                  # below the line / held
    _version(cur, cur.agency, cur.tour_a, n=2, status="ai_generated", qa_status="escalated")   # QA did not clear
    _version(cur, cur.agency, cur.tour_a, n=3, status="ai_generated", qa_status="failed")
    _version(cur, cur.agency, cur.tour_a, n=4, status="ai_generated", qa_status="pending")
    _version(cur, cur.agency, cur.tour_b, n=1, status="pending")                       # AI still writing
    _version(cur, cur.agency, cur.tour_b, n=2, status="failed")
    _version(cur, cur.agency, cur.tour_b, n=3, status="rejected", edit_source="tenant_edit")
    assert _rows(cur, cur.agency) == []
    cur.execute("SET LOCAL ROLE catalog_owner")
    cur.execute(f"SELECT count(*) AS n FROM {TTV} WHERE tenant_id = %s::uuid", (cur.agency,))
    assert cur.fetchone()[0] == 0                                                      # the policy hides them all
    cur.execute("RESET ROLE")


def test_a_writer_json_day_list_itinerary_reads_as_day_blocks(cur):
    days = [{"day": 1, "title": "Arrive", "body": "Invented first day."},
            {"day": 2, "title": None, "body": "", "description": "Invented second day."},
            "a stray string the day reader skips"]
    _version(cur, cur.agency, cur.tour_a, n=1, status="approved", text={"name": "Invented", "itineraries": days})
    _version(cur, cur.agency, cur.tour_b, n=1, status="approved",
             text={"name": "Invented", "itineraries": ["Invented line one", "Invented line two"]})
    got = {r["source_id"]: r["itinerary_text"] for r in _rows(cur, cur.agency)}
    assert got[_source_id(cur, cur.tour_a)] == "Day 1 — Arrive\nInvented first day.\n\nDay 2 — \nInvented second day."
    assert got[_source_id(cur, cur.tour_b)] == "Invented line one\nInvented line two"


def test_suspended_tenant_and_tenant_without_tripplanner_have_no_rows(cur):
    _version(cur, cur.suspended, cur.tour_a, n=1, status="approved")
    _version(cur, cur.no_tp, cur.tour_a, n=1, status="approved")
    assert _rows(cur, cur.suspended) == []
    assert _rows(cur, cur.no_tp) == []


def test_blank_title_falls_back_to_aa_and_blank_fields_are_null(cur):
    _version(cur, cur.agency, cur.tour_a, n=1, status="approved",
             text={"name": "  ", "subtitle": "", "summary": "Invented only summary", "highlights": "not a list"})
    (row,) = _as(cur, "tripplanner", f"SELECT title, subtitle, summary, highlights, itinerary_text FROM {VIEW} "
                                     "WHERE tenant_id = %s::uuid", cur.agency)
    cur.execute("SELECT title FROM catalog.v_tour WHERE tour_id = %s::uuid", (cur.tour_a,))
    assert row["title"] == cur.fetchone()[0]
    assert row["subtitle"] is None and row["itinerary_text"] is None
    assert row["summary"] == "Invented only summary" and row["highlights"] == []


def test_text_follows_the_key_across_a_source_reupload(cur):
    _version(cur, cur.agency, cur.tour_a, n=1, status="approved")
    # AA re-uploads the source: a new raw version of the same group becomes the live one, the old one is superseded.
    cur.execute("UPDATE silver_aa_internal.raw_tours SET source_status = 'superseded' WHERE tour_id = %s::uuid",
                (cur.tour_a,))
    new_tour, _ = t228._tour(cur, name="Invented Ridge Trek v2", country="Laos", group=cur.group_a)
    (row,) = _rows(cur, cur.agency)
    assert row["tour_id"] == new_tour and row["title"] == "1: invented tenant title"


def test_view_rows_are_not_widened_by_the_tenant_guc(cur):
    _version(cur, cur.agency, cur.tour_a, n=1, status="approved")
    _version(cur, cur.other, cur.tour_a, n=1, status="approved")
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (cur.other,))
    assert {r["source_id"] for r in _rows(cur, cur.agency)} == {_source_id(cur, cur.tour_a)}
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute(f"SELECT count(DISTINCT tenant_id) AS n FROM {VIEW} WHERE tenant_id = ANY(%s::uuid[])",
                ([cur.agency, cur.other],))
    assert cur.fetchone()[0] == 2
    cur.execute("RESET ROLE")
