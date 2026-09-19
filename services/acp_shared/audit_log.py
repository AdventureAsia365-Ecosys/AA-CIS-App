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
call sites AA-559 adds.

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


async def write_audit_log(
    conn, *, tenant_id: str, actor: str, action: str,
    resource_type: str, resource_id: str, details: Optional[dict[str, Any]] = None,
) -> None:
    """INSERT one `acp_shared.audit_log` row.

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
    """
    await conn.execute(
        """
        INSERT INTO acp_shared.audit_log
            (tenant_id, actor, action, resource_type, resource_id, details)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        str(tenant_id), actor, action, resource_type, str(resource_id),
        json.dumps(details or {}),
    )
