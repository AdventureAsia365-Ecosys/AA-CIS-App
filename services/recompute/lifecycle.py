"""AA-1056 — a tour on/off change must reach Score and Route: the lag measure and the sweep.

A tour retired, paused or restored (``gold_aa_internal.tour_lifecycle_event``) changes what the tenant Slate and the
Subjects may show; the platform recompute (Score + Route through ``v_active_tour_atoms``) is what makes them follow.
The after-commit step of the lifecycle service (services/tour_lifecycle/effects.recompute_after_status_change)
enqueues it, best-effort. On 05/11/2026 a retire / restore pair waited about 74 hours for its next finished recompute
(root S273 system picture §3) while the audit metric "events not followed by a recompute" said 0: it counted any
recompute that finished after the event, however late.

Two parts, one definition ("the first succeeded ``recompute`` job, any scope, that STARTED after the event"):

- :func:`lag_summary` — over the last :data:`LAG_WINDOW_DAYS`: events, followed, still waiting, waiting longer than
  :data:`LAG_LIMIT_S` (the ops check ``recompute_lifecycle_lag`` alerts on it, shared/ops_checks.py), followed late,
  and the lag p50 / p95 / max. The api ops sampler reads it every tick; the Data coverage page reads the same SQL
  (source ``recompute_lag``, api/services/data_coverage).
- :func:`sweep` — every :data:`SWEEP_EVERY_S` (the api ops sampler loop): every event of the last
  :data:`SWEEP_WINDOW_S`, older than :data:`SWEEP_GRACE_S`, with no queued platform recompute and no recompute running
  or finished since, gets ONE platform recompute through the shared ``enqueue_recompute``, scoped to the Segments of
  the swept tours (the scope the status-change path uses). A second api task sweeping at the same time sees the queued
  job and does nothing.

Any scope counts as "followed": a tour-scope run also rebuilds the platform-wide Score and Route.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger()

LIFECYCLE_EVENTS = ("retired", "paused", "restored")
LAG_LIMIT_S = 1800              # the alert: 20-min debounce cap (MAX_WAIT_S) + a slow run
LAG_WINDOW_DAYS = 30
SWEEP_EVERY_S = 7200
SWEEP_WINDOW_S = 7 * 86400
SWEEP_GRACE_S = 300             # the after-commit enqueue (60 s debounce) lands first
SWEEP_CREATED_BY = "scheduler:recompute_sweep"
SWEEP_REASON = "lifecycle_sweep"

# $1 events, $2 window (days), $3 limit (seconds). One row; NULL lags when nothing was followed yet.
LAG_SQL = """
WITH ev AS (
    SELECT e.occurred_at,
           (SELECT min(j.finished_at) FROM shared.job j
             WHERE j.kind = 'recompute' AND j.status = 'succeeded' AND j.started_at > e.occurred_at) AS fin
      FROM gold_aa_internal.tour_lifecycle_event e
     WHERE e.event = ANY($1::text[]) AND e.occurred_at > now() - make_interval(days => $2)
)
SELECT count(*)::int AS events,
       count(fin)::int AS followed,
       count(*) FILTER (WHERE fin IS NULL)::int AS waiting,
       count(*) FILTER (WHERE fin IS NULL AND occurred_at < now() - make_interval(secs => $3))::int
           AS waiting_over_limit,
       count(*) FILTER (WHERE fin - occurred_at > make_interval(secs => $3))::int AS late,
       (percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM fin - occurred_at)))::float8 AS lag_p50_s,
       (percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM fin - occurred_at)))::float8 AS lag_p95_s,
       max(extract(epoch FROM fin - occurred_at))::float8 AS lag_max_s,
       ceil(max(extract(epoch FROM fin - occurred_at)) / 60)::int AS lag_max_min,
       (max(extract(epoch FROM now() - occurred_at)) FILTER (WHERE fin IS NULL))::float8 AS oldest_waiting_s
  FROM ev
"""

# $1 events, $2 window (seconds), $3 grace (seconds). The events nothing has covered: no queued platform recompute
# (it will read the latest state when it starts) and no recompute of any scope running or finished since the event.
SWEEP_SQL = """
SELECT e.tour_id::text AS tour_id, e.event, e.occurred_at
  FROM gold_aa_internal.tour_lifecycle_event e
 WHERE e.event = ANY($1::text[])
   AND e.occurred_at > now() - make_interval(secs => $2)
   AND e.occurred_at < now() - make_interval(secs => $3)
   AND NOT EXISTS (
        SELECT 1 FROM shared.job j
         WHERE j.kind = 'recompute'
           AND ((j.status = 'queued' AND j.payload->>'scope' = 'platform')
                OR (j.status IN ('running', 'succeeded') AND j.started_at > e.occurred_at)))
 ORDER BY e.occurred_at
"""


def lag_args() -> tuple:
    """The bind values of :data:`LAG_SQL` (the ops sampler and the Data coverage source use the same)."""
    return list(LIFECYCLE_EVENTS), LAG_WINDOW_DAYS, float(LAG_LIMIT_S)


def _num(v: Any) -> Any:
    if v is None:
        return None
    return round(float(v), 1) if isinstance(v, float) else int(v)


async def lag_summary(db) -> dict:
    """The lag measure of the last LAG_WINDOW_DAYS (counts and seconds only, never ids). `db` = pool or connection."""
    row = await db.fetchrow(LAG_SQL, *lag_args())
    if row is None:
        return {}
    return {k: _num(row[k]) for k in ("events", "followed", "waiting", "waiting_over_limit", "late", "lag_p50_s",
                                      "lag_p95_s", "lag_max_s", "oldest_waiting_s")}


async def sweep(pool, *, window_s: int = SWEEP_WINDOW_S, grace_s: int = SWEEP_GRACE_S) -> dict:
    """Enqueue one platform recompute when an event has none (see the module docstring). Returns {"events": 0} when
    nothing was missing, else the counts and the job (created=False: folded into a job queued meanwhile)."""
    from services.export.handler import tour_segment_ids
    from services.jobs.recompute_job import enqueue_recompute

    rows = await pool.fetch(SWEEP_SQL, list(LIFECYCLE_EVENTS), float(window_s), float(grace_s))
    if not rows:
        return {"events": 0}
    tour_ids = sorted({r["tour_id"] for r in rows})
    segment_ids: set[str] = set()
    async with pool.acquire() as conn:
        for tour_id in tour_ids:
            segment_ids.update(await tour_segment_ids(conn, tour_id))
    job_id, created = await enqueue_recompute(
        pool, scope="platform", reason=f"{SWEEP_REASON}:{len(rows)}", created_by=SWEEP_CREATED_BY,
        segment_ids=sorted(segment_ids))
    oldest = min(r["occurred_at"] for r in rows)
    out = {"events": len(rows), "tours": len(tour_ids), "segments": len(segment_ids), "job_id": job_id,
           "created": created, "oldest_event_age_s": round((datetime.now(timezone.utc) - oldest).total_seconds(), 1)}
    logger.warning("recompute_lifecycle_swept", **out)
    return out
