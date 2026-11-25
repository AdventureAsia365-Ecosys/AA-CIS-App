"""AA-1178 (S292; CIS 489) — the running caps of the job runner's two side lanes, read from Platform › Settings.

The worker claims every kind FIFO from shared.job (shared/jobs/queue.py). Two side lanes keep slow or frequent work
apart from the waves and content jobs (shared/jobs/worker.py ``claim_caps_for``):

- **background** (``@job_kind(..., background=True)``, AA-712): long batches (place pipeline, retention, writing-memory
  runs, reports). They share the worker's main slots, never take its last ``BACKGROUND_RESERVE`` free ones, and at most
  ``cis.ops.jobs_background_max_running`` of them run at once on a worker (default 1).
- **monitor** (``@job_kind(..., background=True, monitor=True)``): the short periodic watchers (status page sampler,
  route and quality watchers, usage rollup, connection probe, model lifecycle sync, compliance database checks). They
  run in slots of their own on top of the main slots, at most ``cis.ops.jobs_monitor_max_running`` at once (default
  2): never counted toward, nor held by, the background cap, and never in a slot a wave could use. Before AA-1178 they
  were plain background kinds, and a 90-minute background run stopped the status page for its whole length.

``shared.settings.get`` serves the last snapshot (the worker and the api refresh it in the background) and the compiled
default before the first refresh. A read that still fails (a key not compiled yet, a broken definition) returns the
seeded default here: the claim loop must never stop claiming because a cap could not be read.
"""
from __future__ import annotations

import structlog

from shared import settings

BACKGROUND_RUNNING_KEY = "cis.ops.jobs_background_max_running"
MONITOR_RUNNING_KEY = "cis.ops.jobs_monitor_max_running"

ALL = (BACKGROUND_RUNNING_KEY, MONITOR_RUNNING_KEY)

logger = structlog.get_logger()
_warned: set[str] = set()


def _warn_once(key: str, fallback: int, e: Exception) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning("job_lane_cap_unreadable", key=key, fallback=fallback, error=f"{type(e).__name__}: {e}"[:200])


def _read(key: str, seeded: int) -> int:
    """The value in force, at least 1; the seeded default (CIS 489) when the reader cannot answer (warns once)."""
    try:
        return max(1, int(settings.get(key)))
    except Exception as e:  # noqa: BLE001 — a cap read never stops the claim loop
        _warn_once(key, seeded, e)
        return seeded


def background_max_running() -> int:
    """Long background jobs one worker runs at once (cis.ops.jobs_background_max_running)."""
    return _read(BACKGROUND_RUNNING_KEY, 1)


def monitor_max_running() -> int:
    """Monitoring jobs one worker runs at once, in their own slots (cis.ops.jobs_monitor_max_running)."""
    return _read(MONITOR_RUNNING_KEY, 2)


def monitor_slots_bound() -> int:
    """The most monitoring slots an administrator can set (the definition's upper bound): the worker sizes its DB pool
    and thread executor for it at start, so raising the setting later needs no restart. The value in force when the
    definition cannot be read."""
    try:
        return max(1, int(settings.definition(MONITOR_RUNNING_KEY).max_value))
    except Exception as e:  # noqa: BLE001 — sizing falls back to the cap in force
        _warn_once(MONITOR_RUNNING_KEY + ":bound", monitor_max_running(), e)
        return monitor_max_running()
