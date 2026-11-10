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


# ── AA-1078: a failed piece is a failed job with a typed code (piece e16fbbfb-style case) ───────────────────────────

@pytest.mark.asyncio
async def test_a_piece_the_writer_stored_as_failed_fails_the_job_with_a_typed_code():
    """run_write_background turned its own exception into status 'failed' (content 0 chars) and returned: the job
    used to end succeeded with no error and no code. Now: failed at once, typed error, the class in the result."""
    from shared.jobs.registry import OutputFailed
    from shared.jobs.worker import failure_outcome
    conn = AsyncMock()
    conn.fetchval.side_effect = ["processing", "failed", "failed", "KeyError: 'faq'"]
    ctx = _ctx(_pool(conn))
    with patch.object(t9.service, "run_write_background", AsyncMock()):
        with pytest.raises(OutputFailed) as ei:
            await t9.run(ctx)
    error, retryable, reason = failure_outcome(ei.value)
    assert error == "t9_piece_failed: KeyError: 'faq'" and retryable is False and reason == t9.PIECE_FAILED
    assert ctx.result == {"piece_id": PIECE_ID, "piece_status": "failed", "failure_code": "t9_piece_failed",
                          "error_class": "KeyError"}
    ctx.progress.assert_any_await(phase="done", piece_status="failed")


@pytest.mark.asyncio
async def test_a_rerun_over_a_failed_piece_is_not_green_either():
    from shared.jobs.registry import OutputFailed
    conn = AsyncMock()
    conn.fetchval.side_effect = ["failed", None]
    ctx = _ctx(_pool(conn))
    with patch.object(t9.service, "run_write_background", AsyncMock()) as m_bg:
        with pytest.raises(OutputFailed, match="^t9_piece_failed: unknown$"):
            await t9.run(ctx)
    m_bg.assert_not_awaited()
    assert ctx.result["skipped"] is True and ctx.result["error_class"] == "unknown"


@pytest.mark.parametrize("held, cls", [("ValueError: bad", "ValueError"), ("json.decoder.JSONDecodeError: x",
                                                                            "json.decoder.JSONDecodeError"),
                                       ("free text reason", "unknown"), (None, "unknown"), ("", "unknown")])
def test_failure_class_reads_the_exception_class(held, cls):
    assert t9.failure_class(held) == cls


@pytest.mark.asyncio
async def test_the_worker_stores_a_failed_piece_job_as_failed_with_its_result():
    """Through the worker: queue.fail gets the typed error, no retry, and the result the handler set."""
    from shared.jobs import worker as wmod
    conn = AsyncMock()
    conn.fetchval.side_effect = ["processing", "failed", "failed", "RuntimeError: boom"]
    pool = _pool(conn)
    job = queue.Job(id="j1", kind=t9.KIND, payload=PAYLOAD, status="running", attempt=1, max_attempts=2,
                    progress={}, cost_usd=0.0)
    w = wmod.Worker(pool, worker_id="w1", heartbeat_seconds=5)
    with patch.object(t9.service, "run_write_background", AsyncMock()), \
            patch.object(registry.JobContext, "progress", AsyncMock()), \
            patch.object(wmod.queue, "fail", AsyncMock(return_value="failed")) as m_fail, \
            patch.object(wmod, "run_terminal_hook", AsyncMock()):
        await w._execute_job(job)
    args, kw = m_fail.call_args
    assert args[3] == "t9_piece_failed: RuntimeError: boom" and kw["retryable"] is False
    assert kw["result"]["failure_code"] == "t9_piece_failed" and kw["result"]["piece_status"] == "failed"
