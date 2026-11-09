"""AA-1040 — the machine-readable AI mark against real Postgres (content_piece.writer_input -> 'ai_provenance').

- the tenant edit (PATCH, _PIECE_EDIT_SQL, on the aa_app_user tenant pool and the admin pool) adds human_edited_at to
  the stored provenance and keeps every other writer_input key; a piece without writer_input keeps NULL;
- the writer-model lookup (ai_marking.WRITER_MODEL_SQL) picks the newest billed, non-shadow t9_write row of the
  request since the piece row was created;
- the publish read (_PUBLISH_PIECE_SQL) and the tenant read (_FETCH_PIECE_QUERY) return only the provenance key, and
  the WordPress fragment built from that row carries the mark.

Everything runs in one transaction that is rolled back (the pattern of test_aa859_piece_edit_pool.py).
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core.aa544_tenant_pool import PIECE_EDIT_FLAG  # noqa: E402
from api.routers import v1_publish  # noqa: E402
from services.acp_content_writing import ai_marking, service  # noqa: E402
from services.acp_content_writing.export import render_content_text_to_fragment  # noqa: E402
from services.acp_publish.dispatch import piece_ai_mark  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"


class _Redis:
    def __init__(self, on=True):
        self.on = on

    async def get(self, key):
        return "true" if self.on and key == PIECE_EDIT_FLAG else "false"


class _OneConnPool:
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


def _request(conn, flag_on=True):
    state = SimpleNamespace(pool=_OneConnPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE),
                            redis=_Redis(flag_on))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _tenant(conn):
    tid = uuid.uuid4()
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                       tid, f"AA-1040 mark {tid.hex[:8]}", f"aa1040-{tid.hex[:8]}")
    return tid


async def _piece(conn, tid, writer_input):
    req = await conn.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                              "VALUES ($1, 'aa1040-atom', 'blog', 'approved') RETURNING request_id", tid)
    piece = await conn.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, gate_ledger, repair_log, channel, writer_input) "
        "VALUES ($1, $2, 1, '## Lantern Valley' || chr(10) || chr(10) || 'A walk past the salt mill.', 'approved', "
        "'[]'::jsonb, '[]'::jsonb, 'blog', $3::jsonb) RETURNING piece_id",
        tid, req, json.dumps(writer_input) if writer_input is not None else None)
    return req, piece


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("""
            ALTER TABLE acp_shared.content_piece
                ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid,
                ADD COLUMN IF NOT EXISTS channel text,
                ADD COLUMN IF NOT EXISTS seo_title text, ADD COLUMN IF NOT EXISTS meta_description text,
                ADD COLUMN IF NOT EXISTS slug text, ADD COLUMN IF NOT EXISTS route_hub_name text,
                ADD COLUMN IF NOT EXISTS route_segment_count integer,
                ADD COLUMN IF NOT EXISTS flags jsonb,
                ADD COLUMN IF NOT EXISTS writer_input jsonb""")
        a = await _tenant(conn)
        stamped = {"seed": "the salt mill", ai_marking.PROVENANCE_KEY: ai_marking.stamp("writer-model-x")}
        req, piece = await _piece(conn, a, stamped)
        _, bare = await _piece(conn, a, None)
        yield SimpleNamespace(conn=conn, a=a, req=req, piece=piece, bare=bare)
    finally:
        await tr.rollback()
        await conn.close()


async def _writer_input(c, piece_id):
    raw = await c.conn.fetchval("SELECT writer_input FROM acp_shared.content_piece WHERE piece_id = $1", piece_id)
    return json.loads(raw) if raw is not None else None


@pytest.mark.parametrize("flag_on", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.asyncio
async def test_tenant_edit_records_human_edited_at(ctx, flag_on):
    request = _request(ctx.conn, flag_on)
    out = await service.update_piece_content_text(ctx.a, ctx.piece, "Edited by the agency", request.app.state.pool,
                                                  request=request)
    assert out["ai"]["ai_generated"] is True and out["ai"]["human_edited"] is True
    assert out["ai"]["model"] == "writer-model-x"
    wi = await _writer_input(ctx, ctx.piece)
    assert wi["seed"] == "the salt mill", "the writer input itself is kept"
    prov = wi[ai_marking.PROVENANCE_KEY]
    assert prov["model"] == "writer-model-x" and prov["human_edited_at"]


@pytest.mark.asyncio
async def test_edit_of_a_piece_without_writer_input_keeps_null(ctx):
    request = _request(ctx.conn)
    out = await service.update_piece_content_text(ctx.a, ctx.bare, "Edited", request.app.state.pool,
                                                  request=request)
    assert await _writer_input(ctx, ctx.bare) is None
    assert out["ai"]["ai_generated"] is True and out["ai"]["human_edited"] is False and out["ai"]["model"] is None


async def _ledger(c, *, model, minutes, status="ok", shadow=False, stage="t9_write", request_id=None):
    created = await c.conn.fetchval("SELECT created_at FROM acp_shared.content_piece WHERE piece_id = $1", c.piece)
    await c.conn.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, angle_gate_request_id, created_at, "
        "status, is_shadow) VALUES ($1, 'writer', $2, '{}'::jsonb, $3, $4, $5, $6)",
        stage, model, request_id or c.req, created + timedelta(minutes=minutes), status, shadow)


@pytest.mark.asyncio
async def test_writer_model_is_the_newest_billed_writer_call_since_the_piece(ctx):
    await _ledger(ctx, model="old-model", minutes=-5)                     # before the piece row: an earlier piece
    await _ledger(ctx, model="first-attempt", minutes=1)
    await _ledger(ctx, model="final-attempt", minutes=2, status="truncated")
    await _ledger(ctx, model="refused-model", minutes=3, status="error")
    await _ledger(ctx, model="shadow-model", minutes=4, shadow=True)
    await _ledger(ctx, model="judge-model", minutes=5, stage="t10_judge")
    await _ledger(ctx, model="other-request", minutes=6, request_id=uuid.uuid4())
    assert await ai_marking.writer_model(ctx.conn, ctx.req, ctx.piece) == "final-attempt"


@pytest.mark.asyncio
async def test_writer_model_is_none_without_a_ledger_row(ctx):
    assert await ai_marking.writer_model(ctx.conn, ctx.req, ctx.piece) is None
    assert await ai_marking.writer_answer(ctx.conn, ctx.req, ctx.piece) is None


@pytest.mark.asyncio
async def test_writer_answer_reads_the_same_row_with_its_fallback_marks(ctx):
    # AA-900 W0: the answered model and how it answered (fallback rung, connection, effort) from the same ledger row
    await _ledger(ctx, model="first-attempt", minutes=1)
    created = await ctx.conn.fetchval("SELECT created_at FROM acp_shared.content_piece WHERE piece_id = $1",
                                      ctx.piece)
    await ctx.conn.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, angle_gate_request_id, created_at, "
        "status, is_shadow, fallback_used, connection_key, effort) "
        "VALUES ('t9_write', 'writer', 'final-attempt', '{\"route_position\": 1}'::jsonb, $1, $2, 'ok', false, "
        "false, 'conn-b', 'medium')", ctx.req, created + timedelta(minutes=2))
    await _ledger(ctx, model="shadow-model", minutes=3, shadow=True)
    answer = await ai_marking.writer_answer(ctx.conn, ctx.req, ctx.piece)
    assert answer == {"answered_model": "final-attempt", "fallback_used": True, "route_position": 1,
                      "connection_key": "conn-b", "effort": "medium"}
    assert answer["answered_model"] == await ai_marking.writer_model(ctx.conn, ctx.req, ctx.piece)


@pytest.mark.asyncio
async def test_publish_read_carries_the_mark_into_the_wordpress_fragment(ctx):
    sql = v1_publish._PUBLISH_PIECE_SQL.format(tenant_cond=v1_publish._PUBLISH_PIECE_TENANT_COND)
    row = await ctx.conn.fetchrow(sql, ctx.piece, ctx.a)
    assert "writer_input" not in row.keys()
    mark = piece_ai_mark(row)
    assert mark["model"] == "writer-model-x" and mark["ai_generated"] is True
    html = render_content_text_to_fragment(row["content_text"], ai=mark)
    assert html.startswith('<article data-ai-generated="true"') and '<meta name="ai-generated" content="true">' in html
    bare = await ctx.conn.fetchrow(sql, ctx.bare, ctx.a)
    assert bare["ai_provenance"] is None and piece_ai_mark(bare)["generated_at"] == bare["created_at"].isoformat()


@pytest.mark.asyncio
async def test_tenant_read_returns_only_the_provenance_key(ctx):
    row = await ctx.conn.fetchrow(service._FETCH_PIECE_QUERY, ctx.piece, ctx.a)
    assert "writer_input" not in row.keys()
    out = service._tenant_safe_piece(row)
    assert out["ai"]["model"] == "writer-model-x" and out["ai"]["human_edited"] is False
