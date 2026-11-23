"""AA-1140 (S289) — the Ecosystem status page: ``GET /admin/overview/status`` answers one question for the four apps
(CIS, TripPlanner, Booking, Partner CRM): what is broken or about to break now, and what happened over the chosen range.

``build_status(pool, redis, ...)`` assembles, side by side, sections that each carry their own ``as_of`` and refresh
time and degrade on their own (S285 advisory phase 1-2, mockup v2 approved 19/11):

- ``signals``  golden-signal series (CloudWatch GetMetricData in the range's chart step, cached 5 minutes per range
               in Redis; never without Redis: no AWS call per page view), with the previous period when ``compare``;
- ``apps``     one tile per app: traffic totals coloured by the page's limits, overall health (one definition,
               ``app_health``), deployed commit, readiness, the activity line;
- ``runtimes`` ECS services and health checks with the uptime strip against the objective (the 5-minute sampler,
               ``acp_shared.ops_sample``, CIS 475);
- ``resources`` RDS CPU / connections / free storage, the busiest DB role, the XID age, ECS tasks;
- ``jobs``     shared.job (live backlog + failures of the range), crm.job (crm.ops.v1), booking.job not measured yet,
               the deploy gate, top failing kinds;
- ``ai``       today's spend per app against its daily cap, spend per hour by connection;
- ``cost``     month to date, forecast and budget (the Spend page's forecast, cached 15 minutes);
- ``events``   the timeline (deploys, alarms, service drops, ops check bells, job failures);
- ``delivery`` main branch state per repo and the nightly guards.

``verdict`` (``verdict.decide``) turns them into one sentence and the Needs attention list; ``thresholds`` lists every
limit with its settings key (``shared/ops_status_thresholds.py``, CIS 475) so the page draws it and links Settings;
``sources`` gives each section's source, refresh time, ``as_of`` and whether it is stale (older than
``cis.ops.status_stale_factor`` x its refresh time). Cost: CloudWatch about 0.00025 USD per range a cache miss
(0.0005 with compare), everything else free reads; no new AWS service, no new IAM, no new cross-app grant.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from api.services.ops_overview import cache
from api.services.ops_status import cw_series, sections, store, verdict
from api.services.ops_status.ranges import RangeError, TimeRange, resolve
from shared import ops_status_thresholds as th

logger = structlog.get_logger()

KEY_PREFIX = "admin:overview:status"
SIGNALS_TTL_S = 300  # technical-constant: cache refresh of a read (Redis TTL)
COST_TTL_S = 900  # technical-constant: cache refresh of a read (Redis TTL)

SOURCES = {
    "signals": "AWS CloudWatch GetMetricData: API Gateway (CIS, Booking APIs), Lambda (TripPlanner functions), RDS",
    "apps": "CloudWatch series of this range, the app cards of App health (health and readiness checks, deployed "
            "commit) and the activity of the range (CIS tables, TripPlanner and Booking contract views)",
    "runtimes": "The 5-minute status sampler: ECS service state and the public health checks (acp_shared.ops_sample)",
    "resources": "CloudWatch RDS metrics of this range and the sampler's database rows (busiest role, XID age)",
    "jobs": "shared.job (live), crm.job through the partner CRM ops contract (your own sign-in), the deploy gate",
    "ai": "AI gateway ledger (shared.llm_call_log today, shared.ai_usage_hourly for the range) and app profiles",
    "cost": "The Spend page forecast: LLM gateway estimates and stored Cost Explorer snapshots",
    "events": "Sampler events (deploys, alarms, service drops), ops check bells and failed jobs of the range",
    "delivery": "The published delivery summary (CI and deploys of the six repos) and the nightly probe results",
}


def range_of(range_key: Optional[str], start: Optional[str], end: Optional[str],
             now: Optional[datetime] = None) -> TimeRange:
    return resolve(range_key, start, end, now=now or datetime.now(timezone.utc), history_days=th.history_days())


async def _signals(redis: Any, rng: TimeRange) -> dict:
    async def compute() -> dict:
        return await asyncio.to_thread(cw_series.collect, rng)

    name = f"signals:{rng.key}:{int(rng.start.timestamp())}:{int(rng.end.timestamp())}:{rng.step_s}"
    return await cache.cached_section(redis, name, SIGNALS_TTL_S, compute, needs_cache=True, key_prefix=KEY_PREFIX)


async def _cost(redis: Any, pool: Any, now: datetime) -> dict:
    async def compute() -> dict:
        return await sections.cost(pool, now)

    return await cache.cached_section(redis, "cost", COST_TTL_S, compute, needs_cache=False, key_prefix=KEY_PREFIX)


async def _cards(pool: Any, redis: Any) -> dict[str, dict]:
    from api.services import platform_apps

    try:
        apps = await platform_apps.collect_apps(pool, redis)
        return {a["id"]: a for a in apps.get("apps", [])}
    except Exception as e:  # noqa: BLE001 — the tiles fall back to traffic alone
        logger.warning("ops_status_cards_failed", error=repr(e)[:200])
        return {}


async def _health(pool: Any, now: datetime) -> dict[str, dict]:
    fresh_after = now - timedelta(minutes=store.SAMPLE_MINUTES * th.stale_factor())
    rows = await store.latest(pool, ["app_health"], now - timedelta(days=1))
    return {r["subject"]: {"state": r["state"] if r["sampled_at"] >= fresh_after else "no_sample",
                           "reason": r["value"].get("reason"), "sampled_at": r["sampled_at"].isoformat()}
            for r in rows}


def series_view(sig: dict, rng: TimeRange) -> dict:
    """The charts' data: step times, per-app series, the rds series stay in resources. Pure."""
    if sig.get("status") != "ok":
        return {k: v for k, v in sig.items() if k in ("status", "reason", "message")}
    apps = {}
    for app, s in (sig.get("apps") or {}).items():
        if not s.get("measured"):
            apps[app] = {"measured": False}
            continue
        req = s.get("requests") or []
        err = s.get("errors") or []
        minutes = rng.step_s / 60.0
        apps[app] = {"measured": True,
                     "rate_per_min": [None if v is None else round(v / minutes, 3) for v in req],
                     "error_pct": [None if r is None or not r or e is None else round(100.0 * e / r, 3)
                                   for r, e in zip(req, err)],
                     "requests": req, "p95_ms": s.get("p95_ms") or []}
    return {"status": "ok", "as_of": sig.get("as_of"), "apps": apps}


