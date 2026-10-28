"""AA-665 C2 — migration 293: shared.dfs_call_log.job_id (uuid, nullable, no FK) + partial index. Real Postgres.

conftest applies every migration, 293 included. These tests check the replayed shape, then run the file again directly
(errors NOT swallowed): from the pre-293 state (column and index dropped, registration removed), twice (idempotent),
and that an INSERT with and without job_id works and the index serves the job lookup. Every test leaves the post-293
state behind.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "293_dfs_call_log_job_id.sql")
PROBE_ENDPOINT = "aa665_migration_293_probe"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.dfs_call_log WHERE endpoint = %s", (PROBE_ENDPOINT,))
        _run(c)                     # the post-293 state other tests see (idempotent)
        conn.close()


def _run(c):
    with open(MIGRATION, encoding="utf-8") as f:
        c.execute(f.read())


def _column(c):
    c.execute("""SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
                   FROM pg_attribute a
                   LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                  WHERE a.attrelid = 'shared.dfs_call_log'::regclass AND a.attname = 'job_id'
                    AND NOT a.attisdropped""")
    return c.fetchone()


def _index(c):
    c.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'shared' AND indexname = 'idx_dfs_call_log_job'")
    row = c.fetchone()
    return row[0] if row else None


def _registered(c):
    c.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '293'")
    return c.fetchone()[0]


def _no_fk(c):
    c.execute("""SELECT count(*) FROM pg_constraint
                  WHERE conrelid = 'shared.dfs_call_log'::regclass AND contype = 'f'""")
    return c.fetchone()[0] == 0


def _drop_293(c):
    c.execute("DROP INDEX IF EXISTS shared.idx_dfs_call_log_job")
    c.execute("ALTER TABLE shared.dfs_call_log DROP COLUMN IF EXISTS job_id")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '293'")


def test_replay_state_is_post_293(cur):
    assert _column(cur) == ("uuid", False, None)        # nullable, no default
    idx = _index(cur)
    assert idx is not None and "(job_id)" in idx and "WHERE (job_id IS NOT NULL)" in idx
    assert _no_fk(cur)                                  # fire-and-forget log: never fails on the job table
    assert _registered(cur) == 1
    cur.execute("SELECT col_description('shared.dfs_call_log'::regclass, "
                "(SELECT attnum FROM pg_attribute WHERE attrelid = 'shared.dfs_call_log'::regclass "
                "AND attname = 'job_id'))")
    assert "AA-665" in cur.fetchone()[0]


def test_applies_from_the_pre_293_state_and_is_idempotent(cur):
    _drop_293(cur)
    assert _column(cur) is None and _index(cur) is None
    _run(cur)
    assert _column(cur) == ("uuid", False, None)
    assert _index(cur) is not None
    assert _registered(cur) == 1
    _run(cur)                                           # a re-run changes nothing
    assert _column(cur) == ("uuid", False, None)
    assert _registered(cur) == 1


def test_rows_with_and_without_job_id(cur):
    """The code's INSERT names job_id; old-style rows (no job) keep working; a job's rows are found by job_id even
    when that job no longer exists (no FK)."""
    job_id = str(uuid.uuid4())
    cur.execute("""INSERT INTO shared.dfs_call_log (endpoint, cost_usd, cache_hit, fetched_live, job_id)
                   VALUES (%s, 0.06, false, true, %s), (%s, 0.06, false, true, %s), (%s, 0, true, false, NULL)""",
                (PROBE_ENDPOINT, job_id, PROBE_ENDPOINT, job_id, PROBE_ENDPOINT))
    cur.execute("SELECT count(*), sum(cost_usd)::float FROM shared.dfs_call_log WHERE job_id = %s", (job_id,))
    assert cur.fetchone() == (2, pytest.approx(0.12))
    cur.execute("SELECT count(*) FROM shared.dfs_call_log WHERE endpoint = %s AND job_id IS NULL", (PROBE_ENDPOINT,))
    assert cur.fetchone()[0] == 1
    cur.execute("BEGIN")
    cur.execute("SET LOCAL enable_seqscan = off")
    cur.execute("EXPLAIN SELECT id FROM shared.dfs_call_log WHERE job_id = %s", (job_id,))
    plan = "\n".join(r[0] for r in cur.fetchall())
    cur.execute("ROLLBACK")
    assert "idx_dfs_call_log_job" in plan
