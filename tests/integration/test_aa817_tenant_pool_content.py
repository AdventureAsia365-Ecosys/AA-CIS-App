"""AA-817 — GET /v1/content-writing/requests/{id}/latest-piece on the aa_app_user tenant pool, against
real Postgres, under the policies before migration 229 (113/115: TO public, text compare) and after it
(TO aa_app_user, NULLIF, WITH CHECK): the code must work on Dev before and after 229 is applied.

The "tenant pool" here is the test connection with SET LOCAL ROLE aa_app_user (CI's aa_app_user has
no LOGIN); acquire_scoped_conn opens its own (nested) transaction and sets app.tenant_id LOCAL, as on
Dev. Everything runs in one transaction that is rolled back.

The strongest check simulates a forgotten `WHERE tenant_id`: with the tenant filters stripped from
both queries, tenant B still gets nothing of tenant A's on the tenant pool (RLS holds the boundary),
while the admin pool (owner, exempt) leaks it, which proves the check can fail.
"""
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core.aa544_tenant_pool import CONTENT_PIECE_READS_FLAG  # noqa: E402
from services.acp_content_writing import service  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"
PRE_229_POLICY = "USING (tenant_id::text = current_setting('app.tenant_id', true))"


class _Redis:
    def __init__(self, on):
        self.on = on

    async def get(self, key):
        return "true" if self.on and key == CONTENT_PIECE_READS_FLAG else None


class _OneConnPool:
    """pool.acquire() over the test connection; as_role switches to the tenant role for the block."""

    def __init__(self, conn, as_role=None):
        self.conn, self.as_role = conn, as_role

    @asynccontextmanager
    async def acquire(self):
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
                       tid, f"AA-817 pool {tid.hex[:8]}", f"aa817p-{tid.hex[:8]}")
    return tid


async def _request_with_piece(conn, tid):
    req = await conn.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                              "VALUES ($1, 'aa817-atom', 'blog', 'approved') RETURNING request_id", tid)
    opt = await conn.fetchval("INSERT INTO acp_shared.angle_gate_option (request_id, idx, name, why_it_works, "
                              "formula_fit, best_final_style, chosen) VALUES ($1, 0, 'n', 'w', 'f', 's', true) "
                              "RETURNING option_id", req)
    await conn.execute("INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, angle_gate_option_id, "
                       "attempt_number, content_text, status, gate_ledger, repair_log) "
                       "VALUES ($1, $2, $3, 1, $4, 'approved', '[]'::jsonb, '[]'::jsonb)",
                       tid, req, opt, f"Body of {tid}")
    return req


@pytest_asyncio.fixture(params=["before_229", "after_229"])
async def ctx(request):
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        # columns the latest-piece SELECT reads that the CI DB may lack (124 needs pgvector, absent here)
        await conn.execute("""
            ALTER TABLE acp_shared.content_piece
                ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid,
                ADD COLUMN IF NOT EXISTS seo_title text, ADD COLUMN IF NOT EXISTS meta_description text,
                ADD COLUMN IF NOT EXISTS slug text, ADD COLUMN IF NOT EXISTS route_hub_name text,
                ADD COLUMN IF NOT EXISTS route_segment_count integer,
                ADD COLUMN IF NOT EXISTS flags jsonb""")
        if request.param == "before_229":
            for table in ("acp_shared.content_piece", "acp_shared.angle_gate_request"):
                await conn.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
                await conn.execute(f"CREATE POLICY tenant_isolation ON {table} {PRE_229_POLICY}")
        a, b = await _tenant(conn), await _tenant(conn)
        req_a = await _request_with_piece(conn, a)
        await _request_with_piece(conn, b)
        yield SimpleNamespace(conn=conn, a=a, b=b, req_a=req_a)
    finally:
        await tr.rollback()
        await conn.close()


async def _latest(c, tenant_id, request_id, flag_on):
    return await service.fetch_latest_piece_for_request(tenant_id, request_id, None,
                                                        request=_request(c.conn, flag_on))


@pytest.mark.asyncio
async def test_owner_tenant_gets_same_piece_on_both_pools(ctx):
    on = await _latest(ctx, ctx.a, ctx.req_a, flag_on=True)
    off = await _latest(ctx, ctx.a, ctx.req_a, flag_on=False)
    assert on is not None and on == off
    assert on["content_text"] == f"Body of {ctx.a}"


@pytest.mark.asyncio
async def test_foreign_tenant_gets_nothing(ctx):
    assert await _latest(ctx, ctx.b, ctx.req_a, flag_on=True) is None
    assert await _latest(ctx, ctx.b, ctx.req_a, flag_on=False) is None


@pytest.mark.asyncio
async def test_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    monkeypatch.setattr(service, "_CHOSEN_OPTION_QUERY",
                        service._CHOSEN_OPTION_QUERY.replace("r.tenant_id = $2", "$2::uuid IS NOT NULL"))
    monkeypatch.setattr(service, "_LATEST_PIECE_FOR_OPTION_QUERY",
                        service._LATEST_PIECE_FOR_OPTION_QUERY.replace("tenant_id = $2", "$2::uuid IS NOT NULL"))
    assert await _latest(ctx, ctx.b, ctx.req_a, flag_on=True) is None, "tenant pool leaked another tenant's piece"
    leaked = await _latest(ctx, ctx.b, ctx.req_a, flag_on=False)
    assert leaked and leaked["content_text"] == f"Body of {ctx.a}", "sanity: the admin pool is not RLS-filtered"
