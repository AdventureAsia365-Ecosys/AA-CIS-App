"""AA-1142 follow-up (S293) — job kind ``ops_external_check``: the external checks of the Ecosystem status page, run
by the CIS worker (source ``worker`` of ``acp_shared.ops_external_check``, CIS 493).

Why. The S292 producer, the root repo workflow External checks on GitHub Actions (cron every 15 minutes), had 0
scheduled runs on its first day, and the other root crons start up to 10 hours late: a GitHub schedule cannot feed a
15-minute check. The worker runs the same checks itself (:mod:`api.services.ops_status.external_probe`: DNS, a
verified TLS handshake and the certificate's end date, one GET of the health path without following redirects) from
its internet egress, through the public internet like a customer. The GitHub workflow stays an optional second vantage
point (``cis.ops.external_check_sources``). $0: no new AWS service, a few public requests per run.

Payload: {"trigger": "schedule" | "manual", "bucket": "<ISO start of the interval bucket>"}. Result: the run's time,
the targets checked, the count per state, the entries of ``cis.ops.external_check_targets`` skipped as invalid.

Schedule: queued by the api's ops metrics sampler on every tick (:func:`tick_part`) once per bucket of
``cis.ops.external_check_interval_min`` minutes (aligned to the UTC hour), idempotency key
``ops_external_check:<bucket start>``, so two api tasks during a rolling deploy queue one run; only while ``worker`` is
in ``cis.ops.external_check_sources``. A run that starts more than one interval after its bucket writes nothing (the
next bucket is the retry; its rows would carry a misleading time). max_attempts 1, monitoring lane (background with
monitor=True: its own worker slots, never behind a long background job, AA-1178), deploy-gate exempt (it may sit queued
across a deploy; a re-run of a bucket stores a new run, harmless).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from shared import ops_external_settings as xs
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "ops_external_check"
SCHEDULER = "scheduler:aa-1142"
_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


def bucket_start(now: datetime, minutes: int) -> datetime:
    """The start of the ``minutes`` bucket holding ``now`` (buckets aligned to the UTC hour). Pure."""
    now = now.astimezone(timezone.utc)
    minutes = max(1, int(minutes))
    hour = now.replace(minute=0, second=0, microsecond=0)
    return hour + timedelta(minutes=(now.minute // minutes) * minutes)


def bucket_key(start: datetime) -> str:
    return f"{KIND}:{start.strftime('%Y-%m-%dT%H:%M')}"


async def enqueue_tick(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, str]:
    """This bucket's run (idempotent per bucket): one indexed EXISTS once the bucket's run exists."""
    now = now or datetime.now(timezone.utc)
    start = bucket_start(now, xs.interval_min())
    key = bucket_key(start)
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, key
    job_id, created = await enqueue(pool, KIND, {"trigger": "schedule", "bucket": start.isoformat()},
                                    idempotency_key=key, created_by=SCHEDULER, max_attempts=1)
    return job_id, created, key


async def tick_part(pool, kinds: dict, now: datetime, out: dict) -> None:
    """The ops metrics sampler's part (api/core/ops_metrics_sampler.sample_once): this bucket's run, only while the
    kind is registered and the worker is a source in use. Field ``ops_external_check`` when a run was created."""
    if KIND not in kinds or "worker" not in xs.sources():
        return
    job_id, created, key = await enqueue_tick(pool, now=now)
    if created:
        out[KIND] = {"job_id": job_id, "key": key}


def _bucket(payload: dict) -> Optional[datetime]:
    raw = payload.get("bucket")
    if raw is None:
        return None
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as e:
        raise NonRetryable(f"bad bucket {raw!r}") from e
    if t.tzinfo is None:
        raise NonRetryable(f"bucket {raw!r} has no time zone")
    return t.astimezone(timezone.utc)


@job_kind(KIND, concurrency=1, max_attempts=1, expected_seconds=60, background=True, deploy_gate_exempt=True,
          monitor=True)
async def run(ctx: JobContext) -> dict:
    from api.services.ops_status import external, external_probe

    payload = ctx.payload or {}
    now = datetime.now(timezone.utc)
    bucket = _bucket(payload)
    minutes = xs.interval_min()
    if bucket is not None and now - bucket > timedelta(minutes=minutes):
        result = {"kind": KIND, "status": "skipped_late", "bucket": bucket.isoformat(),
                  "late_s": int((now - bucket).total_seconds())}
        ctx.set_result(result)
        return result
    targets, skipped = xs.targets()
    for s in skipped:
        logger.warning("ops_external_check_target_skipped", target=s["id"], problem=s["problem"])
    checked_at = now.replace(microsecond=0)
    results = await external_probe.run(targets, xs.timeout_s())
    stored = await external.store_run(ctx.pool, "worker", checked_at, results)
    states = Counter(external.state_of(r, checked_at, external.th.cert_days()) for r in results)
    result = {"kind": KIND, "status": "checked", "trigger": payload.get("trigger", "manual"),
              "checked_at": checked_at.isoformat(), "targets": stored, "states": dict(sorted(states.items())),
              "skipped": skipped}
    logger.info("ops_external_checked", job_id=ctx.job_id, targets=stored, states=result["states"],
                skipped=len(skipped))
    ctx.set_result(result)
    return result
