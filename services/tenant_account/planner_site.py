"""AA-1115 (root ADR 0019 d10, plan entitlement ``tp.launcher``): the addresses of an agency's hosted Trip planner, for
the launcher snippet on the Content studio's Add-ons page (AA-Partner ``web/lib/plannerLauncher.ts``).

``GET /v1/account/add-ons`` adds ``planner`` to the tripplanner entry when that add-on is active
(``api/routers/v1_account_addons.py``):

- ``url``: the hosted planner ``<TripPlanner origin>/<slug>/plan``. Only when the tenant is in service and its plan
  includes ``tp.hosted`` (the same rule as ``served`` of ``catalog.v_tenant_tripplanner_v1``, CIS 379 / 438):
  otherwise the page would be a 404, and the add-on has no ``planner`` at all;
- ``domains``: the tenant's ACTIVE custom planner hosts (``shared.tenant_tripplanner_domain``, CIS 428), sorted;
- ``launcher``: the plan includes ``tp.launcher`` (the Add-ons page shows the snippet only then).

The TripPlanner origin is the one of ``TRIPPLANNER_REVALIDATE_URL`` (the public TripPlanner host, set by AA-CIS-Infra
for the catalogue purge job, ``services/jobs/tripplanner_purge_job.py``), else the Dev host. No new setting: it is an
address, not a threshold. Runs on the main pool (cis_app reads the three relations; the domain table is platform-only,
so the tenant is filtered here, as everywhere it is read).
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional
from urllib.parse import urlsplit

DEFAULT_ORIGIN = "https://tripplanner.lumiguides.it.com"
ORIGIN_ENV = "TRIPPLANNER_REVALIDATE_URL"
HOSTED = "tp.hosted"
LAUNCHER = "tp.launcher"
FEATURES = (HOSTED, LAUNCHER)

# The TripPlanner slug shape (AA-TripPlanner-Web lib/tenantSlug.ts): lower-case words joined by single hyphens.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")

SQL = """
    SELECT t.slug::text AS slug,
           ARRAY(SELECT DISTINCT r.feature_key FROM shared.v_tenant_entitlement_resolved r
                  WHERE r.tenant_id = t.tenant_id AND r.feature_key = ANY($2::text[])) AS features,
           ARRAY(SELECT d.domain FROM shared.tenant_tripplanner_domain d
                  WHERE d.tenant_id = t.tenant_id AND d.status = 'active'
                  ORDER BY d.domain) AS domains
      FROM shared.tenants t
     WHERE t.tenant_id = $1::uuid
"""


def planner_origin() -> str:
    """The public TripPlanner origin (scheme and host of ``TRIPPLANNER_REVALIDATE_URL``), https only; else the Dev
    host."""
    raw = (os.environ.get(ORIGIN_ENV) or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return DEFAULT_ORIGIN
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return DEFAULT_ORIGIN
    return f"https://{parts.netloc.lower()}"


def planner_site(slug: Any, features: Any, domains: Any, *, in_service: bool, origin: str) -> Optional[dict]:
    """The ``planner`` of the tripplanner entry, or None (not in service, no ``tp.hosted``, or a slug the planner
    cannot serve). Pure."""
    have = set(features or ())
    if not in_service or HOSTED not in have or not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        return None
    return {"url": f"{origin}/{slug}/plan", "domains": sorted({str(d) for d in (domains or ()) if d}),
            "launcher": LAUNCHER in have}


async def read(conn, tenant_id: str, *, in_service: bool) -> Optional[dict]:
    """The tenant's planner addresses (see :func:`planner_site`); None for an unknown tenant."""
    row = await conn.fetchrow(SQL, tenant_id, list(FEATURES))
    if row is None:
        return None
    return planner_site(row["slug"], row["features"], row["domains"], in_service=in_service, origin=planner_origin())
