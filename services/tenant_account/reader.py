"""The account snapshot of tenant.entitlements.v1 (AA-1028 R1): ``GET /tenant/v1/tenants/{tenant_id}/account``.

Reads run on the CIS main pool (``app.state.pool``, role cis_app, the only role CIS 353 / 355 grant these objects),
one READ ONLY transaction per request; the CRM and Booking never read the database (root ADR 0008a decision 1).

- The tenant row: ``account_status`` (the master of the lifecycle, CIS 354), ``suspend_reason``, ``kind``,
  ``is_pilot``, ``data_region``, ``time_zone``. ``in_service`` = the status is trial, active or past_due: a consumer
  refuses every feature otherwise (contract description).
- Every subscription row of the tenant (any status), product order.
- The resolved entitlements from ``shared.v_tenant_entitlement_v1`` (CIS 355, computed live from the resolver, so an
  expired override or trial is served right before the next sync). ``limit`` is the decimal as text (``15.00``),
  never a JSON number (contracts rule 8); NULL = no limit.

Never served: the bill-to fields, prices, billing references, override reasons, actors (the contract field policy;
no SELECT here names them).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import asyncpg

ACQUIRE_TIMEOUT_S = 5.0
IN_SERVICE = frozenset({"trial", "active", "past_due"})

ACCOUNT_SQL = """
    SELECT tenant_id, account_status, suspend_reason, kind, is_pilot, data_region, time_zone
      FROM shared.tenants
     WHERE tenant_id = $1::uuid
"""
SUBSCRIPTIONS_SQL = """
    SELECT product, plan_key, status, trial_ends_at, seats
      FROM shared.tenant_subscription
     WHERE tenant_id = $1::uuid
     ORDER BY product
"""
ENTITLEMENTS_SQL = """
    SELECT feature_key, product, value_kind, unit, limit_value::text AS limit_text, source, expires_at
      FROM shared.v_tenant_entitlement_v1
     WHERE tenant_id = $1::uuid
     ORDER BY feature_key
"""

_POOL_ERRORS = (asyncpg.PostgresConnectionError, asyncpg.TooManyConnectionsError, asyncpg.InterfaceError,
                OSError, asyncio.TimeoutError)
# Code deployed before CIS 353-355 are applied: a missing table, view or column is "not ready", never a 500.
_NOT_MIGRATED = (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError, asyncpg.UndefinedFunctionError)


class AccountUnavailable(Exception):  # noqa: N818 - reads as an outcome
    """No database connection right now, or CIS 353-355 not applied yet: 503."""


@asynccontextmanager
async def account_connection(pool: Any) -> AsyncIterator[Any]:
    """One READ ONLY transaction on a main-pool connection; :class:`AccountUnavailable` on a pool or connection
    error, or when the control-plane objects are missing."""
    if pool is None:
        raise AccountUnavailable("pool not configured")
    try:
        conn = await pool.acquire(timeout=ACQUIRE_TIMEOUT_S)
    except _POOL_ERRORS as exc:
        raise AccountUnavailable(type(exc).__name__) from exc
    try:
        async with conn.transaction(readonly=True):
            yield conn
    except (*_POOL_ERRORS, asyncpg.QueryCanceledError, *_NOT_MIGRATED) as exc:
        raise AccountUnavailable(type(exc).__name__) from exc
    finally:
        await pool.release(conn)


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """RFC 3339 in UTC with microseconds (the format the CIS 353 triggers write into the feed); None stays None."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def subscription_body(row: Any) -> dict:
    return {
        "product": row["product"],
        "plan_key": row["plan_key"],
        "status": row["status"],
        "trial_ends_at": iso_utc(row["trial_ends_at"]),
        "seats": row["seats"],
    }


def entitlement_body(row: Any) -> dict:
    return {
        "feature_key": row["feature_key"],
        "product": row["product"],
        "kind": row["value_kind"],
        "unit": row["unit"],
        "limit": row["limit_text"],
        "source": row["source"],
        "expires_at": iso_utc(row["expires_at"]),
    }


async def tenant_account(conn: Any, tenant_id: str) -> Optional[dict]:
    """The TenantAccount body without ``as_of`` (the caller adds it, so the ETag hashes the data only), or None when
    no tenant has this id."""
    tenant = await conn.fetchrow(ACCOUNT_SQL, tenant_id)
    if tenant is None:
        return None
    subscriptions = await conn.fetch(SUBSCRIPTIONS_SQL, tenant_id)
    entitlements = await conn.fetch(ENTITLEMENTS_SQL, tenant_id)
    return {
        "tenant_id": str(tenant["tenant_id"]),
        "account_status": tenant["account_status"],
        "suspend_reason": tenant["suspend_reason"],
        "in_service": tenant["account_status"] in IN_SERVICE,
        "kind": tenant["kind"],
        "is_pilot": tenant["is_pilot"] is True,
        "data_region": tenant["data_region"],
        "time_zone": tenant["time_zone"],
        "subscriptions": [subscription_body(r) for r in subscriptions],
        "entitlements": [entitlement_body(r) for r in entitlements],
    }
