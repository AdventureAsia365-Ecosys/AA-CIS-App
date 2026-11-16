"""AA-1116 (CIS 436) — the AI budget alert / stop of a tenant without its own AA-1016 row.

The thresholds are settings on Platform › Settings (area billing, maker-checker): ``cis.billing.ai_alert_pct_default``,
``cis.billing.ai_approval_pct_default`` and ``cis.billing.ai_hard_stop_default`` (80 / 100 / on: the included spend is
the ceiling). They replaced the PLAN_ALERT_PCT / PLAN_APPROVAL_PCT constants of shared/cost_guard.py and the 100 / 150
defaults of a new AA-1016 row. Read per check, so an approved change applies at once.
"""
from __future__ import annotations

from shared import settings


def plan_budget_defaults() -> tuple[int, int, bool]:
    """(alert %, approval %, hard stop) in force."""
    return (int(settings.get("cis.billing.ai_alert_pct_default")),
            int(settings.get("cis.billing.ai_approval_pct_default")),
            bool(settings.get("cis.billing.ai_hard_stop_default")))
