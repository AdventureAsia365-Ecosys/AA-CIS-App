"""AA-1116 (S282 audit gaps G5 / G11) — a tenant's CRM agency and its first-owner invitation, with no hand step.

Before this module a new tenant with a ``crm`` add-on still needed two hand steps after the ``tenant_provision`` saga:
Platform › Agencies "Create agency" and "Invite first owner" (the pilot rehearsal runbook, steps 1 and 2). The saga
cannot make those calls itself: the CRM's ``crm.ops.v1`` writes take only a signed-in staff member's WORKFORCE token
(group ``crm-platform-admin``), which a background job does not have, and CIS keeps no M2M client towards the CRM.
So the provisioning routes (``api/routers/admin_tenant_provision.py``) run :func:`onboard` in the same staff request
that starts the saga, with that staff member's own token (``platform_apps.relay_authorization``), and
``POST /admin/tenants/{tenant_id}/crm-onboarding`` runs it again (Tenant 360, after the CRM was unavailable).

:func:`onboard`, idempotent (every step reads "already done" as done):

1. the tenant exists, is not offboarding / closed, and holds a LIVE ``crm`` subscription (trialing, active,
   past_due): else ``tenant_not_found`` (404) / ``tenant_offboarded`` / ``no_crm_subscription`` (409), no CRM call;
2. ``GET /tenants/{id}`` on the CRM: 200 = the agency exists (``crm_agency_exists``); 404 = ``POST /tenants`` with the
   CIS tenant id, its trading name (at most 200 characters, the CRM's limit) and ``is_synthetic`` = Dev (the CRM takes
   synthetic agencies only on Dev and real ones only elsewhere) -> ``crm_agency_created``; 409 ``tenant_exists`` on a
   race = exists. ``tenant_deleted`` (a trashed test agency: the id is never reused) stops here;
3. ``POST /tenants/{id}/owner-invite`` with the owner address staff typed for THIS tenant (the only address the call
   can carry; the CRM checks the agency has no owner and no travellers) -> ``crm_owner_invited``; 409
   ``owner_invite_pending`` -> ``crm_owner_invite_pending`` and ``owner_exists`` -> ``crm_owner_present`` (both done:
   a second run never sends a second mail). Without an address the step is ``crm_owner_not_invited``;
4. when step 2 created the agency: one ``account.status_changed`` event with the status unchanged (as
   ``pilot.end_pilot``), so ``crm_entitlement_pull`` projects plan, seats and features at its next slot instead of the
   hourly reconcile (the saga's own events may have reached the CRM before the agency existed: "unknown agency");
   Once the agency exists (step 2 done), CIS records it in ``shared.tenant_crm_agency`` (AA-1160, CIS 474:
   ``crm_agency.record_quietly``, source ``crm_onboarding``), so the tenant's TripPlanner leads may go to the CRM;
5. one ``acp_shared.audit_log`` row ``tenant.crm_onboarded`` when anything changed (steps, ticket, CRM status codes;
   the owner address only as ``owner_invite``: sent / pending / owner_present / not_sent, never in clear or hashed:
   the audit chain is kept after an erasure).

Every CRM refusal is returned (``status`` partial / failed with ``error`` {step, http_status, code}), never raised: the
saga's own result does not depend on the CRM. The address is never logged, stored or returned.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

import structlog

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.tenant_account import crm_agency, dev_guard

logger = structlog.get_logger()

CRM_ONBOARDED = AdminTenantAuditAction.CRM_ONBOARDED
AGENCY_NAME_MAX = 200     # technical-constant: crm.ops.v1 TenantCreate.agency_name max_length
EMAIL_MAX = 254           # technical-constant: RFC 5321 path limit, the CRM's OwnerInviteIn max_length
LIVE = ("trialing", "active", "past_due")
TICKET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,10}$")   # crm.ops.v1 TICKET_PATTERN
DEFAULT_TICKET_PREFIX = "CISONB"

TENANT_SQL = """
    SELECT t.tenant_id::text AS tenant_id, t.name, t.account_status, t.suspend_reason, s.status AS crm_status
      FROM shared.tenants t
      LEFT JOIN shared.tenant_subscription s ON s.tenant_id = t.tenant_id AND s.product = 'crm'
     WHERE t.tenant_id = $1::uuid
