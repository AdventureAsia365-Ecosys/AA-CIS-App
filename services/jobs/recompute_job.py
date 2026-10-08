"""AA-723 PR2 nac 1 — `recompute` job kind: the one durable gate for the platform-wide
Segment/Score/Route recompute (ADR 0003 layer A/B).

Replaces the two fire-and-forget in-process tasks that drove this recompute:
  - `admin_atoms.py` recompute-after-atom-delete  -> scope="tour"     (segment + score + route)
  - `admin.py` recompute-after-master-status-change (AA-713) -> scope="platform" (score + route)
Both died on an API deploy and were invisible; one kept no task reference (GC risk). Now every
recompute is a durable job: survives deploys, retryable, visible on /admin/job-runner.

Payload: {"scope": "tour"|"platform", "tour_id": <uuid, when scope=tour>, "reason": <str>}
  scope="tour"     -> recompute_segment_score_route(tour_id)   (segment matching this tour,
                                                                then platform-wide score + route)
  scope="platform" -> recompute_rankings_and_routes()          (platform-wide score + route only)

Runs in its OWN thread + event loop + DB connection, exactly like a3_atomize_job: the recompute
calls compute_embedding() which paces Cohere with a blocking time.sleep(3.5); on the worker's loop
those sleeps starved /health and ECS killed the task (S201). Concurrency 1 — the recompute is
platform-wide, so running two at once only fights over the same tables.

Debounce (enqueue_recompute): a burst of atom edits / status flips should not queue N full
platform rebuilds. If a `recompute` job for the same (scope, tour_id) is already QUEUED (not yet
started), reuse it — the pending job will pick up the latest committed state when it runs. A job
already RUNNING does not block a new enqueue (changes made after it started still need a rerun).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from shared.jobs import queue
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "recompute"


# AA-743 — sliding debounce for the platform pass. Each enqueue pushes the queued job's start to
# now + debounce, but never past created_at + max wait, so a long wave still gets a pass every
# ~20 min and the last tour's Score/Route land a few minutes after the wave goes quiet.
WAVE_DEBOUNCE_S = 180
STATUS_DEBOUNCE_S = 60
MAX_WAIT_S = 1200

# Fold this enqueue into the oldest QUEUED platform job: union the Segment scope (absent key =
# every Segment, which absorbs any list), slide run_after, count the folded enqueues.
# FOR UPDATE SKIP LOCKED + the status re-check: a job being claimed right now is not touched.
_COALESCE_SQL = """
    UPDATE shared.job j SET
        payload = CASE
            WHEN NOT (j.payload ? 'segment_ids') OR $1::jsonb IS NULL THEN j.payload - 'segment_ids'
            ELSE jsonb_set(j.payload, '{segment_ids}', (
                SELECT coalesce(jsonb_agg(DISTINCT x), '[]'::jsonb)
                FROM jsonb_array_elements_text((j.payload->'segment_ids') || $1::jsonb) AS x))
        END || jsonb_build_object('coalesced', coalesce((j.payload->>'coalesced')::int, 0) + 1),
        run_after = GREATEST(j.run_after,
                             LEAST(j.created_at + make_interval(secs => $3), now() + make_interval(secs => $2)))
    WHERE j.id = (SELECT id FROM shared.job
                  WHERE kind = 'recompute' AND status = 'queued' AND payload->>'scope' = 'platform'
                  ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
      AND j.status = 'queued'
    RETURNING j.id
"""


async def enqueue_recompute(pool, *, scope: str, reason: str, tour_id: Optional[str] = None,
                            created_by: str = "system", segment_ids: Optional[list[str]] = None,
                            debounce_s: int = 0) -> tuple[str, bool]:
    """Enqueue a recompute, debounced against an already-QUEUED job.
    Returns (job_id, created). created=False means an existing pending job was reused.

    scope="platform" (AA-743): `segment_ids` limits the PAA re-landing to those Segments (None =
    every Segment). A queued platform job absorbs this enqueue (Segment scope unioned) and its
    start slides to now + `debounce_s`, capped at created_at + MAX_WAIT_S. `pool` only needs
    fetchrow/fetchval/fetch, so a bare asyncpg connection works too."""
    if scope not in ("tour", "platform"):
        raise ValueError(f"scope must be 'tour' or 'platform', got {scope!r}")
    if scope == "tour" and not tour_id:
        raise ValueError("scope='tour' needs tour_id")

    if scope == "platform":
        new_ids = json.dumps(sorted(set(segment_ids))) if segment_ids is not None else None
        reused = await pool.fetchval(_COALESCE_SQL, new_ids, debounce_s, MAX_WAIT_S)
        if reused is not None:
            logger.info("recompute_debounced", reused_job=str(reused), scope=scope, reason=reason,
                        segments=None if segment_ids is None else len(segment_ids))
            return str(reused), False
        payload = {"scope": scope, "reason": reason}
        if segment_ids is not None:
            payload["segment_ids"] = sorted(set(segment_ids))
        run_after = datetime.now(timezone.utc) + timedelta(seconds=debounce_s) if debounce_s else None
        return await enqueue(pool, KIND, payload, created_by=created_by, run_after=run_after)

    # Debounce: a pending (queued, not yet claimed) recompute of the same shape already covers this.
    pending = await queue.list_jobs(pool, kind=KIND, status="queued", limit=50,
                                    tour_id=tour_id if scope == "tour" else None)
    for j in pending:
        p = j.get("payload") or {}
        if p.get("scope") == scope and (scope == "platform" or p.get("tour_id") == tour_id):
            logger.info("recompute_debounced", reused_job=j["id"], scope=scope, tour_id=tour_id,
                        reason=reason)
            return j["id"], False

    payload = {"scope": scope, "reason": reason}
    if scope == "tour":
        payload["tour_id"] = str(tour_id)
    job_id, created = await enqueue(pool, KIND, payload, created_by=created_by)
    return job_id, created


# Platform-wide recompute ~1-5 min (6 markets + route detection + embedding pre-pass).
@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=1800)
async def run(ctx: JobContext) -> dict:
    from services.export.handler import (
        recompute_rankings_and_routes, recompute_segment_score_route,
    )

    p = ctx.payload or {}
    scope = p.get("scope")
    tour_id = p.get("tour_id")
    reason = p.get("reason") or ""
    segment_ids = p.get("segment_ids")            # AA-743: None = every Segment
    if scope not in ("tour", "platform"):
        raise NonRetryable(f"payload.scope must be 'tour' or 'platform', got {scope!r}")
    if scope == "tour" and not tour_id:
        raise NonRetryable("scope='tour' needs tour_id")

    await ctx.progress(phase="recomputing", scope=scope, tour_id=tour_id, reason=reason)
    worker_loop = asyncio.get_running_loop()

    def _log_failure(future) -> None:
        if not future.cancelled() and future.exception() is not None:
            logger.warning("recompute_progress_write_failed", job_id=ctx.job_id,
                           error=str(future.exception()))

    def _report(fields: dict) -> None:
        # Sub-steps use "step", never "phase", so a late write never overwrites phase="done".
        asyncio.run_coroutine_threadsafe(ctx.progress(**fields), worker_loop).add_done_callback(_log_failure)

    result: dict = {}

    def _in_own_loop() -> None:
        import asyncpg
        from shared.secrets import get_database_url
        from services.export.handler import _SingleConnAsPool

        async def _body():
            conn = await asyncpg.connect(get_database_url(), ssl="require")
            try:
                own_pool = _SingleConnAsPool(conn)
                if scope == "tour":
                    return await recompute_segment_score_route(
                        tour_id, own_pool, log_tour_id=tour_id, progress=_report)
                return await recompute_rankings_and_routes(own_pool, log_reason=reason,
                                                            segment_ids=segment_ids)
            finally:
                await conn.close()

        result.update(asyncio.run(_body()) or {})

    await asyncio.to_thread(_in_own_loop)
    await ctx.progress(phase="done")
    logger.info("recompute_job_done", job_id=ctx.job_id, scope=scope, tour_id=tour_id, reason=reason)
    return {"scope": scope, "tour_id": tour_id, "reason": reason, "coalesced": p.get("coalesced", 0),
            "segments_relanded": None if segment_ids is None else len(segment_ids), **result}
