"""AA-712 — `place_extract` job kind: one live tour's day skeleton and places into schema geo
(services/places/pipeline.extract_tour_places; design §3).

Payload: {"tour_id", "version_id"?, "reason"?, "force"?}. Enqueued at the end of A3 atomize, on
set-active / restore, and by the admin backfill (services/places/enqueue.py).

Paid calls: 0–1 `a3_place_days` (only when a non-last day has no overnight from the rules and the
itinerary text changed — days_hash) + ceil(new places / 96) `a3_place_embed` (only for texts whose
text_hash has no stored vector). Geocoding is NOT done here: new names are queued as pending and
one debounced `place_geocode` resolves them for every tour (single writer of shared.destinations).

Cap 1 and `background`: never takes the worker's last 2 free slots (S217 lesson, test_aa737). A tour
that is not (or no longer) live ends as `skipped_not_live`; a model failure gives status `partial`
(rules-only days), never a failed tour.
"""
from __future__ import annotations

from typing import Optional

import structlog

from services.places import llm, store
from services.places.days import MAX_TOKENS as DAYS_MAX_TOKENS
from services.places.enqueue import GEOCODE_KIND, GRAPH_KIND, enqueue_debounced
from services.places.pipeline import ExtractDeps, extract_tour_places
from shared.cost_guard import BudgetExceeded, load_run_budget, maybe_alert
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "place_extract"


async def follow_ups(pool, result: dict, created_by: str) -> dict:
    """Debounced platform jobs after a tour's places changed: geocode (when names wait) and graph."""
    out = {}
    if result.get("status") in ("ok", "partial", "rebased"):
        if result.get("pending_names_due"):
            out["geocode_job"], _ = await enqueue_debounced(pool, GEOCODE_KIND, reason=f"{KIND}:{result['tour_id']}",
                                                            created_by=created_by)
        out["graph_job"], _ = await enqueue_debounced(pool, GRAPH_KIND, reason=f"{KIND}:{result['tour_id']}",
                                                      created_by=created_by)
    return out


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=300, background=True)
async def run(ctx: JobContext) -> Optional[dict]:
    p = ctx.payload or {}
    tour_id = p.get("tour_id")
    if not tour_id:
        raise NonRetryable("payload needs tour_id")
    async with ctx.pool.acquire() as conn:
        tour = await store.live_tour(conn, tour_id)
    if tour is None:
        return {"tour_id": tour_id, "status": "skipped_not_live"}
    await ctx.progress(step="extract", tour_id=tour_id, source_id=tour["source_id"])
    budget = await load_run_budget(ctx.pool, "bedrock", KIND, day_scope="kind")
    spent: dict = {}
    deps = ExtractDeps(
        generate_days=llm.make_generate(ctx.pool, llm.DAYS_STAGE, DAYS_MAX_TOKENS, budget,
                                        {"tour_id": tour_id, "op": "days"}, spent),
        embed=lambda texts: llm.embed_texts(ctx.pool, texts, budget, spent),
        spent=spent)
    try:
        result = await extract_tour_places(ctx.pool, tour, deps, force=bool(p.get("force")), job_id=ctx.job_id)
    except BudgetExceeded as e:
        ctx.set_result({"tour_id": tour_id, "status": "stopped_budget", "spent": spent})
        await maybe_alert(ctx.pool, budget, reason=str(e))
        raise
    try:
        result.update(await follow_ups(ctx.pool, result, created_by=KIND))
    except Exception as e:  # noqa: BLE001 — the places are written; a follow-up enqueue is best-effort
        result["follow_up_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        logger.warning("place_extract_follow_up_failed", tour_id=tour_id, error=str(e)[:200])
    await ctx.progress(step="done")
    logger.info("place_extract_done", **{k: v for k, v in result.items() if not isinstance(v, (dict, list))})
    return result
