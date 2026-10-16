"""AA-859 W5 (PR-D5a) — T2 version reads on the tenant pool against real Postgres, as aa_app_user under the
live policies (229 on tenant_tour_versions, 239 master read on published_tours / raw_tours, 241: no
privilege on shared.job).

  - GET /v1/tours/my-versions and /versions/{id}: tenant A gets the same answer on the tenant pool as on
    the admin pool, job status included (read on MAIN by job id); a forgotten WHERE gives tenant B nothing
    of A's on the tenant pool while the admin pool leaks it (so each check can fail); A's version id is a
    404 for B.
  - GET /v1/tours/pool: master tours are listed on the tenant pool; `already_rewritten` is the caller's own,
    even with its tenant condition forgotten.
  - The premise of the split: aa_app_user cannot read shared.job, so the joined (flag-off) SQL on the tenant
    pool fails with 42501.

Same harness as test_aa817_tenant_pool_routes.py: the "tenant pool" is the test connection with SET LOCAL
ROLE aa_app_user; everything runs in one transaction that is rolled back. The CI DB has no
tenant_tour_versions (migration 001 is skipped; conftest's stand-in lacks version_number / job_id), so the
fixture creates a live-shape stand-in (columns and UNIQUE of db-schema-reference.md) and re-applies 229.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402
from api.routers import v1_tours  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"

# Live columns of gold_aa_internal.tenant_tour_versions (db-schema-reference.md), FK-free.
TTV_LIVE_STANDIN_DDL = """
    CREATE TABLE gold_aa_internal.tenant_tour_versions (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id         uuid NOT NULL,
        published_tour_id uuid NOT NULL,
        version_number    integer NOT NULL,
        parent_version_id uuid,
        rewritten_content jsonb NOT NULL DEFAULT '{}'::jsonb,
        status            text NOT NULL DEFAULT 'pending',
        quality_score     numeric(4,2),
        edit_source       text NOT NULL DEFAULT 'ai_generated',
        rewrite_language  text NOT NULL DEFAULT 'en-US',
        seo_mode          text NOT NULL DEFAULT 'standard',
        edited_at         timestamptz,
        edited_by_user    text,
        created_at        timestamptz NOT NULL DEFAULT now(),
        qa_status         text NOT NULL DEFAULT 'pending',
        qa_repair_count   smallint NOT NULL DEFAULT 0,
        qa_checked_at     timestamptz,
        qa_auto_passed    boolean NOT NULL DEFAULT false,
        job_id            uuid,
        UNIQUE (tenant_id, published_tour_id, version_number)
    )
