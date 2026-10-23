"""AA-986 L3/L4 — migration 274: shared.notifications under RLS (the tenant reads and marks read its own tenant-facing
rows), and the catalog contract views catalog.v_tour_retired (tombstone) + catalog.v_tour_lifecycle_outbox (events),
contract catalog.tour.v1.3.

conftest replays only 274's part A (it skips 253-258, which part B needs). Each test runs in ONE transaction on the
RDS-like state of test_aa860_migration_245 (module fixture): 253 / 256 / 257 / 258 / 273 and then 274 are applied as
cis_owner (DB Migrate's live-owner switch), the checks run, and the transaction is rolled back.

Checks: registered once and re-runnable; both views catalog_owner's, security_barrier, exact grants (booking,
tripplanner, aa_app_user SELECT); read as booking / tripplanner, the tombstone has one row per not-served key with the
right state / reason, a `restored` row for a key served again, nothing for a served key or a key never served; the
outbox shows every event without the internal columns; the tenant role reads only its own tenant-facing
notifications, may flip is_read and nothing else; cis_app keeps every row.
"""
import os
import re
import sys
import uuid

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only)
import test_aa860_migration_245 as t245  # noqa: E402
from conftest import x1_244_ci_unshape  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
BEFORE = ("253_catalog_l4_read_model.sql", "256_catalog_line_places.sql", "257_geo_place_dedupe.sql",
          "258_catalog_live_source_key.sql", "273_published_tours_updated_at.sql")
MIGRATION = "274_tour_retired_outbox_tenant_notifications.sql"
MASTER = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(scope="module", autouse=True)
def migration_255():
    """test_aa712_migration_226 drops schema geo and re-applies 226 only: re-apply 255 (idempotent)."""
    c = t245._connect()
    c.autocommit = True
    with open(os.path.join(MIG, "255_geo_place_shape.sql")) as f:
        c.cursor().execute(f.read())
    c.close()


def _body(name):
    sql = open(os.path.join(MIG, name), encoding="utf-8").read()
    assert sql.count("\nBEGIN;\n") == 1 and sql.rstrip().endswith("COMMIT;")
    return re.sub(r"\nCOMMIT;\s*$", "\n", sql.replace("\nBEGIN;\n", "\n", 1))


def _apply(c, name):
    c.execute("SET LOCAL ROLE cis_owner")          # DB Migrate's live-owner switch (XD8)
    c.execute(_body(name))
    c.execute("RESET ROLE")


def _as(c, role, sql, args=(), guc=None):
    c.execute("SAVEPOINT aa986_274")
    try:
        c.execute(f"SET LOCAL ROLE {role}")
        if guc is not None:
            c.execute("SELECT set_config('app.tenant_id', %s, true)", (guc,))
        c.execute(sql, args)
        out = c.fetchall() if c.description else c.rowcount
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa986_274")
    return out


def _event(c, source_id, tour_id, event, reason, *, replaced_by=None, actor="admin"):
    c.execute("""INSERT INTO gold_aa_internal.tour_lifecycle_event
                     (source_id, tour_id, event, reason, replaced_by_tour_id, action, actor, details, idempotency_key)
                 VALUES (%s, %s, %s, %s, %s, 'test', %s, '{"secret": "internal"}'::jsonb, %s)""",
              (source_id, tour_id, event, reason, replaced_by, actor, f"aa986-274:{uuid.uuid4()}"))


