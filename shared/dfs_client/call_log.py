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

AA-665 C2 (migration 293): every row also carries `job_id`, the shared.job running when the call was made — the same
contextvar the LLM call log uses (shared/llm_client/call_log.current_job_id(), bound by the worker around each
handler). A caller may pass `job_id` explicitly; otherwise it is read at call time, in the caller's context (the sync
wrapper reads it before scheduling the write). A value that is not a UUID is dropped (logged, row kept), so a bad bind
never costs the cost row (the S254 llm_call_log lesson).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Optional

import asyncpg
import structlog

from shared.llm_client.call_log import current_job_id
from shared.secrets import get_database_url

logger = structlog.get_logger()

_INSERT_SQL = """
    INSERT INTO shared.dfs_call_log
        (tenant_id, tour_id, endpoint, keyword, location_code, cost_usd, cache_hit, fetched_live,
         keyword_count, meta, job_id)
    VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::uuid)
"""

_UNSET = object()


def _job_id(job_id: Any = _UNSET, *, endpoint: str = "") -> Optional[str]:
    """The job id to log: the explicit one, else the bound current_job_id(); None unless it parses as a UUID."""
    value = current_job_id() if job_id is _UNSET else job_id
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        logger.warning("dfs_call_log_job_id_not_uuid", endpoint=endpoint, job_id=str(value)[:64])
        return None


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
    meta: Optional[dict[str, Any]] = None, job_id: Any = _UNSET,
) -> None:
    """Own-connection async variant — for call sites (process_seo) that open ad-hoc asyncpg
    connections with no pool threaded in. Fire-and-forget."""
    job = _job_id(job_id, endpoint=endpoint)
    try:
        conn = await asyncpg.connect(get_database_url(), ssl="require")
        try:
            await conn.execute(
                _INSERT_SQL, tenant_id, tour_id, endpoint, keyword, location_code,
                cost_usd, cache_hit, fetched_live, keyword_count,
                json.dumps(meta) if meta is not None else None, job,
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
    meta: Optional[dict[str, Any]] = None, job_id: Any = _UNSET,
) -> None:
    """Pool variant — for call sites (segment_research) that already have an asyncpg.Pool in
    scope, to avoid opening a fresh connection per call. Fire-and-forget."""
    job = _job_id(job_id, endpoint=endpoint)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL, tenant_id, tour_id, endpoint, keyword, location_code,
                cost_usd, cache_hit, fetched_live, keyword_count,
                json.dumps(meta) if meta is not None else None, job,
            )
    except Exception as e:
        logger.warning("dfs_call_log_write_failed", endpoint=endpoint, error=str(e))


def record_dfs_call_sync(**kwargs) -> None:
    """Sync wrapper — same reasoning as call_log.record_call_sync(): schedule the write as a
    background task when a loop is already running, else asyncio.run(). A log write's result is
    never needed synchronously. The job id is read here, in the caller's context, before the write is scheduled."""
    kwargs["job_id"] = _job_id(kwargs.get("job_id", _UNSET), endpoint=kwargs.get("endpoint") or "")
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
