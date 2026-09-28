"""AA-650 — SQL operations on shared.job (migration 174).

Every function takes an asyncpg pool. State changes are single UPDATE statements guarded by the
expected status (and, for a running job, by `locked_by`), so a worker that lost its lease can never
overwrite what the reaper or another worker already did.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

TERMINAL = ("succeeded", "failed", "stopped_budget", "cancelled")

# Serialises claims across processes so the per-kind concurrency cap is exact (two API tasks
# overlap during a rolling deploy). Arbitrary constant, only used here.
_CLAIM_LOCK_KEY = 650_001

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 900


@dataclass
class Job:
    id: str
    kind: str
    payload: dict
    status: str
    attempt: int
    max_attempts: int
    progress: dict
    cost_usd: float
    created_by: Optional[str] = None
    parent_job_id: Optional[str] = None


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else (v or {})


def _to_job(row) -> Job:
    return Job(
        id=str(row["id"]), kind=row["kind"], payload=_json(row["payload"]), status=row["status"],
        attempt=row["attempt"], max_attempts=row["max_attempts"], progress=_json(row["progress"]),
        cost_usd=float(row["cost_usd"] or 0), created_by=row["created_by"],
        parent_job_id=str(row["parent_job_id"]) if row["parent_job_id"] else None,
    )


def backoff_seconds(attempt: int) -> int:
    """Delay before retry number `attempt` (1-based): 30 s, 60 s, 120 s, ... capped at 15 min."""
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0), BACKOFF_MAX_SECONDS)


async def enqueue(pool, kind: str, payload: dict, *, idempotency_key: Optional[str] = None,
                  max_attempts: int = 3, parent_job_id: Optional[str] = None,
                  created_by: Optional[str] = None,
                  run_after: Optional[datetime] = None) -> tuple[str, bool]:
    """Returns (job_id, created). With an idempotency key already used, returns that job instead."""
    row = await pool.fetchrow(
        """
        INSERT INTO shared.job (kind, payload, idempotency_key, max_attempts, parent_job_id,
                                created_by, run_after)
        VALUES ($1, $2::jsonb, $3, $4, $5::uuid, $6, coalesce($7, now()))
        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
        RETURNING id
        """,
        kind, json.dumps(payload, default=str), idempotency_key, max_attempts, parent_job_id,
        created_by, run_after,
    )
    if row is not None:
        return str(row["id"]), True
    existing = await pool.fetchval("SELECT id FROM shared.job WHERE idempotency_key = $1",
                                   idempotency_key)
    return str(existing), False


async def claim(pool, worker_id: str, caps: dict[str, int], lease_seconds: int) -> Optional[Job]:
    """Claims the next due job of a kind in `caps` whose running count is below its cap."""
    if not caps:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _CLAIM_LOCK_KEY)
            row = await conn.fetchrow(
                """
                UPDATE shared.job SET status = 'running', attempt = attempt + 1,
                       locked_by = $1, locked_until = now() + make_interval(secs => $3),
                       started_at = coalesce(started_at, now()), updated_at = now()
                WHERE id = (
                    SELECT j.id FROM shared.job j
                    WHERE j.status = 'queued' AND j.run_after <= now() AND NOT j.cancel_requested
                      AND j.kind = ANY(SELECT jsonb_object_keys($2::jsonb))
                      AND (SELECT count(*) FROM shared.job r
                           WHERE r.kind = j.kind AND r.status = 'running')
                          < ($2::jsonb ->> j.kind)::int
                    ORDER BY j.run_after, j.created_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                worker_id, json.dumps(caps), lease_seconds,
            )
    return _to_job(row) if row else None


async def heartbeat(pool, job_id: str, worker_id: str, lease_seconds: int) -> tuple[bool, bool]:
    """Extends the lease. Returns (still_owned, cancel_requested)."""
    row = await pool.fetchrow(
        """
        UPDATE shared.job SET locked_until = now() + make_interval(secs => $3), updated_at = now()
        WHERE id = $1::uuid AND status = 'running' AND locked_by = $2
        RETURNING cancel_requested
        """,
        job_id, worker_id, lease_seconds,
    )
    return (row is not None, bool(row["cancel_requested"]) if row else False)


async def set_progress(pool, job_id: str, progress: dict, cost_usd: Optional[float] = None) -> None:
    await pool.execute(
        """
        UPDATE shared.job SET progress = progress || $2::jsonb,
               cost_usd = coalesce($3, cost_usd), updated_at = now()
        WHERE id = $1::uuid
        """,
        job_id, json.dumps(progress, default=str), cost_usd,
    )


async def _finish(pool, job_id: str, worker_id: str, status: str, *, result: Optional[dict],
                  error: Optional[str], cost_usd: Optional[float]) -> bool:
    row = await pool.fetchrow(
        """
        UPDATE shared.job SET status = $3, result = $4::jsonb, error = $5,
               cost_usd = coalesce($6, cost_usd), locked_by = NULL, locked_until = NULL,
               finished_at = now(), updated_at = now()
        WHERE id = $1::uuid AND status = 'running' AND locked_by = $2
        RETURNING id
        """,
        job_id, worker_id, status, json.dumps(result, default=str) if result is not None else None,
        error, cost_usd,
    )
    return row is not None


async def complete(pool, job_id: str, worker_id: str, result: Optional[dict],
                   cost_usd: Optional[float]) -> bool:
    return await _finish(pool, job_id, worker_id, "succeeded", result=result, error=None,
                         cost_usd=cost_usd)


async def stop_budget(pool, job_id: str, worker_id: str, error: str, result: Optional[dict],
                      cost_usd: Optional[float]) -> bool:
    return await _finish(pool, job_id, worker_id, "stopped_budget", result=result, error=error,
                         cost_usd=cost_usd)


