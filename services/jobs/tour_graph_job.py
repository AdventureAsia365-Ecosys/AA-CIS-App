"""AA-712 — `tour_graph_build` job kind (platform, debounced): stops / nodes / edges / legs /
junctions of every live source as one new geo.graph_build, swapped in as current in one transaction
(services/places/pipeline.build_tour_graph; ADR 0003 §3 versioned swap). No model, no external call.

Payload: {"reason"?, "junction_km"?, "coalesced"?}. Enqueued (debounced) by place_extract,
place_geocode and liveness changes (status / trash / restore / set-active). Refuses to replace the
current build when no live source is visible (RLS or a reset) — TripPlanner's guard, kept.
Suspect and duplicate destinations are left out of the build.
"""
from __future__ import annotations

from typing import Optional

from services.places.enqueue import GRAPH_KIND
from services.places.graph import DEFAULT_JUNCTION_KM
from services.places.pipeline import build_tour_graph
from shared.jobs.registry import JobContext, job_kind

KIND = GRAPH_KIND


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=600, background=True)
async def run(ctx: JobContext) -> Optional[dict]:
    p = ctx.payload or {}
    junction_km = float(p.get("junction_km") or DEFAULT_JUNCTION_KM)
    await ctx.progress(step="graph")
    result = await build_tour_graph(ctx.pool, junction_km=junction_km, job_id=ctx.job_id)
    result["coalesced"] = p.get("coalesced", 0)
    return result
