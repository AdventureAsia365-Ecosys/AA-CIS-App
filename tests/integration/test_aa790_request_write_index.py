"""AA-790 (S251) — the write index of a piece within its request, on real Postgres (all migrations applied).

`service._request_write_index` gives the LinkedIn lead-fact rotation its index: how many pieces of the same
request were started before this one, any status (a write still `processing` counts: the S251 pilot started six
writes on one request 2 s apart), ordered by (created_at, piece_id) so two writes with the same timestamp still
get distinct indexes. Another request's and another tenant's pieces never count.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_content_writing import service as cw_service  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def ctx():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    tids = [uuid.uuid4(), uuid.uuid4()]
    for tid in tids:
        await p.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) "
                        "VALUES ($1, 'AA-790', $2, 3)", tid, f"aa790-{tid.hex[:8]}")
    yield p, tids
    for tid in tids:
        await p.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = $1", tid)  # cascades pieces
        await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
    await p.close()


async def _request(p, tid):
    return await p.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
        "VALUES ($1, 'a790', 'linkedin', 'approved') RETURNING request_id", tid)


async def _piece(p, tid, req, *, status, at, piece_id=None):
    return await p.fetchval(
        "INSERT INTO acp_shared.content_piece (piece_id, tenant_id, angle_gate_request_id, attempt_number, "
        "content_text, status, gate_ledger, repair_log, created_at) "
        "VALUES ($1, $2, $3, 1, $4, $5, '{}'::jsonb, '[]'::jsonb, $6) RETURNING piece_id",
        piece_id or uuid.uuid4(), tid, req, "" if status == "processing" else "text", status, at)


async def test_index_counts_earlier_pieces_of_the_same_request_only(ctx):
    p, (tid, other_tid) = ctx
    req, other_req = await _request(p, tid), await _request(p, tid)
    first = await _piece(p, tid, req, status="approved", at=T0)
    second = await _piece(p, tid, req, status="processing", at=T0 + timedelta(seconds=2))
    third = await _piece(p, tid, req, status="processing", at=T0 + timedelta(seconds=4))
    await _piece(p, tid, other_req, status="approved", at=T0 - timedelta(minutes=5))  # another request
    foreign_req = await _request(p, other_tid)
    await _piece(p, other_tid, foreign_req, status="approved", at=T0 - timedelta(minutes=5))  # another tenant
    got = [await cw_service._request_write_index(str(tid), req, pid, p) for pid in (first, second, third)]
    assert got == [0, 1, 2]


async def test_same_timestamp_still_gives_distinct_indexes(ctx):
    p, (tid, _) = ctx
    req = await _request(p, tid)
    a, b = sorted([uuid.uuid4(), uuid.uuid4()])
    await _piece(p, tid, req, status="processing", at=T0, piece_id=b)
    await _piece(p, tid, req, status="processing", at=T0, piece_id=a)
    got = {await cw_service._request_write_index(str(tid), req, pid, p) for pid in (a, b)}
    assert got == {0, 1}


async def test_unknown_piece_is_index_zero(ctx):
    p, (tid, _) = ctx
    req = await _request(p, tid)
    await _piece(p, tid, req, status="approved", at=T0)
    assert await cw_service._request_write_index(str(tid), req, uuid.uuid4(), p) == 0
