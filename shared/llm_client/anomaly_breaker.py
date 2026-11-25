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
(``GET /admin/ai-gateway/spend-controls``). Only the apps in ``ENFORCED_APPS`` (CIS) have a gateway that reads the
breaker, so only they are refused in mode enforce; TripPlanner and the CRM show their verdict with ``enforced`` false
("over the line, not enforced for this app yet") until their own gateways call it (a follow-up lane). Every app's
trip still rings the bell: ``alert_tripped`` runs in each ``llm_route_watch`` watch run (every 15 min), not only on
CIS calls (review S283). Reads are
cached per process (the baseline per app and UTC day, on the call path and in ``evaluate`` alike; today's spend
``SPEND_TTL_SECONDS`` on the call path, fresh in ``evaluate``). Never fails closed: a
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
ENFORCED_APPS = ("cis",)                  # the apps whose gateway refuses on the breaker (this one); see the docstring

# The app of a ledger row: the platform_cost rule (an old CIS row has no app). One range scan on created_at
# (llm_call_log_created_id_idx) with this test as a cheap Filter: the ledger has no index on app, so a UNION ALL of
# `app = $n` and `app IS NULL AND ...` would scan and heap-fetch the window twice (S291 review, AA-838).
_APP_EXPR = "coalesce(l.app, l.quality_signal ->> 'app', 'cis')"
TODAY_SQL = f"""
    SELECT coalesce(sum(l.cost_usd), 0)::float AS usd
      FROM shared.llm_call_log l
     WHERE l.created_at >= $1 AND {_APP_EXPR} = $2
"""
# $1 / $2 = the UTC dates [start, end), $3 = the app. A day is either rolled up or raw, never both (platform_cost): a
# raw day is read only when shared.llm_call_daily has no row for it (AA-838, S291, C4.3 W48 finding 3).
# AA-1179 (S293): each raw day is its own sum over a [day 00:00 UTC, next day 00:00 UTC) range of the raw created_at
# (a LATERAL per day: an index range on llm_call_log_created_id_idx, no expression on the column), instead of one scan
# of the whole window grouped by (created_at AT TIME ZONE 'UTC')::date. The grouped form sorted every ledger row of the
# window (~52,000 rows for 28 days on Dev) and spilled the sort to temp; the per-day sums need no sort, and a rolled day
# skips its scan (the NOT EXISTS is a one-time filter of the day). Same median (tests/integration/
# test_aa1126_gateway_caps_sql.py compares it with both earlier statements).
BASELINE_SQL = f"""
    WITH days AS (
        SELECT generate_series($1::date, $2::date - 1, interval '1 day')::date AS day
    ), rolled AS (
        SELECT d.day, sum(d.cost_usd)::float AS usd
          FROM shared.llm_call_daily d
         WHERE d.day >= $1 AND d.day < $2 AND d.app = $3
         GROUP BY 1
    )
    SELECT coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(r.usd, w.usd, 0)), 0)::float AS median
      FROM days
      LEFT JOIN rolled r USING (day)
      LEFT JOIN LATERAL (
            SELECT sum(l.cost_usd)::float AS usd
              FROM shared.llm_call_log l
             WHERE l.created_at >= (days.day::timestamp AT TIME ZONE 'UTC')
               AND l.created_at < ((days.day + 1)::timestamp AT TIME ZONE 'UTC')
               AND {_APP_EXPR} = $3
               AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d WHERE d.day = days.day)
           ) w ON true
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
    def enforced(self) -> bool:
        """The app's gateway reads the breaker (``ENFORCED_APPS``): in mode enforce a trip refuses its calls."""
        return self.app in ENFORCED_APPS

    @property
    def refuses(self) -> bool:
        return self.tripped and self.mode == caps_settings.ENFORCE and self.enforced

    def as_dict(self) -> dict:
        return {**asdict(self), "refuses": self.refuses, "enforced": self.enforced}


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


async def cached_median(conn, app: str, today: date, days: int) -> float:
    """The normal day of ``app`` through this process's baseline cache, the one the call path fills (key: app, UTC
    date, days). The baseline days are whole UTC days before today, so the median cannot change until the day rolls
    over (a rollup moves a day's rows into shared.llm_call_daily with the same sum). AA-838 (S291, C4.3 W48 finding 3):
    ``evaluate`` read the median on every call, and the llm_route_watch run evaluates every app each 15 minutes (3 apps
    x 96 runs = 288 baseline reads a day, the ~300 W48 saw) besides the admin page; it is now read once per process,
    app and UTC day. No lock: a dict read / write is atomic, and ``_lock`` may be held by a sync loader thread."""
    key = (app, today, days)
    hit = _baseline.get(key)
    if hit is not None:
        return hit
    median = await read_median(conn, app, today, days)
    if len(_baseline) > 100:
        _baseline.clear()
    _baseline[key] = median
    return median


