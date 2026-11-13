"""AA-1093 lane L2 — the knobs of the tenant erasure saga, all in the platform settings registry (Platform › Settings,
shared/settings; tech lead S279: a window, validity or switch is a setting, never a code literal or an env var alone):

  * stage 1 window (CIS, Booking, TripPlanner): cis.erasure.tenant_retention_days (CIS 407, 30 days = decision D1),
    read through erasure_job.tenant_retention_days, so the saga and the data_erasure job never disagree on the CIS date;
  * stage 2 window (the CRM): cis.erasure.crm_window_days (CIS 411, 90 days = decision D2);
  * how long a combined fingerprint may be approved, and how long an approval stays valid:
    cis.erasure.saga_approval_valid_hours (CIS 411, 24 hours = decision D7);
  * the apply switch: cis.erasure.saga_apply_enabled (CIS 411, default off). The CIS apply also needs the data_erasure
    job's own switch (ERASURE_APPLY_ENABLED), so turning the saga on alone never erases CIS rows.
A drill (mode drill, synthetic tenants only) uses window 0 for every app.
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Any, Optional

from shared import settings as _settings

CRM_WINDOW_SETTING = "cis.erasure.crm_window_days"
APPROVAL_VALID_SETTING = "cis.erasure.saga_approval_valid_hours"
APPLY_SETTING = "cis.erasure.saga_apply_enabled"
# The approved decisions (D2, D7, switch off), used only by a build whose compiled defaults lack the key (before CIS
# 411's seed is in the tree); everywhere else the registry answers, its default being the same value.
_SEED = {CRM_WINDOW_SETTING: 90, APPROVAL_VALID_SETTING: 24, APPLY_SETTING: False}

APPS = ("cis", "booking", "tripplanner", "crm")


def setting(key: str) -> Any:
    try:
        return _settings.get(key)
    except KeyError:
        return _SEED[key]


def apply_enabled() -> bool:
    return setting(APPLY_SETTING) is True


def windows(mode: str, env: Optional[dict] = None) -> dict[str, timedelta]:
    """The exit window per app: 0 for a drill; else stage 1 = the CIS tenant retention, stage 2 = the CRM window.
    ``env`` (tests): passed to erasure_job.tenant_retention_days (its emergency override)."""
    if mode == "drill":
        return {app: timedelta(0) for app in APPS}
    from services.jobs import erasure_job as ej   # lazy: loading a router must not import every job module

    others = timedelta(days=ej.tenant_retention_days(env))
    return {"cis": others, "booking": others, "tripplanner": others,
            "crm": timedelta(days=int(setting(CRM_WINDOW_SETTING)))}


def approval_validity() -> timedelta:
    return timedelta(hours=max(1, int(setting(APPROVAL_VALID_SETTING))))


def environment(env: Optional[dict] = None) -> str:
    return ((env if env is not None else os.environ).get("ENVIRONMENT") or "").strip().lower()