async def mark_cancelled(pool, job_id: str, worker_id: str, cost_usd: Optional[float]) -> bool:
    return await _finish(pool, job_id, worker_id, "cancelled", result=None,
                         error="cancelled by admin", cost_usd=cost_usd)


async def fail(pool, job: Job, worker_id: str, error: str, *, retryable: bool,
               cost_usd: Optional[float], result: Optional[dict] = None) -> str:
    """Re-queues with backoff while attempts remain (and the error is retryable), else fails.
    Returns the new status, or '' when the job was no longer ours."""
    if retryable and job.attempt < job.max_attempts:
        row = await pool.fetchrow(
            """
            UPDATE shared.job SET status = 'queued', error = $3, cost_usd = coalesce($5, cost_usd),
                   run_after = now() + make_interval(secs => $4),
                   locked_by = NULL, locked_until = NULL, updated_at = now()
            WHERE id = $1::uuid AND status = 'running' AND locked_by = $2
            RETURNING id
            """,
            job.id, worker_id, error, backoff_seconds(job.attempt), cost_usd,
        )
        return "queued" if row else ""
    ok = await _finish(pool, job.id, worker_id, "failed", result=result, error=error,
                       cost_usd=cost_usd)
    return "failed" if ok else ""


async def release(pool, job_id: str, worker_id: str, cost_usd: Optional[float]) -> bool:
    """Graceful shutdown: back to the queue at once, and the interrupted attempt is not counted."""
    row = await pool.fetchrow(
        """
        UPDATE shared.job SET status = 'queued', attempt = greatest(attempt - 1, 0),
               cost_usd = coalesce($3, cost_usd), run_after = now(),
               locked_by = NULL, locked_until = NULL, updated_at = now(),
               progress = progress || '{"released": true}'::jsonb
        WHERE id = $1::uuid AND status = 'running' AND locked_by = $2
        RETURNING id
        """,
        job_id, worker_id, cost_usd,
    )
    return row is not None


async def reap(pool) -> dict:
    """Jobs whose lease expired (the worker died): re-queue while attempts remain, else fail."""
    requeued = await pool.fetch(
        """
        UPDATE shared.job SET status = 'queued', run_after = now(), locked_by = NULL,
               locked_until = NULL, updated_at = now(),
               error = 'lease expired (worker stopped); re-queued'
        WHERE status = 'running' AND locked_until < now() AND attempt < max_attempts
          AND NOT cancel_requested
        RETURNING id
        """
    )
    failed = await pool.fetch(
        """
        UPDATE shared.job SET status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
               error = CASE WHEN cancel_requested THEN 'cancelled by admin'
                            ELSE 'lease expired (worker stopped); no attempts left' END,
               locked_by = NULL, locked_until = NULL, finished_at = now(), updated_at = now()
        WHERE status = 'running' AND locked_until < now()
        RETURNING id
        """
    )
    return {"requeued": [str(r["id"]) for r in requeued], "failed": [str(r["id"]) for r in failed]}


async def request_cancel(pool, job_id: str) -> Optional[str]:
    """Queued jobs are cancelled at once; a running job is flagged and its worker stops it."""
    row = await pool.fetchrow(
        """
        UPDATE shared.job SET
            status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
            finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END,
            error = CASE WHEN status = 'queued' THEN 'cancelled by admin' ELSE error END,
            cancel_requested = true, updated_at = now()
        WHERE id = $1::uuid AND status IN ('queued', 'running')
        RETURNING status
        """,
        job_id,
    )
    return row["status"] if row else None


async def retry(pool, job_id: str) -> bool:
    """A finished-unsuccessfully job gets a fresh set of attempts."""
    row = await pool.fetchrow(
        """
        UPDATE shared.job SET status = 'queued', attempt = 0, run_after = now(), error = NULL,
               cancel_requested = false, finished_at = NULL, result = NULL, updated_at = now()
        WHERE id = $1::uuid AND status IN ('failed', 'stopped_budget', 'cancelled')
        RETURNING id
        """,
        job_id,
    )
    return row is not None


_LIST_COLUMNS = (
    "id::text AS id, kind, status, attempt, max_attempts, payload, progress, result, "
    "cost_usd::float AS cost_usd, error, cancel_requested, created_by, parent_job_id::text AS "
    "parent_job_id, locked_by, locked_until, run_after, created_at, started_at, finished_at"
)


def _row_dict(row) -> dict:
    d = dict(row)
    for k in ("payload", "progress", "result"):
        d[k] = _json(d[k]) if d[k] is not None else None
    return d


async def get(pool, job_id: str) -> Optional[dict]:
    row = await pool.fetchrow(f"SELECT {_LIST_COLUMNS} FROM shared.job WHERE id = $1::uuid", job_id)
    return _row_dict(row) if row else None


async def list_jobs(pool, *, kind: Optional[str] = None, status: Optional[str] = None,
                    limit: int = 50) -> list[dict]:
    rows = await pool.fetch(
        f"""
        SELECT {_LIST_COLUMNS} FROM shared.job
        WHERE ($1::text IS NULL OR kind = $1) AND ($2::text IS NULL OR status = $2)
        ORDER BY created_at DESC LIMIT $3
        """,
        kind, status, limit,
    )
    return [_row_dict(r) for r in rows]


async def latest(pool, kind: str, statuses: tuple = ()) -> Optional[dict]:
    rows = await pool.fetch(
        f"""
        SELECT {_LIST_COLUMNS} FROM shared.job
        WHERE kind = $1 AND (cardinality($2::text[]) = 0 OR status = ANY($2::text[]))
        ORDER BY created_at DESC LIMIT 1
        """,
        kind, list(statuses),
    )
    return _row_dict(rows[0]) if rows else None
