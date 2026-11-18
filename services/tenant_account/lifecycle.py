"""Time-based moves of the tenant lifecycle (AA-1028 R1 slice 3, root ADR 0019 d1; S265 design README §3.2, §3.7),
run by the hourly ``tenant_account_tick`` job (``services/jobs/tenant_account_tick_job.py``).

1. **Trial end.** A ``trialing`` subscription past ``trial_ends_at`` moves to its next state: ``active`` when finance
   recorded payment covering today (``paid_through >= current_date``, billing v1 is manual, §3.7), else ``past_due``
   (the grace state: service goes on, finance sees it; suspending after the grace is the billing tick's job, not this
   one). The account leaves ``trial`` once no trial of it is running: ``active`` when its content subscription is
   ``active``, else ``past_due``. The CIS 353 subscription trigger re-syncs the tenant's entitlements (source
   ``trial`` -> ``plan``, ``expires_at`` cleared) and writes the feed events the CRM and Booking pulls apply. Until
   the tick moves it, an ended trial stays live in the resolver (CIS 361: source ``trial``, ``expires_at`` in the
   past), so the tenant keeps its plan's limits, the AI budget included, between ``trial_ends_at`` and the tick (up
   to an hour plus the queue); ``shared.cost_guard`` also falls back to the content plan's AI budget when no key
   resolves (before CIS 361, CIS 356 dropped an ended trial at once). The budget never reads as unlimited.
2. **Expired overrides.** ``shared.tenant_entitlement_sync_due()`` (CIS 353): a tenant whose materialised rows carry a
   passed ``expires_at`` (an override that ended) is re-synced, so the table and the feed follow the live view.
3. **Pilot end** (root ADR 0019 d16): a pilot tenant past ``pilot_ends_at`` is reported (count + log line
   ``tenant_pilot_ended``), never changed: ending a real agency's service is the tech lead's call, with notice.
4. **AI allowance holds** (AA-1126 L1): the Content studio jobs a tenant's AI budget held (``stopped_budget``, error
   ``tenant_ai_budget_approval``) are re-queued once their tenant no longer needs approval (a new month, an upgrade, a
   raised budget or the hard stop switched off): ``shared.jobs.queue.retry_by_reason`` keeps every job of a tenant
   that still needs approval held. Holds since the start of the previous UTC month are looked at (a monthly allowance:
   an older hold has already had its reset). The agency was told the work continues on its own
   (``tenant_budget.ON_HOLD_MESSAGE``). Best effort: a failure is logged and the tick goes on. The automatic path is
   narrower than the staff bulk Retry (``retry_by_reason`` apply): a job resumes only when its tenant still has a budget
   that no longer needs approval, its account is in service (``reader.IN_SERVICE``, ``is_active``, not cancelled) and
   no erasure request of it is open (:func:`resume_refusal`). A removed budget, a suspended, offboarding or closed
   account, or an erasure in flight leaves the job held for staff Retry.

Each tenant's trial end is its own transaction (one bad row never holds the others); rows another run holds are
skipped (``SKIP LOCKED``). ``aa.actor`` = ``job:tenant_account_tick:<job id>`` on every write.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import structlog

from services.tenant_account.reader import IN_SERVICE

logger = structlog.get_logger()

ENDED_TRIALS_SQL = """
    SELECT DISTINCT tenant_id FROM shared.tenant_subscription
     WHERE status = 'trialing' AND trial_ends_at <= now()
     ORDER BY tenant_id
     LIMIT $1
"""
END_TRIALS_SQL = """
    WITH ended AS (
        SELECT subscription_id FROM shared.tenant_subscription
         WHERE tenant_id = $1 AND status = 'trialing' AND trial_ends_at <= now()
         FOR UPDATE SKIP LOCKED
    )
    UPDATE shared.tenant_subscription s
       SET status = CASE WHEN s.paid_through >= current_date THEN 'active' ELSE 'past_due' END, updated_by = $2
      FROM ended
     WHERE s.subscription_id = ended.subscription_id
    RETURNING s.product, s.status
