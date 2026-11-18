"""AA-1133 (S284) — CIS 454 acp_shared.docs_feedback (conftest replays every migration).

The table is registered; one row per (repo, path, actor) and an upsert replaces the answer; the checks refuse a bad
repo, path, language and an empty or too long note; the runtime role reads, inserts and updates; a re-run changes
nothing. Runs in a transaction that is rolled back; invented paths only."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M454 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "454_aa1133_docs_feedback.sql")
UPSERT = ("INSERT INTO acp_shared.docs_feedback (repo, path, lang, helpful, comment, actor, role) "
          "VALUES ($1, $2, $3, $4, $5, $6, 'admin') "
          "ON CONFLICT (repo, path, actor) DO UPDATE SET helpful = EXCLUDED.helpful, comment = EXCLUDED.comment, "
          "lang = EXCLUDED.lang, role = EXCLUDED.role, updated_at = now()")


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


def _body(path: str) -> str:
    return open(path, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


@pytest.mark.asyncio
async def test_454_registered(conn):
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '454'") == 1


@pytest.mark.asyncio
async def test_454_one_answer_per_person_and_page(conn):
    page = "docs/guides/zz-lantern-market.md"
    await conn.execute(UPSERT, "AA-Ecosys", page, "en", True, None, "admin:zz-1")
    await conn.execute(UPSERT, "AA-Ecosys", page, "vi", False, "Step two names an old menu.", "admin:zz-1")
    await conn.execute(UPSERT, "AA-Ecosys", page, "en", True, None, "admin:zz-2")
    rows = await conn.fetch("SELECT actor, helpful, comment, lang, updated_at >= created_at AS ok "
                            "FROM acp_shared.docs_feedback WHERE path = $1 ORDER BY actor", page)
    assert [(r["actor"], r["helpful"], r["comment"], r["lang"], r["ok"]) for r in rows] == [
        ("admin:zz-1", False, "Step two names an old menu.", "vi", True), ("admin:zz-2", True, None, "en", True)]


@pytest.mark.parametrize("repo,path,lang,comment", [
    ("bad repo", "docs/zz.md", "en", None),
    ("AA-Ecosys", "docs/zz.txt", "en", None),
    ("AA-Ecosys", ".md", "en", None),
    ("AA-Ecosys", "docs/zz.md", "eng", None),
    ("AA-Ecosys", "docs/zz.md", "en", ""),
    ("AA-Ecosys", "docs/zz.md", "en", "x" * 1001),
])
@pytest.mark.asyncio
async def test_454_checks_refuse(conn, repo, path, lang, comment):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with conn.transaction():
            await conn.execute(UPSERT, repo, path, lang, False, comment, "admin:zz-3")


@pytest.mark.asyncio
async def test_454_cis_app_reads_inserts_updates(conn):
    privs = {p: await conn.fetchval("SELECT has_table_privilege('cis_app', 'acp_shared.docs_feedback', $1) AS ok", p)
             for p in ("SELECT", "INSERT", "UPDATE")}
    assert privs == {"SELECT": True, "INSERT": True, "UPDATE": True}


@pytest.mark.asyncio
async def test_454_rerun_is_a_no_op(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM pg_indexes WHERE schemaname = 'acp_shared' "
                                 "AND tablename = 'docs_feedback'")
    await conn.execute(_body(M454))
    after = await conn.fetchval("SELECT count(*) AS n FROM pg_indexes WHERE schemaname = 'acp_shared' "
                                "AND tablename = 'docs_feedback'")
    assert before == after == 3
