"""AA-772 — `place_cards_build` job kind: build the missing place cards for one tour's atoms
(admin "Build place cards" on the Content Quality page). T8 also builds the card of its request
atom on demand; T9 only reads cards (S234, plan C8). This job fills a whole tour ahead of a run.

Payload: {"tour_id", "force"?}. `force` ignores stored misses (place_card_miss) for this tour;
the free rule skip (unnamed / logistics places) still applies.

Cost (S234): ≤1 draft + 1 check per DISTINCT place (atoms are grouped by place, existing cards
are reused by alias / core name, misses are cached), capped at MAX_PLACES drafts per job. S233 —
before grouping — paid ~72 calls / $0.38 per tour.
"""
from __future__ import annotations

import structlog

from services.acp_shared.place_cards import ensure_place_cards
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "place_cards_build"
MAX_PLACES = 40  # paid-run rule: one tour at a time, never the catalog; extra places wait for a re-run
MAX_ATOMS = 400  # safety bound on the atom read only (the largest tours have ~100 atoms)


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=600)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    tour_id = payload.get("tour_id")
    if not tour_id:
        raise NonRetryable("payload needs tour_id")
    async with ctx.pool.acquire() as conn:
        atom_ids = [r["atom_id"] for r in await conn.fetch(
            """
            SELECT atom_id FROM acp_contract.v_active_tour_atoms
            WHERE tour_id = $1::uuid AND NOT deleted AND NOT is_empty_marker AND place IS NOT NULL
            ORDER BY itinerary_day NULLS LAST, atom_id
            LIMIT $2
            """, tour_id, MAX_ATOMS)]
    counters = await ensure_place_cards(atom_ids, ctx.pool, force=bool(payload.get("force")),
                                        max_places=MAX_PLACES)
    logger.info("place_cards_build_done", tour_id=tour_id, **counters)
    out = {"tour_id": tour_id, "force": bool(payload.get("force")), **counters}
    if counters.get("linked_existing") or counters.get("built"):
        out["place_extract_job"] = await _relink_places(ctx.pool, tour_id)
    return out


async def _relink_places(pool, tour_id: str):
    """AA-712: new card links change the tour's place link keys (card identity is preferred), so
    the tour's places are re-extracted — free when nothing else changed (days reused, no model
    call). Reuses a queued place_extract of the tour. Best-effort: never fails the card build."""
    try:
        from services.places.enqueue import auto_enabled, enqueue_place_extract
        if not auto_enabled():   # S236: automatic place runs wait for PLACE_PIPELINE_AUTO
            return None
        job_id, _ = await enqueue_place_extract(pool, tour_id=str(tour_id), version_id=None,
                                                reason="place_cards_build", created_by=KIND)
        return job_id
    except Exception as e:  # noqa: BLE001
        logger.warning("place_extract_enqueue_after_cards_failed", tour_id=tour_id, error=str(e)[:200])
        return None
