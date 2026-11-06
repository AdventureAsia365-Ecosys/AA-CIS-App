"""AA-900 (S271, root ADR 0020 d8) — CIS 387 shared.llm_cassette on real Postgres (migrations applied by conftest).

The table and its CHECKs (256 KiB reply cap at a byte count, withheld = no text, sha shapes), the pii:free_text tag
on reply_text, cis_app's grants (SELECT, INSERT and DELETE for the erasure; never UPDATE), the registration;
re-applying the file is a no-op. The recorder that writes it: test_aa900_cassette_db.py (code PR).

Invented values only; every row of this module has stage adhoc_aa900_mig387 and is deleted at the end.
"""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "adhoc_aa900_mig387"
REPLY_CAP_BYTES = 262144
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "387_llm_cassette.sql")


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    with open(MIGRATION, encoding="utf-8") as f:
        await c.execute(f.read())                  # idempotent: a test before this one may have dropped the table
    await c.execute("DELETE FROM shared.llm_cassette WHERE stage = $1", STAGE)
    yield c
    await c.execute("DELETE FROM shared.llm_cassette WHERE stage = $1", STAGE)
    await c.close()


# ── 387 ────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_387_table_checks_tag_and_grants(conn):
    assert await conn.fetchval("SELECT version FROM shared.schema_versions WHERE version = '387'") == "387"
    tag = await conn.fetchval("SELECT col_description('shared.llm_cassette'::regclass, a.attnum) AS tag "
                              "FROM pg_attribute a WHERE a.attrelid = 'shared.llm_cassette'::regclass "
                              "AND a.attname = 'reply_text'")
    assert tag.startswith("pii:free_text;")
    if await conn.fetchval("SELECT count(*) AS n FROM pg_roles WHERE rolname = 'cis_app'"):
        privs = await conn.fetchrow(
            "SELECT has_table_privilege('cis_app', 'shared.llm_cassette', 'SELECT') AS s, "
            "has_table_privilege('cis_app', 'shared.llm_cassette', 'INSERT') AS i, "
            "has_table_privilege('cis_app', 'shared.llm_cassette', 'UPDATE') AS u, "
            "has_table_privilege('cis_app', 'shared.llm_cassette', 'DELETE') AS d")
        assert (privs["s"], privs["i"], privs["u"], privs["d"]) == (True, True, False, True)
    base = ("INSERT INTO shared.llm_cassette (stage, role, request_sha256, model_key, reply_text, reply_sha256, "
            "reply_bytes, reply_withheld) VALUES ($1, 'writer', repeat('a', 64), 'm', $2, repeat('b', 64), 1, $3)")
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(base, STAGE, "x" * (REPLY_CAP_BYTES + 1), None)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(base, STAGE, "text", "pii_stage")
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(base.replace("repeat('a', 64)", "'not-a-sha'"), STAGE, "text", None)
    await conn.execute(base, STAGE, "x" * REPLY_CAP_BYTES, None)        # exactly at the cap
    with pytest.raises(asyncpg.exceptions.CheckViolationError):         # the cap counts bytes, not characters
        await conn.execute(base, STAGE, "é" * (REPLY_CAP_BYTES // 2 + 1), None)
    await conn.execute(base, STAGE, None, "pii_stage")
    with open(MIGRATION, encoding="utf-8") as f:
        await conn.execute(f.read())          # re-apply: no-op
