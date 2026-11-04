"""Add-on requests (AA-1036 R3, root ADR 0019 d7; clarifications TL-7 step 4): an agency asks for an add-on (CRM, Trip
planner) from the agency app, AA staff approve or decline it in Tenant 360 › Subscription. Table
``shared.tenant_addon_request`` (CIS 374).

- :func:`tenant_view`: what the agency app shows per add-on: ``active`` (a live subscription of the product: trialing
  with a trial end ahead, active or past due), ``requested`` (a pending request), else ``available``; plus the last
  decided request, so a decline shows its reason.
- :func:`create_request`: the Content studio owner's "Request add-on" (the route is owner-only through the tenant role
  middleware, ``/v1/account`` writes). Refused when the account is not in service (409 ``tenant_not_in_service``) or the
  add-on is already live (409 ``already_subscribed``); a second request while one is pending answers that one
  (``created`` false, no new notification); at most :data:`DAILY_CAP` requests per tenant in 24 hours (429). It writes
  the row, the audit row (``tenant.addon_requested``) and the staff bell row (``tenant.addon.requested``, admin and the
  tenant's customer_success manager) in one transaction.
- :func:`approve`: the admin's decision. The plan (an unretired plan of the product) is attached through the existing
  control-plane path, the ``tenant_provision`` saga in attach mode (``services/tenant_account/provision.py``), enqueued
  in the SAME transaction that marks the request approved, with request key ``addon-request:<request id>``: one job per
  request, and no approved request without its job (or the reverse). The saga writes the subscription, the
  entitlements and the ``tenant.subscription_changed`` audit rows; this module adds ``tenant.addon_request_approved``.
- :func:`decline`: the admin's decision with a reason (shown to the tenant), audited ``tenant.addon_request_declined``.

A decision is set once (``status = 'pending'`` in the UPDATE; the CIS 374 CHECKs hold the shape). Errors are
:class:`~services.tenant_account.plans.PlanError` (stable code, HTTP status). The tenant never sees the staff actor.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, TenantAuditAction, write_audit_log
from services.notifications import EventType, NotificationService
from services.tenant_account import provision as pv
from services.tenant_account.plans import PlanError

PRODUCTS = ("crm", "tripplanner")
PRODUCT_LABEL = {"crm": "CRM", "tripplanner": "Trip planner"}
NOTE_MAX = 500
DAILY_CAP = 10
IN_SERVICE = ("trial", "active", "past_due")
REQUEST_KEY_PREFIX = "addon-request:"
APPROVED = AdminTenantAuditAction.ADDON_REQUEST_APPROVED
DECLINED = AdminTenantAuditAction.ADDON_REQUEST_DECLINED

TENANT_SQL = "SELECT name, slug, account_status FROM shared.tenants WHERE tenant_id = $1::uuid"
LIVE_SQL = """
    SELECT product, status FROM shared.tenant_subscription
     WHERE tenant_id = $1::uuid AND product = ANY($2::text[])
       AND (status IN ('active', 'past_due') OR (status = 'trialing' AND trial_ends_at > now()))
"""
REQUESTS_SQL = """
    SELECT request_id::text AS request_id, product, status, note, requested_by, requested_at, decided_at, decided_by,
           decision_note, plan_key, provision_job_id::text AS provision_job_id
      FROM shared.tenant_addon_request WHERE tenant_id = $1::uuid
     ORDER BY requested_at DESC LIMIT $2
"""
PENDING_SQL = """
    SELECT request_id::text AS request_id, product, status, note, requested_by, requested_at, decided_at, decided_by,
           decision_note, plan_key, provision_job_id::text AS provision_job_id
      FROM shared.tenant_addon_request WHERE tenant_id = $1::uuid AND product = $2 AND status = 'pending'
"""
RECENT_SQL = """
    SELECT count(*) FROM shared.tenant_addon_request
     WHERE tenant_id = $1::uuid AND requested_at > now() - interval '24 hours'
"""
INSERT_SQL = """
    INSERT INTO shared.tenant_addon_request (tenant_id, product, note, requested_by)
    VALUES ($1::uuid, $2, $3, $4)
    RETURNING request_id::text AS request_id, product, status, note, requested_by, requested_at, decided_at, decided_by,
              decision_note, plan_key, provision_job_id::text AS provision_job_id
"""
LOCK_REQUEST_SQL = """
    SELECT request_id::text AS request_id, product, status FROM shared.tenant_addon_request
     WHERE request_id = $1::uuid AND tenant_id = $2::uuid FOR UPDATE
