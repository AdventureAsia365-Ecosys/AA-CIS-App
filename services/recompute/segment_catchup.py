"""AA-1059 — the platform recompute re-runs the segment stage for active Masters that have matchable atoms but no
Segment (recompute stage `segment_catchup`).

Why: the `segment` stage runs for ONE tour, only on two paths: inline in the `a3_atomize` chain (right after atomize)
and in a tour-scope `recompute` (an admin atom delete / undo). The platform pass (`PLATFORM_STAGES`) never runs it. So a
Master whose segment run did not happen or did not land stays without a Segment, and so without Score, Route or Slate,
until someone re-atomizes it:
  - the a3 chain's segment step is best-effort: a failure is written into the job result (`segment_score_route:
    failed: ...`) and the job still succeeds, so it is never retried (S218: 566 tours, lessons log "Pipeline");
  - a tour atomized while its Master was not active reads no atoms (`v_active_tour_atoms` keeps active Masters only),
    so its segment run writes nothing; restoring the Master later enqueues a platform pass, which has no segment stage;
  - an a3 run stopped between atomize and the segment step (deploy release before AA-993 G3).
S273 counted 9 such Masters (`RECOMPUTE masters.active.atoms_but_no_segment`), the same 9 as in S252.

What it does, at the start of every platform pass (before `landing`, so the new Segments are landed, scored and routed
in the same pass):
  1. read every live atom (`v_active_tour_atoms`: active Master, live source, not deleted, not an empty-day marker) with
     a place and an action, of the tours none of whose live atoms is a member of any Segment (CANDIDATES_SQL);
  2. keep the tours with at least one atom that may form a Segment (`segment_matching.forms_segment`): a tour whose
     every atom is transit or an unnamed place by rule has no Segment by design and is only counted (`all_excluded`);
  3. run `run_segment_matching(tour_id)` for at most SEGMENT_CATCHUP_MAX of them (env, default 10; 0 = off), each under
     the tour's A3 lock taken without waiting (`a3_tour_lock`, max wait 0): a tour an `a3_atomize` job is working on is
     skipped (`busy`), that job runs its own segment step. A failure is counted per exception class and never fails the
     pass (the next pass tries again).
Cost: the same as the segment step of an atomize (Jev same-place pairs, mostly cached); no new LLM stage.
The Segments the run touched are handed to `landing` (ctx.data `catchup_segment_ids`); brand-new ones are landed anyway
(`questions_count IS NULL`).
"""
from __future__ import annotations

import os
import random
from collections.abc import Iterable, Mapping
from typing import Optional

import structlog

logger = structlog.get_logger()

MAX_ENV = "SEGMENT_CATCHUP_MAX"
DEFAULT_MAX = 10
SAMPLE = 5

# Live atoms (with a place and an action) of the active Masters none of whose live atoms is in any Segment. One scan of
# the active-atom view plus the member table; no correlated subplan per atom.
CANDIDATES_SQL = """
WITH seg_tours AS (
    SELECT DISTINCT a.tour_id
      FROM acp_contract.v_active_tour_atoms a
      JOIN acp_contract.atom_segment_member m ON m.atom_id = a.atom_id)
SELECT a.tour_id::text AS tour_id, a.atom_id, a.place, a.action, a.activity_type
  FROM acp_contract.v_active_tour_atoms a
 WHERE a.place IS NOT NULL AND a.action IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM seg_tours s WHERE s.tour_id = a.tour_id)
 ORDER BY a.tour_id, a.atom_id
"""

# The Segments a tour's live atoms belong to after its run (handed to landing).
TOUR_SEGMENTS_SQL = """
SELECT DISTINCT m.segment_id
  FROM acp_contract.atom_segment_member m
  JOIN acp_contract.tour_atoms t ON t.atom_id = m.atom_id
 WHERE t.tour_id = ANY($1::uuid[]) AND NOT t.deleted AND NOT t.is_empty_marker
"""


