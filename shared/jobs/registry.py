"""AA-650 — job kinds and their handlers.

A handler is `async def handler(ctx: JobContext) -> dict | None`; the returned dict is stored as
the job's `result`. Register with the decorator, next to the code it runs (services/jobs/):

    @job_kind("segment_research", concurrency=1, max_attempts=3)
    async def run(ctx): ...

Raise `NonRetryable` for failures a retry cannot fix (bad payload); raise
`shared.cost_guard.BudgetExceeded` to stop with status `stopped_budget`.
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


_KINDS: dict[str, JobKind] = {}


def job_kind(name: str, *, concurrency: int = 1, max_attempts: int = 3):
    def deco(fn):
        _KINDS[name] = JobKind(name, fn, concurrency, max_attempts)
        return fn
    return deco


def kinds() -> dict[str, JobKind]:
    return dict(_KINDS)


def get_kind(name: str) -> Optional[JobKind]:
    return _KINDS.get(name)


class JobContext:
    """What a handler sees: the job, the pool, and ways to report progress and cost."""

    def __init__(self, pool, job: queue.Job):
        self.pool = pool
        self.job = job
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
