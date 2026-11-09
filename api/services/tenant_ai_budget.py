"""AA-1016 (UI slice C4 of the S262 build plan) — the monthly AI budget of a Content studio tenant.

Table ``shared.tenant_ai_budget`` (CIS migration 321): one row per tenant with a budget, no row = no budget
(unlimited, today's behaviour). ``monthly_usd`` NULL = unlimited too; ``alert_pct`` (default 100) is where the bell
rings and ``approval_pct`` (default 150, never below ``alert_pct``) is where new T-stage jobs wait for staff approval
when ``hard_stop`` is on (the enforcement is a later gateway slice: this module only stores and reports the row).

Spend = the tenant's ``shared.llm_call_log`` rows of the current UTC calendar month, shadow rows left out: the same
definition as Tenant 360 › Cost (``api/routers/admin_tenant360._COST_WHERE``, AA-798; ``is_shadow`` is ledger v2,
migration 319, with the older ``quality_signal`` flag as the fallback).

AA-1028 R1 slice 3 (root ADR 0019 d2): the gateway enforces the tenant's resolved ``ai.budget_usd_monthly``
(``shared.cost_guard.tenant_budget_check``): an override on Tenant 360 › Subscription > this row > the plan's included
spend (alert 80 %, approval 100 %, hard stop on when the tenant has no row). This row is one of those sources, not the
whole story, so ``read_one`` also answers ``effective``: what the gateway applies and where it comes from.

Readers and writers:
  - ``read_tier``  — every budget row with its month spend, for AI providers › Budgets (tier "tenant");
  - ``read_one``   — one tenant's row (or none), its month spend and the effective budget, for Tenant 360 › Cost;
  - ``upsert``     — create or change the row (PUT), one ``acp_shared.audit_log`` row with before / after;
  - ``clear``      — delete the row (the tenant falls back to its plan's included spend, or to no budget when its
                     plan has none), audited the same way.
Before migration 321 the table is missing: the reads answer ``ready: False`` and the writes raise
:class:`TenantBudgetUnavailable` (the router answers 503 "not migrated"). Database only, $0: no provider is called.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

TABLE = "shared.tenant_ai_budget"
NOT_MIGRATED = ("the tenant AI budget table is not in the database yet (CIS migration 321 is not applied); "
                "nothing was saved")
AUDIT_SET = "tenant_ai_budget_set"
AUDIT_CLEARED = "tenant_ai_budget_cleared"
AUDIT_RESOURCE = "shared.tenant_ai_budget"

# The fields a PUT sets (every one, a PUT replaces the row's values).
FIELDS = ("monthly_usd", "alert_pct", "approval_pct", "hard_stop", "notes")
DEFAULTS = {"monthly_usd": None, "alert_pct": 100, "approval_pct": 150, "hard_stop": False, "notes": None}


class TenantBudgetUnavailable(Exception):
    """The table (migration 321) is missing or the API role may not write it: nothing was saved."""


_ROW_COLUMNS = ("b.tenant_id::text AS tenant_id, t.name AS tenant_name, b.monthly_usd::float AS monthly_usd, "
                "b.alert_pct, b.approval_pct, b.hard_stop, b.notes, b.created_at, b.updated_at, b.updated_by")

TIER_SQL = f"""
    SELECT {_ROW_COLUMNS}
      FROM shared.tenant_ai_budget b
      JOIN shared.tenants t ON t.tenant_id = b.tenant_id
     ORDER BY t.name, b.tenant_id
"""

ONE_SQL = f"""
    SELECT {_ROW_COLUMNS}
      FROM shared.tenant_ai_budget b
      JOIN shared.tenants t ON t.tenant_id = b.tenant_id
     WHERE b.tenant_id = $1::uuid
"""

# The month's spend of some tenants ($1 uuid[]), between $2 (UTC first of the month) and $3 (first of next month);
# the tenant_created index serves it. Shadow rows out (Tenant 360 rule).
MONTH_SPEND_SQL = """
    SELECT l.tenant_id::text AS tenant_id, count(*)::int AS calls, coalesce(sum(l.cost_usd), 0)::float AS cost_usd
      FROM shared.llm_call_log l
     WHERE l.tenant_id = ANY($1::uuid[])
       AND l.created_at >= $2 AND l.created_at < $3
       AND NOT coalesce(l.is_shadow, l.quality_signal @> '{"shadow": true}'::jsonb)
     GROUP BY 1
