"""AA-1126 G2 (S283) — the anomaly circuit breaker: the one control allowed to stop customer AI calls in prod.

Tech lead policy S283: no platform-wide fixed USD cap blocks agency features in prod; only a breaker on ABNORMAL spend
may stop calls. Per app (the totals of ``shared.llm_call_log`` by app: cis, tripplanner, crm, booking), the breaker is
tripped when the app's AI spend today exceeds ``max(multiple x normal day, floor)``:

- today = the app's ledger rows since ``max(UTC midnight, the profile's anomaly_reset_at)`` (CIS 446): an admin reset
  gives the rest of the UTC day a fresh allowance, and the day rolling over ends a trip by itself;
- normal day = the median of the app's daily spend over the ``baseline_days`` UTC days before today (rolled-up days
  from ``shared.llm_call_daily`` + raw days not rolled up yet, the platform_cost rule; a day with no spend is 0);
- ``multiple``, ``floor_usd``, ``baseline_days`` and the mode are environment settings (``caps_settings``, Platform ›
  Settings, area Spend): ``off`` (not read), ``alert`` (default: one admin bell + ops e-mail per app and 24 h, nothing
  refused) or ``enforce`` (also refuses the app's new calls with ``AnomalyBreakerTripped``, a BudgetExceeded: the job
  ends ``stopped_budget``; the gateway writes the 0 USD refused / budget ledger row).

On the call path the CIS gateway checks its own app (``call_log.APP``); ``status`` serves every app to the admin
(``GET /admin/ai-gateway/anomaly-breaker``), so TripPlanner and the CRM can read the same verdict later. Reads are
cached per process (the baseline per app and UTC day, today's spend ``SPEND_TTL_SECONDS``). Never fails closed: a
read error leaves the breaker closed (logged), the same rule as the central and connection budgets.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import structlog

from shared import cost_guard

from . import app_profile, caps_settings, connections

logger = structlog.get_logger()

SPEND_TTL_SECONDS = 60.0  # technical-constant: in-process cache refresh of a read
FAILURE_TTL_SECONDS = 60.0  # technical-constant: back-off after a failed cache read
SCOPE_PREFIX = "anomaly:"
APPS = ("cis", "tripplanner", "crm")      # the apps with AI (AA-Booking has none)

# The app of a ledger row: the platform_cost rule (an old CIS row has no app).
_APP_EXPR = "coalesce(l.app, l.quality_signal ->> 'app', 'cis')"
TODAY_SQL = f"""
    SELECT coalesce(sum(l.cost_usd), 0)::float AS usd
      FROM shared.llm_call_log l
     WHERE l.created_at >= $1 AND {_APP_EXPR} = $2
"""
# $1 / $2 = the UTC dates [start, end), $3 = the app. A day is either rolled up or raw, never both (platform_cost).
BASELINE_SQL = f"""
    WITH days AS (
        SELECT generate_series($1::date, $2::date - 1, interval '1 day')::date AS day
    ), rolled AS (
        SELECT d.day, sum(d.cost_usd)::float AS usd
          FROM shared.llm_call_daily d
         WHERE d.day >= $1 AND d.day < $2 AND d.app = $3
         GROUP BY 1
    ), raw AS (
        SELECT (l.created_at AT TIME ZONE 'UTC')::date AS day, coalesce(sum(l.cost_usd), 0)::float AS usd
          FROM shared.llm_call_log l
         WHERE l.created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
           AND l.created_at < ($2::date::timestamp AT TIME ZONE 'UTC')
           AND {_APP_EXPR} = $3
           AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                            WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
         GROUP BY 1
    )
    SELECT coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(r.usd, w.usd, 0)), 0)::float AS median
      FROM days
      LEFT JOIN rolled r USING (day)
      LEFT JOIN raw w USING (day)
