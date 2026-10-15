"""AA-819 (AUP 2026-11) — per-tenant daily publish limit for T11.

Anthropic's usage policy (12/11/2026) forbids spam and coordinated inauthentic activity; a tenant
publishing AI copy without a volume ceiling is exactly that risk. The limit:

- **Default 20 publishes per UTC day** (`DEFAULT_DAILY_PUBLISH_LIMIT`), in code, so the guard is live
  the moment this code deploys — before migration 230 is applied (D3, S237).
- **Override**: `shared.tenants.daily_publish_limit` (migration 230, nullable; NULL = the default),
  set by an admin through `PATCH /admin/tenants/{id}` (audited `tenant.daily_publish_limit_changed`).
  The code tolerates the column being absent: it probes `pg_attribute` once and caches the answer
  (a "missing" answer is re-probed every `COLUMN_RECHECK_SECONDS`, so the override starts working
  after the apply without a redeploy); an `UndefinedColumnError` resets the cache.
- **What counts** (`COUNT_TODAY_SQL`): the tenant's `acp_shared.publish_log` rows whose attempt
  started today (UTC, `COALESCE(requested_at, published_at, created_at)`) with status `requested` or
  `published` — plus `unpublished` rows that did publish (`published_at` set): unpublish only flips
  our row, so it must not hand the slot back (publish → unpublish → publish would bypass the limit).
  `failed` rows never count.
- **What never counts**: an AA-726 replay (same revision already published — no new row), a 409
  in-flight retry, a stale `requested` row reclaimed by its own retry (the row is already counted).
  `idempotency.claim()` checks the limit only on the two paths that make a NEW counted row (a fresh
  INSERT, or a failed row turned back into `requested`), under a per-tenant advisory lock so two
  concurrent publishes cannot both take the last slot.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import structlog

logger = structlog.get_logger()

DEFAULT_DAILY_PUBLISH_LIMIT = 20
# Upper bound for an admin override — a guard against typos (50x the default), not a commercial limit.
DAILY_PUBLISH_LIMIT_MAX = 1000
COLUMN_RECHECK_SECONDS = 300

COUNT_TODAY_SQL = """
    SELECT count(*)
    FROM acp_shared.publish_log
    WHERE tenant_id = $1::uuid
      AND (status IN ('requested', 'published') OR (status = 'unpublished' AND published_at IS NOT NULL))
      AND COALESCE(requested_at, published_at, created_at)
          >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
"""

_COLUMN_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('shared.tenants') AND attname = 'daily_publish_limit'
          AND NOT attisdropped
    )
"""

_column = {"exists": None, "checked_at": 0.0}


def _forget_column() -> None:
    _column.update(exists=None, checked_at=0.0)


async def override_column_exists(conn) -> bool:
    """Is migration 230 applied? True is cached for the process; False is re-checked every
    COLUMN_RECHECK_SECONDS."""
    if _column["exists"]:
        return True
    now = time.monotonic()
    if _column["exists"] is False and now - _column["checked_at"] < COLUMN_RECHECK_SECONDS:
        return False
    exists = bool(await conn.fetchval(_COLUMN_SQL))
    _column.update(exists=exists, checked_at=now)
    return exists


async def tenant_override(conn, tenant_id) -> Optional[int]:
    """The tenant's own limit, or None (no override, or migration 230 not applied yet). Run it OUTSIDE
    a transaction that must survive: an UndefinedColumnError aborts the transaction it runs in."""
    if not await override_column_exists(conn):
        return None
    try:
        return await conn.fetchval("SELECT daily_publish_limit FROM shared.tenants WHERE tenant_id = $1::uuid",
                                   str(tenant_id))
    except asyncpg.UndefinedColumnError:  # rolled back after the probe — fall back to the default
        _column.update(exists=False, checked_at=time.monotonic())
        logger.warning("daily_publish_limit_column_missing", tenant_id=str(tenant_id))
        return None


async def effective_limit(conn, tenant_id) -> int:
    override = await tenant_override(conn, tenant_id)
    return override if override is not None else DEFAULT_DAILY_PUBLISH_LIMIT


async def count_today(conn, tenant_id) -> int:
    return int(await conn.fetchval(COUNT_TODAY_SQL, str(tenant_id)) or 0)


async def lock_tenant(conn, tenant_id) -> None:
    """Per-tenant transaction-scoped advisory lock: serialises the count-then-insert of two concurrent
    publishes by one tenant (different pieces), so the last slot is taken once."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 819))", f"publish_limit:{tenant_id}")


def seconds_until_reset(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def limit_message(used: int, limit: int, now: Optional[datetime] = None) -> str:
    hours, rest = divmod(seconds_until_reset(now), 3600)
    wait = f"{hours} h {rest // 60} min" if hours else f"{max(1, rest // 60)} min"
    return (f"Daily publish limit reached: this account can publish {limit} post(s) per day and has "
            f"published {used} today. The limit resets at 00:00 UTC (in {wait}). "
            "Contact Adventure Asia if you need a higher limit.")


__all__ = ["COUNT_TODAY_SQL", "COLUMN_RECHECK_SECONDS", "DAILY_PUBLISH_LIMIT_MAX", "DEFAULT_DAILY_PUBLISH_LIMIT",
           "count_today", "effective_limit", "limit_message", "lock_tenant", "override_column_exists",
           "seconds_until_reset", "tenant_override"]