def catchup_max(value: Optional[str] = None) -> int:
    """SEGMENT_CATCHUP_MAX as an int >= 0 (0 = the stage is off); a bad value falls back to DEFAULT_MAX."""
    raw = os.environ.get(MAX_ENV, "") if value is None else value
    try:
        return max(0, int(str(raw).strip() or DEFAULT_MAX))
    except ValueError:
        return DEFAULT_MAX


def plan(rows: Iterable[Mapping]) -> dict:
    """Pure: CANDIDATES_SQL rows -> {"tours": [tour ids with >= 1 forming atom, sorted], "all_excluded": [tour ids whose
    every atom is transit / unnamed by rule, sorted]}. Uses the segment stage's own rule (`forms_segment`), so a tour
    is a candidate exactly when its segment run would place at least one atom."""
    from services.acp_contract.segment_matching import SegmentAtom, forms_segment

    forming: dict[str, bool] = {}
    for r in rows:
        tour_id = str(r["tour_id"])
        atom = SegmentAtom(str(r["atom_id"]), tour_id, None, r["place"] or "", r["action"] or "",
                           activity_type=r["activity_type"])
        forming[tour_id] = forming.get(tour_id, False) or forms_segment(atom)
    return {"tours": sorted(t for t, f in forming.items() if f),
            "all_excluded": sorted(t for t, f in forming.items() if not f)}


def pick(tours: list[str], cap: int, rng: Optional[random.Random] = None) -> list[str]:
    """At most `cap` tours. When there are more, a random sample, so a tour that fails every pass cannot hold a slot
    forever and starve the others (no per-tour state is kept)."""
    if cap <= 0:
        return []
    if len(tours) <= cap:
        return list(tours)
    return sorted((rng or random).sample(tours, cap))


async def run_segment_catchup(pool, *, max_tours: Optional[int] = None, scope_tour_ids: Optional[list[str]] = None,
                              rng: Optional[random.Random] = None) -> dict:
    """One catch-up pass. `scope_tour_ids` limits it to those tours (tests); None = every candidate (the stage).
    Returns counts only (plus the touched Segment ids under `segment_ids`, for the landing stage)."""
    cap = catchup_max() if max_tours is None else max(0, max_tours)
    if cap == 0:
        return {"status": "off"}
    from services.acp_contract.segment_matching import run_segment_matching
    from services.export.handler import TourLockTimeout, a3_tour_lock

    async with pool.acquire() as conn:
        rows = await conn.fetch(CANDIDATES_SQL)
    if scope_tour_ids is not None:
        wanted = {str(t) for t in scope_tour_ids}
        rows = [r for r in rows if str(r["tour_id"]) in wanted]
    planned = plan(rows)
    chosen = pick(planned["tours"], cap, rng)
    out: dict = {"status": "ok", "candidates": len(planned["tours"]), "all_excluded": len(planned["all_excluded"]),
                 "deferred": len(planned["tours"]) - len(chosen), "run": 0, "segmented": 0, "busy": 0, "failed": 0,
                 "failed_by": {}, "sample_segmented": [], "sample_all_excluded": planned["all_excluded"][:SAMPLE]}
    segmented: list[str] = []
    for tour_id in chosen:
        try:
            async with a3_tour_lock(tour_id, poll_seconds=0, max_wait_seconds=0):
                result = await run_segment_matching(tour_id, pool)
        except TourLockTimeout:
            out["busy"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 — one tour must not stop the platform pass; the class is recorded
            name = type(exc).__name__
            out["failed"] += 1
            out["failed_by"][name] = out["failed_by"].get(name, 0) + 1
            logger.warning("segment_catchup_tour_failed", tour_id=tour_id, error=name)
            continue
        out["run"] += 1
        if result.get("segments_written"):
            out["segmented"] += 1
            segmented.append(tour_id)
    out["sample_segmented"] = segmented[:SAMPLE]
    segment_ids: list[str] = []
    if segmented:
        async with pool.acquire() as conn:
            segment_ids = sorted(r["segment_id"] for r in await conn.fetch(TOUR_SEGMENTS_SQL, segmented))
    out["segment_ids"] = segment_ids
    logger.info("segment_catchup_done", **{k: v for k, v in out.items()
                                            if not k.startswith("sample") and k != "segment_ids"})
    return out
