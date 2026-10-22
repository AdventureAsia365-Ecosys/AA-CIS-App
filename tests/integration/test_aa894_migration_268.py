"""AA-894 (S247) — migration 268 (FK published_tours.quality_score_id -> quality_scores(id), ON DELETE SET NULL,
index on the referencing column, dangling pointers repaired) against real Postgres.

conftest applies every migration (268 included). These tests run the file again directly, errors NOT swallowed:
from the pre-268 state, twice (idempotent), with dangling pointers to repair, and each guard. Every test restores
the post-268 state and removes its own rows.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "268_published_tours_quality_score_fk.sql")
FK = "published_tours_quality_score_id_fkey"
INDEX = "idx_published_tours_quality_score_id"
TENANT = "00000000-0000-0000-0000-000000000001"
BATCH = "00000000-0000-0000-0000-000000000002"
_MADE: list[str] = []                           # tour ids a test created (removed at teardown)


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    # The test rows' tenant (tenant_isolation policies; CI's superuser bypasses RLS, a local non-superuser does not).
    c.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT,))
    _MADE.clear()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        _cleanup(c, list(_MADE))
        _undo(c)
        _run(c)                                 # back to the post-268 state for other tests
        conn.close()


def _undo(c):
    """The pre-268 state: no FK, no index, version not registered."""
    c.execute(f"ALTER TABLE gold_aa_internal.published_tours DROP CONSTRAINT IF EXISTS {FK}")
    c.execute(f"DROP INDEX IF EXISTS gold_aa_internal.{INDEX}")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '268'")


def _run(c):
    c.execute(open(MIGRATION, encoding="utf-8").read())


def _cleanup(c, tours):
    for t in tours:
        c.execute("DELETE FROM gold_aa_internal.published_tours WHERE tour_id = %s", (t,))
        c.execute("DELETE FROM silver_aa_internal.quality_scores WHERE tour_id = %s", (t,))
        c.execute("DELETE FROM silver_aa_internal.generated_content WHERE tour_id = %s", (t,))
        c.execute("DELETE FROM silver_aa_internal.raw_tours WHERE tour_id = %s", (t,))


def _tour(c):
    """A raw tour + one version; returns (tour_id, generated_content_id)."""
    tour_id, content_id = str(uuid.uuid4()), str(uuid.uuid4())
    _MADE.append(tour_id)
    c.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name) "
              "VALUES (%s, %s, %s, 'aa894 fk test')", (tour_id, TENANT, BATCH))
    c.execute("INSERT INTO silver_aa_internal.generated_content (id, tour_id, tenant_id, version_num, aa_name) "
              "VALUES (%s, %s, %s, 1, 'aa894')", (content_id, tour_id, TENANT))
    return tour_id, content_id


def _score(c, tour_id, content_id, evaluated_at="2026-10-01T00:00:00Z"):
    c.execute("INSERT INTO silver_aa_internal.quality_scores "
              "(generated_content_id, tour_id, tenant_id, score_overall, evaluated_at) "
              "VALUES (%s, %s, %s, 8.0, %s) RETURNING id::text", (content_id, tour_id, TENANT, evaluated_at))
    return c.fetchone()[0]


def _publish(c, tour_id, content_id, score_id):
    c.execute("INSERT INTO gold_aa_internal.published_tours "
              "(tour_id, generated_content_id, tenant_id, aa_name, quality_score, quality_score_id) "
              "VALUES (%s, %s, %s, 'aa894', 8.0, %s) RETURNING id::text", (tour_id, content_id, TENANT, score_id))
    return c.fetchone()[0]


def _pointer(c, pt_id):
    c.execute("SELECT quality_score_id::text FROM gold_aa_internal.published_tours WHERE id = %s", (pt_id,))
    return c.fetchone()[0]


def _fk(c):
    c.execute("""
        SELECT confrelid::regclass::text, confdeltype, convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'gold_aa_internal.published_tours'::regclass AND conname = %s
    """, (FK,))
    return c.fetchone()


def _assert_post_268(c):
    target, deltype, validated, definition = _fk(c)
    assert target == "silver_aa_internal.quality_scores"
    assert deltype == "n"                       # ON DELETE SET NULL
    assert validated is True
    assert definition.startswith("FOREIGN KEY (quality_score_id) REFERENCES silver_aa_internal.quality_scores(id)")
    c.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'gold_aa_internal' AND indexname = %s", (INDEX,))
    (indexdef,) = c.fetchone()
    assert "(quality_score_id)" in indexdef
    c.execute("SELECT 1 FROM shared.schema_versions WHERE version = '268'")
    assert c.fetchone() is not None


def test_replay_left_the_post_268_state(cur):
    _assert_post_268(cur)


def test_from_pre_268_state_and_twice(cur):
    _undo(cur)
    assert _fk(cur) is None
    _run(cur)
    _assert_post_268(cur)
    _run(cur)                                   # idempotent: second run changes nothing and does not fail
    _assert_post_268(cur)
    cur.execute("SELECT count(*) FROM pg_constraint WHERE conrelid = 'gold_aa_internal.published_tours'::regclass "
                "AND contype = 'f' AND conname = %s", (FK,))
    assert cur.fetchone()[0] == 1


def test_dangling_pointers_are_repaired(cur):
    """Revalidation replaced a published version's score row: the pointer moves to the version's current row;
    a version with no score row left gets NULL; a valid pointer is left alone."""
    _undo(cur)
    # A: published with score s_old, then revalidated (s_old deleted, two newer rows): -> latest row of the version.
    t_a, c_a = _tour(cur)
    s_old = _score(cur, t_a, c_a, "2026-09-01T00:00:00Z")
    pt_a = _publish(cur, t_a, c_a, s_old)
    cur.execute("DELETE FROM silver_aa_internal.quality_scores WHERE id = %s", (s_old,))
    _score(cur, t_a, c_a, "2026-10-01T00:00:00Z")
    s_latest = _score(cur, t_a, c_a, "2026-10-02T00:00:00Z")
    # B: published with a score row that is gone and the version has none: -> NULL.
    t_b, c_b = _tour(cur)
    s_gone = _score(cur, t_b, c_b)
    pt_b = _publish(cur, t_b, c_b, s_gone)
    cur.execute("DELETE FROM silver_aa_internal.quality_scores WHERE id = %s", (s_gone,))
    # C: a valid pointer to an OLDER row of the version stays as it is.
    t_c, c_c = _tour(cur)
    s_c = _score(cur, t_c, c_c, "2026-09-01T00:00:00Z")
    _score(cur, t_c, c_c, "2026-10-01T00:00:00Z")
    pt_c = _publish(cur, t_c, c_c, s_c)
    # D: no pointer at all stays NULL.
    t_d, c_d = _tour(cur)
    pt_d = _publish(cur, t_d, c_d, None)

    _run(cur)

    assert _pointer(cur, pt_a) == s_latest
    assert _pointer(cur, pt_b) is None
    assert _pointer(cur, pt_c) == s_c
    assert _pointer(cur, pt_d) is None
    _assert_post_268(cur)


def test_after_268_a_missing_score_is_refused(cur):
    t, c = _tour(cur)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _publish(cur, t, c, str(uuid.uuid4()))


def test_after_268_deleting_the_score_row_nulls_the_pointer(cur):
    """The revalidation path (DELETE the version's quality_scores rows, INSERT new ones) keeps working."""
    t, c = _tour(cur)
    s = _score(cur, t, c)
    pt = _publish(cur, t, c, s)
    cur.execute("DELETE FROM silver_aa_internal.quality_scores WHERE generated_content_id = %s", (c,))
    assert _pointer(cur, pt) is None


def test_refuses_a_different_constraint_under_the_name(cur):
    _undo(cur)
    cur.execute(f"ALTER TABLE gold_aa_internal.published_tours ADD CONSTRAINT {FK} FOREIGN KEY (quality_score_id) "
                "REFERENCES silver_aa_internal.quality_scores (id) NOT VALID")      # NO ACTION, not SET NULL
    with pytest.raises(psycopg2.errors.RaiseException, match="a different constraint"):
        _run(cur)
    cur.execute("ROLLBACK")
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '268'")
    assert cur.fetchone() is None               # a refused file registers nothing


def test_refuses_a_second_fk_on_the_column(cur):
    _undo(cur)
    cur.execute("ALTER TABLE gold_aa_internal.published_tours ADD CONSTRAINT aa894_other_fk FOREIGN KEY "
                "(quality_score_id) REFERENCES silver_aa_internal.quality_scores (id) ON DELETE SET NULL")
    try:
        with pytest.raises(psycopg2.errors.RaiseException, match="2 foreign keys"):
            _run(cur)
        cur.execute("ROLLBACK")
    finally:
        cur.execute("ALTER TABLE gold_aa_internal.published_tours DROP CONSTRAINT IF EXISTS aa894_other_fk")


def test_refuses_when_the_column_is_not_uuid(cur):
    _undo(cur)
    cur.execute("ALTER TABLE gold_aa_internal.published_tours ALTER COLUMN quality_score_id TYPE text "
                "USING quality_score_id::text")
    try:
        with pytest.raises(psycopg2.errors.RaiseException, match="is text, expected uuid"):
            _run(cur)
        cur.execute("ROLLBACK")
    finally:
        cur.execute("ALTER TABLE gold_aa_internal.published_tours ALTER COLUMN quality_score_id TYPE uuid "
                    "USING quality_score_id::uuid")


def test_file_follows_the_migration_rules(cur):
    sql = open(MIGRATION, encoding="utf-8").read()
    body = sql[sql.index("BEGIN;"):]
    assert body.startswith("BEGIN;\nSET LOCAL lock_timeout = '5s';\nSET LOCAL statement_timeout = '15min';")  # MIG-9
    assert "NOT VALID" in body and "VALIDATE CONSTRAINT " + FK in body                                       # MIG-11
    assert body.index("NOT VALID") < body.index("VALIDATE CONSTRAINT")
    assert "ON CONFLICT (version) DO NOTHING" in body and body.rstrip().endswith("COMMIT;")
    assert "OWNER TO" not in body and "RESET ROLE" not in body                                               # X1 rules
