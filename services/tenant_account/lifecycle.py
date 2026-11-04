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

Each tenant's trial end is its own transaction (one bad row never holds the others); rows another run holds are
skipped (``SKIP LOCKED``). ``aa.actor`` = ``job:tenant_account_tick:<job id>`` on every write.
"""
from __future__ import annotations

from typing import Any

import structlog

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


async def tick(pool, *, job_id: str) -> dict[str, Any]:
    actor = actor_for(job_id)
    trials = await end_trials(pool, actor=actor)
    synced = await sync_expired(pool, actor=actor)
    pilots = await ended_pilots(pool)
    return {"trials": trials, "expired_entitlements_synced": synced, "pilots_ended": pilots}
