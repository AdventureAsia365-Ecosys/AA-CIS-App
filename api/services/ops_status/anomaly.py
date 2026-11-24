"""AA-1141 (P1 feature 5) — the AI cost anomaly banner of the Ecosystem status page: an hour whose AI spend of one app x
provider x route is above ``max(N x its median hour, floor)`` (S285 review idea 5, AWS Cost Anomaly Detection style;
tech lead G6: the grouping must keep working when a route moves from the vendor API to Bedrock).

- Source: the hourly usage rollup ``shared.ai_usage_hourly`` (AA-1045; rolled up every quarter hour, so the current
  hour is near live), never a second scan of the raw ledger. One read of ``baseline_days`` x 24 hours plus the two
  hours checked (a few hundred grouped rows; the rollup's unique key leads with ``hour``), cached ``CACHE_TTL_S`` per
  current hour and settings in Redis (computed directly without Redis: it is a small read).
- Group: app x provider family x route (``sections.connection_key``: ``anthropic``, ``openai``, ``bedrock:<account>``),
  live traffic only (shadow and experiment calls are not spend the platform asked for).
- Checked hours: the previous complete hour and the current hour so far. Baseline: the ``baseline_days`` x 24 hours
  before them; an hour with no spend counts as 0 (the A0 assistant's rule), so a route that is usually idle is judged
  by the floor alone.
- Settings (CIS 483): ``cis.ops.status_cost_anomaly_factor`` (N, default 3), ``_baseline_days`` (7) and
  ``_floor_usd_h`` (0.10 USD).

Why not the anomaly breaker's cached median (``shared/llm_client/anomaly_breaker.cached_median``)? It is the median
of a whole UTC DAY of one app (the breaker trips on a day's spend); this banner judges one HOUR of one app x provider x
route against that group's usual hour. The two are not the same quantity, so the breaker's cache cannot be reused; the
breaker's own verdict stays on AI gateway › Budgets. Pure part: :func:`anomalies` (unit-tested). Cost: $0.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Optional

import asyncpg

from api.services.ops_overview import cache
from shared import ops_status_thresholds as th

CACHE_TTL_S = 300  # technical-constant: cache refresh of a read (Redis TTL)
KEY_PREFIX = "admin:overview:status"

HOURLY_SQL = """
    SELECT hour, app, provider, account, sum(cost_usd)::float8 AS usd
      FROM shared.ai_usage_hourly
     WHERE hour >= $1 AND hour < $2 AND traffic = 'live'
     GROUP BY hour, app, provider, account
"""


def hour_floor(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def window(now: datetime, baseline_days: int) -> tuple[datetime, datetime, datetime]:
    """(baseline start, checked start, end): the baseline hours, then the previous and the current hour. Pure."""
    cur = hour_floor(now)
    checked = cur - timedelta(hours=1)
    return checked - timedelta(days=baseline_days), checked, cur + timedelta(hours=1)


def _group(r: dict) -> tuple[str, str, str]:
    from api.services.ops_status.sections import connection_key
    from api.services.platform_cost import provider_family

    return (r.get("app") or "cis", provider_family(r.get("provider")), connection_key(r.get("provider"),
                                                                                       r.get("account")))


def anomalies(rows: list[dict], now: datetime, policy: dict) -> dict:
    """{rows: [the flagged (group, hour)], groups: checked, ...}: each group's spend in the previous and the current
    hour against ``max(factor x median hour, floor)``. ``rows``: {hour, app, provider, account, usd}. Pure."""
    days = int(policy["baseline_days"])
    base_start, checked_start, end = window(now, days)
    n_base = days * 24
    hourly: dict[tuple, dict[datetime, float]] = {}   # baseline: group -> hour -> USD (accounts of a family summed)
    recent: dict[tuple, dict[datetime, float]] = {}   # the checked hours, likewise
    for r in rows:
        h = r["hour"].astimezone(timezone.utc)
        into = hourly if base_start <= h < checked_start else recent if checked_start <= h < end else None
        if into is None:
            continue
        g = into.setdefault(_group(r), {})
        g[h] = g.get(h, 0.0) + float(r.get("usd") or 0.0)
    factor, floor = float(policy["factor"]), float(policy["floor_usd_h"])
    flagged = []
    for g, hours in recent.items():
        vals = list((hourly.get(g) or {}).values())
        med = median(vals + [0.0] * max(0, n_base - len(vals))) if n_base else 0.0
        line = max(factor * med, floor)
        for h, usd in sorted(hours.items()):
            if usd > line:
                flagged.append({"app": g[0], "provider": g[1], "route": g[2], "hour": h.isoformat(),
                                "partial": h == hour_floor(now), "usd": round(usd, 4),
                                "median_usd_h": round(med, 4), "threshold_usd": round(line, 4),
                                "times_median": round(usd / med, 1) if med > 0 else None})
    flagged.sort(key=lambda f: (-f["usd"], f["app"], f["route"]))
    return {"rows": flagged, "groups": len(recent), "factor": factor, "floor_usd_h": floor, "baseline_days": days,
            "baseline_from": base_start.isoformat(), "checked_from": checked_start.isoformat()}


async def collect(pool: Any, redis: Any, now: Optional[datetime] = None) -> dict:
    """The banner's rows now (cached per current hour and settings). Before the rollup table exists: no rows, with
    ``ready`` false."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    policy = th.cost_anomaly()
    base_start, _checked, end = window(now, int(policy["baseline_days"]))

    async def compute() -> dict:
        try:
            rows = [dict(r) for r in await pool.fetch(HOURLY_SQL, base_start, end)]
        except asyncpg.exceptions.UndefinedTableError:
            return {"status": "ok", "as_of": now.isoformat(), "refresh_s": CACHE_TTL_S,
                    "data": {"rows": [], "ready": False}}
        return {"status": "ok", "as_of": now.isoformat(), "refresh_s": CACHE_TTL_S,
                "data": {**anomalies(rows, now, policy), "ready": True}}

    name = (f"anomaly:{int(hour_floor(now).timestamp())}:{now.minute // 5}:{policy['factor']}:"
            f"{policy['baseline_days']}:{policy['floor_usd_h']}")
    out = await cache.cached_section(redis, name, CACHE_TTL_S, compute, needs_cache=False, key_prefix=KEY_PREFIX)
    out.pop("_cached", None)
    return out
