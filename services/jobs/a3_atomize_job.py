"""AA-652 PR-C — `a3_atomize` job kind: atomize one published Master Content tour, then recompute
the platform-wide Segment/Score/Route (services/export/handler.py::_run_a3_atomize_background).

Before AA-652 this ran as an in-process asyncio task launched from `process_export()` (and, for
the admin "atomize un-atomized tours" action, one long task looping over N tours). A deploy
killed it mid-tour; from the export Lambda it died with the Lambda. It is now a durable job:
enqueued in the same transaction scope as the publish, run by the ECS worker.

Re-running is safe and cheap: run_a3_atomize() skips days whose fingerprint is unchanged (no LLM
call) and UPSERTs atoms (ON CONFLICT (atom_id)).

AA-645 (S250): a day still failed after run_a3_atomize()'s own second LLM call (malformed or cut JSON, a
gateway error) fails the attempt with AtomizeIncomplete, a retryable error, so max_attempts applies. Before,
the job ended "succeeded" with `atomize.status = "failed"` in its result and the day stayed without atoms
until someone re-ran the tour. A non-final attempt stops right after atomize; the final one still runs the
Segment/Score/Route and place steps on the days that did succeed, then fails the job (result kept).

Payload: {"tour_id", "version_id", "country", "rewritten": {name, summary, highlights, itineraries}}

Runs in its OWN thread and event loop (not the worker's). The Segment/Score/Route recompute calls
compute_embedding() straight from async code, and that function paces Cohere calls with a
blocking time.sleep(3.5). Run on the API's loop (the worker lives in the API process), those
sleeps starved /health and ECS replaced the task (S201 incident, 29/09/2026). The chain opens its
own DB connection, so nothing is shared with the worker's loop. Trade-off: an admin cancel or a
shutdown cannot interrupt the thread mid-day; the job is released and the thread ends with the process.

AA-993 G3 (ADR 0016 D1) — drain-safe on a deploy, so a released job never runs twice side by side:
* a `threading.Event`, set when the handler is cancelled (shutdown release, admin cancel, lost lease); the thread
  checks it at every day boundary and between the later steps and stops (AtomizeStopped). The day being read
  finishes first, so its atoms + fingerprint stay consistent; the re-claimed attempt skips the days already read
  (fingerprint match; for a `force` job also `atomized_at >= progress.force_since`, the first attempt's start, so a
  forced re-claim does not pay again for the days the stopped thread re-read).
  Late progress writes from a stopped thread are dropped, so they never land on the attempt that re-claimed the row;
* the whole chain runs under a session advisory lock on the tour (handler.a3_tour_lock, one dedicated connection),
  so a re-claimed attempt waits until the old thread let go (or its process, and so its connection, ended);
* `release_delay_seconds = 120` (the worker's ECS stopTimeout): the released row is due again only after the old
  task is gone, so the lock wait above is the exception, not the rule.
Concurrency 1: the Segment/Score/Route recompute is platform-wide, and one tour at a time is what
the admin trigger already did.

AA-688: the thread reports progress (embedding pre-pass, question landing) through a sync
callback that schedules ctx.progress() on the worker's loop — fire-and-forget, so a failed
progress write never fails or blocks the atomize run.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional

import structlog

from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "a3_atomize"


async def enqueue_a3_atomize(db, *, tour_id: str, version_id: str, country: str, rewritten: dict,
                             created_by: str, dedupe: bool = True, force: bool = False) -> str:
    """`db` is a pool or a single connection. `dedupe=True` (publish path) keys the job on the
    content version, so publishing the same version twice does not atomize twice; the admin
    trigger passes False because it exists to re-run tours that are still un-atomized.
    `force=True` (AA-645 step 0, the promote path) re-reads every day even when the version's day
    fingerprints match: a version made current again must serve its own atoms, not the replaced one's."""
    payload = {"tour_id": str(tour_id), "version_id": str(version_id), "country": country or "",
               "rewritten": rewritten}
    if force:
        payload["force"] = True
    job_id, _ = await enqueue(
        db, KIND, payload,
        idempotency_key=f"{KIND}:{version_id}" if dedupe else None, created_by=created_by,
    )
    return job_id


