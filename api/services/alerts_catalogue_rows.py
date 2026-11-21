"""AA-1171 — the row shape of Platform › Alerts › Catalogue and its vocabulary (types, how a threshold is changed,
app labels), shared by api/services/alerts_catalogue.py and api/services/alerts_catalogue_budgets.py."""
from __future__ import annotations

from typing import Any

INFRA_REPO = "AdventureAsia365-Ecosys/AA-CIS-Infra"
SETTINGS_PAGE = "/admin/platform/settings"
OWNER_ROLE = "tech lead"

TYPE_METRIC = "metric"
TYPE_METRIC_MATH = "metric_math"
TYPE_COMPOSITE = "composite"
TYPE_EVENTBRIDGE = "eventbridge"
TYPE_BUDGET = "budget"
TYPE_ANOMALY = "cost_anomaly"
TYPE_IN_APP = "in_app_check"
TYPES = (TYPE_METRIC, TYPE_METRIC_MATH, TYPE_COMPOSITE, TYPE_EVENTBRIDGE, TYPE_BUDGET, TYPE_ANOMALY, TYPE_IN_APP)

EDIT_SETTINGS = "settings"
EDIT_TERRAFORM = "terraform"
EDIT_NONE = "none"

APP_LABELS = {"cis": "CIS", "booking": "Booking", "tripplanner": "TripPlanner", "crm": "Partner CRM",
              "identity": "Identity", "shared": "Shared platform", "ecosystem": "Ecosystem"}
KNOWN_APPS = tuple(APP_LABELS)


def row(**kw: Any) -> dict:
    """One catalogue row: every key present (None / [] when unknown), ``app_label`` from the app."""
    base = {"id": None, "name": None, "app": None, "app_label": None, "component": None, "type": None,
            "source": None, "severity": None, "mutable": None, "condition": None, "threshold": None,
            "threshold_source": None, "editable_via": EDIT_NONE, "edit_href": None, "channel": [],
            "runbook": None, "owner_role": OWNER_ROLE, "state": None, "state_since": None, "last_alert_at": None,
            "account_id": None, "freshness": None, "coverage_gap": None, "description": None}
    base.update(kw)
    base["app_label"] = APP_LABELS.get(base["app"] or "", base["app"] or "Unknown")
    return base
