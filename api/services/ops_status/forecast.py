"""AA-1141 (P1 feature 4) — the capacity forecast of the Ecosystem status page: in how many days does each limit fill
at today's pace (S285 review idea 4, New Relic / Railway style)?

- **RDS free storage.** A least-squares line through the daily-ish minimum of ``FreeStorageSpace`` over
  ``cis.ops.status_forecast_window_days`` (one CloudWatch GetMetricData metric, 6-hour points, cached ``STORAGE_TTL_S``
  in Redis and never read without Redis: at most 4 reads a day, about 0.001 USD a month). Two answers: the days until
  free storage reaches its warn limit at today's size (the CIS 477 share of the allocated storage, from the sampler's
  database row), and, when storage autoscaling is on, the days until the used storage reaches the autoscaling
  ceiling (MaxAllocatedStorage) — the real limit, so the level follows it when it is known.
- **AI caps.** Per LLM connection with a monthly budget (AI gateway › Budgets; the CIS share of the connection the
  CIS gateway enforces), the month-to-date spend as a straight line: the day it reaches the cap, or "not this month".
- **GitHub Actions cache.** No source in the database: the cache size is only read by the CI hygiene check on GitHub,
  so the row says "not measured yet" with that reason, never a number.

Level: ``crit`` at or under ``cis.ops.status_forecast_crit_days`` days left, ``warn`` at or under
``cis.ops.status_forecast_warn_days`` (CIS 483), ``ok`` otherwise or when the limit is not reached this month / the
trend does not shrink. A ``crit`` row is a SEV3 Needs-attention item. Pure parts (:func:`trend`, :func:`storage_rows`,
:func:`ai_rows`, :func:`level_of`) are unit-tested.
"""
from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from api.services.ops_overview import cache, cloudwatch
from api.services.ops_status import cw_series, store
from shared import ops_status_thresholds as th

logger = structlog.get_logger()

STORAGE_TTL_S = 6 * 3600  # technical-constant: cache life of the storage trend read (Redis TTL)
STORAGE_PERIOD_S = 6 * 3600  # technical-constant: the CloudWatch point of the trend (4 a day)
KEY_PREFIX = "admin:overview:status"
NOT_MEASURED = "not_measured"
ACTIONS_CACHE_REASON = ("No source in the database: the GitHub Actions cache size is read only by the CI hygiene "
                        "check on GitHub.")


def level_of(days: Optional[float], lim: dict) -> str:
    """ok / warn / crit of a days-left value (None = not reached in the horizon: ok). Pure."""
    if days is None:
        return "ok"
    if days <= float(lim["crit"]):
        return "crit"
    return "warn" if days <= float(lim["warn"]) else "ok"