"""

TENANT_EXISTS_SQL = "SELECT name FROM shared.tenants WHERE tenant_id = $1::uuid"

# Where the effective budget comes from (CIS 353 / 356 resolver, rank: override > this row > plan): the resolved
# source and the tenant's active override of the key, if any (a disabled one is served as 0 USD by cost_guard).
EFFECTIVE_SOURCE_SQL = """
    SELECT (SELECT e.source FROM shared.v_tenant_entitlement_resolved e
             WHERE e.tenant_id = $1::uuid AND e.feature_key = 'ai.budget_usd_monthly') AS resolved_source,
           (SELECT o.enabled FROM shared.tenant_entitlement_override o
             WHERE o.tenant_id = $1::uuid AND o.feature_key = 'ai.budget_usd_monthly'
               AND (o.expires_at IS NULL OR o.expires_at > now())) AS override_enabled,
           EXISTS (SELECT 1 FROM shared.tenant_ai_budget b WHERE b.tenant_id = $1::uuid) AS has_row
"""

LOCK_SQL = ("SELECT monthly_usd::float AS monthly_usd, alert_pct, approval_pct, hard_stop, notes "
            "FROM shared.tenant_ai_budget WHERE tenant_id = $1::uuid FOR UPDATE")

UPSERT_SQL = """
    INSERT INTO shared.tenant_ai_budget
        (tenant_id, monthly_usd, alert_pct, approval_pct, hard_stop, notes, updated_at, updated_by)
    VALUES ($1::uuid, $2, $3, $4, $5, $6, now(), $7)
    ON CONFLICT (tenant_id) DO UPDATE SET
        monthly_usd = excluded.monthly_usd, alert_pct = excluded.alert_pct, approval_pct = excluded.approval_pct,
        hard_stop = excluded.hard_stop, notes = excluded.notes, updated_at = now(), updated_by = excluded.updated_by
"""

DELETE_SQL = "DELETE FROM shared.tenant_ai_budget WHERE tenant_id = $1::uuid"

AUDIT_SQL = ("INSERT INTO acp_shared.audit_log (tenant_id, actor, action, resource_type, resource_id, details) "
             "VALUES ($1, $2, $3, $4, $5, $6::jsonb)")

_MISSING = (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError)


def month_window(now: datetime) -> tuple[datetime, datetime]:
    """[UTC first of the month, first of the next month) of ``now``. Pure."""
    now = now.astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def budget_state(monthly_usd: Optional[float], spent: float, alert_pct: int, approval_pct: int,
                 hard_stop: bool) -> tuple[Optional[float], str]:
    """(used % of the monthly budget, state) of one tenant. Pure.

    unlimited — no budget amount; ok — under the alert %; alert — at or over the alert %; approval — at or over the
    approval % with hard_stop on (new T-stage jobs wait for staff approval); over — at or over the approval % with
    hard_stop off (alert only). A budget of 0 is "no AI spend": past every threshold before the first call, as the
    gateway reads it (``shared.cost_guard.TenantAIBudget.used_pct``)."""
    if monthly_usd is None:
        return None, "unlimited"
    if monthly_usd <= 0:
        return None, "approval" if hard_stop else "over"
    used = round(100.0 * spent / monthly_usd, 1)
    if used >= approval_pct:
        return (used if monthly_usd > 0 else None), "approval" if hard_stop else "over"
    if used >= alert_pct:
        return used, "alert"
    return used, "ok"


def budget_view(row: dict, spend: Optional[tuple[float, int]] = None) -> dict:
    """One budget row with this month's spend and its state (``spend`` = (usd, calls), None = no call). Pure."""
    usd, calls = spend or (0.0, 0)
    monthly = None if row.get("monthly_usd") is None else float(row["monthly_usd"])
    alert = int(row.get("alert_pct") or DEFAULTS["alert_pct"])
    approval = int(row.get("approval_pct") or DEFAULTS["approval_pct"])
    hard_stop = bool(row.get("hard_stop"))
    used, state = budget_state(monthly, float(usd), alert, approval, hard_stop)
    return {"tenant_id": row["tenant_id"], "tenant_name": row.get("tenant_name"), "monthly_usd": monthly,
            "alert_pct": alert, "approval_pct": approval, "hard_stop": hard_stop, "notes": row.get("notes"),
            "updated_at": _iso(row.get("updated_at")), "updated_by": row.get("updated_by"),
            "month_spent_usd": round(float(usd), 4), "month_calls": int(calls), "used_pct": used, "state": state}


