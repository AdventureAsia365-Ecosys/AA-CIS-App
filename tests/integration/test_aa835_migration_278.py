"""AA-835 — migration 278: the CIS supply content tables (shared.supply_*), the supply_content_writer stage, and the
Booking read-back view catalog.v_supply_content.

conftest replays only 278's part A (the catalog recipe of part B needs 245's owners). Each test runs in ONE
transaction on the RDS-like state of test_aa860_migration_245 (module fixture): 278 is applied as cis_owner (DB
Migrate's live-owner switch), the checks run, and the transaction is rolled back.

Checks: registered once and re-runnable; the view catalog_owner's, security_barrier, SELECT to booking only; exact
table privileges (cis_app DML without DELETE, an append-only inbox, catalog_owner SELECT for the view, nothing for
aa_app_user or booking); read as booking, the view serves text and photos only for a published, active entity with
an approved version, and only photos with usage_rights that were copied; a draft / stale / unpublished row has NULL
text; the version guard keeps content immutable and the status moves legal; Booking's own reader SQL runs as booking.
"""
import json
import os
import re
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
MIGRATION = "278_supply_content.sql"
TABLES = ("supply_entity", "supply_content_version", "supply_event_inbox", "supply_feed_cursor", "supply_media")

# AA-Booking backend/app/supply/content.py CONTENT_SQL (S4), as Booking runs it.
BOOKING_SQL = """
    SELECT c.entity_type::text AS entity_type, c.source_id::text AS source_id, c.content_status::text AS content_status,
           c.content_version, c.description::text AS description, to_jsonb(c.highlights)::text AS highlights,
           to_jsonb(c.photo_urls)::text AS photo_urls, c.edit_url::text AS edit_url
    FROM catalog.v_supply_content c
    WHERE c.source_system::text = 'booking'
      AND c.source_id::text = ANY(%s::text[]) AND c.content_status::text = 'published'
"""


def _body(name):
    sql = open(os.path.join(MIG, name), encoding="utf-8").read()
    assert sql.count("\nBEGIN;\n") == 1 and sql.rstrip().endswith("COMMIT;")
    return re.sub(r"\nCOMMIT;\s*$", "\n", sql.replace("\nBEGIN;\n", "\n", 1))


def _apply(c, name=MIGRATION):
    c.execute("SET LOCAL ROLE cis_owner")          # DB Migrate's live-owner switch (XD8)
    c.execute(_body(name))
    c.execute("RESET ROLE")


def _as(c, role, sql, args=()):
    c.execute("SAVEPOINT aa835_278")
    try:
        c.execute(f"SET LOCAL ROLE {role}")
        c.execute(sql, args)
        out = c.fetchall() if c.description else c.rowcount
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa835_278")
    return out


def _raises(c, sql, args=(), exc=psycopg2.Error):
    c.execute("SAVEPOINT aa835_278_err")
    try:
        with pytest.raises(exc):
            c.execute(sql, args)
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa835_278_err")


def _entity(c, *, entity_type="hotel", status="draft", active=True, version=1):
    source_id = str(uuid.uuid4())
    c.execute("""INSERT INTO shared.supply_entity (source_id, entity_type, title, facts, applied_supply_version,
                                                   active, content_status)
                 VALUES (%s, %s, 'Riverside Hotel', %s::jsonb, %s, %s, %s) RETURNING id""",
              (source_id, entity_type, json.dumps({"name": "Riverside Hotel", "status": "active"}), version, active,
               status))
    return c.fetchone()[0], source_id


def _version(c, entity_id, n, *, status="draft", photos=(), supply_version=1):
    reviewed = status in ("approved", "rejected")
    c.execute("""INSERT INTO shared.supply_content_version
                     (entity_id, content_version, supply_version, origin, description, highlights, good_to_know,
                      photos, status, created_by, reviewed_by, reviewed_at)
                 VALUES (%s, %s, %s, 'llm', %s, ARRAY['River views'], ARRAY['Check-in from 14:00'], %s::jsonb, %s,
                         'system:test', %s, CASE WHEN %s THEN now() END)
                 RETURNING id""",
              (entity_id, n, supply_version, f"Version {n} text.", json.dumps(list(photos)), status,
               "staff:test" if reviewed else None, reviewed))
    return c.fetchone()[0]


def _media(c, entity_id, *, rights, copied=True, active=True):
    media_id = str(uuid.uuid4())
    sha = uuid.uuid4().hex * 2
    c.execute("""INSERT INTO shared.supply_media (booking_media_id, entity_id, usage_rights, active, sha256,
                                                  content_type, cis_key_1600, cis_key_600, url_1600, url_600,
                                                  copied_sha256, copied_at)
                 VALUES (%s, %s, %s, %s, %s, 'image/jpeg', %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)""",
              (media_id, entity_id, rights, active, sha,
               f"photos/supply/hotel/{media_id}-1600.webp" if copied else None,
               f"photos/supply/hotel/{media_id}-600.webp" if copied else None,
               f"https://img.example.test/supply/hotel/{media_id}-1600.webp" if copied else None,
               f"https://img.example.test/supply/hotel/{media_id}-600.webp" if copied else None,
               sha if copied else None, copied))
    return media_id


