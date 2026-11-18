"""AA-1126 L2 / L4 (S284 redesign): CIS pulls TripPlanner's AI notice events and writes its own bell rows.

TripPlanner never writes a CIS table besides its ledger rows (CIS 274: the tenant notification policies; CIS 351: the
tripplanner role holds INSERT on shared.llm_call_log and nothing else of schema shared; data standards DM-5.4: lineage
across apps runs through a producer outbox and a contract view). So TripPlanner records two events in its own outbox
(AA-TripPlanner-Web tp-034) and exposes them through the owner view ``tripplanner.v_ai_notices_v1`` (SELECT to cis_app
only; columns id, tenant_id, event, notice_key, payload, created_at; no personal data, a tp-034 CHECK):

* ``ai.allowance_used`` (payload kind, month, resets_at): an agency's monthly AI sessions (plan key tp.ai_sessions) are
  used up. CIS writes the agency's Content studio bell row ``tripplanner.ai_allowance_used`` (target_roles {tenant},
  entity ``tripplanner:<YYYY-MM>``; GET /v1/notifications words it and links Upgrade), once per tenant and month (CIS
  447 index notifications_ai_allowance_once_uq).
* ``ai.spend_degraded`` (payload check, stage, day): under the prod spend rules a TripPlanner spend, AI budget or AI
  sessions read failed and the AI went on without it. CIS writes the admin bell ``platform.tripplanner.spend_degraded``
  (target_roles {admin}, the AA internal tenant, entity ``<check>:<YYYY-MM-DD>``), once per check and UTC day (CIS 447
  index notifications_tp_spend_degraded_once_uq), with the words staff read.

:func:`relay` runs on the job worker's ops tick (shared/jobs/worker.py, every few minutes). It reads the events of the
current and the previous UTC month that have no bell row yet (the notices are monthly and daily, so a relay that was
down across a month boundary still catches up) and writes each with ``ON CONFLICT DO NOTHING``: running it twice, or
on two workers at once, writes each bell row once. Before tp-034 is applied (no view, no grant) it does nothing.
Best effort: :func:`relay_safe` never raises.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import structlog

from shared.cost_guard import _PLATFORM_TENANT_ID
from shared.tenant_ai_notices import TP_ALLOWANCE_EVENT, notify_tenant_allowance_used

logger = structlog.get_logger()

VIEW = "tripplanner.v_ai_notices_v1"
TP_ALLOWANCE = "ai.allowance_used"          # TripPlanner outbox event (tp-034)
TP_DEGRADED = "ai.spend_degraded"           # TripPlanner outbox event (tp-034)
DEGRADED_EVENT = "platform.tripplanner.spend_degraded"
DEGRADED_ENTITY_TYPE = "tripplanner_ai"

# The checks TripPlanner degrades (AA-TripPlanner-Web backend/shared/spend_bell.py CHECKS) and how staff read them.
CHECK_WORDS = {
    "spend_unreadable": "today's AI spend",
    "tenant_budget_unreadable": "an agency's AI budget",
    "monthly_spend_unreadable": "the planning chat's monthly AI spend",
    "tenant_sessions": "an agency's AI sessions quota",
}

# The events of the window that no bell row covers yet ($1 = the window start, $2 = the AA internal tenant).
PENDING_SQL = f"""
    SELECT v.id, v.tenant_id::text AS tenant_id, v.event, v.payload::text AS payload, v.created_at
      FROM {VIEW} v
     WHERE v.created_at >= $1
       AND v.event IN ('{TP_ALLOWANCE}', '{TP_DEGRADED}')
       AND NOT EXISTS (
           SELECT 1 FROM shared.notifications n
            WHERE (v.event = '{TP_ALLOWANCE}' AND n.tenant_id = v.tenant_id
                   AND n.event_type = '{TP_ALLOWANCE_EVENT}'
                   AND n.entity_id = 'tripplanner:' || (v.payload ->> 'month'))
               OR (v.event = '{TP_DEGRADED}' AND n.tenant_id = $2::uuid
                   AND n.event_type = '{DEGRADED_EVENT}'
                   AND n.entity_id = left((v.payload ->> 'check') || ':' || (v.payload ->> 'day'), 100)))
     ORDER BY v.id