class _Cur(psycopg2.extensions.cursor):
    """Carries the seeded keys."""


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
        # rds_like_245 keeps the X1 S16 CI shape (RLS off on the post-244 and CI-only legacy tables, the 244 window);
        # 274's AA-859 invariant guard needs the replay state back, as on Dev after 262-266 (rolled back below)
        x1_244_ci_unshape(c)
        for name in BEFORE:
            _apply(c, name)
        tag = uuid.uuid4().hex[:8]
        seeded = {}
        # served, no event
        seeded["served"] = t228._tour(c, name=f"Served {tag}", country="Laos")
        # retired: source trashed at S0/A0 (Master switched off by the service, reason source_deleted)
        tid, grp = t228._tour(c, name=f"Retired {tag}", country="Laos")
        c.execute("UPDATE silver_aa_internal.raw_tours SET source_status = 'trashed', deleted_at = now() "
                  "WHERE tour_id = %s", (tid,))
        c.execute("UPDATE gold_aa_internal.published_tours SET master_status = 'inactive', "
                  "master_status_reason = 'source_deleted' WHERE tour_id = %s", (tid,))
        _event(c, grp, tid, "retired", "source_deleted")
        seeded["retired"] = (tid, grp)
        # paused: Master inactive by staff, source live
        tid, grp = t228._tour(c, name=f"Paused {tag}", country="Laos", master_status="inactive")
        _event(c, grp, tid, "paused", "master_inactive")
        seeded["paused"] = (tid, grp)
        # restored: retired then restored, served again
        tid, grp = t228._tour(c, name=f"Restored {tag}", country="Laos")
        _event(c, grp, tid, "retired", "master_trashed")
        _event(c, grp, tid, "restored", "restore")
        seeded["restored"] = (tid, grp)
        # retired before L2 (no event): Master trashed
        seeded["old_retired"] = t228._tour(c, name=f"Old trashed {tag}", country="Laos", master_status="trashed")
        # never served: a source without a Master
        never = str(uuid.uuid4())
        c.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                                                               source_group_id, source_status)
                     VALUES (%s, %s, %s, %s, 'Laos', %s, 'active')""",
                  (never, MASTER, t228.BATCH_ID, f"Never {tag}", never))
        seeded["never"] = (never, never)
        c.seeded = seeded
        _apply(c, MIGRATION)
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_registered_rerunnable_owned_and_granted(cur):
    _apply(cur, MIGRATION)                                  # idempotent re-run
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '274'")
    assert cur.fetchone()[0] == 1
    cur.execute("""SELECT relname, pg_get_userbyid(relowner), reloptions FROM pg_class
                    WHERE oid IN ('catalog.v_tour_retired'::regclass, 'catalog.v_tour_lifecycle_outbox'::regclass)
                    ORDER BY relname""")
    rows = cur.fetchall()
    assert [(r[0], r[1]) for r in rows] == [("v_tour_lifecycle_outbox", "catalog_owner"),
                                            ("v_tour_retired", "catalog_owner")]
    assert all("security_barrier=true" in (r[2] or []) for r in rows)
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False
    for view in ("catalog.v_tour_retired", "catalog.v_tour_lifecycle_outbox"):
        for role, allowed in (("booking", True), ("tripplanner", True), ("aa_app_user", True), ("cis_app", False)):
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, view))
            assert cur.fetchone()[0] is allowed, (view, role)


def test_tombstone_rows_per_key(cur):
    s = cur.seeded
    rows = _as(cur, "booking", "SELECT source_id, tour_id::text, state, reason FROM catalog.v_tour_retired "
                               "WHERE source_id = ANY(%s)",
               ([g for _, g in s.values()],))
    by_key = {r[0]: r[1:] for r in rows}
    assert by_key[s["retired"][1]] == (s["retired"][0], "retired", "source_deleted")
    assert by_key[s["paused"][1]] == (s["paused"][0], "paused", "master_inactive")
    assert by_key[s["restored"][1]] == (s["restored"][0], "restored", "restore")
    assert by_key[s["old_retired"][1]] == (s["old_retired"][0], "retired", "master_trashed")   # no event needed
    assert s["served"][1] not in by_key                     # served, never changed: no row
    assert s["never"][1] not in by_key                      # never served: no row (404, not 410)


def test_tombstone_never_disagrees_with_the_catalog(cur):
    """A key live in catalog.v_tour is never retired / paused in the tombstone (only `restored`)."""
    states = _as(cur, "tripplanner", "SELECT r.source_id, r.state FROM catalog.v_tour_retired r "
                                     "JOIN catalog.v_tour t ON t.source_id = r.source_id")
    assert {r[1] for r in states} <= {"restored"}
    assert cur.seeded["restored"][1] in {r[0] for r in states}


def test_outbox_shows_events_without_internal_columns(cur):
    s = cur.seeded
    cols = _as(cur, "booking", "SELECT * FROM catalog.v_tour_lifecycle_outbox LIMIT 0")
    cur.execute("SELECT a.attname FROM pg_attribute a WHERE a.attrelid = 'catalog.v_tour_lifecycle_outbox'::regclass "
                "AND a.attnum > 0 ORDER BY a.attnum")
    assert [r[0] for r in cur.fetchall()] == ["event_id", "source_system", "source_id", "tour_id", "event", "reason",
                                              "replaced_by_tour_id", "occurred_at", "idempotency_key"]
    assert cols == []
    rows = _as(cur, "tripplanner", "SELECT event, reason FROM catalog.v_tour_lifecycle_outbox WHERE source_id = %s "
                                   "ORDER BY event_id", (s["restored"][1],))
    assert rows == [("retired", "master_trashed"), ("restored", "restore")]


# ── part A: shared.notifications ───────────────────────────────────────────────────────────────────────────────────

def _notif(c, tenant, event, roles):
    c.execute("""INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload,
                                                   target_roles)
                 VALUES (%s, 'system', %s, 'tour', 'x', '{}'::jsonb, %s) RETURNING id""", (tenant, event, roles))
    return c.fetchone()[0]


def _tenant(c):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
              (tid, f"AA-986 {tid[:8]}", f"aa986-{tid[:8]}"))
    return tid


def test_notifications_policies_and_grants(cur):
    cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'shared.notifications'::regclass")
    assert cur.fetchone() == (True, False)
    cur.execute("SELECT polname, polpermissive FROM pg_policy WHERE polrelid = 'shared.notifications'::regclass "
                "ORDER BY polname")
    assert cur.fetchall() == [("notifications_cis_app_all", True), ("notifications_tenant_audience", False),
                              ("tenant_isolation", True)]
    for priv, allowed in (("SELECT", True), ("INSERT", False), ("DELETE", False), ("UPDATE", False)):
        cur.execute("SELECT has_table_privilege('aa_app_user', 'shared.notifications', %s)", (priv,))
        assert cur.fetchone()[0] is allowed, priv
    cur.execute("SELECT has_column_privilege('aa_app_user', 'shared.notifications', 'is_read', 'UPDATE'), "
                "has_column_privilege('aa_app_user', 'shared.notifications', 'payload', 'UPDATE')")
    assert cur.fetchone() == (True, False)


def test_tenant_reads_only_its_own_tenant_facing_rows(cur):
    a, b = _tenant(cur), _tenant(cur)
    a_tenant = _notif(cur, a, "tour.discontinued", ["tenant"])
    a_admin = _notif(cur, a, "tenant.cta.review_claim", ["admin"])     # carries a's id, staff only
    b_tenant = _notif(cur, b, "tour.discontinued", ["tenant"])
    staff = _notif(cur, MASTER, "tour.source.trashed", ["admin", "content"])
    seen = lambda guc: {r[0] for r in _as(cur, "aa_app_user", "SELECT id FROM shared.notifications", guc=guc)}  # noqa
    assert seen(a) == {a_tenant}
    assert seen(b) == {b_tenant}
    assert seen(str(uuid.uuid4())) == set()
    assert seen("") == set()                                 # empty GUC: no rows, no error
    # cis_app (the staff bell, the service) still reads everything
    all_ids = {r[0] for r in _as(cur, "cis_app", "SELECT id FROM shared.notifications WHERE id = ANY(%s)",
                                 ([a_tenant, a_admin, b_tenant, staff],))}
    assert all_ids == {a_tenant, a_admin, b_tenant, staff}


def test_tenant_marks_read_but_writes_nothing_else(cur):
    a, b = _tenant(cur), _tenant(cur)
    mine = _notif(cur, a, "tour.discontinued", ["tenant"])
    _notif(cur, a, "tenant.cta.review_claim", ["admin"])
    _notif(cur, b, "tour.discontinued", ["tenant"])
    assert _as(cur, "aa_app_user", "UPDATE shared.notifications SET is_read = true", guc=a) == 1   # no WHERE on purpose
    assert _as(cur, "aa_app_user", "UPDATE shared.notifications SET is_read = true WHERE id = %s", (mine,),
               guc=b) == 0
    for sql, args in (("UPDATE shared.notifications SET payload = '{}'::jsonb", ()),
                      ("UPDATE shared.notifications SET tenant_id = %s", (b,)),
                      ("DELETE FROM shared.notifications", ()),
                      ("INSERT INTO shared.notifications (tenant_id, event_type, target_roles) "
                       "VALUES (%s, 'tour.discontinued', ARRAY['tenant'])", (a,))):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _as(cur, "aa_app_user", sql, args, guc=a)
