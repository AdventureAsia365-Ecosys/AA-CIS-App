"""AA-1160 — the tenants the CRM holds an agency for, as CIS sees it (``shared.tenant_crm_agency``, CIS 475).

A tenant's TripPlanner leads go to the CRM only when the CRM can take them: the CRM add-on (``crm.core``) live AND a CRM
agency for the tenant. A CRM agency (AA-Partner ``crm.tenant``) is made after the add-on, by the CIS CRM onboarding
(``crm_onboarding.onboard``) or the CRM's own console, and may never be; CIS reads no CRM table (root ADR 0008a). So CIS
records what the CRM itself proves:

* ``crm_pull``: the CRM read the tenant.entitlements.v1 snapshot (``GET /tenant/v1/tenants/{id}/account``) with its M2M
  client. ``crm_entitlement_pull`` reads it only for an agency it holds (active or locked), on every account event that
  names the tenant and in an hourly pass over all of them (AA-Partner ``app/cis/entitlement_feed.py``);
* ``crm_onboarding``: the CIS onboarding found the agency (CRM ``GET /tenants/{id}`` 200) or created it.

:func:`record` upserts one row; a sighting within :data:`REFRESH_AFTER_S` of the stored one writes nothing (a write
throttle for a GET the CRM repeats, not a policy value). Readers: ``catalog.v_tenant_tripplanner_v1.crm_agency``
(TripPlanner) and :func:`services.tenant_account.tripplanner_site.module_view` (``effective_channel``). The row goes
with the tenant (AA-839 erasure inventory, FK cascade). Best effort: a failed write is logged and never fails the
caller's request.
"""
from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

SOURCES = ("crm_pull", "crm_onboarding")
REFRESH_AFTER_S = 300     # technical-constant: write throttle of a repeated sighting (the marker has no expiry)

UPSERT_SQL = """
    INSERT INTO shared.tenant_crm_agency (tenant_id, source)
    SELECT t.tenant_id, $2 FROM shared.tenants t WHERE t.tenant_id = $1::uuid
    ON CONFLICT (tenant_id) DO UPDATE
       SET last_seen_at = now(), source = EXCLUDED.source
     WHERE shared.tenant_crm_agency.last_seen_at < now() - make_interval(secs => $3)
        OR shared.tenant_crm_agency.source IS DISTINCT FROM EXCLUDED.source
"""
EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.tenant_crm_agency WHERE tenant_id = $1::uuid) AS ok"


async def record(conn: Any, tenant_id: str, source: str) -> bool:
    """Record that the CRM holds an agency for ``tenant_id``; True when a row was written. Raises on a database
    error (callers use :func:`record_quietly`)."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    status = await conn.execute(UPSERT_SQL, str(tenant_id), source, float(REFRESH_AFTER_S))
    return str(status).rsplit(" ", 1)[-1] not in ("0", "")


async def record_quietly(pool: Any, tenant_id: str, source: str) -> bool:
    """:func:`record` on its own pooled connection; a failure (no pool, no table yet, a lost connection) is logged and
    answered False, never raised: the sighting is a side effect of the caller's request."""
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            written = await record(conn, tenant_id, source)
    except Exception as exc:  # noqa: BLE001 — never fail the snapshot read or the onboarding for the marker
        logger.warning("crm_agency_marker_failed", tenant_id=str(tenant_id), source=source,
                       error=type(exc).__name__)
        return False
    if written:
        logger.info("crm_agency_marker", tenant_id=str(tenant_id), source=source)
    return written


async def has_agency(conn: Any, tenant_id: str) -> bool:
    """True when CIS has seen a CRM agency for the tenant."""
    return bool(await conn.fetchval(EXISTS_SQL, str(tenant_id)))
