"""AA-1160 — when the CRM last proved it holds a tenant's agency, as CIS sees it (``shared.tenant_crm_agency``,
CIS 475).

A tenant's TripPlanner leads go to the CRM only when the CRM can take them: the CRM add-on (``crm.core``) live AND a
live CRM agency for the tenant. A CRM agency (AA-Partner ``crm.tenant``) is made after the add-on, by the CIS CRM
onboarding (``crm_onboarding.onboard``) or the CRM's own console, and may never be; it may also be locked, offboarded or
deleted later. TripPlanner routes each lead on its own liveness signal, the CRM lead pull beacon. CIS reads no CRM table
(root ADR 0008a), so for its own display (the agency module's ``effective_channel``, Tenant 360) it records what the CRM
itself proves:

* ``crm_pull``: the CRM read the tenant.entitlements.v1 snapshot (``GET /tenant/v1/tenants/{id}/account``) with its M2M
  client. ``crm_entitlement_pull`` reads it only for an agency it holds (active or locked), on every account event that
  names the tenant and in an hourly pass over all of them (AA-Partner ``app/cis/entitlement_feed.py``);
* ``crm_onboarding``: the CIS onboarding found the agency (CRM ``GET /tenants/{id}`` 200) or created it.

:func:`record` upserts one row; a sighting within :data:`REFRESH_AFTER_S` of the stored one writes nothing (a write
throttle for a GET the CRM repeats, not a policy value). A row counts only while ``last_seen_at`` is younger than
``cis.tenant.crm_agency_seen_hours`` (:func:`seen_hours`, Platform › Settings, default 3): the CRM re-reads every agency
it holds at least hourly, so a deleted or offboarded agency, never read again, ages out. Readers: :func:`has_agency`
(Tenant 360) and :func:`services.tenant_account.tripplanner_site.module_view` (``effective_channel``, with
:data:`LIVE_SQL`). The row goes with the tenant (AA-839 erasure inventory, FK cascade). Best effort: a failed write is
logged and never fails the caller's request.
"""
from __future__ import annotations

from typing import Any

import structlog

from shared import settings
from services.tenant_account.crm_agency_settings import CRM_AGENCY_SEEN_HOURS

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
# The live-sighting test, for a query whose tenant is ``t`` and whose window in hours is parameter ``${n}``.
LIVE_SQL = ("EXISTS (SELECT 1 FROM shared.tenant_crm_agency a WHERE a.tenant_id = t.tenant_id "
            "AND a.last_seen_at > now() - make_interval(hours => ${n}))")
EXISTS_SQL = ("SELECT " + LIVE_SQL.format(n=2) + " AS ok FROM shared.tenants t WHERE t.tenant_id = $1::uuid")


def seen_hours() -> int:
    """How many hours a CRM sighting counts (cis.tenant.crm_agency_seen_hours, Platform › Settings)."""
    return int(settings.get(CRM_AGENCY_SEEN_HOURS))


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
    """True when the CRM proved the tenant's agency within :func:`seen_hours` (False for no such tenant)."""
    return bool(await conn.fetchval(EXISTS_SQL, str(tenant_id), seen_hours()))
