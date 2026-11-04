"""AA-1028 R1 slice 3 (root ADR 0019 d1, d16) — job kind ``tenant_account_tick``: the hourly time-based moves of the
tenant lifecycle (``services/tenant_account/lifecycle.py``): a trial past ``trial_ends_at`` moves to ``active`` or
``past_due`` and its account out of ``trial``, overrides past ``expires_at`` are re-synced
(``shared.tenant_entitlement_sync_due()``), a pilot past ``pilot_ends_at`` is reported.

Payload: {"trigger": "schedule" | "manual"}. Queued by the api's ops metrics sampler once per UTC hour (idempotency
key ``tenant_account_tick:<YYYY-MM-DDTHH>``: one indexed EXISTS a tick after the first). ``TENANT_ACCOUNT_TICK=off``
on the api stops the schedule. $0 (database only, no LLM). A re-run is harmless (it moves only what is due), so a
failure retries (max_attempts 2) and the next hour's run picks up anything left.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import structlog

from services.tenant_account import lifecycle
from shared.jobs.registry import JobContext, enqueue, job_kind

logger = structlog.get_logger()

KIND = "tenant_account_tick"
SCHEDULER = "scheduler:aa-1028"
SWITCH_ENV = "TENANT_ACCOUNT_TICK"

_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


def schedule_enabled() -> bool:
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def schedule_key(now: datetime) -> str:
    return f"{KIND}:{now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H')}"


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=60)
async def run(ctx: JobContext) -> dict:
    out = await lifecycle.tick(ctx.pool, job_id=str(ctx.job_id))
    result = {"kind": KIND, "trigger": (ctx.payload or {}).get("trigger", "manual"), **out}
    logger.info("tenant_account_tick_done", job_id=str(ctx.job_id), trials_ended=out["trials"]["tenants"],
                trial_failures=out["trials"]["failed"], expired_synced=out["expired_entitlements_synced"],
                pilots_ended=len(out["pilots_ended"]))
    ctx.set_result(result)
    return result


async def enqueue_hourly(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, str]:
    """This UTC hour's run (idempotent per hour). Called by the ops metrics sampler on every tick."""
    now = now or datetime.now(timezone.utc)
    key = schedule_key(now)
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, key
    job_id, created = await enqueue(pool, KIND, {"trigger": "schedule"}, idempotency_key=key, created_by=SCHEDULER)
    return job_id, created, key