async def evaluate(conn, app: str, *, mode: str, policy: caps_settings.AnomalyPolicy,
                   profile: Optional[app_profile.AppProfile] = None, now: Optional[datetime] = None) -> Verdict:
    """The verdict of ``app`` now, read through ``conn``: today's spend fresh, the median through the per-process
    baseline cache (``cached_median``). Used by the admin status, the reset and the watch run."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reset_at = getattr(profile, "anomaly_reset_at", None)
    since = window_start(now, reset_at)
    spent = await read_spend_since(conn, app, since)
    median = await cached_median(conn, app, now.date(), policy.baseline_days)
    return verdict(app, mode, policy, since=since, spent_usd=spent, median_usd=median, reset_at=reset_at,
                   reset_by=getattr(profile, "anomaly_reset_by", None))


async def verdicts(conn, *, now: Optional[datetime] = None) -> list[Verdict]:
    """The verdict of every app with AI, read through ``conn`` (no cache); [] when the breaker is off."""
    mode, policy = caps_settings.anomaly_mode(), caps_settings.anomaly_policy()
    if mode == caps_settings.OFF or policy is None:
        return []
    try:
        profiles = await app_profile.read_all(conn)
    except app_profile.AppProfileUnavailable:
        profiles = {}
    return [await evaluate(conn, app, mode=mode, policy=policy, profile=profiles.get(app), now=now) for app in APPS]


async def status(conn, *, now: Optional[datetime] = None) -> dict:
    """The admin view: the settings in force and the verdict of every app with AI (mode off: no verdicts read)."""
    mode, policy = caps_settings.anomaly_mode(), caps_settings.anomaly_policy()
    out = {"mode": mode, "policy": None if policy is None else asdict(policy), "apps": []}
    if mode == caps_settings.OFF or policy is None:
        return out
    out["apps"] = [v.as_dict() for v in await verdicts(conn, now=now)]
    return out


async def alert_tripped(conn, *, now: Optional[datetime] = None) -> list[str]:
    """The periodic evaluation (each ``llm_route_watch`` watch run): ring the bell of every tripped app, whatever app
    it is (the CIS call path only sees CIS). The bell is once per app and 24 h (``cost_guard.maybe_alert_central``).
    Returns the tripped apps. Raises what the reads raise (the job logs it)."""
    tripped = [v for v in await verdicts(conn, now=now) if v.tripped]
    for v in tripped:
        if _first_time(v):
            logger.warning("llm_anomaly_breaker_tripped", source="watch", **v.as_dict())
            await alert_on(conn, v)
    return [v.app for v in tripped]


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
    if v.refuses:
        action = ("new AI calls of the app are refused until an administrator resets the breaker (AI gateway > "
                  "Budgets > Spend controls) or the UTC day ends")
    elif v.mode == caps_settings.ENFORCE and not v.enforced:
        action = "alert only: this app's gateway does not read the breaker yet, so its calls go on"
    else:
        action = "alert only: calls go on"
    return (f"AI anomaly breaker tripped for app {v.app}: ${v.spent_usd:.2f} spent since {v.since} is above "
            f"${v.threshold_usd:.2f} ({v.multiple:g} x the ${v.median_usd:.2f} median day of the last "
            f"{v.baseline_days} days, floor ${v.floor_usd:.2f}); {action}.")


async def alert_on(conn, v: Verdict) -> None:
    """The bell + ops e-mail of a tripped verdict through ``conn`` (deduplicated per scope and 24 h)."""
    summary = {"provider": "llm", "scope": f"{SCOPE_PREFIX}{v.app}", "day_spent_usd": round(v.spent_usd, 4),
               "per_day_usd": round(v.threshold_usd, 4), "hard_stop": v.refuses, "hard_stop_day": v.refuses,
               "used_pct": round(100.0 * v.spent_usd / v.threshold_usd, 1) if v.threshold_usd > 0 else None}
    await cost_guard.maybe_alert_central(conn, summary, alert_pct=100, stopped=v.refuses, message=message(v))


async def _alert(v: Verdict) -> None:
    conn = await connections._connect()
    try:
        await alert_on(conn, v)
    finally:
        await conn.close()


def send(v: Verdict) -> None:
    """Write the bell (tests replace it; no database in unit tests)."""
    connections.run_sync(lambda: _alert(v))


def _first_time(v: Verdict) -> bool:
    """True once per trip window and state in this process (then False): the bell is not rewritten on every call or
    every watch run; the bell itself is deduplicated per scope and 24 h too."""
    key = (v.app, v.since, v.refuses)
    if key in _alerted:
        return False
    if len(_alerted) > 100:
        _alerted.clear()
    _alerted.add(key)
    return True


def alert(v: Verdict) -> None:
    """One admin bell + ops e-mail per app and 24 h (scope llm:anomaly:<app>); once per trip window in this process
    (no database call on every refused call). Best effort: never raises."""
    if not _first_time(v):
        return
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
