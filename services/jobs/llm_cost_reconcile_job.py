"""AA-1002 (gateway slice S10) — job kind ``llm_cost_reconcile``: the gateway's cost ledger next to each provider's
own figure, per LLM connection and UTC day (services/llm_cost/reconcile.py, table shared.llm_cost_reconcile, CIS 316).

Payload: {"trigger": "schedule" | "manual", "days": 1..31 (default 3), "end_day": "YYYY-MM-DD" (default yesterday,
UTC), "requested_by": "admin:<id>" (manual)}.

Queued by the api's ops metrics sampler once per UTC day from ``SCHEDULE_HOUR_UTC`` (idempotency key
``llm_cost_reconcile:<day>``; the last 3 days, because Cost Explorer revises the latest 24-48 h), and by
``POST /admin/llm-connections/reconcile``. ``LLM_COST_RECONCILE=off`` on the api stops the schedule.

$0: database reads and writes, the stored Cost Explorer snapshot (no live Cost Explorer call), and the vendor usage
APIs only for a connection that names a usage key (none today: every vendor source answers ``not_configured``).
Background kind; not deploy-blocking (a re-run of the same window rewrites the same rows).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import structlog

from services.llm_cost import reconcile
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "llm_cost_reconcile"
SCHEDULER = "scheduler:aa-1002"
SCHEDULE_HOUR_UTC = 2
SWITCH_ENV = "LLM_COST_RECONCILE"


def schedule_enabled() -> bool:
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def _parse_payload(payload: dict) -> tuple[int, Optional[date]]:
    days = payload.get("days", reconcile.DEFAULT_DAYS)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= reconcile.MAX_DAYS:
        raise NonRetryable(f"days must be an integer from 1 to {reconcile.MAX_DAYS}")
    raw = payload.get("end_day")
    if raw is None:
        return days, None
    try:
        return days, date.fromisoformat(str(raw))
    except ValueError:
        raise NonRetryable(f"end_day {raw!r} is not YYYY-MM-DD")


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=300, background=True)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    days, end_day = _parse_payload(payload)
    try:
        summary = await reconcile.reconcile(ctx.pool, days=days, end_day=end_day, job_id=str(ctx.job_id))
    except ValueError as e:
        raise NonRetryable(str(e))
    except reconcile.ReconcileUnavailable as e:
        raise NonRetryable(str(e))
    result = {"kind": KIND, "trigger": payload.get("trigger", "manual"), **summary}
    ctx.set_result(result)
    return result


def schedule_key(day: date) -> str:
    return f"{KIND}:{day.isoformat()}"


_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


async def enqueue_nightly(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, date]:
    """Today's (UTC) nightly run, from SCHEDULE_HOUR_UTC on (idempotent per day). Called by the ops metrics sampler on
    every tick: before the hour nothing is read; after it one indexed EXISTS, so only the first tick INSERTs."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if now.hour < SCHEDULE_HOUR_UTC:
        return None, False, today
    key = schedule_key(today)
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, today
    job_id, created = await enqueue(pool, KIND, {"trigger": "schedule", "days": reconcile.DEFAULT_DAYS,
                                                 "end_day": (today - timedelta(days=1)).isoformat()},
                                    idempotency_key=key, created_by=SCHEDULER)
    return job_id, created, today


async def enqueue_manual(pool, *, days: int, end_day: Optional[date], actor: str) -> tuple[Optional[str], bool]:
    """An admin's run (no idempotency key: each click is a run; the job is concurrency 1)."""
    payload = {"trigger": "manual", "days": days, "requested_by": actor[:64]}
    if end_day is not None:
        payload["end_day"] = end_day.isoformat()
    return await enqueue(pool, KIND, payload, created_by=actor[:64])
