"""AA-1140 (S289, the Ecosystem status page) — job kind ``ops_status_sample``: one tick of the status sampler
(``api/services/ops_status/sampler.py``) per 5-minute UTC bucket, writing ``acp_shared.ops_sample`` and
``acp_shared.ops_event`` (CIS 476). $0: ECS DescribeServices / DescribeTaskDefinition and CloudWatch DescribeAlarms are
free control-plane reads of the existing read-only grant, the health checks are public URLs, the rest is DB.

Payload: {"trigger": "schedule" | "manual", "bucket": "<ISO start of the 5-minute bucket>"}.

Schedule: queued by the api's ops metrics sampler on every tick (no cron in the api), idempotency key
``ops_status_sample:<bucket start>``, so two api tasks during a rolling deploy queue one run. Switch
``OPS_STATUS_SAMPLER`` (ECS env, default ON: the run costs nothing). A run that starts more than ``LATE_AFTER_S`` after
its bucket (a worker that was down) writes nothing and says so: its numbers would be labelled with the wrong time, and
the uptime strip then shows that bucket as "no sample", which is the truth. max_attempts 1 (the next bucket is the
retry), background (never takes one of the last free worker slots), deploy-gate exempt (it may sit queued across a
deploy; a re-run of a bucket writes nothing twice).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "ops_status_sample"
SCHEDULER = "scheduler:aa-1140"
SWITCH_ENV = "OPS_STATUS_SAMPLER"
BUCKET_MINUTES = 5
LATE_AFTER_S = 600  # technical-constant: two buckets late = the sample would carry the wrong time


def schedule_enabled() -> bool:
    """``OPS_STATUS_SAMPLER`` (default on): off / false / 0 / no stops the schedule."""
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def bucket_start(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return now.replace(minute=now.minute - now.minute % BUCKET_MINUTES, second=0, microsecond=0)


def bucket_key(now: datetime) -> str:
    return f"{KIND}:{bucket_start(now).strftime('%Y-%m-%dT%H:%M')}"


_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


async def enqueue_tick(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, str]:
    """This bucket's run (idempotent per bucket). Called by the ops metrics sampler on every tick: one indexed EXISTS
    once the bucket's run exists."""
    now = now or datetime.now(timezone.utc)
    key = bucket_key(now)
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, key
    job_id, created = await enqueue(pool, KIND, {"trigger": "schedule", "bucket": bucket_start(now).isoformat()},
                                    idempotency_key=key, created_by=SCHEDULER, max_attempts=1)
    return job_id, created, key


def _bucket(payload: dict, now: datetime) -> datetime:
    raw = payload.get("bucket")
    if raw is None:
        return bucket_start(now)
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as e:
        raise NonRetryable(f"bad bucket {raw!r}") from e
    if t.tzinfo is None:
        raise NonRetryable(f"bucket {raw!r} has no time zone")
    return bucket_start(t)


@job_kind(KIND, concurrency=1, max_attempts=1, expected_seconds=60, background=True, deploy_gate_exempt=True)
async def run(ctx: JobContext) -> dict:
    from api.services.ops_status import sampler

    payload = ctx.payload or {}
    now = datetime.now(timezone.utc)
    at = _bucket(payload, now)
    if now - at > timedelta(seconds=LATE_AFTER_S):
        result = {"kind": KIND, "status": "skipped_late", "bucket": at.isoformat(),
                  "late_s": int((now - at).total_seconds())}
        ctx.set_result(result)
        return result
    out = await sampler.tick(ctx.pool, at, str(ctx.job_id))
    result = {"kind": KIND, "status": "sampled", "trigger": payload.get("trigger", "manual"), **out}
    logger.info("ops_status_sampled", job_id=ctx.job_id, **{k: v for k, v in out.items() if k.endswith("_error")})
    ctx.set_result(result)
    return result
