"""Add-on requests (AA-1036 R3, root ADR 0019 d7; clarifications TL-7 step 4): an agency asks for an add-on (CRM, Trip
planner) from the agency app, AA staff approve or decline it in Tenant 360 › Subscription. Table
``shared.tenant_addon_request`` (CIS 374).

- :func:`tenant_view`: what the agency app shows per add-on: ``active`` (a live subscription of the product: trialing
  with a trial end ahead, active or past due), ``requested`` (a pending request), else ``available``; plus the last
  decided request, so a decline shows its reason.
- :func:`create_request`: the Content studio owner's "Request add-on" (the route is owner-only through the tenant role
  middleware, ``/v1/account`` writes). Refused when the account is not in service (:func:`in_service`; 409
  ``tenant_not_in_service``) or the
  add-on is already live (409 ``already_subscribed``); a second request while one is pending answers that one
  (``created`` false, no new notification); at most :data:`DAILY_CAP` requests per tenant in 24 hours (429). It writes
  the row, the audit row (``tenant.addon_requested``) and the staff bell row (``tenant.addon.requested``, admin and the
  tenant's customer_success manager) in one transaction, the audit row last (QRY-4, as the saga).
- :func:`approve`: the decision of an admin or of the tenant's account manager (customer_success, its own book: the
  route's capability and ``require_tenant_scope``). Refused (409 ``tenant_not_in_service``) when the account is not in
  service, and (409 ``already_subscribed``) when the add-on is live by now, before anything is written: an approval
  turns an add-on on, it never changes the plan or seats of a live one (``tenant.write`` / ``billing.write``); the
  saga request carries ``only_new`` so a subscription that goes live after the approval is not changed either, and
  any attach closes the pending requests of the products it made live (``provision.py``). The plan (an unretired
  plan of the product) is attached through the existing control-plane path, the ``tenant_provision`` saga in attach
  mode (``services/tenant_account/provision.py``), enqueued in the SAME transaction that marks the request approved,
  with request key ``addon-request:<request id>``: one job per request, and no approved request without its job (or
  the reverse). The saga writes the subscription, the entitlements and the ``tenant.subscription_changed`` audit
  rows; this module adds ``tenant.addon_request_approved``. An approved request then carries ``setup`` from its job
  (:func:`setup_state`): ``in_progress``, ``done`` or ``failed`` (the saga failed for good: the add-on is not on), so
  neither staff nor the agency read a bare "approved".
- :func:`decline`: the same deciders, with a reason (shown to the tenant), audited ``tenant.addon_request_declined``.

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
SETUP_IN_PROGRESS = ("queued", "running")
SETUP_FAILED = ("failed", "cancelled", "stopped_budget")
REQUEST_KEY_PREFIX = "addon-request:"
APPROVED = AdminTenantAuditAction.ADDON_REQUEST_APPROVED
DECLINED = AdminTenantAuditAction.ADDON_REQUEST_DECLINED

TENANT_SQL = "SELECT name, slug, account_status, is_active, cancelled_at FROM shared.tenants WHERE tenant_id = $1::uuid"
LIVE_SQL = """
    SELECT product, status FROM shared.tenant_subscription
     WHERE tenant_id = $1::uuid AND product = ANY($2::text[])
       AND (status IN ('active', 'past_due') OR (status = 'trialing' AND trial_ends_at > now()))
"""
# The job's status says how the approval's setup went (no FK: shared.job rows are pruned; then setup is unknown).
REQUESTS_SQL = """
    SELECT r.request_id::text AS request_id, r.product, r.status, r.note, r.requested_by, r.requested_at,
           r.decided_at, r.decided_by, r.decision_note, r.plan_key, r.provision_job_id::text AS provision_job_id,
           j.status AS job_status
      FROM shared.tenant_addon_request r
      LEFT JOIN shared.job j ON j.id = r.provision_job_id
     WHERE r.tenant_id = $1::uuid
     ORDER BY r.requested_at DESC LIMIT $2
