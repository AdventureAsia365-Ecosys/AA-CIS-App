"""AA-1126 l16 (S283): the tenant-facing AI allowance notices and the "budget unreadable" admin bell (moved out of
shared/cost_guard.py, which keeps the budget rules).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from shared.cost_guard import _INSERT_ALERT_SQL, _PLATFORM_TENANT_ID, _RECENT_ALERT_SQL, BUDGET_ALERT_EVENT

logger = structlog.get_logger()


# L1: when a tenant's AI budget stops its Content studio writing, the agency is told in its own bell (a
# shared.notifications row for target_roles {tenant}, GET /v1/notifications), once per tenant and UTC month: the CIS
# 447 partial unique index notifications_ai_allowance_once_uq on (tenant_id, event_type, entity_id) makes a second
# write a no-op. The TripPlanner notice (event tripplanner.ai_allowance_used, kind tripplanner) is written here too, by
# CIS: TripPlanner records an ai.allowance_used event in its own outbox when an agency's AI sessions quota stops new AI
# sessions (AA-TripPlanner-Web tp-034, backend/shared/tenant_sessions.py) and services/tripplanner_notices.py relays
# it from the contract view tripplanner.v_ai_notices_v1 (TripPlanner holds no grant on shared.notifications: CIS 274 /
# 351).
# L4: with the prod spend rules (cis.spend.environment) a budget that cannot be read lets the call go on; staff get
# one bell per tenant per 24 h.

TENANT_ALLOWANCE_EVENT = "ai.allowance_used"
TP_ALLOWANCE_EVENT = "tripplanner.ai_allowance_used"
ALLOWANCE_ENTITY_TYPE = "ai_allowance"
TENANT_ALLOWANCE_NOTICE_SQL = """
    INSERT INTO shared.notifications
        (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, $3, $4, $5::jsonb, ARRAY['tenant'])
    ON CONFLICT (tenant_id, event_type, entity_id)
        WHERE event_type IN ('ai.allowance_used', 'tripplanner.ai_allowance_used') DO NOTHING
    RETURNING id
"""
TENANT_BUDGET_UNREADABLE_EVENT = BUDGET_ALERT_EVENT


def allowance_entity(kind: str, now: Optional[datetime] = None) -> str:
    """The notice's entity_id: '<kind>:<YYYY-MM>' (UTC), so a kind is noticed once per tenant and month."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{kind}:{now.strftime('%Y-%m')}"


async def notify_tenant_allowance_used(conn, tenant_id: str, *, event: str = TENANT_ALLOWANCE_EVENT,
                                       kind: str = "content", now: Optional[datetime] = None) -> bool:
    """The tenant-facing notice that its plan's AI allowance for this month is used up (``kind`` content: the Content
    studio's writing; tripplanner: new planner AI sessions). True when written now, False when it was already
    written this month or the write failed (best effort: never raises; before CIS 447 the ON CONFLICT has no index to
    use and fails, logged)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    resets = (now.replace(day=1) + timedelta(days=32)).replace(day=1).date()
    payload = {"kind": kind, "month": now.strftime("%Y-%m"), "resets_at": resets.isoformat()}
    try:
        written = await conn.fetchval(TENANT_ALLOWANCE_NOTICE_SQL, str(tenant_id), event, ALLOWANCE_ENTITY_TYPE,
                                      allowance_entity(kind, now), json.dumps(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning("tenant_ai_allowance_notice_failed", tenant_id=str(tenant_id), event_type=event,
                       error=str(exc)[:300])
        return False
    if written is not None:
        logger.info("tenant_ai_allowance_notice", tenant_id=str(tenant_id), event_type=event, kind=kind)
    return written is not None


async def maybe_alert_tenant_unreadable(conn, tenant_id: str) -> bool:
    """AA-1126 L4: one admin bell that ``tenant_id`` has an AI budget that could not be read, and that its AI calls go
    on without the check (prod spend rules); at most one unread bell per tenant per 24 h. Best effort: never raises."""
    entity = f"tenant_ai_budget_unreadable:{tenant_id}"[:100]
    try:
        if await conn.fetchval(_RECENT_ALERT_SQL, TENANT_BUDGET_UNREADABLE_EVENT, entity):
            return False
        message = (f"Tenant {tenant_id}: its AI budget could not be read. Under the prod spend rules "
                   "(cis.spend.environment) its AI calls go on without the budget check until the budget can be "
                   "read again.")
        await conn.execute(_INSERT_ALERT_SQL, _PLATFORM_TENANT_ID, TENANT_BUDGET_UNREADABLE_EVENT, entity,
                           json.dumps({"message": message, "provider": "tenant_ai", "tenant_id": str(tenant_id),
                                       "scope": f"tenant:{tenant_id}", "state": "unreadable"}))
        logger.warning("tenant_ai_budget_unreadable_alert", tenant_id=str(tenant_id))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("tenant_ai_budget_unreadable_alert_failed", tenant_id=str(tenant_id), error=str(exc)[:300])
        return False
