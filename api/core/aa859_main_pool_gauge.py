"""
AA-859 PR-S -- main-pool gauge for the api task, plus (wave 4, S247) the main-pool acquire-wait signal.

Gauge. Every MAIN_POOL_GAUGE_INTERVAL_S the api logs one INFO line `aa859_main_pool_gauge` with the size, idle
count and max of app.state.pool (cis_app since X1 S18, application_name `cis-api`). Since S247 the same line
also carries what happened BETWEEN two ticks: `acquires`, `peak_in_use` (highest in-use count right after an
acquire), `waited` (acquires that waited >= MAIN_POOL_WAIT_LOG_MS), `max_wait_ms` and `timeouts`. A 60 s
point sample alone misses a burst; the peak does not. These counters are what the main-pool shrink is sized
from (plan D5 / §2: peak in-use under a burst x 1.5). Started from api/main.py's lifespan only (the worker
service has its own pools and does not run it).

Acquire-wait signal. instrument_main_pool() gives the api's main pool a subclass whose `_acquire` times every
acquire, whether it comes from `pool.acquire()` or from `pool.fetch*/execute` (asyncpg routes both through
`Pool._acquire`). Low noise, same shape as the tenant pool's `aa859_tenant_pool_timing`: one INFO line
`aa859_main_pool_wait` (wait_ms, in_use, size, max) only for an acquire that waited >= MAIN_POOL_WAIT_LOG_MS,
at most MAIN_POOL_WAIT_LOG_CAP lines per gauge interval (the rest are still counted in `waited`). The wait
includes opening a new connection when the pool grows (TLS connect to RDS), so the first burst after a quiet
spell can log a few lines on growth alone.

Acquire timeout. Main-pool acquires used to have no timeout, so starvation (or two nested acquires per request
on a small pool) only showed as requests hanging to the API Gateway 30 s limit. An acquire with no timeout of
its own now gets MAIN_POOL_ACQUIRE_TIMEOUT_S; when it runs out the api logs ERROR
`aa859_main_pool_acquire_timeout` (wait_ms, size, idle, max) and asyncpg's TimeoutError goes up as before
(a 500 with a request id on a route). An explicit `acquire(timeout=...)` keeps the caller's value.

Never raises from the gauge: a failing read is logged (`aa859_main_pool_gauge_failed`) and the loop goes on.
Cancelled on shutdown before the pool closes.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import asyncpg
import structlog

logger = structlog.get_logger()

MAIN_POOL_GAUGE_INTERVAL_S = 60.0
MAIN_POOL_WAIT_LOG_MS = 50.0          # log one aa859_main_pool_wait line per acquire that waited this long
MAIN_POOL_WAIT_LOG_CAP = 200          # at most this many wait lines per gauge interval
MAIN_POOL_ACQUIRE_TIMEOUT_S = 5.0     # data standards C2.3g: fail fast on exhaustion, <= 5 s


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


class _MainPoolStats:
    """Counters between two gauge ticks. One main pool per api process, so one module-level instance."""

    __slots__ = ("acquires", "waited", "max_wait_ms", "peak_in_use", "timeouts", "wait_lines")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.acquires = 0
        self.waited = 0
        self.max_wait_ms = 0.0
        self.peak_in_use = 0
        self.timeouts = 0
        self.wait_lines = 0

    def take(self) -> dict:
        """The counters for the gauge line, then reset for the next interval."""
        out = {"acquires": self.acquires, "peak_in_use": self.peak_in_use, "waited": self.waited,
               "max_wait_ms": self.max_wait_ms, "timeouts": self.timeouts}
        self.reset()
        return out


_STATS = _MainPoolStats()


def _record_acquire(pool: asyncpg.Pool, wait_s: float) -> None:
    """Never raises: a failing count must not fail the acquire that already succeeded."""
    try:
        wait_ms = _ms(wait_s)
        size = pool.get_size()
        in_use = size - pool.get_idle_size()
        _STATS.acquires += 1
        if in_use > _STATS.peak_in_use:
            _STATS.peak_in_use = in_use
        if wait_ms > _STATS.max_wait_ms:
            _STATS.max_wait_ms = wait_ms
        if wait_ms >= MAIN_POOL_WAIT_LOG_MS:
            _STATS.waited += 1
            if _STATS.wait_lines < MAIN_POOL_WAIT_LOG_CAP:
                _STATS.wait_lines += 1
                logger.info("aa859_main_pool_wait", wait_ms=wait_ms, in_use=in_use, size=size,
                            max=pool.get_max_size())
    except Exception:  # pragma: no cover - defensive
        pass


class _InstrumentedMainPool(asyncpg.Pool):
    """asyncpg.Pool with a timed `_acquire`. No new slots, so an already created Pool can take this class
    (instrument_main_pool) and every caller keeps the same object. asyncpg 0.29: `Pool.acquire()` and the
    `Pool.fetch*/execute*` shortcuts all go through `_acquire(timeout)` (tests/unit/test_aa859_main_pool_gauge.py
    pins that, so an asyncpg upgrade that changes it fails CI instead of silently dropping the signal)."""

    __slots__ = ()

    async def _acquire(self, timeout):
        effective = MAIN_POOL_ACQUIRE_TIMEOUT_S if timeout is None else timeout
        started = time.monotonic()
        try:
            conn = await super()._acquire(effective)
        except asyncio.TimeoutError:
            _STATS.timeouts += 1
            logger.error("aa859_main_pool_acquire_timeout", wait_ms=_ms(time.monotonic() - started),
                         timeout_s=effective, size=self.get_size(), idle=self.get_idle_size(),
                         max=self.get_max_size())
            raise
        _record_acquire(self, time.monotonic() - started)
        return conn


def instrument_main_pool(pool: Any) -> Any:
    """Switch a plain asyncpg.Pool to the timed subclass in place and return it. Anything else (a test
    double) is returned unchanged."""
    if type(pool) is asyncpg.Pool:
        pool.__class__ = _InstrumentedMainPool
        logger.info("aa859_main_pool_instrumented", max=pool.get_max_size(), wait_log_ms=MAIN_POOL_WAIT_LOG_MS,
                    acquire_timeout_s=MAIN_POOL_ACQUIRE_TIMEOUT_S)
    return pool


def log_main_pool_gauge(pool) -> None:
    """One gauge line for an asyncpg Pool (get_size / get_idle_size / get_max_size) plus the counters of
    the interval since the previous line."""
    size = pool.get_size()
    idle = pool.get_idle_size()
    logger.info("aa859_main_pool_gauge", size=size, idle=idle, in_use=size - idle, max=pool.get_max_size(),
                **_STATS.take())


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
