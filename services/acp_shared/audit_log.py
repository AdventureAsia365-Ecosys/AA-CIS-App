"""
services.acp_shared.audit_log — shared writer for `acp_shared.audit_log` (migration 030;
`actor_type` column from migration 021).

AA-559 STEP0 finding: this table already has 5 real call sites before this issue touches it —
`agency.onboard`/`agency.offboard` (api/routers/admin.py), `hitl.gate3_social.{status}`
(api/routers/v1_social.py), `publish_mode_transition` (services/acp_produce/trust_ramp.py), and
`trip_reallocation_suggestion` (services/acp_planning/trip_reallocation.py) — NOT just the 2
(`trip_reallocation_suggestion`/`agency.onboard`) the issue's own "Bối cảnh" names; the AA-558
investigation this issue is based on apparently didn't grep exhaustively. All 5 are raw inline
`INSERT INTO acp_shared.audit_log ...` statements — no shared helper existed before this file.
Kept those 5 call sites completely untouched (per this issue's explicit "tránh phá vỡ 2 use case
hiện tại" instruction, generalized to all 5 found) — this module is used ONLY by the 6 new
call sites AA-559 adds. (AA-797 later moved the two admin.py rows onto this writer, with the
real admin as actor; see AdminTenantAuditAction.)

`actor_type` (acp_shared.audit_actor_type enum: hitl_reviewer/tenant_admin/tenant_reviewer,
migration 021) is deliberately left NULL by every writer this module has — none of those 3
enum values describe an ordinary tenant self-service action (they all name WHO reviews/approves
HITL content, not "a tenant did something on their own account"), and the one pre-existing
tenant-self-service audit row (trip_reallocation.py's confirm_trip_reallocation(), actor=
f"tenant:{tenant_id}") already leaves it NULL rather than force-fit one of the 3 values. Adding
a 4th enum value needs its own `ALTER TYPE ... ADD VALUE` migration + product sign-off on what
it should be called — out of scope here; NULL is the honest, correct value today.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


class TenantAuditAction:
    """`action` string constants for the 6 tenant-activity audit_log rows AA-559 adds. NOT an
    exhaustive registry of every audit_log action in this codebase — the 5 pre-existing call
    sites (see this module's docstring) keep their own inline action-string literals, untouched.
    AA-557 mục 21's future Activity-tab UI should import these rather than re-typing the strings."""
    TENANT_LOGIN = "tenant.login"
    SLATE_SUBJECT_PICKED = "slate.subject_picked"
    WRITE_STARTED = "write.started"
    CONTENT_PIECE_CREATED = "content_piece.created"
    CONTENT_PIECE_FINISHED = "content_piece.finished"
    TOUR_REWRITE_TRIGGERED = "tour.rewrite_triggered"
    CONTENT_PIECE_EDITED = "content_piece.edited"  # AA-569 — tenant hand-edit on My Content
    CONTENT_EXPORTED = "content_piece.exported"  # AA-613 — tenant downloaded/exported a piece
    CONTENT_PUBLISHED = "content_piece.published"  # AA-613 — tenant published a piece to a channel
    # AA-802 — tenant downloaded a T2 tour version as DOCX (the admin "tenant has an older copy" check)
    TOUR_VERSION_EXPORTED = "tenant_tour_version.exported"
    CONTENT_UNPUBLISHED = "content_piece.unpublished"  # AA-815 — tenant removed a published post
    INTEGRATION_DISCONNECTED = "integration.disconnected"  # AA-815 — tenant removed a channel connection


class PlatformAuditAction:
    """`action` strings for admin changes to platform data (no tenant: tenant_id is NULL)."""
    PLACE_CARD_STATUS = "place_card.status"  # AA-772 — admin verified / rejected / reset a card
    PLACE_CARD_MERGE = "place_card.merge"  # AA-772 (S234) — admin merged duplicate cards
    # AA-774 (S235) — admin re-kinded / re-worded / dropped card facts; details hold before/after per fact
    PLACE_CARD_FACTS_EDITED = "place_card.facts_edited"


class AdminAuditAction:
    """AA-769 (S234) — an AA admin changed a tenant's T9 piece from the Content Quality drawer.
    Admin-only rows (tenants never read audit_log); `details` holds before/after and the offsets,
    so each edit can be reviewed or reverted by hand. Shared prefix: ADMIN_SENTENCE_PREFIX."""
    ADMIN_SENTENCE_PREFIX = "content_piece.admin_sentence_"
    SENTENCE_CUT = "content_piece.admin_sentence_cut"
    SENTENCE_REWRITTEN = "content_piece.admin_sentence_rewritten"  # the model's suggestion, unchanged
    SENTENCE_EDITED = "content_piece.admin_sentence_edited"  # the admin's own (or edited) text
    # AA-802 — the same sentence actions on a tenant's T2 tour version (silent correction: the
    # tenant's unpublished copy is fixed in place; details keep field, before/after and the whole
    # section before/after, which is the version history — the tenant never sees these rows).
    TOUR_SENTENCE_PREFIX = "tenant_tour_version.admin_sentence_"
    TOUR_SENTENCE_CUT = "tenant_tour_version.admin_sentence_cut"
    TOUR_SENTENCE_REWRITTEN = "tenant_tour_version.admin_sentence_rewritten"
    TOUR_SENTENCE_EDITED = "tenant_tour_version.admin_sentence_edited"
    # S234 — an admin re-ran T8 for a request (new 3 angles, choice reset); details keep the old ones.
    ANGLES_REGENERATED = "angle_gate_request.admin_angles_regenerated"


class AdminTenantAuditAction:
    """AA-797 (S235) — every admin write to a tenant account (api/routers/admin.py tenant routes).
    Actor is `admin:{x-admin-user-id}`; `details` holds before/after where the write changes a
    value. `agency.onboard` / `agency.offboard` keep the action strings their rows already use."""
    ONBOARD = "agency.onboard"
    OFFBOARD = "agency.offboard"
    STATUS_CHANGED = "tenant.status_changed"  # suspend <-> active (PATCH is_active)
    PLAN_CHANGED = "tenant.plan_changed"  # plan_tier + plan_id + rate_limit_rpm
    RATE_LIMIT_CHANGED = "tenant.rate_limit_changed"  # AA-766: RPM override without a plan change
    DAILY_PUBLISH_LIMIT_CHANGED = "tenant.daily_publish_limit_changed"  # AA-819: publish/day override (NULL = default)
    KEY_ROTATED = "tenant.key_rotated"
    BRAND_UPDATED = "tenant.brand_updated"
    CONFIG_UPDATED = "tenant.config_updated"  # markets / channels / posts_per_week
    PLAN_ID_BACKFILL = "tenant.plan_id_backfill"  # S234 one-off data fix (script, not a route)
    RESTORED = "tenant.restored"  # offboarded -> active, new key (Nghiệp 14/10/2026)


async def write_audit_log(
    conn, *, tenant_id: Optional[str], actor: str, action: str,
    resource_type: str, resource_id: str, details: Optional[dict[str, Any]] = None,
    actor_type: Optional[str] = None, http_method: Optional[str] = None, route: Optional[str] = None,
    status: Optional[int] = None,
) -> None:
    """INSERT one `acp_shared.audit_log` row. `tenant_id=None` (platform data, e.g. place cards)
    is stored as NULL.

    `conn` may be an asyncpg Connection OR a Pool — both expose a compatible `.execute()`
    (`Pool.execute()` acquires a connection internally), so a caller already holding a
    Connection inside its own `async with pool.acquire()` block reuses it (keeping the audit
    row in the same implicit unit of work as the real state change it's logging, matching the
    5 pre-existing call sites' own "write the log next to the thing it logs" shape); a caller
    with only a `Pool` in scope passes that directly.

    Deliberately NOT best-effort/swallowed here (unlike services/acp_shared/hitl_events.py's
    EventBridge publish, which catches and returns False) — a lost audit row is a silent,
    permanent gap in the Activity feed AA-557 mục 21 is waiting on, with no retry path; a lost
    EventBridge event at least has its own separate failure signal. A caller that must not let
    an audit-log failure break its primary action (this issue's tenant-login call site is the
    one case — see api/routers/auth.py) wraps this call in its own try/except instead of relying
    on this function to swallow errors silently for every caller.

    AA-884 (CIS 248): `actor_type` ('staff' / 'machine' for the request audit; the 021 values stay unused here),
    `http_method`, `route` and `status` describe a staff HTTP request (api/middleware/staff_audit.py); every other
    caller leaves them NULL. The chain columns are set by the table's BEFORE INSERT trigger, never by a caller; the
    trigger holds a transaction-scoped lock, so a caller inside a transaction writes its audit row last (QRY-4).
    """
    if actor_type is None and http_method is None and route is None and status is None:
        await conn.execute(
            """
            INSERT INTO acp_shared.audit_log
                (tenant_id, actor, action, resource_type, resource_id, details)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            str(tenant_id) if tenant_id is not None else None, actor, action, resource_type, str(resource_id),
            json.dumps(details or {}),
        )
        return
    await conn.execute(
        """
        INSERT INTO acp_shared.audit_log
            (tenant_id, actor, action, resource_type, resource_id, details,
             actor_type, http_method, route, status)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::acp_shared.audit_actor_type, $8, $9, $10)
        """,
        str(tenant_id) if tenant_id is not None else None, actor, action, resource_type, str(resource_id),
        json.dumps(details or {}), actor_type, http_method, route, status,
    )
