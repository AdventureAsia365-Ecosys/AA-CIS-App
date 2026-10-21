"""AA-847 — one Geoapify client at a time: place_geocode and place_shape share the account's 5 req/s limit and its
daily credit quota, and the daily credit guard reads only FINISHED runs (geo.geocode_call rows are written at the end
of a run). Two runs at once would each see the full remaining credit and double the request rate, so the two kinds
are mutually exclusive through one session-level advisory lock held for the run (plan §4).

The lock is tried, never waited for: a kind that finds it taken re-enqueues itself (debounced) and ends.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

# pg advisory lock key (bigint) shared by place_geocode and place_shape; any fixed value no other code uses.
GEOAPIFY_LOCK_KEY = 847_712_001


@asynccontextmanager
async def geoapify_lock(pool):
    """Yields True when this run holds the lock (released on exit), False when another run holds it."""
    conn = await pool.acquire()
    try:
        held = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", GEOAPIFY_LOCK_KEY))
        try:
            yield held
        finally:
            if held:
                await conn.execute("SELECT pg_advisory_unlock($1)", GEOAPIFY_LOCK_KEY)
    finally:
        await pool.release(conn)
