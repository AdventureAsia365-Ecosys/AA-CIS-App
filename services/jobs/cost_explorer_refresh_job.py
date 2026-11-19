"""AA-1127 (C2 "spend truth"; tech lead decision S285 E2; CIS 461) — job kind ``cost_explorer_refresh``: one AWS Cost
Explorer read of both stored snapshots (per account and service, and the main account by App tag) for the month to date,
the same data ``POST /admin/cost-explorer/check`` stores (api/services/cost_explorer_daily.py ``refresh``).

Payload: {"trigger": "schedule" | "manual", "start": "YYYY-MM-DD", "end_exclusive": "YYYY-MM-DD"} (both dates optional:
default ``cost_explorer_daily.refresh_window`` of the run's UTC day).

Queued by the api's ops metrics sampler once per UTC day from ``cis.spend.ce_daily_refresh_hour_utc`` while
``cis.spend.ce_daily_refresh_enabled`` is on (Platform › Settings, area "spend"; idempotency key
``cost_explorer_refresh:<day>``). Paid: about 0.01 USD per Cost Explorer request, about 3 requests a run; the estimate
is the job's own cost (``shared.job.cost_usd``). max_attempts 1: a failed paid read is not retried (the next day's run,
the By app lazy refresh or "Refresh from AWS" cover it). Background kind, concurrency 1; not deploy-gate exempt (a run
is short, and a run released by a deploy would pay again).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import structlog

from api.services import cost_explorer_daily as daily
from shared.aws_client.cost_explorer import CostExplorerUnavailable
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "cost_explorer_refresh"
SCHEDULER = "scheduler:aa-1127"


def _window(payload: dict, today: date) -> tuple[date, date]:
    raw_start, raw_end = payload.get("start"), payload.get("end_exclusive")
    if raw_start is None and raw_end is None:
        return daily.refresh_window(today)
    try:
        start, end = date.fromisoformat(str(raw_start)), date.fromisoformat(str(raw_end))
    except ValueError:
        raise NonRetryable("start and end_exclusive must both be YYYY-MM-DD")
    if not start < end:
        raise NonRetryable("start must be before end_exclusive")
    return start, end


@job_kind(KIND, concurrency=1, max_attempts=1, expected_seconds=120, background=True)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    start, end = _window(payload, datetime.now(timezone.utc).date())
    try:
        summary = await daily.refresh(ctx.pool, start, end)
    except CostExplorerUnavailable:
        # both reads failed (nothing stored): the job fails without a retry; the server log has the AWS error
        raise NonRetryable("Cost Explorer reads failed for every account and the App-tag read (see the server log)")
    ctx.add_cost(summary["est_cost_usd"])
    result = {"kind": KIND, "trigger": payload.get("trigger", "manual"), **summary}
    ctx.set_result(result)
    return result


def schedule_key(day: date) -> str:
    return f"{KIND}:{day.isoformat()}"


_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


async def enqueue_daily(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, date]:
    """Today's (UTC) run, from the setting's hour on, only with the setting on (idempotent per day). Called by the ops
    metrics sampler on every tick: before the hour nothing is read; after it one indexed EXISTS, so only the first tick
    INSERTs."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    if not daily.schedule_enabled() or now.astimezone(timezone.utc).hour < daily.run_hour_utc():
        return None, False, today
    key = schedule_key(today)
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, today
    start, end = daily.refresh_window(today)
    job_id, created = await enqueue(pool, KIND, {"trigger": "schedule", "start": start.isoformat(),
                                                 "end_exclusive": end.isoformat()},
                                    idempotency_key=key, created_by=SCHEDULER)
    return job_id, created, today


async def sampler_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """The ops metrics sampler's part (api/core/ops_metrics_sampler.py sample_once, one short budget): today's run via
    :func:`enqueue_daily`, only while the kind is registered. Field ``cost_explorer_refresh`` when a job was created."""
    if KIND not in kinds:
        return
    job_id, created, day = await enqueue_daily(pool, now=ts)
    if created:
        out["cost_explorer_refresh"] = {"job_id": job_id, "day": day.isoformat()}
