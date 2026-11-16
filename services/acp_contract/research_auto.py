"""services/acp_contract/research_auto.py — AA-1122 item 4: automatic search demand research, under caps.

Research used to run only when an administrator called the API (AA-646 took the automatic sweep away after the 25/09
spend; S273 lesson: switching a paid mechanism off must be replaced by a schedule with caps and a freshness guard, not
by "an admin clicks"). This module is that schedule. It never buys anything itself: events (a new tour atomized, a
rewrite, a rename) only change what the next selection returns, and only the nightly tick enqueues a
``segment_research`` job, which buys inside its caps (segment_research_job: the hard monthly cap and the automatic run
cap over DFS + LLM + Jev, re-checked before every paid post).

The tick (the job worker calls :func:`tick_safe` hourly, right after its landing sweep, shared/jobs/worker.py):

  1. off (``cis.seo.research_auto_enabled`` false) -> ``disabled``; outside the UTC window
     [``research_auto_window_start_utc``, ``_end_utc``) -> ``outside_window`` (a run blocks backend deploys, DM-11.4);
  2. a research job (segment_research, tour_paa_backfill) queued or running -> ``research_active``;
  3. ``research_auto_max_consecutive_failures`` automatic runs in a row failed / stopped since the last successful run
     (any run, a manual one resets it) -> ``paused_failures`` + alert ``research_auto_failed`` (critic C9: a run that
     fails after spending must not spend again every night);
  4. measure what waits ($0, bounded reads, stored Jev picks only: :func:`pending`): never-researched places and the
     longest wait, stale places per market, markets so stale they count as new (activation, left to an administrator);
  5. decide: a **new places** run when at least ``research_auto_new_min_places`` wait or the oldest waited
     ``research_auto_new_max_wait_days``; else a **refresh** run when the last automatic refresh run (any outcome) was
     created more than ``research_auto_refresh_every_days`` ago; else ``nothing_due``;
  6. money: the run may spend min(``cis.spend.research_auto_max_usd_per_run``, the month left under
     ``cis.spend.research_max_usd_per_month``, ``cis.spend.segment_research_max_usd_per_run``); below
     ``cis.spend.research_auto_min_run_usd`` -> ``skipped_cap`` + alert ``research_auto_capped``. An unreadable ledger
     refuses (fail closed);
  7. enqueue ONE ``segment_research`` job: scope ``auto_new`` / ``auto_refresh``, the markets active agencies use
     (kind agency, account status in ``research_market_tenant_statuses``) plus the platform default, minus activation
     markets; the endpoint ``research_auto_dfs_queue``; created_by ``system:auto``; idempotency key
     ``segment_research:auto:<kind>:<UTC day>`` (one per kind per day, for good: shared.job.idempotency_key is unique).

Alerts (shared/ops_checks.py, the admin bell + one e-mail a day): ``research_auto_failed``, ``research_auto_capped``,
``research_new_waiting`` (the oldest new place waited more than ``research_new_wait_alert_days``) and
``research_month_spend`` (the month spend reached ``research_month_warn_pct`` of the month cap).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from services.seo_intelligence import research_settings as rs

logger = structlog.get_logger()

KIND = "segment_research"
AUTO_CREATOR = "system:auto"
TICK_TIMEOUT_S = 180.0           # technical-constant: bound of one tick (bounded reads, at most one enqueue)
NEW_LIST = 50                    # technical-constant: rows of the panel's never-researched list (display only)
MASTER_TENANT = "00000000-0000-0000-0000-000000000001"

ELIGIBLE_MARKETS_SQL = """
    SELECT c.target_market
      FROM shared.tenants t
      LEFT JOIN shared.tenant_seo_config c ON c.tenant_id = t.tenant_id
     WHERE t.is_active AND t.cancelled_at IS NULL AND t.tenant_id <> $1::uuid
       AND t.kind = 'agency' AND t.account_status = ANY($2::text[])
"""
ACTIVE_RESEARCH_SQL = """
    SELECT 1 AS ok FROM shared.job WHERE kind = ANY($1::text[]) AND status IN ('queued', 'running') LIMIT 1
"""
# The latest research jobs (manual and automatic) for the failure streak and the refresh rhythm.
RECENT_JOBS_SQL = """
    SELECT id::text AS id, status, created_by, payload->>'scope' AS scope, created_at, finished_at
      FROM shared.job WHERE kind = $1 ORDER BY created_at DESC LIMIT 50