"""
PLAN_SQL = "SELECT 1 FROM shared.plan WHERE plan_key = $1 AND product = $2 AND retired_at IS NULL"
APPROVE_SQL = """
    UPDATE shared.tenant_addon_request
       SET status = 'approved', decided_at = now(), decided_by = $3, decision_note = $4, plan_key = $5,
           provision_job_id = $6::uuid
     WHERE request_id = $1::uuid AND tenant_id = $2::uuid AND status = 'pending'
    RETURNING request_id::text AS request_id, product, status, note, requested_by, requested_at, decided_at, decided_by,
              decision_note, plan_key, provision_job_id::text AS provision_job_id
"""
DECLINE_SQL = """
    UPDATE shared.tenant_addon_request
       SET status = 'declined', decided_at = now(), decided_by = $3, decision_note = $4
     WHERE request_id = $1::uuid AND tenant_id = $2::uuid AND status = 'pending'
    RETURNING request_id::text AS request_id, product, status, note, requested_by, requested_at, decided_at, decided_by,
              decision_note, plan_key, provision_job_id::text AS provision_job_id
"""


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _text(v: Any, field: str, *, required: bool) -> Optional[str]:
    """Trimmed text of at most NOTE_MAX characters; None for empty when not required."""
    s = v.strip() if isinstance(v, str) else ""
    if not s:
        if required:
            raise PlanError(f"{field}_required", f"say why (1-{NOTE_MAX} characters)")
        return None
    if len(s) > NOTE_MAX:
        raise PlanError(f"{field}_too_long", f"at most {NOTE_MAX} characters")
    return s


def _product(product: Any) -> str:
    if product not in PRODUCTS:
        raise PlanError("unknown_product", "product must be crm or tripplanner")
    return product


def staff_row(r: Any) -> dict:
    """A request as staff see it (Tenant 360)."""
    return {"request_id": r["request_id"], "product": r["product"], "status": r["status"], "note": r["note"],
            "requested_by": r["requested_by"], "requested_at": _iso(r["requested_at"]),
            "decided_at": _iso(r["decided_at"]), "decided_by": r["decided_by"], "decision_note": r["decision_note"],
            "plan_key": r["plan_key"], "provision_job_id": r["provision_job_id"]}


def tenant_row(r: Any) -> dict:
    """A request as the agency sees it: no staff actor, no job id."""
    return {"request_id": r["request_id"], "product": r["product"], "status": r["status"], "note": r["note"],
            "requested_at": _iso(r["requested_at"]), "decided_at": _iso(r["decided_at"]),
            "decision_note": r["decision_note"]}


def add_on_states(live: dict[str, str], requests: list[Any]) -> list[dict]:
    """One entry per add-on product, from the live subscriptions (product -> status) and the tenant's requests (newest
    first). Pure."""
    out = []
    for product in PRODUCTS:
        mine = [r for r in requests if r["product"] == product]
        pending = next((r for r in mine if r["status"] == "pending"), None)
        decided = next((r for r in mine if r["status"] != "pending"), None)
        state = "active" if product in live else "requested" if pending is not None else "available"
        out.append({"product": product, "state": state, "subscription_status": live.get(product),
                    "request": tenant_row(pending) if pending is not None else None,
                    "last_decision": tenant_row(decided) if decided is not None else None})
    return out


async def tenant_view(conn, tenant_id: str) -> dict:
    """The agency app's add-on list. Raises :class:`PlanError` 404 for an unknown tenant."""
    t = await conn.fetchrow(TENANT_SQL, tenant_id)
    if t is None:
        raise PlanError("tenant_not_found", "no tenant with this id", 404)
    live = {r["product"]: r["status"] for r in await conn.fetch(LIVE_SQL, tenant_id, list(PRODUCTS))}
    requests = list(await conn.fetch(REQUESTS_SQL, tenant_id, 20))
    return {"in_service": t["account_status"] in IN_SERVICE, "add_ons": add_on_states(live, requests)}


async def staff_list(conn, tenant_id: str, limit: int = 50) -> list[dict]:
    return [staff_row(r) for r in await conn.fetch(REQUESTS_SQL, tenant_id, limit)]


def _message(name: str, slug: str, product: str, note: Optional[str]) -> str:
    base = f"{name} ({slug}) asks for the {PRODUCT_LABEL[product]} add-on"
    return f"{base}: \"{note[:200]}\"" if note else base


