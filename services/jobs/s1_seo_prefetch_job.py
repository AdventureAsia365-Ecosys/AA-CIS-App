"""AA-653 — `s1_seo_prefetch` job kind: buy S1's DataForSEO data for many tours at once.

Enqueued by the S1 Rewrite page before it starts the per-tour rewrites, so the rewrites find a
fresh `seo_context` row and buy nothing (see services/seo_intelligence/s1_prefetch.py).

Payload: {"tour_ids": [...], "tenant_id": str}
"""
from __future__ import annotations

import asyncpg
import structlog

from shared.cost_guard import load_run_budget
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "s1_seo_prefetch"
AA_INTERNAL = "00000000-0000-0000-0000-000000000001"


async def _market(conn, tenant_id: str) -> tuple[int, str, str]:
    from services.seo_intelligence.seed_builder import resolve_buyer_market
    from shared.services.tenant_config_service import TenantConfigService
    try:
        cfg = await TenantConfigService(conn).get_seo_config(tenant_id)
        return resolve_buyer_market(cfg.target_market)
    except Exception as e:  # same default as process_seo()
        logger.warning("s1_prefetch_market_resolve_failed", tenant_id=tenant_id, error=str(e))
        from services.seo_intelligence.dataforseo_client import (
            DEFAULT_LANGUAGE_CODE, DEFAULT_LOCATION_CODE, DEFAULT_LOCATION_NAME)
        return DEFAULT_LOCATION_CODE, DEFAULT_LOCATION_NAME, DEFAULT_LANGUAGE_CODE


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=900)
async def run(ctx: JobContext) -> dict:
    from services.seo_intelligence.dataforseo_client import DataForSEOClient
    from services.seo_intelligence.s1_prefetch import prefetch
    from shared.secrets import get_database_url

    tour_ids = [str(t) for t in (ctx.payload.get("tour_ids") or [])]
    if not tour_ids:
        raise NonRetryable("payload has no tour_ids")
    tenant_id = ctx.payload.get("tenant_id") or AA_INTERNAL

    budget = await load_run_budget(ctx.pool, "dfs", KIND)
    conn = await asyncpg.connect(get_database_url())
    try:
        rows = [dict(r) for r in await conn.fetch(
            """SELECT tour_id, src_name, country, activities FROM silver_aa_internal.raw_tours
                WHERE tour_id = ANY($1::uuid[]) AND source_status = 'active'""", tour_ids)]
        location_code, _name, language_code = await _market(conn, tenant_id)
        await ctx.progress(phase="fetching", tours=len(rows))

        async def _progress(**f):
            await ctx.progress(phase="fetching", **f)
        client = DataForSEOClient(tenant_id=tenant_id, budget=budget)
        summary = await prefetch(conn, rows, tenant_id=tenant_id, location_code=location_code,
                                 language_code=language_code, client=client, progress=_progress)
    finally:
        await conn.close()
    ctx.add_cost(budget.run_spent)
    ctx.set_result(summary)
    await ctx.progress(phase="done", **summary)
    return summary
