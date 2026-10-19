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

``cost_tags``: values of the ``App`` cost-allocation tag that belong to the app (Cost Explorer GroupBy TAG App; an
untagged resource has the empty value). Tags that belong to no single app (``shared``, ``identity``, ``platform``)
are reported as their own rows by the cost module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

GITHUB_ORG = "AdventureAsia365-Ecosys"


@dataclass(frozen=True)
class AppInfo:
    id: str
    label: str
    repo: str
    cost_tags: tuple[str, ...]
    llm_app: Optional[str] = None            # quality_signal->>'app' of its shared.llm_call_log rows
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
