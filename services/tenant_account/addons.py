"""AA-1116 (option B, CIS 435 / 437) — the add-ons a tenant holds beyond its product subscriptions.

``shared.tenant_addon`` rows of ``addon.*`` plans (Plans & add-ons): the custom domain (gives tp.custom_domain and
crm.custom_domain where that product is live) and the AI pack (each unit adds the plan's ai.budget_addon_usd_monthly
to the tenant's AI budget, resolver 437). Staff grant them in Tenant 360 › Add-ons (admin, ``tenant.write``), with a
reason; every change is audited (``tenant.addon_set`` / ``tenant.addon_canceled``) and re-syncs the tenant's
entitlements (the 435 trigger).

Rules (S282 decisions):
  * one live row per tenant and add-on plan: granting more of an add-on the tenant holds changes its quantity / end;
  * a new add-on starts trialing for ``cis.billing.trial_days_default`` days (decision 3), once per tenant and plan: a
    plan the tenant held before (a canceled row) comes back active, never trialing again;
  * the tenant must be in service (trial, active, past_due) with a live content subscription (the 435 guard);
  * ``ends_at`` is optional (an AI pack for one month); the hourly tick re-syncs a tenant whose add-on ended.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.tenant_account import pricing_settings as ps

QUANTITY_MAX = 1000          # technical-constant: CIS 435 tenant_addon_quantity_check (1-1000)
REASON_MIN, REASON_MAX = 3, 500   # technical-constant: CIS 435 tenant_addon_text_check


class AddonError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code, message, status)
        self.code, self.message, self.status = code, message, status


LIST_SQL = """
    SELECT a.addon_id::text AS addon_id, a.plan_key, p.name AS plan_name, a.quantity, a.status, a.trial_ends_at,
           a.starts_at, a.ends_at, a.canceled_at, a.reason, a.created_by, a.created_at, a.updated_at, a.updated_by,
           p.price_usd_monthly::text AS price_usd_monthly, p.price_vnd_monthly::text AS price_vnd_monthly,
           (a.status IN ('trialing', 'active') AND a.starts_at <= now()
            AND (a.ends_at IS NULL OR a.ends_at > now())) AS live
      FROM shared.tenant_addon a
      JOIN shared.plan p ON p.plan_key = a.plan_key
     WHERE a.tenant_id = $1::uuid
     ORDER BY a.status = 'canceled', p.name, a.created_at
"""
TENANT_SQL = "SELECT account_status FROM shared.tenants WHERE tenant_id = $1::uuid FOR UPDATE"
PLAN_SQL = "SELECT plan_key FROM shared.plan WHERE plan_key = $1 AND product = 'addon' AND retired_at IS NULL"
LIVE_ROW_SQL = """
    SELECT addon_id::text AS addon_id, quantity, ends_at, status FROM shared.tenant_addon
     WHERE tenant_id = $1::uuid AND plan_key = $2 AND status <> 'canceled' FOR UPDATE
"""
HELD_BEFORE_SQL = "SELECT EXISTS (SELECT 1 FROM shared.tenant_addon WHERE tenant_id = $1::uuid AND plan_key = $2)"
INSERT_SQL = """
    INSERT INTO shared.tenant_addon (tenant_id, plan_key, quantity, status, trial_ends_at, ends_at, reason,
                                     created_by, updated_by)
    VALUES ($1::uuid, $2, $3, CASE WHEN $4::int IS NULL THEN 'active' ELSE 'trialing' END,
            CASE WHEN $4::int IS NULL THEN NULL ELSE now() + make_interval(days => $4::int) END, $5, $6, $7, $7)
    RETURNING addon_id::text AS addon_id, status, trial_ends_at
"""
UPDATE_SQL = """
    UPDATE shared.tenant_addon SET quantity = $2, ends_at = $3, reason = $4, updated_by = $5
     WHERE addon_id = $1::uuid
    RETURNING addon_id::text AS addon_id, status, trial_ends_at