"""
FAILED = ("failed", "stopped_budget", "cancelled")


@dataclass
class Pending:
    """What waits for research ($0 measure)."""
    places_in_scope: int = 0
    new: int = 0
    oldest_new_wait_days: Optional[float] = None
    new_places: list = field(default_factory=list)      # [{place, tours, first_seen}] (NEW_LIST, most active first)
    stale: dict = field(default_factory=dict)           # market -> places stale there
    activation: list = field(default_factory=list)      # markets whose stale share marks them new
    markets: list = field(default_factory=list)         # the markets automatic research buys

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Decision:
    action: str                       # enqueue | disabled | outside_window | research_active | paused_failures |
    kind: Optional[str] = None        # nothing_due | skipped_cap | no_markets
    max_usd: Optional[float] = None
    reason: str = ""
    alerts: list = field(default_factory=list)          # [(check, summary, details)]


def _get(key: str):
    from shared import settings
    return settings.get(key)


def in_window(hour: int, start: int, end: int) -> bool:
    """[start, end) in UTC hours; a window that wraps midnight (start > end) works too. Pure."""
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def failure_streak(jobs: list[dict]) -> int:
    """Automatic runs in a row (newest first) that failed, stopped at a budget or were cancelled, until the newest run
    that succeeded (manual or automatic). Queued / running rows are skipped. Pure."""
    n = 0
    for j in jobs:
        if j["status"] in ("queued", "running"):
            continue
        if j["status"] == "succeeded":
            break
        if j.get("created_by") == AUTO_CREATOR and j["status"] in FAILED:
            n += 1
    return n


def last_refresh_at(jobs: list[dict]):
    """created_at of the newest automatic refresh run, whatever its outcome (critic C9). Pure."""
    for j in jobs:
        if j.get("created_by") == AUTO_CREATOR and j.get("scope") == "auto_refresh":
            return j["created_at"]
    return None


def decide(now: datetime, cfg: dict, pending: Pending, jobs: list[dict], month: Any) -> Decision:
    """The tick's decision (module docstring steps 5-6). Pure: `cfg` holds the settings in force, `month` the
    SharedCap of the month (cap and ledger spend), `jobs` the latest research jobs (newest first)."""
    alerts = []
    if pending.oldest_new_wait_days is not None and pending.oldest_new_wait_days > cfg["new_wait_alert_days"]:
        alerts.append(("research_new_waiting",
                       f"the oldest never-researched place has waited {pending.oldest_new_wait_days:.0f} days "
                       f"({pending.new} places wait)", {"new": pending.new,
                                                         "oldest_wait_days": pending.oldest_new_wait_days}))
    cap = month.cap_usd
    if cap and month.spent >= cap * cfg["month_warn_pct"] / 100.0:
        alerts.append(("research_month_spend", f"research spent ${month.spent:.2f} of the ${cap:.2f} month cap",
                       {"spent_usd": round(month.spent, 4), "cap_usd": cap}))
    new_due = pending.new > 0 and (pending.new >= cfg["new_min_places"] or (
        pending.oldest_new_wait_days is not None and pending.oldest_new_wait_days >= cfg["new_max_wait_days"]))
    last = last_refresh_at(jobs)
    refresh_due = last is None or now - last >= timedelta(days=cfg["refresh_every_days"])
    if new_due:
        kind = "new"
    elif refresh_due:
        kind = "refresh"
    else:
        return Decision("nothing_due", alerts=alerts,
                        reason=f"{pending.new} new places (start at {cfg['new_min_places']} or "
                               f"{cfg['new_max_wait_days']} days); last refresh {last}")
    if not pending.markets:
        return Decision("no_markets", kind=kind, alerts=alerts, reason="every target market is new (activation)")
    money = min(cfg["auto_max_usd_per_run"], month.remaining(), cfg["max_usd_per_run"])
    if money < cfg["min_run_usd"] or money <= 0:
        alerts.append(("research_auto_capped",
                       f"automatic {kind} run skipped: ${money:.2f} left (month ${month.spent:.2f} of "
                       f"${cap if cap is not None else 0:.2f}); {pending.new} new places wait",
                       {"kind": kind, "left_usd": round(money, 4), "new": pending.new, "stale": pending.stale}))
        return Decision("skipped_cap", kind=kind, max_usd=round(money, 4), alerts=alerts,
                        reason="the money left is below the smallest run worth starting")
    return Decision("enqueue", kind=kind, max_usd=round(money, 4), alerts=alerts,
                    reason=f"{kind} run due" + (f": {pending.new} new places" if kind == "new" else ""))


def config() -> dict:
    """The settings in force for one tick (named in research_settings, CIS 442)."""
    from services.jobs.segment_research_job import max_usd_per_run

    return {
        "enabled": bool(_get(rs.AUTO_ENABLED)),
        "window_start": int(_get(rs.AUTO_WINDOW_START_UTC)), "window_end": int(_get(rs.AUTO_WINDOW_END_UTC)),
        "new_min_places": int(_get(rs.AUTO_NEW_MIN_PLACES)), "new_max_wait_days": int(_get(rs.AUTO_NEW_MAX_WAIT_DAYS)),
        "refresh_every_days": int(_get(rs.AUTO_REFRESH_EVERY_DAYS)),
        "max_failures": int(_get(rs.AUTO_MAX_CONSECUTIVE_FAILURES)),
        "activation_stale_pct": float(_get(rs.AUTO_ACTIVATION_STALE_PCT)),
        "new_wait_alert_days": int(_get(rs.NEW_WAIT_ALERT_DAYS)), "month_warn_pct": float(_get(rs.MONTH_WARN_PCT)),
        "auto_max_usd_per_run": float(_get(rs.AUTO_MAX_USD_PER_RUN)), "max_usd_per_run": max_usd_per_run(),
        "min_run_usd": float(_get(rs.AUTO_MIN_RUN_USD)), "dfs_queue": str(_get(rs.AUTO_DFS_QUEUE)),
        "tenant_statuses": list(_get(rs.MARKET_TENANT_STATUSES) or []),
    }


async def auto_markets(pool, statuses: list[str]) -> list[str]:
    """The markets automatic research buys: the targeted markets of active agencies whose account status is listed
    (never demo / internal tenants, critic C5), plus the platform default (a tenant with no market resolves to it)."""
    from services.acp_contract.research_coverage import targeted_markets

    rows = await pool.fetch(ELIGIBLE_MARKETS_SQL, MASTER_TENANT, list(statuses))
    return targeted_markets([r["target_market"] for r in rows] + [None])


async def pending(pool, markets: list[str], activation_stale_pct: float, now: Optional[datetime] = None) -> Pending:
    """What waits, $0: the scope (stored Jev picks only), its research rows and the per-market staleness (the research
    run's own definitions: segment_research.stale_tiers / log_ages)."""
    from services.acp_contract import research_coverage as rc
    from services.acp_contract import research_plan as rp
    from services.acp_contract import segment_research as sr
    from services.seo_intelligence.research_cache import fresh_windows

    now = now or datetime.now(timezone.utc)
    rows = await sr.scoped_rows(pool, cached_only=True)
    info = rp.place_info(rows)
    names = sorted(info)
    ages = await rp.log_ages(pool, names)
    fresh = await sr._fresh_tiers(pool, names, await fresh_windows()) if names else {}
    new = [n for n in names if n not in ages]
    out = Pending(places_in_scope=len(names), new=len(new))
    seen = [info[n].first_seen for n in new if info[n].first_seen is not None]
    if seen:
        out.oldest_new_wait_days = round((now - min(seen)).total_seconds() / 86400.0, 1)
    ordered = sorted(new, key=lambda n: rp.order_key("new", n, info, ages))
    out.new_places = [{"place": n, "tours": info[n].tours,
                       "first_seen": info[n].first_seen.isoformat() if info[n].first_seen else None}
                      for n in ordered[:NEW_LIST]]
    buy = []
    for m in markets:
        stale = sum(1 for n in names if rc.is_stale(fresh.get(n, set()), m))
        out.stale[m] = stale
        if names and 100.0 * stale / len(names) > activation_stale_pct:
            out.activation.append(m)
        else:
            buy.append(m)
    out.markets = buy
    return out


async def recent_jobs(pool) -> list[dict]:
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch(RECENT_JOBS_SQL, KIND)]


