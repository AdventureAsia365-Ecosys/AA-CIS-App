"""AA-650 — SQL operations on shared.job (migration 174).

Every function takes an asyncpg pool. State changes are single UPDATE statements guarded by the
expected status (and, for a running job, by `locked_by`), so a worker that lost its lease can never
overwrite what the reaper or another worker already did.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

import asyncpg

TERMINAL = ("succeeded", "failed", "stopped_budget", "cancelled")

# Serialises claims across processes so the per-kind concurrency cap is exact (two API tasks
# overlap during a rolling deploy). Arbitrary constant, only used here.
_CLAIM_LOCK_KEY = 650_001

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 900
# AA-1018: up to this share of the delay is added at random, so jobs that failed together (a provider outage, a
# blocked account) do not all come back in the same second.
BACKOFF_JITTER_RATIO = 0.25


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


def backoff_seconds(attempt: int, *, rand: Optional[Callable[[], float]] = None) -> int:
    """Delay before retry number `attempt` (1-based): 30 s, 60 s, 120 s, ... capped at 15 min, plus a random jitter of
    up to BACKOFF_JITTER_RATIO of that delay (AA-1018; `rand` returns 0..1, pass `lambda: 0.0` for the bare delay).
    Never shorter than the bare delay."""
    base = min(BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0), BACKOFF_MAX_SECONDS)
    r = (rand or random.random)()
    return base + int(base * BACKOFF_JITTER_RATIO * min(max(r, 0.0), 1.0))


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
    """Claims the next due job of a kind in `caps` whose running count is below its cap.

    `started_at` keeps the FIRST attempt's start; AA-751 (plan F18) also merges
    `progress.attempt_started_at` = now() on every claim, so the overdue-running metric
    (shared/jobs/backlog.py) measures the current attempt, not a re-claimed job's first one."""
    if not caps:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _CLAIM_LOCK_KEY)
            row = await conn.fetchrow(
                """
                UPDATE shared.job SET status = 'running', attempt = attempt + 1,
                       locked_by = $1, locked_until = now() + make_interval(secs => $3),
                       started_at = coalesce(started_at, now()), updated_at = now(),
                       progress = coalesce(progress, '{}'::jsonb)
                                  || jsonb_build_object('attempt_started_at', now())
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
# attempt, so without this cap such a job would never end. AA-993 G1: a release while a worker of a newer
# task revision is alive (a deploy) is not counted here (release(count_release=False)).
MAX_RELEASES = 3

# AA-993 G1: every release on shutdown appends {at, counted, worker} to progress.release_log (the last
# RELEASE_LOG_KEEP entries are kept), read by GET /admin/job-runner/released. No new column: shared.job is a hot table
# (critic 6: an ADD COLUMN would break the running `RETURNING *` claim), and progress is in the shared job template.
RELEASE_LOG_KEEP = 20


async def release(pool, job_id: str, worker_id: str, cost_usd: Optional[float], *,
                  count_release: bool = True, delay_seconds: int = 0) -> str:
    """Graceful shutdown: back to the queue, and the interrupted attempt is not counted.

    `count_release` (AA-993 G1): False when a worker of a newer task revision is alive, i.e. a deploy replaced
    this worker; then `progress.releases` (the MAX_RELEASES count) is unchanged, so deploys never fail a job.
    `delay_seconds`: the job is due again that much later (the kind's release_delay_seconds).
    Returns the new status: 'queued', 'cancelled' (an admin asked to cancel it), 'failed'
    (a counted release reached MAX_RELEASES), or '' when the job was no longer ours."""
    row = await pool.fetchrow(
        """
        WITH cur AS (
            SELECT coalesce((progress->>'releases')::int, 0) + CASE WHEN $5 THEN 1 ELSE 0 END AS releases,
                   cancel_requested,
                   CASE WHEN jsonb_typeof(progress->'release_log') = 'array' THEN progress->'release_log'
                        ELSE '[]'::jsonb END
                   || jsonb_build_array(jsonb_build_object('at', now(), 'counted', $5::boolean,
                                                           'worker', $2::text)) AS log
            FROM shared.job WHERE id = $1::uuid
        ), kept AS (
            SELECT coalesce(jsonb_agg(e ORDER BY o), '[]'::jsonb) AS log
            FROM cur, jsonb_array_elements(cur.log) WITH ORDINALITY AS t(e, o)
            WHERE o > jsonb_array_length(cur.log) - $7
        )
        UPDATE shared.job j SET
            status = CASE WHEN cur.cancel_requested THEN 'cancelled'
                          WHEN $5 AND cur.releases >= $4 THEN 'failed' ELSE 'queued' END,
            attempt = greatest(j.attempt - 1, 0),
            error = CASE WHEN cur.cancel_requested THEN 'cancelled by admin'
                         WHEN $5 AND cur.releases >= $4 THEN 'stopped mid-run ' || cur.releases
                              || ' times (the worker process keeps stopping while this job runs)'
                         ELSE j.error END,
            finished_at = CASE WHEN cur.cancel_requested OR ($5 AND cur.releases >= $4) THEN now()
                               ELSE j.finished_at END,
            cost_usd = coalesce($3, j.cost_usd), run_after = now() + make_interval(secs => $6),
            locked_by = NULL, locked_until = NULL, updated_at = now(),
            progress = j.progress || jsonb_build_object(
                'released', true, 'releases', cur.releases, 'last_released_at', now(),
                'shutdown_releases', coalesce((j.progress->>'shutdown_releases')::int, 0) + 1,
                'release_log', kept.log)
        FROM cur, kept
        WHERE j.id = $1::uuid AND j.status = 'running' AND j.locked_by = $2
        RETURNING j.status
        """,
        job_id, worker_id, cost_usd, MAX_RELEASES, bool(count_release), max(int(delay_seconds or 0), 0),
        RELEASE_LOG_KEEP,
    )
    return row["status"] if row else ""


async def released_since(pool, since: datetime) -> dict:
    """AA-993 G1 — releases on shutdown at or after `since`, from progress.release_log: the total, how many counted
    toward MAX_RELEASES (no newer worker was alive), the distinct jobs, and the split per kind."""
    rows = await pool.fetch(
        """
        SELECT j.kind, count(*)::int AS released,
               count(*) FILTER (WHERE coalesce((e->>'counted')::boolean, true))::int AS counted,
               count(DISTINCT j.id)::int AS jobs
        FROM shared.job j
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(j.progress->'release_log') = 'array' THEN j.progress->'release_log'
                 ELSE '[]'::jsonb END) AS e
        WHERE j.updated_at >= $1 AND (e->>'at')::timestamptz >= $1
        GROUP BY j.kind ORDER BY j.kind
        """,
        since,
    )
    by_kind = {r["kind"]: {"released": r["released"], "counted": r["counted"], "jobs": r["jobs"]} for r in rows}
    return {"released": sum(v["released"] for v in by_kind.values()),
            "counted": sum(v["counted"] for v in by_kind.values()),
            "jobs": sum(v["jobs"] for v in by_kind.values()),
            "by_kind": by_kind}


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


async def cancel_retired_kinds(pool, retired: dict[str, str]) -> int:
    """Cancel every queued/running row of the retired kinds ({kind: issue}); returns the count.
    The lock is cleared too, so a worker still finishing such a row cannot overwrite the status
    (_finish matches on locked_by). Idempotent; a no-op when nothing matches."""
    if not retired:
        return 0
    status = await pool.execute(
        """
        UPDATE shared.job SET status = 'cancelled',
               error = 'kind removed (' || ($2::jsonb ->> kind) || ')',
               locked_by = NULL, locked_until = NULL, finished_at = now(), updated_at = now()
        WHERE kind = ANY($1::text[]) AND status IN ('queued', 'running')
        """,
        list(retired), json.dumps(retired),
    )
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


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


# ── AA-1018: re-queue the jobs an LLM account block failed, once the connection answers again ───────────────────────

RETRY_REASON_DEFAULT = "llm_access_denied"      # errors.LLM_ACCESS_DENIED_REASON (the job error prefix, AA-900)
RETRY_REASON_LIMIT = 200                        # rows read per call (one page; call again for more)
RETRY_REASON_MAX_LIMIT = 500                    # a dry run may list this many
RETRY_REASON_APPLY_LIMIT = 50                   # an apply re-queues at most this many per call (paid runs: subset)
RETRY_SPREAD_SECONDS = 60                       # re-queued rows are due over this window, not all in one second

_RETRY_CANDIDATES_SQL = """
    SELECT j.id::text AS id, j.kind, j.payload, j.error, j.finished_at
      FROM shared.job j
     WHERE j.status = 'failed' AND j.error IS NOT NULL AND left(j.error, length($1)) = $1
       AND j.finished_at >= $2
       AND (cardinality($3::text[]) = 0 OR j.kind = ANY($3::text[]))
     ORDER BY j.finished_at, j.id
     LIMIT $4
"""
_RETRY_ONE_SQL = """
    UPDATE shared.job SET status = 'queued', attempt = 0, run_after = now() + make_interval(secs => $2),
           error = NULL, cancel_requested = false, finished_at = NULL, result = NULL, updated_at = now()
     WHERE id = $1::uuid AND status = 'failed'
    RETURNING id
"""
_CONNECTION_KEYS_SQL = "SELECT connection_key FROM shared.llm_provider_connection"


async def blocked_connections(pool, now: Optional[datetime] = None) -> tuple[dict[str, str], list[str]]:
    """({connection_key: class} of the LLM connections whose circuit still refuses, every registry connection key).
    Blocked = ``breaker.connection_refusing``: the connection circuit is tripped (open, or half-open of any class) and
    no call on the connection has answered since the refusals (read from shared.llm_provider_health). A half-open
    access_denied circuit lets calls probe, but a bulk re-queue waits for a real answer. Before migrations 304 / 309:
    ({}, [])."""
    from shared.llm_client import breaker  # lazy: keeps the job queue import-light

    try:
        health = [breaker.row_to_health(r) for r in await pool.fetch(breaker.SELECT_SQL)]
        keys = [r["connection_key"] for r in await pool.fetch(_CONNECTION_KEYS_SQL)]
    except asyncpg.exceptions.UndefinedTableError:
        return {}, []
    now = now or datetime.now(timezone.utc)
    out: dict[str, str] = {}
    for key in sorted({h.connection_key for h in health}):
        h = breaker.connection_health(health, key, now)
        if breaker.connection_refusing(h, now):
            out[key] = h.last_error_class
    return out, keys


def _named_connections(error: str, keys: Sequence[str]) -> list[str]:
    """The registry connection keys a job error names (whole words: `acc3` does not match `acc30`)."""
    text = error or ""
    return [k for k in keys if re.search(rf"(?<![A-Za-z0-9-]){re.escape(k)}(?![A-Za-z0-9-])", text)]


def llm_hold(error: str, blocked: dict[str, str], keys: Sequence[str]) -> Optional[str]:
    """Why a job refused by an LLM account must not be re-queued yet, or None. A job whose error names a connection
    waits for that connection; one that names none (a Bedrock "Error 002" says no connection) waits while any
    connection is blocked with the same class (access_denied / quota_exhausted). Pure."""
    from shared.llm_client.errors import classify_text  # lazy, pure

    named = _named_connections(error, keys)
    if named:
        held = [f"{k} ({blocked[k]})" for k in named if k in blocked]
        return f"connection still refused: {', '.join(held)}" if held else None
    cls = classify_text(error)
    same = [f"{k} ({c})" for k, c in sorted(blocked.items()) if c == cls]
    return f"a connection is still refused with the same class: {', '.join(same)}" if same else None


async def retry_by_reason(pool, prefix: str = RETRY_REASON_DEFAULT, *, since: datetime, kinds: Sequence[str] = (),
                          dry_run: bool = True, limit: int = RETRY_REASON_LIMIT,
                          rand: Optional[Callable[[], float]] = None) -> dict:
    """AA-1018: re-queue the `failed` jobs whose error starts with `prefix` (default `llm_access_denied`, the
    account-level LLM refusal of AA-900) and that finished at or after `since`, optionally only `kinds`. Each job gets
    a fresh set of attempts, like `retry`, due within RETRY_SPREAD_SECONDS (jitter). Skipped (in `held`, with the
    reason): a kind that is not registered, a kind whose `retry_refusal` refuses the job (AA-839), and a job whose
    connection is still refused (`blocked_connections` / `llm_hold`; only for the LLM prefix). `dry_run` (the
    default) changes nothing and lists what an apply would do (up to RETRY_REASON_MAX_LIMIT rows); an apply reads at
    most RETRY_REASON_APPLY_LIMIT rows per call (call again for the next ones). The apply re-checks every row in its
    own guarded UPDATE (`status = 'failed'`), so a job retried by hand meanwhile is not touched twice."""
    from shared.jobs.registry import get_kind  # lazy: registry imports this module
    from shared.jobs.worker import load_kinds

    if not prefix or not prefix.strip():
        raise ValueError("prefix must not be empty")
    limit = max(1, min(int(limit), RETRY_REASON_MAX_LIMIT if dry_run else RETRY_REASON_APPLY_LIMIT))
    load_kinds()
    rows = [dict(r) for r in await pool.fetch(_RETRY_CANDIDATES_SQL, prefix, since, list(kinds), limit)]
    llm = prefix.startswith(RETRY_REASON_DEFAULT)
    blocked, keys = await blocked_connections(pool) if llm else ({}, [])
    eligible, held = [], []
    for r in rows:
        kind = get_kind(r["kind"])
        entry = {"id": r["id"], "kind": r["kind"], "error": (r["error"] or "")[:200],
                 "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None}
        why: Optional[str]
        if kind is None:
            why = "kind is not registered"
        else:
            why = kind.retry_refusal({**r, "payload": _json(r["payload"])}) if kind.retry_refusal else None
            if not why and llm:
                why = llm_hold(r["error"] or "", blocked, keys)
        if why:
            held.append({**entry, "held": why})
        else:
            eligible.append(entry)
    requeued: list[str] = []
    if not dry_run:
        r_fn = rand or random.random
        for e in eligible:
            delay = int(RETRY_SPREAD_SECONDS * min(max(r_fn(), 0.0), 1.0))
            if await pool.fetchrow(_RETRY_ONE_SQL, e["id"], delay) is not None:
                requeued.append(e["id"])
    return {"dry_run": dry_run, "prefix": prefix, "since": since.isoformat(), "kinds": list(kinds), "limit": limit,
            "matched": len(rows), "eligible": len(eligible), "held_count": len(held), "requeued": len(requeued),
            "requeued_ids": requeued, "jobs": eligible, "held": held, "blocked_connections": blocked,
            "more": len(rows) == limit}


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


# S241 — the Jobs page list polls; payload/result can be large JSON (and only the drawer, which
# reads one job via `get`, shows them). `fields="summary"` selects them as NULL so the response
# keeps the same keys but carries no blobs. `progress` stays: the list's progress column reads it.
# AA-993 G2 review: the s1_rewrite checkpoint payloads (`s1_writer_state`, `s1_rewrite_result`, tens of KB of draft
# text) stay out of the polled list too; the drawer's get() still returns the whole progress.
_SUMMARY_COLUMNS = _LIST_COLUMNS.replace(
    "payload, progress, result,",
    "NULL::jsonb AS payload, (j.progress - 's1_writer_state' - 's1_rewrite_result') AS progress, "
    "NULL::jsonb AS result,",
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
    "created_by": "j.created_by",
    "cost_usd": f"(j.cost_usd + {LLM_COST_SQL})",
    "duration": "(j.finished_at - j.started_at)",
}

# Shared WHERE for list + count so the paged total matches exactly what the page can scroll through.
# Built once from the same filter set (AA-755: + since/until/created_by/q) so `total` always counts
# exactly the rows the list pages through. The $N order is fixed and shared by both queries; list
# appends its own LIMIT/OFFSET params ($9/$10) after these (AA-1018: + reason, $8).
_LIST_WHERE = (
    "($1::text IS NULL OR kind = $1) "
    "AND ($2::text IS NULL OR status = $2) "
    "AND ($3::text IS NULL OR payload->>'tour_id' = $3 OR payload->>'published_tour_id' = $3) "
    "AND ($4::timestamptz IS NULL OR j.created_at >= $4) "
    "AND ($5::timestamptz IS NULL OR j.created_at < $5) "
    "AND ($6::text IS NULL OR j.created_by = $6) "
    # q: free-text match on id prefix, kind substring, or created_by substring (case-insensitive).
    "AND ($7::text IS NULL OR j.id::text LIKE $7 || '%' "
    "     OR j.kind ILIKE '%' || $7 || '%' OR j.created_by ILIKE '%' || $7 || '%') "
    # AA-1018 reason: the error starts with this text (e.g. llm_access_denied); left() so no LIKE wildcard applies.
    "AND ($8::text IS NULL OR left(j.error, length($8)) = $8)"
)


def _filter_args(kind, status, tour_id, since, until, created_by, q, reason=None) -> list:
    """The positional args for `_LIST_WHERE`, in a fixed order shared by list_jobs + count_jobs."""
    return [kind, status, tour_id, since, until, created_by, q, reason]


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
                    since: Optional[datetime] = None, until: Optional[datetime] = None,
                    created_by: Optional[str] = None, q: Optional[str] = None,
                    sort: str = "created_at", sort_dir: str = "desc",
                    include_blobs: bool = True, reason: Optional[str] = None) -> list[dict]:
    """`tour_id` (AA-687) matches the payload's tour: a3_atomize / segment work (`tour_id`) and
    tenant rewrites of it (t2_rewrite's `published_tour_id`). AA-755: `since`/`until` bound
    created_at, `created_by` is an exact match, `q` is a free-text match on id prefix / kind /
    created_by; `sort`/`sort_dir` select a whitelisted ORDER BY (unknown key → ValueError),
    `offset`/`limit` page the result. `include_blobs=False` (S241) returns payload/result as None. AA-1018 `reason`:
    the error starts with it (e.g. `llm_access_denied`)."""
    order_by = _order_by(sort, sort_dir)
    args = _filter_args(kind, status, tour_id, since, until, created_by, q, reason)
    columns = _LIST_COLUMNS if include_blobs else _SUMMARY_COLUMNS
    rows = await pool.fetch(
        f"""
        SELECT {columns} FROM shared.job j
        WHERE {_LIST_WHERE}
        ORDER BY {order_by} LIMIT $9 OFFSET $10
        """,
        *args, limit, offset,
    )
    return [_row_dict(r) for r in rows]


async def count_jobs(pool, *, kind: Optional[str] = None, status: Optional[str] = None,
                     tour_id: Optional[str] = None, since: Optional[datetime] = None,
                     until: Optional[datetime] = None, created_by: Optional[str] = None,
                     q: Optional[str] = None, reason: Optional[str] = None) -> int:
    """AA-755 — total rows matching the same filters as list_jobs, for the page's pagination total."""
    args = _filter_args(kind, status, tour_id, since, until, created_by, q, reason)
    return int(await pool.fetchval(
        f"SELECT count(*)::int FROM shared.job j WHERE {_LIST_WHERE}",
        *args,
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


# AA-990: a queued row due more than this far ahead is not interrupted by a deploy (it sits in the table until its
# run_after; whichever image is running then claims it), so the deploy guard does not count it. Every scheduled
# run_after in the runner is <= 20 min (debounce cap, retry backoff <= 15 min, staff invite delay) except the
# place_geocode run queued for the next Geoapify credit reset (up to ~24 h ahead), which would otherwise block every
# deploy until 00:10 UTC.
DEPLOY_GATE_HORIZON_S = 3600


async def active_count(pool, exempt_kinds: Sequence[str] = ()) -> dict:
    """AA-725 — queued + running jobs right now, with NO lower time bound. Since AA-993 G5 the deploy gate reports
    this as information only (the decision is `deploy_blockers`): a deploy releases the running ones and the new
    revision re-claims them; queued ones are claimed by whichever image is live.
    AA-832: rows of `exempt_kinds` (kinds registered with deploy_gate_exempt) are not counted.
    AA-990: queued rows whose run_after is more than DEPLOY_GATE_HORIZON_S ahead are not counted either; they are
    reported as `scheduled_later` (no running job, nothing due soon: a deploy loses nothing)."""
    rows = await pool.fetch(
        "SELECT CASE WHEN status = 'queued' AND run_after > now() + make_interval(secs => $2) "
        "THEN 'scheduled_later' ELSE status END AS status, count(*)::int AS n FROM shared.job "
        "WHERE status IN ('queued', 'running') AND NOT (kind = ANY($1::text[])) GROUP BY 1",
        list(exempt_kinds), DEPLOY_GATE_HORIZON_S,
    )
    counts = {r["status"]: r["n"] for r in rows}
    later = counts.pop("scheduled_later", 0)
    return {"active": sum(counts.values()), "by_status": counts, "scheduled_later": later}


# AA-993 G5 review: a QUEUED row of a deploy_blocking kind due within this window also blocks. The guard runs before
# the image build + push and the old task keeps claiming until its SIGTERM several minutes later, so such a row would
# otherwise be claimed by the old task and cut mid-run. 15 min covers build + push + roll; a row due later waits for
# whichever image is live then (the blocking kinds are enqueued due at once, max_attempts=1, so this is rare).
DEPLOY_BLOCKING_HORIZON_S = 900


async def deploy_blockers(pool, blocking_kinds: Sequence[str] = ()) -> dict:
    """AA-993 G5 (design D1.4) — what blocks a deploy: open rerun waves (`shared.rerun_wave`, migration 217), and rows
    of `blocking_kinds` (kinds registered with `deploy_blocking`) per kind that are RUNNING (`blocking_running`) or
    QUEUED and due within DEPLOY_BLOCKING_HORIZON_S (`blocking_due`: the old task would claim them before its SIGTERM).
    A queued row due later does not block. One round trip; raises on a read error so the deploy gate fails closed."""
    rows = await pool.fetch(
        "SELECT 'wave' AS what, NULL::text AS kind, count(*)::int AS n FROM shared.rerun_wave WHERE status = 'open' "
        "UNION ALL "
        "SELECT CASE WHEN status = 'running' THEN 'running' ELSE 'due' END, kind, count(*)::int FROM shared.job "
        "WHERE kind = ANY($1::text[]) AND (status = 'running' "
        "OR (status = 'queued' AND run_after <= now() + make_interval(secs => $2))) GROUP BY 1, 2",
        list(blocking_kinds), DEPLOY_BLOCKING_HORIZON_S,
    )
    open_waves = sum(r["n"] for r in rows if r["what"] == "wave")
    running = {r["kind"]: r["n"] for r in rows if r["what"] == "running" and r["n"]}
    due = {r["kind"]: r["n"] for r in rows if r["what"] == "due" and r["n"]}
    return {"open_waves": open_waves, "blocking_running": dict(sorted(running.items())),
            "blocking_due": dict(sorted(due.items()))}


# ── worker liveness (AA-687, migration 178) ─────────────────────────────────────────────────────
# Read by the Jobs page only; the queue functions above never depend on it.

WORKER_ALIVE_SECONDS = 60  # a worker writes about every 15 s; silent for 60 s = gone


async def worker_register(pool, worker_id: str, host: str, max_parallel: int, caps: dict,
                          task_revision: Optional[int] = None, git_sha: Optional[str] = None) -> None:
    """A worker's start: its row, plus pruning rows of workers stopped/silent for 7 days. AA-884: `git_sha` is the
    image's APP_GIT_SHA (CIS 248 column), compared with the API's on /admin/platform/apps and in the deploy smoke."""
    await pool.execute(
        """
        INSERT INTO shared.job_worker (worker_id, host, max_parallel, caps, task_revision, git_sha)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6)
        ON CONFLICT (worker_id) DO UPDATE SET last_seen_at = now(), stopped_at = NULL,
            max_parallel = excluded.max_parallel, caps = excluded.caps,
            task_revision = excluded.task_revision, git_sha = excluded.git_sha
        """,
        worker_id, host, max_parallel, json.dumps(caps), task_revision, git_sha,
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
        SELECT worker_id, host, started_at, last_seen_at, stopped_at, max_parallel, caps, task_revision, git_sha,
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
        except (asyncpg.UndefinedTableError, asyncpg.InvalidSchemaNameError):
            return []

    versions = await _rows(
        "SELECT id::text AS version_id, published_tour_id::text AS published_tour_id, "
        "tenant_id::text AS tenant_id, version_number, status "
        "FROM gold_aa_internal.tenant_tour_versions WHERE job_id = $1::uuid LIMIT 20")
    pieces = await _rows(
        "SELECT piece_id::text AS piece_id, tenant_id::text AS tenant_id, status "
        "FROM acp_shared.content_piece WHERE job_id = $1::uuid LIMIT 20")
    # AA-712: the place-pipeline build rows (geo, migration 226) the job wrote.
    place_builds = await _rows(
        "SELECT source_id, tour_id::text AS tour_id, country, status, counts FROM geo.tour_build "
        "WHERE job_id = $1::uuid LIMIT 20")
    graph_builds = await _rows(
        "SELECT build_id::text AS build_id, is_current, stats->'tours' AS tours, stats->'nodes' AS nodes "
        "FROM geo.graph_build WHERE job_id = $1::uuid LIMIT 5")
    p = payload or {}
    return {"tour_versions": versions, "content_pieces": pieces,
            "tour_id": p.get("tour_id"), "version_id": p.get("version_id"),
            "place_builds": place_builds, "graph_builds": graph_builds}
