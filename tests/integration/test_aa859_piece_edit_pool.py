"""AA-859 W2 — PATCH /v1/content-writing/pieces/{id} (service.update_piece_content_text) on the aa_app_user
tenant pool, against real Postgres with the live content_piece policy (229: FOR ALL TO aa_app_user, USING +
WITH CHECK on NULLIF(app.tenant_id)).

The "tenant pool" is the test connection with SET LOCAL ROLE aa_app_user (CI's aa_app_user has no LOGIN), as
in test_aa817_tenant_pool_content.py; acquire_scoped_conn opens its own (nested) transaction and sets
app.tenant_id LOCAL, as on Dev. Everything runs in one transaction that is rolled back.

Forgotten-WHERE check: with `tenant_id = $2` replaced by `$2::uuid IS NOT NULL` in _PIECE_EDIT_SQL, tenant B
still cannot edit tenant A's piece on the tenant pool (RLS), while the admin pool (owner, exempt) does,
which proves the check can fail.
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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core.aa544_tenant_pool import PIECE_EDIT_FLAG  # noqa: E402
from services.acp_content_writing import service  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"


class _Redis:
    def __init__(self, on):
        self.on = on

    async def get(self, key):
        return "true" if self.on and key == PIECE_EDIT_FLAG else "false"


class _OneConnPool:
    """pool.acquire() over the test connection; as_role switches to the tenant role for the block.
    execute() is the MAIN pool's (write_audit_log(pool, ...)), always as the test (owner) role."""

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

    async def execute(self, sql, *args):
        return await self.conn.execute(sql, *args)


def _request(conn, flag_on):
    state = SimpleNamespace(pool=_OneConnPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE),
                            redis=_Redis(flag_on))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _tenant(conn):
    tid = uuid.uuid4()
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                       tid, f"AA-859 edit {tid.hex[:8]}", f"aa859e-{tid.hex[:8]}")
    return tid


async def _piece(conn, tid, status="approved"):
    req = await conn.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                              "VALUES ($1, 'aa859-atom', 'blog', 'approved') RETURNING request_id", tid)
    return await conn.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, gate_ledger, repair_log, channel) VALUES ($1, $2, 1, $3, $4, '[]'::jsonb, '[]'::jsonb, 'blog') "
        "RETURNING piece_id", tid, req, f"Body of {tid}", status)


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        # columns the RETURNING list reads that the CI DB may lack (124 needs pgvector, absent here)
        await conn.execute("""
            ALTER TABLE acp_shared.content_piece
                ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid,
                ADD COLUMN IF NOT EXISTS channel text,
                ADD COLUMN IF NOT EXISTS seo_title text, ADD COLUMN IF NOT EXISTS meta_description text,
                ADD COLUMN IF NOT EXISTS slug text, ADD COLUMN IF NOT EXISTS route_hub_name text,
                ADD COLUMN IF NOT EXISTS route_segment_count integer,
                ADD COLUMN IF NOT EXISTS flags jsonb""")
        a, b = await _tenant(conn), await _tenant(conn)
        piece_a, held_a = await _piece(conn, a), await _piece(conn, a, status="held")
        processing_a = await _piece(conn, a, status="processing")
        yield SimpleNamespace(conn=conn, a=a, b=b, piece_a=piece_a, held_a=held_a, processing_a=processing_a)
    finally:
        await tr.rollback()
        await conn.close()


async def _edit(c, tenant_id, piece_id, text, flag_on):
    request = _request(c.conn, flag_on)  # the router passes request.app.state.pool as `pool`
    return await service.update_piece_content_text(tenant_id, piece_id, text, request.app.state.pool,
                                                   request=request)


async def _text(c, piece_id):
    return await c.conn.fetchval("SELECT content_text FROM acp_shared.content_piece WHERE piece_id = $1", piece_id)


async def _audit_rows(c, piece_id):
    rows = await c.conn.fetch("SELECT tenant_id, actor, action, details FROM acp_shared.audit_log "
                              "WHERE resource_type = 'content_piece' AND resource_id = $1", str(piece_id))
    return [dict(r) for r in rows]


@pytest.mark.asyncio
async def test_tenant_policy_and_grant_are_what_the_route_relies_on(ctx):
    """Guards the precondition: content_piece has RLS and aa_app_user may UPDATE it (else the flag-on edit
    would be a 42501, which the matrix maps to a 500, not a silent pass)."""
    rls = await ctx.conn.fetchval(
        "SELECT relrowsecurity FROM pg_class WHERE oid = 'acp_shared.content_piece'::regclass")
    assert rls is True
    assert await ctx.conn.fetchval("SELECT has_table_privilege($1, 'acp_shared.content_piece', 'UPDATE')", TENANT_ROLE)
    assert not await ctx.conn.fetchval("SELECT has_table_privilege($1, 'acp_shared.audit_log', 'INSERT')",
                                       TENANT_ROLE), "audit_log stays MAIN-only (D2 exception)"