async def _active(pool) -> bool:
    from shared.research_budget import RESEARCH_JOB_KINDS
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(ACTIVE_RESEARCH_SQL, list(RESEARCH_JOB_KINDS)))


async def _alert(pool, alerts: list) -> dict:
    from shared import ops_checks

    if not alerts:
        return {}
    return await ops_checks.alert_all(pool, [ops_checks.Finding(c, s, d) for c, s, d in alerts])


def _closed(cfg: dict, now: datetime, force: bool) -> Optional[str]:
    """The reason the tick does nothing before any read ("disabled", "outside_window"), or None. Pure."""
    if force:
        return None
    if not cfg["enabled"]:
        return "disabled"
    return None if in_window(now.hour, cfg["window_start"], cfg["window_end"]) else "outside_window"


async def _enqueue(pool, d: Decision, waiting: Pending, cfg: dict, now: datetime) -> dict:
    """Enqueue the decided run: one per kind per UTC day (idempotency key), created_by system:auto."""
    from shared.jobs.registry import enqueue

    payload = {"markets": waiting.markets, "scope": f"auto_{d.kind}", "max_usd": d.max_usd,
               "dfs_queue": cfg["dfs_queue"], "tiers": ["volume", "serp"], "strategy": "batch", "recompute": True}
    key = f"{KIND}:auto:{d.kind}:{now.strftime('%Y-%m-%d')}"
    job_id, created = await enqueue(pool, KIND, payload, idempotency_key=key, created_by=AUTO_CREATOR)
    logger.info("research_auto_enqueued", job_id=job_id, created=created, kind=d.kind, max_usd=d.max_usd,
                markets=waiting.markets, new=waiting.new)
    return {"job_id": job_id, "created": created}


