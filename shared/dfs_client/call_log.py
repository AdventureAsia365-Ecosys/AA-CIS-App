"""AA-618 — persist shared.dfs_call_log for every real DataForSEO interaction.

Mirrors shared/llm_client/call_log.py exactly in spirit: fire-and-forget (a logging failure must
never break an SEO fetch), with an own-connection async variant, a pool variant, and a sync
wrapper. One row per DFS interaction:
  - a live HTTP call    -> fetched_live=True, cost_usd = the DFS response's own `cost` field
  - a cache hit (Redis/ -> cache_hit=True, cost_usd=0 (no DFS call was made)
    search_demand/seo_context)

DataForSEO returns a top-level `cost` (float, total for the request) on every live response — see
extract_cost() below. Callers that made a live call pass that through; cache-hit callers pass
cost_usd=0, cache_hit=True.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

logger = structlog.get_logger()

_INSERT_SQL = """
    INSERT INTO shared.dfs_call_log
        (tenant_id, tour_id, endpoint, keyword, location_code, cost_usd, cache_hit, fetched_live,
         keyword_count, meta)
    VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
"""


def extract_cost(response: dict) -> Optional[float]:
    """Read the real cost from a DataForSEO JSON response. DFS puts a top-level `cost` (float,
    total for the whole request) on every live endpoint; some responses also carry a per-task
    `tasks[i].cost`. Prefer the top-level total (matches how the bulk endpoint is flat-priced per
    request regardless of keyword count). Returns None if the field is absent/malformed — the log
    row still records the call, just with NULL cost rather than a fabricated number."""
    if not isinstance(response, dict):
        return None
    top = response.get("cost")
    if isinstance(top, (int, float)):
        return float(top)
    try:
        tasks = response.get("tasks") or []
        total = sum(float(t["cost"]) for t in tasks if isinstance(t, dict) and t.get("cost") is not None)
        return total or None
    except (TypeError, ValueError):
        return None


async def record_dfs_call(
    *, endpoint: str, cache_hit: bool = False, fetched_live: bool = False,
    cost_usd: Optional[float] = None, tenant_id: Optional[str] = None,
    tour_id: Optional[str] = None, keyword: Optional[str] = None,
    location_code: Optional[int] = None, keyword_count: Optional[int] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    """Own-connection async variant — for call sites (process_seo) that open ad-hoc asyncpg
    connections with no pool threaded in. Fire-and-forget."""
    try:
        conn = await asyncpg.connect(get_database_url(), ssl="require")
        try:
            await conn.execute(
                _INSERT_SQL, tenant_id, tour_id, endpoint, keyword, location_code,
                cost_usd, cache_hit, fetched_live, keyword_count,
                json.dumps(meta) if meta is not None else None,
            )
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("dfs_call_log_write_failed", endpoint=endpoint, error=str(e))


async def record_dfs_call_with_pool(
    pool, *, endpoint: str, cache_hit: bool = False, fetched_live: bool = False,
    cost_usd: Optional[float] = None, tenant_id: Optional[str] = None,
    tour_id: Optional[str] = None, keyword: Optional[str] = None,
    location_code: Optional[int] = None, keyword_count: Optional[int] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    """Pool variant — for call sites (segment_research) that already have an asyncpg.Pool in
    scope, to avoid opening a fresh connection per call. Fire-and-forget."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL, tenant_id, tour_id, endpoint, keyword, location_code,
                cost_usd, cache_hit, fetched_live, keyword_count,
                json.dumps(meta) if meta is not None else None,
            )
    except Exception as e:
        logger.warning("dfs_call_log_write_failed", endpoint=endpoint, error=str(e))


def record_dfs_call_sync(**kwargs) -> None:
    """Sync wrapper — same reasoning as call_log.record_call_sync(): schedule the write as a
    background task when a loop is already running, else asyncio.run(). A log write's result is
    never needed synchronously."""
    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(record_dfs_call(**kwargs))
        return
    except RuntimeError:
        pass
    try:
        asyncio.run(record_dfs_call(**kwargs))
    except Exception as e:
        logger.warning("dfs_call_log_write_failed_sync", endpoint=kwargs.get("endpoint"), error=str(e))
