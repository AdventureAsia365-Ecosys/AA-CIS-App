"""AA-1047 (Quality W1, root ADR 0020) — CIS 386 acp_shared.fact_sheet + fs_id, on real Postgres (conftest replays
every migration).

The table, its CHECKs (workflow / builder values, sha shape, jsonb shapes, the 1 MiB text cap), the one row per
(tenant, workflow, fs_sha), the pii:free_text tags, RLS (ENABLE + FORCE, the X1 parity policies and tenant_isolation),
cis_app's grants (SELECT, INSERT, DELETE; never UPDATE), the fs_id columns (generated_content always; tenant_tour_versions
when the table exists: the CI replay has no CIS 001, so the stand-in is used inside a rolled-back transaction), the
registration, and that re-applying the file is a no-op that narrows the Dev default privileges.

Invented values only; every row of this module has tenant TENANT and is deleted at the end.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TTV_STANDIN_DDL  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = "00000000-0000-4000-8000-000000a10470"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "386_fact_sheet.sql")
TEXT_CAP_BYTES = 1048576
INSERT = ("INSERT INTO acp_shared.fact_sheet (tenant_id, workflow, builder, schema_version, builder_version, fs_sha, "
          "line_count, lines, render_text) VALUES ($1::uuid, $2, $3, 'factsheet/1', '1', $4, 1, $5::jsonb, $6) "
          "RETURNING fs_id")


def _migration() -> str:
    with open(MIGRATION, encoding="utf-8") as f:
        return f.read()


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    await c.execute(_migration())           # idempotent: a test before this one may have dropped the table
    await c.execute("DELETE FROM acp_shared.fact_sheet WHERE tenant_id = $1::uuid", TENANT)
    yield c
    await c.execute("DELETE FROM acp_shared.fact_sheet WHERE tenant_id = $1::uuid", TENANT)
    await c.close()


@pytest.mark.asyncio
async def test_386_table_checks_and_unique_sha(conn):
    assert await conn.fetchval("SELECT version FROM shared.schema_versions WHERE version = '386'") == "386"
    sha = "a" * 64
    fs_id = await conn.fetchval(INSERT, TENANT, "t9", "route", sha, '[{"id": "S1"}]', "Lumen Gorge walk")
    assert isinstance(fs_id, uuid.UUID)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await conn.fetchval(INSERT, TENANT, "t9", "route", sha, "[]", "again")
    # the same sha under another workflow is another row
    assert await conn.fetchval(INSERT, TENANT, "t2", "tour", sha, "[]", "x")
    for bad in (("t5", "route", "b" * 64, "[]", "x"),          # workflow
                ("t9", "page", "b" * 64, "[]", "x"),           # builder
                ("t9", "route", "not-a-sha", "[]", "x"),       # sha shape
                ("t9", "route", "c" * 64, "{}", "x"),          # lines must be an array
                ("t9", "route", "d" * 64, "[]", "x" * (TEXT_CAP_BYTES + 1))):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.fetchval(INSERT, TENANT, *bad)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):    # the cap counts bytes, not characters
        await conn.fetchval(INSERT, TENANT, "t9", "route", "e" * 64, "[]", "é" * (TEXT_CAP_BYTES // 2 + 1))
    row = await conn.fetchrow("SELECT excluded, pointer_failures, meta, excluded_count, pointer_failure_count "
                              "FROM acp_shared.fact_sheet WHERE fs_id = $1", fs_id)
    assert (row["excluded"], row["pointer_failures"], row["meta"]) == ("[]", "[]", "{}")
    assert (row["excluded_count"], row["pointer_failure_count"]) == (0, 0)
    await conn.execute(_migration())          # re-apply: no-op
    assert await conn.fetchval("SELECT count(*) AS n FROM acp_shared.fact_sheet WHERE tenant_id = $1::uuid",
                               TENANT) == 2


@pytest.mark.asyncio
async def test_386_tags_rls_and_policies(conn):
    tags = dict(await conn.fetch(
        "SELECT a.attname AS col, col_description('acp_shared.fact_sheet'::regclass, a.attnum) AS tag "
        "FROM pg_attribute a WHERE a.attrelid = 'acp_shared.fact_sheet'::regclass AND a.attnum > 0"))
    assert tags["lines"].startswith("pii:free_text;") and tags["render_text"].startswith("pii:free_text;")
    rls = await conn.fetchrow("SELECT relrowsecurity AS on_, relforcerowsecurity AS force FROM pg_class "
                              "WHERE oid = 'acp_shared.fact_sheet'::regclass")
    assert rls["on_"] and rls["force"]
    policies = {r["policyname"]: r for r in await conn.fetch(
        "SELECT policyname, roles::text[] AS roles, qual FROM pg_policies "
        "WHERE schemaname = 'acp_shared' AND tablename = 'fact_sheet'")}
    assert set(policies) == {"fact_sheet_cis_app_all", "fact_sheet_cis_owner_all", "tenant_isolation"}
    assert policies["tenant_isolation"]["roles"] == ["aa_app_user"]
    assert "NULLIF" in policies["tenant_isolation"]["qual"]


@pytest.mark.asyncio
async def test_386_cis_app_grants_and_the_dev_default_privileges(conn):
    """On Dev Migration Apply creates the table as cis_owner, whose default privileges in acp_shared give cis_app every
    privilege on a new table. Simulate them (and a PUBLIC / tenant pool grant), re-run the file in a rolled-back
    transaction: cis_app keeps SELECT, INSERT, DELETE; the others keep nothing."""
    n = await conn.fetchval("SELECT count(*) AS n FROM pg_roles WHERE rolname IN ('cis_app', 'aa_app_user', "
                            "'catalog_owner', 'cis_owner')")
    if n != 4:
        pytest.skip("the runtime roles are not in this database")
    body = _migration().replace("BEGIN;", "").replace("COMMIT;", "")
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("GRANT ALL ON acp_shared.fact_sheet TO cis_app, aa_app_user, catalog_owner, PUBLIC")
        await conn.execute(body)
        for priv, want in (("SELECT", True), ("INSERT", True), ("DELETE", True), ("UPDATE", False),
                           ("TRUNCATE", False), ("REFERENCES", False), ("TRIGGER", False)):
            got = await conn.fetchval("SELECT has_table_privilege('cis_app', 'acp_shared.fact_sheet', $1) AS ok",
                                      priv)
            assert got is want, priv
        for role in ("aa_app_user", "catalog_owner", "public"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                got = await conn.fetchval("SELECT has_table_privilege($1, 'acp_shared.fact_sheet', $2) AS ok",
                                          role, priv)
                assert got is False, (role, priv)
    finally:
        await tr.rollback()


@pytest.mark.asyncio
async def test_386_fs_id_columns(conn):
    col = await conn.fetchval("SELECT data_type FROM information_schema.columns WHERE table_schema = "
                              "'silver_aa_internal' AND table_name = 'generated_content' AND column_name = 'fs_id'")
    assert col == "uuid"
    present = await conn.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NOT NULL AS ok")
    tr = conn.transaction()
    await tr.start()
    try:
        if not present:
            await conn.execute(TTV_STANDIN_DDL)
        await conn.execute(_migration().replace("BEGIN;", "").replace("COMMIT;", ""))
        ttv = await conn.fetchval("SELECT data_type FROM information_schema.columns WHERE table_schema = "
                                  "'gold_aa_internal' AND table_name = 'tenant_tour_versions' "
                                  "AND column_name = 'fs_id'")
        assert ttv == "uuid"
    finally:
        await tr.rollback()
