"""AA-712 — `place_geocode` job kind (platform, debounced): pending place names → coordinates →
shared.destinations → relink of every unlinked place / night / card → transit-aware outlier guard
(services/places/pipeline.geocode_pending; design §3 + §4).

The ONLY writer of shared.destinations coordinates and of geo.place_name status (cap 1). Never
overwrites an admin-fixed (`manual`) destination.

Payload: {"reason"?, "limit"?, "coalesced"?}. Enqueued (debounced, one queued job absorbs the rest)
by place_extract when names wait. Re-enqueues itself while more names are due.

Paid calls: one `a3_place_locate` per 40 names (budget row bedrock/job:place_geocode, $3/run) and,
with GEOCODER_PROVIDER = mapbox_permanent | amazon_location, one geocoder request per located name
(budget rows geocoder/global $25/day and geocoder/job:place_geocode $50/run; spend in
geo.geocode_call and the job's own cost_usd). Default model_only: no external call.
"""
from __future__ import annotations

from typing import Optional

import structlog

from services.places import llm, locate, store
from services.places.enqueue import GEOCODE_KIND, GRAPH_KIND, enqueue_debounced
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

    async def confirm(self, name, lat, lng, country):
        if self.budget is not None:
            self.budget.check(self.cost_per_request)
        try:
            return await self.inner.confirm(name, lat, lng, country)
        finally:
            if self.budget is not None:
                self.budget.charge(self.cost_per_request)

    async def aclose(self):
        await self.inner.aclose()


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=1800, background=True)
async def run(ctx: JobContext) -> Optional[dict]:
    p = ctx.payload or {}
    limit = max(1, min(int(p.get("limit") or MAX_NAMES_PER_RUN), MAX_NAMES_PER_RUN))
    inner = get_geocoder()
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
        async with ctx.pool.acquire() as conn:
            remaining = await store.pending_names_due(conn)
        result["pending_names_due"] = remaining
        if remaining and result.get("names"):
            result["next_job"], _ = await enqueue_debounced(ctx.pool, KIND, reason=f"{KIND}:continue",
                                                            created_by=KIND, debounce_s=CONTINUE_DEBOUNCE_S)
    except Exception as e:  # noqa: BLE001 — follow-ups are best-effort
        result["follow_up_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    logger.info("place_geocode_done", **{k: v for k, v in result.items() if not isinstance(v, (dict, list))})
    return result