def trend(points: list[tuple[datetime, float]]) -> Optional[dict]:
    """Least-squares line of (time, value): {slope_per_day, latest, at}; None with fewer than 2 points or no time
    spread. Pure."""
    pts = sorted((t, v) for t, v in points if v is not None)
    if len(pts) < 2:
        return None
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 86400.0 for t, _ in pts]
    ys = [float(v) for _, v in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return {"slope_per_day": round(slope, 4), "latest": round(ys[-1], 3), "at": pts[-1][0].isoformat(), "points": n}


def _days(room: float, per_day: float) -> Optional[float]:
    """Days until ``room`` is used at ``per_day`` (None when nothing is used). Pure."""
    if per_day <= 0:
        return None
    return round(max(0.0, room) / per_day, 1)


def storage_rows(tr: Optional[dict], db: Optional[dict], lim: dict) -> list[dict]:
    """The RDS storage rows from the free storage trend (GiB) and the sampler's database value (allocated, max).
    Pure."""
    v = db or {}
    alloc, max_alloc = v.get("allocated_gib"), v.get("max_allocated_gib")
    free_lim = th.rds_free_storage(alloc)
    if tr is None:
        return [{"id": "rds_free", "label": "Database free storage", "status": NOT_MEASURED,
                 "reason": "Not enough CloudWatch points yet for a trend.", "level": "none"}]
    shrink = -float(tr["slope_per_day"])
    free = float(tr["latest"])
    to_warn = _days(free - float(free_lim["warn"]), shrink) if free > float(free_lim["warn"]) else 0.0
    row = {"id": "rds_free", "label": "Database free storage", "status": "ok", "unit": "GiB", "latest": free,
           "per_day": round(-shrink, 3), "days_to_warn": to_warn, "warn_gib": free_lim["warn"],
           "allocated_gib": alloc, "max_allocated_gib": max_alloc, "days_to_ceiling": None, "basis": "warn_limit"}
    days = to_warn
    if alloc and max_alloc and float(max_alloc) > float(alloc):
        used = float(alloc) - free
        row["days_to_ceiling"] = _days(float(max_alloc) - used, shrink)
        row["basis"] = "autoscaling_ceiling"
        days = row["days_to_ceiling"]
    row["days_left"] = days
    row["level"] = level_of(days, lim)
    return [row]


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    now = now.astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days = calendar.monthrange(now.year, now.month)[1]
    return start, start + timedelta(days=days)


def ai_rows(budgets: list[dict], now: datetime, lim: dict) -> list[dict]:
    """One row per LLM connection with a monthly budget: month-to-date as a straight line to the cap. Pure."""
    start, end = month_bounds(now)
    elapsed = max((now - start).total_seconds() / 86400.0, 1.0 / 24)
    left_in_month = round((end - now).total_seconds() / 86400.0, 1)
    out = []
    for b in budgets:
        cap = b.get("monthly_budget_usd")
        if cap is None or not b.get("enabled", True):
            continue
        cap, spent = float(cap), float(b.get("month_spent_usd") or 0.0)
        rate = spent / elapsed
        forecast = round(spent + rate * left_in_month, 2)
        if spent >= cap:
            days: Optional[float] = 0.0
        else:
            days = _days(cap - spent, rate)
            if days is not None and days > left_in_month:
                days = None   # not reached before the month (and the cap) starts again
        out.append({"id": f"ai:{b['connection_key']}", "label": b.get("label") or b["connection_key"],
                    "connection_key": b["connection_key"], "status": "ok", "unit": "USD", "latest": round(spent, 2),
                    "cap_usd": cap, "per_day": round(rate, 3), "forecast_usd": forecast, "days_left": days,
                    "month_days_left": left_in_month, "level": level_of(days, lim)})
    return out


def actions_cache_row() -> dict:
    return {"id": "actions_cache", "label": "GitHub Actions cache", "status": NOT_MEASURED,
            "reason": ACTIONS_CACHE_REASON, "level": "none"}


# ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _storage_points(window_days: int, now: datetime, env: Optional[str] = None, client: Any = None) -> dict:
    """Sync (run it in a thread): the FreeStorageSpace minimum per 6 hours over the window, in GiB. One metric."""
    env = env or cloudwatch.environment()
    client = client or cloudwatch.aws_client("cloudwatch")
    start = now - timedelta(days=window_days)
    q = [{"Id": "free", "ReturnData": True, "MetricStat": {"Metric": {
        "Namespace": "AWS/RDS", "MetricName": "FreeStorageSpace",
        "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": cw_series.db_instance(env)}]},
        "Period": STORAGE_PERIOD_S, "Stat": "Minimum"}}]
    try:
        results, _calls = cloudwatch.get_metric_data(client, q, start, now)
    except Exception as e:  # noqa: BLE001 — AWS / network: the row says why
        logger.warning("ops_status_forecast_storage_failed", error=repr(e)[:300])
        return {"status": "unavailable", "reason": "not_permitted" if cloudwatch.is_denied(e) else "aws_error"}
    r = next((x for x in results if x.get("Id") == "free"), {})
    pts = [[t.isoformat() if hasattr(t, "isoformat") else str(t), round(float(v) / cw_series.GIB, 3)]
           for t, v in zip(r.get("Timestamps") or [], r.get("Values") or [])]
    return {"status": "ok", "as_of": now.isoformat(), "points": pts}


async def _storage(redis: Any, now: datetime, window_days: int) -> dict:
    async def compute() -> dict:
        return await asyncio.to_thread(_storage_points, window_days, now)

    slot = int(now.timestamp()) // STORAGE_TTL_S
    out = await cache.cached_section(redis, f"forecast_storage:{window_days}:{slot}", STORAGE_TTL_S, compute,
                                     needs_cache=True, key_prefix=KEY_PREFIX)
    out.pop("_cached", None)
    return out


async def _budgets(pool: Any) -> list[dict]:
    from api.services.ai_gateway import read_connection_budgets

    return await read_connection_budgets(pool)


async def collect(pool: Any, redis: Any, now: Optional[datetime] = None) -> dict:
    """{status, as_of, refresh_s, data: {rows, window_days, warn, crit}}: storage, AI caps and the Actions cache."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lim = th.forecast()
    window = int(lim["window_days"])
    storage, budgets, latest = await asyncio.gather(
        _storage(redis, now, window), _budgets(pool),
        store.latest(pool, ["database"], now - timedelta(days=th.history_days())), return_exceptions=True)
    rows: list[dict] = []
    if isinstance(storage, dict) and storage.get("status") == "ok":
        pts = []
        for t, v in storage.get("points") or []:
            try:
                pts.append((datetime.fromisoformat(str(t).replace("Z", "+00:00")), float(v)))
            except (TypeError, ValueError):
                continue
        db = next((r["value"] for r in latest if r.get("subject") == "rds"), None) \
            if isinstance(latest, list) else None
        rows += storage_rows(trend(pts), db, lim)
    else:
        why = storage.get("message") or storage.get("reason") if isinstance(storage, dict) else type(storage).__name__
        rows.append({"id": "rds_free", "label": "Database free storage", "status": "unavailable",
                     "reason": str(why or "error"), "level": "none"})
    if isinstance(budgets, list):
        rows += ai_rows(budgets, now, lim)
    else:
        logger.warning("ops_status_forecast_budgets_failed", error=repr(budgets)[:200])
        rows.append({"id": "ai", "label": "AI connection caps", "status": "unavailable",
                     "reason": type(budgets).__name__, "level": "none"})
    rows.append(actions_cache_row())
    return {"status": "ok", "as_of": now.isoformat(), "refresh_s": 900,
            "data": {"rows": rows, "window_days": window, "warn": lim["warn"], "crit": lim["crit"],
                     "warn_key": lim["warn_key"], "crit_key": lim["crit_key"], "window_key": lim["window_key"]}}
