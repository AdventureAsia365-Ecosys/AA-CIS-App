"""AA-650 — the worker: claims jobs from shared.job and runs their handlers.

Runs as its own ECS service via `python -m worker` (its own pool, SIGTERM handling, AA-651). It can
also run inside the API process (started from the FastAPI lifespan) for local/dev, but only when
`JOB_WORKER_IN_API` is explicitly set truthy — the default is off (AA-735, ADR 0003 nac 5), so no
default/stray API deploy runs a second worker against shared.job.

Lifecycle of one job:
  claim (lease) -> heartbeat every `heartbeat_seconds` -> handler returns   -> succeeded
                                                         raises BudgetExceeded -> stopped_budget
                                                         raises                -> retry w/ backoff or failed
  admin cancel  -> the heartbeat sees cancel_requested -> handler task cancelled -> cancelled
  shutdown      -> running handlers get `grace_seconds`, then are released back to the queue
                   (AA-993 G1: `JOB_WORKER_GRACE_SECONDS`; a release during a deploy does not count toward
                   queue.MAX_RELEASES; the kind's `release_delay_seconds` delays the re-claim)
  worker dies   -> lease expires -> reaper (any worker) re-queues or fails the job
"""
from __future__ import annotations

import asyncio
import math
import os
import signal
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import sentry_sdk
import structlog

from shared.cost_guard import BudgetExceeded
from shared.llm_client import call_log
from shared.llm_client.errors import LLM_ACCESS_DENIED_REASON, LLMAccessDenied, is_llm_access_denied
from shared.llm_client.tenant_budget import approval_stop_in
from shared.sentry_init import release  # AA-884: APP_GIT_SHA of this image, written to shared.job_worker

from . import queue
from .registry import JobContext, NonRetryable, kinds, run_terminal_hook

logger = structlog.get_logger()


def _degraded_collect():
    """AA-925 — the per-job degraded collector (services/quality/degraded.collect); a null context when the
    module cannot load, so the worker never depends on it."""
    try:
        from services.quality import degraded
        return degraded.collect()
    except Exception:  # pragma: no cover
        from contextlib import nullcontext
        return nullcontext({})


def _degraded_result(result, box):
    """The job result with `degraded` counts added (dict / None results; anything else unchanged)."""
    try:
        from services.quality import degraded
        return degraded.merge_job_result(result, box)
    except Exception:  # pragma: no cover
        return result


def ecs_task_revision() -> Optional[int]:
    """AA-711 — this task's ECS task-definition revision, from the task metadata endpoint v4
    (`JOB_WORKER_TASK_REVISION` overrides it, for tests/local). None outside ECS."""
    forced = os.environ.get("JOB_WORKER_TASK_REVISION")
    if forced:
        return int(forced)
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not uri:
        return None
    try:
        import json
        import urllib.request
        with urllib.request.urlopen(f"{uri}/task", timeout=2) as r:
            return int(json.load(r).get("Revision"))
    except Exception as e:
        logger.warning("job_worker_revision_unknown", error=str(e)[:200])
        return None


PLATFORM_TENANT_SLUG = "aa_internal"   # the slug TourRunRequest.tenant_id accepts for the platform tenant


def job_tenant_id(payload: Optional[dict]) -> Optional[str]:
    """AA-741 G1 — the tenant a job works for, for call_log.bind_tenant. `shared.job` has no
    tenant column: T2/T9/S1 payloads carry `tenant_id` (T9 also nests it in `context`).
    The platform tenant (aa_internal) is not a customer — its LLM rows keep tenant_id NULL.

    AA-993 follow-up: the platform tenant also arrives as its slug — `POST /admin/run-tour-async` has always accepted
    `"tenant_id": "aa_internal"` (the executor maps it). Bound as-is, every llm_call_log write of the job sent
    `'aa_internal'` as `$1::uuid` and was dropped, and decide() refused Jev for "a tenant not on the allow-list". A
    tenant is a UUID: the slug and any other non-UUID value bind no tenant (platform)."""
    if not isinstance(payload, dict):
        return None
    ctx = payload.get("context")
    tid = payload.get("tenant_id") or (ctx.get("tenant_id") if isinstance(ctx, dict) else None)
    if tid is None:
        return None
    raw = str(tid).strip()
    if raw in ("", "None") or raw.lower() == PLATFORM_TENANT_SLUG:
        return None
    try:
        tid = str(uuid.UUID(raw))
    except ValueError:
        logger.warning("job_tenant_not_uuid", tenant_id=raw[:64])
        return None
    return None if tid == call_log.PLATFORM_TENANT_ID else tid


