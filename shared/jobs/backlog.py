"""AA-751 (C2) — capacity-aware backlog metrics of shared.job, for CloudWatch `AA/Jobs/CIS`.

Published every 5 minutes by the api's ops metrics sampler (api/core/ops_metrics_sampler.py), never
by the worker: the sample must not depend on the queue it measures. Two values, no dimensions:

  OldestQueuedAgeSeconds  how long the longest-STARVED due queued row has been starved (0 if none)
  OverdueRunningJobs      running rows whose CURRENT attempt is older than 2 x the kind's
                          expected_seconds (registry; 600 for a kind this process does not know)

A plain "oldest queued" would page in every wave: per-kind caps, per-worker slots and the background
lane keep due rows queued by design (plan F13). A due queued row (not cancel_requested) counts only
when it is starved:

  (a) no live worker at all (shared.job_worker: stopped_at IS NULL and last_seen_at within
      queue.WORKER_ALIVE_SECONDS, the Jobs page rule), or no live worker has the kind in its caps
      (unregistered: never claimable, a bug or a dropped kind);
  (b) or some live, non-yielding worker would claim it right now: it has a free slot
      (max_parallel - running rows locked_by it), the kind survives that worker's claim rule
      (worker.claim_caps_for, the same function Worker.claim_caps runs), and the kind's global
      running count is below that worker's cap for it.

How long it has been starved: (a) since the last worker was seen alive (or since run_after when no
worker row exists; since run_after for an unregistered kind); (b) since the capacity last freed up
— the latest job finish or re-queue, or the worker's start — never earlier than the row's
run_after. Without that bound a sample taken in the ~5 s between a wave job finishing and the
worker's next poll would report the full queue wait of the next row of that kind and page (the
deviation from plan §5's plain `now - run_after`, recorded in docs/implementation-notes/AA-751.md).
A wave that fills every slot gives 0; stuck slots show up as overdue-running instead.

`attempt_started_at` is merged into progress by queue.claim() (plan F18); rows claimed before that
change fall back to started_at (the first attempt's start).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from . import queue
from .worker import claim_caps_for

DEFAULT_EXPECTED_SECONDS = 600
OVERDUE_FACTOR = 2

# One round trip each; all read-only, all on indexed or tiny sets (job_queued_due_idx,
# job_running_idx, job_finished_idx; shared.job_worker is pruned to 7 days).
CLOCK_SQL = """
    SELECT now() AS now,
           greatest((SELECT max(finished_at) FROM shared.job WHERE finished_at IS NOT NULL),
                    (SELECT max(updated_at) FROM shared.job WHERE status = 'queued' AND attempt > 0))
               AS last_freed_at
"""

QUEUED_DUE_SQL = """
    SELECT kind, count(*)::int AS n, min(run_after) AS oldest_run_after
      FROM shared.job
     WHERE status = 'queued' AND run_after <= now() AND NOT cancel_requested
     GROUP BY kind
"""

RUNNING_SQL = """
    SELECT id, kind, locked_by, started_at, progress ->> 'attempt_started_at' AS attempt_started_at
      FROM shared.job
     WHERE status = 'running'
"""

WORKERS_SQL = f"""
    SELECT worker_id, started_at, last_seen_at, max_parallel, caps, task_revision,
           (stopped_at IS NULL AND last_seen_at > now() - interval '{queue.WORKER_ALIVE_SECONDS} seconds')
               AS alive,
           (stopped_at IS NULL AND last_seen_at > now() - interval '{queue.NEWER_WORKER_SECONDS} seconds')
               AS fresh
      FROM shared.job_worker
