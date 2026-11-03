"""AA-1040 (EU AI Act Art. 50(2)) — migration 358: catalog.v_supply_content appends the machine-readable AI mark of
the published supply text (ai_generated, ai_generated_at, ai_human_edited) for Booking.

conftest skips 358 (catalog recipe). Each test runs in ONE transaction on the RDS-like state of
test_aa860_migration_245: 278 and 358 are applied as cis_owner (DB Migrate's live-owner switch), the checks run as
booking, and the transaction is rolled back.

Checks: registered once and re-runnable; 278's columns unchanged and first, the three mark columns appended; owner,
security_barrier and the exact booking grant kept; the mark of an llm draft, of a staff edit of a draft (also an edit
of an edit), of a text written from scratch, and nothing on a row that is not published; Booking's reader SQL (both
the 278 read and the AA-1040 mark read) runs as booking.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa835_migration_278 as t278  # noqa: E402  (helpers: _apply, _entity, _version, _publish, BOOKING_SQL)
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)

MIGRATION = "358_supply_content_ai_mark.sql"
COLUMNS_278 = ["source_system", "source_id", "entity_type", "content_status", "content_version", "description",
               "highlights", "good_to_know", "photo_urls", "edit_url", "published_at"]
MARK = ["ai_generated", "ai_generated_at", "ai_human_edited"]

# AA-Booking backend/app/supply/content.py MARK_SQL (AA-1040), as Booking runs it.
BOOKING_MARK_SQL = """
    SELECT c.source_id::text AS source_id, c.ai_generated, c.ai_generated_at, c.ai_human_edited
    FROM catalog.v_supply_content c
    WHERE c.source_system::text = 'booking' AND c.source_id::text = ANY(%s::text[])
"""


@pytest.fixture
def cur(rds_like_245):  # noqa: F811
    conn = t245._connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        t278._apply(c)
        t278._apply(c, MIGRATION)
        yield c
    finally:
        conn.rollback()
        conn.close()


def _human(c, entity_id, n, *, based_on=None, status="draft"):
    reviewed = status in ("approved", "rejected")
    c.execute("""INSERT INTO shared.supply_content_version
                     (entity_id, content_version, supply_version, origin, description, highlights, good_to_know,
                      photos, status, based_on_version_id, created_by, reviewed_by, reviewed_at)
                 VALUES (%s, %s, 1, 'human', %s, ARRAY['Quiet garden'], ARRAY[]::text[], '[]'::jsonb, %s, %s,
                         'staff:test', %s, CASE WHEN %s THEN now() END)
                 RETURNING id""",
              (entity_id, n, f"Edited version {n}.", status, based_on, "staff:test" if reviewed else None,
               reviewed))
    return c.fetchone()[0]


def _created(c, version_id):
    c.execute("SELECT created_at FROM shared.supply_content_version WHERE id = %s", (version_id,))
    return c.fetchone()[0]


def _mark(c, source_ids):
    rows = t278._as(c, "booking", BOOKING_MARK_SQL, (list(source_ids),))
    return {r[0]: r[1:] for r in rows}


def test_registered_rerunnable_and_columns_appended(cur):
    t278._apply(cur, MIGRATION)                               # idempotent re-run
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '358'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT attname FROM pg_attribute WHERE attrelid = 'catalog.v_supply_content'::regclass "
                "AND attnum > 0 AND NOT attisdropped ORDER BY attnum")
    assert [r[0] for r in cur.fetchall()] == COLUMNS_278 + MARK
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class "
                "WHERE oid = 'catalog.v_supply_content'::regclass")
    owner, options = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (options or [])
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False
    for role, allowed in (("booking", True), ("tripplanner", False), ("aa_app_user", False), ("cis_app", False)):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_supply_content', 'SELECT')", (role,))
        assert cur.fetchone()[0] is allowed, role


def test_mark_of_each_published_origin(cur):
    # an llm draft published as written
    llm_id, llm_src = t278._entity(cur)
    draft = t278._version(cur, llm_id, 1, status="approved")
    t278._publish(cur, llm_id, draft)

    # a staff edit of a draft, and an edit of that edit: still AI-assisted, edited, generated when the draft was
    edit_id, edit_src = t278._entity(cur)
    d1 = t278._version(cur, edit_id, 1, status="superseded")
    e1 = _human(cur, edit_id, 2, based_on=d1, status="superseded")
    e2 = _human(cur, edit_id, 3, based_on=e1, status="approved")
    t278._publish(cur, edit_id, e2)

    # a text a person wrote from scratch (no base), even though an llm draft of the entity exists
    own_id, own_src = t278._entity(cur)
    t278._version(cur, own_id, 1, status="rejected")
    own = _human(cur, own_id, 2, status="approved")
    t278._publish(cur, own_id, own)

    # not published: a draft only, and a stale entity (text hidden, so no mark either)
    draft_id, draft_src = t278._entity(cur)
    t278._version(cur, draft_id, 1)
    stale_id, stale_src = t278._entity(cur)
    sv = t278._version(cur, stale_id, 1, status="approved")
    t278._publish(cur, stale_id, sv)
    cur.execute("UPDATE shared.supply_entity SET content_status = 'stale' WHERE id = %s", (stale_id,))

    marks = _mark(cur, [llm_src, edit_src, own_src, draft_src, stale_src])
    assert marks[llm_src] == (True, _created(cur, draft), False)
    assert marks[edit_src] == (True, _created(cur, d1), True)
    assert marks[own_src] == (False, None, None)
    assert marks[draft_src] == (None, None, None)
    assert marks[stale_src] == (None, None, None)


def test_booking_reader_sql_still_runs(cur):
    pub_id, pub_src = t278._entity(cur)
    v1 = t278._version(cur, pub_id, 1, status="approved")
    t278._publish(cur, pub_id, v1)
    rows = t278._as(cur, "booking", t278.BOOKING_SQL, ([pub_src],))
    assert len(rows) == 1 and rows[0][4] == "Version 1 text."


def test_closing_check_refuses_a_marked_row_without_text(cur):
    """Mutation check: the after-guard of 358 catches a definition that marks an unpublished row."""
    body = t278._body(MIGRATION).replace(
        "CASE WHEN p.id IS NOT NULL THEN ai.llm_created_at IS NOT NULL END AS ai_generated",
        "true AS ai_generated")
    entity_id, _ = t278._entity(cur)
    t278._version(cur, entity_id, 1)
    cur.execute("SAVEPOINT aa1040_358")
    try:
        cur.execute("SET LOCAL ROLE cis_owner")
        with pytest.raises(psycopg2.Error, match="marks a row that serves no text"):
            cur.execute(body)
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT aa1040_358")


def test_view_explain_reads_one_entity_by_key(cur):
    """Lesson S238: Booking's single-entity read stays a keyed lookup (no scan of every entity's chain)."""
    t278._entity(cur)
    cur.execute("SAVEPOINT aa1040_358_x")
    try:
        cur.execute("SET LOCAL ROLE booking")
        cur.execute("EXPLAIN " + BOOKING_MARK_SQL, ([str(uuid.uuid4())],))
        plan = "\n".join(r[0] for r in cur.fetchall())
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT aa1040_358_x")
    assert "supply_entity" in plan