"""
CANCEL_SQL = """
    UPDATE shared.tenant_addon SET status = 'canceled', canceled_at = now(), reason = $3, updated_by = $4
     WHERE addon_id = $1::uuid AND tenant_id = $2::uuid AND status <> 'canceled'
    RETURNING addon_id::text AS addon_id, plan_key, quantity
"""
IN_SERVICE = ("trial", "active", "past_due")


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


async def list_addons(conn, tenant_id: str) -> list[dict]:
    return [{k: _iso(v) for k, v in dict(r).items()} for r in await conn.fetch(LIST_SQL, tenant_id)]


def _reason(reason: Any) -> str:
    if not isinstance(reason, str) or not REASON_MIN <= len(reason.strip()) <= REASON_MAX:
        raise AddonError("invalid_reason", f"reason: {REASON_MIN}-{REASON_MAX} characters")
    return reason.strip()


def _quantity(quantity: Any) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= QUANTITY_MAX:
        raise AddonError("invalid_quantity", f"quantity: 1-{QUANTITY_MAX}")
    return quantity


async def _audit(conn, tenant_id: str, actor: str, action: str, details: dict) -> None:
    await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=action, resource_type="tenant",
                          resource_id=tenant_id, details=json.loads(json.dumps(details, default=str)))


async def set_addon(conn, tenant_id: str, *, plan_key: str, quantity: Any = 1, ends_at: Optional[datetime] = None,
                    reason: Any, actor: str) -> dict:
    """Grant an add-on, or change the quantity / end of the one the tenant holds. Returns the row as listed."""
    quantity, reason = _quantity(quantity), _reason(reason)
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor[:64])
        status = await conn.fetchval(TENANT_SQL, tenant_id)
        if status is None:
            raise AddonError("tenant_not_found", "no tenant with this id", 404)
        if status not in IN_SERVICE:
            raise AddonError("tenant_not_in_service", "the tenant is not in service", 409)
        if await conn.fetchval(PLAN_SQL, plan_key) is None:
            raise AddonError("unknown_addon_plan", f"no live add-on plan {plan_key}", 404)
        if ends_at is not None and ends_at <= datetime.now(ends_at.tzinfo):
            raise AddonError("invalid_ends_at", "ends_at is in the future")
        live = await conn.fetchrow(LIVE_ROW_SQL, tenant_id, plan_key)
        if live is not None:
            row = await conn.fetchrow(UPDATE_SQL, live["addon_id"], quantity, ends_at, reason, actor[:64])
            before = {"quantity": live["quantity"], "ends_at": live["ends_at"], "status": live["status"]}
        else:
            # decision 3: one trial per tenant and add-on plan; a plan held before comes back active
            trial = None if await conn.fetchval(HELD_BEFORE_SQL, tenant_id, plan_key) else \
                ps.int_value(ps.TRIAL_DAYS_DEFAULT)
            row = await conn.fetchrow(INSERT_SQL, tenant_id, plan_key, quantity, trial, ends_at, reason, actor[:64])
            before = None
        await _audit(conn, tenant_id, actor, AdminTenantAuditAction.ADDON_SET, {
            "addon_id": row["addon_id"], "plan_key": plan_key, "reason": reason, "before": before,
            "after": {"quantity": quantity, "ends_at": ends_at, "status": row["status"],
                      "trial_ends_at": row["trial_ends_at"]}})
    return next(a for a in await list_addons(conn, tenant_id) if a["addon_id"] == row["addon_id"])


async def cancel_addon(conn, tenant_id: str, addon_id: str, *, reason: Any, actor: str) -> dict:
    reason = _reason(reason)
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor[:64])
        row = await conn.fetchrow(CANCEL_SQL, addon_id, tenant_id, reason, actor[:64])
        if row is None:
            raise AddonError("addon_not_found", "no live add-on with this id for the tenant", 404)
        await _audit(conn, tenant_id, actor, AdminTenantAuditAction.ADDON_CANCELED,
                     {"addon_id": addon_id, "plan_key": row["plan_key"], "quantity": row["quantity"],
                      "reason": reason})
    return next(a for a in await list_addons(conn, tenant_id) if a["addon_id"] == addon_id)
