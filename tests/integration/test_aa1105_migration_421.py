"""AA-1105 (S281) — CIS 421: every learn child row carries its learner's tenant, a learner's tenant never changes, and
learn.lab_run has its learner index (conftest replays 421 with the other files). Staff rows (tenant NULL) and agency
rows (tenant set) are accepted only with the learner's own tenant, on INSERT and on UPDATE; a learner hidden by the
caller's row-level security (aa_app_user of another tenant, the P7 shape) is refused; the retention cascade still
works; a re-run changes nothing. Runs in a transaction that is rolled back."""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M421 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "421_aa1105_learn_child_tenant.sql")
CHILDREN = ("enrollment", "statement", "lesson_progress", "quiz_attempt", "review_card", "lab_run", "certificate")
ISS = "https://cognito-idp.example.invalid/pool"
TENANT_A = uuid.UUID("11050421-0000-4000-8000-00000000000a")
TENANT_B = uuid.UUID("11050421-0000-4000-8000-00000000000b")
SHA = "c" * 64

# One INSERT per child table: $1 learner_id, $2 tenant_id.
INSERTS = {
    "enrollment": "INSERT INTO learn.enrollment (learner_id, tenant_id, path_id) VALUES ($1, $2, 'sample-path')",
    "statement": ("INSERT INTO learn.statement (learner_id, tenant_id, verb, object_type, object_id, body_sha256) "
                  f"VALUES ($1, $2, 'opened', 'lesson', 'Z9-01', '{SHA}')"),
    "lesson_progress": ("INSERT INTO learn.lesson_progress (learner_id, tenant_id, lesson_id, module_id, "
                        "module_version) VALUES ($1, $2, 'Z9-01', 'Z9', '1.0.0')"),
    "quiz_attempt": ("INSERT INTO learn.quiz_attempt (learner_id, tenant_id, scope, target_id, item_ids, "
                     "module_versions, bundle_sha, pass_mark_pct) VALUES ($1, $2, 'module', 'Z9', ARRAY['Z9-Q1'], "
                     "'{\"Z9\": \"1.0.0\"}', 'abc', 80)"),
    "review_card": ("INSERT INTO learn.review_card (learner_id, tenant_id, item_id, module_id, card, due_at) "
                    "VALUES ($1, $2, 'Z9-Q1', 'Z9', '{}', now())"),
    "lab_run": "INSERT INTO learn.lab_run (learner_id, tenant_id, module_id) VALUES ($1, $2, 'Z9')",
    "certificate": ("INSERT INTO learn.certificate (learner_id, tenant_id, path_id, path_title, code_sha256, "
                    "module_versions) VALUES ($1, $2, 'sample-path', 'Sample path', "
                    "encode(sha256(gen_random_uuid()::text::bytea), 'hex'), '{}')"),
}


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


async def _learner(conn, tenant_id=None) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO learn.learner (iss, sub, kind, tenant_id) VALUES ($1, $2, $3, $4) RETURNING learner_id",
        ISS, str(uuid.uuid4()), "partner" if tenant_id else "staff", tenant_id)


async def _refused(conn, error, sql, *args):
    with pytest.raises(error):
        async with conn.transaction():
            await conn.execute(sql, *args)


@pytest.mark.asyncio
async def test_421_registered_with_triggers_and_index(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '421'") == "421"
    names = {r["t"]: r["g"] for r in await conn.fetch(
        "SELECT c.relname AS t, array_agg(g.tgname::text) AS g FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'learn' AND NOT g.tgisinternal GROUP BY 1")}
    for t in CHILDREN:
        assert f"{t}_tenant_trg" in names[t], t
    assert "learner_tenant_fixed_trg" in names["learner"]
    assert await conn.fetchval("SELECT indexdef AS ok FROM pg_indexes WHERE schemaname = 'learn' "
                               "AND indexname = 'lab_run_learner_idx'").endswith("(learner_id, module_id, started_at)")


@pytest.mark.asyncio
@pytest.mark.parametrize("table", CHILDREN)
async def test_child_takes_its_learners_tenant_only(conn, table):
    staff = await _learner(conn)
    agency = await _learner(conn, TENANT_A)
    sql = INSERTS[table]
    await _refused(conn, asyncpg.CheckViolationError, sql, staff, TENANT_A)       # staff rows carry no tenant
    await _refused(conn, asyncpg.CheckViolationError, sql, agency, None)          # agency rows carry theirs
    await _refused(conn, asyncpg.CheckViolationError, sql, agency, TENANT_B)      # never another agency's
    await _refused(conn, asyncpg.ForeignKeyViolationError, sql, uuid.uuid4(), None)
    async with conn.transaction():
        await conn.execute(sql, staff, None)
    async with conn.transaction():
        await conn.execute(sql, agency, TENANT_A)


@pytest.mark.asyncio
async def test_update_cannot_move_a_row_to_another_tenant_or_learner(conn):
    agency = await _learner(conn, TENANT_A)
    other = await _learner(conn, TENANT_B)
    await conn.execute(INSERTS["enrollment"], agency, TENANT_A)
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE learn.enrollment SET tenant_id = $2 WHERE learner_id = $1", agency, TENANT_B)
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE learn.enrollment SET learner_id = $2 WHERE learner_id = $1", agency, other)
    async with conn.transaction():                                      # other columns update freely
        await conn.execute("UPDATE learn.enrollment SET status = 'withdrawn' WHERE learner_id = $1", agency)


@pytest.mark.asyncio
async def test_learner_tenant_never_changes(conn):
    agency = await _learner(conn, TENANT_A)
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE learn.learner SET tenant_id = $2 WHERE learner_id = $1", agency, TENANT_B)
    async with conn.transaction():
        await conn.execute("UPDATE learn.learner SET display_name = 'Sample Learner' WHERE learner_id = $1", agency)


@pytest.mark.asyncio
async def test_a_learner_hidden_by_row_security_is_refused(conn):
    """The P7 shape: aa_app_user under tenant A cannot hang a row below a learner of tenant B, even writing B's id."""
    own = await _learner(conn, TENANT_A)
    other = await _learner(conn, TENANT_B)
    await conn.execute("GRANT USAGE ON SCHEMA learn TO aa_app_user")
    await conn.execute("GRANT SELECT, INSERT ON learn.learner, learn.enrollment TO aa_app_user")
    await conn.execute("SET LOCAL ROLE aa_app_user")
    await conn.execute(f"SET LOCAL app.tenant_id = '{TENANT_A}'")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with conn.transaction():
            await conn.execute(INSERTS["enrollment"], other, TENANT_A)
    async with conn.transaction():                                      # its own tenant's learner works
        await conn.execute(INSERTS["enrollment"], own, TENANT_A)


@pytest.mark.asyncio
async def test_retention_cascade_still_deletes_everything(conn):
    agency = await _learner(conn, TENANT_A)
    for table in CHILDREN:
        await conn.execute(INSERTS[table], agency, TENANT_A)
    await conn.execute("SET LOCAL learn.retention_purge = 'on'")
    await conn.execute("DELETE FROM learn.learner WHERE learner_id = $1", agency)
    for table in CHILDREN:
        assert await conn.fetchval(f"SELECT count(*) AS n FROM learn.{table} WHERE learner_id = $1", agency) == 0


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M421, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
                                 "WHERE c.relnamespace = 'learn'::regnamespace AND NOT g.tgisinternal")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
                               "WHERE c.relnamespace = 'learn'::regnamespace AND NOT g.tgisinternal") == before
