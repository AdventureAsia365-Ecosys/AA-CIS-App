# api/routers/admin_pipeline/runs.py — S1 run routes: run-tour, SEO prefetch, async jobs, progress, Bedrock batch,
# brand rules.
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import asyncpg
from fastapi import Header, HTTPException, Query, Request
from api.core.model_tier import refuse_model_tier
from pydantic import BaseModel, model_validator
from typing import Optional, List, Literal
from api.routers.admin import verify_admin_secret
from shared.secrets import get_database_url  # AA-838 D9: default application_name

from api.routers.admin_pipeline._shared import (
    TourRunRequest, _MASTER_TENANT_ID, _is_uuid, _record_review_reason, router,
)
from api.routers.admin_pipeline._engine import _execute_run_tour


@router.post("/run-tour")
async def run_tour(req: TourRunRequest, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    return await _execute_run_tour(req)


class S1SeoPrefetchRequest(BaseModel):
    tour_ids: list[str]
    tenant_id: str = "00000000-0000-0000-0000-000000000001"


@router.post("/s1/seo-prefetch")
async def s1_seo_prefetch(req: S1SeoPrefetchRequest, request: Request, x_admin_secret: str = Header(None),
                          x_admin_user_id: Optional[str] = Header(None)):
    """AA-653: enqueue one `s1_seo_prefetch` job for the tours about to be rewritten, so their
    DataForSEO data is bought in shared tasks (and reused for a year) instead of per tour."""
    verify_admin_secret(x_admin_secret)
    from shared.jobs.registry import enqueue
    if not req.tour_ids:
        raise HTTPException(status_code=422, detail="tour_ids is empty")
    job_id, created = await enqueue(
        request.app.state.pool, "s1_seo_prefetch",
        {"tour_ids": req.tour_ids[:1000], "tenant_id": req.tenant_id},
        created_by=f"admin:{x_admin_user_id or 'unknown'}")
    return {"job_id": job_id, "created": created}


@router.post("/run-tour-async")
async def run_tour_async(req: TourRunRequest, request: Request, x_admin_secret: str = Header(None),
                         x_admin_user_id: Optional[str] = Header(None)):
    """AA-223 / AA-723: 202 + job poll. Enqueues an `s1_rewrite` job onto the durable runner
    (shared.job + worker ECS) and returns immediately. Moved off the in-process
    asyncio.create_task(_run_tour_job) path (shared.pipeline_jobs), which died on every API deploy
    and never showed on the Jobs page. The job survives deploys, is retryable, and lists on
    /admin/job-runner/*. The poll contract is unchanged: GET /admin/jobs/{job_id} maps the
    shared.job row to the same shape the S1 page already reads."""
    verify_admin_secret(x_admin_secret)
    from fastapi.responses import JSONResponse
    from shared.jobs.registry import enqueue

    payload = req.model_dump(exclude={"review_id", "reason_code"})
    # AA-993 follow-up: the job carries the platform tenant's UUID, never its slug. The worker binds the payload's
    # tenant for every LLM log row and Jev decision of the job; the slug made each llm_call_log write fail.
    if str(payload.get("tenant_id") or "").strip().lower() == "aa_internal":
        payload["tenant_id"] = _MASTER_TENANT_ID
    # Dedup key = tour + batch (AA-1024 A2: model_tier left the request, so it left the key), so a double-click or an
    # overlapping wave reuses the in-flight job instead of writing a 2nd version.
    idem = f"s1_rewrite:{payload.get('tour_id')}:{payload.get('batch_id')}"
    job_id, created = await enqueue(
        request.app.state.pool, "s1_rewrite", payload,
        idempotency_key=idem, created_by=f"admin:{x_admin_user_id or 'unknown'}")

    if created and req.review_id and _is_uuid(req.review_id):  # AA-916: one signal per real regeneration
        await _record_review_reason(request.app.state.pool, req.review_id, "regenerate", req.reason_code,
                                    x_admin_user_id)
    return JSONResponse(status_code=202, content={
        "job_id": job_id,
        "status": "queued" if created else "running",
        "dedup": not created,
        "poll_url": f"/admin/jobs/{job_id}",
    })


# shared.job status -> the status vocabulary the S1 Rewrite page already understands
# (succeeded / failed / interrupted / running / queued). cancelled + stopped_budget are terminal
# non-success states the page treats like a failure.
_SHARED_JOB_STATUS_MAP = {
    "queued": "queued", "running": "running", "succeeded": "succeeded",
    "failed": "failed", "cancelled": "interrupted", "stopped_budget": "failed",
}


@router.get("/jobs/{job_id}")
async def get_run_tour_job(job_id: str, request: Request, x_admin_secret: str = Header(None)):
    """AA-223 / AA-723: poll a run-tour job. Reads the durable shared.job row first (new path) and
    maps it to the legacy pipeline_jobs shape the S1 page polls; falls back to the old
    pipeline_jobs row so jobs created before this change still poll."""
    verify_admin_secret(x_admin_secret)
    from shared.jobs import queue as _queue

    row = await _queue.get(request.app.state.pool, job_id)
    if row is not None:
        progress = row.get("progress") or {}
        result = row.get("result") or {}
        return {
            "id":                row["id"],
            "job_type":          row["kind"],
            "status":            _SHARED_JOB_STATUS_MAP.get(row["status"], row["status"]),
            "result_version_id": result.get("version_id") or progress.get("version_id"),
            "pipeline_run_id":   None,
            "error":             row.get("error"),
            "current_stage":     progress.get("current_stage"),
            "created_at":        row.get("created_at").isoformat() if row.get("created_at") else None,
            "started_at":        row.get("started_at").isoformat() if row.get("started_at") else None,
            "finished_at":       row.get("finished_at").isoformat() if row.get("finished_at") else None,
            "heartbeat_at":      None,
        }

    # Legacy: a job created on the old in-process path before AA-723.
    from ..jobs_repo import get_job
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/progress/s1/{job_id}")
async def get_s1_progress(job_id: str, request: Request, x_admin_secret: str = Header(None)):
    """AA-667: live writing progress for one S1 rewrite job (steps + streamed tour fields),
    the admin counterpart of GET /v1/progress/tour/{id}. Reads the same Redis snapshot the
    WritingProgress sink bound in _run_tour_job writes, under the master/platform tenant.

    `found: false` is a normal answer (job not started, finished >1h ago, or run before this
    feature) — the page then falls back to the lifecycle poll (GET /admin/jobs/{job_id})."""
    verify_admin_secret(x_admin_secret)
    from services.acp_shared.writing_progress import read_progress

    if not job_id or len(job_id) > 64:
        raise HTTPException(status_code=404, detail="Unknown job")
    try:
        snap = await read_progress(request.app.state.redis, _MASTER_TENANT_ID, "tour", job_id)
    except Exception:
        snap = None  # Redis unavailable -> behave like "no live view", never a 500
    if snap is None:
        return {"found": False}
    return {"found": True, **snap}


# ── AA-606: S1 rewrite via Bedrock Batch Inference ─────────────────────────────

class S1BatchSubmitRequest(BaseModel):
    """Submit an S1-rewrite Bedrock Batch job for a set of tours (writer attempt-1 only)."""
    tour_ids: List[str]
    batch_id: str                              # UUID → pipeline_runs accounting (like sync ingest)
    # satellite account owning Batch perms (AA-399); AA-998: acc3 only, anything else is a 422
    account: Literal["acc3"] = "acc3"
    seo_mode: str = "standard"
    enforce_min: bool = True                   # reject sub-minimum batches (Bedrock rejects them)

    # AA-1024 A2: no model_tier (it was "haiku"): a tour the batch could not write falls back to the s1_generate route.
    @model_validator(mode="before")
    @classmethod
    def _refuse_model_tier(cls, data):
        return refuse_model_tier(data)


@router.post("/s1-batch/submit")
async def submit_s1_batch_endpoint(req: S1BatchSubmitRequest, request: Request,
                                   x_admin_secret: str = Header(None)):
    """AA-606 / AA-723: materialize + submit an S1-rewrite Bedrock Batch job, then enqueue a
    durable `s1_batch_ingest` job that polls+ingests on the worker. Returns 202 with job_id (poll
    GET /admin/s1-batch/{job_id}) and the batch ARN.

    The writer attempt-1 runs on Bedrock Batch (Haiku); every tour is then validated/judged/gated/
    persisted by the existing _execute_run_tour path (gate misses fall to the on-demand retry loop).
    The submit step stays in the request (it returns the ARN); the long poll+ingest is the job."""
    verify_admin_secret(x_admin_secret)
    # AA-642 (D4): 503 with the reason while the Batch kill switch is closed (the default; bedrock_batch.py).
    from shared.llm_client.bedrock_batch import batch_submit_closed_reason
    # S271 W0 (root ADR 0020): refused while the S1 writer v2 is live (the batch would seed v1 drafts), whatever the
    # kill switch says.
    from services.content_generation.s1_batch import writer_v2_refusal
    if (refused := writer_v2_refusal()) is not None:
        raise HTTPException(status_code=409, detail=refused)
    if (closed := batch_submit_closed_reason()) is not None:
        raise HTTPException(status_code=503, detail=closed)
    from fastapi.responses import JSONResponse
    from shared.jobs.registry import enqueue
    from services.content_generation.s1_batch import submit_s1_batch
    from shared.llm_client.bedrock_batch import BatchUnavailable

    try:
        submit = await submit_s1_batch(
            req.tour_ids, account=req.account, enforce_min=req.enforce_min,
        )
    except BatchUnavailable as e:
        raise HTTPException(status_code=400, detail=f"batch submit failed: {e}")

    job_id, created = await enqueue(
        request.app.state.pool, "s1_batch_ingest",
        {"job_arn": submit["job_arn"], "output_uri": submit["output_uri"],
         "account": submit["account"], "tour_ids": submit["tour_ids"],
         "batch_id": req.batch_id, "seo_mode": req.seo_mode,
         "record_count": submit["record_count"]},
        idempotency_key=f"s1_batch_ingest:{submit['job_arn']}",
        created_by="admin:s1-batch")

    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "submitted", "job_arn": submit["job_arn"],
        "record_count": submit["record_count"], "account": submit["account"],
        "poll_url": f"/admin/s1-batch/{job_id}",
    })


