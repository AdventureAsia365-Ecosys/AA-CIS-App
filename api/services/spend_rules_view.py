"""AA-1126 (S283 review fix): the AI spend rules each app follows and the TripPlanner caps, as the admin pages that show
an app's caps read them, so those pages say what the code enforces (tech lead S283: the threshold pages stay in sync).

- The spend environment of each app (dev | prod): ``cis.spend.environment`` and ``tripplanner.spend.environment`` from
  the CIS registry (CIS 447), ``crm.spend.environment`` from the CRM's own registry (crm-076, crm.ops.v1 ``/settings``,
  relayed with the staff member's own token). Under prod the app-wide daily caps of TripPlanner and the CRM (AI
  gateway › App profiles, the TripPlanner stage caps, the chat's monthly app cap) only alert; under dev they stop the
  AI of every agency. None = not known (the CRM did not answer, Booking has no AI).
- The TripPlanner stage caps and near-cap share (CIS 447 ``tripplanner.spend.stage_cap_*_usd`` / ``near_cap_pct``):
  the values TripPlanner reads through ``catalog.v_app_settings_v1``. They replace the constants CIS used to mirror
  (``api/core/app_registry.TP_STAGE_CAPS_USD`` / ``TP_NEAR_CAP_RATIO``), which nothing kept in step.

A read never raises: a key that is not registered yet (CIS 447 not applied) reads as unknown (None).
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from shared import settings

logger = structlog.get_logger()

DEV, PROD = "dev", "prod"
CIS_ENV_KEY = "cis.spend.environment"
TP_ENV_KEY = "tripplanner.spend.environment"
CRM_ENV_KEY = "crm.spend.environment"
TP_STAGE_KEYS = {"tp_compose": "tripplanner.spend.stage_cap_compose_usd",
                 "tp_search_embed": "tripplanner.spend.stage_cap_search_usd"}
TP_NEAR_KEY = "tripplanner.spend.near_cap_pct"
# The apps whose daily budget on AI gateway › App profiles is their app-wide daily cap (CIS uses its Budgets cap).
PROFILE_CAP_APPS = ("tripplanner", "crm")


def environment_of(value: Any) -> Optional[str]:
    """'prod' / 'dev' from a setting's value; None when there is no value."""
    if value is None:
        return None
    return PROD if str(value).strip().lower() == PROD else DEV


def _setting(key: str) -> Any:
    try:
        return settings.get(key)
    except Exception as e:  # noqa: BLE001 — a key not registered yet (its migration not applied): unknown
        logger.warning("spend_rules_setting_unreadable", key=key, error=type(e).__name__)
        return None


def environment(key: str) -> Optional[str]:
    return environment_of(_setting(key))


def tp_stage_caps() -> dict[str, Optional[float]]:
    """Each TripPlanner online stage's daily cap in USD; None = no stage cap (0, the seed) or not known."""
    out: dict[str, Optional[float]] = {}
    for stage, key in TP_STAGE_KEYS.items():
        value = _setting(key)
        out[stage] = float(value) if value is not None and float(value) > 0 else None
    return out


def tp_near_ratio() -> Optional[float]:
    """The share of a TripPlanner cap at which it warns (near_cap_pct / 100); None when not known."""
    value = _setting(TP_NEAR_KEY)
    return float(value) / 100.0 if value is not None else None


async def crm_environment(authorization: Optional[str]) -> Optional[str]:
    """The CRM's spend environment, read from its registry through crm.ops.v1 with the staff member's token; None when
    the CRM does not answer (never raises)."""
    from api.services import crm_console

    try:
        status, body = await crm_console.crm_ops_call("GET", "/settings", authorization, params={"area": "spend"})
    except Exception as e:  # noqa: BLE001 — the page still renders without it
        logger.warning("crm_spend_environment_unreadable", error=type(e).__name__)
        return None
    if status != 200:
        return None
    for row in body.get("settings") or []:
        if isinstance(row, dict) and row.get("key") == CRM_ENV_KEY:
            return environment_of(row.get("value"))
    return None


async def app_environments(authorization: Optional[str]) -> dict[str, Optional[str]]:
    return {"cis": environment(CIS_ENV_KEY), "tripplanner": environment(TP_ENV_KEY),
            "crm": await crm_environment(authorization), "booking": None}


def cap_rule(app: str, env: Optional[str]) -> Optional[str]:
    """What the app's daily budget on App profiles does when reached: 'alert_only' (prod), 'stops_ai' (dev), or None
    (not an app-wide cap of that app, or the rules are not known). Pure."""
    if app not in PROFILE_CAP_APPS or env is None:
        return None
    return "alert_only" if env == PROD else "stops_ai"
