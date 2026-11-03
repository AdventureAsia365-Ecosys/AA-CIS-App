"""Tenant 360 › Subscription (AA-1028 R1 slice 3, root ADR 0019 d1 / d2): one tenant's subscriptions, its resolved
entitlements with their source, and the per-tenant overrides (``shared.tenant_entitlement_override``, CIS 353).

An override is the staff's exception to the plan for one tenant and one feature key: ``enabled = false`` takes a plan
feature away (the revocation path, e.g. a CRM channel), ``enabled = true`` with a ``limit_value`` sets another limit
(``ai.budget_usd_monthly`` 0.01 for the fail-closed proof; NULL = no limit), a flag the plan lacks can be granted.
A ``reason`` is required; ``expires_at`` (optional) ends it (the hourly tick re-syncs at the end). An override of a crm
/ tripplanner key applies only while the tenant has a live subscription of that product (the resolver); the response
says so (``applies``). The CIS 353 trigger re-syncs the tenant and writes ``entitlement.changed`` (``aa.actor`` = the
admin); the audit row (``tenant.entitlement_override_set`` / ``_removed``) holds before and after.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.tenant_account.plans import LIMIT_MAX, PlanError, parse_decimal

OVERRIDE_SET = AdminTenantAuditAction.ENTITLEMENT_OVERRIDE_SET
OVERRIDE_REMOVED = AdminTenantAuditAction.ENTITLEMENT_OVERRIDE_REMOVED

TENANT_SQL = """
    SELECT tenant_id::text AS tenant_id, name, plan_tier::text AS plan_tier, account_status, suspend_reason, kind,
           is_pilot, pilot_ends_at
      FROM shared.tenants WHERE tenant_id = $1::uuid
"""
SUBSCRIPTIONS_SQL = """
    SELECT s.product, s.plan_key, p.name AS plan_name, s.status, s.trial_ends_at, s.paid_through, s.seats,
           s.started_at, s.canceled_at, s.updated_at, s.updated_by, p.retired_at IS NOT NULL AS plan_retired
      FROM shared.tenant_subscription s
      JOIN shared.plan p ON p.plan_key = s.plan_key
     WHERE s.tenant_id = $1::uuid
     ORDER BY CASE s.product WHEN 'content' THEN 0 WHEN 'crm' THEN 1 ELSE 2 END
"""
ENTITLEMENTS_SQL = """
    SELECT r.feature_key, r.product, r.value_kind, r.unit, r.limit_value::text AS limit_value, r.source, r.plan_key,
           r.expires_at, f.description
      FROM shared.v_tenant_entitlement_resolved r
      JOIN shared.entitlement_feature f ON f.feature_key = r.feature_key
     WHERE r.tenant_id = $1::uuid
     ORDER BY CASE r.product WHEN 'content' THEN 0 WHEN 'crm' THEN 1 WHEN 'tripplanner' THEN 2 ELSE 3 END,
              r.feature_key
"""
OVERRIDES_SQL = """
    SELECT o.feature_key, f.product, f.value_kind, o.limit_value::text AS limit_value, o.enabled, o.reason,
           o.granted_by, o.expires_at, o.updated_at, (o.expires_at IS NULL OR o.expires_at > now()) AS active
      FROM shared.tenant_entitlement_override o
      JOIN shared.entitlement_feature f ON f.feature_key = o.feature_key
     WHERE o.tenant_id = $1::uuid
     ORDER BY o.feature_key
"""
ADDON_PLANS_SQL = """
    SELECT plan_key, product, name, seats_included FROM shared.plan
     WHERE product IN ('crm', 'tripplanner') AND retired_at IS NULL ORDER BY product, plan_key
"""


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v is not None else None


async def snapshot(conn, tenant_id: str) -> Optional[dict]:
    """The Subscription tab's data, or None when no tenant has this id."""
    t = await conn.fetchrow(TENANT_SQL, tenant_id)
    if t is None:
        return None
    subs = await conn.fetch(SUBSCRIPTIONS_SQL, tenant_id)
    ents = await conn.fetch(ENTITLEMENTS_SQL, tenant_id)
    overrides = await conn.fetch(OVERRIDES_SQL, tenant_id)
    plans = await conn.fetch(ADDON_PLANS_SQL)
    return {
        "tenant": {**{k: t[k] for k in ("tenant_id", "name", "plan_tier", "account_status", "suspend_reason", "kind")},
                   "is_pilot": t["is_pilot"] is True, "pilot_ends_at": _iso(t["pilot_ends_at"]),
                   "in_service": t["account_status"] in ("trial", "active", "past_due")},
        "subscriptions": [{**{k: r[k] for k in ("product", "plan_key", "plan_name", "status", "seats", "updated_by",
                                                "plan_retired")},
                           "trial_ends_at": _iso(r["trial_ends_at"]),
                           "paid_through": r["paid_through"].isoformat() if r["paid_through"] else None,
                           "started_at": _iso(r["started_at"]), "canceled_at": _iso(r["canceled_at"]),
                           "updated_at": _iso(r["updated_at"])} for r in subs],
        "entitlements": [{**{k: r[k] for k in ("feature_key", "product", "value_kind", "unit", "limit_value",
                                               "source", "plan_key", "description")},
                          "expires_at": _iso(r["expires_at"])} for r in ents],
        "overrides": [{**{k: r[k] for k in ("feature_key", "product", "value_kind", "limit_value", "enabled", "reason",
                                            "granted_by", "active")},
                       "expires_at": _iso(r["expires_at"]), "updated_at": _iso(r["updated_at"])} for r in overrides],
        "add_on_plans": [dict(r) for r in plans],
    }


