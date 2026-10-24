"""The served reads of tenant.membership.v1 (AA-978): one agency's snapshot and one identity's memberships.

Reads run on the CIS main pool (``app.state.pool``, role cis_app, the only role CIS 276 grants these tables): the
objects are in ``shared``, where ``aa_app_user`` has no grant (AA-859 invariant), and the CRM never reads the
database (root ADR 0008a decision 1). Each request is one READ ONLY transaction.

Bodies follow the contract (root ``docs/contracts/tenant.membership.v1.openapi.yaml``, byte copy in this repo's
``docs/contracts/``): ids, the identity (iss, sub) and the Content studio role only; never an e-mail, a name or a
CRM role (the table has no column for any of them).

- The snapshot lists the memberships that are not removed (invited and active). Removals reach the CRM through the
  feed; a member missing from the snapshot is not a member.
- ``tenant_state``: ``deleted`` when the agency is offboarded (``cancelled_at``), ``locked`` when it is not active,
  else ``active`` (the same mapping as the CIS 276 tenant trigger).
- The lookup is keyed by iss AND sub (ADR 0006 decision 9), never sub alone.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import asyncpg

ACQUIRE_TIMEOUT_S = 5.0
MAX_ISS = 300
MAX_SUB = 128

_MEMBER_COLS = ("membership_id, tenant_id, cognito_iss, cognito_sub, content_studio_role, status, invitation_id, "
                "updated_at")

TENANT_SQL = "SELECT tenant_id, is_active, cancelled_at FROM shared.tenants WHERE tenant_id = $1::uuid"
TENANT_MEMBERS_SQL = f"""
    SELECT {_MEMBER_COLS}
      FROM shared.tenant_members
     WHERE tenant_id = $1::uuid AND status <> 'removed'
     ORDER BY created_at, membership_id
"""
IDENTITY_SQL = f"""
    SELECT {_MEMBER_COLS}
      FROM shared.tenant_members
     WHERE cognito_iss = $1 AND cognito_sub = $2 AND status <> 'removed'
     ORDER BY created_at, membership_id
"""

_POOL_ERRORS = (asyncpg.PostgresConnectionError, asyncpg.TooManyConnectionsError, asyncpg.InterfaceError,
                OSError, asyncio.TimeoutError)


class MembershipUnavailable(Exception):  # noqa: N818 - reads as an outcome
    """No database connection right now (pool missing or at its limit, the database unreachable, or the CIS 276
    objects missing): 503."""


class InvalidRequest(ValueError):
    """A parameter or cursor the contract does not accept: 400 ``invalid``."""


@asynccontextmanager
async def membership_connection(pool: Any) -> AsyncIterator[Any]:
    """One READ ONLY transaction on a main-pool connection; :class:`MembershipUnavailable` on a pool or connection
    error, or when CIS 276 is not applied yet (code deployed before the migration)."""
    if pool is None:
        raise MembershipUnavailable("pool not configured")
    try:
        conn = await pool.acquire(timeout=ACQUIRE_TIMEOUT_S)
    except _POOL_ERRORS as exc:
        raise MembershipUnavailable(type(exc).__name__) from exc
    try:
        async with conn.transaction(readonly=True):
            yield conn
    except (*_POOL_ERRORS, asyncpg.QueryCanceledError, asyncpg.UndefinedTableError) as exc:
        raise MembershipUnavailable(type(exc).__name__) from exc
    finally:
        await pool.release(conn)


# ── bodies ────────────────────────────────────────────────────────────────────

def iso_utc(value: datetime) -> str:
    """RFC 3339 in UTC, microseconds always written: the format the CIS 276 trigger writes into the feed."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def tenant_state(is_active: bool, cancelled_at: Optional[datetime]) -> str:
    if cancelled_at is not None:
        return "deleted"
    return "active" if is_active else "locked"


def membership_body(row: Any) -> dict:
    """The contract Membership of one ``shared.tenant_members`` row."""
    identity = None
    if row["cognito_iss"] is not None:
        identity = {"iss": row["cognito_iss"], "sub": row["cognito_sub"]}
    return {
        "membership_id": str(row["membership_id"]),
        "tenant_id": str(row["tenant_id"]),
        "identity": identity,
        "content_studio_role": row["content_studio_role"],
        "status": row["status"],
        "invitation_id": str(row["invitation_id"]) if row["invitation_id"] is not None else None,
        "updated_at": iso_utc(row["updated_at"]),
    }


# ── reads ─────────────────────────────────────────────────────────────────────

def parse_tenant_id(value: str) -> Optional[str]:
    """The canonical uuid text, or None for a value that cannot be a tenant id (answered 404, like an unknown id)."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def tenant_members(conn: Any, tenant_id: str) -> Optional[dict]:
    """The TenantMembers body without ``as_of`` (the caller adds it, so the ETag hashes the data only), or None
    when no tenant has this id."""
    tenant = await conn.fetchrow(TENANT_SQL, tenant_id)
    if tenant is None:
        return None
    rows = await conn.fetch(TENANT_MEMBERS_SQL, tenant_id)
    return {
        "tenant_id": str(tenant["tenant_id"]),
        "tenant_state": tenant_state(tenant["is_active"], tenant["cancelled_at"]),
        "members": [membership_body(r) for r in rows],
    }


def check_identity(iss: Optional[str], sub: Optional[str]) -> tuple[str, str]:
    """Both parts are required (never sub alone); iss is an https URI as the pool publishes it."""
    if not iss or not sub:
        raise InvalidRequest("iss and sub are both required")
    if len(iss) > MAX_ISS or not iss.startswith("https://") or any(c.isspace() for c in iss):
        raise InvalidRequest(f"iss must be an https URI of at most {MAX_ISS} characters")
    if len(sub) > MAX_SUB:
        raise InvalidRequest(f"sub must be at most {MAX_SUB} characters")
    return iss, sub


async def identity_memberships(conn: Any, iss: str, sub: str) -> dict:
    rows = await conn.fetch(IDENTITY_SQL, iss, sub)
    return {"identity": {"iss": iss, "sub": sub}, "memberships": [membership_body(r) for r in rows]}
