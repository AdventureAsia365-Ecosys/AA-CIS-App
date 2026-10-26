"""AA-884 (S241 GĐ7) — what /health and /health/ready report, and the API's own build identity.

- ``git_sha()``: the commit the running image was built from (Dockerfile ARG GIT_SHA → ENV APP_GIT_SHA, set by
  deploy-dev.yml), the same value Sentry uses as its release. ``"unknown"`` outside an image.
- ``readiness(pool)``: one ``SELECT`` that both proves the DB answers and reads the highest **numeric** CIS migration
  in ``shared.schema_versions`` (the table also holds ``bk-*``, ``tp-*`` and ``rl-*`` keys, which are never compared).
  Ready = DB answered and that number >= ``REQUIRED_CIS_SCHEMA``, the newest migration this code needs. Raise the
  constant in the same PR as code that needs a newer migration, so the deploy smoke and the Platform Apps page show a
  missing apply instead of a 500 on first use.

``/health/ready`` is public (the ALB / CI probe shape), so the answer is cached in-process for
``READY_CACHE_SECONDS`` (failures too): a burst of probes reaches RDS at most once per window per task.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import structlog

from shared.sentry_init import release

logger = structlog.get_logger()

# The newest CIS migration this code needs: 287 (AA-919 WM-18 — the writers resolve their writing-memory release from
# shared.writing_memory_release / writing_memory_writer on every call (failing open without them) and
# /admin/writing-memory/releases reads and writes them). Before: 280 (AA-917 — the writing-memory jobs and
# /admin/writing-memory read and write shared.writing_rule; 277, AA-916: the admin A1 field edit and both sentence
# applies write shared.writing_signal inside their own transaction; without the table they fail).
REQUIRED_CIS_SCHEMA = 287
READY_CACHE_SECONDS = 5.0

SCHEMA_VERSION_SQL = """
    SELECT max(version::int) FROM shared.schema_versions WHERE version ~ '^[0-9]+$'
"""

_cache: dict[str, Any] = {"at": None, "value": None}


def git_sha() -> str:
    return release()


async def _probe(pool: Any) -> tuple[int, dict]:
    sha = git_sha()
    if pool is None:
        return 503, {"status": "unavailable", "db": "not_ready", "schema_version": None,
                     "required_schema": str(REQUIRED_CIS_SCHEMA), "git_sha": sha}
    try:
        version: Optional[int] = await pool.fetchval(SCHEMA_VERSION_SQL)
    except Exception as e:  # any DB failure is "not ready", never a 500
        logger.warning("health_ready_db_failed", error=repr(e)[:200])
        return 503, {"status": "unavailable", "db": "error", "schema_version": None,
                     "required_schema": str(REQUIRED_CIS_SCHEMA), "git_sha": sha}
    ok = version is not None and version >= REQUIRED_CIS_SCHEMA
    body = {"status": "ok" if ok else "schema_behind", "db": "ok",
            "schema_version": None if version is None else str(version),
            "required_schema": str(REQUIRED_CIS_SCHEMA), "git_sha": sha}
    return (200 if ok else 503), body


async def readiness(pool: Any, *, now: Optional[float] = None) -> tuple[int, dict]:
    """(HTTP status, body) for /health/ready; cached for READY_CACHE_SECONDS."""
    t = time.monotonic() if now is None else now
    at = _cache["at"]
    if at is not None and t - at < READY_CACHE_SECONDS and _cache["value"] is not None:
        return _cache["value"]
    value = await _probe(pool)
    _cache["at"], _cache["value"] = t, value
    return value


def reset_cache() -> None:
    _cache["at"], _cache["value"] = None, None
