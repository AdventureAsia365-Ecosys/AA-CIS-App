"""AA-1050 (Quality W4, root ADR 0020 D7) — CIS 391 shared.claim_shadow_verdict on real Postgres (conftest replays
every migration).

The table, its CHECKs (run, surface and object id, sha and instrument shapes, the typed reason that starts with its
verdict, the cached row that bills nothing, the details cap), one row per (run kind, run, cache key), RLS (ENABLE +
FORCE, the X1 parity policies and tenant_isolation), cis_app's grants (SELECT, INSERT, DELETE; never UPDATE), the
registration, and a re-apply that changes nothing. The shadow's store path is test_aa1050_claim_shadow_store.py
(the code PR).

Invented values only; every row of this module has tenant TENANT or run id prefix zz-aa1050 and is deleted at the end.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = "00000000-0000-4000-8000-0000000a1070"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "391_claim_shadow_verdict.sql")
COLUMNS = ("run_kind", "run_id", "surface", "tenant_id", "piece_id", "version_id", "content_sha256", "cache_key",
           "instrument_id", "cached", "verdict", "reason", "shadow_action", "baseline", "agreement", "pinned",
           "calls", "cost_usd", "details")


def _migration() -> str:
    with open(MIGRATION, encoding="utf-8") as f:
        return f.read()


def _row(**over) -> dict:
    row = {"run_kind": "live", "run_id": f"zz-aa1050-{uuid.uuid4().hex[:8]}", "surface": "t9_blog",
           "tenant_id": TENANT, "piece_id": str(uuid.uuid4()), "version_id": None, "content_sha256": "a" * 64,
           "cache_key": "b" * 64, "instrument_id": "gpt-6-luna-openai@openai-direct:extraction", "cached": False,
           "verdict": "unsupported", "reason": "unsupported:fact.inclusion", "shadow_action": "warn",
           "baseline": "pass", "agreement": "disagree", "pinned": True, "calls": 3, "cost_usd": 0.004,
           "details": "{}"}
    row.update(over)
    return row


async def _insert(conn, **over):
    row = _row(**over)
    casts = {"tenant_id": "::uuid", "piece_id": "::uuid", "version_id": "::uuid", "details": "::jsonb"}
    args = ", ".join(f"${i + 1}{casts.get(c, '')}" for i, c in enumerate(COLUMNS))
    return await conn.fetchval(
        f"INSERT INTO shared.claim_shadow_verdict ({', '.join(COLUMNS)}) VALUES ({args}) RETURNING verdict_id",
        *[row[c] for c in COLUMNS])


async def _clean(c):
    await c.execute("DELETE FROM shared.claim_shadow_verdict WHERE tenant_id = $1::uuid OR run_id LIKE 'zz-aa1050%'",
                    TENANT)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    await c.execute(_migration())           # idempotent: a test before this one may have dropped the table
    await _clean(c)
    yield c
    await _clean(c)
    await c.close()


@pytest.mark.asyncio
async def test_391_registered_and_one_row_per_run_and_key(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '391'") == "391"
    first = await _insert(conn, run_id="zz-aa1050-run")
    assert isinstance(first, uuid.UUID)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await _insert(conn, run_id="zz-aa1050-run")
    assert await _insert(conn, run_id="zz-aa1050-run", run_kind="replay")
    assert await _insert(conn, run_id="zz-aa1050-run", cache_key="c" * 64)
    t2 = await _insert(conn, surface="t2", piece_id=None, version_id=str(uuid.uuid4()), baseline="flag",
                       agreement="agree", verdict="unverified", reason="unverified:miss", shadow_action="none",
                       pinned=False, calls=0, cost_usd=0)
    assert t2


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {"run_kind": "nightly"}, {"run_id": "bad id"}, {"surface": "t7"},
    {"piece_id": None},                                                   # a T9 row needs its piece
    {"version_id": "00000000-0000-4000-8000-0000000a1071"},               # ... and no version
    {"content_sha256": "x" * 64}, {"cache_key": "B" * 64}, {"instrument_id": "gpt-6@openai-direct:vote"},
    {"verdict": "pass", "reason": "pass:clear"},                          # a miss is never a pass: no such verdict
    {"reason": "supported:clear"},                                        # the reason starts with its verdict
    {"reason": "unsupported:Fact Inclusion"}, {"shadow_action": "cut"}, {"baseline": "held"},
    {"agreement": "maybe"}, {"cached": True},                              # a cached row bills nothing
    {"calls": -1}, {"details": "[]"},
    {"details": json.dumps({"x": "y" * 70000})},
])
async def test_391_checks_refuse(conn, over):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _insert(conn, **over)


@pytest.mark.asyncio
async def test_391_rls_policies_and_grants(conn):
    flags = await conn.fetchrow(
        "SELECT relrowsecurity AS rls, relforcerowsecurity AS force FROM pg_class "
        "WHERE oid = 'shared.claim_shadow_verdict'::regclass")
    assert flags["rls"] and flags["force"]
    policies = {r["polname"]: r["roles"] for r in await conn.fetch(
        "SELECT policyname AS polname, roles::text[] AS roles FROM pg_policies "
        "WHERE schemaname = 'shared' AND tablename = 'claim_shadow_verdict'")}
    assert policies == {"claim_shadow_verdict_cis_app_all": ["cis_app"],
                        "claim_shadow_verdict_cis_owner_all": ["cis_owner"], "tenant_isolation": ["aa_app_user"]}
    privs = {r["p"] for r in await conn.fetch(
        "SELECT privilege_type AS p FROM information_schema.role_table_grants "
        "WHERE table_schema = 'shared' AND table_name = 'claim_shadow_verdict' AND grantee = 'cis_app'")}
    assert privs == {"SELECT", "INSERT", "DELETE"}
    assert not await conn.fetchval(
        "SELECT count(*) AS ok FROM information_schema.role_table_grants WHERE table_schema = 'shared' "
        "AND table_name = 'claim_shadow_verdict' AND grantee IN ('aa_app_user', 'catalog_owner', 'PUBLIC')")
    await conn.execute(_migration())                                          # re-apply: no error, nothing changes
    assert await conn.fetchval("SELECT count(*) AS ok FROM shared.schema_versions WHERE version = '391'") == 1
