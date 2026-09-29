"""AA-652 PR-B — `t9_write` job kind: a tenant's T9 write + T10-inline check for one piece.

Moved out of `api/routers/v1_content_writing.py::write()`, where it ran as an in-process asyncio
task. A deploy or restart killed it and the piece stayed 'processing' ("Writing…") forever. The
router still does the fast pre-flight (`service.start_write()`: 404/409/422, placeholder piece)
and now enqueues this job; the job runs `service.run_write_background()` with the same live
progress (ADR 0004) as before.

Payload: {"request_id", "piece_id", "tenant_id", "context"}  (context = start_write()'s dict)

`run_write_background()` already turns its own errors into piece status 'failed'. The job adds
what it could not do: survive an interrupted process (re-queue), and mark the piece 'failed' when
the job itself ends without success (on_terminal), so the portal's Retry shows.
"""
from __future__ import annotations

from uuid import UUID

import structlog

from services.acp_content_writing import service
from services.acp_shared.writing_progress import WritingProgress
from shared.jobs.registry import JobContext, NonRetryable, job_kind
from shared.llm_client import stream_sink

logger = structlog.get_logger()

KIND = "t9_write"


# AA-637 — tenant-facing steps for a T9 write (plain language: no gate names, no model names).
T9_STEPS = [
    ("prepare", "Reading your topic and brand voice"),
    ("write", "Writing your post"),
    ("check", "Checking facts, tone and quality"),
    ("revise", "Revising what the checks flagged"),
    ("save", "Saving to My Content"),
]


async def _run_write_with_progress(request_id: UUID, piece_id: UUID, context: dict, pool, progress) -> None:
    """run_write_background() unchanged, with a live-progress sink bound around it. The piece's
    own final status decides how the live view ends; the view never affects the piece."""
    progress.start()
    progress.step("prepare")
    try:
        with stream_sink.bind(progress):
            await service.run_write_background(request_id, piece_id, context, pool)
    except Exception:
        progress.fail()
        raise
    finally:
        if progress.failed:
            await progress.finish(False, "Writing didn't finish. Please try again.")
    status = None
    try:
        async with pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM acp_shared.content_piece WHERE piece_id = $1", piece_id
            )
    except Exception:
        pass
    if status == "failed":
        progress.fail()
        await progress.finish(False, "Writing didn't finish. Please try again.")
    else:
        await progress.finish(True)


async def mark_piece_failed(pool, job_row: dict) -> None:
    """on_terminal: the job ended without success, so the piece must not stay 'processing'."""
    piece_id = (job_row.get("payload") or {}).get("piece_id")
    if not piece_id:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE acp_shared.content_piece SET status = 'failed' "
            "WHERE piece_id = $1::uuid AND status = 'processing'", piece_id)
    logger.warning("t9_write_job_failed", piece_id=piece_id, job_id=job_row.get("id"),
                   job_status=job_row.get("status"), error=(job_row.get("error") or "")[:300])


# Live T9 write ≈ 190 s (S201).
@job_kind(KIND, concurrency=4, max_attempts=2, on_terminal=mark_piece_failed, expected_seconds=600)
async def run(ctx: JobContext) -> dict:
    p = ctx.payload
    request_id, piece_id, context = p.get("request_id"), p.get("piece_id"), p.get("context")
    if not (request_id and piece_id and isinstance(context, dict)):
        raise NonRetryable("payload needs request_id, piece_id and context")
    pool = ctx.pool

    async with pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM acp_shared.content_piece WHERE piece_id = $1::uuid", piece_id)
    if status is None:
        raise NonRetryable(f"piece {piece_id} not found")
    if status != "processing":  # finished by an earlier attempt before the process died
        return {"skipped": True, "piece_status": status}

    progress = WritingProgress(
        ctx.resources.get("redis"), tenant_id=str(p.get("tenant_id") or context.get("tenant_id")),
        kind="piece", job_id=str(piece_id), steps=T9_STEPS, stream_stages={"t9_write"},
        display="blog_json" if context.get("channel") == "blog" else "text",
    )
    await ctx.progress(phase="writing", piece_id=piece_id)
    await _run_write_with_progress(UUID(str(request_id)), UUID(str(piece_id)), context, pool, progress)

    async with pool.acquire() as conn:
        final = await conn.fetchval(
            "SELECT status FROM acp_shared.content_piece WHERE piece_id = $1::uuid", piece_id)
    await ctx.progress(phase="done")
    return {"piece_id": piece_id, "piece_status": final}
