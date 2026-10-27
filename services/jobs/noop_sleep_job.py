"""AA-993 G1 — `noop_sleep`, a test-only job kind for the worker-drain proof (ADR 0016 D1).

Registered only when `JOB_TEST_KINDS=1` is set on the process (api and worker must agree: the post-deploy verify
compares the worker's caps with the API's registered kinds). It sleeps `payload.seconds` (1..3600) and writes
`progress.slept_seconds` every step, so a job released on shutdown resumes where it stopped. It touches no data,
calls nothing paid, and is `deploy_gate_exempt`, so the drain proof needs no deploy-guard override (critic 15):

    enqueue noop_sleep {"seconds": 300} -> deploy -> old worker logs job_worker_draining, then
    job_released_on_shutdown (counted=false) -> the new revision re-claims, attempt unchanged -> succeeded
"""
from __future__ import annotations

import asyncio
import os

from shared.jobs.registry import JobContext, NonRetryable, job_kind

KIND = "noop_sleep"
MAX_SECONDS = 3600
STEP_SECONDS = 5.0


def enabled() -> bool:
    """`JOB_TEST_KINDS` set truthy (1 / true / yes). Unset everywhere outside a drain proof."""
    return os.environ.get("JOB_TEST_KINDS", "").strip().lower() in ("1", "true", "yes")


async def run(ctx: JobContext) -> dict:
    seconds = ctx.payload.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= MAX_SECONDS:
        raise NonRetryable(f"payload.seconds must be a number from 1 to {MAX_SECONDS}")
    slept = float((ctx.job.progress or {}).get("slept_seconds") or 0.0)
    resumed_at = slept
    while slept < seconds:
        step = min(STEP_SECONDS, seconds - slept)
        await asyncio.sleep(step)
        slept += step
        await ctx.progress(slept_seconds=round(slept, 3), total_seconds=seconds)
    return {"slept_seconds": round(slept, 3), "resumed_at_seconds": round(resumed_at, 3)}


def register() -> None:
    """Registers the kind (tests call it directly; the module calls it when JOB_TEST_KINDS is on)."""
    job_kind(KIND, concurrency=1, max_attempts=1, expected_seconds=MAX_SECONDS + 60,
             deploy_gate_exempt=True)(run)


if enabled():
    register()
