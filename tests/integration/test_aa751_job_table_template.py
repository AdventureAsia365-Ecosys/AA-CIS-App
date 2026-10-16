"""AA-751 (X6) — shared.job against the job table DDL template, on a real Postgres.

conftest applies every migration, 242 included, and swallows migration errors: the shape comparison below
is what catches a 242 that failed to apply. The template is rendered into a scratch schema inside a
transaction that is rolled back, then compared with shared.job through pg_catalog
(shared/jobs/job_table_shape.py). Also covered: the new CHECKs and the updated_at trigger by behaviour,
the comparator catching drift (mutation checks), 242's guard refusing rows that break a new CHECK, 242 on
the pre-242 shape (what Dev runs), and a re-run being a no-op.
"""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

# The integration run adds only this directory to sys.path (conftest); add the repo root for `shared.*`.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import job_table_shape as shape  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M242 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "242_job_table_template_conformance.sql")
SCRATCH = "x6_tmpl"
TAG = "aa751-test"


def _m242_body() -> str:
    """242 without its own BEGIN/COMMIT, so a test can run it in a transaction it rolls back."""
    with open(M242) as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


async def _template_shape(conn) -> shape.Shape:
    """Renders the CIS instance of the template into SCRATCH (caller holds a transaction it rolls back)."""
    await conn.execute(f"CREATE SCHEMA {SCRATCH}")
    await conn.execute(shape.render(shape.load_template(), schema=SCRATCH, kind_pattern=shape.CIS_KIND_PATTERN,
                                    extra_statuses=", 'stopped_budget'", max_attempts_default=3))
    return await shape.read_shape(conn, SCRATCH)


async def _undo_242(conn) -> None:
    """shared.job back to its CIS 174 + 233 shape (inside the caller's rolled-back transaction)."""
    await conn.execute("""
        DROP TRIGGER IF EXISTS job_set_updated_at ON shared.job;
        DROP FUNCTION IF EXISTS shared.job_set_updated_at();
        ALTER TABLE shared.job DROP CONSTRAINT IF EXISTS job_kind_chk,
                               DROP CONSTRAINT IF EXISTS job_lease_chk,
                               DROP CONSTRAINT IF EXISTS job_attempts_chk,
                               ADD CONSTRAINT job_max_attempts_check CHECK (max_attempts >= 1);
        ALTER TABLE shared.job RESET (autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold);
    """)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def test_242_is_registered(conn):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '242'") == 1


async def test_shared_job_matches_the_template(conn):
    tmpl = await _template_shape(conn)
    diffs = shape.compare(tmpl, await shape.read_shape(conn, "shared"))
    assert diffs == [], "\n".join(diffs)


async def test_read_shape_restores_the_search_path(conn):
    before = await conn.fetchval("SHOW search_path")
    await shape.read_shape(conn, "shared")
    assert await conn.fetchval("SHOW search_path") == before


async def test_read_shape_of_a_missing_table_reports_not_found(conn):
    # Booking B2's first run, or a wrong schema name: compare() says "table not found", read_shape does not
    # raise InFailedSQLTransactionError over the real problem, and the caller's transaction stays usable.
    before = await conn.fetchval("SHOW search_path")
    missing = await shape.read_shape(conn, "x6_no_such_schema")
    assert missing.columns == {} and missing.constraints == [] and missing.triggers == []
    tmpl = await _template_shape(conn)
    assert shape.compare(tmpl, missing) == ["x6_no_such_schema.job: table not found or has no columns"]
    assert await conn.fetchval("SHOW search_path") == before


async def test_read_shape_error_is_not_masked(conn):
    # A catalog query that fails mid-read surfaces its own error (not "current transaction is aborted"),
    # and the savepoint rollback leaves the caller's transaction and search_path intact.
    before = await conn.fetchval("SHOW search_path")
    orig = shape._INDEXES_SQL
    shape._INDEXES_SQL = "SELECT no_such_column FROM pg_index WHERE indrelid = $1::regclass"
    try:
        with pytest.raises(asyncpg.UndefinedColumnError):
            await shape.read_shape(conn, "shared")
    finally:
        shape._INDEXES_SQL = orig
    assert await conn.fetchval("SHOW search_path") == before
    assert (await shape.read_shape(conn, "shared")).columns


@pytest.mark.parametrize("kind", ["bk_x", "tp_x", "Bad-Kind", "1kind", ""])
async def test_kind_check_rejects_other_owners_and_bad_names(conn, kind):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("INSERT INTO shared.job (kind, created_by) VALUES ($1, $2)", kind, TAG)


async def test_kind_check_accepts_cis_kinds(conn):
    for kind in ("a3_atomize", "retention_llm_call_log", "s1_rewrite", "x6_probe", "ops_rds_role_connections"):
        await conn.execute("INSERT INTO shared.job (kind, created_by) VALUES ($1, $2)", kind, TAG)


