"""AA-997 — `place_relocate` job kind (platform, background, cap 1): re-locate destinations whose point came from the
model through the geocoder-only path (services/places/relocate.py; S254 geocode audit, fix plan step 1).

Payload: {"mode": "dry_run" | "apply" (default dry_run), "located_by"?: ["llm"] (| "mapbox"), "country"?,
"limit"? (apply, ≤ 400 a run), "hide_unconfirmed"? (default false), "reason"?}. Queued by POST /admin/places/relocate.

dry_run: no paid call and no write — the counts and the Geoapify credit estimate in the job result.
apply: ≤ `limit` rows a run under the Geoapify lock shared with place_geocode / place_shape (services/places/geolock);
re-enqueues itself while rows remain. Geoapify credits: one search request (1 credit) per query of a row's name
(geomatch.normalise: 1, or 2 for "X to Y"); this job stops RESERVE_CREDITS below the daily limit
(GEOAPIFY_DAILY_CREDIT_LIMIT, default 2,500 of the Free plan's 3,000/day), so new names of the day are still geocoded,
and resumes after the UTC credit reset. Free plan $0; on a paid plan GEOCODER_COST_PER_REQUEST_USD per request (the
geocoder/job budget rows apply as in place_geocode). No model call. Each run logs its requests in geo.geocode_call.
A run that confirmed or hid rows queues place_dedupe and tour_graph_build.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from services.jobs.place_geocode_job import BudgetedGeocoder
from services.places import relocate, store
from services.places.enqueue import (GRAPH_KIND, RELOCATE_KIND, enqueue_debounced, enqueue_dedupe,
                                     next_credit_reset)
from services.places.geocoder import DEFAULT_DAILY_CREDITS, _env_float, get_geocoder
from services.places.geolock import geoapify_lock
from shared.cost_guard import BudgetExceeded, load_run_budget, maybe_alert
from shared.jobs.registry import JobContext, enqueue, job_kind

logger = structlog.get_logger()

KIND = RELOCATE_KIND
LOCKED_DELAY_S = 120
CONTINUE_DELAY_S = 15


def _payload(p: dict) -> dict:
    mode = p.get("mode") if p.get("mode") in ("dry_run", "apply") else "dry_run"
    limit = max(1, min(int(p.get("limit") or relocate.MAX_ROWS_PER_RUN), relocate.MAX_ROWS_PER_RUN))
    return {"mode": mode, "located_by": relocate.located_by_list(p.get("located_by")),
            "country": (p.get("country") or None), "limit": limit,
            "hide_unconfirmed": bool(p.get("hide_unconfirmed"))}


def daily_credits() -> int:
    """This job's share of the day: the place_geocode credit limit minus RESERVE_CREDITS (never below 0)."""
    limit = int(_env_float("GEOAPIFY_DAILY_CREDIT_LIMIT", DEFAULT_DAILY_CREDITS))
    return max(0, limit - relocate.RESERVE_CREDITS)


async def _requeue(ctx: JobContext, p: dict, *, run_after: datetime, key: Optional[str]) -> str:
    job_id, _ = await enqueue(ctx.pool, KIND, {**p, "reason": f"{KIND}:continue"}, created_by=KIND,
                              run_after=run_after, idempotency_key=key)
    return job_id


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=1800, background=True)
async def run(ctx: JobContext) -> Optional[dict]:
    p = _payload(ctx.payload or {})
    inner = get_geocoder()
    if p["mode"] == "dry_run":
        async with ctx.pool.acquire() as conn:
            return await relocate.dry_run(conn, located_by=p["located_by"], country=p["country"],
                                          cost_per_request=inner.cost_per_request, daily_credits=daily_credits())
    if daily_credits() <= 0:
        return {"status": "refused_no_credit_share", "detail": (
            f"GEOAPIFY_DAILY_CREDIT_LIMIT leaves no share after the {relocate.RESERVE_CREDITS} credits kept for "
            "place_geocode: raise the limit; re-queueing would wait for a credit reset that never helps")}
    if not inner.calls_external:
        return {"status": "refused_no_geocoder", "geocoder": inner.name,
                "detail": "apply needs GEOCODER_PROVIDER=geoapify: re-locating with model_only would change nothing"}
    async with geoapify_lock(ctx.pool) as held:
        if not held:
            job = await _requeue(ctx, p, run_after=datetime.now(timezone.utc) + timedelta(seconds=LOCKED_DELAY_S),
                                 key=None)
            return {"status": "waiting_for_geoapify_lock", "next_job": job}
        return await _apply_locked(ctx, p, inner)


async def _apply_locked(ctx: JobContext, p: dict, inner) -> dict:
    started = datetime.now(timezone.utc)
    if hasattr(inner, "credits_used_today"):
        async with ctx.pool.acquire() as conn:
            inner.credits_used_today = await store.geocoder_requests_today(conn, inner.name)
        inner.daily_credit_limit = daily_credits()
    geo_budget = await load_run_budget(ctx.pool, "geocoder", KIND)
    geocoder = BudgetedGeocoder(inner, geo_budget)
    result: dict = {}
    try:
        result = await relocate.apply(ctx.pool, geocoder, located_by=p["located_by"], country=p["country"],
                                      limit=p["limit"], hide_unconfirmed=p["hide_unconfirmed"],
                                      progress=ctx.progress)
    except BudgetExceeded as e:
        ctx.set_result({"status": "stopped_budget", **result})
        await maybe_alert(ctx.pool, geo_budget, reason=str(e))
        raise
    finally:
        cost = round(inner.requests * inner.cost_per_request, 6)
        ctx.add_cost(cost)
        try:
            async with ctx.pool.acquire() as conn:
                await store.log_geocode_calls(conn, inner.name, inner.requests, cost, ctx.job_id)
        except Exception as e:  # noqa: BLE001 — the spend is also on the job row
            logger.warning("geocode_call_log_failed", error=str(e)[:200])
        await inner.aclose()
    result["geocoder_requests"] = inner.requests
    result["geocoder_cost_usd"] = round(inner.requests * inner.cost_per_request, 6)
    try:
        if result.get("confirmed") or (p["hide_unconfirmed"] and result.get("done")):
            result["dedupe_job"], _ = await enqueue_dedupe(ctx.pool, reason=KIND, created_by=KIND)
            result["graph_job"], _ = await enqueue_debounced(ctx.pool, GRAPH_KIND, reason=KIND, created_by=KIND)
        if result.get("stopped_geocoder_unavailable"):
            result["status"] = "stopped_geocoder_unavailable"
        elif result.get("stopped_credit_limit"):
            result["status"] = "stopped_credit_limit"
            at = next_credit_reset(started)
            result["next_job"] = await _requeue(ctx, p, run_after=at, key=f"{KIND}:credit_reset:{at:%Y-%m-%d}")
            result["next_run_after"] = at.isoformat()
        elif result.get("remaining") and result.get("done"):
            result["next_job"] = await _requeue(
                ctx, p, run_after=datetime.now(timezone.utc) + timedelta(seconds=CONTINUE_DELAY_S), key=None)
            result["status"] = "continuing"
        else:
            result["status"] = "ok"
    except Exception as e:  # noqa: BLE001 — follow-ups are best-effort
        result["follow_up_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    logger.info("place_relocate_done", **{k: v for k, v in result.items() if not isinstance(v, (dict, list))})
    return result