async def create_request(conn, tenant_id: str, product: Any, note: Any, *, actor: str) -> tuple[dict, bool]:
    """The owner's request: (request, created). Raises :class:`PlanError`."""
    product = _product(product)
    note = _text(note, "note", required=False)
    async with conn.transaction():
        # One request at a time per tenant and product (two clicks at once must not both insert or both notify).
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                           f"tenant_addon_request:{tenant_id}:{product}")
        t = await conn.fetchrow(TENANT_SQL, tenant_id)
        if t is None:
            raise PlanError("tenant_not_found", "no tenant with this id", 404)
        if t["account_status"] not in IN_SERVICE:
            raise PlanError("tenant_not_in_service", "this account cannot request add-ons now", 409)
        if await conn.fetch(LIVE_SQL, tenant_id, [product]):
            raise PlanError("already_subscribed", f"the {PRODUCT_LABEL[product]} add-on is already on", 409)
        pending = await conn.fetchrow(PENDING_SQL, tenant_id, product)
        if pending is not None:
            return tenant_row(pending), False
        if (await conn.fetchval(RECENT_SQL, tenant_id) or 0) >= DAILY_CAP:
            raise PlanError("too_many_requests", "too many add-on requests today; try again tomorrow", 429)
        row = await conn.fetchrow(INSERT_SQL, tenant_id, product, note, actor[:64])
        await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=TenantAuditAction.ADDON_REQUESTED,
                              resource_type="tenant_addon_request", resource_id=row["request_id"],
                              details={"product": product, "note": note is not None})
        await NotificationService(conn).emit(
            EventType.TENANT_ADDON_REQUESTED, entity_type="tenant_addon_request", entity_id=row["request_id"],
            tenant_id=tenant_id, actor_type="tenant",
            payload={"message": _message(t["name"], t["slug"], product, note), "tenant_name": t["name"],
                     "tenant_slug": t["slug"], "product": product, "request_id": row["request_id"]})
    return tenant_row(row), True


async def _decidable(conn, tenant_id: str, request_id: str) -> Any:
    try:
        uuid.UUID(str(request_id))
    except ValueError:
        raise PlanError("request_not_found", "no add-on request with this id", 404) from None
    r = await conn.fetchrow(LOCK_REQUEST_SQL, request_id, tenant_id)
    if r is None:
        raise PlanError("request_not_found", "no add-on request with this id", 404)
    if r["status"] != "pending":
        raise PlanError("already_decided", f"this request was already {r['status']}", 409)
    return r


async def approve(conn, tenant_id: str, request_id: str, *, plan_key: Any, seats: Optional[int], note: Any,
                  actor: str, staff_id: Optional[str]) -> tuple[dict, str]:
    """Approves a pending request: (request, provision job id). Raises :class:`PlanError` (and
    :class:`~services.tenant_account.provision.ProvisionError` for a refused saga request, 422)."""
    from services.jobs import tenant_provision_job

    note = _text(note, "note", required=False)
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        r = await _decidable(conn, tenant_id, request_id)
        if not isinstance(plan_key, str) or await conn.fetchval(PLAN_SQL, plan_key, r["product"]) is None:
            raise PlanError("unknown_plan", f"no live {PRODUCT_LABEL[r['product']]} plan {plan_key!r}")
        add_on: dict[str, Any] = {"product": r["product"], "plan_key": plan_key}
        if seats is not None:
            add_on["seats"] = seats
        req = pv.parse({"request_key": f"{REQUEST_KEY_PREFIX}{r['request_id']}", "mode": "attach",
                        "tenant_id": tenant_id, "add_ons": [add_on], "requested_by": staff_id})
        job_id, _created = await tenant_provision_job.enqueue_request(conn, req)
        row = await conn.fetchrow(APPROVE_SQL, r["request_id"], tenant_id, actor[:64], note, plan_key, job_id)
        await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=APPROVED,
                              resource_type="tenant_addon_request", resource_id=r["request_id"],
                              details=json.loads(json.dumps({"product": r["product"], "plan_key": plan_key,
                                                             "seats": seats, "provision_job_id": job_id})))
    return staff_row(row), job_id


async def decline(conn, tenant_id: str, request_id: str, *, reason: Any, actor: str) -> dict:
    """Declines a pending request with a reason the tenant sees. Raises :class:`PlanError`."""
    reason = _text(reason, "reason", required=True)
    async with conn.transaction():
        r = await _decidable(conn, tenant_id, request_id)
        row = await conn.fetchrow(DECLINE_SQL, r["request_id"], tenant_id, actor[:64], reason)
        await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=DECLINED,
                              resource_type="tenant_addon_request", resource_id=r["request_id"],
                              details={"product": r["product"]})
    return staff_row(row)
