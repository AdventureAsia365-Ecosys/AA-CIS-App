"""AA-1140 (S289) — the Ecosystem status page: ``GET /admin/overview/status`` answers one question for the four apps
(CIS, TripPlanner, Booking, Partner CRM): what is broken or about to break now, and what happened over the chosen range.

``build_status(pool, redis, ...)`` assembles, side by side, sections that each carry their own ``as_of`` and refresh
time and degrade on their own (S285 advisory phase 1-2, mockup v2 approved 19/11):

- ``signals``  golden-signal series (CloudWatch GetMetricData in the range's chart step, read on a 5-minute grid and
               cached 5 minutes in Redis, the previous period hourly; never without Redis: no AWS call per page
               view), with the previous period when ``compare``;
- ``apps``     one tile per app: traffic totals coloured by the page's limits (the CIS client errors without the
               refused staff session checks, shown apart as ``session_checks``: :mod:`.session_checks`), overall
               health (one definition, ``app_health``), deployed commit, readiness, the activity line;
- ``runtimes`` ECS services and health checks with the uptime strip against the objective (the 5-minute sampler,
               ``acp_shared.ops_sample``, CIS 475);
- ``resources`` RDS CPU / connections / free storage, the busiest DB role, the XID age, ECS tasks;
- ``jobs``     shared.job (live backlog + failures of the range), crm.job (crm.ops.v1), booking.job not measured yet,
               the deploy gate, top failing kinds;
- ``ai``       today's spend per app against its daily cap, spend per hour by connection;
- ``cost``     month to date, forecast and budget (the Spend page's forecast, cached 15 minutes);
- ``events``   the timeline (deploys, alarms, service drops, ops check bells, job failures);
- ``delivery`` main branch state per repo and the nightly guards.

AA-1141 (P1, S291) adds, all free:
- ``changes``  "What changed": deploys, setting changes, migrations, waves and route control versions of the range
               (:mod:`.changes`), also drawn as typed annotations on every chart (``signals.annotations``);
- ``forecast`` capacity forecast: days until RDS storage, each AI connection's monthly cap (the Actions cache has no
               source: not measured) reach their limit (:mod:`.forecast`);
- ``anomaly``  AI cost anomaly by app x provider x route: an hour above N x its median hour (:mod:`.anomaly`);
- ``acks``     ack / snooze / mute in force on the attention items, the recent trail and the rules (:mod:`.acks`);
               ``viewer.can_ack`` says whether this caller may act (ops.write, never the business owner);
- ``pipelines`` freshness of the background producers (sampler, digest, probes, delivery summary, schema dump).

``verdict`` (``verdict.decide``) turns them into one sentence and the Needs attention list; ``thresholds`` lists every
limit with its settings key (``shared/ops_status_thresholds.py``, CIS 475) so the page draws it and links Settings;
``sources`` gives each section's source, refresh time, ``as_of`` and whether it is stale (older than
``cis.ops.status_stale_factor`` x its refresh time). Cost: CloudWatch about 0.00025 USD a cache miss (about 25
metrics); the current period misses at most once per 5 minutes per preset (``fetch_range``: a live range is read on
a 5-minute grid, so the 1 h view does not miss on each 60 s refresh) and the previous period once an hour, so a
view left open all day is about 288 misses, 0.07 USD a day at most (about 2 USD a month, shared by every viewer of
the same preset); everything else free reads; no new AWS service, no new IAM, no new cross-app grant.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from api.services.issue_refs import clean
from api.services.ops_overview import cache
from api.services.ops_status import (
    acks, anomaly, changes, cw_series, forecast, pipelines, sections, session_checks, store, verdict,
)
from api.services.ops_status.ranges import RangeError, TimeRange, resolve
from shared import ops_status_thresholds as th

logger = structlog.get_logger()

KEY_PREFIX = "admin:overview:status"
SIGNALS_TTL_S = 300  # technical-constant: cache refresh of a read (Redis TTL)
PREVIOUS_TTL_S = 3600  # technical-constant: cache life of a complete past period (Redis TTL)
PREVIOUS_GRID_S = 3600  # technical-constant: read grid of a past period, so its cache key moves hourly
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
    "changes": "Sampler deploy events, approved setting values, shared.schema_versions, rerun waves and route control "
               "versions of this range",
    "forecast": "CloudWatch RDS free storage trend (cached 6 h), the AI connection budgets and their month to date",
    "anomaly": "The hourly AI usage rollup (shared.ai_usage_hourly): the last two hours against the median hour",
}


def range_of(range_key: Optional[str], start: Optional[str], end: Optional[str],
             now: Optional[datetime] = None) -> TimeRange:
    return resolve(range_key, start, end, now=now or datetime.now(timezone.utc), history_days=th.history_days())


def _epoch_floor(t: datetime, step_s: int) -> datetime:
    e = int(t.timestamp())
    return datetime.fromtimestamp(e - e % step_s, tz=timezone.utc)


def _epoch_ceil(t: datetime, step_s: int) -> datetime:
    f = _epoch_floor(t, step_s)
    return f if f == t else f + timedelta(seconds=step_s)


def fetch_range(rng: TimeRange, *, previous: bool = False) -> TimeRange:
    """The range actually read from CloudWatch (and the cache key) for the page's range ``rng``. Pure.

    A live preset moves with the clock (the 1 h view every minute), so reading it as is would make every refresh a
    paid cache miss. The current period is read on a grid of ``max(step, SIGNALS_TTL_S)``: its end is rounded up to
    that grid (the same span, at most 4 steps later), so the key moves at most every 5 minutes. A previous period
    (compare) is complete and will not change: it is read on an hourly grid, widened to cover the page's range, and
    cached ``PREVIOUS_TTL_S``. A custom range is fixed and read as is. Same step, so every bucket of the fetch lies
    on the page's grid (:func:`relay`)."""
    if not rng.is_live:
        return rng
    if previous:
        grid = max(rng.step_s, PREVIOUS_GRID_S)
        return TimeRange(rng.key, _epoch_floor(rng.start, grid), _epoch_ceil(rng.end, grid), rng.step_s)
    grid = max(rng.step_s, SIGNALS_TTL_S)
    end = _epoch_ceil(rng.end, grid)
    return TimeRange(rng.key, end - rng.span, end, rng.step_s)