def _publish(c, entity_id, version_id):
    c.execute("UPDATE shared.supply_entity SET content_status = 'published', published_version_id = %s "
              "WHERE id = %s", (version_id, entity_id))


@pytest.fixture
def cur(rds_like_245):  # noqa: F811
    conn = t245._connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        _apply(c)
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_registered_rerunnable_owned_and_granted(cur):
    _apply(cur)                                             # idempotent re-run
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '278'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class "
                "WHERE oid = 'catalog.v_supply_content'::regclass")
    owner, options = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (options or [])
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False
    for role, allowed in (("booking", True), ("tripplanner", False), ("aa_app_user", False), ("cis_app", False)):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_supply_content', 'SELECT')", (role,))
        assert cur.fetchone()[0] is allowed, role
    # 278 seeds the stage ('sonnet', ON CONFLICT DO NOTHING); the replay already ran 282 (S251), which moved it to the
    # catalog key sonnet-5-5 with the legacy 'sonnet' last, and a re-run of 278 leaves that row alone.
    cur.execute("SELECT stage, role, provider, model_id, account_route, is_active, shadow_model_id, fallback_model_ids "
                "FROM shared.llm_role_config WHERE stage = 'supply_content_writer'")
    assert cur.fetchone() == ("supply_content_writer", "writer", "claude", "sonnet-5-5", "acc3", True, None,
                              ["sonnet"])
    cur.execute("SELECT per_run_usd, per_day_usd, hard_stop FROM shared.spend_budget "
                "WHERE provider = 'bedrock' AND scope = 'job:supply_content_draft'")
    assert [float(x) if not isinstance(x, bool) else x for x in cur.fetchone()] == [0.10, 1.00, True]


def test_table_privileges_exact(cur):
    expected = {
        "supply_entity": {"SELECT", "INSERT", "UPDATE"},
        "supply_content_version": {"SELECT", "INSERT", "UPDATE"},
        "supply_event_inbox": {"SELECT", "INSERT"},
        "supply_feed_cursor": {"SELECT", "INSERT", "UPDATE"},
        "supply_media": {"SELECT", "INSERT", "UPDATE"},
    }
    for table in TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            cur.execute("SELECT has_table_privilege('cis_app', %s, %s)", (f"shared.{table}", privilege))
            assert cur.fetchone()[0] is (privilege in expected[table]), (table, privilege)
        for role in ("aa_app_user", "booking", "tripplanner"):
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"shared.{table}"))
            assert cur.fetchone()[0] is False, (role, table)
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT')", (f"shared.{table}",))
        assert cur.fetchone()[0] is (table in ("supply_entity", "supply_content_version", "supply_media")), table
    # booking reads the view, never the tables behind it
    _raises(cur, "SET LOCAL ROLE booking; SELECT 1 FROM shared.supply_entity",
            exc=psycopg2.errors.InsufficientPrivilege)


def test_view_serves_only_published_text_and_rights_cleared_photos(cur):
    draft_id, draft_src = _entity(cur)
    _version(cur, draft_id, 1)
    _version(cur, draft_id, 2)

    pub_id, pub_src = _entity(cur)
    ok = _media(cur, pub_id, rights=True)
    no_rights = _media(cur, pub_id, rights=False)
    not_copied = _media(cur, pub_id, rights=True, copied=False)
    removed = _media(cur, pub_id, rights=True, active=False)
    photos = [{"media_id": m, "caption": None, "alt": "Room"} for m in (not_copied, no_rights, ok, removed)]
    v1 = _version(cur, pub_id, 1, status="approved", photos=photos)
    _publish(cur, pub_id, v1)
    _version(cur, pub_id, 2)                                 # a newer draft does not change what is served

    stale_id, stale_src = _entity(cur)
    sv = _version(cur, stale_id, 1, status="approved")
    _publish(cur, stale_id, sv)
    cur.execute("UPDATE shared.supply_entity SET content_status = 'stale' WHERE id = %s", (stale_id,))

    off_id, off_src = _entity(cur)
    ov = _version(cur, off_id, 1, status="approved")
    _publish(cur, off_id, ov)
    cur.execute("UPDATE shared.supply_entity SET active = false, content_status = 'unpublished' WHERE id = %s",
                (off_id,))

    rows = _as(cur, "booking", "SELECT source_id, content_status, content_version, description, highlights, "
                               "good_to_know, photo_urls, edit_url FROM catalog.v_supply_content "
                               "WHERE source_id = ANY(%s)", ([draft_src, pub_src, stale_src, off_src],))
    by = {r[0]: r[1:] for r in rows}
    assert by[draft_src] == ("draft", 2, None, None, None, None, f"/admin/supply-content/{draft_id}")
    status, number, text, highlights, gtk, urls, edit = by[pub_src]
    assert (status, number, text, highlights, gtk) == ("published", 1, "Version 1 text.", ["River views"],
                                                       ["Check-in from 14:00"])
    assert urls == [f"https://img.example.test/supply/hotel/{ok}-1600.webp"]
    assert edit == f"/admin/supply-content/{pub_id}"
    assert by[stale_src][:3] == ("stale", 1, None) and by[stale_src][5] is None
    assert by[off_src][:3] == ("unpublished", 1, None) and by[off_src][5] is None

    booking = _as(cur, "booking", BOOKING_SQL, ([draft_src, pub_src, stale_src, off_src],))
    assert [(r[1], r[2], r[4]) for r in booking] == [(pub_src, "published", "Version 1 text.")]