@router.get("/s1-batch/{job_id}")
async def get_s1_batch_job(job_id: str, request: Request, x_admin_secret: str = Header(None)):
    """AA-606 / AA-723: poll an S1 batch job. Reads the durable shared.job row (mapped to the
    legacy shape) and falls back to the old pipeline_jobs row for jobs created before AA-723."""
    verify_admin_secret(x_admin_secret)
    from shared.jobs import queue as _queue

    row = await _queue.get(request.app.state.pool, job_id)
    if row is not None:
        progress = row.get("progress") or {}
        return {
            "id":                row["id"],
            "job_type":          row["kind"],
            "status":            _SHARED_JOB_STATUS_MAP.get(row["status"], row["status"]),
            "result_version_id": None,
            "pipeline_run_id":   None,
            "error":             row.get("error"),
            "current_stage":     progress.get("phase"),
            "created_at":        row.get("created_at").isoformat() if row.get("created_at") else None,
            "started_at":        row.get("started_at").isoformat() if row.get("started_at") else None,
            "finished_at":       row.get("finished_at").isoformat() if row.get("finished_at") else None,
            "heartbeat_at":      None,
            "result":            row.get("result"),
        }
    from ..jobs_repo import get_job
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# ── GET /admin/brand-rules ────────────────────────────────────────────────────

@router.get("/brand-rules")
async def list_brand_rules(
    tenant_id: str = Query(...),
    x_admin_secret: str = Header(None),
):
    """List active brand rules for a tenant (id/name/version only — no prompt content)."""
    verify_admin_secret(x_admin_secret)
    TENANT_SLUG_MAP = {"aa_internal": "00000000-0000-0000-0000-000000000001"}
    tenant_uuid = TENANT_SLUG_MAP.get(tenant_id, tenant_id)
    conn = await asyncpg.connect(get_database_url())
    try:
        rows = await conn.fetch("""
            SELECT id, brand_name, version, is_active
            FROM shared.tenant_brand_rules
            WHERE tenant_id = $1::uuid AND is_active = true
            ORDER BY brand_name
        """, tenant_uuid)
    finally:
        await conn.close()
    return [
        {
            "id": str(r["id"]),
            "brand_name": r["brand_name"],
            "version": r["version"],
            "is_active": r["is_active"],
        }
        for r in rows
    ]