"""
ACTOR_SQL = "SELECT set_config('aa.actor', $1, true)"
# The StatusChange body of tenant.entitlements.v1 with the status unchanged (services/tenant_account/pilot.py).
EMIT_SQL = """
    SELECT shared.tenant_account_emit('account.status_changed', $1::uuid, jsonb_build_object(
        'status', $2::text, 'previous_status', $2::text, 'suspend_reason', $3::text))
"""

CrmCall = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]


class OnboardingError(ValueError):
    """A request refused before any CRM call; ``status`` is the HTTP status the route answers."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(code, message, status)
        self.code = code
        self.message = message
        self.status = status


def clean_email(value: Any) -> str:
    """The owner address as sent: trimmed, lower-case, one ``@`` with a dotted domain, no space, at most 254
    characters. Raises ``invalid_owner_email`` (the address is never part of the message)."""
    text = value.strip().lower() if isinstance(value, str) else ""
    local, _, domain = text.partition("@")
    if (not 3 <= len(text) <= EMAIL_MAX or not local or "@" in domain or "." not in domain.strip(".")
            or any(c.isspace() for c in text)):
        raise OnboardingError("invalid_owner_email", "Enter the owner's e-mail address (name@agency-domain).", 422)
    return text


def ticket_ref(value: Optional[str] = None, *, now: Optional[float] = None) -> str:
    """The CRM ticket reference of the calls: the one staff gave (crm.ops.v1 pattern), else ``CISONB-<unix time>``."""
    if value is not None:
        if not isinstance(value, str) or not TICKET_RE.match(value):
            raise OnboardingError("invalid_ticket_ref", "A ticket reference looks like AA-1116.", 422)
        return value
    return f"{DEFAULT_TICKET_PREFIX}-{int(now if now is not None else time.time())}"


def _code(body: dict[str, Any]) -> str:
    code = body.get("detail") if isinstance(body, dict) else None
    return code if isinstance(code, str) else "crm_error"


def _outcome(tenant_id: str, steps: list[str], error: Optional[dict] = None) -> dict[str, Any]:
    agency = any(s in ("crm_agency_created", "crm_agency_exists") for s in steps)
    status = "done" if agency and error is None else ("partial" if agency else "failed")
    return {"tenant_id": tenant_id, "status": status, "steps": steps, "error": error}


async def _check(pool, tid: str) -> Any:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(TENANT_SQL, tid)
    if row is None:
        raise OnboardingError("tenant_not_found", "No tenant with this id.", 404)
    if row["account_status"] in ("offboarding", "closed"):
        raise OnboardingError("tenant_offboarded", "Restore the tenant first.")
    if row["crm_status"] not in LIVE:
        raise OnboardingError("no_crm_subscription", "This tenant has no live CRM add-on.")
    return row


async def _ensure_agency(call: CrmCall, row, authorization: Optional[str], ref: str, steps: list[str],
                         codes: dict[str, int]) -> tuple[bool, Optional[dict]]:
    """Step 2: (created, error). The agency exists afterwards when error is None."""
    tid = row["tenant_id"]
    status, body = await call("GET", f"/tenants/{tid}", authorization)
    codes["agency_read"] = status
    if status == 200:
        steps.append("crm_agency_exists")
        return False, None
    if status != 404:
        return False, {"step": "agency_read", "http_status": status, "code": _code(body)}
    status, body = await call("POST", "/tenants", authorization, json={
        "tenant_id": tid, "agency_name": str(row["name"]).strip()[:AGENCY_NAME_MAX] or tid,
        "is_synthetic": dev_guard.on_dev(), "ticket_ref": ref})
    codes["agency_create"] = status
    if status == 201:
        steps.append("crm_agency_created")
        return True, None
    if status == 409 and _code(body) == "tenant_exists":
        steps.append("crm_agency_exists")
        return False, None
    return False, {"step": "agency_create", "http_status": status, "code": _code(body)}


