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

import asyncpg

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


# A job whose worker keeps stopping mid-run (e.g. the process is replaced each time the job runs)
# is failed after this many releases instead of looping forever — a release does not count as an
# attempt, so without this cap such a job would never end.
MAX_RELEASES = 3


async def release(pool, job_id: str, worker_id: str, cost_usd: Optional[float]) -> str:
    """Graceful shutdown: back to the queue at once, and the interrupted attempt is not counted.
    Returns the new status: 'queued', 'cancelled' (an admin asked to cancel it), 'failed'
    (released MAX_RELEASES times), or '' when the job was no longer ours."""
    row = await pool.fetchrow(
        """
        WITH cur AS (
            SELECT coalesce((progress->>'releases')::int, 0) + 1 AS releases, cancel_requested
            FROM shared.job WHERE id = $1::uuid
        )
        UPDATE shared.job j SET
            status = CASE WHEN cur.cancel_requested THEN 'cancelled'
                          WHEN cur.releases >= $4 THEN 'failed' ELSE 'queued' END,
            attempt = greatest(j.attempt - 1, 0),
            error = CASE WHEN cur.cancel_requested THEN 'cancelled by admin'
                         WHEN cur.releases >= $4 THEN 'stopped mid-run ' || cur.releases
                              || ' times (the worker process keeps stopping while this job runs)'
                         ELSE j.error END,
            finished_at = CASE WHEN cur.cancel_requested OR cur.releases >= $4 THEN now()
                               ELSE j.finished_at END,
            cost_usd = coalesce($3, j.cost_usd), run_after = now(),
            locked_by = NULL, locked_until = NULL, updated_at = now(),
            progress = j.progress || jsonb_build_object('released', true, 'releases', cur.releases)
        FROM cur
        WHERE j.id = $1::uuid AND j.status = 'running' AND j.locked_by = $2
        RETURNING j.status
        """,
        job_id, worker_id, cost_usd, MAX_RELEASES,
    )
    return row["status"] if row else ""


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


# Read-side job cost (AA-652 follow-up): `shared.job.cost_usd` holds what the handler reported
# itself (non-LLM spend such as DataForSEO, via ctx.add_cost); LLM spend is summed from
# shared.llm_call_log rows the worker tagged with the job id. Summing on read keeps it exact across
# retries and late fire-and-forget log writes, with nothing to double count.
LLM_COST_SQL = (
    "(SELECT coalesce(sum(l.cost_usd), 0) FROM shared.llm_call_log l WHERE l.job_id = j.id)"
)

_LIST_COLUMNS = (
    "j.id::text AS id, kind, status, attempt, max_attempts, payload, progress, result, "
    f"(j.cost_usd + {LLM_COST_SQL})::float AS cost_usd, {LLM_COST_SQL}::float AS llm_cost_usd, "
    "error, cancel_requested, created_by, parent_job_id::text AS "
    "parent_job_id, locked_by, locked_until, run_after, created_at, started_at, finished_at"
)


def _row_dict(row) -> dict:
    d = dict(row)
    for k in ("payload", "progress", "result"):
        d[k] = _json(d[k]) if d[k] is not None else None
    return d


async def get(pool, job_id: str) -> Optional[dict]:
    row = await pool.fetchrow(f"SELECT {_LIST_COLUMNS} FROM shared.job j WHERE j.id = $1::uuid", job_id)
    return _row_dict(row) if row else None


# AA-755 — server-side sort whitelist. The page only ever asks for one of these keys; the value is
# a trusted SQL expression (never built from user input), so the column name can never be injected.
# `id` is appended as a final tie-break on every sort so OFFSET pages are stable across equal keys.
# Duration = finished_at - started_at (a running/queued job has no end, so it sorts NULLs last).
# Cost reuses the same read-side expression the list already shows (j.cost_usd + summed LLM log).
JOB_SORTS: dict[str, str] = {
    "created_at": "j.created_at",
    "started_at": "j.started_at",
    "finished_at": "j.finished_at",
    "kind": "j.kind",
    "status": "j.status",
    "attempt": "j.attempt",
    "cost_usd": f"(j.cost_usd + {LLM_COST_SQL})",
    "duration": "(j.finished_at - j.started_at)",
}

# Shared WHERE for list + count so the paged total matches exactly what the page can scroll through.
_LIST_WHERE = (
    "($1::text IS NULL OR kind = $1) AND ($2::text IS NULL OR status = $2) "
    "AND ($3::text IS NULL OR payload->>'tour_id' = $3 OR payload->>'published_tour_id' = $3)"
)


def _order_by(sort: str, sort_dir: str) -> str:
    """SQL ORDER BY for a whitelisted `sort` key. Raises ValueError on an unknown key so the router
    can answer 422 (never silently fall back, which would hide a client bug)."""
    expr = JOB_SORTS.get(sort)
    if expr is None:
        raise ValueError(f"unknown sort key: {sort!r}")
    direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
    nulls = "NULLS FIRST" if direction == "ASC" else "NULLS LAST"
    # j.id tie-break keeps OFFSET pages stable when the primary key ties.
    return f"{expr} {direction} {nulls}, j.id {direction}"


