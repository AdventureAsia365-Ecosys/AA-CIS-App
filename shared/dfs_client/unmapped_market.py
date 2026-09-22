"""AA-629 — persist shared.unmapped_market_requests (Tier 1 detect+record).

Written whenever seed_builder.unmatched_countries() finds a tenant declared real
target_market.countries that DFS_LOCATION_MAP does not know (as opposed to declaring nothing at
all, which is a reasonable US-default case, not a bug). One row per (tenant_id, country_code);
re-detecting the same pair bumps requested_at instead of inserting a duplicate (UNIQUE
constraint, migration 164).

Mirrors balance.py's (AA-627) write/read shape: record_* is best-effort from the CALLER's point
of view (the caller — resolve_buyer_market()'s consumers — must keep working even if this write
fails; a DB hiccup here must never break a tenant's actual Slate/SEO read), so this module itself
raises on error and the caller decides whether to swallow it (unlike balance.py's check endpoint,
which deliberately wants to fail loud — there is no equivalent "the whole point is a failed
write must surface" requirement here).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import structlog

logger = structlog.get_logger()

_UPSERT_SQL = """
    INSERT INTO shared.unmapped_market_requests (tenant_id, country_code)
    VALUES ($1::uuid, $2)
    ON CONFLICT (tenant_id, country_code)
    DO UPDATE SET requested_at = now()
    RETURNING id, tenant_id, country_code, requested_at, first_seen_at, resolved_at
"""

_LIST_UNRESOLVED_SQL = """
    SELECT country_code, COUNT(DISTINCT tenant_id) AS tenant_count,
           MIN(first_seen_at) AS first_seen_at, MAX(requested_at) AS last_requested_at,
           array_agg(DISTINCT tenant_id::text ORDER BY tenant_id::text) AS tenant_ids
      FROM shared.unmapped_market_requests
     WHERE resolved_at IS NULL
     GROUP BY country_code
     ORDER BY tenant_count DESC, last_requested_at DESC
"""


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for key in ("id", "tenant_id"):
        if d.get(key) is not None:
            d[key] = str(d[key])
    for key in ("requested_at", "first_seen_at", "resolved_at"):
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    return d


async def record_unmapped_market_request(
    conn_or_pool: asyncpg.Connection | asyncpg.Pool, tenant_id: UUID | str, country_code: str,
) -> dict[str, Any]:
    """Upsert one (tenant_id, country_code) unmapped-market request. Accepts either a live
    connection (caller already holds one, e.g. inside slate.py's own TenantConfigService(conn)
    scope — reuse it, don't acquire a second one) or a pool (acquires its own). Raises on DB
    error — callers that must not let this fail their own read path should wrap the call
    themselves (see slate.py::_tenant_market_codes()), matching this repo's convention of
    surfacing write failures at the call site closest to the decision, not swallowing them
    centrally."""
    if isinstance(conn_or_pool, asyncpg.Pool):
        async with conn_or_pool.acquire() as conn:
            row = await conn.fetchrow(_UPSERT_SQL, str(tenant_id), country_code)
    else:
        row = await conn_or_pool.fetchrow(_UPSERT_SQL, str(tenant_id), country_code)
    result = _row_to_dict(row)
    logger.info("unmapped_market_request_recorded", tenant_id=str(tenant_id), country_code=country_code)
    return result


async def list_unmapped_market_requests(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """AA-629 Tier 2 — every unresolved country_code, grouped, most-waited-on first. Empty list
    when there is nothing unresolved (the healthy/expected steady state)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_LIST_UNRESOLVED_SQL)
    out = []
    for r in rows:
        d = dict(r)
        d["first_seen_at"] = d["first_seen_at"].isoformat() if d["first_seen_at"] else None
        d["last_requested_at"] = d["last_requested_at"].isoformat() if d["last_requested_at"] else None
        out.append(d)
    return out
