"""AA-723 — `revalidate` job kind: re-score one human-edited generated_content version.

Moved out of `api/routers/admin_pipeline.py::revalidate_generated_content()`, where it ran as an
in-process `asyncio.create_task(_revalidate_job(...))` tracked in `shared.pipeline_jobs` — dying on
every API deploy and invisible to the Jobs page. The endpoint now enqueues this job; the worker
runs `_revalidate_tour` (build_revalidation_graph: validate + judge + brand_audit, no flag_fix),
overwrites the version's quality_scores rows and sets `generated_content.revalidate_passed`.

Payload: {"content_id", "tour_id"}.

Idempotent by construction: `_revalidate_tour` DELETEs then re-INSERTs the version's quality_scores
rows and overwrites `revalidate_passed`, so a reap/retry simply recomputes the same version in
place — no duplicate rows.
"""
from __future__ import annotations

import structlog

from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "revalidate"


# Live revalidate is a single graph pass; 10 min means something is stuck.
@job_kind(KIND, concurrency=2, max_attempts=2, expected_seconds=600)
async def run(ctx: JobContext) -> dict:
    from api.routers.admin_pipeline import _revalidate_tour

    content_id = (ctx.payload or {}).get("content_id")
    if not content_id:
        raise NonRetryable("payload needs content_id")

    await ctx.progress(phase="revalidating", content_id=content_id)
    result = await _revalidate_tour(content_id)
    await ctx.progress(phase="done", revalidate_passed=result.get("revalidate_passed"))
    logger.info("revalidate_job_done", job_id=ctx.job_id, content_id=content_id,
                revalidate_passed=result.get("revalidate_passed"))
    # result_version_id surfaced for the Jobs page poll (the version re-scored in place).
    return {"version_id": content_id, **result}