async def _month_spend(conn, tenant_ids: list[str], now: datetime) -> dict[str, tuple[float, int]]:
    if not tenant_ids:
        return {}
    start, end = month_window(now)
    rows = await conn.fetch(MONTH_SPEND_SQL, tenant_ids, start, end)
    return {r["tenant_id"]: (float(r["cost_usd"]), int(r["calls"])) for r in rows}


async def _names(conn, rows: list[dict]) -> dict:
    """Staff names for the rows' ``updated_by`` (ai_gateway.actor_names); {} when the read fails (optional)."""
    from api.services import ai_gateway

    try:
        return await ai_gateway.actor_names(conn, [r.get("updated_by") for r in rows])
    except asyncpg.PostgresError:
        return {}


def _with_actor(view: dict, names: dict) -> dict:
    from api.services import ai_gateway

    return {**view, **ai_gateway.actor_view(view.get("updated_by"), names)}


async def read_tier(pool, *, now: Optional[datetime] = None) -> dict:
    """{ready, rows}: every tenant budget with its month spend (Budgets tab). Before 321: ready False, no rows."""
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        try:
            rows = [dict(r) for r in await conn.fetch(TIER_SQL)]
        except _MISSING:
            return {"ready": False, "rows": []}
        spend = await _month_spend(conn, [r["tenant_id"] for r in rows], now)
        names = await _names(conn, rows)
    return {"ready": True, "rows": [_with_actor(budget_view(r, spend.get(r["tenant_id"])), names) for r in rows]}


def effective_source(resolved_source: Optional[str], override_enabled: Optional[bool], has_row: bool) -> str:
    """Which source the gateway's budget comes from. Pure.

    subscription_override — an active override on Tenant 360 › Subscription (it wins over this row); budget_row — this
    row (AA-1016); plan — the included spend of the tenant's plan (trial included); none — no budget at all."""
    if override_enabled is False or (override_enabled is True and resolved_source == "override"):
        return "subscription_override"
    if resolved_source in ("plan", "trial"):
        return "plan"
    if resolved_source == "override" or has_row:
        return "budget_row"
    return "none"


async def _effective(conn, tenant_id: str) -> Optional[dict]:
    """What the gateway applies to ``tenant_id`` this month (``cost_guard.tenant_budget_check``) and its source; None
    before CIS 353 (no resolver: the row alone decides, as ``budget`` shows)."""
    from shared import cost_guard

    try:
        src = await conn.fetchrow(EFFECTIVE_SOURCE_SQL, tenant_id)
    except _MISSING:
        return None
    b = await cost_guard.tenant_budget_check(conn, tenant_id)
    source = effective_source(src["resolved_source"], src["override_enabled"], bool(src["has_row"]))
    if b is None:
        return {"source": source, "monthly_usd": None, "alert_pct": None, "approval_pct": None, "hard_stop": None,
                "used_pct": None, "state": "unlimited"}
    if source == "none":
        # no key resolves and no row: the gateway fell back to the content subscription plan's value (cost_guard)
        source = "plan"
    used, state = budget_state(b.monthly_usd, b.month_spent_usd, b.alert_pct, b.approval_pct, b.stops)
    return {"source": source, "monthly_usd": round(b.monthly_usd, 2), "alert_pct": b.alert_pct,
            "approval_pct": b.approval_pct, "hard_stop": b.stops, "used_pct": used, "state": state}


