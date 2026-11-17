"""AA-1037 R4 slice 1 (root ADR 0019 d9) — job kind ``tenant_billing_tick``: the hourly move of the tenant account
from AA-Booking's billing status (``booking.v_tenant_billing_status``, bk-042): newer paid-through days, warn ->
``past_due`` + the tenant's bell, past the suspend days -> ``suspended`` (reason ``billing``), paid again -> back in
service (``services/tenant_account/billing.py``). On Dev only synthetic (``demo``) tenants move. AA-1116 (contract
tenant.entitlements.v1 1.10): the same run then refreshes the quote discount of every subscription line
(``services/tenant_account/quote_discount.py``, CIS 443), which Booking reads in ``catalog.v_tenant_billing_v1``; a
failure there never fails the billing move (``discounts.outcome`` failed).

Payload: {"trigger": "schedule" | "manual"}. Queued by the api's ops metrics sampler once per UTC hour (idempotency
key ``tenant_billing_tick:<YYYY-MM-DDTHH>``). ``TENANT_BILLING_TICK=off`` on the api stops the schedule. $0 (database
only, no LLM). A re-run is harmless (it moves only what Booking's status asks for), so a failure retries
(max_attempts 2) and the next hour's run picks up anything left.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import structlog

from services.tenant_account import billing, quote_discount
from shared.jobs.registry import JobContext, enqueue, job_kind

logger = structlog.get_logger()

KIND = "tenant_billing_tick"
SCHEDULER = "scheduler:aa-1037"
SWITCH_ENV = "TENANT_BILLING_TICK"

_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


def schedule_enabled() -> bool:
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def schedule_key(now: datetime) -> str:
    return f"{KIND}:{now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H')}"


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=60)
async def run(ctx: JobContext) -> dict:
    out = await billing.tick(ctx.pool, job_id=str(ctx.job_id))
    try:
        discounts = await quote_discount.refresh_all(ctx.pool)
    except Exception:  # noqa: BLE001 — logged; the billing move above is done, the next hour retries the discounts
        logger.warning("tenant_billing_tick_discounts_failed", job_id=str(ctx.job_id), exc_info=True)
        discounts = {"outcome": "failed"}
    result = {"kind": KIND, "trigger": (ctx.payload or {}).get("trigger", "manual"), **out, "discounts": discounts}
    logger.info("tenant_billing_tick_done", job_id=str(ctx.job_id), outcome=out["outcome"],
                warned=out.get("warned", 0), suspended=out.get("suspended", 0), restored=out.get("restored", 0),
                failed=out.get("failed", 0))
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