_INVITE_DONE = {"owner_invite_pending": "crm_owner_invite_pending", "owner_exists": "crm_owner_present"}


async def _invite(call: CrmCall, tid: str, address: Optional[str], authorization: Optional[str], ref: str,
                  steps: list[str], codes: dict[str, int]) -> Optional[dict]:
    """Step 3: the error, or None (invited, already pending, owner present, or no address given)."""
    if address is None:
        steps.append("crm_owner_not_invited")
        return None
    status, body = await call("POST", f"/tenants/{tid}/owner-invite", authorization,
                              json={"owner_email": address, "ticket_ref": ref})
    codes["owner_invite"] = status
    code = _code(body)
    if status == 202:
        steps.append("crm_owner_invited")
    elif status == 409 and code in _INVITE_DONE:
        steps.append(_INVITE_DONE[code])
    else:
        return {"step": "owner_invite", "http_status": status, "code": code}
    return None


async def onboard(pool, tenant_id: str, *, owner_email: Optional[str], authorization: Optional[str], actor: str,
                  ticket: Optional[str] = None, request_key: Optional[str] = None,
                  call: Optional[CrmCall] = None) -> dict[str, Any]:
    """Create the tenant's CRM agency when it has none and invite its first owner (module docstring)."""
    if call is None:
        from api.services.crm_console import crm_ops_call as call    # the relay (allow-listed L1 routes)
    try:
        tid = str(uuid.UUID(str(tenant_id)))
    except ValueError:
        raise OnboardingError("tenant_not_found", "No tenant with this id.", 404) from None
    address = clean_email(owner_email) if owner_email is not None else None
    ref = ticket_ref(ticket)
    row = await _check(pool, tid)
    steps: list[str] = []
    codes: dict[str, int] = {}
    created, error = await _ensure_agency(call, row, authorization, ref, steps, codes)
    if error is None:
        # AA-1160: the agency exists now; TripPlanner leads may go to the CRM (shared.tenant_crm_agency, CIS 474).
        await crm_agency.record_quietly(pool, tid, "crm_onboarding")
        error = await _invite(call, tid, address, authorization, ref, steps, codes)
    return await _finish(pool, row, steps, codes, created, ref, address, actor, request_key, error)


_INVITE_STATE = {"crm_owner_invited": "sent", "crm_owner_invite_pending": "pending",
                 "crm_owner_present": "owner_present"}


async def _finish(pool, row, steps: list[str], codes: dict[str, int], created: bool, ref: str,
                  address: Optional[str], actor: str, request_key: Optional[str],
                  error: Optional[dict]) -> dict[str, Any]:
    tid = row["tenant_id"]
    out = _outcome(tid, steps, error)
    invite = next((_INVITE_STATE[s] for s in steps if s in _INVITE_STATE), "not_sent")
    logger.info("crm_onboarding", tenant_id=tid, status=out["status"], steps=",".join(steps),
                error_code=(error or {}).get("code"), owner_invite=invite)
    if created or invite == "sent":
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(ACTOR_SQL, actor[:64])
            if created:
                await conn.execute(EMIT_SQL, tid, row["account_status"], row["suspend_reason"])
            await write_audit_log(
                conn, tenant_id=tid, actor=actor, action=CRM_ONBOARDED, resource_type="tenant", resource_id=tid,
                details={"steps": steps, "status": out["status"], "ticket_ref": ref, "owner_invite": invite,
                         "owner_email_given": address is not None, "crm_status_codes": codes, "error": error,
                         "provision_request_key": request_key})
    return out
