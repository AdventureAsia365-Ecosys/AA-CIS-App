"""AA-1126 (S283) — the Spend controls section of AI gateway › Budgets: the gateway's spend controls of this
environment, read from the settings registry (one source of truth: the values the code reads, edited only on
Platform › Settings, area Spend), and the anomaly breaker of every app with its reset.

- ``read(pool)``: each key of ``shared/llm_client/caps_settings.ALL`` with its label, unit, value in force, default and
  where the value comes from (``shared.settings.resolve``), the Platform › Settings link that edits it, and the anomaly
  breaker's verdict per app (``anomaly_breaker.status``), and ``run_cap_scopes`` (the central budget scopes whose
  per-run cap is one of these settings, shown on the Budgets tab's central caps instead of a per-run amount). A key the compiled defaults do not have yet (the code shipped
  before CIS 446) is listed under ``missing`` and the gateway keeps today's behaviour for it.
- ``reset(pool, app, actor)``: the admin reset of one app's breaker (``app_profile.reset_anomaly``), audited with the
  verdict it cleared.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

import structlog

from shared import settings
from shared.llm_client import anomaly_breaker, app_profile, caps_settings

logger = structlog.get_logger()

SETTINGS_PAGE = "/admin/platform/settings"
AREA = "spend"


def settings_href(key: Optional[str] = None) -> str:
    """The Platform › Settings link of the Spend area of CIS (one key: its search filled in). Pure."""
    q = {"app": "cis", "area": AREA, **({"key": key} if key else {})}
    return f"{SETTINGS_PAGE}?{urlencode(q)}"


def setting_rows() -> tuple[list[dict], list[str]]:
    """(the rows of every registered spend-control key, the keys not registered yet). Never raises."""
    rows, missing = [], []
    for key in caps_settings.ALL:
        try:
            d = settings.definition(key)
            res = settings.resolve(key)
        except KeyError:
            missing.append(key)
            continue
        rows.append({"key": key, "label": d.label, "description": d.description, "value_type": d.value_type,
                     "unit": d.unit, "allowed_values": list(d.allowed_values) if d.allowed_values is not None else None,
                     "min_value": d.min_value, "max_value": d.max_value, "default_value": d.default_value,
                     "value": res.value, "source": res.source, "version": res.version, "href": settings_href(key)})
    return rows, missing


async def read(pool: Any) -> dict:
    try:
        await settings.READER.refresh(pool=pool)
    except Exception as e:  # noqa: BLE001 — the last good snapshot is shown
        logger.warning("ai_gateway_spend_controls_settings_refresh_failed", error=str(e)[:200])
    rows, missing = setting_rows()
    anomaly: dict = {"mode": caps_settings.anomaly_mode(), "policy": None, "apps": []}
    error = None
    try:
        async with pool.acquire() as conn:
            anomaly = await anomaly_breaker.status(conn)
    except Exception as e:  # noqa: BLE001 — the settings still show
        error = f"anomaly breaker: {str(e)[:200]}"
        logger.warning("ai_gateway_spend_controls_anomaly_failed", error=str(e)[:300])
    return {"settings": rows, "missing": missing, "settings_href": settings_href(),
            "connection_cap_mode": caps_settings.connection_cap_mode(), "anomaly": anomaly,
            "run_cap_scopes": run_cap_scopes(), "errors": [error] if error else []}


def run_cap_scopes() -> dict:
    """The central budget scopes whose per-run cap is a setting (review S283, one source of truth) -> that setting's
    key: the Budgets tab's central caps show the setting there and its editor takes no per-run cap for them. Pure."""
    return {scope: caps_settings.RUN_CAP_KEYS[cls] for scope, cls in caps_settings.RUN_CAP_SCOPES.items()}


async def reset(pool: Any, app: str, *, actor: str) -> dict:
    """Reset ``app``'s anomaly breaker (audited with the verdict it cleared). LookupError: not an app with AI."""
    if app not in anomaly_breaker.APPS:
        raise LookupError(f"no anomaly breaker for app {app!r}")
    cleared: dict = {}
    policy = caps_settings.anomaly_policy()
    if policy is not None:
        try:
            async with pool.acquire() as conn:
                profiles = await app_profile.read_all(conn)
                v = await anomaly_breaker.evaluate(conn, app, mode=caps_settings.anomaly_mode(), policy=policy,
                                                   profile=profiles.get(app))
                cleared = v.as_dict()
        except Exception as e:  # noqa: BLE001 — the reset goes on; the audit row says what could be read
            cleared = {"read_error": str(e)[:200]}
    out = await app_profile.reset_anomaly(pool, app, actor=actor, details=cleared)
    anomaly_breaker.invalidate()
    return {"profile": out, "cleared": cleared}