"""
ONE_SQL = """
    SELECT r.request_id::text AS request_id, r.product, r.status, r.note, r.requested_by, r.requested_at,
           r.decided_at, r.decided_by, r.decision_note, r.plan_key, r.provision_job_id::text AS provision_job_id,
           j.status AS job_status, j.error AS job_error
      FROM shared.tenant_addon_request r
      LEFT JOIN shared.job j ON j.id = r.provision_job_id
     WHERE r.request_id = $1::uuid AND r.tenant_id = $2::uuid
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


def in_service(t: Any) -> bool:
    """Is the tenant row in service: the lifecycle status (trial, active, past_due), not suspended (``is_active``) and
    not offboarded (``cancelled_at``)? Every column must say so (a drifted row is not in service)."""
    return t["account_status"] in IN_SERVICE and t["is_active"] is True and t["cancelled_at"] is None


def setup_state(status: Any, job_status: Any) -> Optional[str]:
    """How an approved request's setup (its tenant_provision job) went: in_progress, done, failed; None for a request
    not approved, or when the job row is gone (retention) and the outcome is no longer known."""
    if status != "approved" or not isinstance(job_status, str):
        return None
    if job_status in SETUP_IN_PROGRESS:
        return "in_progress"
    if job_status == "succeeded":
        return "done"
    return "failed" if job_status in SETUP_FAILED else None


def _setup(r: Any) -> Optional[str]:
    return setup_state(r["status"], r.get("job_status"))


def staff_row(r: Any) -> dict:
    """A request as staff see it (Tenant 360)."""
    return {"request_id": r["request_id"], "product": r["product"], "status": r["status"], "note": r["note"],
            "requested_by": r["requested_by"], "requested_at": _iso(r["requested_at"]),
            "decided_at": _iso(r["decided_at"]), "decided_by": r["decided_by"], "decision_note": r["decision_note"],
            "plan_key": r["plan_key"], "provision_job_id": r["provision_job_id"], "setup": _setup(r)}


def tenant_row(r: Any) -> dict:
    """A request as the agency sees it: no staff actor, no job id (only how the setup went)."""
    return {"request_id": r["request_id"], "product": r["product"], "status": r["status"], "note": r["note"],
            "requested_at": _iso(r["requested_at"]), "decided_at": _iso(r["decided_at"]),
            "decision_note": r["decision_note"], "setup": _setup(r)}


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
    return {"in_service": in_service(t), "add_ons": add_on_states(live, requests)}


async def staff_list(conn, tenant_id: str, limit: int = 50) -> list[dict]:
    return [staff_row(r) for r in await conn.fetch(REQUESTS_SQL, tenant_id, limit)]


