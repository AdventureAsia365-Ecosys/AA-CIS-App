"""AA-1122 item 5 — `tour_paa_backfill` job kind: fill the People Also Ask questions of published tours.

Store first ($0), then, when the payload says ``paid`` and the caps allow it, ONE live SERP per distinct tour keyword
through the research cache (services/seo_intelligence/tour_paa.py). Enqueued by an administrator from the Research tab
(POST /admin/segment-research/tour-paa/run, after the free preview) and, store-only (``paid`` false), by a research run
that bought SERPs (segment_research_job.enqueue_paa_derive, once per UTC day).

Caps of a paid run, one counter over its DataForSEO calls (shared.cost_guard.SharedCap on the run budget, checked by the
client before every post): the per-run cap ``cis.spend.tour_paa_backfill_max_usd_per_run`` minus what this job already
spent (retries never start a fresh budget) and the research month cap ``cis.spend.research_max_usd_per_month`` (this
kind is a research kind: its spend counts there, RESEARCH_JOB_KINDS). A month already spent, an unreadable ledger or a
per-run cap of 0 refuses the paid part before anything is bought; the store part still runs.

Payload: {"paid": bool, "tour_ids"?: [uuid, ...]}
"""
from __future__ import annotations

import structlog

from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "tour_paa_backfill"


async def paid_budget(pool, job_id: str):
    """The DataForSEO run budget of a paid backfill with its two shared caps, or (None, reason) when nothing may be
    bought. Fail closed: an unreadable ledger buys nothing."""
    from services.seo_intelligence import research_settings as rs
    from shared import settings
    from shared.cost_guard import SharedCap, job_spend, load_research_month_cap, load_run_budget

    try:
        month = await load_research_month_cap(pool)
        prior = await job_spend(pool, job_id)
    except Exception as exc:  # noqa: BLE001 — unknown spend = buy nothing
        return None, f"research spend ledger unreadable: {type(exc).__name__}"
    run = SharedCap("run", float(settings.get(rs.TOUR_PAA_MAX_USD_PER_RUN)), spent_at_start=prior, limit="per_run")
    if month.remaining() <= 0:
        return None, f"research month cap reached: spent ${month.spent:.2f} of ${month.cap_usd:.2f}"
    if run.remaining() <= 0:
        return None, f"per-run cap reached or 0 (${run.cap_usd:.2f}, this job spent ${prior:.2f})"
    budget = await load_run_budget(pool, "dfs", KIND)
    budget.shared_caps = (month, run)
    return budget, None


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=1800)
async def run(ctx: JobContext) -> dict:
    from services.seo_intelligence import tour_paa
    from services.seo_intelligence.dataforseo_client import DataForSEOClient

    paid = bool(ctx.payload.get("paid"))
    tour_ids = ctx.payload.get("tour_ids") or None
    if tour_ids is not None and not isinstance(tour_ids, list):
        raise NonRetryable("tour_ids must be a list")
    client, budget, refused = None, None, None
    if paid:
        budget, refused = await paid_budget(ctx.pool, ctx.job_id)
        if budget is not None:
            client = DataForSEOClient(budget=budget, stage=tour_paa.STAGE)
            client.meta = {"job": KIND}
    await ctx.progress(phase="filling", paid=paid and budget is not None, refused=refused)
    result = await tour_paa.run(ctx.pool, paid=budget is not None, client=client, dfs_budget=budget,
                                tour_ids=tour_ids)
    if budget is not None:
        ctx.add_cost(budget.run_spent)
        result["budget"] = budget.summary()
    result["paid_refused"] = refused
    result["coverage"] = await tour_paa.coverage(ctx.pool)
    ctx.set_result(result)
    await ctx.progress(phase="done")
    logger.info("tour_paa_backfill_done", **{k: v for k, v in result.items() if isinstance(v, (int, str, type(None)))})
    return result
