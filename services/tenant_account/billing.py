"""The billing tick of the tenant account (AA-1037 R4 slice 1, root ADR 0019 d9), run by the hourly
``tenant_billing_tick`` job (``services/jobs/tenant_billing_tick_job.py``).

Every agency invoice is issued from AA-Booking; Booking publishes each agency's billing status in its contract view
``booking.v_tenant_billing_status`` (bk-042, SELECT to ``cis_app``): ``status`` ok | warn | past_due from the oldest
overdue invoice against the agency's warn / suspend days (Booking ``billing_terms``, finance edits them),
``oldest_overdue_days``, ``overdue_amount`` ({currency: amount}) and ``paid_through`` (the last day of the newest paid
software-fee period). CIS stays the only writer of the account and subscription state (d1), so this tick moves them:

1. **Paid through.** A newer ``paid_through`` is written to the tenant's live subscriptions (``trialing``, ``active``,
   ``past_due``; never backwards). When Booking says ``ok``, a ``past_due`` subscription paid through today moves back
   to ``active`` (the trial-end rule of ``lifecycle.py`` reads the same column).
2. **warn** (overdue past the warn days): an ``active`` account moves to ``past_due`` (the grace state: service goes
   on) and the tenant's bell gets ``tenant.billing.overdue`` (once per tenant and UTC day; staff admin and finance see
   it too).
3. **past_due** (overdue past the suspend days): a live account (``trial``, ``active``, ``past_due``) is suspended
   through the existing account path: ``account_status = 'suspended'``, ``suspend_reason = 'billing'`` (the CIS 354
   triggers derive ``is_active`` and write ``account.status_changed`` for the CRM and Booking pulls). Never a delete.
4. **ok** again: an account suspended for ``billing`` comes back (``active`` when its content subscription is
   ``active``, else ``past_due``); a ``past_due`` account whose content subscription is ``active`` again is ``active``.
   Only a ``billing`` suspension is lifted: compliance, security and tenant-request suspensions stay.

On Dev only synthetic tenants (``kind`` ``demo``) are moved (root ADR 0019 d16: the pilot agency and any older real
row are counted ``skipped_real``); AA itself (``internal``) never. Without the Booking view (bk-042 not applied, or no
grant) the run answers ``skipped``. Each tenant is its own transaction (one bad row never holds the others), with
``aa.actor`` = ``job:tenant_billing_tick:<job id>``.

AA-1037 (root ADR 0019 d9 "Screens now") adds :func:`tenant_snapshot`, the read behind Tenant 360 › Billing ›
Booking invoices (``GET /admin/tenants/{tenant_id}/booking-billing``): the same view row for one tenant (status, the
overdue figures, paid through, the warn / suspend days), next to the CIS side it drives (account status and suspend
reason, the live subscriptions' paid through). Read only, no invoice line, bill-to or person; the view unreadable ->
``available: false`` with the CIS side still filled.
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import asyncpg
import structlog

from services.notifications import EventType, NotificationService
from services.tenant_account import dev_guard

logger = structlog.get_logger()

STATUS_SQL = """
    SELECT tenant_id, status, oldest_overdue_days, overdue_invoices, overdue_amount, paid_through, warn_after_days,
           suspend_after_days
      FROM booking.v_tenant_billing_status
"""
TENANTS_SQL = """
    SELECT tenant_id, account_status, suspend_reason, kind FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])
"""
TENANT_LOCK_SQL = """
    SELECT account_status, suspend_reason, kind FROM shared.tenants WHERE tenant_id = $1::uuid FOR UPDATE
"""
PAID_THROUGH_SQL = """
    UPDATE shared.tenant_subscription SET paid_through = $2, updated_by = $3
     WHERE tenant_id = $1::uuid AND status IN ('trialing', 'active', 'past_due')
       AND (paid_through IS NULL OR paid_through < $2)
"""
REACTIVATE_SUBSCRIPTIONS_SQL = """
    UPDATE shared.tenant_subscription SET status = 'active', updated_by = $2
     WHERE tenant_id = $1::uuid AND status = 'past_due' AND paid_through >= current_date
"""
CONTENT_STATUS_SQL = """
    SELECT status FROM shared.tenant_subscription WHERE tenant_id = $1::uuid AND product = 'content'
"""
SET_ACCOUNT_SQL = """
    UPDATE shared.tenants SET account_status = $2, suspend_reason = $3, updated_at = now() WHERE tenant_id = $1::uuid
