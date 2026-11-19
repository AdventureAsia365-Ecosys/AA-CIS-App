"""AA-1127 (C2 "spend truth", S283 UI audit; tech lead decision S285 E2; CIS 461) — the daily Cost Explorer refresh and
the freshness of the stored figures.

    def schedule_enabled() -> bool
    def run_hour_utc() -> int
    def stale_after_hours() -> float
    def refresh_window(today) -> tuple[date, date]
    async def refresh(pool, start, end_exclusive) -> dict
    async def freshness(pool, now=None) -> dict

Before this module the two Cost Explorer snapshots (``shared.cost_explorer_snapshot`` per account and service,
``shared.cost_explorer_app_snapshot`` for the main account by App tag, CIS 248) were only written when someone opened
Spend › By app (the lazy refresh of api/services/platform_cost.py) or pressed "Refresh from AWS"
(``POST /admin/cost-explorer/check``). The forecast's AWS rows and the nightly LLM cost reconciliation then read figures
of unknown age. Now job kind ``cost_explorer_refresh`` (services/jobs/cost_explorer_refresh_job.py) runs :func:`refresh`
once per UTC day, queued by the api's ops metrics sampler from the hour of ``cis.spend.ce_daily_refresh_hour_utc``
while ``cis.spend.ce_daily_refresh_enabled`` is on; the Spend page shows a "stale" badge from :func:`freshness` when the
newest stored fetch is older than ``cis.spend.ce_stale_after_hours``. The three keys are seeded by CIS 461 and edited on
Platform › Settings (area "spend").

What a run fetches: the same two reads as ``POST /admin/cost-explorer/check`` — the SERVICE (+ RECORD_TYPE) split of
each fetched account (``cost_explorer.fetch_all_accounts_cost``, one request per account plus pages) and the main
account by App tag (``platform_cost.refresh_app_snapshot``, one request plus pages) — for :func:`refresh_window`: the
current UTC month to date, reaching back far enough that every day gets one fetch after Cost Explorer stopped revising
it (``platform_cost.FINAL_AFTER``), so the lazy refresh of By app finds no stale day either.

Cost line: about ``cost_explorer.CE_REQUEST_COST_USD`` (0.01 USD) per Cost Explorer request, about 3 requests a run.
The run reports the estimate (requests x 0.01) as the job's own cost (``shared.job.cost_usd``, the job ledger the Jobs
page and Spend › DataForSEO runs read) and in its result; the lazy path only logs the same estimate. The real charge
shows up as the "AWS Cost Explorer" service line of the main account in the snapshot itself.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any, Optional

import structlog

from api.services import platform_cost
from api.services.cost_explorer_daily_settings import ALL, ENABLED, RUN_HOUR_UTC, STALE_AFTER_HOURS
from shared.aws_client import cost_explorer as ce

logger = structlog.get_logger()

__all__ = ["ALL", "ENABLED", "FRESHNESS_SQL", "RUN_HOUR_UTC", "STALE_AFTER_HOURS", "freshness", "freshness_of",
           "refresh", "refresh_window", "run_hour_utc", "schedule_enabled", "stale_after_hours", "with_freshness"]

# The seeded defaults, used only while the registry cannot serve a key (not seeded yet, unreadable): the same values as
# CIS 461, so behaviour does not change between "registry down" and "registry at its defaults".
_FALLBACK = {ENABLED: True, RUN_HOUR_UTC: 1, STALE_AFTER_HOURS: 36}


def _setting(key: str) -> Any:
    try:
        from shared import settings
        return settings.get(key)
    except Exception:  # noqa: BLE001 — a settings problem never stops the sampler tick or the Spend page
        return _FALLBACK[key]


def schedule_enabled() -> bool:
    v = _setting(ENABLED)
    return v if isinstance(v, bool) else bool(_FALLBACK[ENABLED])


def run_hour_utc() -> int:
    v = _setting(RUN_HOUR_UTC)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 23:
        return int(_FALLBACK[RUN_HOUR_UTC])
    return int(v)


def stale_after_hours() -> float:
    v = _setting(STALE_AFTER_HOURS)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return float(_FALLBACK[STALE_AFTER_HOURS])
    return float(v)


def refresh_window(today: date) -> tuple[date, date]:
    """[start, end_exclusive) of a daily run on UTC day ``today``: the month to date (tomorrow exclusive, today's
    partial day included, as month_window does), started early enough that the days Cost Explorer still revised at
    yesterday's run are fetched once more after they became final (FINAL_AFTER after the day ended, plus the CE lag)."""
    start, end = platform_cost.month_window(None, today)
    back = today - (platform_cost.FINAL_AFTER + platform_cost.CE_LAG)
    return min(start, back), end


def _error_class(e: BaseException) -> str:
    """A short class for the job result: never the AWS error text (it names the role ARN and the account ID, AA-912)."""
    return type(e).__name__