"""


class AnomalyBreakerTripped(cost_guard.BudgetExceeded, RuntimeError):
    """The app's anomaly breaker is tripped (mode enforce): the gateway refused the call before any provider attempt.
    A ``BudgetExceeded`` (the job ends ``stopped_budget``, never retried) and a ``RuntimeError`` (callers that catch
    the gateway's failures still do). Its own class, so a caller and the logs tell it from a plan or connection cap."""

    app: str = ""


@dataclass(frozen=True)
class Verdict:
    app: str
    mode: str
    since: str                # ISO time today's spend is counted from
    spent_usd: float
    median_usd: float
    multiple: float
    floor_usd: float
    baseline_days: int
    threshold_usd: float
    tripped: bool
    reset_at: Optional[str] = None
    reset_by: Optional[str] = None

    @property
    def refuses(self) -> bool:
        return self.tripped and self.mode == caps_settings.ENFORCE

    def as_dict(self) -> dict:
        return {**asdict(self), "refuses": self.refuses}


def threshold(median_usd: float, policy: caps_settings.AnomalyPolicy) -> float:
    """The trip line: the multiple of the normal day, never below the floor. Pure."""
    return max(policy.multiple * max(median_usd, 0.0), policy.floor_usd)


def verdict(app: str, mode: str, policy: caps_settings.AnomalyPolicy, *, since: datetime, spent_usd: float,
            median_usd: float, reset_at: Optional[str] = None, reset_by: Optional[str] = None) -> Verdict:
    """The breaker's state from its inputs. Tripped = today's spend strictly above the trip line. Pure."""
    line = threshold(median_usd, policy)
    return Verdict(app=app, mode=mode, since=since.isoformat(), spent_usd=round(spent_usd, 6),
                   median_usd=round(median_usd, 6), multiple=policy.multiple, floor_usd=policy.floor_usd,
                   baseline_days=policy.baseline_days, threshold_usd=round(line, 6), tripped=spent_usd > line,
                   reset_at=reset_at, reset_by=reset_by)


def window_start(now: datetime, reset_at: Optional[str]) -> datetime:
    """``max(UTC midnight of now, reset_at)``. A reset from an earlier day (or unparsable) is ignored. Pure."""
    now = now.astimezone(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not reset_at:
        return midnight
    try:
        at = datetime.fromisoformat(str(reset_at).replace("Z", "+00:00"))
    except ValueError:
        return midnight
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max(midnight, min(at.astimezone(timezone.utc), now))


def baseline_window(today: date, days: int) -> tuple[date, date]:
    """[today - days, today): the UTC days of the normal-day median. Pure."""
    return today - timedelta(days=days), today


# ── reads (asyncpg) ─────────────────────────────────────────────────────────────────────────────────────────────────

async def read_spend_since(conn, app: str, since: datetime) -> float:
    return float(await conn.fetchval(TODAY_SQL, since, app) or 0.0)


async def read_median(conn, app: str, today: date, days: int) -> float:
    start, end = baseline_window(today, days)
    return float(await conn.fetchval(BASELINE_SQL, start, end, app) or 0.0)


async def evaluate(conn, app: str, *, mode: str, policy: caps_settings.AnomalyPolicy,
                   profile: Optional[app_profile.AppProfile] = None, now: Optional[datetime] = None) -> Verdict:
    """The verdict of ``app`` now, read through ``conn`` (no cache). Used by the admin status and by the loaders."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reset_at = getattr(profile, "anomaly_reset_at", None)
    since = window_start(now, reset_at)
    spent = await read_spend_since(conn, app, since)
    median = await read_median(conn, app, now.date(), policy.baseline_days)
    return verdict(app, mode, policy, since=since, spent_usd=spent, median_usd=median, reset_at=reset_at,
                   reset_by=getattr(profile, "anomaly_reset_by", None))


async def status(conn, *, now: Optional[datetime] = None) -> dict:
    """The admin view: the settings in force and the verdict of every app with AI (mode off: no verdicts read)."""
    mode, policy = caps_settings.anomaly_mode(), caps_settings.anomaly_policy()
    out = {"mode": mode, "policy": None if policy is None else asdict(policy), "apps": []}
    if mode == caps_settings.OFF or policy is None:
        return out
    try:
        profiles = await app_profile.read_all(conn)
    except app_profile.AppProfileUnavailable:
        profiles = {}
    for app in APPS:
        v = await evaluate(conn, app, mode=mode, policy=policy, profile=profiles.get(app), now=now)
        out["apps"].append(v.as_dict())
    return out


# ── the call path (sync, cached) ────────────────────────────────────────────────────────────────────────────────────

_baseline: dict[tuple, float] = {}             # (app, UTC date, days) -> median
_spend: dict[tuple, tuple[float, float]] = {}  # (app, since ISO) -> (usd, monotonic time read)
_alerted: set[tuple] = set()                   # (app, since ISO, refuses): this process already rang the bell
_failed_at = 0.0
_lock = threading.Lock()


async def _load(app: str, since: datetime, today: date, days: int, need_median: bool) -> tuple[float, Optional[float]]:
    conn = await connections._connect()
    try:
        spent = await read_spend_since(conn, app, since)
        median = await read_median(conn, app, today, days) if need_median else None
        return spent, median
    finally:
        await conn.close()


def loader(app: str, since: datetime, today: date, days: int, need_median: bool) -> tuple[float, Optional[float]]:
    """The sync loader of the cache (tests replace it; no database in unit tests)."""
    return connections.run_sync(lambda: _load(app, since, today, days, need_median))


def _cached(app: str, since: datetime, days: int) -> Optional[tuple[float, float]]:
    """(spent since ``since``, median) from the cache, reloaded when stale. None when unreadable. Never raises."""
    global _failed_at
    now = time.monotonic()
    bkey, skey = (app, since.date(), days), (app, since.isoformat())
    hit = _spend.get(skey)
    if hit is not None and now - hit[1] < SPEND_TTL_SECONDS and bkey in _baseline:
        return hit[0], _baseline[bkey]
    if _failed_at and now - _failed_at < FAILURE_TTL_SECONDS:
        return None
    with _lock:
        try:
            spent, median = loader(app, since, since.date(), days, bkey not in _baseline)
        except Exception as e:  # noqa: BLE001 — fail open, see the module docstring
            _failed_at = time.monotonic()
            logger.warning("llm_anomaly_breaker_read_failed", app=app, error=str(e)[:300],
                           hint="the anomaly breaker stays closed until the spend can be read")
            return None
        _failed_at = 0.0
        if len(_spend) > 100 or len(_baseline) > 100:
            _spend.clear()
            _baseline.clear()
        if median is not None:
            _baseline[bkey] = median
        _spend[skey] = (spent, time.monotonic())
        return spent, _baseline.get(bkey, 0.0)


def current(app: str, now: Optional[datetime] = None) -> Optional[Verdict]:
    """The cached verdict of ``app`` (None: breaker off, settings unreadable, or spend unreadable). Never raises."""
    try:
        mode, policy = caps_settings.anomaly_mode(), caps_settings.anomaly_policy()
        if mode == caps_settings.OFF or policy is None:
            return None
        profile = app_profile.profile(app)
        reset_at = profile.anomaly_reset_at
        since = window_start(now or datetime.now(timezone.utc), reset_at)
        read = _cached(app, since, policy.baseline_days)
        if read is None:
            return None
        return verdict(app, mode, policy, since=since, spent_usd=read[0], median_usd=read[1], reset_at=reset_at,
                       reset_by=profile.anomaly_reset_by)
    except Exception as e:  # noqa: BLE001 — never on the call path
        logger.warning("llm_anomaly_breaker_check_failed", app=app, error=str(e)[:200])
        return None


def message(v: Verdict) -> str:
    action = ("new AI calls of the app are refused until an administrator resets the breaker (AI gateway > Budgets > "
              "Spend controls) or the UTC day ends" if v.refuses else "alert only: calls go on")
    return (f"AI anomaly breaker tripped for app {v.app}: ${v.spent_usd:.2f} spent since {v.since} is above "
            f"${v.threshold_usd:.2f} ({v.multiple:g} x the ${v.median_usd:.2f} median day of the last "
            f"{v.baseline_days} days, floor ${v.floor_usd:.2f}); {action}.")


async def _alert(v: Verdict) -> None:
    conn = await connections._connect()
    try:
        summary = {"provider": "llm", "scope": f"{SCOPE_PREFIX}{v.app}", "day_spent_usd": round(v.spent_usd, 4),
                   "per_day_usd": round(v.threshold_usd, 4), "hard_stop": v.refuses, "hard_stop_day": v.refuses,
                   "used_pct": round(100.0 * v.spent_usd / v.threshold_usd, 1) if v.threshold_usd > 0 else None}
        await cost_guard.maybe_alert_central(conn, summary, alert_pct=100, stopped=v.refuses, message=message(v))
    finally:
        await conn.close()


def send(v: Verdict) -> None:
    """Write the bell (tests replace it; no database in unit tests)."""
    connections.run_sync(lambda: _alert(v))


def alert(v: Verdict) -> None:
    """One admin bell + ops e-mail per app and 24 h (scope llm:anomaly:<app>); once per trip window in this process
    (no database call on every refused call). Best effort: never raises."""
    key = (v.app, v.since, v.refuses)
    if key in _alerted:
        return
    if len(_alerted) > 100:
        _alerted.clear()
    _alerted.add(key)
    logger.warning("llm_anomaly_breaker_tripped", **v.as_dict())
    try:
        send(v)
    except Exception as e:  # noqa: BLE001 — the verdict stands whatever the bell does
        logger.warning("llm_anomaly_breaker_alert_failed", app=v.app, error=str(e)[:200])


def check(stage: Optional[str], app: Optional[str] = None) -> None:
    """The gateway's check before any attempt: alert when the app's breaker is tripped, and raise
    ``AnomalyBreakerTripped`` when the mode is enforce. Raises nothing else."""
    if app is None:
        from .call_log import APP
        app = APP
    v = current(app)
    if v is None or not v.tripped:
        return
    alert(v)
    if v.refuses:
        logger.warning("llm_anomaly_breaker_refused", stage=stage, app=app, spent_usd=v.spent_usd,
                       threshold_usd=v.threshold_usd)
        exc = AnomalyBreakerTripped(message(v), "llm", "anomaly")
        exc.app = app
        raise exc


def refusal(stage: Optional[str], app: Optional[str] = None) -> Optional[AnomalyBreakerTripped]:
    """``check`` returning its refusal instead of raising it (the gateway writes the ledger row and raises it)."""
    try:
        check(stage, app)
    except AnomalyBreakerTripped as e:
        return e
    return None


def invalidate() -> None:
    global _failed_at
    with _lock:
        _baseline.clear()
        _spend.clear()
        _alerted.clear()
        _failed_at = 0.0
