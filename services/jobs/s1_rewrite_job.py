"""AA-723 — `s1_rewrite` job kind: the admin S1 (A1) rewrite of one source tour.

Moved out of `api/routers/admin_pipeline.py::run_tour_async()`, where it ran as an in-process
`asyncio.create_task(_run_tour_job(...))` tracked in `shared.pipeline_jobs`. That path died on
every API deploy (S212: a Thailand rerun wave lost 16 tours to `cancelled (deploy/shutdown)`) and
never showed on the Jobs page. The endpoint now enqueues this job and returns; the worker runs it,
so it survives an API deploy (worker drain + task-revision gating, AA-711), is retryable and shows
on `/admin/job-runner/*` like every other kind.

Payload: a `TourRunRequest` dump — {"tour_id", "batch_id", "tenant_id", "model_tier", "seo_mode",
"rewrite_language", "brand_identity_id", "brand_name", ...}.

Read-before-write (AA-723 review §3.1): if the worker dies after the executor has already written
the version but before the job is marked complete, a reap+retry must NOT write a second version for
the same tour. The job records `phase = "done"` + the written `version_id` in its progress as soon as the
executor returns; a retry that sees `phase = "done"` returns that version instead of rewriting.

AA-993 G2 (ADR 0016 D1): a deploy releases a running job and a newer worker re-claims it. The executor now records
each step it finished in `progress.persist_stage` (services/content_generation/s1_checkpoint.py: writer_done →
rewritten → content_inserted → scored → fix_pass_saved → routed → accounted) and a re-claimed job resumes from the
last one: no new writer call once the writer finished, one version, one score row, one review entry. The early return
fires ONLY on `phase = "done"`: a `version_id` alone (written at content_inserted) means the persist steps after the
INSERT may still be owed.

Concurrency 6 (AA-737, was 4): the job is I/O-bound on Bedrock — the S217 Nepal wave ran 4 in
parallel at ~20% worker CPU with no Bedrock fallbacks on s1_generate. The worker has 8 slots
(worker.WORKER_MAX_PARALLEL_DEFAULT), so 6 rewrites still leave a slot for a3_atomize.
max_attempts 1 because `_execute_run_tour`'s internal retry (`_run_tour_safe`, 3x) is preserved
below — a job-level retry would otherwise write a fresh version per attempt.
"""
from __future__ import annotations

import structlog

from services.content_generation.s1_checkpoint import BULKY_KEYS, S1Checkpoint
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "s1_rewrite"


@job_kind(KIND, concurrency=6, max_attempts=1, expected_seconds=600)
async def run(ctx: JobContext) -> dict:
    # Imported here (not at module import) so registering the kind never drags the heavy
    # admin_pipeline router + its LangGraph imports into the worker's import graph eagerly.
    from api.routers.admin_pipeline import (
        _MASTER_TENANT_ID, TourRunRequest, _build_s1_progress_sink, _run_tour_safe,
    )
    from shared.llm_client import stream_sink

    payload = dict(ctx.payload or {})
    tour_id = payload.get("tour_id")
    if not tour_id:
        raise NonRetryable("payload needs tour_id")

    # Read-before-write: a reap/retry of a job that already finished must not write again (AA-993 G2: only phase=done).
    prior = dict(ctx.job.progress or {})
    if prior.get("phase") == "done":
        logger.info("s1_rewrite_already_written", job_id=ctx.job_id, tour_id=tour_id,
                    version_id=prior.get("version_id"))
        return {"version_id": prior.get("version_id"), "resumed": True}

    async def _save(fields: dict) -> None:
        await ctx.progress(**fields)

    checkpoint = S1Checkpoint(ctx.job_id, prior, _save)
    if checkpoint.resumed_from:
        logger.info("s1_rewrite_resuming", job_id=ctx.job_id, tour_id=tour_id, persist_stage=checkpoint.resumed_from,
                    version_id=checkpoint.version_id)

    req = TourRunRequest(**{k: v for k, v in payload.items()
                            if k in TourRunRequest.model_fields})

    # AA-667 live streaming — bind a WritingProgress sink around the whole run so the s1_generate
    # writer streams into Redis (key wp:{master}:tour:{job_id}), readable by
    # GET /admin/progress/s1/{job_id}. On the worker we reuse the client it owns
    # (ctx.resources["redis"]) instead of opening our own — same precedent as t2_rewrite_job.
    progress = _build_s1_progress_sink(ctx.resources.get("redis"), ctx.job_id)
    await ctx.progress(phase="writing", tour_id=tour_id, resumed_from=checkpoint.resumed_from)

    # AA-250 B2 stage bar — on the worker, each completed LangGraph node goes into
    # shared.job.progress.current_stage (the old in-process path wrote pipeline_jobs instead).
    async def _stage_cb(node_name: str) -> None:
        await ctx.progress(current_stage=node_name)

    result = None
    if progress is not None:
        progress.start()
        try:
            with stream_sink.bind(progress):
                result = await _run_tour_safe(req, job_id=ctx.job_id, stage_cb=_stage_cb, checkpoint=checkpoint)
        finally:
            await progress.finish(ok=result is not None)
    else:
        result = await _run_tour_safe(req, job_id=ctx.job_id, stage_cb=_stage_cb, checkpoint=checkpoint)

    # _run_tour_safe swallows the final failure and returns None after 3 internal attempts.
    if result is None:
        raise RuntimeError("run_tour failed after retries (see logs)")

    version_id = result.get("version_id")
    # Persist the written version id immediately so a crash before `complete()` is idempotent; the bulky checkpoint
    # payloads (writer state, rewrite result) are dropped from progress in the same write.
    await ctx.progress(phase="done", version_id=version_id, **{k: None for k in BULKY_KEYS})
    return {
        "version_id":   version_id,
        "status":       result.get("status"),
        "quality_score": result.get("quality_score"),
        "model_used":   result.get("model_used"),
        "brand_audit_status": result.get("brand_audit_status"),
        # AA-990 N3: the job row says why a version was held (e.g. ITINERARY_DAY_COUNT_MISMATCH on a v2 draft)
        # instead of a bare success + score.
        "publishable":   result.get("publishable"),
        "hard_block_codes": result.get("hard_block_codes", []),
        "failure_codes": result.get("failure_codes", []),
    }
