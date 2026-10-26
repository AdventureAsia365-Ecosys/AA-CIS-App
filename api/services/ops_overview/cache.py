"""AA-858 — per-section Redis cache for GET /admin/overview/ops.

Each section of the ops overview is cached on its own key, so one slow or failing source never invalidates the
others. Rules (cost safety first):

- ``needs_cache=True`` (sections that call AWS: CloudWatch health): no Redis, or a Redis error → the section is
  ``unavailable: cache_offline`` and AWS is NOT called. A compute takes a ``SET NX`` lock; a request that loses the
  race gets the last good value (kept ``LAST_GOOD_TTL_S`` under ``<key>:last``) or ``unavailable: refreshing``.
- ``needs_cache=False`` (DB-only sections, and the cost card whose CE refresh is AA-884's own locked lazy refresh):
  without Redis the section is computed directly.
- A result whose ``status`` is not ``ok`` is cached only ``ERROR_TTL_S`` (60 s), so a fixed IAM grant or an applied
  migration shows up within a minute, without a call per page view meanwhile.
- AA-992 (data coverage): ``key_prefix`` puts another page's sections under its own keys, and ``single_flight=True``
  gives a DB-only section the lock + last-good behaviour of ``needs_cache`` while still computing without Redis.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

import structlog

logger = structlog.get_logger()

KEY_PREFIX = "admin:overview:ops"
KEY_VERSION = "v1"
LOCK_TTL_S = 60
LAST_GOOD_TTL_S = 24 * 3600
ERROR_TTL_S = 60


def section_key(name: str, key_prefix: str = KEY_PREFIX) -> str:
    return f"{key_prefix}:{name}:{KEY_VERSION}"


def unavailable(reason: str, message: str, **extra: Any) -> dict:
    """The one shape of a section that could not be computed (the UI shows ``message`` as is)."""
    return {"status": "unavailable", "reason": reason, "message": message, **extra}


def _decode(raw: Any) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


async def _set(redis: Any, key: str, value: dict, ttl_s: int) -> None:
    try:
        await redis.set(key, json.dumps(value, default=str), ex=ttl_s)
    except Exception as e:  # a cache write failure never fails the section
        logger.warning("ops_overview_cache_write_failed", key=key, error=repr(e)[:200])


async def cached_section(redis: Any, name: str, ttl_s: int, compute: Callable[[], Awaitable[dict]], *,
                         needs_cache: bool, key_prefix: str = KEY_PREFIX, single_flight: Optional[bool] = None,
                         lock_ttl_s: int = LOCK_TTL_S) -> dict:
    """Get-or-compute one section. Never raises: a failing ``compute`` becomes ``unavailable: error``."""
    key = section_key(name, key_prefix)
    flight = needs_cache if single_flight is None else single_flight

    async def run() -> dict:
        try:
            return await compute()
        except Exception as e:  # noqa: BLE001 — one section must never 500 the endpoint
            logger.warning("ops_overview_section_failed", section=name, error=repr(e)[:300])
            return unavailable("error", "This section could not be loaded. Try again in a minute.")

    if redis is None:
        if needs_cache:
            return {**unavailable("cache_offline", "Live numbers are paused while the server cache is offline."),
                    "_cached": False}
        return {**await run(), "_cached": False}

    try:
        hit = await redis.get(key)
    except Exception as e:
        logger.warning("ops_overview_cache_read_failed", key=key, error=repr(e)[:200])
        if needs_cache:
            return {**unavailable("cache_offline", "Live numbers are paused while the server cache is offline."),
                    "_cached": False}
        return {**await run(), "_cached": False}
    if hit:
        try:
            return {**_decode(hit), "_cached": True}
        except (ValueError, TypeError):
            logger.warning("ops_overview_cache_corrupt", key=key)

    lock_key = f"{key}:lock"
    if flight:
        try:
            got = await redis.set(lock_key, "1", nx=True, ex=lock_ttl_s)
        except Exception as e:
            logger.warning("ops_overview_lock_failed", key=key, error=repr(e)[:200])
            if not needs_cache:   # a DB-only single-flight section still answers without its lock
                return {**await run(), "_cached": False}
            return {**unavailable("cache_offline", "Live numbers are paused while the server cache is offline."),
                    "_cached": False}
        if not got:
            try:
                last = await redis.get(f"{key}:last")
            except Exception:
                last = None
            if last:
                try:
                    return {**_decode(last), "_cached": True, "_stale": True}
                except (ValueError, TypeError):
                    pass
            return {**unavailable("refreshing", "Fresh numbers are being fetched. Reload in a few seconds."),
                    "_cached": False}

    try:
        data = await run()
        ok = data.get("status") == "ok"
        await _set(redis, key, data, ttl_s if ok else ERROR_TTL_S)
        if ok and flight:
            await _set(redis, f"{key}:last", data, LAST_GOOD_TTL_S)
    finally:
        if flight:
            try:
                await redis.delete(lock_key)
            except Exception:
                pass
    return {**data, "_cached": False}
