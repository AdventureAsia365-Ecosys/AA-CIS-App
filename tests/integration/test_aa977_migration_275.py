"""AA-977 (CRM-B5) — CIS 275 (AA-authored content: gold_aa_internal.aa_content_item, aa_content_version,
v_aa_content_published) on real Postgres.

conftest replays 275 like every file. Each test runs in one transaction that is rolled back.

  * the file applied and a re-run is a no-op; the grants it leaves are exact (cis_app only; nothing for PUBLIC,
    aa_app_user or catalog_owner), also after a stray grant (the re-run takes it away), and the AA-859 invariant v3
    finds nothing on these objects;
  * guard 1 refuses a re-run over a table with other columns;
  * as cis_app: the draft -> publish -> draft -> publish cycle; a published version never changes and is never
    deleted (rule E), a draft is never in the served view (rule R), one draft per item, a version is inserted only
    as the next draft, an item is never deleted and its kind / locale / group_key never change;
  * the CHECKs refuse an unknown kind / locale, a bad group_key, an applies_to key outside the contract, a published
    row without its hash and a malformed hash.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import NO_PRIVILEGE_WITHOUT_RLS_SQL_V3, migration_body  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ITEM = "gold_aa_internal.aa_content_item"
VERSION = "gold_aa_internal.aa_content_version"
VIEW = "gold_aa_internal.v_aa_content_published"
HASH = "sha256:" + "a" * 64


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


async def _item(conn, *, group=None, locale="en", kind="prep_pack"):
    return await conn.fetchval(
        f"INSERT INTO {ITEM} (kind, locale, group_key, created_by) VALUES ($1, $2, $3, 'admin:test') "
        "RETURNING content_id", kind, locale, group or f"zz977-{uuid.uuid4().hex[:12]}")


async def _draft(conn, content_id, version, title="Before you go"):
    await conn.execute(
        f"INSERT INTO {VERSION} (content_id, version, title, body_markdown, applies_to, created_by) "
        "VALUES ($1, $2, $3, 'Body', $4::jsonb, 'admin:test')",
        content_id, version, title, json.dumps({"country_codes": ["VN"]}))


async def _publish(conn, content_id, version, content_hash=HASH):
    await conn.execute(
        f"UPDATE {VERSION} SET status = 'published', content_hash = $3, published_at = now(), "
        "published_by = 'admin:test', updated_at = now() WHERE content_id = $1 AND version = $2",
        content_id, version, content_hash)


async def _as_runtime(conn):
    """Log in as the CIS runtime: re-apply 275 (other modules re-create cis_app, which drops its grants), give it the
    schema USAGE that 244 gives on Dev (the replay skips 244), then SET ROLE. Rolled back with the test."""
    await conn.execute(migration_body(275))
    await conn.execute("GRANT USAGE ON SCHEMA gold_aa_internal TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")


async def _refused(conn, exc, sql, *args):
    with pytest.raises(exc):
        async with conn.transaction():
            await conn.execute(sql, *args)


# ── the file ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_275_applied_and_rerun_is_a_noop(conn):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '275'"), \
        "conftest swallowed an error in 275 (see its log)"
    await conn.execute(migration_body(275))
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '275'") == 1


@pytest.mark.asyncio
async def test_275_privileges_are_exact(conn):
    # Other modules drop and re-create catalog_owner / cis_app (DROP OWNED takes their grants), and a stray grant
    # must not survive a re-run: grant one, re-apply, check what the file leaves.
    await conn.execute(f"GRANT SELECT ON {ITEM}, {VIEW} TO aa_app_user, catalog_owner, PUBLIC")
    await conn.execute(f"GRANT DELETE, TRUNCATE ON {ITEM} TO cis_app")
    await conn.execute(migration_body(275))
    rows = await conn.fetch(f"""
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END AS r, c.relname AS o,
               a.privilege_type AS p
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.oid IN ('{ITEM}'::regclass, '{VERSION}'::regclass, '{VIEW}'::regclass)
           AND (a.grantee = 0 OR a.grantee IN ('cis_app'::regrole, 'aa_app_user'::regrole,
                                               'catalog_owner'::regrole))""")
    got = sorted((r["r"], r["o"], r["p"]) for r in rows)
    assert got == sorted([
        ("cis_app", "aa_content_item", "SELECT"), ("cis_app", "aa_content_item", "INSERT"),
        ("cis_app", "aa_content_item", "UPDATE"),
        ("cis_app", "aa_content_version", "SELECT"), ("cis_app", "aa_content_version", "INSERT"),
        ("cis_app", "aa_content_version", "UPDATE"), ("cis_app", "aa_content_version", "DELETE"),
        ("cis_app", "v_aa_content_published", "SELECT"),
    ])
    bad = [r for r in await conn.fetch(NO_PRIVILEGE_WITHOUT_RLS_SQL_V3) if "aa_content" in r["rel"]]
    assert bad == []


@pytest.mark.asyncio
async def test_275_guard_refuses_a_table_with_other_columns(conn):
    await conn.execute(f"ALTER TABLE {VERSION} ADD COLUMN zz977 int")
    with pytest.raises(asyncpg.RaiseError, match="exists with other columns"):
        async with conn.transaction():
            await conn.execute(migration_body(275))


# ── behaviour, as the CIS runtime ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_draft_publish_cycle_as_cis_app(conn):
    await _as_runtime(conn)
    cid = await _item(conn)
    await _draft(conn, cid, 1)
    # R: a draft is never served
    assert await conn.fetchval(f"SELECT count(*) FROM {VIEW} WHERE content_id = $1", cid) == 0
    await conn.execute(f"UPDATE {VERSION} SET title = 'Edited', updated_at = now() WHERE content_id = $1", cid)
    await _publish(conn, cid, 1)
    v1 = await conn.fetchrow(f"SELECT * FROM {VIEW} WHERE content_id = $1", cid)
    assert (v1["version"], v1["title"], v1["content_hash"], v1["kind"], v1["locale"]) == \
        (1, "Edited", HASH, "prep_pack", "en")

    # E: a published version never changes and is never deleted
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"UPDATE {VERSION} SET title = 'x' WHERE content_id = $1 AND version = 1", cid)
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"UPDATE {VERSION} SET status = 'draft', content_hash = NULL, published_at = NULL, "
                   "published_by = NULL WHERE content_id = $1 AND version = 1", cid)
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"DELETE FROM {VERSION} WHERE content_id = $1 AND version = 1", cid)

    # the next draft: only as version max + 1, only one at a time, never inserted as published
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"INSERT INTO {VERSION} (content_id, version, title, created_by) VALUES ($1, 3, 't', 'a')", cid)
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"INSERT INTO {VERSION} (content_id, version, status, title, content_hash, published_at, "
                   "published_by, created_by) VALUES ($1, 2, 'published', 't', $2, now(), 'a', 'a')", cid, HASH)
    await _draft(conn, cid, 2, title="Second")
    await _refused(conn, asyncpg.UniqueViolationError,
                   f"INSERT INTO {VERSION} (content_id, version, title, created_by) VALUES ($1, 3, 't', 'a')", cid)
    await _refused(conn, asyncpg.RestrictViolationError,
                   f"UPDATE {VERSION} SET version = 5 WHERE content_id = $1 AND version = 2", cid)
    assert [r["version"] for r in await conn.fetch(f"SELECT version FROM {VIEW} WHERE content_id = $1", cid)] == [1]

    # discard the draft, start again, publish v2: v1 stays as it was
    assert await conn.execute(f"DELETE FROM {VERSION} WHERE content_id = $1 AND version = 2", cid) == "DELETE 1"
    await _draft(conn, cid, 2, title="Second again")
    await _publish(conn, cid, 2, "sha256:" + "b" * 64)
    rows = await conn.fetch(f"SELECT version, title, content_hash FROM {VIEW} WHERE content_id = $1 ORDER BY 1", cid)
    assert [tuple(r) for r in rows] == [(1, "Edited", HASH), (2, "Second again", "sha256:" + "b" * 64)]

    # retire hides nothing in the view (the reader hides retired items from the list only)
    await conn.execute(f"UPDATE {ITEM} SET retired_at = now(), retired_by = 'admin:test' WHERE content_id = $1", cid)
    assert await conn.fetchval(f"SELECT count(*) FROM {VIEW} WHERE content_id = $1 AND retired_at IS NOT NULL",
                               cid) == 2


@pytest.mark.asyncio
async def test_item_is_never_deleted_and_its_identity_never_changes(conn):
    await _as_runtime(conn)
    cid = await _item(conn, group="zz977-identity")
    await _refused(conn, asyncpg.InsufficientPrivilegeError, f"DELETE FROM {ITEM} WHERE content_id = $1", cid)
    await _refused(conn, asyncpg.InsufficientPrivilegeError, f"TRUNCATE {VERSION}")
    for column, value in (("locale", "vi"), ("kind", "campaign_block"), ("group_key", "zz977-other")):
        await _refused(conn, asyncpg.RestrictViolationError,
                       f"UPDATE {ITEM} SET {column} = $2 WHERE content_id = $1", cid, value)
    # the other locale of the same group is a second item; the same locale twice is refused
    await _item(conn, group="zz977-identity", locale="vi")
    await _refused(conn, asyncpg.UniqueViolationError,
                   f"INSERT INTO {ITEM} (kind, locale, group_key, created_by) "
                   "VALUES ('prep_pack', 'en', 'zz977-identity', 'admin:test')")


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [
    f"INSERT INTO {ITEM} (kind, locale, group_key, created_by) VALUES ('newsletter', 'en', 'zz977-k', 'a')",
    f"INSERT INTO {ITEM} (kind, locale, group_key, created_by) VALUES ('prep_pack', 'fr', 'zz977-k', 'a')",
    f"INSERT INTO {ITEM} (kind, locale, group_key, created_by) VALUES ('prep_pack', 'en', 'Bad Key', 'a')",
    f"INSERT INTO {ITEM} (kind, locale, group_key, created_by) VALUES ('prep_pack', 'en', 'zz977-k', '')",
    f"UPDATE {ITEM} SET retired_at = now() WHERE content_id = $1",
    f"INSERT INTO {VERSION} (content_id, version, title, created_by) VALUES ($1, 1, '', 'a')",
    f"INSERT INTO {VERSION} (content_id, version, title, body_markdown, created_by) "
    "VALUES ($1, 1, 't', repeat('x', 20001), 'a')",
    f"INSERT INTO {VERSION} (content_id, version, title, applies_to, created_by) "
    "VALUES ($1, 1, 't', '{\"regions\": []}'::jsonb, 'a')",
    f"INSERT INTO {VERSION} (content_id, version, title, applies_to, created_by) VALUES ($1, 1, 't', '[]'::jsonb, 'a')",
    f"INSERT INTO {VERSION} (content_id, version, title, content_hash, created_by) "
    f"VALUES ($1, 1, 't', '{HASH}', 'a')",
])
async def test_checks_refuse(conn, sql):
    cid = await _item(conn)
    args = (cid,) if "$1" in sql else ()
    await _refused(conn, asyncpg.CheckViolationError, sql, *args)


@pytest.mark.asyncio
async def test_publish_needs_hash_by_and_at(conn):
    cid = await _item(conn)
    await _draft(conn, cid, 1)
    await _refused(conn, asyncpg.CheckViolationError,
                   f"UPDATE {VERSION} SET status = 'published' WHERE content_id = $1", cid)
    await _refused(conn, asyncpg.CheckViolationError,
                   f"UPDATE {VERSION} SET status = 'published', content_hash = 'sha256:abc', published_at = now(), "
                   "published_by = 'a' WHERE content_id = $1", cid)