async def list_jobs(pool, *, kind: Optional[str] = None, status: Optional[str] = None,
                    limit: int = 50, offset: int = 0, tour_id: Optional[str] = None,
                    sort: str = "created_at", sort_dir: str = "desc") -> list[dict]:
    """`tour_id` (AA-687) matches the payload's tour: a3_atomize / segment work (`tour_id`) and
    tenant rewrites of it (t2_rewrite's `published_tour_id`). AA-755: `sort`/`sort_dir` select a
    whitelisted ORDER BY (unknown key → ValueError), `offset`/`limit` page the result."""
    order_by = _order_by(sort, sort_dir)
    rows = await pool.fetch(
        f"""
        SELECT {_LIST_COLUMNS} FROM shared.job j
        WHERE {_LIST_WHERE}
        ORDER BY {order_by} LIMIT $4 OFFSET $5
        """,
        kind, status, tour_id, limit, offset,
    )
    return [_row_dict(r) for r in rows]


async def count_jobs(pool, *, kind: Optional[str] = None, status: Optional[str] = None,
                     tour_id: Optional[str] = None) -> int:
    """AA-755 — total rows matching the same filters as list_jobs, for the page's pagination total."""
    return int(await pool.fetchval(
        f"SELECT count(*)::int FROM shared.job j WHERE {_LIST_WHERE}",
        kind, status, tour_id,
    ))


async def latest(pool, kind: str, statuses: tuple = ()) -> Optional[dict]:
    rows = await pool.fetch(
        f"""
        SELECT {_LIST_COLUMNS} FROM shared.job j
        WHERE kind = $1 AND (cardinality($2::text[]) = 0 OR status = ANY($2::text[]))
        ORDER BY created_at DESC LIMIT 1
        """,
        kind, list(statuses),
    )
    return _row_dict(rows[0]) if rows else None


async def active_count(pool) -> dict:
    """AA-725 — queued + running jobs right now, with NO time filter, for the pre-deploy guard.
    A deploy restarts the containers; any job in these two states would be interrupted (re-queued
    by the reaper, or lost if in-process), so CI blocks the deploy while this is non-zero."""
    rows = await pool.fetch(
        "SELECT status, count(*)::int AS n FROM shared.job "
        "WHERE status IN ('queued', 'running') GROUP BY status"
    )
    by_status = {r["status"]: r["n"] for r in rows}
    return {"active": sum(by_status.values()), "by_status": by_status}


# ── worker liveness (AA-687, migration 178) ─────────────────────────────────────────────────────
# Read by the Jobs page only; the queue functions above never depend on it.

WORKER_ALIVE_SECONDS = 60  # a worker writes about every 15 s; silent for 60 s = gone


async def worker_register(pool, worker_id: str, host: str, max_parallel: int, caps: dict,
                          task_revision: Optional[int] = None) -> None:
    """A worker's start: its row, plus pruning rows of workers stopped/silent for 7 days."""
    await pool.execute(
        """
        INSERT INTO shared.job_worker (worker_id, host, max_parallel, caps, task_revision)
        VALUES ($1, $2, $3, $4::jsonb, $5)
        ON CONFLICT (worker_id) DO UPDATE SET last_seen_at = now(), stopped_at = NULL,
            max_parallel = excluded.max_parallel, caps = excluded.caps,
            task_revision = excluded.task_revision
        """,
        worker_id, host, max_parallel, json.dumps(caps), task_revision,
    )
    await pool.execute("DELETE FROM shared.job_worker WHERE last_seen_at < now() - interval '7 days'")


async def worker_beat(pool, worker_id: str, running_jobs: int,
                      reaped: Optional[dict] = None) -> None:
    """Liveness + running count; `reaped` (from reap()) is added to the totals when non-empty."""
    requeued = len((reaped or {}).get("requeued", []))
    failed = len((reaped or {}).get("failed", []))
    await pool.execute(
        """
        UPDATE shared.job_worker SET last_seen_at = now(), running_jobs = $2,
            reaped_requeued = reaped_requeued + $3, reaped_failed = reaped_failed + $4,
            last_reap_at = CASE WHEN $3 + $4 > 0 THEN now() ELSE last_reap_at END,
            last_reaped = CASE WHEN $3 + $4 > 0 THEN $5::jsonb ELSE last_reaped END
        WHERE worker_id = $1
        """,
        worker_id, running_jobs, requeued, failed, json.dumps(reaped or {}),
    )


NEWER_WORKER_SECONDS = 45  # a newer worker must have written its row this recently to count


