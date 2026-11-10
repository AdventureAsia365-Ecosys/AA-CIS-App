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
    # AA-1039 (option A, S275) — tenant-facing: the tour is back on sale and the tenant has WordPress pages the
    # retirement moved to draft; the bell lists them with "Put back online" (written by the restore fan-out).
    TOUR_PAGES_TO_PUT_BACK = "tour.pages_to_put_back"
    MASTER_ACTIVATED = "tour.master.activated"
    MASTER_DEACTIVATED = "tour.master.deactivated"
    MASTER_TRASHED = "tour.master.trashed"
    MASTER_RESTORED = "tour.master.restored"
    # AA-819 follow-up (S237) — a tenant saved a CTA that claims reviews or ratings. Admin-only:
    # the tenant sees a warning in the portal, never this row.
    TENANT_CTA_REVIEW_CLAIM = "tenant.cta.review_claim"
    # AA-1036 R3 (root ADR 0019 d7) — a tenant asked for an add-on (CRM, Trip planner) from the agency app. Staff-only:
    # admin and the tenant's customer_success manager (the bell's role scope narrows it to the manager's book).
    TENANT_ADDON_REQUESTED = "tenant.addon.requested"
    # AA-1037 R4 (root ADR 0019 d9) — the hourly tenant_billing_tick: an invoice from AA is overdue (the account is in
    # its grace state), the account was suspended for billing, or service came back after payment. Tenant-facing (the
    # agency's bell) and staff (admin, finance).
    TENANT_BILLING_OVERDUE = "tenant.billing.overdue"
    TENANT_BILLING_SUSPENDED = "tenant.billing.suspended"
    TENANT_BILLING_RESTORED = "tenant.billing.restored"


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
    EventType.TOUR_PAGES_TO_PUT_BACK: ["tenant"],  # AA-1039 — tenant-facing
    EventType.MASTER_ACTIVATED:    ["admin"],
    EventType.MASTER_DEACTIVATED:  ["admin"],
    EventType.MASTER_TRASHED:      ["admin", "content"],
    EventType.MASTER_RESTORED:     ["admin"],
    EventType.TENANT_CTA_REVIEW_CLAIM: ["admin"],
    EventType.TENANT_ADDON_REQUESTED: ["admin", "customer_success"],
    EventType.TENANT_BILLING_OVERDUE: ["tenant", "admin", "finance"],
    EventType.TENANT_BILLING_SUSPENDED: ["tenant", "admin", "finance"],
    EventType.TENANT_BILLING_RESTORED: ["tenant", "admin", "finance"],
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
