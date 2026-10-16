"""
AA-859 PR-S -- main-pool gauge for the api task.

Every MAIN_POOL_GAUGE_INTERVAL_S the api logs one INFO line `aa859_main_pool_gauge` with the size, idle
count and max of app.state.pool (aa_cis_admin, `cis-api`). Main-pool acquires have no timeout, so
starvation would only show as requests hanging to the API Gateway 30 s limit; this line is the baseline
wave 4 uses to decide whether the main pool can shrink (plan D5 / §2). Started from api/main.py's
lifespan only (the worker service has its own pools and does not run it).

Never raises: a failing read is logged (`aa859_main_pool_gauge_failed`) and the loop goes on. Cancelled
on shutdown before the pool closes.
"""
from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger()

MAIN_POOL_GAUGE_INTERVAL_S = 60.0


def log_main_pool_gauge(pool) -> None:
    """One gauge line for an asyncpg Pool (get_size / get_idle_size / get_max_size)."""
    size = pool.get_size()
    idle = pool.get_idle_size()
    logger.info("aa859_main_pool_gauge", size=size, idle=idle, in_use=size - idle, max=pool.get_max_size())


async def run_main_pool_gauge(app, interval_s: float = MAIN_POOL_GAUGE_INTERVAL_S) -> None:
    """Loops until cancelled. Reads app.state.pool on every tick, so it never holds a stale pool."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            pool = getattr(app.state, "pool", None)
            if pool is not None:
                log_main_pool_gauge(pool)
        except Exception as e:  # never kill the loop (CancelledError is not an Exception)
            logger.warning("aa859_main_pool_gauge_failed", error=repr(e))


def start_main_pool_gauge(app, interval_s: float = MAIN_POOL_GAUGE_INTERVAL_S) -> asyncio.Task:
    return asyncio.create_task(run_main_pool_gauge(app, interval_s), name="aa859-main-pool-gauge")


async def stop_main_pool_gauge(task: asyncio.Task | None) -> None:
    """Cancel and wait; swallows the CancelledError (and anything else) so shutdown goes on."""
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
