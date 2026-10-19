"""One health definition for every admin page (S243 UI verify, finding 1).

Before this module the admin console had two answers to "is this app healthy?": /admin/overview judged each app
from CloudWatch traffic (AA-858) and /admin/platform/apps from its own probes (AA-884), so TripPlanner read
"Healthy" on one page and "Unknown" on the other. Both pages now take an app's status from ``overall_status``:

- **traffic** — the CloudWatch section of AA-858 (``ops_overview.cloudwatch.collect_health``: API Gateway or Lambda
  requests, server errors, throttles and firing alarms, last 24 h). ``traffic_section`` is the ONE cached accessor
  (Redis key ``admin:overview:ops:health:v1``, 5 min, never an AWS call without Redis), shared by both pages.
- **probe** — an app's own check where it has one: CIS (live workers on the API's commit, schema ready) and Booking
  (``/booking/health`` + ``/health/ready``). TripPlanner has no health endpoint, so traffic alone decides.

Rule: each known signal maps to ok / degraded / down; the app's status is the worst of them. A signal with no data
(CloudWatch saw no metric, a probe timed out) is not a problem by itself; the status is ``unknown`` only when no
signal is known.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

HEALTH_TTL_S = 300

# CloudWatch app state (cloudwatch.app_state) -> app status. Firing alarms or a high 5xx share make an app
# "degraded", not "down": the app still answers; "down" comes only from a failing health endpoint.
TRAFFIC_TO_STATUS = {"ok": "ok", "warn": "degraded", "alarm": "degraded"}
_RANK = {"ok": 0, "degraded": 1, "down": 2}

TRAFFIC_BASIS = "Traffic, last 24 hours (AWS CloudWatch)"
PROBE_BASIS = "Health check"


async def traffic_section(redis: Any, now: Optional[datetime] = None) -> dict:
    """The CloudWatch traffic section, cached on its own key (see module docstring). Never raises."""
    from api.services.ops_overview import cache, cloudwatch

    async def compute() -> dict:
        return await asyncio.to_thread(cloudwatch.collect_health, now)

    return await cache.cached_section(redis, "health", HEALTH_TTL_S, compute, needs_cache=True)


def traffic_for(section: Optional[dict], app_id: str) -> Optional[dict]:
    """One app's entry of the traffic section, or None when the section is unavailable."""
    if not section or section.get("status") != "ok":
        return None
    return next((a for a in section.get("apps") or [] if a.get("app") == app_id), None)


def traffic_summary(section: Optional[dict], app_id: str) -> Optional[dict]:
    """The few traffic numbers the Apps page shows next to a card (same source as the overview tiles)."""
    app = traffic_for(section, app_id)
    if app is None:
        return None
    alarms = app.get("alarms") or {}
    return {
        "state": app.get("state"), "reason": app.get("state_reason"), "has_data": app.get("has_data"),
        "requests": app.get("requests"), "errors": app.get("errors"), "error_rate_pct": app.get("error_rate_pct"),
        "throttles": app.get("throttles"), "p95_ms": app.get("p95_ms"),
        "alarms_total": alarms.get("total"), "alarms_firing": len(alarms.get("firing") or []),
        "kind": app.get("kind"), "resources": app.get("resources"),
        "window_hours": section.get("window_hours") if section else None,
        "as_of": section.get("as_of") if section else None,
    }


def overall_status(probe: Optional[dict], traffic: Optional[dict]) -> dict:
    """{status: ok|degraded|down|unknown, reason, basis[]} from an app's probe and its traffic entry.

    ``probe``: ``{"status": ok|degraded|down|unknown, "reason": str}`` or None (the app has no probe).
    ``traffic``: one app of the traffic section (``state``, ``state_reason``) or None (section unavailable)."""
    signals: list[tuple[str, str, str]] = []           # (status, reason, basis)
    if probe and probe.get("status") in _RANK:
        signals.append((probe["status"], probe.get("reason") or "", PROBE_BASIS))
    if traffic and traffic.get("state") in TRAFFIC_TO_STATUS:
        signals.append((TRAFFIC_TO_STATUS[traffic["state"]], traffic.get("state_reason") or "", TRAFFIC_BASIS))
    if not signals:
        why = []
        if probe and probe.get("reason"):
            why.append(probe["reason"])
        if traffic and traffic.get("state_reason"):
            why.append(f"traffic: {traffic['state_reason'].lower()}")
        return {"status": "unknown", "reason": "; ".join(why) or "No health check and no traffic numbers right now",
                "basis": []}
    worst = max(signals, key=lambda s: _RANK[s[0]])
    reason = worst[1]
    if worst[0] == "ok" and len(signals) > 1:
        reason = "Health check passes and traffic looks normal"
    return {"status": worst[0], "reason": reason, "basis": [s[2] for s in signals]}