async def refresh(pool: Any, start: date, end_exclusive: date) -> dict:
    """One Cost Explorer read of both snapshots for [start, end_exclusive), stored. Raises ce.CostExplorerUnavailable
    only when both reads failed (nothing was stored). A partial failure (one account, or the App-tag read) is listed in
    ``errors`` by account key and error class; the full text goes to the server log."""
    errors: dict[str, str] = {}
    service_rows = 0
    stats: dict[str, int] = {}   # filled by the fetch, failed accounts included (an AssumeRole refusal asks nothing)
    try:
        result = await asyncio.to_thread(ce.fetch_all_accounts_cost, start.isoformat(), end_exclusive.isoformat(),
                                         stats=stats)
        service_rows = await ce.record_snapshot(pool, result["rows"])
        for account, text in (result.get("errors") or {}).items():
            logger.warning("cost_explorer_daily_account_failed", account=account, error=str(text)[:300])
            errors[account] = "CostExplorerUnavailable"
    except Exception as e:  # noqa: BLE001 — every account failed, or the write failed; the App-tag read still runs
        logger.warning("cost_explorer_daily_service_failed", error=repr(e)[:300])
        errors["service"] = _error_class(e)
    service_requests = int(stats.get("requests", 0))
    app_rows: Optional[int] = None
    app_requests = 0
    try:
        out = await platform_cost.refresh_app_snapshot(pool, start, end_exclusive)
        app_rows, app_requests = out["row_count"], int(out["ce_requests"])
    except Exception as e:  # noqa: BLE001
        logger.warning("cost_explorer_daily_app_tag_failed", error=repr(e)[:300])
        errors["acc2_app_tag"] = _error_class(e)
        app_requests = 1
    requests = service_requests + app_requests
    summary = {"period_start": start.isoformat(), "period_end_exclusive": end_exclusive.isoformat(),
               "row_count": service_rows, "app_tag_row_count": app_rows, "ce_requests": requests,
               "est_cost_usd": round(requests * ce.CE_REQUEST_COST_USD, 2), "errors": errors}
    logger.info("cost_explorer_daily_refreshed", **{k: v for k, v in summary.items() if k != "errors"},
                errors=sorted(errors))
    if "service" in errors and "acc2_app_tag" in errors:
        raise ce.CostExplorerUnavailable(f"both Cost Explorer reads failed: {sorted(errors.items())}")
    return summary


FRESHNESS_SQL = """
    SELECT (SELECT max(fetched_at) FROM shared.cost_explorer_snapshot) AS service_fetched_at,
           (SELECT max(fetched_at) FROM shared.cost_explorer_app_snapshot) AS app_fetched_at
"""


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v is not None else None


def freshness_of(service_at: Optional[datetime], app_at: Optional[datetime], now: datetime,
                 stale_after: float) -> dict:
    """The freshness block of the Spend page from the newest fetch of each snapshot. ``oldest_newest_at`` is the older
    of the two newest fetches: a daily run writes both, so either one lagging means a read stopped working. Stale when
    it is older than ``stale_after`` hours, or when either snapshot has no row at all."""
    newest = [v for v in (service_at, app_at) if v is not None]
    oldest_newest = min(newest) if len(newest) == 2 else None
    age_h = round((now - oldest_newest).total_seconds() / 3600, 1) if oldest_newest is not None else None
    return {"service_fetched_at": _iso(service_at), "app_fetched_at": _iso(app_at),
            "oldest_newest_at": _iso(oldest_newest), "age_hours": age_h, "stale_after_hours": stale_after,
            "stale": age_h is None or age_h > stale_after,
            "daily_refresh": {"enabled": schedule_enabled(), "hour_utc": run_hour_utc()}}


async def freshness(pool: Any, now: Optional[datetime] = None) -> dict:
    """``ce_freshness`` of the Spend page's two cost reads (GET /admin/platform/cost, GET /admin/cost-ops/forecast).
    Two indexed max() reads; never raises (a failed read is ``stale: null`` with ``error``, the page shows no badge)."""
    now = now or datetime.now(timezone.utc)
    try:
        row = await pool.fetchrow(FRESHNESS_SQL)
        return freshness_of(row["service_fetched_at"], row["app_fetched_at"], now, stale_after_hours())
    except Exception as e:  # noqa: BLE001 — the badge never breaks the page
        logger.warning("cost_explorer_freshness_failed", error=repr(e)[:200])
        return {"stale": None, "error": "freshness unavailable", "stale_after_hours": stale_after_hours(),
                "daily_refresh": {"enabled": schedule_enabled(), "hour_utc": run_hour_utc()}}


async def with_freshness(pool: Any, out: dict) -> dict:
    """``out`` with its ``ce_freshness`` block (the two Spend reads' routers); never raises."""
    out["ce_freshness"] = await freshness(pool)
    return out
