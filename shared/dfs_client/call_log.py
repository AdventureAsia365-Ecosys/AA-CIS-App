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

# AA-804 (ledger v2, migration 319): the same row plus its outcome, latency, error, stage and app. Used for every row
# once 319 is applied; before that (UndefinedColumnError) the row is written with _INSERT_SQL (its v2 fields dropped)
# and v2 is not tried again for _V2_RETRY_SECONDS.
_INSERT_V2_SQL = """
    INSERT INTO shared.dfs_call_log
        (tenant_id, tour_id, endpoint, keyword, location_code, cost_usd, cache_hit, fetched_live,
         keyword_count, meta, job_id, status, latency_ms, error, stage, app)
    VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::uuid, $12, $13, $14, $15, $16)
"""
_V2_RETRY_SECONDS = 300.0
_v2_missing_at = 0.0
APP = "cis"
# shared.dfs_call_log.status (AA-804): ok = an answered live call or a cache hit; refused = nothing billed because the
# run's budget, auth or payment said no; error = the call failed.
STATUSES = ("ok", "refused", "error")

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


def _v2_values(status, latency_ms, error, stage, app) -> tuple:
    lat = latency_ms if isinstance(latency_ms, int) and not isinstance(latency_ms, bool) and latency_ms >= 0 else None
    return (status if status in STATUSES else "ok", lat,
            str(error)[:500] if error else None, str(stage)[:100] if stage else None, str(app or APP)[:40])


def _v2_ready() -> bool:
    import time as _time
    return not (_v2_missing_at and _time.monotonic() - _v2_missing_at < _V2_RETRY_SECONDS)


async def _insert(conn, base: tuple, v2: tuple) -> None:
    """The v2 INSERT when migration 319 is applied, else the pre-319 one (the row is never lost)."""
    global _v2_missing_at
    if _v2_ready():
        try:
            await conn.execute(_INSERT_V2_SQL, *base, *v2)
            return
        except asyncpg.exceptions.UndefinedColumnError:
            import time as _time
            _v2_missing_at = _time.monotonic()
            logger.warning("dfs_call_log_v2_columns_missing", hint="migration 319 not applied: status / latency "
                                                                   "dropped from this row")
    await conn.execute(_INSERT_SQL, *base)


async def record_dfs_call(
    *, endpoint: str, cache_hit: bool = False, fetched_live: bool = False,
    cost_usd: Optional[float] = None, tenant_id: Optional[str] = None,
    tour_id: Optional[str] = None, keyword: Optional[str] = None,
    location_code: Optional[int] = None, keyword_count: Optional[int] = None,
    meta: Optional[dict[str, Any]] = None, job_id: Any = _UNSET,
    status: Optional[str] = None, latency_ms: Optional[int] = None, error: Optional[str] = None,
    stage: Optional[str] = None, app: Optional[str] = None,
) -> None:
    """Own-connection async variant — for call sites (process_seo) that open ad-hoc asyncpg
    connections with no pool threaded in. Fire-and-forget.

    AA-804 (ledger v2): ``status`` (ok | refused | error; default ok), ``latency_ms`` of the live call, ``error`` (why
    a refused / failed call did not answer), ``stage`` and ``app`` (default cis). A refused / failed call is written
    with cost_usd 0 and fetched_live False."""
    job = _job_id(job_id, endpoint=endpoint)
    try:
        conn = await asyncpg.connect(get_database_url(), ssl="require")
        try:
            await _insert(conn, (
                tenant_id, tour_id, endpoint, keyword, location_code,
                cost_usd, cache_hit, fetched_live, keyword_count,
                json.dumps(meta) if meta is not None else None, job,
            ), _v2_values(status, latency_ms, error, stage, app))
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
    status: Optional[str] = None, latency_ms: Optional[int] = None, error: Optional[str] = None,
    stage: Optional[str] = None, app: Optional[str] = None,
) -> None:
    """Pool variant — for call sites (segment_research) that already have an asyncpg.Pool in
    scope, to avoid opening a fresh connection per call. Fire-and-forget. AA-804: see record_dfs_call."""
    job = _job_id(job_id, endpoint=endpoint)
    try:
        async with pool.acquire() as conn:
            await _insert(conn, (
                tenant_id, tour_id, endpoint, keyword, location_code,
                cost_usd, cache_hit, fetched_live, keyword_count,
                json.dumps(meta) if meta is not None else None, job,
            ), _v2_values(status, latency_ms, error, stage, app))
    except Exception as e:
        logger.warning("dfs_call_log_write_failed", endpoint=endpoint, error=str(e))


def record_dfs_call_sync(**kwargs) -> None:
    """Sync wrapper — same reasoning as call_log.record_call_sync(): a log write's result is never needed
    synchronously. The job id is read here, in the caller's context, before the write is scheduled.

    AA-804: under a running loop (every async DataForSEOClient method) the write runs on its own non-daemon thread
    (shared.llm_client.call_log._in_background, the AA-927 fix for llm_call_log): an unreferenced
    asyncio.ensure_future task could be garbage-collected or cancelled when the caller's loop closed, losing the row
    of a billed call."""
    kwargs["job_id"] = _job_id(kwargs.get("job_id", _UNSET), endpoint=kwargs.get("endpoint") or "")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        from shared.llm_client.call_log import _in_background
        _in_background(record_dfs_call, kwargs)
        return
    try:
        asyncio.run(record_dfs_call(**kwargs))
    except Exception as e:
        logger.warning("dfs_call_log_write_failed_sync", endpoint=kwargs.get("endpoint"), error=str(e))
