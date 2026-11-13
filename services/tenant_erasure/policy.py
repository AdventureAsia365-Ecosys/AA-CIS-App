"""AA-1093 lane L2 — the knobs of the tenant erasure saga.

Where each value comes from today (the settings registry reader of S279 lane L1 is not on main yet; moving these
onto it is a recorded follow-up, never a new env var):
  * the exit window of CIS, Booking and TripPlanner: the CIS tenant retention window of the data_erasure job
    (erasure_job.tenant_retention_days, ERASURE_TENANT_RETENTION_DAYS, default 30 = decision D1). The same value gates
    the CIS apply itself, so the saga can never schedule CIS earlier than the job allows.
  * the CRM window: decision D2 (90 days; AA erases the CRM data after it without the owner's click). Follow-up:
    setting cis.erasure.crm_window_days.
  * how long a combined fingerprint may be approved, and how long an approval stays valid: the DRY RUN validity of
    the data_erasure job (erasure_job.dry_run_max_age, ERASURE_DRY_RUN_MAX_AGE_HOURS, default 24 h = decision D7).
  * the apply switch: default OFF (TENANT_ERASURE_SAGA_APPLY must be "on" / "true" on the api and the worker; follow-up:
    setting cis.erasure.saga_apply_enabled). The CIS apply also needs ERASURE_APPLY_ENABLED (the data_erasure job's own
    switch), so turning the saga on alone never erases CIS rows.
A drill (mode drill, synthetic tenants only) uses window 0 for every app.
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Optional

APPLY_SWITCH_ENV = "TENANT_ERASURE_SAGA_APPLY"
# Decision D2 (S279 plan). Follow-up: the settings registry key cis.erasure.crm_window_days (no env var on purpose).
CRM_WINDOW = timedelta(days=90)

APPS = ("cis", "booking", "tripplanner", "crm")


def apply_enabled(env: Optional[dict] = None) -> bool:
    raw = ((env if env is not None else os.environ).get(APPLY_SWITCH_ENV) or "off").strip().lower()
    return raw in ("on", "true", "1")


def windows(mode: str, env: Optional[dict] = None) -> dict[str, timedelta]:
    """The exit window per app: 0 for a drill; else the CRM window and the CIS retention window for the rest."""
    if mode == "drill":
        return {app: timedelta(0) for app in APPS}
    from services.jobs import erasure_job as ej   # lazy: loading a router must not import every job module

    others = timedelta(days=ej.tenant_retention_days(env))
    return {"cis": others, "booking": others, "tripplanner": others, "crm": CRM_WINDOW}


def approval_validity(env: Optional[dict] = None) -> timedelta:
    from services.jobs import erasure_job as ej

    return ej.dry_run_max_age(env)


def environment(env: Optional[dict] = None) -> str:
    return ((env if env is not None else os.environ).get("ENVIRONMENT") or "").strip().lower()