def _force_since(progress: Optional[dict]) -> datetime:
    """The first attempt's start, read back from progress (set once), else now (UTC). Compared with
    `atomize_day_fingerprint.atomized_at` (DB `now()`); ECS clocks are NTP-synced, and a skew only costs a day read
    twice or (by the size of the skew) skips a day re-read just before the job started, never a wrong atom."""
    raw = (progress or {}).get("force_since")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    return datetime.now(timezone.utc)


# AA-993 G3: = the worker container's ECS stopTimeout (AA-CIS-Infra modules/ecs), see the module docstring.
RELEASE_DELAY_SECONDS = 120


# Platform-wide recompute ≈ 5 min after AA-688 (first run after a reset embeds more).
@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=1800, release_delay_seconds=RELEASE_DELAY_SECONDS)
async def run(ctx: JobContext) -> Optional[dict]:
    from services.acp_contract.a3_atomize import AtomizeIncomplete
    from services.export.handler import _run_a3_atomize_background

    p = ctx.payload
    if not (p.get("tour_id") and p.get("version_id") and isinstance(p.get("rewritten"), dict)):
        raise NonRetryable("payload needs tour_id, version_id and rewritten")
    force = bool(p.get("force"))
    # AA-993 G3 review: a forced job remembers when its first attempt started, so a later attempt (deploy release,
    # AtomizeIncomplete, Retry) skips the days this job already re-read instead of re-reading every day again.
    force_since = _force_since(ctx.job.progress) if force else None
    await ctx.progress(phase="atomizing", tour_id=p["tour_id"],
                       **({"force_since": force_since.isoformat()} if force_since else {}))
    final_attempt = ctx.job.attempt >= ctx.job.max_attempts
    worker_loop = asyncio.get_running_loop()
    stop = threading.Event()   # AA-993 G3: set when this handler is cancelled

    def _log_failure(future) -> None:
        if not future.cancelled() and future.exception() is not None:
            logger.warning("a3_progress_write_failed", job_id=ctx.job_id, error=str(future.exception()))

    def _report(fields: dict) -> None:
        # Sub-steps use the "step" key, never "phase", so a late write cannot overwrite the
        # final phase="done" below. AA-993 G3: nothing once the handler was cancelled (the row may
        # already belong to the attempt that re-claimed it).
        if stop.is_set():
            return
        asyncio.run_coroutine_threadsafe(ctx.progress(**fields), worker_loop).add_done_callback(_log_failure)

    def _in_own_loop() -> dict:
        return asyncio.run(_run_a3_atomize_background(
            tour_id=p["tour_id"], rewritten=p["rewritten"], country=p.get("country") or "",
            version_id=p["version_id"], reraise=True, progress=_report, force=force,
            retry_failed_days=not final_attempt, should_stop=stop.is_set, tour_lock=True,
            **({"force_since": force_since} if force_since else {}),
        ))

    try:
        outcome = await asyncio.to_thread(_in_own_loop) or {}
    except asyncio.CancelledError:
        # The thread cannot be cancelled; tell it to stop at its next day boundary / step.
        stop.set()
        logger.warning("a3_atomize_thread_stop_requested", job_id=ctx.job_id, tour_id=p["tour_id"])
        raise
    result = {"tour_id": p["tour_id"], "version_id": p["version_id"], **outcome}
    atomize = outcome.get("atomize") or {}
    if atomize.get("status") == "failed":
        ctx.set_result(result)   # stored on the failed row (queue.fail keeps ctx.result)
        raise AtomizeIncomplete(atomize)
    await ctx.progress(phase="done")
    return result
