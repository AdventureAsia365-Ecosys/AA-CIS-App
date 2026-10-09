"""AA-223 / ADR-2026-016 — pipeline_jobs repo (async run-tour job lifecycle).

AA-735 (ADR 0003 nac 5): `shared.pipeline_jobs` is now **read-only history**. The async run-tour /
revalidate / s1-batch jobs run on the durable job runner (`shared.job` + the worker ECS service),
so nothing writes to `shared.pipeline_jobs` any more — every writer (`create_job`, `mark_running`,
`mark_succeeded`, `update_stage`, `mark_failed`, `mark_interrupted`, `sweep_interrupted`) was
retired here. Only the read helpers remain so job ids created before AA-723 still resolve: the S1
page's poll falls back to `get_job()` when the shared.job row is absent (admin_pipeline.py).

Each function opens/closes its own asyncpg.connect(os.environ["DATABASE_URL"]).
PK + result_version_id + pipeline_run_id are all UUID (see migration 071).
"""

import os

import asyncpg


async def get_job(job_id: str) -> dict | None:
    """Return a JSON-safe dict (UUID->str, timestamp->isoformat) or None.

    AA-735: the legacy-job-id fallback read — a job created on the old in-process pipeline_jobs
    path (before AA-723) still resolves through here when GET /admin/jobs/{id} finds no shared.job
    row.
    """
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        row = await conn.fetchrow(
            "SELECT id, job_type, status, result_version_id, pipeline_run_id, error, "
            "       current_stage, created_at, started_at, finished_at, heartbeat_at "
            "FROM shared.pipeline_jobs WHERE id=$1::uuid",
            job_id,
        )
        if row is None:
            return None

        def _ts(v):
            return v.isoformat() if v is not None else None

        def _uid(v):
            return str(v) if v is not None else None

        return {
            "id":                _uid(row["id"]),
            "job_type":          row["job_type"],
            "status":            row["status"],
            "result_version_id": _uid(row["result_version_id"]),
            "pipeline_run_id":   _uid(row["pipeline_run_id"]),
            "error":             row["error"],
            "current_stage":     row["current_stage"],
            "created_at":        _ts(row["created_at"]),
            "started_at":        _ts(row["started_at"]),
            "finished_at":       _ts(row["finished_at"]),
            "heartbeat_at":      _ts(row["heartbeat_at"]),
        }
    finally:
        await conn.close()


async def find_active_duplicate(request: dict) -> str | None:
    """Job-tier idempotency guard: an in-flight job for the same
    (tour_id, model_tier, batch_id) triple. request->> returns text, so compare
    against text params directly. IS NOT DISTINCT FROM makes NULL batch_id/model_tier match.
    Returns str(id) of the newest active match, or None.

    AA-735: a pure read over pipeline_jobs; the live dedup now runs off the shared.job
    idempotency key (admin_pipeline.run_tour_async), so this never finds a current in-flight row —
    kept read-only for history/compatibility.
    """
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        dup_id = await conn.fetchval(
            "SELECT id FROM shared.pipeline_jobs "
            "WHERE status IN ('queued', 'running') "
            "  AND request->>'tour_id'    = $1 "
            # AA-702: the S1 page now sends model_tier=null (use Settings) by default; a plain
            # "=" never matches NULL, which would let a double-click queue the same tour twice.
            "  AND request->>'model_tier' IS NOT DISTINCT FROM $2 "
            "  AND request->>'batch_id'   IS NOT DISTINCT FROM $3 "
            "ORDER BY created_at DESC LIMIT 1",
            request.get("tour_id"),
            request.get("model_tier"),
            request.get("batch_id"),
        )
        return str(dup_id) if dup_id is not None else None
    finally:
        await conn.close()