def freshness(name: str, section: dict, now: datetime, factor: float) -> dict:
    """One row of Sources and freshness. Pure."""
    refresh = int(section.get("refresh_s") or SIGNALS_TTL_S)
    as_of = section.get("as_of")
    stale = None
    if as_of:
        try:
            age = (now - datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))).total_seconds()
            stale = age > factor * refresh
        except ValueError:
            stale = None
    state = section.get("status")
    if state == "ok" and stale:
        state = "stale"
    return {"section": name, "source": SOURCES.get(name, ""), "refresh_s": refresh, "as_of": as_of,
            "state": state, "reason": section.get("reason")}


async def build_status(pool: Any, redis: Any = None, *, range_key: Optional[str] = None, start: Optional[str] = None,
                       end: Optional[str] = None, compare: bool = False, authorization: Optional[str] = None,
                       now: Optional[datetime] = None) -> dict:
    """The whole page. RangeError on a range the page cannot show (the router answers 422)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rng = range_of(range_key, start, end, now)
    prev = rng.previous() if compare else None

    async def none() -> Optional[dict]:
        return None

    sig, sig_prev, cards, acts, health = await asyncio.gather(
        _signals(redis, rng), _signals(redis, prev) if prev else none(), _cards(pool, redis),
        sections.run("activity", sections.activity(pool, rng)), _health(pool, now))
    run = sections.run
    rt, res, jobs, ai, cost, ev_src, dl = await asyncio.gather(
        run("runtimes", sections.runtimes(pool, rng, now)),
        run("resources", sections.resources(pool, sig, rng, now)),
        run("jobs", sections.jobs(pool, rng, authorization)),
        run("ai", sections.ai(pool, rng, now, authorization)),
        run("cost", _cost(redis, pool, now)),
        sections.run("events", _events(pool, rng)),
        run("delivery", sections.delivery(pool, now)))
    events_rows = ev_src.get("data", []) if ev_src.get("status") == "ok" else []
    apps = sections.ok(sections.app_tiles(sig, cards, acts if isinstance(acts, dict) and "status" not in acts else {},
                                          health), 300, as_of=_as_of(sig, now))
    secs = {"signals": {**series_view(sig, rng), "refresh_s": SIGNALS_TTL_S}, "apps": apps, "runtimes": rt,
            "resources": res, "jobs": jobs, "ai": ai, "cost": cost, "events": ev_src, "delivery": dl}
    if prev is not None:
        secs["signals"]["previous"] = {**series_view(sig_prev or {}, prev), "range": prev.as_dict()}
    deploys = [e for e in events_rows if e["lane"] == "deploy"]
    bands = [{"start": e["t"], "end": e.get("t2"), "app": e["app"], "severity": e["severity"], "text": e["text"]}
             for e in events_rows if e["kind"] == "alarm"]
    secs["signals"]["deploys"] = [{"t": e["t"], "app": e["app"], "text": e["text"]} for e in deploys]
    secs["signals"]["alarm_bands"] = bands
    thresholds = th.snapshot()
    factor = th.stale_factor()
    return {
        "generated_at": now.isoformat(),
        "environment": cw_series.cloudwatch.environment(),
        "range": rng.as_dict(),
        "previous": prev.as_dict() if prev else None,
        "buckets": [b.isoformat() for b in rng.buckets()],
        "verdict": verdict.decide(secs, thresholds, events_rows),
        "thresholds": thresholds,
        "sections": secs,
        "sources": [freshness(k, v, now, factor) for k, v in secs.items()],
    }


def _as_of(sig: dict, now: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(sig.get("as_of")).replace("Z", "+00:00"))
    except ValueError:
        return now


async def _events(pool: Any, rng: TimeRange) -> dict:
    ev, bells, failures = await sections.event_sources(pool, rng)
    return sections.ok(sections.event_rows(ev, bells, failures), 300)


__all__ = ["RangeError", "build_status", "range_of"]