def relay(sig: dict, src: TimeRange, dst: TimeRange) -> dict:
    """The series of a fetch over ``src`` laid on the page's range ``dst`` (same step): a step ``src`` does not
    cover is None. Totals stay those of the fetch (the same span; the steps it adds lie after now). Pure."""
    if sig.get("status") != "ok" or src == dst:
        return sig
    n_src = len(src.buckets())
    index = {int(b.timestamp()): i for i, b in enumerate(src.buckets())}
    picks = [index.get(int(b.timestamp())) for b in dst.buckets()]

    def lay(values: Any) -> Any:
        if not isinstance(values, list) or len(values) != n_src:
            return values
        return [None if i is None else values[i] for i in picks]

    out = dict(sig)
    out["apps"] = {app: {k: lay(v) for k, v in s.items()} for app, s in (sig.get("apps") or {}).items()}
    if isinstance(sig.get("rds"), dict):
        out["rds"] = {k: lay(v) for k, v in sig["rds"].items()}
    return out


async def _signals(redis: Any, rng: TimeRange, *, previous: bool = False) -> dict:
    src = fetch_range(rng, previous=previous)

    async def compute() -> dict:
        sig = await asyncio.to_thread(cw_series.collect, src)
        if previous:
            return sig
        # S290: the refused staff session checks over the same span, kept out of the CIS client error rate
        return session_checks.apply(sig, await session_checks.count(redis, src.start, src.end))

    kind = "signals_prev" if previous else "signals"
    name = f"{kind}:{src.key}:{int(src.start.timestamp())}:{int(src.end.timestamp())}:{src.step_s}"
    ttl = PREVIOUS_TTL_S if previous else SIGNALS_TTL_S
    sig = await cache.cached_section(redis, name, ttl, compute, needs_cache=True, key_prefix=KEY_PREFIX)
    return relay(sig, src, rng)


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
    fresh_after = store.fresh_since(now)
    rows = await store.latest(pool, ["app_health"], now - timedelta(days=th.history_days()))
    return {r["subject"]: {"state": r["state"] if r["sampled_at"] >= fresh_after else "no_sample",
                           "reason": clean(r["value"].get("reason"), 300), "sampled_at": r["sampled_at"].isoformat()}
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
                       now: Optional[datetime] = None, can_ack: bool = False) -> dict:
    """The whole page. RangeError on a range the page cannot show (the router answers 422)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rng = range_of(range_key, start, end, now)
    prev = rng.previous() if compare else None

    async def none() -> Optional[dict]:
        return None

    sig, sig_prev, cards, acts, health = await asyncio.gather(
        _signals(redis, rng), _signals(redis, prev, previous=True) if prev else none(), _cards(pool, redis),
        sections.run("activity", sections.activity(pool, rng)), _health(pool, now))
    run = sections.run
    rt, res, jobs, ai, cost, ev_src, dl, chg, fc, an, ack_rows, ack_trail = await asyncio.gather(
        run("runtimes", sections.runtimes(pool, rng, now)),
        run("resources", sections.resources(pool, sig, rng, now)),
        run("jobs", sections.jobs(pool, rng, authorization)),
        run("ai", sections.ai(pool, rng, now, authorization)),
        run("cost", _cost(redis, pool, now)),
        sections.run("events", _events(pool, rng)),
        run("delivery", sections.delivery(pool, now)),
        run("changes", _changes(pool, rng, now)),
        run("forecast", forecast.collect(pool, redis, now)),
        run("anomaly", anomaly.collect(pool, redis, now)),
        _guarded(acks.read_in_force(pool, now), ({}, {})),
        _guarded(acks.history(pool, rng.start), []))
    events_rows = ev_src.get("data", []) if ev_src.get("status") == "ok" else []
    apps = sections.ok(sections.app_tiles(sig, cards, acts if isinstance(acts, dict) and "status" not in acts else {},
                                          health), 300, as_of=_as_of(sig, now))
    secs = {"signals": {**series_view(sig, rng), "refresh_s": SIGNALS_TTL_S}, "apps": apps, "runtimes": rt,
            "resources": res, "jobs": jobs, "ai": ai, "cost": cost, "events": ev_src, "delivery": dl,
            "changes": chg, "forecast": fc, "anomaly": an}
    if prev is not None:
        secs["signals"]["previous"] = {**series_view(sig_prev or {}, prev), "range": prev.as_dict()}
    deploys = [e for e in events_rows if e["lane"] == "deploy"]
    bands = [{"start": e["t"], "end": e.get("t2"), "app": e["app"], "severity": e["severity"], "text": e["text"]}
             for e in events_rows if e["kind"] == "alarm"]
    secs["signals"]["deploys"] = [{"t": e["t"], "app": e["app"], "text": e["text"]} for e in deploys]
    secs["signals"]["alarm_bands"] = bands
    secs["signals"]["annotations"] = annotations(chg)
    thresholds = th.snapshot()
    factor = th.stale_factor()
    in_force, names = ack_rows
    return {
        "generated_at": now.isoformat(),
        "environment": cw_series.cloudwatch.environment(),
        "range": rng.as_dict(),
        "previous": prev.as_dict() if prev else None,
        "buckets": [b.isoformat() for b in rng.buckets()],
        "verdict": verdict.decide(secs, thresholds, events_rows, in_force, names),
        "thresholds": thresholds,
        "sections": secs,
        "sources": [freshness(k, v, now, factor) for k, v in secs.items()],
        "pipelines": await pipelines.collect(pool, dl, now, factor),
        "acks": {"rules": thresholds["acks"], "recent": ack_trail},
        "viewer": {"can_ack": bool(can_ack)},
    }


def annotations(chg: dict) -> list[dict]:
    """The chart markers of the range: one per change (time, type, app, short title). Pure."""
    if not isinstance(chg, dict) or chg.get("status") != "ok":
        return []
    return [{"t": r["t"], "type": r["type"], "app": r["app"], "text": r["title"], "id": r["id"]}
            for r in (chg.get("data") or {}).get("rows") or [] if r.get("t")]


async def _guarded(coro: Any, fallback: Any) -> Any:
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — acks are an overlay: the page still answers without them
        logger.warning("ops_status_acks_failed", error=repr(e)[:200])
        return fallback


async def _changes(pool: Any, rng: TimeRange, now: datetime) -> dict:
    return sections.ok(await changes.collect(pool, rng), 300, as_of=now)


def _as_of(sig: dict, now: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(sig.get("as_of")).replace("Z", "+00:00"))
    except ValueError:
        return now


async def _events(pool: Any, rng: TimeRange) -> dict:
    ev, bells, failures = await sections.event_sources(pool, rng)
    return sections.ok(sections.event_rows(ev, bells, failures), 300)


__all__ = ["RangeError", "build_status", "range_of"]
