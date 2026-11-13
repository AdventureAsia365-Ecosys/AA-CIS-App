"""Leave pilot mode (AA-1086 gap G2, root ADR 0019 d16): clear a tenant's pilot marker.

The pilot marker (``shared.tenants.is_pilot`` + ``pilot_ends_at``, CIS 354) is set only by the ``tenant_provision``
saga (``pilot_weeks``, Dev only). Until this module nothing cleared it: offboarding keeps it, the lifecycle tick only
reports a pilot past ``pilot_ends_at``, and the unique partial index ``tenants_one_pilot_key`` keeps Dev's one pilot
slot taken (a second pilot is refused ``pilot_taken``). :func:`end_pilot` is the one tool that clears it, behind the
administrator-only route ``POST /admin/tenants/{tenant_id}/pilot/end`` (``tenant.write``; api/routers/admin.py).

One transaction:

1. lock the tenant row (``FOR UPDATE``): an unknown tenant is ``tenant_not_found``;
2. a tenant that is not the pilot is a no-op (``changed`` false, nothing written): a second call, or a call after the
   saga refused the pilot, answers the same way;
3. ``is_pilot`` false and ``pilot_ends_at`` NULL together (``tenants_pilot_check`` ties them);
4. one ``account.status_changed`` event with ``status`` = ``previous_status`` (the account status does not change), so
   the consumers of ``tenant.entitlements.v1`` re-read the snapshot at their next slot: the CRM's
   ``crm_entitlement_pull`` then clears ``crm.tenant.is_pilot`` (crm-044) and ``crm_pilot_backup`` stops exporting the
   agency, instead of waiting for the hourly reconcile. ``aa.actor`` names the admin (the feed never serves it);
5. one ``acp_shared.audit_log`` row ``tenant.pilot_ended`` with the reason, the end date it had and whether the pilot
   ended early.

Any status is accepted, an offboarded tenant included: the slot must be freeable whatever state the pilot is left in.
The tenant keeps ``kind`` agency; on Dev a non-pilot agency is then offboarded at teardown (root ADR 0019 d16; the
pilot rehearsal runbook, Teardown).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log

REASON_MAX = 500

LOCK_SQL = """
    SELECT tenant_id::text AS tenant_id, name, is_pilot, pilot_ends_at, account_status, suspend_reason
      FROM shared.tenants WHERE tenant_id = $1::uuid FOR UPDATE
"""
END_SQL = """
    UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL, updated_at = now()
     WHERE tenant_id = $1::uuid AND is_pilot
"""
ACTOR_SQL = "SELECT set_config('aa.actor', $1, true)"
# The StatusChange body of tenant.entitlements.v1 (status, previous_status, suspend_reason), status unchanged.
EMIT_SQL = """
    SELECT shared.tenant_account_emit('account.status_changed', $1::uuid, jsonb_build_object(
        'status', $2::text, 'previous_status', $2::text, 'suspend_reason', $3::text))
"""


class PilotError(ValueError):
    """A request :func:`end_pilot` refuses; ``code`` is the stable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(code, message)
        self.code = code
        self.message = message


def clean_reason(reason: Any) -> str:
    """The reason as stored: whitespace-trimmed, 1-500 characters; raises ``reason_required`` / ``reason_too_long``."""
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise PilotError("reason_required", "a reason is required to end a pilot")
    if len(text) > REASON_MAX:
        raise PilotError("reason_too_long", f"the reason is at most {REASON_MAX} characters")
    return text


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


async def end_pilot(conn, tenant_id: str, *, reason: str, actor: str,
                    now: Optional[datetime] = None) -> dict[str, Any]:
    """Clear the pilot marker of ``tenant_id`` (see the module docstring). Returns the outcome; idempotent."""
    text = clean_reason(reason)
    async with conn.transaction():
        row = await conn.fetchrow(LOCK_SQL, tenant_id)
        if row is None:
            raise PilotError("tenant_not_found", "no tenant with this id")
        out: dict[str, Any] = {"tenant_id": row["tenant_id"], "is_pilot": False, "changed": False,
                               "previous_pilot_ends_at": None, "ended_early": None}
        if not row["is_pilot"]:
            return out
        ends_at = row["pilot_ends_at"]
        moment = now or datetime.now(timezone.utc)
        early = ends_at is not None and ends_at > moment
        await conn.execute(END_SQL, tenant_id)
        await conn.execute(ACTOR_SQL, actor[:64])
        await conn.execute(EMIT_SQL, tenant_id, row["account_status"], row["suspend_reason"])
        await write_audit_log(
            conn, tenant_id=row["tenant_id"], actor=actor, action=AdminTenantAuditAction.PILOT_ENDED,
            resource_type="tenant", resource_id=row["tenant_id"],
            details={"reason": text, "name": row["name"], "pilot_ends_at": _iso(ends_at), "ended_early": early,
                     "account_status": row["account_status"]},
        )
    return {**out, "changed": True, "previous_pilot_ends_at": _iso(ends_at), "ended_early": early}