def test_published_needs_a_version_of_the_same_entity(cur):
    a, _ = _entity(cur)
    b, _ = _entity(cur)
    vb = _version(cur, b, 1, status="approved")
    _raises(cur, "UPDATE shared.supply_entity SET content_status = 'published' WHERE id = %s", (a,),
            exc=psycopg2.errors.CheckViolation)
    _raises(cur, "UPDATE shared.supply_entity SET content_status = 'published', published_version_id = %s "
                 "WHERE id = %s", (vb, a), exc=psycopg2.errors.ForeignKeyViolation)
    _raises(cur, "UPDATE shared.supply_entity SET active = false WHERE id = %s", (a,),
            exc=psycopg2.errors.CheckViolation)


def test_version_guard(cur):
    e, _ = _entity(cur)
    v1 = _version(cur, e, 1)
    for sql in ("UPDATE shared.supply_content_version SET description = 'changed' WHERE id = %s",
                "UPDATE shared.supply_content_version SET photos = '[{\"media_id\": \"x\"}]' WHERE id = %s",
                "DELETE FROM shared.supply_content_version WHERE id = %s"):
        _raises(cur, sql, (v1,), exc=psycopg2.errors.RestrictViolation)
    # draft -> approved needs a reviewer
    _raises(cur, "UPDATE shared.supply_content_version SET status = 'approved' WHERE id = %s", (v1,),
            exc=psycopg2.errors.CheckViolation)
    cur.execute("UPDATE shared.supply_content_version SET status = 'approved', reviewed_by = 'staff:t', "
                "reviewed_at = now() WHERE id = %s", (v1,))
    for target in ("draft", "rejected"):
        _raises(cur, "UPDATE shared.supply_content_version SET status = %s WHERE id = %s", (target, v1),
                exc=psycopg2.errors.RestrictViolation)
    _raises(cur, "UPDATE shared.supply_content_version SET reviewed_by = 'staff:other' WHERE id = %s", (v1,),
            exc=psycopg2.errors.RestrictViolation)
    v2 = _version(cur, e, 2)
    _raises(cur, "UPDATE shared.supply_content_version SET status = 'approved', reviewed_by = 'staff:t', "
                 "reviewed_at = now() WHERE id = %s", (v2,), exc=psycopg2.errors.UniqueViolation)
    cur.execute("UPDATE shared.supply_content_version SET status = 'superseded' WHERE id = %s", (v1,))
    _raises(cur, "UPDATE shared.supply_content_version SET status = 'approved' WHERE id = %s", (v1,),
            exc=psycopg2.errors.RestrictViolation)


def test_inbox_dedupes_and_is_append_only_for_cis_app(cur):
    key = str(uuid.uuid4())
    insert = ("INSERT INTO shared.supply_event_inbox (contract, idempotency_key, source_id, event_type, "
              "supply_version, facts_supply_version, feed_seq, outcome) "
              "VALUES ('supply.hotel.v1', %s, 'x', 'supply.upsert', 1, 1, 7, 'applied') "
              "ON CONFLICT (source, contract, idempotency_key) DO NOTHING")
    cur.execute(insert, (key,))
    cur.execute(insert, (key,))
    cur.execute("SELECT count(*) FROM shared.supply_event_inbox WHERE idempotency_key = %s", (key,))
    assert cur.fetchone()[0] == 1
    _raises(cur, "SET LOCAL ROLE cis_app; DELETE FROM shared.supply_event_inbox",
            exc=psycopg2.errors.InsufficientPrivilege)
    _raises(cur, "SET LOCAL ROLE cis_app; UPDATE shared.supply_event_inbox SET outcome = 'missing'",
            exc=psycopg2.errors.InsufficientPrivilege)