def _last_failed(jobs: list[dict], streak: int) -> list:
    """The alert for an automatic run that just failed (below the pause streak). Pure."""
    if streak and jobs and jobs[0].get("created_by") == AUTO_CREATOR and jobs[0]["status"] in FAILED:
        return [("research_auto_failed", f"the last automatic research run {jobs[0]['status']}",
                 {"job_id": jobs[0]["id"], "streak": streak})]
    return []


async def _month(pool, now: datetime):
    """(the month cap, None) or (None, the alert): an unreadable ledger never starts a run (fail closed)."""
    from shared import research_budget

    try:
        return await research_budget.load_research_month_cap(pool, now), None
    except Exception as exc:  # noqa: BLE001
        return None, ("research_auto_failed", "the research spend ledger could not be read; no automatic run",
                      {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})


async def tick(pool, *, now: Optional[datetime] = None, force: bool = False, dry_run: bool = False) -> dict:
    """One tick (module docstring). `force` skips the switch and the window (an administrator's check, tests);
    `dry_run` decides and returns the decision without enqueueing or alerting ($0)."""
    from services.acp_contract.research_coverage import ReadOnlyPool

    now = now or datetime.now(timezone.utc)
    cfg = config()
    closed = _closed(cfg, now, force) or ("research_active" if await _active(pool) else None)
    if closed:
        return {"action": closed}
    jobs = await recent_jobs(pool)
    streak = failure_streak(jobs)
    if streak >= cfg["max_failures"]:
        await _alert(pool, [] if dry_run else [(
            "research_auto_failed", f"{streak} automatic research runs in a row failed or stopped; automatic research "
            "waits until a run succeeds", {"streak": streak, "last": jobs[0]["id"] if jobs else None})])
        return {"action": "paused_failures", "streak": streak}
    ro = ReadOnlyPool(pool, statement_timeout="20s")
    waiting = await pending(ro, await auto_markets(ro, cfg["tenant_statuses"]), cfg["activation_stale_pct"], now)
    month, failed = await _month(pool, now)
    if month is None:
        await _alert(pool, [] if dry_run else [failed])
        return {"action": "ledger_error"}
    d = decide(now, cfg, waiting, jobs, month)
    out = {"action": d.action, "kind": d.kind, "max_usd": d.max_usd, "reason": d.reason, "markets": waiting.markets,
           "activation": waiting.activation, "new": waiting.new, "month": month.summary(),
           "alerts": [a[0] for a in d.alerts]}
    if dry_run:
        return {**out, "dry_run": True}
    if d.action == "enqueue":
        out.update(await _enqueue(pool, d, waiting, cfg, now))
    out["alerted"] = await _alert(pool, d.alerts + _last_failed(jobs, streak))
    return out


async def tick_safe(pool) -> dict:
    """The worker tick's entry: never raises."""
    try:
        return await tick(pool)
    except Exception as exc:  # noqa: BLE001 — the next tick tries again
        logger.warning("research_auto_tick_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        return {"action": "error", "error": str(exc)[:200]}


__all__ = ["tick", "tick_safe", "decide", "pending", "auto_markets", "config", "failure_streak", "in_window",
           "Pending", "Decision", "AUTO_CREATOR"]