async def staff_one(conn, tenant_id: str, request_id: str) -> tuple[dict, Optional[str], Optional[str]]:
    """One request of the tenant with its job: (request, job status, job error). Raises :class:`PlanError` 404 (an
    unknown id, or another tenant's request)."""
    try:
        uuid.UUID(str(request_id))
    except ValueError:
        raise PlanError("request_not_found", "no add-on request with this id", 404) from None
    r = await conn.fetchrow(ONE_SQL, request_id, tenant_id)
    if r is None:
        raise PlanError("request_not_found", "no add-on request with this id", 404)
    return staff_row(r), r["job_status"], r["job_error"]


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
        if not in_service(t):
            raise PlanError("tenant_not_in_service", "this account cannot request add-ons now", 409)
        if await conn.fetch(LIVE_SQL, tenant_id, [product]):
            raise PlanError("already_subscribed", f"the {PRODUCT_LABEL[product]} add-on is already on", 409)
        pending = await conn.fetchrow(PENDING_SQL, tenant_id, product)
        if pending is not None:
            return tenant_row(pending), False
        if (await conn.fetchval(RECENT_SQL, tenant_id) or 0) >= DAILY_CAP:
            raise PlanError("too_many_requests", "too many add-on requests today; try again tomorrow", 429)
        row = await conn.fetchrow(INSERT_SQL, tenant_id, product, note, actor[:64])
        await NotificationService(conn).emit(
            EventType.TENANT_ADDON_REQUESTED, entity_type="tenant_addon_request", entity_id=row["request_id"],
            tenant_id=tenant_id, actor_type="tenant",
            payload={"message": _message(t["name"], t["slug"], product, note), "tenant_name": t["name"],
                     "tenant_slug": t["slug"], "product": product, "request_id": row["request_id"]})
        # last in the transaction: the audit chain trigger holds a transaction-scoped lock (QRY-4), as in provision.py
        await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=TenantAuditAction.ADDON_REQUESTED,
                              resource_type="tenant_addon_request", resource_id=row["request_id"],
                              details={"product": product, "note": note is not None})
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
    """Approves a pending request: (request, provision job id). Raises :class:`PlanError` (409
    ``tenant_not_in_service`` before anything is written when the account is not in service; and
    :class:`~services.tenant_account.provision.ProvisionError` for a refused saga request, 422)."""
    from services.jobs import tenant_provision_job

    note = _text(note, "note", required=False)
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        # The tenant row first (FOR SHARE), then the request: the saga's order (tenant FOR UPDATE, then the pending
        # requests an attach closes), so the two never deadlock, and no attach of this tenant runs between the live
        # check below and the commit.
        t = await conn.fetchrow(TENANT_SQL + " FOR SHARE", tenant_id)
        r = await _decidable(conn, tenant_id, request_id)
        if t is None or not in_service(t):
            raise PlanError("tenant_not_in_service", "this account is not in service: no add-on can be attached", 409)
        # The add-on went live after the request (Tenant 360 "Add an add-on", another saga): approving now would change
        # the plan and seats of a live subscription, a tenant.write / billing.write decision, never the account
        # manager's (tenant.book.write). The saga's only_new keeps that true if it goes live after this commit too.
        if await conn.fetch(LIVE_SQL, tenant_id, [r["product"]]):
            raise PlanError("already_subscribed",
                            f"the {PRODUCT_LABEL[r['product']]} add-on is already on; decline this request", 409)
        if not isinstance(plan_key, str) or await conn.fetchval(PLAN_SQL, plan_key, r["product"]) is None:
            raise PlanError("unknown_plan", f"no live {PRODUCT_LABEL[r['product']]} plan {plan_key!r}")
        add_on: dict[str, Any] = {"product": r["product"], "plan_key": plan_key}
        if seats is not None:
            add_on["seats"] = seats
        req = pv.parse({"request_key": f"{REQUEST_KEY_PREFIX}{r['request_id']}", "mode": "attach",
                        "tenant_id": tenant_id, "add_ons": [add_on], "requested_by": staff_id, "only_new": True})
        job_id, _created = await tenant_provision_job.enqueue_request(conn, req)
        job_status = await conn.fetchval("SELECT status FROM shared.job WHERE id = $1::uuid", job_id)
        row = await conn.fetchrow(APPROVE_SQL, r["request_id"], tenant_id, actor[:64], note, plan_key, job_id)
        # last in the transaction (QRY-4, as in provision.py)
        await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=APPROVED,
                              resource_type="tenant_addon_request", resource_id=r["request_id"],
                              details=json.loads(json.dumps({"product": r["product"], "plan_key": plan_key,
                                                             "seats": seats, "provision_job_id": job_id})))
    return staff_row({**dict(row), "job_status": job_status}), job_id


async def decline(conn, tenant_id: str, request_id: str, *, reason: Any, actor: str) -> dict:
    """Declines a pending request with a reason the tenant sees. Raises :class:`PlanError`."""
    reason = _text(reason, "reason", required=True)
    async with conn.transaction():
        r = await _decidable(conn, tenant_id, request_id)
        row = await conn.fetchrow(DECLINE_SQL, r["request_id"], tenant_id, actor[:64], reason)
        # last in the transaction (QRY-4, as in provision.py)
        await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=DECLINED,
                              resource_type="tenant_addon_request", resource_id=r["request_id"],
                              details={"product": r["product"]})
    return staff_row(row)