"""


class _Redis:
    def __init__(self, on):
        self.on = on

    async def get(self, key):
        return "true" if self.on else None


class _OneConnPool:
    """pool.acquire() over the test connection; as_role switches to the tenant role for the block."""

    def __init__(self, conn, as_role=None):
        self.conn, self.as_role = conn, as_role

    @asynccontextmanager
    async def acquire(self, timeout=None):
        if self.as_role:
            await self.conn.execute(f"SET LOCAL ROLE {self.as_role}")
        try:
            yield self.conn
        finally:
            if self.as_role:
                await self.conn.execute("RESET ROLE")


def _request(conn, flag_on):
    state = SimpleNamespace(pool=_OneConnPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE),
                            redis=_Redis(flag_on))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _tenant(conn):
    tid = uuid.uuid4()
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                       tid, f"AA-859 d5 {tid.hex[:8]}", f"aa859d5-{tid.hex[:8]}")
    return str(tid)


async def _master_tour(conn, name):
    tour_id = uuid.uuid4()
    await conn.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, "
                       "duration) VALUES ($1, $2, $3, $4, 'Vietnam', '3 days')", tour_id, MASTER, BATCH_ID, name)
    return await conn.fetchval(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
        "aa_summary, aa_highlights, master_status) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, 'active') "
        "RETURNING id", tour_id, uuid.uuid4(), MASTER, name)


async def _version(conn, tid, pt_id, number, job_status=None):
    job_id = None
    if job_status:
        job_id = await conn.fetchval("INSERT INTO shared.job (kind, status) VALUES ('t2_rewrite', $1) RETURNING id",
                                     job_status)
    return await conn.fetchval(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, version_number, "
        "rewritten_content, status, job_id) VALUES ($1::uuid, $2, $3, $4::jsonb, 'ai_generated', $5) RETURNING id",
        tid, pt_id, number, json.dumps({"name": f"v{number} of {tid}"}), job_id)


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("DROP TABLE IF EXISTS gold_aa_internal.tenant_tour_versions")
        await conn.execute(TTV_LIVE_STANDIN_DDL)
        await conn.execute(migration_body(229))
        name = f"AA859-D5 {uuid.uuid4().hex[:8]}"
        a, b, c = await _tenant(conn), await _tenant(conn), await _tenant(conn)
        pt = await _master_tour(conn, name)
        va1 = await _version(conn, a, pt, 1, job_status="succeeded")
        va2 = await _version(conn, a, pt, 2)
        vb1 = await _version(conn, b, pt, 1, job_status="failed")
        yield SimpleNamespace(conn=conn, a=a, b=b, c=c, pt=pt, name=name, va1=va1, va2=va2, vb1=vb1)
    finally:
        await tr.rollback()
        await conn.close()


async def _my_versions(c, tid, flag_on):
    return await v1_tours.list_my_versions(_request(c.conn, flag_on), {"sub": tid}, page=1, page_size=100,
                                           status=None, country=None)


async def _get_version(c, tid, version_id, flag_on):
    return await v1_tours.get_version(str(version_id), _request(c.conn, flag_on), {"sub": tid})


async def _browse(c, tid, flag_on):
    return await v1_tours.browse_pool(_request(c.conn, flag_on), {"sub": tid}, page=1, page_size=20,
                                      country=None, search=c.name, duration_min=None, duration_max=None,
                                      sort="newest")


def _forget(monkeypatch, name, old, new):
    value = getattr(v1_tours, name)
    assert old in value
    monkeypatch.setattr(v1_tours, name, value.replace(old, new))


# ── my-versions ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_my_versions_on_tenant_pool_matches_admin_pool_with_job_status(ctx):
    on = await _my_versions(ctx, ctx.a, flag_on=True)
    off = await _my_versions(ctx, ctx.a, flag_on=False)
    assert on == off
    assert {d["id"] for d in on["data"]} == {ctx.va1, ctx.va2} and on["pagination"]["total"] == 2
    assert {d["id"]: d["job_status"] for d in on["data"]} == {ctx.va1: "succeeded", ctx.va2: None}


@pytest.mark.asyncio
async def test_my_versions_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    _forget(monkeypatch, "_MY_VERSIONS_TENANT_COND", "ttv.tenant_id = $1::uuid", "$1::uuid IS NOT NULL")
    on = await _my_versions(ctx, ctx.b, flag_on=True)
    assert {d["id"] for d in on["data"]} == {ctx.vb1}, "tenant B saw tenant A's versions on the tenant pool"
    assert on["pagination"]["total"] == 1
    leaked = await _my_versions(ctx, ctx.b, flag_on=False)
    assert {ctx.va1, ctx.va2} <= {d["id"] for d in leaked["data"]}, "sanity: the admin pool is not filtered"


# ── versions/{id} ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_version_on_tenant_pool_matches_admin_pool(ctx):
    on = await _get_version(ctx, ctx.a, ctx.va1, flag_on=True)
    off = await _get_version(ctx, ctx.a, ctx.va1, flag_on=False)
    assert on == off
    assert on["job_status"] == "succeeded"
    assert [h["id"] for h in on["version_history"]] == [ctx.va1, ctx.va2]


@pytest.mark.asyncio
async def test_foreign_version_id_is_404(ctx):
    with pytest.raises(HTTPException) as exc:
        await _get_version(ctx, ctx.b, ctx.va1, flag_on=True)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_version_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    _forget(monkeypatch, "_VERSION_TENANT_COND", "ttv.tenant_id = $2::uuid", "$2::uuid IS NOT NULL")
    with pytest.raises(HTTPException) as exc:
        await _get_version(ctx, ctx.b, ctx.va1, flag_on=True)
    assert exc.value.status_code == 404, "tenant B read tenant A's version on the tenant pool"
    leaked = await _get_version(ctx, ctx.b, ctx.va1, flag_on=False)
    assert leaked["id"] == ctx.va1, "sanity: the admin pool is not filtered"


@pytest.mark.asyncio
async def test_version_history_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    _forget(monkeypatch, "_VERSION_HISTORY_TENANT_COND", "tenant_id = $1::uuid", "$1::uuid IS NOT NULL")
    on = await _get_version(ctx, ctx.a, ctx.va1, flag_on=True)
    assert [h["id"] for h in on["version_history"]] == [ctx.va1, ctx.va2]
    leaked = await _get_version(ctx, ctx.a, ctx.va1, flag_on=False)
    assert ctx.vb1 in {h["id"] for h in leaked["version_history"]}, "sanity: the admin pool is not filtered"


# ── pool browse ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pool_on_tenant_pool_lists_master_tours_with_own_rewrite_state(ctx):
    on = await _browse(ctx, ctx.a, flag_on=True)
    assert on == await _browse(ctx, ctx.a, flag_on=False)
    assert [(d["id"], d["already_rewritten"]) for d in on["data"]] == [(ctx.pt, True)]
    assert "Vietnam" in on["countries"]
    fresh = await _browse(ctx, ctx.c, flag_on=True)
    assert [(d["id"], d["already_rewritten"]) for d in fresh["data"]] == [(ctx.pt, False)]


@pytest.mark.asyncio
async def test_pool_rewritten_flag_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    _forget(monkeypatch, "_POOL_REWRITTEN_TENANT_COND", "ttv.tenant_id = ${tid}::uuid", "${tid}::uuid IS NOT NULL")
    on = await _browse(ctx, ctx.c, flag_on=True)
    assert on["data"][0]["already_rewritten"] is False, "tenant C saw A's / B's rewrites on the tenant pool"
    leaked = await _browse(ctx, ctx.c, flag_on=False)
    assert leaked["data"][0]["already_rewritten"] is True, "sanity: the admin pool is not filtered"


# ── premise: shared.job is not readable on the tenant pool ────────────────────

@pytest.mark.asyncio
async def test_joined_job_status_sql_fails_on_the_tenant_pool(ctx, monkeypatch):
    """Mutation check for the split: the flag-off SQL (LEFT JOIN shared.job) on the tenant pool is a 42501
    after 241, so the tests above prove the job status really comes from the MAIN read."""
    monkeypatch.setattr(v1_tours, "_MY_VERSIONS_JOB_SPLIT", v1_tours._MY_VERSIONS_JOB_JOINED)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _my_versions(ctx, ctx.a, flag_on=True)
