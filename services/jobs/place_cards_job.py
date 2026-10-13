"""AA-772 (S233) — `place_cards_build` job kind: build the missing place cards for one tour's
atoms (admin "Build place cards" on the Content Quality page). T8/T9 also build cards on demand
for the atoms they write from; this job fills a whole tour ahead of a run.

Payload: {"tour_id"}. Cost: ~2 short calls per new card; one tour has ~30-50 atoms, and atoms
that already have a card are skipped.
"""
from __future__ import annotations

import structlog

from services.acp_shared.place_cards import ensure_place_cards
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "place_cards_build"
MAX_ATOMS = 60  # paid-run rule: one tour at a time, never the catalog


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=600)
async def run(ctx: JobContext) -> dict:
    tour_id = (ctx.payload or {}).get("tour_id")
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
    built = await ensure_place_cards(atom_ids, ctx.pool)
    logger.info("place_cards_build_done", tour_id=tour_id, atoms=len(atom_ids), built=built)
    return {"tour_id": tour_id, "atoms": len(atom_ids), "cards_built": built}
