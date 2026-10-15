"""AA-802 (S238 scope) — the latest-piece rule on real Postgres (all migrations applied by conftest).

GET /v1/publish-log/pending (list_pending) must return at most one card per angle_gate_request:
its latest content_piece, and nothing when that latest piece is published / in flight or not
written yet. It must agree with My Content (fetch_review_list, the AA-497 rule). publish() refuses
a superseded piece.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers import v1_publish as pub_mod  # noqa: E402
from services.acp_content_writing import service as cw_service  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def ctx():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    # Migration 124 (pgvector) never applies in the test DB; add the columns the reads select.
    await p.execute("""
        ALTER TABLE acp_shared.content_piece
            ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid, ADD COLUMN IF NOT EXISTS channel text,
            ADD COLUMN IF NOT EXISTS route_hub_name text, ADD COLUMN IF NOT EXISTS route_segment_count smallint,
            ADD COLUMN IF NOT EXISTS flags jsonb""")
    tid = uuid.uuid4()
    await p.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, 'AA-802', $2, 3)",
                    tid, f"aa802-{tid.hex[:8]}")
    yield p, tid
    await p.execute("DELETE FROM acp_shared.publish_log WHERE tenant_id = $1", tid)
    await p.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = $1", tid)  # cascades pieces
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
    await p.close()


def _request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    req.app.state.tenant_pool = None
    return req


async def _request_row(p, tid, channel="blog"):
    req = await p.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
        "VALUES ($1, 'a802', $2, 'approved') RETURNING request_id", tid, channel)
    opt = await p.fetchval(
        "INSERT INTO acp_shared.angle_gate_option (request_id, idx, name, why_it_works, formula_fit, "
        "best_final_style, chosen) VALUES ($1, 0, 'Where Two Worlds Meet', 'w', 'f', 's', true) "
        "RETURNING option_id", req)
    return req, opt


async def _piece(p, tid, req, opt, *, status, minutes, attempt=1, channel="blog"):
    return await p.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, angle_gate_option_id, "
        "attempt_number, content_text, status, gate_ledger, repair_log, channel, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, '{}'::jsonb, '[]'::jsonb, $7, $8) RETURNING piece_id",
        tid, req, opt, attempt, "" if status in ("processing", "failed") else f"text {minutes}",
        status, channel, T0 + timedelta(minutes=minutes))


async def _log(p, tid, piece, status):
    await p.execute(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, published_at) "
        "VALUES ($1, $2, 'blog', $3, now())", piece, tid, status)


async def _three_versions(p, tid):
    req, opt = await _request_row(p, tid)
    await _piece(p, tid, req, opt, status="approved", minutes=0)
    await _piece(p, tid, req, opt, status="held", minutes=10, attempt=2)
    latest = await _piece(p, tid, req, opt, status="approved", minutes=20)
    return req, opt, latest


async def _pending_ids(p, tid):
    out = await pub_mod.list_pending(_request(p), tenant={"sub": str(tid)})
    assert out["total"] == len(out["data"])
    return [r["piece_id"] for r in out["data"]]


@pytest.mark.asyncio
async def test_three_pieces_under_one_request_give_one_card_the_latest(ctx):
    p, tid = ctx
    _, _, latest = await _three_versions(p, tid)
    assert await _pending_ids(p, tid) == [str(latest)]


@pytest.mark.asyncio
@pytest.mark.parametrize("log_status", ["published", "requested"])
async def test_request_whose_latest_piece_is_published_or_in_flight_shows_nothing(ctx, log_status):
    p, tid = ctx
    _, _, latest = await _three_versions(p, tid)
    await _log(p, tid, latest, log_status)
    assert await _pending_ids(p, tid) == []


@pytest.mark.asyncio
async def test_failed_or_unpublished_latest_stays_publishable(ctx):
    p, tid = ctx
    _, _, latest = await _three_versions(p, tid)
    await _log(p, tid, latest, "failed")
    await _log(p, tid, latest, "unpublished")
    assert await _pending_ids(p, tid) == [str(latest)]


@pytest.mark.asyncio
@pytest.mark.parametrize("latest_status", ["processing", "failed"])
async def test_older_approved_piece_never_stands_in_for_an_unwritten_latest(ctx, latest_status):
    p, tid = ctx
    req, opt, _ = await _three_versions(p, tid)
    await _piece(p, tid, req, opt, status=latest_status, minutes=30)
    assert await _pending_ids(p, tid) == []


@pytest.mark.asyncio
async def test_older_published_piece_does_not_hide_a_newer_version(ctx):
    """A re-write after a publish is new content: the newer piece is publishable (the older post
    stays live on the channel; the admin sees the tenant's older copy via AA-802 tenant_copy)."""
    p, tid = ctx
    req, opt = await _request_row(p, tid)
    old = await _piece(p, tid, req, opt, status="approved", minutes=0)
    await _log(p, tid, old, "published")
    new = await _piece(p, tid, req, opt, status="approved", minutes=10)
    assert await _pending_ids(p, tid) == [str(new)]


@pytest.mark.asyncio
async def test_pending_matches_my_content_and_never_repeats_a_request(ctx):
    p, tid = ctx
    _, _, a_latest = await _three_versions(p, tid)                 # pending
    _, _, b_latest = await _three_versions(p, tid)
    await _log(p, tid, b_latest, "published")                      # live publish -> not pending
    c_req, c_opt = await _request_row(p, tid, channel="facebook")
    c_latest = await _piece(p, tid, c_req, c_opt, status="held", minutes=5, channel="facebook")  # pending
    d_req, d_opt = await _request_row(p, tid)
    await _piece(p, tid, d_req, d_opt, status="approved", minutes=0)
    await _piece(p, tid, d_req, d_opt, status="processing", minutes=40)  # in progress -> not pending

    pending = await _pending_ids(p, tid)
    assert sorted(pending) == sorted([str(a_latest), str(c_latest)])
    assert len(pending) == len(set(pending))

    my_content = await cw_service.fetch_review_list(tid, p)
    assert len(my_content) == 4  # one item per request
    live = {str(r["piece_id"]) for r in await p.fetch(
        "SELECT piece_id FROM acp_shared.publish_log WHERE tenant_id = $1 AND status IN ('published', 'requested')",
        tid)}
    ready_unpublished = {i["piece_id"] for i in my_content
                         if i["ready_state"] == "ready" and i["piece_id"] not in live}
    assert set(pending) == ready_unpublished


@pytest.mark.asyncio
async def test_publish_refuses_a_superseded_piece(ctx):
    p, tid = ctx
    req, opt = await _request_row(p, tid)
    old = await _piece(p, tid, req, opt, status="approved", minutes=0)
    await _piece(p, tid, req, opt, status="approved", minutes=10)
    with pytest.raises(HTTPException) as exc_info:
        await pub_mod.publish(old, _request(p), tenant={"sub": str(tid)})
    assert exc_info.value.status_code == 409
    assert await p.fetchval("SELECT count(*) FROM acp_shared.publish_log WHERE tenant_id = $1", tid) == 0
