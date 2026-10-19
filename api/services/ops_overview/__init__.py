"""AA-858 — the ops overview: a business view of the whole ecosystem for /admin/overview.

``build_ops_overview(pool, redis)`` returns four independent sections, each cached on its own (``cache.py``):

- ``health`` — CloudWatch GetMetricData + DescribeAlarms (``cloudwatch``), 300 s, the only direct AWS call.
- ``tripplanner`` — tripplanner.v_ops_traffic_daily + shared.llm_call_log (``activity``), 300 s.
- ``booking`` — booking.v_ops_funnel_daily + booking.v_ops_case_status (``activity``), 300 s.
- ``cost`` — api.services.platform_cost.cost_by_app (AA-884, via ``cost``), 900 s; its lazy CE refresh is AA-884's.

One failing section never fails the endpoint: it comes back as ``{"status": "unavailable", "reason", "message"}``.
No AWS call is made without Redis (health → ``cache_offline``). No new env var: names derive from ``ENVIRONMENT``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from api.services.ops_overview import activity, cache, cloudwatch, cost

HEALTH_TTL_S = 300
ACTIVITY_TTL_S = 300
COST_TTL_S = 900

SOURCES = {
    "health": "AWS CloudWatch (acc2, us-west-1): API Gateway Count/5XXError/4XXError/Latency for the CIS and Booking "
              "APIs, Lambda Invocations/Errors/Throttles/Duration for TripPlanner, and alarm states by name prefix",
    "tripplanner": "tripplanner.v_ops_traffic_daily (TripPlanner contract view, tp-015) and shared.llm_call_log "
                   "stage tp_search_embed",
    "booking": "booking.v_ops_funnel_daily and booking.v_ops_case_status (Booking contract views, bk-017)",
    "cost": "AWS Cost Explorer by App tag (acc2) and LLM gateway estimates, via the Platform cost module "
            "(/admin/platform/cost)",
}


async def build_ops_overview(pool: Any, redis: Any = None, now: Optional[datetime] = None) -> dict:
    async def health() -> dict:
        return await asyncio.to_thread(cloudwatch.collect_health, now)

    async def tripplanner() -> dict:
        return await activity.tripplanner_activity(pool, now)

    async def booking() -> dict:
        return await activity.booking_funnel(pool, now)

    async def cost_section() -> dict:
        return await cost.cost_card(pool, redis, now)

    h, tp, bk, c = await asyncio.gather(
        cache.cached_section(redis, "health", HEALTH_TTL_S, health, needs_cache=True),
        cache.cached_section(redis, "tripplanner", ACTIVITY_TTL_S, tripplanner, needs_cache=False),
        cache.cached_section(redis, "booking", ACTIVITY_TTL_S, booking, needs_cache=False),
        cache.cached_section(redis, "cost", COST_TTL_S, cost_section, needs_cache=False),
    )
    sections = {"health": h, "tripplanner": tp, "booking": bk, "cost": c}
    return {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        **sections,
        "sources": SOURCES,
        "_cached": all(s.get("_cached") for s in sections.values()),
    }