"""
END_ACCOUNT_TRIAL_SQL = """
    UPDATE shared.tenants t
       SET account_status = CASE WHEN c.status = 'active' THEN 'active' ELSE 'past_due' END, updated_at = now()
      FROM shared.tenant_subscription c
     WHERE t.tenant_id = $1 AND t.account_status = 'trial'
       AND c.tenant_id = t.tenant_id AND c.product = 'content' AND c.status IN ('active', 'past_due')
       AND NOT EXISTS (SELECT 1 FROM shared.tenant_subscription s
                        WHERE s.tenant_id = t.tenant_id AND s.status = 'trialing' AND s.trial_ends_at > now())
    RETURNING t.account_status
"""
SYNC_DUE_SQL = "SELECT shared.tenant_entitlement_sync_due()"
PILOT_ENDED_SQL = """
    SELECT tenant_id::text AS tenant_id, pilot_ends_at FROM shared.tenants WHERE is_pilot AND pilot_ends_at <= now()
"""
BATCH = 200


def actor_for(job_id: str) -> str:
    return f"job:tenant_account_tick:{job_id}"[:64]


async def _set_actor(conn, actor: str) -> None:
    await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)


async def end_trials(pool, *, actor: str) -> dict[str, Any]:
    """Step 1 (module docstring). Returns {tenants, subscriptions: {status: n}, accounts: {status: n}, failed}."""
    async with pool.acquire() as conn:
        tenants = [r["tenant_id"] for r in await conn.fetch(ENDED_TRIALS_SQL, BATCH)]
    out: dict[str, Any] = {"tenants": 0, "subscriptions": {}, "accounts": {}, "failed": 0}
    for tenant_id in tenants:
        try:
            async with pool.acquire() as conn, conn.transaction():
                await _set_actor(conn, actor)
                moved = await conn.fetch(END_TRIALS_SQL, tenant_id, actor)
                account = await conn.fetchval(END_ACCOUNT_TRIAL_SQL, tenant_id)
        except Exception as e:  # noqa: BLE001 — one tenant never holds the others; the next tick retries it
            out["failed"] += 1
            logger.warning("tenant_trial_end_failed", tenant_id=str(tenant_id), error=str(e)[:300])
            continue
        if not moved:
            continue
        out["tenants"] += 1
        for r in moved:
            out["subscriptions"][r["status"]] = out["subscriptions"].get(r["status"], 0) + 1
        if account:
            out["accounts"][account] = out["accounts"].get(account, 0) + 1
        logger.info("tenant_trial_ended", tenant_id=str(tenant_id), account_status=account,
                    subscriptions=",".join(f"{r['product']}:{r['status']}" for r in moved))
    return out


async def sync_expired(pool, *, actor: str) -> int:
    """Step 2: the number of entitlement rows the sweep changed."""
    async with pool.acquire() as conn, conn.transaction():
        await _set_actor(conn, actor)
        return int(await conn.fetchval(SYNC_DUE_SQL) or 0)


async def ended_pilots(pool) -> list[dict]:
    """Step 3: the pilot tenants past their end (reported only)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(PILOT_ENDED_SQL)
    for r in rows:
        logger.warning("tenant_pilot_ended", tenant_id=r["tenant_id"], pilot_ends_at=r["pilot_ends_at"].isoformat())
    return [{"tenant_id": r["tenant_id"], "pilot_ends_at": r["pilot_ends_at"].isoformat()} for r in rows]


def allowance_holds_since(now: Optional[datetime] = None) -> datetime:
    """The first instant of the previous UTC month: the oldest AI allowance hold the tick looks at."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first.replace(year=first.year - 1, month=12) if first.month == 1 else first.replace(month=first.month - 1))


# AA-1126 L1 review: the account state the automatic resume reads for the tenants its held jobs name (one query).
SERVING_SQL = """
    SELECT t.tenant_id::text AS tenant_id, t.account_status, t.is_active, t.cancelled_at,
           EXISTS (SELECT 1 FROM shared.tenant_erasure_request e
                    WHERE e.tenant_id = t.tenant_id AND e.phase <> ALL ($2::text[])) AS erasing
      FROM shared.tenants t
     WHERE t.tenant_id::text = ANY($1::text[])