"""
NOTIFIED_TODAY_SQL = """
    SELECT EXISTS (SELECT 1 FROM shared.notifications
                    WHERE tenant_id = $1::uuid AND event_type = $2
                      AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc')
"""
LIVE_ACCOUNT = ("trial", "active", "past_due")
BILLING = "billing"

# AA-1037 Tenant 360 read: one tenant's row of the view, its account and its live subscriptions.
TENANT_STATUS_SQL = """
    SELECT status, oldest_overdue_days, overdue_invoices, overdue_amount, paid_through, warn_after_days,
           suspend_after_days
      FROM booking.v_tenant_billing_status WHERE tenant_id = $1::uuid
"""
TENANT_ACCOUNT_SQL = "SELECT account_status, suspend_reason FROM shared.tenants WHERE tenant_id = $1::uuid"
LIVE_SUBSCRIPTIONS_SQL = """
    SELECT product, status, paid_through FROM shared.tenant_subscription
     WHERE tenant_id = $1::uuid AND status IN ('trialing', 'active', 'past_due') ORDER BY product
"""
VIEW_UNREADABLE = (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError, asyncpg.InvalidSchemaNameError)
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def actor_for(job_id: str) -> str:
    return f"job:tenant_billing_tick:{job_id}"[:64]


def decide(view_status: str, account_status: str, suspend_reason: Optional[str],
           content_status: Optional[str]) -> Optional[tuple[str, Optional[str], Optional[EventType]]]:
    """The account move for one tenant (pure): (new status, suspend reason, bell event) or None (no move).

    ``content_status`` is the content subscription's status after step 1 of the module docstring."""
    if view_status == "past_due":
        if account_status in LIVE_ACCOUNT:
            return "suspended", BILLING, EventType.TENANT_BILLING_SUSPENDED
        return None
    if view_status == "warn":
        if account_status == "active":
            return "past_due", None, EventType.TENANT_BILLING_OVERDUE
        if account_status == "past_due":
            return "past_due", None, EventType.TENANT_BILLING_OVERDUE   # the daily bell only
        return None
    if view_status == "ok":
        if account_status == "suspended" and suspend_reason == BILLING:
            return ("active" if content_status == "active" else "past_due"), None, EventType.TENANT_BILLING_RESTORED
        if account_status == "past_due" and content_status == "active":
            return "active", None, None
    return None


async def read_status(pool) -> Optional[list[dict[str, Any]]]:
    """Booking's billing status rows, or None when the view is not readable (bk-042 not applied, or no grant)."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(STATUS_SQL)
    except VIEW_UNREADABLE as e:
        logger.warning("tenant_billing_status_unreadable", error=type(e).__name__)
        return None
    return [dict(r) for r in rows]


def _amount(value: Any) -> dict:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return dict(value or {})


def overdue_amounts(value: Any) -> dict[str, str]:
    """The view's ``overdue_amount`` ({currency: amount}) as {ISO code: decimal string}, sorted by code. A key that is
    not a 3-letter code or an amount that is not a finite number is dropped (the screen formats, never computes)."""
    try:   # parse_float=Decimal keeps the amount's scale (240.50 stays "240.50", never the float 240.5)
        raw = json.loads(value, parse_float=Decimal) if isinstance(value, str) else dict(value or {})
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for code, amount in raw.items():
        if not isinstance(code, str) or not _CURRENCY.match(code) or isinstance(amount, bool):
            continue
        try:
            d = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            continue
        if d.is_finite():
            out[code] = format(d, "f")
    return dict(sorted(out.items()))


def _day(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, date) else None


def booking_block(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """One view row as the Tenant 360 answer shows it; None when Booking has no row for the tenant (no agency
    invoice terms yet)."""
    if row is None:
        return None
    return {
        "status": row["status"],
        "oldest_overdue_days": row["oldest_overdue_days"],
        "overdue_invoices": row["overdue_invoices"],
        "overdue_amount": overdue_amounts(row["overdue_amount"]),
        "paid_through": _day(row["paid_through"]),
        "warn_after_days": row["warn_after_days"],
        "suspend_after_days": row["suspend_after_days"],
    }


async def tenant_snapshot(conn, tenant_id: str) -> dict[str, Any]:
    """Tenant 360 › Billing › Booking invoices for one tenant (the caller has checked the tenant exists and is in the
    caller's scope). ``available`` is false when the view cannot be read (bk-042 not applied, or no grant); the CIS
    side (account, subscriptions) is filled either way. The view is read last, so a refusal leaves nothing undone."""
    account = await conn.fetchrow(TENANT_ACCOUNT_SQL, tenant_id)
    subscriptions = [{"product": r["product"], "status": r["status"], "paid_through": _day(r["paid_through"])}
                     for r in await conn.fetch(LIVE_SUBSCRIPTIONS_SQL, tenant_id)]
    available = True
    booking: Optional[dict[str, Any]] = None
    try:
        row = await conn.fetchrow(TENANT_STATUS_SQL, tenant_id)
        booking = booking_block(dict(row) if row is not None else None)
    except VIEW_UNREADABLE as e:
        logger.warning("tenant_billing_status_unreadable", error=type(e).__name__, where="tenant360")
        available = False
    return {
        "available": available,
        "booking": booking,
        "account": {"account_status": account["account_status"] if account else None,
                    "suspend_reason": account["suspend_reason"] if account else None},
        "subscriptions": subscriptions,
    }


async def _apply(conn, row: dict[str, Any], actor: str, out: dict[str, Any]) -> None:
    tid = str(row["tenant_id"])
    await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
    t = await conn.fetchrow(TENANT_LOCK_SQL, tid)
    if t is None:
        out["not_in_cis"] += 1
        return
    paid_through: Optional[date] = row.get("paid_through")
    if paid_through is not None:
        moved = await conn.execute(PAID_THROUGH_SQL, tid, paid_through, actor)
        if moved and not moved.endswith(" 0"):
            out["paid_through_moved"] += 1
    if row["status"] == "ok":
        await conn.execute(REACTIVATE_SUBSCRIPTIONS_SQL, tid, actor)
    content = await conn.fetchval(CONTENT_STATUS_SQL, tid)
    move = decide(row["status"], t["account_status"], t["suspend_reason"], content)
    if move is None:
        return
    status, reason, event = move
    if status != t["account_status"] or reason != t["suspend_reason"]:
        await conn.execute(SET_ACCOUNT_SQL, tid, status, reason)
        key = {"suspended": "suspended", "past_due": "warned"}.get(status, "restored")
        out[key] += 1
        logger.info("tenant_billing_moved", tenant_id=tid, account_status=status, previous=t["account_status"],
                    oldest_overdue_days=row["oldest_overdue_days"])
    if event is not None and not await conn.fetchval(NOTIFIED_TODAY_SQL, tid, event.value):
        await NotificationService(conn).emit(
            event, "tenant", tid, tid,
            {"billing_status": row["status"], "oldest_overdue_days": row["oldest_overdue_days"],
             "overdue_invoices": row["overdue_invoices"], "overdue_amount": _amount(row["overdue_amount"]),
             "suspend_after_days": row["suspend_after_days"]},
        )
        out["notified"] += 1


async def tick(pool, *, job_id: str) -> dict[str, Any]:
    rows = await read_status(pool)
    if rows is None:
        return {"outcome": "skipped", "reason": "booking.v_tenant_billing_status not readable (bk-042)"}
    out: dict[str, Any] = {"outcome": "ok", "tenants": len(rows), "warned": 0, "suspended": 0, "restored": 0,
                           "notified": 0, "paid_through_moved": 0, "skipped_real": 0, "not_in_cis": 0, "failed": 0}
    if not rows:
        return out
    async with pool.acquire() as conn:
        tenants = {str(r["tenant_id"]): dict(r) for r in await conn.fetch(TENANTS_SQL, [str(r["tenant_id"])
                                                                                       for r in rows])}
    actor = actor_for(job_id)
    dev = dev_guard.on_dev()
    for row in rows:
        t = tenants.get(str(row["tenant_id"]))
        if t is None:
            out["not_in_cis"] += 1
            continue
        if t["kind"] == "internal" or (dev and t["kind"] != "demo"):
            out["skipped_real"] += 1
            continue
        try:
            async with pool.acquire() as conn, conn.transaction():
                await _apply(conn, row, actor, out)
        except Exception as e:  # noqa: BLE001 — one tenant never holds the others; the next hour retries it
            out["failed"] += 1
            logger.warning("tenant_billing_tick_failed", tenant_id=str(row["tenant_id"]), error=str(e)[:300])
    return out