async def test_running_needs_a_lease(conn):
    job_id = await conn.fetchval("INSERT INTO shared.job (kind, created_by) VALUES ('k', $1) RETURNING id", TAG)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE shared.job SET status = 'running' WHERE id = $1", job_id)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE shared.job SET status = 'running', locked_by = 'w' WHERE id = $1", job_id)
    await conn.execute("UPDATE shared.job SET status = 'running', locked_by = 'w', "
                       "locked_until = now() + interval '1 minute' WHERE id = $1", job_id)


async def test_attempt_cannot_go_negative(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("INSERT INTO shared.job (kind, attempt, created_by) VALUES ('k', -1, $1)", TAG)


async def test_update_bumps_updated_at(conn):
    job_id = await conn.fetchval(
        "INSERT INTO shared.job (kind, created_by, updated_at) VALUES ('k', $1, now() - interval '1 day') "
        "RETURNING id", TAG)
    # An UPDATE that does not mention updated_at (like the two _COALESCE_SQL) still sets it.
    bumped = await conn.fetchval(
        "UPDATE shared.job SET run_after = now() WHERE id = $1 RETURNING updated_at = now()", job_id)
    assert bumped is True


async def test_comparator_catches_drift(conn):
    tmpl = await _template_shape(conn)
    await conn.execute("""
        ALTER TABLE shared.job DROP CONSTRAINT job_lease_chk;
        ALTER TABLE shared.job ADD CONSTRAINT job_lease_chk
            CHECK (status <> 'running' OR (locked_by IS NOT NULL AND locked_until IS NOT NULL)) NOT VALID;
        DROP TRIGGER job_set_updated_at ON shared.job;
        DROP INDEX shared.job_parent_idx;
        CREATE INDEX job_due_extra_idx ON shared.job (run_after);
        ALTER TABLE shared.job RESET (autovacuum_vacuum_threshold);
        ALTER TABLE shared.job ALTER COLUMN max_attempts SET DEFAULT 5;
        ALTER TABLE shared.job ADD COLUMN extra_col TEXT;
        ALTER TABLE shared.job DROP CONSTRAINT job_status_check;
        ALTER TABLE shared.job ADD CONSTRAINT job_status_check
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'));
    """)
    diffs = "\n".join(shape.compare(tmpl, await shape.read_shape(conn, "shared")))
    for expected in (
        "constraint not validated (NOT VALID)",
        "trigger missing: BEFORE UPDATE FOR EACH ROW",
        "index missing: USING btree (parent_job_id) WHERE (parent_job_id IS NOT NULL)",
        "index not in template: USING btree (run_after)",
        "reloption missing: autovacuum_vacuum_threshold=50",
        "column max_attempts: default '5', template '3'",
        "column not in template: extra_col text",
        "status CHECK: missing ['stopped_budget']",
    ):
        assert expected in diffs, diffs
    # Owner-only extras can be allowed explicitly.
    allowed = shape.compare(tmpl, await shape.read_shape(conn, "shared"),
                            allow_extra_indexes=("USING btree (run_after)",), allow_extra_columns=("extra_col",))
    assert not any("run_after)" in d and "not in template" in d for d in allowed)
    assert not any("extra_col" in d for d in allowed)


async def test_242_on_the_pre_242_shape_conforms_and_reruns(conn):
    await _undo_242(conn)
    tmpl = await _template_shape(conn)
    assert shape.compare(tmpl, await shape.read_shape(conn, "shared")) != []
    await conn.execute(_m242_body())
    assert shape.compare(tmpl, await shape.read_shape(conn, "shared")) == []
    await conn.execute(_m242_body())  # re-run: no-op
    assert shape.compare(tmpl, await shape.read_shape(conn, "shared")) == []
    assert await conn.fetchval("SELECT count(*) FROM shared.job WHERE created_by = 'migration-242-probe'") == 0


@pytest.mark.parametrize("bad_row, rule", [
    ("INSERT INTO shared.job (kind, status, created_by) VALUES ('k', 'running', 'aa751-test')", "1 lease"),
    ("INSERT INTO shared.job (kind, created_by) VALUES ('bk_foreign', 'aa751-test')", "1 kind"),
    ("INSERT INTO shared.job (kind, attempt, created_by) VALUES ('k', -2, 'aa751-test')", "1 attempt"),
])
async def test_242_guard_refuses_rows_that_break_a_new_check(conn, bad_row, rule):
    await _undo_242(conn)
    await conn.execute(bad_row)
    with pytest.raises(asyncpg.RaiseError, match=rf"{rule} rows|{rule} /"):
        await conn.execute(_m242_body())


async def test_242_guard_refuses_a_drifted_constraint_of_the_same_name(conn):
    await _undo_242(conn)
    await conn.execute("ALTER TABLE shared.job ADD CONSTRAINT job_lease_chk CHECK (locked_by IS NOT NULL "
                       "OR status <> 'running')")
    with pytest.raises(asyncpg.RaiseError, match="constraint job_lease_chk exists as"):
        await conn.execute(_m242_body())
