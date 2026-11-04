"""AA-1045 — job kind ``ai_usage_rollup``: (re)build the hourly usage rollup ``shared.ai_usage_hourly`` (CIS 372) from
the paid-call ledgers (api/services/usage_rollup.py). $0: database reads and writes only.

Payload: {"mode": "schedule" | "backfill" | "manual", "from": ISO (backfill, manual), "to": ISO (manual),
"requested_by": "admin:<id>" (manual)}.

- schedule: the last ``LATE_HOURS`` UTC hours, the current one included, so a row that lands late (a long
  transaction, a retried write) is counted on a later run. Queued by the api's ops metrics sampler every
  ``SCHEDULE_EVERY_MIN`` minutes (idempotency key ``ai_usage_rollup:<YYYY-MM-DDTHH:MM>``).
- backfill: every hour from ``from`` (default: the start of the ledgers' raw window) to now, one day per transaction,
  with progress. Queued once by the sampler (idempotency key ``ai_usage_rollup:backfill:v1``) when the table exists.
- manual: [from, to) from POST /admin/usage-explorer/rollup/run (audited there).

Every mode clamps to whole hours inside the raw window (usage_rollup.clamp_range): an hour whose raw rows the retention
job may have deleted is never rewritten. Idempotent per hour, so a retry or an overlap with another mode is harmless;
concurrency 1 keeps them in turn. ``AI_USAGE_ROLLUP=off`` on the api stops the schedule and the backfill. Background
and deploy-gate exempt (it is always queued); a deploy releases it and the next run redoes the same hours.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from api.services import usage_rollup as ur
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = ur.KIND
SCHEDULER = "scheduler:aa-1045"
SWITCH_ENV = "AI_USAGE_ROLLUP"
LATE_HOURS = 6
SCHEDULE_EVERY_MIN = 15
BACKFILL_KEY = f"{KIND}:backfill:v1"
MODES = ("schedule", "backfill", "manual")

_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"
_TABLE_SQL = "SELECT to_regclass('shared.ai_usage_hourly') IS NOT NULL"


def schedule_enabled() -> bool:
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def _ts(value, name: str) -> Optional[datetime]:
    if value is None or value == "":
        return None
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        raise NonRetryable(f"{name} {value!r} is not an ISO date-time")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def plan(payload: dict, now: datetime) -> tuple[str, datetime, datetime]:
    """(mode, since, until) of one run, clamped to whole hours inside the raw window. Pure."""
    mode = payload.get("mode", "schedule")
    if mode not in MODES:
        raise NonRetryable(f"mode must be one of {', '.join(MODES)}")
    end = ur.floor_hour(now) + timedelta(hours=1)
    if mode == "schedule":
        since, until = end - timedelta(hours=LATE_HOURS), end
    elif mode == "backfill":
        since, until = _ts(payload.get("from"), "from") or (now - ur.RAW_WINDOW), end
    else:
        since, until = _ts(payload.get("from"), "from"), _ts(payload.get("to"), "to")
        if since is None or until is None:
            raise NonRetryable("a manual run needs from and to")
    try:
        a, b = ur.clamp_range(since, until, now=now)
    except ValueError as e:
        raise NonRetryable(str(e))
    return mode, a, b


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=1200, background=True, deploy_gate_exempt=True)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    mode, since, until = plan(payload, datetime.now(timezone.utc))

    async def progress(fields: dict) -> None:
        if mode != "schedule":
            await ctx.progress(**fields)

    try:
        summary = await ur.roll_hours(ctx.pool, since, until, progress=progress)
    except ur.RollupUnavailable as e:
        raise NonRetryable(str(e))
    result = {"kind": KIND, "mode": mode, **summary}
    ctx.set_result(result)
    logger.info("ai_usage_rollup_done", **result)
    return result


def schedule_key(now: datetime) -> str:
    t = now.astimezone(timezone.utc)
    slot = t.replace(minute=t.minute - t.minute % SCHEDULE_EVERY_MIN, second=0, microsecond=0)
    return f"{KIND}:{slot.strftime('%Y-%m-%dT%H:%M')}"


async def enqueue_scheduled(pool, now: Optional[datetime] = None) -> list[dict]:
    """The one-time backfill (first) and this quarter hour's run, each idempotent. Called by the ops metrics sampler
    on every tick: one to_regclass and at most two indexed EXISTS. Nothing is queued before CIS 372 is applied."""
    now = now or datetime.now(timezone.utc)
    if not await pool.fetchval(_TABLE_SQL):
        return []
    out = []
    for key, payload in ((BACKFILL_KEY, {"mode": "backfill"}), (schedule_key(now), {"mode": "schedule"})):
        if await pool.fetchval(_EXISTS_SQL, key):
            continue
        job_id, created = await enqueue(pool, KIND, payload, idempotency_key=key, created_by=SCHEDULER)
        if created:
            out.append({"job_id": job_id, "mode": payload["mode"]})
    return out


async def enqueue_manual(pool, *, since: datetime, until: datetime, actor: str) -> tuple[Optional[str], bool]:
    """An admin's re-roll of [since, until) (no idempotency key: each click is a run; the kind is concurrency 1)."""
    payload = {"mode": "manual", "from": since.isoformat(), "to": until.isoformat(), "requested_by": actor[:64]}
    return await enqueue(pool, KIND, payload, created_by=actor[:64])
