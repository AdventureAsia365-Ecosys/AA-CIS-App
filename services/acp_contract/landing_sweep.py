"""services/acp_contract/landing_sweep.py — AA-1055 (system picture item 1): the sweep that catches Segments whose
newest demand never landed.

New PAA questions reach a Segment's Questions axis (and through it Score and the Blog bar) only when a recompute
re-lands them (atom_ranking.precompute_question_landings). Research now asks for that recompute itself
(segment_research.enqueue_recompute_for_places), but an enqueue can fail, a recompute can fail, and demand written
before this change never asked at all: on 08/11/2026 (S273, recompute_health) 1,272 live Segments had never landed
(`landing.live_never_landed`) and 7,786 had landed before the newest demand (`landing.live_landed_before_newest_demand`,
measured against the newest demand row of the whole store).

The sweep finds, among the Segments a platform recompute lands (an atom of an active platform Master, not excluded as
transit / unnamed place by the same rule and stored Jev pick), those that
  - never landed (`questions_count IS NULL`), or
  - landed before their own place was last researched (`questions_computed_at` older than the newest
    `segment_research_log.researched_at` of their canonical place: a per-place test, not the store-wide newest),
and enqueues ONE debounced platform recompute limited to them (`segment_ids`, at most MAX_SEGMENTS, never-landed
first). $0 itself (reads only); the recompute it asks for costs what any recompute costs (Jev landing verdicts and
question embeddings, mostly cached).

When: the job worker calls `sweep_safe` on its tick (shared/jobs/worker.py, every SWEEP_TICK_S). It acts only inside
the night window SWEEP_UTC_HOURS (00:00-06:00 ICT, a long recompute blocks deploys, DM-11.4), at most once per
MIN_GAP_H (it looks for its own recompute in shared.job), and never while a recompute is queued or running (that one
lands every never-landed Segment anyway). Switch: env LANDING_SWEEP=off.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

SWITCH_ENV = "LANDING_SWEEP"
REASON = "landing_sweep"
SWEEP_TICK_S = 3600.0
SWEEP_TIMEOUT_S = 120.0
SWEEP_UTC_HOURS = range(17, 23)
MIN_GAP_H = 20
MAX_SEGMENTS = 5000

CANDIDATES_SQL = """
    WITH live AS (
        SELECT s.segment_id, s.canonical_place, s.canonical_action, s.questions_count, s.questions_computed_at,
               array_agg(ta.activity_type) AS activity_types
        FROM acp_contract.atom_segment s
        JOIN acp_contract.atom_segment_member m ON m.segment_id = s.segment_id
        JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id
        WHERE ta.owner_scope = 'platform' AND NOT ta.deleted
        GROUP BY s.segment_id, s.canonical_place, s.canonical_action, s.questions_count, s.questions_computed_at
    ), research AS (
        SELECT canonical_place, max(researched_at) AS newest
        FROM acp_contract.segment_research_log GROUP BY canonical_place
    )
    SELECT l.segment_id, l.canonical_place, l.canonical_action, l.activity_types,
           (l.questions_count IS NULL) AS never_landed
    FROM live l LEFT JOIN research r ON r.canonical_place = l.canonical_place
    WHERE l.questions_count IS NULL
       OR (r.newest IS NOT NULL AND (l.questions_computed_at IS NULL OR l.questions_computed_at < r.newest))
"""

RECENT_SWEEP_SQL = """
    SELECT 1 AS ok FROM shared.job
    WHERE kind = 'recompute' AND payload->>'reason' = $1 AND created_at > now() - make_interval(hours => $2)
    LIMIT 1
"""

ACTIVE_RECOMPUTE_SQL = """
    SELECT 1 AS ok FROM shared.job WHERE kind = 'recompute' AND status IN ('queued', 'running') LIMIT 1
"""


def enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def in_window(now: datetime | None = None) -> bool:
    return (now or datetime.now(timezone.utc)).hour in SWEEP_UTC_HOURS


async def find_unlanded(pool) -> list[str]:
    """Segment ids that need a re-landing (module docstring), never-landed first, at most MAX_SEGMENTS. Excluded
    Segments are left out with the stored Jev picks only (no paid call): a platform recompute never lands them, so
    listing them would ask for a recompute every night."""
    from services.acp_contract.atom_ranking import resolve_exclusions

    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(CANDIDATES_SQL)]
    if not rows:
        return []
    exclusions = await resolve_exclusions(pool, rows, cached_only=True)
    kept = [r for r in rows if not exclusions.get(r["segment_id"])]
    kept.sort(key=lambda r: (not r["never_landed"], r["segment_id"]))
    return [r["segment_id"] for r in kept[:MAX_SEGMENTS]]


async def sweep(pool, *, now: datetime | None = None, force: bool = False) -> dict:
    """One pass. `force` skips the window and the once-per-day gap (an admin or a test); the active-recompute check
    always applies."""
    if not force:
        if not enabled():
            return {"action": "off"}
        if not in_window(now):
            return {"action": "outside_window"}
    async with pool.acquire() as conn:
        if await conn.fetchval(ACTIVE_RECOMPUTE_SQL):
            return {"action": "recompute_active"}
        if not force and await conn.fetchval(RECENT_SWEEP_SQL, REASON, MIN_GAP_H):
            return {"action": "recent"}
    segment_ids = await find_unlanded(pool)
    if not segment_ids:
        return {"action": "nothing", "segments": 0}
    from services.jobs.recompute_job import WAVE_DEBOUNCE_S, enqueue_recompute

    job_id, created = await enqueue_recompute(pool, scope="platform", reason=REASON, segment_ids=segment_ids,
                                              debounce_s=WAVE_DEBOUNCE_S, created_by=f"system:{REASON}")
    logger.info("landing_sweep_enqueued", job_id=job_id, created=created, segments=len(segment_ids))
    return {"action": "enqueued", "job_id": job_id, "created": created, "segments": len(segment_ids)}


async def sweep_safe(pool) -> dict:
    """The worker tick's entry: never raises."""
    try:
        return await sweep(pool)
    except Exception as exc:  # noqa: BLE001 — the next tick tries again
        logger.warning("landing_sweep_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        return {"action": "error", "error": str(exc)[:200]}
