"""AA-847 — `place_shape` job kind (platform, debounced, cap 1, background): the geometry of LINE places (rivers,
roads, trekking stretches — owner decision S246, D1: drawn as lines, never a pin, a circle or hidden). Work:
services/places/shape_run.resolve_shapes; rules: services/places/shape.py; sources: services/places/shape_sources.py.

The ONLY writer of geo.place_shape (apart from the admin's manual GeoJSON). Never moves shared.destinations lat / lng
(place_geocode stays the single coordinate writer).

Payload: {"reason"?, "limit"?, "coalesced"?}. Enqueued (debounced) by place_geocode when it stored a new line and by
POST /admin/places/shapes/backfill. Re-enqueues itself while shapes are due. Enqueues tour_graph_build when a shape
changed (the per-day stretches, geo.tour_segment, are cut at build time).

Switches (⚠️ underscore env names — Nghiệp sets them by hand in the task definition):
  PLACE_SHAPES_ENABLED   kill switch, default OFF: the job ends `disabled` and makes no call and no write.
  GEOCODER_PROVIDER      model_only → no Geoapify and no Overpass call; only the no-call anchored route is built.
  GEOAPIFY_DAILY_CREDIT_LIMIT / GEOCODER_COST_PER_REQUEST_USD — shared with place_geocode (per credit here).

Paid calls: Geoapify Place Details (Free plan $0; credits per call are ASSUMED 1 for `details` + 1 for
`details.full_geometry` and logged in geo.geocode_call.credits — the 5-name trial reads the real use from the Geoapify
dashboard before any backfill). Overpass is free (≤ 1 request / 2 s). No model call.

Mutual exclusion with place_geocode (services/places/geolock.py): both share the Geoapify 5 req/s limit and daily
credits, and the credit guard only sees finished runs; a run that finds the lock taken re-enqueues itself and ends.
"""
from __future__ import annotations

import os
from typing import Optional

import structlog

from services.places import geocoder as geocoder_mod
from services.places import shape_run, store
from services.places.enqueue import GRAPH_KIND, SHAPE_KIND, enqueue_debounced
from services.places.geolock import geoapify_lock
from services.places.shape_sources import GeoapifyDetails, Overpass
from shared.cost_guard import BudgetExceeded, load_run_budget, maybe_alert
from shared.jobs.registry import JobContext, job_kind

logger = structlog.get_logger()

KIND = SHAPE_KIND
ENABLED_ENV = "PLACE_SHAPES_ENABLED"
_ON = {"1", "true", "on", "yes"}
MAX_NAMES_PER_RUN = 50
CONTINUE_DEBOUNCE_S = 30
LOCKED_DEBOUNCE_S = 120


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "").strip().lower() in _ON


def make_clients(provider: str, credits_used_today: int):
    """(details, overpass) for the provider: geoapify → both; model_only → (None, None)."""
    if provider != "geoapify":
        return None, None
    details = GeoapifyDetails(
        daily_credit_limit=int(geocoder_mod._env_float("GEOAPIFY_DAILY_CREDIT_LIMIT",
                                                       geocoder_mod.DEFAULT_DAILY_CREDITS)),
        rate_per_s=geocoder_mod.clamp_rate(geocoder_mod._env_float("GEOAPIFY_RATE_PER_S",
                                                                   geocoder_mod.DEFAULT_RATE_PER_S)),
        credits_used_today=credits_used_today)
    return details, Overpass()


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=1800, background=True)
async def run(ctx: JobContext) -> Optional[dict]:
    p = ctx.payload or {}
    if not enabled():
        logger.info("place_shape_disabled", env=ENABLED_ENV)
        return {"status": "disabled", "env": ENABLED_ENV}
    limit = max(1, min(int(p.get("limit") or MAX_NAMES_PER_RUN), MAX_NAMES_PER_RUN))
    async with geoapify_lock(ctx.pool) as held:
        if not held:
            job, _ = await enqueue_debounced(ctx.pool, KIND, reason=f"{KIND}:locked", created_by=KIND,
                                             debounce_s=LOCKED_DEBOUNCE_S)
            return {"status": "waiting_for_place_geocode", "next_job": job}
        return await _run_locked(ctx, limit)


async def _run_locked(ctx: JobContext, limit: int) -> dict:
    provider = geocoder_mod.provider_name()
    async with ctx.pool.acquire() as conn:
        used = await store.geocoder_requests_today(conn, "geoapify")
    details, overpass = make_clients(provider, used)
    cost_per_credit = geocoder_mod._env_float("GEOCODER_COST_PER_REQUEST_USD", 0.0)
    budget = await load_run_budget(ctx.pool, "geocoder", KIND) if details is not None else None
    result: dict = {"provider": provider}
    try:
        result.update(await shape_run.resolve_shapes(ctx.pool, details=details, overpass=overpass, limit=limit,
                                                     progress=ctx.progress, budget=budget,
                                                     cost_per_credit=cost_per_credit))
    except BudgetExceeded as e:
        ctx.set_result({"status": "stopped_budget", **result})
        if budget is not None:
            await maybe_alert(ctx.pool, budget, reason=str(e))
        raise
    finally:
        await _log_calls(ctx, details, overpass, cost_per_credit)
    result["geoapify_credits"] = details.credits if details is not None else 0
    result["overpass_requests"] = overpass.requests if overpass is not None else 0
    try:
        if result.get("ok") or result.get("approximate"):
            result["graph_job"], _ = await enqueue_debounced(ctx.pool, GRAPH_KIND, reason=KIND, created_by=KIND)
        async with ctx.pool.acquire() as conn:
            remaining = await store.shapes_due_count(conn)
        result["due"] = remaining
        if result.get("stopped_geocoder_unavailable"):
            result["status"] = "stopped_geocoder_unavailable"
        elif result.get("stopped_credit_limit"):
            result["status"] = "stopped_credit_limit"
        else:
            result["status"] = "ok"
            if remaining and result.get("names"):
                result["next_job"], _ = await enqueue_debounced(ctx.pool, KIND, reason=f"{KIND}:continue",
                                                                created_by=KIND, debounce_s=CONTINUE_DEBOUNCE_S)
    except Exception as e:  # noqa: BLE001 — follow-ups are best-effort
        result["follow_up_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    logger.info("place_shape_done", **{k: v for k, v in result.items() if not isinstance(v, (dict, list))})
    return result


async def _log_calls(ctx: JobContext, details, overpass, cost_per_credit: float) -> None:
    try:
        async with ctx.pool.acquire() as conn:
            if details is not None and details.requests:
                cost = round(details.credits * cost_per_credit, 6)
                ctx.add_cost(cost)
                await store.log_geocode_calls(conn, "geoapify", details.requests, cost, ctx.job_id,
                                              credits=details.credits, endpoint="place_details")
            if overpass is not None and overpass.requests:
                await store.log_geocode_calls(conn, "overpass", overpass.requests, 0.0, ctx.job_id,
                                              credits=0, endpoint="overpass")
    except Exception as e:  # noqa: BLE001 — the spend is also on the job row
        logger.warning("place_shape_call_log_failed", error=str(e)[:200])
    for c in (details, overpass):
        if c is not None:
            try:
                await c.aclose()
            except Exception:  # noqa: BLE001
                pass