async def newer_worker_alive(pool, task_revision: Optional[int]) -> bool:
    """AA-711 — is a live worker of a newer ECS task-definition revision running? Then this one is
    on a draining task after a deploy and must stop claiming. Unknown revision: never yields."""
    if task_revision is None:
        return False
    return bool(await pool.fetchval(
        f"""SELECT EXISTS (SELECT 1 FROM shared.job_worker
                            WHERE stopped_at IS NULL AND task_revision > $1
                              AND last_seen_at > now() - interval '{NEWER_WORKER_SECONDS} seconds')""",
        task_revision))


async def worker_stopped(pool, worker_id: str) -> None:
    await pool.execute(
        "UPDATE shared.job_worker SET stopped_at = now(), last_seen_at = now(), running_jobs = 0 "
        "WHERE worker_id = $1",
        worker_id,
    )


async def worker_health(pool) -> dict:
    """Workers seen in the last 7 days, running jobs per worker and kind, and queue depth."""
    workers = await pool.fetch(
        f"""
        SELECT worker_id, host, started_at, last_seen_at, stopped_at, max_parallel, caps, task_revision,
               running_jobs, reaped_requeued, reaped_failed, last_reap_at, last_reaped,
               (stopped_at IS NULL AND last_seen_at > now() - interval '{WORKER_ALIVE_SECONDS} seconds')
                   AS alive
        FROM shared.job_worker ORDER BY last_seen_at DESC LIMIT 20
        """
    )
    running = await pool.fetch(
        """
        SELECT locked_by AS worker_id, kind, count(*)::int AS n,
               min(started_at) AS oldest_started_at
        FROM shared.job WHERE status = 'running' GROUP BY locked_by, kind
        """
    )
    queued = await pool.fetch(
        """
        SELECT kind, count(*)::int AS n,
               count(*) FILTER (WHERE run_after <= now())::int AS ready,
               min(created_at) AS oldest_created_at
        FROM shared.job WHERE status = 'queued' GROUP BY kind
        """
    )
    out = []
    for w in workers:
        d = dict(w)
        for k in ("caps", "last_reaped"):
            d[k] = _json(d[k]) if d[k] is not None else None
        out.append(d)
    return {"workers": out, "running": [dict(r) for r in running],
            "queued": [dict(r) for r in queued], "alive_seconds": WORKER_ALIVE_SECONDS}


async def llm_calls(pool, job_id: str, limit: int = 500) -> dict:
    """The shared.llm_call_log rows tagged with this job (AA-652 follow-up, migration 177)."""
    rows = await pool.fetch(
        """
        SELECT created_at, stage, role, model, account, provider, fallback_used,
               tokens_in, tokens_out, cost_usd::float AS cost_usd,
               (quality_signal->>'texts')::int AS texts
        FROM shared.llm_call_log WHERE job_id = $1::uuid
        ORDER BY created_at DESC LIMIT $2
        """,
        job_id, limit,
    )
    totals = await pool.fetchrow(
        """
        SELECT count(*)::int AS calls, coalesce(sum(cost_usd), 0)::float AS cost_usd,
               coalesce(sum(tokens_in), 0)::bigint AS tokens_in,
               coalesce(sum(tokens_out), 0)::bigint AS tokens_out,
               min(created_at) AS first_at, max(created_at) AS last_at
        FROM shared.llm_call_log WHERE job_id = $1::uuid
        """,
        job_id,
    )
    by_stage = await pool.fetch(
        """
        SELECT stage, model, count(*)::int AS calls, coalesce(sum(cost_usd), 0)::float AS cost_usd
        FROM shared.llm_call_log WHERE job_id = $1::uuid
        GROUP BY stage, model ORDER BY cost_usd DESC
        """,
        job_id,
    )
    return {"calls": [dict(r) for r in rows], "totals": dict(totals),
            "by_stage": [dict(r) for r in by_stage], "truncated": totals["calls"] > limit}


async def domain_links(pool, job_id: str, payload: Optional[dict]) -> dict:
    """What the job wrote: tenant tour versions and content pieces tagged with its id (migrations
    175/176), plus the tour it atomized/researched when the payload names one. A table that does
    not exist (a fresh test database) gives an empty list, not an error."""
    async def _rows(sql: str) -> list[dict]:
        try:
            return [dict(r) for r in await pool.fetch(sql, job_id)]
        except asyncpg.UndefinedTableError:
            return []

    versions = await _rows(
        "SELECT id::text AS version_id, published_tour_id::text AS published_tour_id, "
        "tenant_id::text AS tenant_id, version_number, status "
        "FROM gold_aa_internal.tenant_tour_versions WHERE job_id = $1::uuid LIMIT 20")
    pieces = await _rows(
        "SELECT piece_id::text AS piece_id, tenant_id::text AS tenant_id, status "
        "FROM acp_shared.content_piece WHERE job_id = $1::uuid LIMIT 20")
    p = payload or {}
    return {"tour_versions": versions, "content_pieces": pieces,
            "tour_id": p.get("tour_id"), "version_id": p.get("version_id")}