@pytest.mark.parametrize("which", ["piece_a", "held_a"])
@pytest.mark.asyncio
async def test_owner_edits_approved_and_held_pieces_on_the_tenant_pool(ctx, which):
    piece_id = getattr(ctx, which)
    out = await _edit(ctx, ctx.a, piece_id, "Edited on the tenant pool", flag_on=True)
    assert out["content_text"] == "Edited on the tenant pool" and out["ready_state"] == "ready"
    assert await _text(ctx, piece_id) == "Edited on the tenant pool"
    audits = await _audit_rows(ctx, piece_id)
    assert len(audits) == 1 and audits[0]["action"] == "content_piece.edited"
    assert audits[0]["tenant_id"] == str(ctx.a) and audits[0]["actor"] == f"tenant:{ctx.a}"
    details = json.loads(audits[0]["details"])
    # AA-916 WM-04: the audit row names the writing signal and holds hashes, never the text
    assert details["content_length"] == len("Edited on the tenant pool")
    assert set(details) == {"content_length", "signal_id", "before_sha256", "after_sha256"}
    sig = await ctx.conn.fetchrow("SELECT writer, scope, tenant_id, before_text, after_text, actor_type, action, "
                                  "before_sha256 FROM shared.writing_signal WHERE signal_id = $1::uuid",
                                  details["signal_id"])
    assert dict(sig) == {"writer": "t9_blog", "scope": "tenant", "tenant_id": ctx.a, "before_text": f"Body of {ctx.a}",
                         "after_text": "Edited on the tenant pool", "actor_type": "tenant", "action": "tenant_edit",
                         "before_sha256": details["before_sha256"]}


async def _signals(c, piece_id):
    return await c.conn.fetch("SELECT tenant_id, before_text, after_text FROM shared.writing_signal "
                              "WHERE source_table = 'content_piece' AND source_id = $1", str(piece_id))


@pytest.mark.asyncio
async def test_signal_is_written_by_the_tenant_role_and_stays_in_its_tenant(ctx):
    """AA-916 WM-04: the insert runs on the tenant pool (aa_app_user, RLS WITH CHECK own tenant); tenant B's
    tenant-pool read sees none of A's signals; an unchanged text writes no signal."""
    await _edit(ctx, ctx.a, ctx.piece_a, "A's new text", flag_on=True)
    await _edit(ctx, ctx.a, ctx.piece_a, "A's new text", flag_on=True)              # unchanged: no 2nd signal
    rows = await _signals(ctx, ctx.piece_a)
    assert [(r["tenant_id"], r["before_text"], r["after_text"]) for r in rows] == [
        (ctx.a, f"Body of {ctx.a}", "A's new text")]
    for tenant, seen in ((ctx.b, 0), (ctx.a, 1)):
        async with ctx.conn.transaction():
            await ctx.conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
            await ctx.conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            n = await ctx.conn.fetchval("SELECT count(*) FROM shared.writing_signal WHERE source_id = $1",
                                        str(ctx.piece_a))                           # no tenant filter on purpose
            await ctx.conn.execute("RESET ROLE")
        assert n == seen, tenant


@pytest.mark.asyncio
async def test_a_foreign_tenant_edit_writes_no_signal(ctx):
    for flag_on in (True, False):
        with pytest.raises(service.ContentWritingError):
            await _edit(ctx, ctx.b, ctx.piece_a, "B was here", flag_on=flag_on)
    assert await _signals(ctx, ctx.piece_a) == []


@pytest.mark.asyncio
async def test_both_pools_give_the_same_answer_for_the_owner(ctx):
    on = await _edit(ctx, ctx.a, ctx.piece_a, "same text", flag_on=True)
    off = await _edit(ctx, ctx.a, ctx.piece_a, "same text", flag_on=False)
    assert on == off
    assert len(await _audit_rows(ctx, ctx.piece_a)) == 2


@pytest.mark.parametrize("flag_on", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.asyncio
async def test_foreign_tenant_cannot_edit(ctx, flag_on):
    with pytest.raises(service.ContentWritingError):
        await _edit(ctx, ctx.b, ctx.piece_a, "B was here", flag_on=flag_on)
    assert await _text(ctx, ctx.piece_a) == f"Body of {ctx.a}"
    assert await _audit_rows(ctx, ctx.piece_a) == []


@pytest.mark.parametrize("flag_on", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.asyncio
async def test_processing_piece_is_not_editable(ctx, flag_on):
    with pytest.raises(service.ContentWritingError):
        await _edit(ctx, ctx.a, ctx.processing_a, "too early", flag_on=flag_on)
    assert await _text(ctx, ctx.processing_a) == f"Body of {ctx.a}"


@pytest.mark.asyncio
async def test_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    forgotten = service._PIECE_EDIT_SQL.replace("tenant_id = $2", "$2::uuid IS NOT NULL")
    assert forgotten != service._PIECE_EDIT_SQL
    monkeypatch.setattr(service, "_PIECE_EDIT_SQL", forgotten)
    with pytest.raises(service.ContentWritingError):
        await _edit(ctx, ctx.b, ctx.piece_a, "B was here", flag_on=True)
    assert await _text(ctx, ctx.piece_a) == f"Body of {ctx.a}", "tenant pool let B edit A's piece"
    leaked = await _edit(ctx, ctx.b, ctx.piece_a, "B was here", flag_on=False)
    assert leaked["content_text"] == "B was here", "sanity: the admin pool is not RLS-filtered"
