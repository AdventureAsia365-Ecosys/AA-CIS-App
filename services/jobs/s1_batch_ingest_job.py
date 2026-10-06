"""AA-723 — `s1_batch_ingest` job kind: poll a Bedrock Batch S1 job to completion and ingest it.

Moved out of `api/routers/admin_pipeline.py::_s1_batch_ingest_task` (in-process asyncio +
shared.pipeline_jobs). The submit step stays in the request (it returns the batch ARN); this job
owns the long poll + ingest, so it survives an API deploy and shows on /admin/job-runner.

Payload: {"job_arn", "output_uri", "account", "tour_ids", "batch_id", "seo_mode", "model_tier"}.

NOTE (AA-624): Bedrock Batch is currently blocked on acc3, so this kind cannot be verified live
yet. It is moved for consistency (so no background task is left in the API process); the ingest
logic itself is unchanged from the in-process version.
"""
from __future__ import annotations

import structlog

from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "s1_batch_ingest"


# A Bedrock Batch job can take a long time; the worker lease is heartbeated, so a high expected is
# just the "running longer than usual" flag on the Jobs page, not a timeout.
@job_kind(KIND, concurrency=1, max_attempts=1, expected_seconds=7200)
async def run(ctx: JobContext) -> dict:
    from services.content_generation.s1_batch import ingest_s1_batch

    p = ctx.payload or {}
    for key in ("job_arn", "output_uri", "account", "tour_ids", "batch_id"):
        if p.get(key) in (None, ""):
            raise NonRetryable(f"payload needs {key}")

    await ctx.progress(phase="polling", batch_id=p["batch_id"], record_count=len(p["tour_ids"]))
    summary = await ingest_s1_batch(
        job_arn=p["job_arn"],
        output_uri=p["output_uri"],
        account=p["account"],
        tour_ids=p["tour_ids"],
        batch_id=p["batch_id"],
        seo_mode=p.get("seo_mode", "standard"),
        model_tier=p.get("model_tier"),
        poll=True,
    )
    await ctx.progress(phase="done", ingested=summary.get("ingested"))
    logger.info("s1_batch_ingest_done", job_id=ctx.job_id, batch_id=p["batch_id"],
                ingested=summary.get("ingested"),
                gate_fail_retry=summary.get("gate_fail_retry"),
                batch_write_failed=summary.get("batch_write_failed"))
    return dict(summary)
