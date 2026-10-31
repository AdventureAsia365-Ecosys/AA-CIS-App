"""AA-884 (S241 GĐ7) — the backend registry of the ecosystem's apps, for /admin/platform/apps and cost per app.

Code constants, not env vars (Dev only today; a hostname change is a PR). The frontend has its own link registry
(frontend/lib/apps.ts); this one holds what the BACKEND needs to probe an app or attribute its spend:

- ``cis``: this repo; ECS services api + worker (their SHA parity comes from /health and shared.job_worker, never
  from ECS describe calls — no new IAM).
- ``booking``: probed over HTTP only (``/booking/health`` and ``/booking/health/ready``, public). CIS never reads the
  ``booking`` schema (S241 constraint: no cross-schema reads).
- ``tripplanner``: no health endpoint yet (GĐ9, deferred). Its card shows today's customer-facing LLM spend against
  the daily cap, read from ``shared.llm_call_log`` (a CIS table TP writes, marked ``quality_signal.app``).
  ``ONLINE_STAGES`` and ``DEFAULT_DAILY_CAP_USD`` MIRROR AA-TripPlanner-Web ``backend/shared/llm_gateway.py``
  ``ONLINE_STAGES`` and ``backend/config.py`` ``TP_DAILY_SPEND_CAP_USD`` (default "5.0"); the live cap is a TP Lambda
  env var CIS cannot see, so the page labels it "default cap". If TP changes either, change it here.

- ``crm`` (AA-936, AA-Partner, root ADR 0010): probed over HTTP like Booking (``/crm/health`` and
  ``/crm/health/ready`` on the partner host, public). Its jobs come from the CRM's ``crm.ops.v1`` contract
  (``GET /crm/ops/v1/jobs/summary``, ``extra["ops_base"]``), called with the signed-in staff member's own WORKFORCE
  ``cis-admin`` token (the route class the CRM accepts on ``/crm/ops/v1``); CIS never reads schema ``crm``. CRM LLM
  calls are logged by the CRM's own gateway (``crm.llm_call``, D12), not in ``shared.llm_call_log``, so ``llm_app``
  is None.

``cost_tags``: values of the ``App`` cost-allocation tag that belong to the app (Cost Explorer GroupBy TAG App; an
untagged resource has the empty value). Tags that belong to no single app (``shared``, ``identity``, ``platform``)
are reported as their own rows by the cost module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

GITHUB_ORG = "AdventureAsia365-Ecosys"


@dataclass(frozen=True)
class AppInfo:
    id: str
    label: str
    repo: str
    cost_tags: tuple[str, ...]
    llm_app: Optional[str] = None            # coalesce(app, quality_signal->>'app') of its llm_call_log rows
    health_base: Optional[str] = None
    health_paths: tuple[str, ...] = ()
    ecs_services: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{GITHUB_ORG}/{self.repo}"

    def commit_url(self, sha: Optional[str]) -> Optional[str]:
        if not sha or sha == "unknown":
            return None
        return f"{self.repo_url}/commit/{sha}"


# AA-TripPlanner-Web backend/shared/llm_gateway.py ONLINE_STAGES (mirrored, see module docstring).
TP_ONLINE_STAGES: tuple[str, ...] = ("tp_compose", "tp_search_embed")
# AA-TripPlanner-Web backend/config.py TP_DAILY_SPEND_CAP_USD default (mirrored; the live value may differ).
TP_DEFAULT_DAILY_CAP_USD = 5.0
# The CIS API's public origin (API Gateway custom domain): the OPS-12 digest probes /health + /health/ready from the
# worker the way an outside caller sees them (AA-1003 R2). The Dev default; PUBLIC_API_BASE (the variable
# services/photos/sync.py already reads for the same origin) overrides it, so another environment needs no code change.
CIS_API_ORIGIN = "https://api-cis.lumiguides.it.com"
CIS_HEALTH_PATHS: tuple[str, str] = ("/health", "/health/ready")


def cis_api_origin() -> str:
    return (os.environ.get("PUBLIC_API_BASE") or CIS_API_ORIGIN).strip().rstrip("/") or CIS_API_ORIGIN
# AA-Partner's Dev partner host (CloudFront, AA-CIS-Infra crm_edge.tf).
CRM_PARTNER_ORIGIN = "https://partner-dev.lumiguides.it.com"

APPS: dict[str, AppInfo] = {
    "cis": AppInfo(
        id="cis", label="CIS (content + catalog)", repo="AA-CIS-App", cost_tags=("cis",), llm_app="cis",
        ecs_services=("aa-cis-dev-api", "aa-cis-dev-worker"),
    ),
    "booking": AppInfo(
        id="booking", label="Booking", repo="AA-Booking", cost_tags=("booking",), llm_app="booking",
        health_base="https://api-booking.lumiguides.it.com",
        health_paths=("/booking/health", "/booking/health/ready"),
    ),
    "tripplanner": AppInfo(
        id="tripplanner", label="TripPlanner", repo="AA-TripPlanner-Web", cost_tags=("tripplanner",),
        llm_app="tripplanner",
        extra={"online_stages": TP_ONLINE_STAGES, "default_cap_usd": TP_DEFAULT_DAILY_CAP_USD},
    ),
    # AA-936 (card CRM-08 "CIS AppInfo('crm') + jobs tab"): the partner CRM. AWS resources carry App = crm.
    "crm": AppInfo(
        id="crm", label="Partner CRM", repo="AA-Partner", cost_tags=("crm",),
        health_base=CRM_PARTNER_ORIGIN,
        health_paths=("/crm/health", "/crm/health/ready"),
        extra={"ops_base": f"{CRM_PARTNER_ORIGIN}/crm/ops/v1"},
    ),
}

# App-tag values shared by several apps; shown as their own cost rows, never split.
SHARED_COST_TAGS: tuple[str, ...] = ("shared", "identity", "platform")

APP_IDS: tuple[str, ...] = tuple(APPS)


def app_for_cost_tag(tag: str) -> Optional[str]:
    """The app id that owns an App-tag value, or None (shared / unknown / untagged)."""
    for app in APPS.values():
        if tag in app.cost_tags:
            return app.id
    return None
