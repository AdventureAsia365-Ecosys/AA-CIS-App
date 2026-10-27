"""AA-650 — job kinds and their handlers.

A handler is `async def handler(ctx: JobContext) -> dict | None`; the returned dict is stored as
the job's `result`. Register with the decorator, next to the code it runs (services/jobs/):

    @job_kind("segment_research", concurrency=1, max_attempts=3)
    async def run(ctx): ...

Raise `NonRetryable` for failures a retry cannot fix (bad payload); raise
`shared.cost_guard.BudgetExceeded` to stop with status `stopped_budget`.

`on_terminal(pool, job_row)` (optional) runs once a job ends without success — failed,
stopped_budget or cancelled — whoever ended it (the worker, the reaper, or an admin cancelling a
queued job). Use it to mark the domain row (e.g. a tour version) so no UI waits forever.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from . import queue


class NonRetryable(Exception):
    """The job fails at once, without using its remaining attempts."""


@dataclass(frozen=True)
class JobKind:
    name: str
    handler: Callable[["JobContext"], Awaitable[Optional[dict]]]
    concurrency: int = 1
    max_attempts: int = 3
    on_terminal: Optional[Callable[[Any, dict], Awaitable[None]]] = None
    # AA-687: a running job older than this is flagged on the Jobs page (not stopped).
    expected_seconds: int = 600
    # AA-712: a background kind never takes one of the worker's last BACKGROUND_RESERVE free
    # slots (worker.claim_caps), so it cannot starve the pipeline kinds of a wave (S217 lesson).
    background: bool = False
    # AA-832: the pre-deploy guard (GET /admin/job-runner/deploy-gate) does not count this kind's
    # queued/running rows. Only for short, read-only, frequently re-scheduled kinds whose loss to a
    # deploy costs one sample at most. Unused since AA-751 (the RDS sample left the job runner).
    deploy_gate_exempt: bool = False
    # AA-839: POST /admin/job-runner/jobs/{id}/retry asks this first; a message = refused (409). For kinds whose
    # job must never run twice from the same payload (an approved, destructive apply).
    retry_refusal: Optional[Callable[[dict], Optional[str]]] = None
    # AA-993 G1 (ADR 0016 D1): a job of this kind released on shutdown is due again only this many seconds later
    # (queue.release run_after). For kinds whose work outlives the cancelled handler (a thread keeps running on the
    # draining task until the process ends): set it to the ECS stopTimeout so the old task is gone before a new
    # worker re-claims. 0 = due at once (the default).
    release_delay_seconds: int = 0
    # AA-993 G1: a kind that a deploy must not interrupt (not safe to re-run after a release). Registry flag only in
    # G1; the deploy guard reads it from G5 on (a running row of such a kind will block the deploy).
    deploy_blocking: bool = False


_KINDS: dict[str, JobKind] = {}

# Kinds whose handler was removed -> the issue that removed it. No worker can claim their rows, so
# the api's ops metrics sampler (api/core/ops_metrics_sampler.py) cancels any queued/running row of
# them on every tick (queue.cancel_retired_kinds), while the kind is not registered. Keeps a row
# left over from a rolling deploy from paging `*-jobs-queue-age` and blocking Deploy Dev. Drop an
# entry once no row of it can appear any more (its scheduler is gone and a deploy has passed).
RETIRED_KINDS: dict[str, str] = {
    "ops_rds_role_connections": "AA-751",   # AA-832 Lambda -> job chain, replaced by the sampler
}


def job_kind(name: str, *, concurrency: int = 1, max_attempts: int = 3,
             on_terminal: Optional[Callable[[Any, dict], Awaitable[None]]] = None,
             expected_seconds: int = 600, background: bool = False, deploy_gate_exempt: bool = False,
             retry_refusal: Optional[Callable[[dict], Optional[str]]] = None,
             release_delay_seconds: int = 0, deploy_blocking: bool = False):
    if release_delay_seconds < 0:
        raise ValueError(f"job kind {name!r}: release_delay_seconds must be >= 0")
    if deploy_blocking and deploy_gate_exempt:
        raise ValueError(f"job kind {name!r}: deploy_blocking and deploy_gate_exempt contradict each other")

    def deco(fn):
        _KINDS[name] = JobKind(name, fn, concurrency, max_attempts, on_terminal, expected_seconds, background,
                               deploy_gate_exempt, retry_refusal, release_delay_seconds, deploy_blocking)
        return fn
    return deco


async def run_terminal_hook(pool, job_id: str) -> None:
    """Calls the kind's on_terminal for a job that ended without success. Never raises."""
    import structlog
    log = structlog.get_logger()
    try:
        row = await queue.get(pool, job_id)
        if row is None or row["status"] not in ("failed", "stopped_budget", "cancelled"):
            return
        kind = _KINDS.get(row["kind"])
        if kind is not None and kind.on_terminal is not None:
            await kind.on_terminal(pool, row)
    except Exception as e:
        log.warning("job_terminal_hook_failed", job_id=job_id, error=str(e)[:300])


def kinds() -> dict[str, JobKind]:
    return dict(_KINDS)


def get_kind(name: str) -> Optional[JobKind]:
    return _KINDS.get(name)


class JobContext:
    """What a handler sees: the job, the pool, and ways to report progress and cost."""

    def __init__(self, pool, job: queue.Job, resources: Optional[dict] = None):
        self.pool = pool
        self.job = job
        # Shared clients the worker owns (e.g. "redis" for live-writing progress, ADR 0004).
        self.resources: dict = resources or {}
        self.payload: dict = job.payload
        self.cost_usd: float = job.cost_usd
        self.result: Optional[dict] = None

    @property
    def job_id(self) -> str:
        return self.job.id

    def add_cost(self, usd: Optional[float]) -> None:
        self.cost_usd += float(usd or 0.0)

    async def progress(self, **fields: Any) -> None:
        """Merges `fields` into the job's progress (shown on the Jobs page) and saves the cost."""
        await queue.set_progress(self.pool, self.job.id, fields, round(self.cost_usd, 6))

    def set_result(self, result: dict) -> None:
        """Kept even when the handler then stops on a budget breach."""
        self.result = result


async def enqueue(pool, kind: str, payload: dict, **kw) -> tuple[str, bool]:
    """Enqueue with the kind's registered defaults (max_attempts) unless overridden."""
    k = _KINDS.get(kind)
    if k is None:
        raise ValueError(f"unknown job kind {kind!r}")
    kw.setdefault("max_attempts", k.max_attempts)
    return await queue.enqueue(pool, kind, payload, **kw)