"""


def _caps(v: Any) -> dict[str, int]:
    if isinstance(v, str):
        v = json.loads(v)
    return {str(k): int(c) for k, c in (v or {}).items()}


def _ts(v: Any) -> Optional[datetime]:
    """A timestamptz from the row, or the ISO string jsonb_build_object wrote; None if unparsable."""
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _age(now: datetime, since: Optional[datetime]) -> float:
    return max(0.0, (now - since).total_seconds()) if since is not None else 0.0


def _latest(*ts: Optional[datetime]) -> Optional[datetime]:
    known = [t for t in ts if t is not None]
    return max(known) if known else None


def _yielding(w: Mapping, workers: Iterable[Mapping]) -> bool:
    """queue.newer_worker_alive from the sampler's side: a live worker of a newer task revision makes
    this one stop claiming (AA-711). Unknown revision never yields."""
    rev = w.get("task_revision")
    if rev is None:
        return False
    return any(o.get("fresh") and o.get("task_revision") is not None and o["task_revision"] > rev
               for o in workers)


def attempt_started(row: Mapping) -> Optional[datetime]:
    return _ts(row.get("attempt_started_at")) or _ts(row.get("started_at"))


def backlog_sample(queued: Iterable[Mapping], running: Iterable[Mapping], workers: Iterable[Mapping],
                   kinds: Mapping[str, Any], now: datetime,
                   last_freed_at: Optional[datetime] = None) -> dict:
    """Pure. `queued`: rows of QUEUED_DUE_SQL (per kind); `running`: RUNNING_SQL; `workers`:
    WORKERS_SQL; `kinds`: the registry (name -> JobKind: background, expected_seconds); `now` and
    `last_freed_at`: CLOCK_SQL (the DB clock, so ages never mix two clocks)."""
    queued, running, workers = list(queued), list(running), list(workers)

    # ── overdue running ──
    overdue = []
    for r in running:
        k = kinds.get(r["kind"])
        expected = int(getattr(k, "expected_seconds", None) or DEFAULT_EXPECTED_SECONDS)
        age = _age(now, attempt_started(r))
        if age > OVERDUE_FACTOR * expected:
            overdue.append({"id": str(r["id"]), "kind": r["kind"], "age_s": round(age), "expected_s": expected})

    # ── starved queued ──
    background = frozenset(name for name, k in kinds.items() if getattr(k, "background", False))
    running_by_kind: dict[str, int] = {}
    for r in running:
        running_by_kind[r["kind"]] = running_by_kind.get(r["kind"], 0) + 1
    live = [w for w in workers if w.get("alive")]
    claimers = []   # (worker, caps it would pass to claim() now, when its capacity last freed)
    for w in live:
        if _yielding(w, workers):
            continue
        mine = [r for r in running if r.get("locked_by") == w["worker_id"]]
        free = int(w["max_parallel"]) - len(mine)
        if free <= 0:
            continue
        caps = _caps(w["caps"])
        running_bg = sum(1 for r in mine if r["kind"] in background)
        claim_caps = claim_caps_for(int(w["max_parallel"]), caps, free, running_bg, background)
        claimers.append((w, claim_caps, _latest(last_freed_at, _ts(w.get("started_at")))))
    live_kinds = set().union(*(_caps(w["caps"]) for w in live)) if live else set()
    last_seen = _latest(*(_ts(w.get("last_seen_at")) for w in workers))

    starved = {}
    for q in queued:
        kind, oldest = q["kind"], _ts(q["oldest_run_after"])
        if not live:
            reason, since = "no_live_worker", _latest(oldest, last_seen)
        elif kind not in live_kinds:
            reason, since = "unregistered", oldest
        else:
            frees = [freed for _w, cc, freed in claimers
                     if kind in cc and running_by_kind.get(kind, 0) < cc[kind]]
            if not frees:
                continue   # every worker that could take it is full or at the kind's cap: by design
            # the worker whose capacity has been free the longest; unknown = since run_after
            earliest = min(frees, key=lambda t: t or datetime.min.replace(tzinfo=now.tzinfo))
            reason, since = "claimable", _latest(oldest, earliest)
        starved[kind] = {"reason": reason, "ready": int(q["n"]), "age_s": round(_age(now, since))}

    return {
        "oldest_queued_age_seconds": float(max((s["age_s"] for s in starved.values()), default=0)),
        "overdue_running_jobs": len(overdue),
        "starved": starved,
        "overdue": overdue[:10],
        "queued_due": sum(int(q["n"]) for q in queued),
        "running": len(running),
        "live_workers": len(live),
    }


async def sample(pool, kinds: Mapping[str, Any]) -> dict:
    """Reads the four sets on one connection and returns backlog_sample()."""
    async with pool.acquire() as conn:
        clock = await conn.fetchrow(CLOCK_SQL)
        queued = await conn.fetch(QUEUED_DUE_SQL)
        running = await conn.fetch(RUNNING_SQL)
        workers = await conn.fetch(WORKERS_SQL)
    return backlog_sample([dict(r) for r in queued], [dict(r) for r in running], [dict(r) for r in workers],
                          kinds, clock["now"], clock["last_freed_at"])