FEATURE_SQL = "SELECT feature_key, product, value_kind FROM shared.entitlement_feature WHERE feature_key = $1 " \
              "AND retired_at IS NULL"
TENANT_LOCK_SQL = "SELECT account_status FROM shared.tenants WHERE tenant_id = $1::uuid FOR UPDATE"
OVERRIDE_ROW_SQL = """
    SELECT limit_value::text AS limit_value, enabled, reason, expires_at
      FROM shared.tenant_entitlement_override WHERE tenant_id = $1::uuid AND feature_key = $2 FOR UPDATE
"""
UPSERT_OVERRIDE_SQL = """
    INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, enabled, reason, granted_by,
                                                    expires_at)
    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (tenant_id, feature_key) DO UPDATE
       SET limit_value = EXCLUDED.limit_value, enabled = EXCLUDED.enabled, reason = EXCLUDED.reason,
           granted_by = EXCLUDED.granted_by, expires_at = EXCLUDED.expires_at, updated_at = now()
"""
DELETE_OVERRIDE_SQL = "DELETE FROM shared.tenant_entitlement_override WHERE tenant_id = $1::uuid AND feature_key = $2"
LIVE_PRODUCT_SQL = """
    SELECT EXISTS (SELECT 1 FROM shared.tenant_subscription
                    WHERE tenant_id = $1::uuid AND product = $2
                      AND (status IN ('active', 'past_due') OR (status = 'trialing' AND trial_ends_at > now())))
"""
RESOLVED_SQL = """
    SELECT limit_value::text AS limit_value, source FROM shared.v_tenant_entitlement_resolved
     WHERE tenant_id = $1::uuid AND feature_key = $2
"""


def _row(r: Any) -> Optional[dict]:
    if r is None:
        return None
    return {"limit_value": r["limit_value"], "enabled": r["enabled"], "reason": r["reason"],
            "expires_at": _iso(r["expires_at"])}


async def _resolved(conn, tenant_id: str, feature_key: str) -> Optional[dict]:
    r = await conn.fetchrow(RESOLVED_SQL, tenant_id, feature_key)
    return None if r is None else {"limit_value": r["limit_value"], "source": r["source"]}


async def set_override(conn, tenant_id: str, feature_key: str, *, enabled: bool, limit_value: Any, reason: Any,
                       expires_at: Optional[datetime], actor: str) -> dict:
    """Creates or replaces the override; returns {override, applies, resolved}. Raises :class:`PlanError`."""
    reason = reason.strip() if isinstance(reason, str) else ""
    if not 1 <= len(reason) <= 500:
        raise PlanError("reason_required", "say why (1-500 characters)")
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise PlanError("expires_in_past", "expires_at must be in the future")
    limit: Optional[Decimal] = parse_decimal(limit_value, f"{feature_key} limit", LIMIT_MAX)
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        status = await conn.fetchval(TENANT_LOCK_SQL, tenant_id)
        if status is None:
            raise PlanError("tenant_not_found", "no tenant with this id", 404)
        if status in ("offboarding", "closed"):
            raise PlanError("tenant_offboarded", "restore the tenant first", 409)
        f = await conn.fetchrow(FEATURE_SQL, feature_key)
        if f is None:
            raise PlanError("unknown_feature", f"no live feature key {feature_key!r}", 404)
        if f["value_kind"] == "flag" and limit is not None:
            raise PlanError("flag_takes_no_limit", f"{feature_key} is on/off and takes no limit")
        if not enabled and limit is not None:
            raise PlanError("disabled_takes_no_limit", "a disabled feature takes no limit")
        before = _row(await conn.fetchrow(OVERRIDE_ROW_SQL, tenant_id, feature_key))
        await conn.execute(UPSERT_OVERRIDE_SQL, tenant_id, feature_key, limit, enabled, reason, actor[:64], expires_at)
        after = {"limit_value": None if limit is None else str(limit), "enabled": enabled, "reason": reason,
                 "expires_at": _iso(expires_at)}
        product = "content" if f["product"] == "platform" else f["product"]
        applies = bool(await conn.fetchval(LIVE_PRODUCT_SQL, tenant_id, product))
        resolved = await _resolved(conn, tenant_id, feature_key)
        await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=OVERRIDE_SET, resource_type="tenant",
                              resource_id=tenant_id,
                              details=json.loads(json.dumps({"feature_key": feature_key, "before": before,
                                                             "after": after, "applies": applies,
                                                             "resolved": resolved}, default=str)))
    return {"feature_key": feature_key, "override": after, "applies": applies, "resolved": resolved}


async def remove_override(conn, tenant_id: str, feature_key: str, *, actor: str) -> dict:
    """Deletes the override (the plan's value applies again). Raises :class:`PlanError` (404 when there is none)."""
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        if await conn.fetchval(TENANT_LOCK_SQL, tenant_id) is None:
            raise PlanError("tenant_not_found", "no tenant with this id", 404)
        before = _row(await conn.fetchrow(OVERRIDE_ROW_SQL, tenant_id, feature_key))
        if before is None:
            raise PlanError("override_not_found", "this tenant has no override of this key", 404)
        await conn.execute(DELETE_OVERRIDE_SQL, tenant_id, feature_key)
        resolved = await _resolved(conn, tenant_id, feature_key)
        await write_audit_log(conn, tenant_id=tenant_id, actor=actor, action=OVERRIDE_REMOVED, resource_type="tenant",
                              resource_id=tenant_id,
                              details=json.loads(json.dumps({"feature_key": feature_key, "before": before,
                                                             "resolved": resolved}, default=str)))
    return {"feature_key": feature_key, "removed": True, "resolved": resolved}