"""
DEGRADED_SQL = f"""
    INSERT INTO shared.notifications
        (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', '{DEGRADED_EVENT}', '{DEGRADED_ENTITY_TYPE}', $2, $3::jsonb, ARRAY['admin'])
    ON CONFLICT (tenant_id, event_type, entity_id) WHERE event_type = '{DEGRADED_EVENT}' DO NOTHING
    RETURNING id
"""
# tp-034 not applied yet, or the view not granted: nothing to relay (not an error).
_NOT_READY = (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.InvalidSchemaNameError,
              asyncpg.exceptions.InsufficientPrivilegeError)


def window_start(now: Optional[datetime] = None) -> datetime:
    """The first instant of the previous UTC month (the window the relay reads)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first.replace(year=first.year - 1, month=12) if first.month == 1
            else first.replace(month=first.month - 1))


def degraded_message(check: str, stage: Optional[str]) -> str:
    what = CHECK_WORDS.get(check, check)
    where = f" (stage {stage})" if stage else ""
    return (f"TripPlanner could not read {what}{where}. Under the prod spend rules (the TripPlanner spend environment "
            "setting) the AI went on without this check. Check the database and the TripPlanner logs "
            "(llm_gateway_error).")


def _payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _allowance(conn, row) -> bool:
    payload = _payload(row["payload"])
    try:
        month = datetime.strptime(str(payload.get("month")), "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("tripplanner_notice_skipped", tp_event=row["event"], id=row["id"], reason="bad_month")
        return False
    return await notify_tenant_allowance_used(conn, row["tenant_id"], event=TP_ALLOWANCE_EVENT, kind="tripplanner",
                                              now=month)


async def _degraded(conn, row) -> bool:
    payload = _payload(row["payload"])
    check, day, stage = payload.get("check"), payload.get("day"), payload.get("stage")
    if not check or not day:
        logger.warning("tripplanner_notice_skipped", tp_event=row["event"], id=row["id"], reason="bad_payload")
        return False
    body = {"message": degraded_message(str(check), stage), "check": check, "stage": stage, "day": day,
            "tenant_id": row["tenant_id"], "source": "tripplanner"}
    written = await conn.fetchval(DEGRADED_SQL, _PLATFORM_TENANT_ID, f"{check}:{day}"[:100], json.dumps(body))
    return written is not None


async def relay(pool, now: Optional[datetime] = None) -> dict:
    """Write the bell rows of TripPlanner's pending AI notice events. Returns counts; ``ready`` False when the view
    cannot be read yet (tp-034 not applied or not granted)."""
    out = {"ready": True, "allowance": 0, "degraded": 0, "skipped": 0}
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():                      # a savepoint when the caller holds a transaction
                rows = await conn.fetch(PENDING_SQL, window_start(now), _PLATFORM_TENANT_ID)
        except _NOT_READY:
            return {**out, "ready": False}
        for row in rows:
            allowance = row["event"] == TP_ALLOWANCE
            try:
                async with conn.transaction():                  # one row's failure never stops the others
                    written = await (_allowance(conn, row) if allowance else _degraded(conn, row))
            except Exception as e:  # noqa: BLE001 — counted and logged; the next tick tries the row again
                logger.warning("tripplanner_notice_write_failed", tp_event=row["event"], id=row["id"],
                               error=f"{type(e).__name__}: {str(e)[:200]}")
                written = False
            if written:
                out["allowance" if allowance else "degraded"] += 1
            else:
                out["skipped"] += 1
    return out


async def relay_safe(pool, now: Optional[datetime] = None) -> dict:
    """:func:`relay` that never raises (the worker tick)."""
    try:
        return await relay(pool, now)
    except Exception as e:  # noqa: BLE001 — the next tick tries again
        logger.warning("tripplanner_notice_relay_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return {"ready": False, "error": type(e).__name__}
