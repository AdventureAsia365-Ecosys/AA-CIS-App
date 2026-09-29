"""AA-652 PR-C — `a3_atomize` job kind: atomize one published Master Content tour, then recompute
the platform-wide Segment/Score/Route (services/export/handler.py::_run_a3_atomize_background).

Before AA-652 this ran as an in-process asyncio task launched from `process_export()` (and, for
the admin "atomize un-atomized tours" action, one long task looping over N tours). A deploy
killed it mid-tour; from the export Lambda it died with the Lambda. It is now a durable job:
enqueued in the same transaction scope as the publish, run by the ECS worker.

Re-running is safe and cheap: run_t5_atomize() skips days whose fingerprint is unchanged (no LLM
call) and UPSERTs atoms (ON CONFLICT (atom_id)).

Payload: {"tour_id", "version_id", "country", "rewritten": {name, summary, highlights, itineraries}}

Runs in its OWN thread and event loop (not the worker's). The Segment/Score/Route recompute calls
compute_embedding() straight from async code, and that function paces Cohere calls with a
blocking time.sleep(3.5). Run on the API's loop (the worker lives in the API process), those
sleeps starved /health and ECS replaced the task (S201 incident, 29/09/2026). The chain opens its
own DB connection, so nothing is shared with the worker's loop. Trade-off: an admin cancel or a
shutdown cannot interrupt the thread; the job is released and the thread ends with the process.
Concurrency 1: the Segment/Score/Route recompute is platform-wide, and one tour at a time is what
the admin trigger already did.

AA-688: the thread reports progress (embedding pre-pass, question landing) through a sync
callback that schedules ctx.progress() on the worker's loop — fire-and-forget, so a failed
progress write never fails or blocks the atomize run.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "a3_atomize"


async def enqueue_a3_atomize(db, *, tour_id: str, version_id: str, country: str, rewritten: dict,
                             created_by: str, dedupe: bool = True) -> str:
    """`db` is a pool or a single connection. `dedupe=True` (publish path) keys the job on the
    content version, so publishing the same version twice does not atomize twice; the admin
    trigger passes False because it exists to re-run tours that are still un-atomized."""
    job_id, _ = await enqueue(
        db, KIND,
        {"tour_id": str(tour_id), "version_id": str(version_id), "country": country or "",
         "rewritten": rewritten},
        idempotency_key=f"{KIND}:{version_id}" if dedupe else None, created_by=created_by,
    )
    return job_id


@job_kind(KIND, concurrency=1, max_attempts=2)
async def run(ctx: JobContext) -> Optional[dict]:
    from services.export.handler import _run_a3_atomize_background

    p = ctx.payload
    if not (p.get("tour_id") and p.get("version_id") and isinstance(p.get("rewritten"), dict)):
        raise NonRetryable("payload needs tour_id, version_id and rewritten")
    await ctx.progress(phase="atomizing", tour_id=p["tour_id"])
    worker_loop = asyncio.get_running_loop()

    def _log_failure(future) -> None:
        if not future.cancelled() and future.exception() is not None:
            logger.warning("a3_progress_write_failed", job_id=ctx.job_id, error=str(future.exception()))

    def _report(fields: dict) -> None:
        # Sub-steps use the "step" key, never "phase", so a late write cannot overwrite the
        # final phase="done" below.
        asyncio.run_coroutine_threadsafe(ctx.progress(**fields), worker_loop).add_done_callback(_log_failure)

    def _in_own_loop() -> None:
        asyncio.run(_run_a3_atomize_background(
            tour_id=p["tour_id"], rewritten=p["rewritten"], country=p.get("country") or "",
            version_id=p["version_id"], reraise=True, progress=_report,
        ))

    await asyncio.to_thread(_in_own_loop)
    await ctx.progress(phase="done")
    return {"tour_id": p["tour_id"], "version_id": p["version_id"]}