"""
ERASURE_CLOSED_PHASES = ["done", "cancelled", "failed"]  # the phases tenant_erasure_request_open_key leaves out
# The guarded re-queue of one held job: it is still stopped by the same AI allowance hold (a job staff retried or
# cancelled meanwhile is not touched), due within queue.RETRY_SPREAD_SECONDS like the bulk re-queue.
RESUME_ONE_SQL = """
    UPDATE shared.job SET status = 'queued', attempt = 0, run_after = now() + make_interval(secs => $3),
           error = NULL, cancel_requested = false, finished_at = NULL, result = NULL, updated_at = now()
     WHERE id = $1::uuid AND status = 'stopped_budget' AND left(error, length($2)) = $2
    RETURNING id
"""


def resume_refusal(tenant_id: Optional[str], budget: Any, serving: dict) -> Optional[str]:
    """Why the hourly tick must leave an AI-allowance-held job alone, or None to re-queue it. Pure. ``budget`` is the
    tenant's ``TenantAIBudget`` now (None = no budget any more), ``serving`` {tenant_id: SERVING_SQL row}."""
    if tenant_id is None:
        return "the stop names no tenant"
    acct = serving.get(tenant_id)
    if acct is None:
        return "tenant not found"
    if acct["erasing"]:
        return "an erasure request of the tenant is open"
    if acct["account_status"] not in IN_SERVICE or acct["is_active"] is not True or acct["cancelled_at"] is not None:
        return f"the account is not in service ({acct['account_status']})"
    if budget is None:
        return "the tenant has no AI budget any more: staff Retry only"
    if budget.needs_approval:
        return "the tenant still waits for approval"
    return None


async def _serving(pool, tenant_ids: list[str]) -> dict:
    rows = await pool.fetch(SERVING_SQL, tenant_ids, ERASURE_CLOSED_PHASES)
    return {r["tenant_id"]: dict(r) for r in rows}


async def _resume(pool, now: Optional[datetime], rand: Callable[[], float]) -> tuple[int, int]:
    """(re-queued, left held): the held jobs the staff bulk Retry would take (a dry run, one apply page), narrowed by
    :func:`resume_refusal`."""
    from shared.jobs import queue  # lazy: the job queue imports the registry
    from shared.llm_client.tenant_budget import tenant_of_error

    prefix = queue.RETRY_REASON_TENANT_BUDGET
    listed = await queue.retry_by_reason(pool, prefix, since=allowance_holds_since(now), dry_run=True,
                                         limit=queue.RETRY_REASON_APPLY_LIMIT)
    jobs = listed.get("jobs") or []
    if not jobs:
        return 0, int(listed.get("held_count") or 0)
    budgets = await queue.tenants_waiting(pool, jobs)
    serving = await _serving(pool, sorted(budgets))
    requeued, held = 0, int(listed.get("held_count") or 0)
    for job in jobs:
        tid = tenant_of_error(job.get("error"))
        why = resume_refusal(tid, budgets.get(tid), serving)
        if why:
            held += 1
            logger.info("tenant_ai_allowance_hold_kept", job_id=job["id"], tenant_id=tid, reason=why)
            continue
        delay = int(queue.RETRY_SPREAD_SECONDS * min(max(rand(), 0.0), 1.0))
        if await pool.fetchrow(RESUME_ONE_SQL, job["id"], prefix, delay) is not None:
            requeued += 1
    return requeued, held


async def resume_allowance_holds(pool, now: Optional[datetime] = None,
                                 rand: Optional[Callable[[], float]] = None) -> int:
    """AA-1126 L1: re-queue the jobs a tenant's AI budget held once the tenant no longer needs approval and is still in
    service (module docstring, step 4); the number re-queued. Never raises (logged)."""
    try:
        requeued, held = await _resume(pool, now, rand or random.random)
    except Exception as exc:  # noqa: BLE001 — best effort, the tick's other steps go on
        logger.warning("tenant_ai_allowance_resume_failed", error=str(exc)[:300])
        return 0
    if requeued:
        logger.info("tenant_ai_allowance_holds_resumed", requeued=requeued, held=held)
    return requeued


async def tick(pool, *, job_id: str) -> dict[str, Any]:
    actor = actor_for(job_id)
    trials = await end_trials(pool, actor=actor)
    synced = await sync_expired(pool, actor=actor)
    pilots = await ended_pilots(pool)
    resumed = await resume_allowance_holds(pool)
    return {"trials": trials, "expired_entitlements_synced": synced, "pilots_ended": pilots,
            "allowance_holds_resumed": resumed}