async def read_one(pool, tenant_id: str, *, now: Optional[datetime] = None) -> Optional[dict]:
    """One tenant's budget for Tenant 360 › Cost: {ready, tenant_id, tenant_name, budget (this row; None = no row),
    effective (what the gateway applies: source, amount, thresholds, state; None before CIS 353), month_spent_usd,
    month_calls, month_start, defaults}; None when the tenant does not exist."""
    now = now or datetime.now(timezone.utc)
    start, _ = month_window(now)
    async with pool.acquire() as conn:
        name = await conn.fetchval(TENANT_EXISTS_SQL, tenant_id)
        if name is None:
            return None
        spend = (await _month_spend(conn, [tenant_id], now)).get(tenant_id)
        ready = True
        try:
            row = await conn.fetchrow(ONE_SQL, tenant_id)
        except _MISSING:
            ready, row = False, None
        names = await _names(conn, [dict(row)]) if row is not None else {}
        effective = await _effective(conn, tenant_id) if ready else None
    usd, calls = spend or (0.0, 0)
    return {"ready": ready, "tenant_id": tenant_id, "tenant_name": name,
            "budget": _with_actor(budget_view(dict(row), spend), names) if row is not None else None,
            "effective": effective,
            "month_spent_usd": round(float(usd), 4), "month_calls": int(calls), "month_start": start.isoformat(),
            "defaults": dict(DEFAULTS)}


def _audit_details(before: Optional[dict], after: Optional[dict]) -> str:
    def clean(d: Optional[dict]) -> Optional[dict]:
        return None if d is None else {k: d.get(k) for k in FIELDS}
    return json.dumps({"before": clean(before), "after": clean(after)}, default=str)


async def upsert(pool, tenant_id: str, values: dict, *, actor: str) -> dict:
    """Create or replace the tenant's budget (every field of ``values``; the router validated them) and write one
    audit row, in one transaction. LookupError: no such tenant. TenantBudgetUnavailable: table missing / no grant."""
    after = {k: values.get(k, DEFAULTS[k]) for k in FIELDS}
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                if await conn.fetchval(TENANT_EXISTS_SQL, tenant_id) is None:
                    raise LookupError(f"no tenant {tenant_id}")
                prev = await conn.fetchrow(LOCK_SQL, tenant_id)
                before = dict(prev) if prev is not None else None
                await conn.execute(UPSERT_SQL, tenant_id, after["monthly_usd"], after["alert_pct"],
                                   after["approval_pct"], after["hard_stop"], after["notes"], actor[:200])
                await conn.execute(AUDIT_SQL, tenant_id, actor[:64], AUDIT_SET, AUDIT_RESOURCE, tenant_id,
                                   _audit_details(before, after))
        except _MISSING as e:
            raise TenantBudgetUnavailable(NOT_MIGRATED) from e
        except asyncpg.exceptions.InsufficientPrivilegeError as e:
            raise TenantBudgetUnavailable(NOT_MIGRATED) from e
    return {"tenant_id": tenant_id, "created": before is None, **after}


async def clear(pool, tenant_id: str, *, actor: str) -> dict:
    """Delete the tenant's budget row (the plan's included spend applies again, if any) and write one audit row.
    {tenant_id, deleted}: deleted False when there was no row (no audit row then). LookupError /
    TenantBudgetUnavailable as in :func:`upsert`."""
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                if await conn.fetchval(TENANT_EXISTS_SQL, tenant_id) is None:
                    raise LookupError(f"no tenant {tenant_id}")
                prev = await conn.fetchrow(LOCK_SQL, tenant_id)
                if prev is None:
                    return {"tenant_id": tenant_id, "deleted": False}
                await conn.execute(DELETE_SQL, tenant_id)
                await conn.execute(AUDIT_SQL, tenant_id, actor[:64], AUDIT_CLEARED, AUDIT_RESOURCE, tenant_id,
                                   _audit_details(dict(prev), None))
        except _MISSING as e:
            raise TenantBudgetUnavailable(NOT_MIGRATED) from e
        except asyncpg.exceptions.InsufficientPrivilegeError as e:
            raise TenantBudgetUnavailable(NOT_MIGRATED) from e
    return {"tenant_id": tenant_id, "deleted": True}
