"""AA-652 PR-C — `a3_atomize` job kind: atomize one published Master Content tour, then recompute
the platform-wide Segment/Score/Route (services/export/handler.py::_run_a3_atomize_background).

Before AA-652 this ran as an in-process asyncio task launched from `process_export()` (and, for
the admin "atomize un-atomized tours" action, one long task looping over N tours). A deploy
killed it mid-tour; from the export Lambda it died with the Lambda. It is now a durable job:
enqueued in the same transaction scope as the publish, run by the ECS worker.

Re-running is safe and cheap: run_t5_atomize() skips days whose fingerprint is unchanged (no LLM
call) and UPSERTs atoms (ON CONFLICT (atom_id)).

Payload: {"tour_id", "version_id", "country", "rewritten": {name, summary, highlights, itineraries}}
Concurrency 1: the Segment/Score/Route recompute is platform-wide, and one tour at a time is what
the admin trigger already did.
"""
from __future__ import annotations

from typing import Optional

from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

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
    await _run_a3_atomize_background(
        tour_id=p["tour_id"], rewritten=p["rewritten"], country=p.get("country") or "",
        version_id=p["version_id"], reraise=True,
    )
    await ctx.progress(phase="done")
    return {"tour_id": p["tour_id"], "version_id": p["version_id"]}
