"""AA-712 — `place_geocode` job kind (platform, debounced): pending place names → coordinates →
shared.destinations → relink of every unlinked place / night / card → transit-aware outlier guard
(services/places/pipeline.geocode_pending; design §3 + §4).

The ONLY writer of shared.destinations coordinates and of geo.place_name status (cap 1). Never
overwrites an admin-fixed (`manual`) destination. AA-847: every upsert / relink takes the destinations advisory lock
(serialised with place_dedupe merges); a run that inserted or moved a destination queues place_dedupe.

Payload: {"reason"?, "limit"?, "coalesced"?}. Enqueued (debounced, one queued job absorbs the rest)
by place_extract when names wait. Re-enqueues itself while more names are due.

Paid calls: one `a3_place_locate` per 40 names (budget row bedrock/job:place_geocode, $3/run) and,
with GEOCODER_PROVIDER = geoapify (default since S236), one to three Geoapify requests per located name
("X to Y" endpoints, a native-script retry for Taiwan / South Korea; generic phrases send none). Free
plan: $0, so the guard that matters is the daily CREDIT guard (GEOAPIFY_DAILY_CREDIT_LIMIT, default
2,500 of the 3,000/day quota, counted per UTC day from geo.geocode_call): when it trips, the remaining
names stay pending without a try counted and the job ends `stopped_credit_limit`; AA-990: with names still due it
queues the next run for just after the credit reset (enqueue.enqueue_after_credit_reset: next 00:00 UTC + 10 min +
jitter, one row per reset day; result keys `next_job`, `next_run_after`), so the backlog drains without a person —
before, nothing ran until the next place_extract. A setup / auth failure (no key, secret
unreadable, HTTP 401/403) ends the job the same way with `stopped_geocoder_unavailable` (names are never
marked `unknown` for it). The $ rows (geocoder/global $25/day,
geocoder/job:place_geocode $50/run) apply on a paid plan via GEOCODER_COST_PER_REQUEST_USD.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog

from services.places import llm, locate, store
from services.places.enqueue import (GEOCODE_KIND, GRAPH_KIND, SHAPE_KIND, enqueue_after_credit_reset,
                                     enqueue_debounced, enqueue_dedupe)
from services.places.geolock import geoapify_lock
from services.places.geocoder import get_geocoder
from services.places.pipeline import geocode_pending
from shared.cost_guard import BudgetExceeded, load_run_budget, maybe_alert
from shared.jobs.registry import JobContext, job_kind

logger = structlog.get_logger()

KIND = GEOCODE_KIND
MAX_NAMES_PER_RUN = 400     # 10 locate calls; more names → the job re-enqueues itself
CONTINUE_DEBOUNCE_S = 15


class BudgetedGeocoder:
    """Wraps a geocoder: checks the geocoder budget before each external request, charges after."""

    def __init__(self, inner, budget):
        self.inner = inner
        self.budget = budget
        self.name = inner.name
        self.calls_external = inner.calls_external
        self.cost_per_request = inner.cost_per_request

    @property
    def requests(self) -> int:
        return self.inner.requests

    def credits_left(self) -> int:
        fn = getattr(self.inner, "credits_left", None)
        return fn() if fn is not None else 1

    async def confirm(self, name, lat, lng, country, *, native_name=None, routes=(), settlement=False, **kw):
        """`kw`: AA-997 v2 strategy / context / max_queries, passed through only when given."""
        if self.budget is not None:
            self.budget.check(self.cost_per_request)
        before = self.inner.requests
        try:
            return await self.inner.confirm(name, lat, lng, country, native_name=native_name, routes=routes,
                                            settlement=settlement, **kw)
        finally:
            if self.budget is not None:   # charge what was really sent (0 for a skipped generic name)
                self.budget.charge(self.cost_per_request * (self.inner.requests - before))

    async def aclose(self):
        await self.inner.aclose()


LOCKED_DEBOUNCE_S = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=1800, background=True)
async def run(ctx: JobContext) -> Optional[dict]:
    """AA-847: one Geoapify client at a time — a place_shape run holding the lock makes this run re-enqueue itself
    (services/places/geolock.py: shared 5 req/s limit and daily credits)."""
    async with geoapify_lock(ctx.pool) as held:
        if not held:
            job, _ = await enqueue_debounced(ctx.pool, KIND, reason=f"{KIND}:locked", created_by=KIND,
                                             debounce_s=LOCKED_DEBOUNCE_S)
            return {"status": "waiting_for_place_shape", "next_job": job}
        return await _run_locked(ctx)


async def _run_locked(ctx: JobContext) -> Optional[dict]:
    p = ctx.payload or {}
    limit = max(1, min(int(p.get("limit") or MAX_NAMES_PER_RUN), MAX_NAMES_PER_RUN))
    inner = get_geocoder()
    # AA-990: the credit count below is the count of THIS UTC day; the reset after it is the next midnight after
    # `started`, not after the stop (a run crossing 00:00 UTC would otherwise wait a whole extra day).
    started = _utcnow()
    if hasattr(inner, "credits_used_today"):
        async with ctx.pool.acquire() as conn:
            inner.credits_used_today = await store.geocoder_requests_today(conn, inner.name)
    llm_budget = await load_run_budget(ctx.pool, "bedrock", KIND)
    geo_budget = await load_run_budget(ctx.pool, "geocoder", KIND) if inner.calls_external else None
    geocoder = BudgetedGeocoder(inner, geo_budget)
    generate = llm.make_generate(ctx.pool, llm.LOCATE_STAGE, locate.MAX_TOKENS, llm_budget, {"op": "locate"})
    result: dict = {}
    try:
        result = await geocode_pending(ctx.pool, generate, geocoder, limit=limit, job_id=ctx.job_id,
                                       progress=ctx.progress)
    except BudgetExceeded as e:
        ctx.set_result({"status": "stopped_budget", **result})
        await maybe_alert(ctx.pool, geo_budget if e.provider == "geocoder" and geo_budget else llm_budget,
                          reason=str(e))
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
        if any((result.get("relinked") or {}).values()) or result.get("suspects"):
            result["graph_job"], _ = await enqueue_debounced(ctx.pool, GRAPH_KIND, reason=KIND, created_by=KIND)
        if result.get("shapes_pending"):   # AA-847: new line destinations wait for their geometry
            result["shape_job"], _ = await enqueue_debounced(ctx.pool, SHAPE_KIND, reason=KIND, created_by=KIND)
        if result.get("inserted") or result.get("updated"):
            # AA-847 Problem 2: a destination was written or moved → the automatic duplicate merge looks again
            result["dedupe_job"], _ = await enqueue_dedupe(ctx.pool, reason=KIND, created_by=KIND)
        async with ctx.pool.acquire() as conn:
            remaining = await store.pending_names_due(conn)
        result["pending_names_due"] = remaining
        if result.get("stopped_geocoder_unavailable"):
            # no key / secret unreadable / HTTP 401-403: an operator must fix it; do not re-enqueue
            result["status"] = "stopped_geocoder_unavailable"
            logger.error("place_geocode_geocoder_unavailable", provider=inner.name,
                         deferred=result.get("deferred"), pending=remaining)
        elif result.get("stopped_credit_limit"):
            result["status"] = "stopped_credit_limit"
            if remaining:   # AA-990: resume after the daily credit reset, no person needed
                job, at, _ = await enqueue_after_credit_reset(ctx.pool, reason=f"{KIND}:credit_reset", now=started,
                                                             caller_job_id=ctx.job_id)
                result["next_job"], result["next_run_after"] = job, at.isoformat()
            logger.warning("place_geocode_credit_limit", provider=inner.name, deferred=result.get("deferred"),
                           pending=remaining, next_run_after=result.get("next_run_after"))
        elif remaining and result.get("names"):
            result["next_job"], _ = await enqueue_debounced(ctx.pool, KIND, reason=f"{KIND}:continue",
                                                            created_by=KIND, debounce_s=CONTINUE_DEBOUNCE_S)
    except Exception as e:  # noqa: BLE001 — follow-ups are best-effort
        result["follow_up_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    logger.info("place_geocode_done", **{k: v for k, v in result.items() if not isinstance(v, (dict, list))})
    return result
