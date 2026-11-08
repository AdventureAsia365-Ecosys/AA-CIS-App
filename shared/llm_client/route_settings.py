"""AA-1042 (S268, route control center) — the gateway's read of ``shared.llm_route_settings`` (CIS migration 370).

One row: the route ORDER MODE (``model_first``: each model on all its deployments before the next model, the tech
lead's S267 default; ``aws_first``: every AWS deployment of a route first, then the vendor APIs,
``client.route_attempts``)
and the route config version in force (``shared.llm_route_config_version``, written by ``route_versions``).

- ``order_mode_sync()``: the DB value; while the row cannot be read (before 370, DB down, a unit test without a DB) the
  env var ``LLM_ROUTE_AWS_FIRST`` (on = aws_first) is the bootstrap default, then model_first. DB wins.
- ``current_version_sync()``: the version this process routes with (None without the row).
- ``watch_version()``: every gateway route resolution calls it first (``role_config.get_stage_config_sync`` and
  ``deployments._ensure_loaded``, the two caches that hold route rows), whatever the stage or order mode. Inside the
  ``_CACHE_TTL_SECONDS`` window it is a clock read (no DB); after it, one small SELECT per process. When this process
  reads a NEW version number, it drops its stage cache (``role_config``) and its deployment cache (``deployments``),
  so an admin change made on another task (api vs worker) reaches every process and every stage within
  ``_CACHE_TTL_SECONDS`` instead of the 5 minutes of the deployment cache. The watch runs before the deployment
  cache reloads, so the first version a process reads is never newer than the rows it routes with.
- ``override_order_mode(mode)``: a context-variable override for the admin preview (the proposed mode for one
  request; a concurrent gateway call never sees it).

Read-only and sync (the gateway's route path is sync). Never raises.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

import structlog

from . import connections

logger = structlog.get_logger()

MODEL_FIRST = "model_first"
AWS_FIRST = "aws_first"
ORDER_MODES = (MODEL_FIRST, AWS_FIRST)
ROUTE_AWS_FIRST_ENV = "LLM_ROUTE_AWS_FIRST"
_ON = ("1", "true", "on", "yes")

_CACHE_TTL_SECONDS = 20.0
_FAILURE_TTL_SECONDS = 20.0
_SELECT_SQL = "SELECT order_mode, current_version FROM shared.llm_route_settings WHERE singleton"

_state: Optional[tuple[str, int]] = None
_loaded_at = 0.0
_failed_at = 0.0
_seen_version: Optional[int] = None
_load_lock = threading.Lock()

_order_override: ContextVar[Optional[str]] = ContextVar("llm_route_order_override", default=None)


async def _fetch() -> Optional[dict]:
    conn = await connections._connect()
    try:
        row = await conn.fetchrow(_SELECT_SQL)
        return dict(row) if row is not None else None
    finally:
        await conn.close()


def _fresh(now: float) -> bool:
    return bool((_loaded_at and now - _loaded_at < _CACHE_TTL_SECONDS)
                or (_failed_at and now - _failed_at < _FAILURE_TTL_SECONDS))


def _on_new_version(version: int) -> None:
    """A version this process has not routed with yet: drop the caches that hold route rows."""
    global _seen_version
    previous, _seen_version = _seen_version, version
    if previous is None or previous == version:
        return
    try:
        from . import deployments, role_config
        role_config.invalidate()
        deployments.invalidate()
        logger.info("llm_route_config_version_changed", before=previous, after=version)
    except Exception as e:  # noqa: BLE001 — a cache drop never fails a call
        logger.warning("llm_route_cache_invalidate_failed", error=str(e)[:200])


def _load(now: float) -> None:
    global _state, _loaded_at, _failed_at
    try:
        row = connections.run_sync(_fetch)
    except Exception as e:  # noqa: BLE001 — no table before 370, DB down: the env var decides
        _failed_at = now
        logger.warning("llm_route_settings_read_failed", error=str(e)[:300])
        return
    if isinstance(row, dict) and row.get("order_mode") in ORDER_MODES and row.get("current_version") is not None:
        _state = (row["order_mode"], int(row["current_version"]))
        _loaded_at, _failed_at = now, 0.0
        _on_new_version(_state[1])
    else:                                    # no row (or a test double): the bootstrap default
        _state = None
        _failed_at = now


def _ensure_loaded() -> None:
    if _fresh(time.monotonic()):
        return
    with _load_lock:
        now = time.monotonic()
        if _fresh(now):
            return
        _load(now)


def watch_version() -> None:
    """Called at the start of every gateway route resolution (stage config and deployment reads): re-read the settings
    row when the cache is older than ``_CACHE_TTL_SECONDS`` and drop the route caches of this process on a new version.
    A clock read on a cache hit; never raises (a failed read keeps the caches and their own TTLs)."""
    try:
        _ensure_loaded()
    except Exception as e:  # noqa: BLE001 — the watch never fails a call
        logger.warning("llm_route_version_watch_failed", error=str(e)[:200])


def env_order_mode() -> str:
    """The bootstrap default: ``LLM_ROUTE_AWS_FIRST`` on = aws_first, anything else model_first."""
    return AWS_FIRST if os.environ.get(ROUTE_AWS_FIRST_ENV, "").strip().lower() in _ON else MODEL_FIRST


def order_mode_sync() -> str:
    """The route order mode the gateway runs now (see the module docstring)."""
    override = _order_override.get()
    if override in ORDER_MODES:
        return override
    _ensure_loaded()
    return _state[0] if _state is not None else env_order_mode()


def order_mode_source() -> str:
    """Where ``order_mode_sync`` takes its value from: ``db`` or ``env`` (the bootstrap default)."""
    _ensure_loaded()
    return "db" if _state is not None else "env"


def current_version_sync() -> Optional[int]:
    """The route config version in force as this process last read it (None without the settings row)."""
    _ensure_loaded()
    return _state[1] if _state is not None else None


def cached_version() -> Optional[int]:
    """The route config version this process last read, WITHOUT a read (AA-1052 lineage stamp): None when the
    process has not read the settings row yet or has no row. The gateway's ``watch_version`` refreshes it at the
    start of every route resolution, so after a writer's calls it names the version they routed with."""
    return _state[1] if _state is not None else None


@contextmanager
def override_order_mode(mode: Optional[str]) -> Iterator[None]:
    """For the admin preview: ``order_mode_sync`` answers ``mode`` inside the block (this context only)."""
    token = _order_override.set(mode if mode in ORDER_MODES else None)
    try:
        yield
    finally:
        _order_override.reset(token)


def invalidate() -> None:
    """Re-read the row on the next call (this process just changed it)."""
    global _loaded_at, _failed_at
    _loaded_at = 0.0
    _failed_at = 0.0


__all__ = ["AWS_FIRST", "MODEL_FIRST", "ORDER_MODES", "cached_version", "current_version_sync", "env_order_mode",
           "invalidate", "order_mode_source", "order_mode_sync", "override_order_mode", "watch_version"]
