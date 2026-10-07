from __future__ import annotations
import json
import logging
from enum import Enum
from typing import Optional
import asyncpg

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    PIPELINE_COMPLETED = "tour.pipeline.completed"
    PIPELINE_FAILED = "tour.pipeline.failed"
    BRAND_AUDIT_FLAGGED = "tour.brand_audit.flagged"
    BRAND_AUDIT_FIXED = "tour.brand_audit.fixed"
    DEDUP_STAGED = "tour.dedup.staged"
    DEDUP_PROMOTED = "tour.dedup.promoted"
    SOURCE_TRASHED = "tour.source.trashed"
    SOURCE_RESTORED = "tour.source.restored"
    # AA-716 — tenant-facing: a source tour the tenant adopted is no longer supplied (its Master
    # went inactive), or has been reinstated. Distinct from SOURCE_TRASHED/RESTORED, which are the
    # admin/content-facing events for the same action.
    TOUR_DISCONTINUED = "tour.discontinued"
    TOUR_REINSTATED = "tour.reinstated"
    MASTER_ACTIVATED = "tour.master.activated"
    MASTER_DEACTIVATED = "tour.master.deactivated"
    MASTER_TRASHED = "tour.master.trashed"
    MASTER_RESTORED = "tour.master.restored"


_DEFAULT_ROLES: dict[EventType, list[str]] = {
    EventType.PIPELINE_COMPLETED:  ["admin", "content"],
    EventType.PIPELINE_FAILED:     ["admin", "content"],
    EventType.BRAND_AUDIT_FLAGGED: ["admin", "content"],
    EventType.BRAND_AUDIT_FIXED:   ["admin", "content"],
    EventType.DEDUP_STAGED:        ["admin", "content"],
    EventType.DEDUP_PROMOTED:      ["admin"],
    EventType.SOURCE_TRASHED:      ["admin", "content"],
    EventType.SOURCE_RESTORED:     ["admin"],
    EventType.TOUR_DISCONTINUED:   ["tenant"],   # AA-716 — tenant-facing
    EventType.TOUR_REINSTATED:     ["tenant"],   # AA-716 — tenant-facing
    EventType.MASTER_ACTIVATED:    ["admin"],
    EventType.MASTER_DEACTIVATED:  ["admin"],
    EventType.MASTER_TRASHED:      ["admin", "content"],
    EventType.MASTER_RESTORED:     ["admin"],
}


class NotificationService:
    """
    Emit lifecycle notifications within the caller's DB transaction.
    NEVER acquires own connection. NEVER starts own transaction.
    Caller owns conn + transaction — emit() only does the INSERT.
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def emit(
        self,
        event_type: EventType | str,
        entity_type: str,
        entity_id: str,
        tenant_id: str,
        payload: dict,
        actor_type: str = "system",
        target_roles: Optional[list[str]] = None,
    ) -> None:
        if isinstance(event_type, str):
            event_type = EventType(event_type)
        roles = target_roles or _DEFAULT_ROLES.get(event_type, ["admin"])
        await self._conn.execute(
            """
            INSERT INTO shared.notifications
                (tenant_id, actor_type, event_type,
                 entity_type, entity_id, payload, target_roles)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7)
            """,
            tenant_id, actor_type, event_type.value,
            entity_type, str(entity_id), json.dumps(payload), roles,
        )
        logger.info(
            "notif.emit event=%s entity=%s/%s tenant=%s",
            event_type.value, entity_type, entity_id, tenant_id,
        )
