"""AA-1051 (Quality W5 code part, root ADR 0020) — CIS 400 shared.eval_golden_split on real Postgres (conftest replays
every migration).

The table, its CHECKs (split name, rule shape, bucket range, actor), one row per golden item (set, sample), the FK to
the golden item (a row needs its item; deleting the item takes the row), the append-only trigger (UPDATE refused
even with an UPDATE grant), RLS (ENABLE + FORCE, the X1 parity policies), cis_app's grants (SELECT, INSERT, DELETE)
and the registration. The split writers and readers are tested in the code PR (test_aa1051_golden_split_db.py).

Invented values only; everything runs in a transaction that is rolled back."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SAMPLE = "zz-1051-t9b-1"


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


async def _item(c) -> str:
    """A golden set with one T9 blog item (as the owner); returns the set id."""
    set_id = await c.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, status, source_uri, source_sha256, seed, holdout_pct, "
        "tier_rules, shared_inputs, meta, label_history, item_count, imported_by) VALUES ('zz_aa1051', '1.0.0', "
        "'active', 'file:zz-aa1051.json', $1, 7, 0.300, '{}', '{}', '{}', '[]', 1, 'pytest') RETURNING set_id",
        "c" * 64)
    await c.execute(
        "INSERT INTO shared.eval_golden_item (set_id, sample_id, writer, channel, scope, tenant_id, split, trap, kind, "
        "tiers, input, label, label_verdict, label_source, refs, input_sha256) VALUES ($1, $2, 't9_blog', 'blog', "
        "'tenant', '00000000-0000-4000-8000-0000000a1051', 'tune', false, 'synthetic', '{}', '{}', '{}', 'pass', "
        "'synthetic', '{}', $3)", set_id, SAMPLE, "d" * 64)
    return set_id


async def _split(c, set_id, **over):
    row = {"sample_id": SAMPLE, "eval_split": "gate", "rule": "aa1051-split-v1", "bucket": 0.812345,
           "assigned_by": "pytest"}
    row.update(over)
    await c.execute(
        "INSERT INTO shared.eval_golden_split (set_id, sample_id, eval_split, rule, bucket, assigned_by) "
        "VALUES ($1, $2, $3, $4, $5, $6)", set_id, row["sample_id"], row["eval_split"], row["rule"], row["bucket"],
        row["assigned_by"])


@pytest.mark.asyncio
async def test_400_registered_one_row_per_item_and_cascade(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '400'") == "400"
    s = await _item(conn)
    await _split(conn, s)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with conn.transaction():                          # a savepoint: the test goes on after the error
            await _split(conn, s, eval_split="dev")
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        async with conn.transaction():
            await _split(conn, s, sample_id="zz-1051-missing")
    await conn.execute("DELETE FROM shared.eval_golden_item WHERE set_id = $1", s)
    assert await conn.fetchval("SELECT count(*) AS ok FROM shared.eval_golden_split WHERE set_id = $1", s) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {"eval_split": "train"}, {"eval_split": "GATE"}, {"rule": "Bad Rule"}, {"rule": ""},
    {"bucket": 1.0}, {"bucket": -0.1}, {"assigned_by": ""},
])
async def test_400_checks_refuse(conn, over):
    s = await _item(conn)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _split(conn, s, **over)


@pytest.mark.asyncio
async def test_400_append_only(conn):
    s = await _item(conn)
    await _split(conn, s)
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn.execute("UPDATE shared.eval_golden_split SET eval_split = 'dev' WHERE set_id = $1", s)


@pytest.mark.asyncio
async def test_400_rls_policies_and_grants(conn):
    flags = await conn.fetchrow(
        "SELECT relrowsecurity AS rls, relforcerowsecurity AS force FROM pg_class "
        "WHERE oid = 'shared.eval_golden_split'::regclass")
    assert flags["rls"] and flags["force"]
    policies = {r["polname"]: r["roles"] for r in await conn.fetch(
        "SELECT policyname AS polname, roles::text[] AS roles FROM pg_policies "
        "WHERE schemaname = 'shared' AND tablename = 'eval_golden_split'")}
    assert policies == {"eval_golden_split_cis_app_all": ["cis_app"],
                        "eval_golden_split_cis_owner_all": ["cis_owner"]}
    privs = {r["p"] for r in await conn.fetch(
        "SELECT privilege_type AS p FROM information_schema.role_table_grants "
        "WHERE table_schema = 'shared' AND table_name = 'eval_golden_split' AND grantee = 'cis_app'")}
    assert {"SELECT", "INSERT", "DELETE"} <= privs
    assert not await conn.fetchval(
        "SELECT count(*) AS ok FROM information_schema.role_table_grants WHERE table_schema = 'shared' "
        "AND table_name = 'eval_golden_split' AND grantee IN ('aa_app_user', 'catalog_owner', 'PUBLIC')")
