"""AA-650 — the worker: claims jobs from shared.job and runs their handlers.

Runs inside the API process today (started from the FastAPI lifespan, `JOB_WORKER_IN_API`), or
standalone with `python -m shared.jobs.worker` (its own pool, SIGTERM handling) once it gets its
own ECS service (AA-651).

Lifecycle of one job:
  claim (lease) -> heartbeat every `heartbeat_seconds` -> handler returns   -> succeeded
                                                         raises BudgetExceeded -> stopped_budget
                                                         raises                -> retry w/ backoff or failed
  admin cancel  -> the heartbeat sees cancel_requested -> handler task cancelled -> cancelled
  shutdown      -> running handlers get `grace_seconds`, then are released back to the queue
  worker dies   -> lease expires -> reaper (any worker) re-queues or fails the job
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import uuid
from typing import Optional

import structlog

from shared.cost_guard import BudgetExceeded
from shared.llm_client import call_log

from . import queue
from .registry import JobContext, NonRetryable, kinds, run_terminal_hook

logger = structlog.get_logger()


class Worker:
    def __init__(self, pool, *, worker_id: Optional[str] = None, poll_seconds: float = 5.0,
                 lease_seconds: int = 90, heartbeat_seconds: float = 20.0,
                 reap_every_seconds: float = 30.0, grace_seconds: float = 20.0,
                 max_parallel: int = 4, resources: Optional[dict] = None):
        self.pool = pool
        self.resources = resources or {}
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.reap_every_seconds = reap_every_seconds
        self.grace_seconds = grace_seconds
        self.max_parallel = max_parallel
        self._stopping = asyncio.Event()
        self._running: dict[str, asyncio.Task] = {}
        # AA-687: how often this worker writes its shared.job_worker row (liveness for the Jobs page).
        self.beat_every_seconds = 15.0

    def caps(self) -> dict[str, int]:
        return {name: k.concurrency for name, k in kinds().items()}

    # ── main loop ─────────────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        logger.info("job_worker_started", worker_id=self.worker_id, kinds=sorted(self.caps()))
        await self._liveness(queue.worker_register, self.pool, self.worker_id, socket.gethostname(),
                             self.max_parallel, self.caps())
        last_reap = last_beat = 0.0
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            reaped = None
            try:
                if loop.time() - last_reap >= self.reap_every_seconds:
                    reaped = await queue.reap(self.pool)
                    if reaped["requeued"] or reaped["failed"]:
                        logger.warning("job_reaped", **reaped)
                    for job_id in reaped["failed"]:
                        await run_terminal_hook(self.pool, job_id)
                    last_reap = loop.time()
                claimed = await self._fill_slots()
            except Exception as e:  # never let one bad cycle kill the loop
                logger.warning("job_worker_cycle_failed", error=str(e)[:300])
                claimed = 0
            if loop.time() - last_beat >= self.beat_every_seconds or (
                    reaped and (reaped["requeued"] or reaped["failed"])):
                await self._liveness(queue.worker_beat, self.pool, self.worker_id,
                                     len(self._running), reaped)
                last_beat = loop.time()
            if not claimed:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        logger.info("job_worker_loop_stopped", worker_id=self.worker_id)

    async def _liveness(self, fn, *args) -> None:
        """AA-687: shared.job_worker writes are for the Jobs page only — never fail the loop."""
        try:
            await fn(*args)
        except Exception as e:
            logger.warning("job_worker_liveness_write_failed", step=fn.__name__, error=str(e)[:200])

    async def _fill_slots(self) -> int:
        claimed = 0
        while len(self._running) < self.max_parallel and not self._stopping.is_set():
            job = await queue.claim(self.pool, self.worker_id, self.caps(), self.lease_seconds)
            if job is None:
                break
            task = asyncio.create_task(self._execute(job), name=f"job:{job.kind}:{job.id}")
            self._running[job.id] = task
            task.add_done_callback(lambda _t, jid=job.id: self._running.pop(jid, None))
            claimed += 1
        return claimed

    # ── one job ───────────────────────────────────────────────────────────────────────────────
    async def _execute(self, job: queue.Job) -> None:
        kind = kinds().get(job.kind)
        ctx = JobContext(self.pool, job, self.resources)
        log = logger.bind(job_id=job.id, kind=job.kind, attempt=job.attempt)
        if kind is None:  # claimed only kinds in caps(), so this means a deploy dropped the kind
            await queue.fail(self.pool, job, self.worker_id, f"no handler for kind {job.kind!r}",
                             retryable=False, cost_usd=None)
            await run_terminal_hook(self.pool, job.id)
            return
        log.info("job_started")
        # The task copies the context at creation, so every LLM call the handler makes (threads
        # included) logs this job's id — its cost shows on the Jobs page (call_log.bind_job).
        with call_log.bind_job(job.id):
            handler_task = asyncio.create_task(kind.handler(ctx))
        cancelled_by_admin = False
        try:
            while True:
                done, _ = await asyncio.wait({handler_task}, timeout=self.heartbeat_seconds)
                if done:
                    break
                owned, cancel = await queue.heartbeat(self.pool, job.id, self.worker_id,
                                                      self.lease_seconds)
                if not owned:  # the reaper took it (lease lost) — stop, someone else owns it now
                    log.warning("job_lease_lost")
                    handler_task.cancel()
                    await asyncio.gather(handler_task, return_exceptions=True)
                    return
                if cancel:
                    cancelled_by_admin = True
                    handler_task.cancel()
                    await asyncio.gather(handler_task, return_exceptions=True)
                    await queue.mark_cancelled(self.pool, job.id, self.worker_id, ctx.cost_usd)
                    await run_terminal_hook(self.pool, job.id)
                    log.info("job_cancelled")
                    return
            result = handler_task.result()
            await queue.complete(self.pool, job.id, self.worker_id, result or ctx.result,
                                 round(ctx.cost_usd, 6))
            # AA-687: ctx.cost_usd is only what the handler reported (non-LLM, e.g. DataForSEO);
            # the LLM part is summed from llm_call_log, the same as the Jobs page shows.
            llm_cost = await self._llm_cost(job.id)
            log.info("job_succeeded", handler_cost_usd=round(ctx.cost_usd, 6), llm_cost_usd=llm_cost,
                     cost_usd=round(ctx.cost_usd + (llm_cost or 0.0), 6))
        except asyncio.CancelledError:
            # Shutdown: this wrapper was cancelled after the grace period.
            if not handler_task.done():
                handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)
            if not cancelled_by_admin:
                status = await queue.release(self.pool, job.id, self.worker_id, ctx.cost_usd)
                log.warning("job_released_on_shutdown", next_status=status)
                if status in ("failed", "cancelled"):
                    await run_terminal_hook(self.pool, job.id)
            raise
        except BudgetExceeded as e:
            await queue.stop_budget(self.pool, job.id, self.worker_id, str(e), ctx.result,
                                    round(ctx.cost_usd, 6))
            await run_terminal_hook(self.pool, job.id)
            log.warning("job_stopped_budget", reason=str(e))
        except Exception as e:
            status = await queue.fail(self.pool, job, self.worker_id,
                                      f"{type(e).__name__}: {str(e)[:900]}",
                                      retryable=not isinstance(e, NonRetryable),
                                      cost_usd=round(ctx.cost_usd, 6), result=ctx.result)
            log.error("job_failed", next_status=status, error=str(e)[:300], exc_info=True)
            if status == "failed":
                await run_terminal_hook(self.pool, job.id)

    async def _llm_cost(self, job_id: str) -> Optional[float]:
        try:
            v = await self.pool.fetchval(
                "SELECT coalesce(sum(cost_usd), 0)::float FROM shared.llm_call_log WHERE job_id = $1::uuid",
                job_id)
            return round(float(v or 0.0), 6)
        except Exception:
            return None

    # ── shutdown ──────────────────────────────────────────────────────────────────────────────
    async def shutdown(self) -> None:
        """Stop claiming, give running jobs `grace_seconds` to finish, release the rest."""
        self._stopping.set()
        tasks = list(self._running.values())
        if not tasks:
            await self._liveness(queue.worker_stopped, self.pool, self.worker_id)
            return
        logger.info("job_worker_draining", running=len(tasks), grace_seconds=self.grace_seconds)
        _, pending = await asyncio.wait(tasks, timeout=self.grace_seconds)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._liveness(queue.worker_stopped, self.pool, self.worker_id)


def in_api_enabled() -> bool:
    return os.environ.get("JOB_WORKER_IN_API", "true").lower() not in ("0", "false", "no")


def load_kinds() -> None:
    """Importing the package registers every job kind."""
    import services.jobs  # noqa: F401


async def _main() -> None:
    import asyncpg

    from shared.secrets import get_database_url

    load_kinds()
    pool = await asyncpg.create_pool(get_database_url(), min_size=1, max_size=5)
    redis = None
    if os.environ.get("REDIS_HOST"):
        import redis.asyncio as aioredis
        redis = aioredis.from_url(f"redis://{os.environ['REDIS_HOST']}:6379", encoding="utf-8",
                                  decode_responses=True)
    worker = Worker(pool, resources={"redis": redis})
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker._stopping.set)
    runner = asyncio.create_task(worker.run())
    await worker._stopping.wait()
    await worker.shutdown()
    await runner
    await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
