"""AA-802 (S238 scope) — the latest-piece rule on every tenant read of T9 pieces.

GET /v1/publish-log/pending listed every approved/held piece without a live publish, so each older
version of a request was its own Ready-to-Publish card (atlas-hearth: 46 cards for 3 titles). It
now applies the AA-497 rule My Content already uses: only the latest piece per
angle_gate_request_id. publish() refuses a superseded piece with a 409.

The behaviour itself is asserted on real Postgres in
tests/integration/test_aa802_pending_latest_piece.py; these tests pin the SQL shape and the gate.
"""
import re
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.routers import v1_publish as mod
from services.acp_content_writing import service as cw_service

TENANT = {"sub": str(uuid.uuid4()), "role": "tenant"}
PIECE_ID = uuid.uuid4()


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


def _pool(*fetchrow_returns):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_returns))
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    return req


# ── /pending SQL ────────────────────────────────────────────────────────────────────────────────

def test_pending_picks_the_latest_piece_per_request_before_any_filter():
    sql = _norm(mod._PENDING_SQL)
    assert "SELECT DISTINCT ON (angle_gate_request_id) piece_id" in sql
    assert "ORDER BY angle_gate_request_id, created_at DESC" in sql
    # The latest piece is chosen over every status: the status / channel / publish filters sit in
    # the outer WHERE, after `latest`, so an older approved piece cannot stand in for a newer
    # processing / failed / published one.
    inner = sql[sql.index("FROM ( SELECT DISTINCT ON"):sql.index(") latest")]
    assert "status" not in inner and "publish_log" not in inner
    outer = sql[sql.index(") latest"):]
    assert "cp.status IN ('approved', 'held')" in outer
    assert "NOT EXISTS" in outer and "pl.status IN ('published', 'requested')" in outer


def test_pending_uses_the_same_latest_rule_as_my_content():
    """My Content (_TENANT_REVIEWS_QUERY, AA-497/AA-501) and /pending must agree on which piece is
    a request's content: same key, same order."""
    reviews = _norm(cw_service._TENANT_REVIEWS_QUERY)
    pending = _norm(mod._PENDING_SQL)
    assert "DISTINCT ON (cp.angle_gate_request_id)" in reviews
    assert "ORDER BY cp.angle_gate_request_id, cp.created_at DESC" in reviews
    assert "DISTINCT ON (angle_gate_request_id)" in pending
    assert "ORDER BY angle_gate_request_id, created_at DESC" in pending


@pytest.mark.asyncio
async def test_list_pending_runs_the_latest_piece_sql():
    pool, conn = _pool()
    conn.fetch = AsyncMock(return_value=[])
    out = await mod.list_pending(_request(pool), TENANT)
    assert out == {"data": [], "total": 0}
    sql, tenant_param, channels = conn.fetch.call_args[0]
    assert sql is mod._PENDING_SQL
    assert tenant_param == TENANT["sub"] and set(channels) == {"blog", "facebook"}


# ── publish() gate ──────────────────────────────────────────────────────────────────────────────

def _piece(**over):
    row = {"piece_id": str(PIECE_ID), "content_text": "x", "status": "approved",
           "channel": "blog", "angle_name": "A", "superseded": False}
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_publish_refuses_a_superseded_piece_with_409():
    pool, conn = _pool(_piece(superseded=True))
    with pytest.raises(HTTPException) as exc_info:
        await mod.publish(PIECE_ID, _request(pool), TENANT)
    assert exc_info.value.status_code == 409
    assert "newer version" in exc_info.value.detail
    assert conn.fetchrow.call_count == 1  # refused before the integration lookup / claim
    lookup = _norm(conn.fetchrow.call_args_list[0][0][0])
    assert "newer.angle_gate_request_id = cp.angle_gate_request_id" in lookup
    assert "newer.created_at > cp.created_at" in lookup


@pytest.mark.asyncio
async def test_publish_latest_piece_passes_the_gate():
    # Latest piece, no integration row -> the next gate (422 connect) answers, not the 409.
    pool, _ = _pool(_piece(superseded=False), None)
    with pytest.raises(HTTPException) as exc_info:
        await mod.publish(PIECE_ID, _request(pool), TENANT)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_publish_not_written_yet_still_422_before_the_superseded_check():
    pool, _ = _pool(_piece(status="processing", content_text="", superseded=True))
    with pytest.raises(HTTPException) as exc_info:
        await mod.publish(PIECE_ID, _request(pool), TENANT)
    assert exc_info.value.status_code == 422
