"""AA-652 PR-B — T9 write as a durable job (services/jobs/t9_write_job.py)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.jobs import t9_write_job as t9
from shared.jobs import queue, registry
from shared.jobs.registry import JobContext, NonRetryable

PIECE_ID = "77777777-7777-7777-7777-777777777777"
REQUEST_ID = "88888888-8888-8888-8888-888888888888"
PAYLOAD = {"request_id": REQUEST_ID, "piece_id": PIECE_ID, "tenant_id": "t-1",
           "context": {"channel": "facebook", "tenant_id": "t-1"}}


def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _ctx(pool, payload=PAYLOAD):
    job = queue.Job(id="j1", kind=t9.KIND, payload=payload, status="running", attempt=1,
                    max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(pool, job, {"redis": None})
    ctx.progress = AsyncMock()
    return ctx


def test_kind_is_registered_with_a_terminal_hook():
    k = registry.get_kind(t9.KIND)
    assert k is not None and k.on_terminal is t9.mark_piece_failed and k.concurrency == 4


@pytest.mark.asyncio
async def test_incomplete_payload_is_not_retryable():
    with pytest.raises(NonRetryable):
        await t9.run(_ctx(_pool(AsyncMock()), {"piece_id": PIECE_ID}))


@pytest.mark.asyncio
async def test_finished_piece_is_skipped_on_a_rerun():
    conn = AsyncMock()
    conn.fetchval.return_value = "approved"
    with patch.object(t9.service, "run_write_background", AsyncMock()) as m_bg:
        out = await t9.run(_ctx(_pool(conn)))
    assert out == {"skipped": True, "piece_status": "approved"}
    m_bg.assert_not_awaited()


@pytest.mark.asyncio
async def test_processing_piece_is_written():
    conn = AsyncMock()
    conn.fetchval.side_effect = ["processing", "held", "held"]
    with patch.object(t9.service, "run_write_background", AsyncMock()) as m_bg:
        out = await t9.run(_ctx(_pool(conn)))
    m_bg.assert_awaited_once()
    assert str(m_bg.call_args.args[1]) == PIECE_ID
    assert out == {"piece_id": PIECE_ID, "piece_status": "held"}


@pytest.mark.asyncio
async def test_terminal_hook_marks_a_processing_piece_failed():
    conn = AsyncMock()
    await t9.mark_piece_failed(_pool(conn), {"id": "j1", "status": "failed", "payload": PAYLOAD})
    sql, piece = conn.execute.call_args.args
    assert "status = 'failed'" in sql and "status = 'processing'" in sql and piece == PIECE_ID
