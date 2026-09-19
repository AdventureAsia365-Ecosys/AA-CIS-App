"""AA-627 — persist shared.dfs_balance_snapshot for the DataForSEO account balance.

The "remaining balance" layer, distinct from call_log.py (AA-618 = cost already spent). The
daily-check job reads the balance via DataForSEOClient.fetch_balance() (FREE, cost:0) and calls
record_balance_snapshot() here to append one row; the External Spend page reads the latest via
read_latest_balance().

Unlike call_log.py this is NOT fire-and-forget: the whole point of AA-627 is to remove a blind
spot, so a write failure surfaces to the caller (the /admin/dfs-balance/check endpoint) rather
than being swallowed. Reads tolerate an empty table (no snapshot yet) by returning None.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg
import structlog

logger = structlog.get_logger()

_INSERT_SQL = """
    INSERT INTO shared.dfs_balance_snapshot
        (balance_usd, currency, below_threshold, threshold_usd, raw)
    VALUES ($1, $2, $3, $4, $5::jsonb)
    RETURNING id, balance_usd, currency, below_threshold, threshold_usd, fetched_at
"""

_LATEST_SQL = """
    SELECT id, balance_usd, currency, below_threshold, threshold_usd, raw, fetched_at
      FROM shared.dfs_balance_snapshot
     ORDER BY fetched_at DESC
     LIMIT 1
"""


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    if d.get("balance_usd") is not None:
        d["balance_usd"] = float(d["balance_usd"])
    if d.get("threshold_usd") is not None:
        d["threshold_usd"] = float(d["threshold_usd"])
    if d.get("id") is not None:
        d["id"] = str(d["id"])
    if d.get("fetched_at") is not None:
        d["fetched_at"] = d["fetched_at"].isoformat()
    raw = d.get("raw")
    if isinstance(raw, str):
        try:
            d["raw"] = json.loads(raw)
        except (TypeError, ValueError):
            d["raw"] = None
    return d


async def record_balance_snapshot(
    pool: asyncpg.Pool,
    *,
    balance_usd: float,
    currency: Optional[str] = None,
    below_threshold: bool = False,
    threshold_usd: Optional[float] = None,
    raw: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Append one balance snapshot and return the written row. Raises on DB error (caller-owned
    error handling — the daily check treats a failed write as a failed check, not a silent pass)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_SQL, balance_usd, currency, below_threshold, threshold_usd,
            json.dumps(raw) if raw is not None else None,
        )
    result = _row_to_dict(row)
    logger.info(
        "dfs_balance_snapshot_written",
        balance=balance_usd, below_threshold=below_threshold, threshold=threshold_usd,
    )
    return result


async def read_latest_balance(pool: asyncpg.Pool) -> Optional[dict[str, Any]]:
    """Return the most recent balance snapshot as a dict, or None if none recorded yet."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_LATEST_SQL)
    return _row_to_dict(row) if row else None