def failure_outcome(exc: BaseException) -> tuple[str, bool, Optional[str]]:
    """How a handler exception ends its attempt: (shared.job.error text, may the runner retry, reason).

    AA-900: when the LLM gateway decided that every model was refused at account level (a typed LLMAccessDenied
    anywhere in the cause chain: Bedrock "Error 002", AccessDenied, no OpenAI credit), a retry minutes later is
    refused the same way, so the job fails at once and its error starts with ``llm_access_denied`` (the admin LLM
    availability card and the Jobs page read that prefix). Any other exception keeps the old rule: retry unless
    NonRetryable."""
    if is_llm_access_denied(exc):
        body = str(exc) if isinstance(exc, LLMAccessDenied) else f"{type(exc).__name__}: {exc}"
        if not body.startswith(LLM_ACCESS_DENIED_REASON):
            body = f"{LLM_ACCESS_DENIED_REASON}: {body}"
        return body[:900 + len(LLM_ACCESS_DENIED_REASON) + 2], False, LLM_ACCESS_DENIED_REASON
    return f"{type(exc).__name__}: {str(exc)[:900]}", not isinstance(exc, NonRetryable), None


OPS_FLUSH_EVERY_S = 300.0     # S263: same cadence as the api ops sampler
OPS_FLUSH_TIMEOUT_S = 20.0


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
        self._running_kinds: dict[str, str] = {}   # job id -> kind (background group cap, AA-712)
        # AA-687: how often this worker writes its shared.job_worker row (liveness for the Jobs page).
        self.beat_every_seconds = 15.0
        # S263 (AA-1025): how often this worker turns the ops log events it counted (lost cost-log writes, cis_app
        # refused connections; shared/ops_checks.py) into the admin bell + alert e-mail. The api does it on its tick.
        self.ops_flush_every_seconds = OPS_FLUSH_EVERY_S
        # AA-711: a worker on a draining (older) task stops claiming once a newer one is live.
        self.task_revision = ecs_task_revision()
        self.yielding = False

    def caps(self) -> dict[str, int]:
        return {name: k.concurrency for name, k in kinds().items()}

    def background_reserve(self) -> int:
        return background_reserve_for(self.max_parallel)

    def running_background(self) -> int:
        background = background_kinds()
        return sum(1 for kind in self._running_kinds.values() if kind in background)

    def claim_caps(self, free_slots: int, running_background: int = 0) -> dict[str, int]:
        """The caps passed to claim() — see claim_caps_for (AA-751: the ops metrics sampler applies
        the same rule to each live worker's registered caps)."""
        return claim_caps_for(self.max_parallel, self.caps(), free_slots, running_background,
                              background_kinds())

    # ── main loop ─────────────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        logger.info("job_worker_started", worker_id=self.worker_id, kinds=sorted(self.caps()))
        await self._liveness(queue.worker_register, self.pool, self.worker_id, socket.gethostname(),
                             self.max_parallel, self.caps(), self.task_revision, release())
        last_reap = last_beat = 0.0
        loop = asyncio.get_running_loop()
        last_ops_flush = loop.time()
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
            if loop.time() - last_ops_flush >= self.ops_flush_every_seconds:
                await self._flush_ops_events()
                last_ops_flush = loop.time()
            if not claimed:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        logger.info("job_worker_loop_stopped", worker_id=self.worker_id)

    async def _flush_ops_events(self) -> None:
        """S263: alert on the ops log events this process counted. Bounded and never fails the loop."""
        from shared import ops_checks

        try:
            results = await asyncio.wait_for(ops_checks.flush_events(self.pool), timeout=OPS_FLUSH_TIMEOUT_S)
            if results:
                logger.warning("job_worker_ops_checks", worker_id=self.worker_id, ops_checks=results)
        except Exception as e:  # noqa: BLE001 — the events stay counted until a flush succeeds
            logger.warning("job_worker_ops_checks_failed", error=f"{type(e).__name__}: {str(e)[:200]}")

    async def _liveness(self, fn, *args) -> None:
        """AA-687: shared.job_worker writes are for the Jobs page only — never fail the loop."""
        try:
            await fn(*args)
        except Exception as e:
            logger.warning("job_worker_liveness_write_failed", step=fn.__name__, error=str(e)[:200])

    async def _fill_slots(self) -> int:
        claimed = 0
        if await queue.newer_worker_alive(self.pool, self.task_revision):
            if not self.yielding:
                logger.info("job_worker_yielding", worker_id=self.worker_id, task_revision=self.task_revision)
                self.yielding = True
            return 0
        self.yielding = False
        while len(self._running) < self.max_parallel and not self._stopping.is_set():
            caps = self.claim_caps(self.max_parallel - len(self._running), self.running_background())
            job = await queue.claim(self.pool, self.worker_id, caps, self.lease_seconds)
            if job is None:
                break
            task = asyncio.create_task(self._execute(job), name=f"job:{job.kind}:{job.id}")
            self._running[job.id] = task
            self._running_kinds[job.id] = job.kind
            task.add_done_callback(lambda _t, jid=job.id: self._forget(jid))
            claimed += 1
        return claimed

    def _forget(self, job_id: str) -> None:
        self._running.pop(job_id, None)
        self._running_kinds.pop(job_id, None)

    # ── one job ───────────────────────────────────────────────────────────────────────────────
    async def _execute(self, job: queue.Job) -> None:
        # AA-871 — every log line written while this job runs (handler included: its task copies this context)
        # carries job_id / job_kind. `_execute` runs in its own task (`_fill_slots`), so the binding never reaches
        # another job; it is cleared when the job ends.
        # Its own Sentry isolation scope too: tasks copy the context but share the parent's scope object, so without
        # the fork the breadcrumbs and tags of concurrent jobs would mix in each other's events.
        with structlog.contextvars.bound_contextvars(job_id=job.id, job_kind=job.kind), \
                sentry_sdk.isolation_scope() as scope:
            scope.set_tag("job_id", job.id)
            scope.set_tag("job_kind", job.kind)
            await self._execute_job(job)

    async def _execute_job(self, job: queue.Job) -> None:
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
        # included) logs this job's id — its cost shows on the Jobs page (call_log.bind_job) —
        # and, for a tenant job, the tenant (AA-741 G1, call_log.bind_tenant).
        # AA-925 — and every Jev outage (zone=error) the handler meets is counted into this job's collector
        # (services/quality/degraded.py), written to shared.job.result.degraded below.
        with call_log.bind_job(job.id), call_log.bind_tenant(job_tenant_id(job.payload)), \
                _degraded_collect() as degraded_box:
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
            result = _degraded_result(handler_task.result() or ctx.result, degraded_box)
            await queue.complete(self.pool, job.id, self.worker_id, result,
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
                # AA-993 G1: a deploy (a newer task revision is live) does not burn one of the job's releases.
                newer = await self._newer_worker_alive_safe()
                delay = kind.release_delay_seconds
                status = await queue.release(self.pool, job.id, self.worker_id, ctx.cost_usd,
                                             count_release=not newer, delay_seconds=delay)
                log.warning("job_released_on_shutdown", next_status=status, counted=not newer,
                            release_delay_seconds=delay, task_revision=self.task_revision)
                if status in ("failed", "cancelled"):
                    await run_terminal_hook(self.pool, job.id)
            raise
        except BudgetExceeded as e:
            await queue.stop_budget(self.pool, job.id, self.worker_id, str(e), ctx.result,
                                    round(ctx.cost_usd, 6))
            await run_terminal_hook(self.pool, job.id)
            log.warning("job_stopped_budget", reason=str(e))
        except Exception as e:
            # AA-1016 (review S262): a tenant approval stop raised `from` inside a pipeline (or wrapped by LangGraph)
            # still ends the job stopped_budget, so the approval re-queue finds it.
            stop = approval_stop_in(e)
            if stop is not None:
                await queue.stop_budget(self.pool, job.id, self.worker_id, str(stop), ctx.result,
                                        round(ctx.cost_usd, 6))
                await run_terminal_hook(self.pool, job.id)
                log.warning("job_stopped_budget", reason=str(stop))
                return
            error, retryable, reason = failure_outcome(e)
            status = await queue.fail(self.pool, job, self.worker_id, error, retryable=retryable,
                                      cost_usd=round(ctx.cost_usd, 6), result=ctx.result)
            # .exception: the traceback reaches Sentry with the event (event name unchanged: metric filters).
            log.exception("job_failed", next_status=status, error=str(e)[:300], reason=reason)
            if status == "failed":
                await run_terminal_hook(self.pool, job.id)

    async def _newer_worker_alive_safe(self) -> bool:
        """AA-993 G1 — is a worker of a newer task revision alive (this task is being replaced by a deploy)?
        False when unknown (no revision, or the read failed): the release then counts, as before G1."""
        try:
            return await queue.newer_worker_alive(self.pool, self.task_revision)
        except Exception as e:
            logger.warning("job_worker_newer_check_failed", error=str(e)[:200])
            return False

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
        logger.info("job_worker_draining", worker_id=self.worker_id, running=len(tasks),
                    kinds=sorted(set(self._running_kinds.values())), grace_seconds=self.grace_seconds,
                    task_revision=self.task_revision)
        _, pending = await asyncio.wait(tasks, timeout=self.grace_seconds)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("job_worker_drained", worker_id=self.worker_id, finished=len(tasks) - len(pending),
                    released=len(pending))
        await self._liveness(queue.worker_stopped, self.pool, self.worker_id)


# AA-737: slot budget of the standalone worker service. S217 Nepal wave: with 4 slots, s1_rewrite
# (cap 4) held every slot for ~75 min while 60 a3_atomize jobs sat queued (median wait ~59 min),
# then atomize ran alone as a ~55 min tail. 8 slots = s1_rewrite (cap 6) + a3_atomize (cap 1, by
# design) + 1 for prefetch/recompute, so atomize drains alongside the rewrites.
WORKER_MAX_PARALLEL_DEFAULT = 8
# AA-712: background kinds (place pipeline) never take the last 2 free slots of a worker, and at
# most BACKGROUND_MAX_RUNNING of them run at once on a worker (all background kinds together).
BACKGROUND_RESERVE = 2
BACKGROUND_MAX_RUNNING = 1


def background_kinds() -> set[str]:
    return {name for name, k in kinds().items() if k.background}


def background_reserve_for(max_parallel: int) -> int:
    """AA-712: free slots kept away from background kinds — BACKGROUND_RESERVE, but never so many
    that a small worker (max_parallel <= 2) could not run a background job at all."""
    return min(BACKGROUND_RESERVE, max(max_parallel - 1, 0))


def claim_caps_for(max_parallel: int, caps: dict[str, int], free_slots: int, running_background: int,
                   background: set[str] | frozenset[str]) -> dict[str, int]:
    """The caps a worker passes to claim(): every kind in `caps`, minus the `background` kinds when
    only the reserved slots are free OR when BACKGROUND_MAX_RUNNING background jobs already run on
    this worker. claim() is FIFO across kinds, so without this a background batch queued before a
    wave could fill the slots a3_atomize needs (S217: 59-minute atomize tail). The reserve alone is
    not enough: background jobs claimed before a wave keep their slots, and each slot a wave job
    frees goes to the next s1_rewrite (review finding 2) — so background work is also capped as a
    group: 8 slots = s1_rewrite 6 + a3_atomize 1 + background 1.

    Pure (AA-751): Worker.claim_caps and the ops metrics sampler (shared/jobs/backlog.py) both call
    it, so the "would a live worker claim this row now" rule cannot drift from the worker's."""
    if free_slots <= background_reserve_for(max_parallel) or running_background >= BACKGROUND_MAX_RUNNING:
        return {name: cap for name, cap in caps.items() if name not in background}
    return dict(caps)


# AA-993 G1 (ADR 0016 D1, design Q1): the service worker's drain window. `JOB_WORKER_GRACE_SECONDS` (Infra sets 100 on
# aa-cis-dev-worker); absent = the pre-G1 20 s. It must leave RELEASE_MARGIN_SECONDS of the container's ECS stopTimeout
# (`JOB_WORKER_STOP_TIMEOUT_SECONDS`, default 120 = the worker container in AA-CIS-Infra modules/ecs) for the cancel,
# the release UPDATE and the pool close; after stopTimeout ECS sends SIGKILL and the job waits for the 90 s lease.
WORKER_GRACE_SECONDS_DEFAULT = 20.0
WORKER_STOP_TIMEOUT_SECONDS_DEFAULT = 120.0
RELEASE_MARGIN_SECONDS = 15.0


def _env_seconds(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number of seconds") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name}={raw!r} must be a finite number >= 0")
    return value


def worker_grace_seconds() -> float:
    """`JOB_WORKER_GRACE_SECONDS` (env) or the default, checked against the stopTimeout. Raises ValueError (the
    worker does not start, the deploy's verify goes red) rather than run with a window ECS would cut short."""
    grace = _env_seconds("JOB_WORKER_GRACE_SECONDS", WORKER_GRACE_SECONDS_DEFAULT)
    stop = _env_seconds("JOB_WORKER_STOP_TIMEOUT_SECONDS", WORKER_STOP_TIMEOUT_SECONDS_DEFAULT)
    if grace + RELEASE_MARGIN_SECONDS > stop:
        raise ValueError(f"JOB_WORKER_GRACE_SECONDS={grace:g} leaves less than {RELEASE_MARGIN_SECONDS:g} s of the "
                         f"{stop:g} s stopTimeout (JOB_WORKER_STOP_TIMEOUT_SECONDS) for the release")
    return grace


def worker_max_parallel() -> int:
    """`JOB_WORKER_MAX_PARALLEL` (env) or the default; never below 1."""
    try:
        return max(1, int(os.environ.get("JOB_WORKER_MAX_PARALLEL", WORKER_MAX_PARALLEL_DEFAULT)))
    except ValueError:
        return WORKER_MAX_PARALLEL_DEFAULT


def in_api_enabled() -> bool:
    """AA-735 (ADR 0003 nac 5): the worker no longer runs inside the API by default. The worker
    runs as its own ECS service (`python -m worker`); the API process starts an in-process worker
    only when `JOB_WORKER_IN_API` is explicitly set truthy (local/dev convenience). Unset or any
    falsy value → False, so a stray/default deploy can never run two workers against shared.job."""
    return os.environ.get("JOB_WORKER_IN_API", "false").lower() in ("1", "true", "yes")


def load_kinds() -> None:
    """Importing the package registers every job kind."""
    import services.jobs  # noqa: F401


async def deploy_gate_status(pool) -> dict:
    """AA-993 G5 (design D1.4) — may a deploy roll the api + worker now? Shared by GET /admin/job-runner/deploy-gate
    (the CI pre-deploy guard) and the Platform Apps page (AA-884), so both answer the same question the same way.

    `safe_to_deploy` is false only while
      - a rerun wave is open (`shared.rerun_wave.status = 'open'`, count in `open_waves`): a wave's expected set must
        not be split across two images (C6.3a), or
      - a job of a `deploy_blocking` kind is RUNNING (`blocking_running`, per kind): such a job is not safe to re-run
        after a release, or QUEUED and due within `queue.DEPLOY_BLOCKING_HORIZON_S` (`blocking_due`): the guard runs
        before the image build and the old task would claim it before its SIGTERM.
    Queued and running rows of every other kind no longer block: the worker drains on SIGTERM and releases a job still
    running after its grace window (G1-G4), and a queued row is claimed by whichever image is live. They stay in the
    answer as information (`active`, `by_status`, `scheduled_later`; `deploy_gate_exempt` kinds not counted there).
    `blocked_by` lists the reasons in words (empty when safe). A failed read raises (the endpoint answers 5xx), so the
    CI guard fails closed."""
    load_kinds()  # both flags live in the registry: make sure every kind is registered here
    exempt = sorted(k.name for k in kinds().values() if k.deploy_gate_exempt)
    blocking = sorted(k.name for k in kinds().values() if k.deploy_blocking)
    counts = await queue.active_count(pool, exempt_kinds=exempt)
    blockers = await queue.deploy_blockers(pool, blocking_kinds=blocking)
    open_waves, running, due = blockers["open_waves"], blockers["blocking_running"], blockers["blocking_due"]
    blocked_by = []
    if open_waves:
        blocked_by.append(f"{open_waves} rerun wave(s) open")
    for kind, n in running.items():
        blocked_by.append(f"{n} {kind} job(s) running (deploy-blocking kind)")
    for kind, n in due.items():
        blocked_by.append(f"{n} {kind} job(s) due (deploy-blocking kind, queued to start within 15 min)")
    return {**counts, "exempt_kinds": exempt, "open_waves": open_waves, "blocking_kinds": blocking,
            "blocking_running": running, "blocking_due": due, "blocked_by": blocked_by,
            "safe_to_deploy": not blocked_by}


async def _main() -> None:
    import asyncpg

    from shared.secrets import get_database_url

    load_kinds()
    max_parallel = worker_max_parallel()
    grace_seconds = worker_grace_seconds()  # AA-993 G1: fails before the pool opens on a bad value
    # AA-737: LangGraph runs sync nodes (every LLM call) on the loop's default executor, which is
    # sized from the CPU count (6 threads on the 0.5 vCPU task) — fewer than the slots. Size it to
    # the slots (2 per job: a node + a blocking side call such as atomize's Cohere pacing) so
    # raising max_parallel is not silently capped by threads. Same reason the pool grows with it.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=2 * max_parallel, thread_name_prefix="job"))
    pool = await asyncpg.create_pool(get_database_url(), min_size=1, max_size=max(5, max_parallel + 2),
                                     server_settings={"application_name": "cis-worker"})  # AA-824
    redis = None
    if os.environ.get("REDIS_HOST"):
        import redis.asyncio as aioredis
        from shared.cache.redis_conn import redis_conn_args  # AA-874: TLS + AUTH from env
        redis_url, redis_kwargs = redis_conn_args()
        redis = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True, **redis_kwargs)
    worker = Worker(pool, max_parallel=max_parallel, grace_seconds=grace_seconds, resources={"redis": redis})
    logger.info("job_worker_config", max_parallel=max_parallel, executor_threads=2 * max_parallel,
                grace_seconds=grace_seconds)
    # AA-919 (WM-18 scope 5): a deploy can change the gate taxonomy (services/quality/registry.TAXONOMY_VERSION): flag
    # the active writing-memory releases whose fingerprint no longer matches (they keep serving). Never fails the start.
    from services.writing_memory.releases import reverify_scan_safe
    await reverify_scan_safe(pool, actor="system:worker_start")
    await serve(worker)
    await pool.close()


async def serve(worker: Worker, signals: tuple = (signal.SIGTERM, signal.SIGINT)) -> None:
    """Run the worker until SIGTERM / SIGINT (ECS stop), then drain: stop claiming, give running jobs
    `grace_seconds`, release the rest (AA-993 G1). The handlers are removed on return."""
    loop = asyncio.get_running_loop()
    for sig in signals:
        loop.add_signal_handler(sig, worker._stopping.set)
    try:
        runner = asyncio.create_task(worker.run())
        await worker._stopping.wait()
        await worker.shutdown()
        await runner
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    asyncio.run(_main())
