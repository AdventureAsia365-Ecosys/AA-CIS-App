"""AA-1140 — the sections of the Ecosystem status page. Each builder returns ``{status, as_of, refresh_s, data}`` (or
the ``unavailable`` shape of :func:`api.services.ops_overview.cache.unavailable`) and never raises past
:func:`api.services.ops_status.build_status`, which runs them side by side.

Sources (no new cross-app grant, no new paid service):
- apps: the golden-signal series (``cw_series``), the app cards of /admin/platform/apps (``platform_apps``: deployed
  commit, readiness, one health definition) and a day-scale activity line (CIS: its own tables; TripPlanner and
  Booking: their ``ops.overview.v1`` contract views; Partner CRM: not measured yet).
- runtimes: the ``ops_sample`` rows of the 5-minute sampler (ECS services, health checks), uptime against
  ``cis.ops.status_uptime_slo_pct``.
- resources: RDS CPU / connections / free storage from ``cw_series``; the busiest role and XID age from the samples.
- jobs: ``shared.job`` live backlog and the failures of the range, the deploy gate, ``crm.job`` through crm.ops.v1
  ``/jobs/summary`` with the staff member's own token; ``booking.job`` has no owner view or endpoint CIS may read yet
  (not measured yet).
- ai: today's spend per app against its daily cap (``shared.llm_app_profile``), spend per hour by connection from
  ``shared.ai_usage_hourly``; the partner CRM's own gateway through crm.ops.v1 ``/llm/usage``.
- cost: the month-end forecast of the Spend page (``cost_ops.cost_forecast``, DB only).
- events: ``ops_event`` rows, the ops check bells (``shared.notifications``) and job failures of the range.
- delivery: the published delivery summary (``delivery_metrics``) and the nightly probe KPIs (``shared.kpi_daily``).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
import structlog

from api.services.ops_status import store
from api.services.ops_status.ranges import TimeRange
from shared import ops_status_thresholds as th

logger = structlog.get_logger()

APPS = ("cis", "tripplanner", "booking", "crm")
APP_LABELS = {"cis": "CIS", "tripplanner": "TripPlanner", "booking": "Booking", "crm": "Partner CRM",
              "shared": "Shared platform"}
NOT_MEASURED = "not_measured"
_CONTRACT_ERRORS = (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.InvalidSchemaNameError,
                    asyncpg.exceptions.InsufficientPrivilegeError, asyncpg.exceptions.UndefinedColumnError)


def ok(data: Any, refresh_s: int, as_of: Optional[datetime] = None, **extra: Any) -> dict:
    return {"status": "ok", "as_of": (as_of or datetime.now(timezone.utc)).isoformat(), "refresh_s": refresh_s,
            "data": data, **extra}


def not_measured(reason: str) -> dict:
    """A signal with no source wired yet: the page shows a grey "not measured yet", never green."""
    return {"status": NOT_MEASURED, "reason": reason}


def level(value: Optional[float], warn: Optional[float], crit: Optional[float], *, below: bool = False) -> str:
    """ok / warn / crit of one value against its limits; none without a value. Pure."""
    if value is None:
        return "none"
    if below:
        if crit is not None and value <= crit:
            return "crit"
        if warn is not None and value <= warn:
            return "warn"
        return "ok"
    if crit is not None and value >= crit:
        return "crit"
    if warn is not None and value >= warn:
        return "warn"
    return "ok"


def share(part: Optional[int], whole: Optional[int]) -> Optional[float]:
    return round(100.0 * part / whole, 2) if whole and part is not None else None


# ── apps ───────────────────────────────────────────────────────────────────────────────────────────────────────

def app_signal_levels(app: str, totals: dict) -> dict:
    """The colour of each golden signal of one app over the range (``few`` below the minimum requests). Pure."""
    req = totals.get("requests") or 0
    few = req < th.min_requests()
    err_pct = share(totals.get("errors"), req)
    cerr_pct = share(totals.get("client_errors"), req) if totals.get("client_errors") is not None else None
    er, p = th.error_rate(), th.p95_ms()
    if app == "tripplanner":
        lim = th.lambda_p95_limit_ms()
        p95_level = level(totals.get("p95_ms"), lim, None)
    else:
        p95_level = level(totals.get("p95_ms"), p["warn"], p["crit"])
    return {
        "error_rate_pct": err_pct, "client_error_rate_pct": cerr_pct, "few_requests": few,
        "errors_level": "few" if few and err_pct is not None else level(err_pct, er["warn"], er["crit"]),
        "client_errors_level": ("few" if few and cerr_pct is not None
                                else level(cerr_pct, th.client_error_warn_pct(), None)),
        "p95_level": p95_level,
    }


CIS_ACTIVITY_SQL = """
    SELECT (SELECT count(*) FROM shared.job WHERE status = 'succeeded' AND finished_at >= $1 AND finished_at < $2)::int
               AS jobs_done,
           (SELECT count(*) FROM gold_aa_internal.published_tours
             WHERE updated_at >= $1 AND updated_at < $2 AND master_status <> 'trashed')::int AS tours_published
"""
TP_ACTIVITY_SQL = """
    SELECT metric, sum(n)::bigint AS n FROM tripplanner.v_ops_traffic_daily
     WHERE day >= $1 AND day <= $2 GROUP BY metric
"""
BK_ACTIVITY_SQL = """
    SELECT metric, sum(n)::bigint AS n FROM booking.v_ops_funnel_daily WHERE day >= $1 AND day <= $2 GROUP BY metric
"""


async def activity(pool: Any, rng: TimeRange) -> dict:
    """{app: {status, items: [{metric, n}], unit}}: the business activity of the range (days for the contract views,
    which are daily). Never raises."""
    out: dict[str, dict] = {"crm": not_measured("The partner CRM publishes no activity count CIS may read yet.")}
    try:
        r = await pool.fetchrow(CIS_ACTIVITY_SQL, rng.start, rng.end)
        out["cis"] = {"status": "ok", "unit": "range",
                      "items": [{"metric": "jobs_done", "n": r["jobs_done"]},
                                {"metric": "tours_published", "n": r["tours_published"]}]}
    except Exception as e:  # noqa: BLE001
        logger.warning("ops_status_cis_activity_failed", error=repr(e)[:200])
        out["cis"] = {"status": "unavailable", "reason": "error"}
    days = (rng.start.date(), (rng.end - timedelta(seconds=1)).date())
    for app, sql in (("tripplanner", TP_ACTIVITY_SQL), ("booking", BK_ACTIVITY_SQL)):
        try:
            rows = await pool.fetch(sql, *days)
            out[app] = {"status": "ok", "unit": "days", "from": days[0].isoformat(), "to": days[1].isoformat(),
                        "items": [{"metric": r["metric"], "n": int(r["n"])} for r in rows]}
        except _CONTRACT_ERRORS:
            out[app] = {"status": "unavailable", "reason": "contract_view_missing"}
        except Exception as e:  # noqa: BLE001
            logger.warning("ops_status_activity_failed", app=app, error=repr(e)[:200])
            out[app] = {"status": "unavailable", "reason": "error"}
    return out


def ready_status(app: str, card: dict) -> Optional[str]:
    """The readiness of one app card: CIS its schema check, Booking and the CRM their /health/ready. Pure."""
    part = card.get("schema") if app == "cis" else card.get("ready")
    return part.get("status") if isinstance(part, dict) else None


def app_tiles(series: dict, cards: dict[str, dict], acts: dict, health: dict[str, dict]) -> list[dict]:
    """One tile per app from the series section, the platform app cards, the activity and the latest health
    samples. Pure."""
    tiles = []
    cw_apps = (series.get("apps") or {}) if series.get("status") == "ok" else {}
    for app in APPS:
        s = cw_apps.get(app)
        card = cards.get(app) or {}
        totals = (s or {}).get("totals") if s and s.get("measured") else None
        traffic = {"status": "ok", **totals, **app_signal_levels(app, totals)} if totals else (
            not_measured("Its API sits behind the shared load balancer; per-app request counts are not read yet.")
            if app == "crm" else {"status": "unavailable", "reason": series.get("reason") or "no_data"})
        api = card.get("api") or {}
        tiles.append({
            "app": app, "label": APP_LABELS[app],
            "overall": card.get("overall") or {"status": "unknown", "reason": "App card not available"},
            "traffic": traffic,
            "deployed_sha": api.get("git_sha") if app == "cis" else card.get("git_sha"),
            "ready": ready_status(app, card),
            "workers": len(card.get("workers") or []) if app == "cis" else None,
            "health": health.get(app),
            "activity": acts.get(app) or not_measured("No activity source."),
        })
    return tiles


# ── runtimes (services and data) ───────────────────────────────────────────────────────────────────────────────

def runtime_rows(samples: list[dict], latest: list[dict], rng: TimeRange, slo: float, now: datetime,
                 fresh_after: datetime) -> list[dict]:
    """The services grid: one row per ECS service and health check, with its latest state and the uptime strip.
    A runtime whose newest sample is older than ``fresh_after`` reads ``no_sample``. Pure."""
    by_subject: dict[tuple[str, str], list[dict]] = {}
    for s in samples:
        by_subject.setdefault((s["kind"], s["subject"]), []).append(s)
    rows = []
    for last in latest:
        key = (last["kind"], last["subject"])
        v = last["value"]
        stale = last["sampled_at"] < fresh_after
        rows.append({
            "kind": last["kind"], "subject": last["subject"], "app": last["app"] or v.get("app"),
            "role": v.get("role") or ("health check" if last["kind"] == "app_health" else last["kind"]),
            "state": "no_sample" if stale else last["state"], "reason": v.get("reason"),
            "running": v.get("running"), "desired": v.get("desired"), "pending": v.get("pending"),
            "rollout": v.get("rollout"), "task_definition": v.get("task_definition"),
            "version": v.get("image_tag") or v.get("git_sha"), "sampled_at": last["sampled_at"].isoformat(),
            **store.strip(by_subject.get(key, []), rng, slo),
        })
    order = {"ecs_service": 0, "app_health": 1}
    return sorted(rows, key=lambda r: (APPS.index(r["app"]) if r["app"] in APPS else 9, order.get(r["kind"], 2),
                                       r["subject"]))


async def runtimes(pool: Any, rng: TimeRange, now: datetime) -> dict:
    slo = th.uptime_slo_pct()
    fresh_after = now - timedelta(minutes=store.SAMPLE_MINUTES * th.stale_factor())
    kinds = ["ecs_service", "app_health"]
    rows_now = await store.latest(pool, kinds, now - timedelta(days=th.history_days()))
    in_range = await store.samples(pool, kinds, rng)
    rows = runtime_rows(in_range, rows_now, rng, slo, now, fresh_after)
    extra = [
        {"kind": "lambda", "subject": "TripPlanner browse and assembly functions", "app": "tripplanner",
         "role": "functions", "state": NOT_MEASURED,
         "reason": "Lambda versions and an uptime sample need a read the CIS role does not have yet; traffic and "
                   "errors are in the charts."},
        {"kind": "web", "subject": "TripPlanner web (Vercel)", "app": "tripplanner", "role": "web",
         "state": NOT_MEASURED, "reason": "No Vercel token on the CIS server."},
    ]
    newest = max((r["sampled_at"] for r in rows_now), default=None)
    data = {"slo_pct": slo, "cell_minutes": store.cell_minutes(rng), "rows": rows + extra,
            "sampler_seen_at": newest.isoformat() if newest else None}
    if not rows_now:
        return {"status": "unavailable", "reason": "no_samples", "refresh_s": 300, "data": data,
                "message": "The 5-minute sampler has not written a sample yet."}
    return ok(data, 300, newest)


# ── shared resources ───────────────────────────────────────────────────────────────────────────────────────────

MAX_CONNECTIONS_SQL = "SELECT current_setting('max_connections')::int AS n"


async def resources(pool: Any, series: dict, rng: TimeRange, now: datetime) -> dict:
    db_rows = await store.samples(pool, ["database", "queue"], rng)
    latest = {(r["kind"], r["subject"]): r for r in await store.latest(pool, ["database", "ecs_service"],
                                                                         now - timedelta(minutes=30))}
    db_samples = [r for r in db_rows if r["kind"] == "database"]
    try:
        max_conn = int(await pool.fetchval(MAX_CONNECTIONS_SQL))
    except Exception:  # noqa: BLE001
        max_conn = None
    tiles = []
    rds = series.get("rds") if series.get("status") == "ok" else None

    def tile(tid: str, label: str, unit: str, values: list, lim: dict, agg: str, note: Optional[str] = None,
             source: str = "cloudwatch") -> dict:
        vals = [v for v in values if v is not None]
        latest_v = next((v for v in reversed(values) if v is not None), None)
        worst = (min(vals) if lim.get("dir") == "below" else max(vals)) if vals else None
        return {"id": tid, "label": label, "unit": unit, "series": values, "latest": latest_v, "worst": worst,
                "agg": agg, "warn": lim.get("warn"), "crit": lim.get("crit"), "dir": lim.get("dir", "above"),
                "warn_key": lim.get("warn_key"), "crit_key": lim.get("crit_key"),
                "level": level(latest_v, lim.get("warn"), lim.get("crit"), below=lim.get("dir") == "below"),
                "note": note, "source": source}

    if rds and rds.get("measured"):
        tiles.append(tile("cpu", "Database CPU", "%", rds["cpu_pct"], th.rds_cpu_pct(), "mean"))
        conn_pct = [None if v is None or not max_conn else round(100.0 * v / max_conn, 1) for v in rds["connections"]]
        tiles.append(tile("connections", "Database connections", "% of max", conn_pct, th.rds_connections_pct(), "max",
                          note=f"max_connections {max_conn}" if max_conn else None))
        tiles.append(tile("free_storage", "Free storage", "GiB", rds["free_gib"], th.rds_free_storage_gib(), "min"))
    else:
        for tid, label in (("cpu", "Database CPU"), ("connections", "Database connections"),
                           ("free_storage", "Free storage")):
            tiles.append({"id": tid, "label": label, "level": "none", "status": "unavailable",
                          "reason": series.get("reason") or "no_data", "series": []})
    role_series = store.step_series(db_samples, rng, "busiest_role_pct")
    xid_series = store.step_series(db_samples, rng, "xid_age", scale=1e-6)
    db_latest = latest.get(("database", "rds"))
    busiest = (db_latest or {}).get("value", {}).get("busiest_role")
    tiles.append(tile("role", "Busiest DB role", "% of its limit", role_series, th.role_connections_pct(), "max",
                      note=busiest, source="sampler"))
    x = th.xid_age()
    tiles.append(tile("xid", "Transaction ID age", "M", xid_series,
                      {**x, "warn": x["warn"] / 1e6, "crit": x["crit"] / 1e6}, "max", source="sampler"))
    ecs_latest = [r for k, r in latest.items() if k[0] == "ecs_service"]
    running = sum(int(r["value"].get("running") or 0) for r in ecs_latest)
    desired = sum(int(r["value"].get("desired") or 0) for r in ecs_latest)
    tiles.append({"id": "ecs", "label": "ECS tasks running", "unit": "tasks", "latest": running, "capacity": desired,
                  "level": "none" if not ecs_latest else ("warn" if running < desired else "ok"), "series": [],
                  "source": "sampler", "note": f"{running} of {desired} desired" if ecs_latest else None})
    worst = max((t.get("level", "none") for t in tiles), key=lambda lv: {"none": -1, "ok": 0, "warn": 1,
                                                                            "crit": 2}.get(lv, -1))
    return ok({"tiles": tiles, "worst": worst}, 300)


# ── jobs ───────────────────────────────────────────────────────────────────────────────────────────────────────

JOBS_NOW_SQL = """
    SELECT count(*) FILTER (WHERE status = 'queued' AND run_after <= now())::int AS queued_due,
           count(*) FILTER (WHERE status = 'queued' AND run_after > now())::int AS scheduled_later,
           count(*) FILTER (WHERE status = 'running')::int AS running
      FROM shared.job WHERE status IN ('queued', 'running')
"""
JOBS_RANGE_SQL = """
    SELECT count(*) FILTER (WHERE status = 'succeeded')::int AS succeeded,
           count(*) FILTER (WHERE status = 'failed')::int AS failed
      FROM shared.job WHERE finished_at >= $1 AND finished_at < $2
"""
FAILING_KINDS_SQL = """
    SELECT kind, count(*)::int AS n, max(finished_at) AS last_at,
           (array_agg(left(coalesce(error, ''), 160) ORDER BY finished_at DESC))[1] AS last_error
      FROM shared.job
     WHERE status = 'failed' AND finished_at >= $1 AND finished_at < $2
     GROUP BY kind ORDER BY n DESC, kind LIMIT 8
"""
FAILURES_BY_HOUR_SQL = """
    SELECT kind, date_trunc('hour', finished_at) AS hour, count(*)::int AS n,
           (array_agg(left(coalesce(error, ''), 160) ORDER BY finished_at DESC))[1] AS last_error
      FROM shared.job
     WHERE status = 'failed' AND finished_at >= $1 AND finished_at < $2
     GROUP BY kind, 2 ORDER BY 2 DESC LIMIT 200
"""


def queue_level(wait_min: Optional[float], workers: Optional[int]) -> str:
    q = th.queue_wait_min()
    if workers == 0:
        return "crit"
    return level(wait_min, q["warn"], q["crit"])


async def jobs(pool: Any, rng: TimeRange, authorization: Optional[str]) -> dict:
    from api.services import platform_apps
    from shared.jobs import backlog
    from shared.jobs.registry import kinds
    from shared.jobs.worker import deploy_gate_status, load_kinds

    load_kinds()
    now_row = await pool.fetchrow(JOBS_NOW_SQL)
    range_row = await pool.fetchrow(JOBS_RANGE_SQL, rng.start, rng.end)
    failing = await pool.fetch(FAILING_KINDS_SQL, rng.start, rng.end)
    b = await backlog.sample(pool, kinds())
    wait_min = round(float(b.get("oldest_queued_age_seconds") or 0.0) / 60.0, 1)
    q_samples = await store.samples(pool, ["queue"], rng)
    shared_q = {
        "id": "shared", "name": "shared.job", "app": "cis", "status": "ok",
        "queued": now_row["queued_due"], "scheduled_later": now_row["scheduled_later"], "running": now_row["running"],
        "oldest_wait_min": wait_min, "workers": b.get("live_workers"),
        "succeeded": range_row["succeeded"], "failed": range_row["failed"],
        "level": queue_level(wait_min, b.get("live_workers")),
        "series_wait_min": store.step_series(q_samples, rng, "oldest_wait_s", scale=1 / 60.0),
    }
    crm = await platform_apps.crm_jobs(authorization)
    if crm.get("status") == "ok":
        ks = crm.get("kinds") or []
        waits = [k.get("oldest_queued_age_s") for k in ks if isinstance(k.get("oldest_queued_age_s"), (int, float))]
        cw = round(max(waits) / 60.0, 1) if waits else 0.0
        crm_q = {"id": "crm", "name": "crm.job", "app": "crm", "status": "ok", "as_of": crm.get("generated_at"),
                 "queued": sum(int(k.get("queued") or 0) for k in ks),
                 "running": sum(int(k.get("running") or 0) for k in ks),
                 "failed": sum(int(k.get("failed_24h") or 0) for k in ks), "failed_window": "24h",
                 "oldest_wait_min": cw, "workers": None, "level": queue_level(cw, None),
                 "series_wait_min": None,
                 "failing_kinds": [{"kind": k.get("kind"), "n": int(k.get("failed_24h") or 0)} for k in ks
                                   if int(k.get("failed_24h") or 0) > 0]}
    else:
        crm_q = {"id": "crm", "name": "crm.job", "app": "crm", "status": "unavailable",
                 "reason": crm.get("reason") or crm.get("status")}
    booking_q = {"id": "booking", "name": "booking.job", "app": "booking", **not_measured(
        "Booking exposes its job counts only to its own deploy workflow; an owner view or ops endpoint for CIS is a "
        "follow-up.")}
    try:
        gate = await deploy_gate_status(pool)
        gate_view = {"safe_to_deploy": gate.get("safe_to_deploy"), "blocked_by": gate.get("blocked_by") or [],
                     "active": gate.get("active"), "open_waves": gate.get("open_waves")}
    except Exception as e:  # noqa: BLE001
        logger.warning("ops_status_deploy_gate_failed", error=repr(e)[:200])
        gate_view = {"safe_to_deploy": None, "blocked_by": [], "error": type(e).__name__}
    return ok({"queues": [shared_q, booking_q, crm_q], "deploy_gate": gate_view,
               "failing_kinds": [{"kind": r["kind"], "n": r["n"], "last_at": r["last_at"].isoformat(),
                                  "last_error": r["last_error"] or None, "queue": "shared.job"} for r in failing]},
              60)


# ── AI gateway ─────────────────────────────────────────────────────────────────────────────────────────────────

APP_SPEND_TODAY_SQL = """
    SELECT coalesce(l.app, l.quality_signal ->> 'app', 'cis') AS app, coalesce(sum(l.cost_usd), 0)::float8 AS spent
      FROM shared.llm_call_log l
     WHERE l.created_at >= date_trunc('day', $1::timestamptz, 'UTC')
       AND l.created_at < date_trunc('day', $1::timestamptz, 'UTC') + interval '1 day'
     GROUP BY 1
"""
APP_CAPS_SQL = "SELECT app, default_daily_budget_usd::float8 AS cap_usd FROM shared.llm_app_profile"
HOURLY_SQL = """
    SELECT hour, provider, account, app, sum(cost_usd)::float8 AS cost, sum(requests)::bigint AS requests,
           sum(requests) FILTER (WHERE status <> 'ok')::bigint AS failed
      FROM shared.ai_usage_hourly
     WHERE hour >= $1 AND hour < $2
     GROUP BY hour, provider, account, app
"""


def connection_key(provider: Optional[str], account: Optional[str]) -> str:
    from api.services.platform_cost import provider_family

    fam = provider_family(provider)
    return f"{fam}:{account}" if fam == "bedrock" and account else fam


def spend_by_connection(rows: list[dict], rng: TimeRange) -> dict:
    """{connections: [{key, cost, requests, failed, series}], apps: {app: cost}} from ai_usage_hourly rows laid on the
    range's steps (an hour row goes to the step that holds its start). Pure."""
    n = len(rng.buckets())
    conns: dict[str, dict] = {}
    apps: dict[str, float] = {}
    for r in rows:
        key = connection_key(r["provider"], r["account"])
        c = conns.setdefault(key, {"key": key, "cost": 0.0, "requests": 0, "failed": 0, "series": [0.0] * n})
        cost = float(r["cost"] or 0.0)
        c["cost"] += cost
        c["requests"] += int(r["requests"] or 0)
        c["failed"] += int(r["failed"] or 0)
        i = int((r["hour"] - rng.start).total_seconds() // rng.step_s)
        if 0 <= i < n:
            c["series"][i] += cost
        apps[r["app"]] = apps.get(r["app"], 0.0) + cost
    out = sorted(conns.values(), key=lambda c: -c["cost"])
    for c in out:
        c["cost"] = round(c["cost"], 4)
        c["series"] = [round(v, 5) for v in c["series"]]
        c["error_pct"] = share(c["failed"], c["requests"])
    return {"connections": out, "apps": {k: round(v, 4) for k, v in apps.items()}}


def app_caps(spent: dict[str, float], caps: dict[str, Optional[float]], near_pct: float) -> list[dict]:
    """Today's spend per app against its daily cap (bullet bars). Pure."""
    out = []
    for app in APPS:
        cap, s = caps.get(app), spent.get(app)
        pct = round(100.0 * s / cap, 1) if cap and s is not None else None
        lvl = ("none" if s is None else "crit" if cap is not None and s >= cap else
               "warn" if pct is not None and pct >= near_pct else "ok")
        out.append({"app": app, "spent_usd": None if s is None else round(s, 4), "cap_usd": cap, "used_pct": pct,
                    "level": lvl})
    return out


async def ai(pool: Any, rng: TimeRange, now: datetime, authorization: Optional[str]) -> dict:
    from api.services import platform_apps

    spent = {r["app"]: float(r["spent"]) for r in await pool.fetch(APP_SPEND_TODAY_SQL, now)}
    for app in ("cis", "tripplanner", "booking"):
        spent.setdefault(app, 0.0)
    caps = {r["app"]: (float(r["cap_usd"]) if r["cap_usd"] is not None else None)
            for r in await pool.fetch(APP_CAPS_SQL)}
    today = now.astimezone(timezone.utc).date()
    crm = await platform_apps.crm_llm_usage(authorization, today, today + timedelta(days=1))
    crm_note = None
    if crm.get("status") == "ok":
        spent["crm"] = float(crm.get("cost_usd") or 0.0)
    else:
        crm_note = crm.get("reason") or crm.get("status")
    try:
        hourly = [dict(r) for r in await pool.fetch(HOURLY_SQL, rng.start, rng.end)]
    except asyncpg.exceptions.UndefinedTableError:
        hourly = []
    near = th.snapshot()["app_spend_near_pct"]["warn"]
    by_conn = spend_by_connection(hourly, rng)
    # "apps" is today's spend against the caps (the verdict reads it); the range's spend per app is "range_by_app"
    return ok({"day": today.isoformat(), "apps": app_caps(spent, caps, near), "near_pct": near,
               "crm_note": crm_note, "connections": by_conn["connections"], "range_by_app": by_conn["apps"]}, 300)


# ── cost ───────────────────────────────────────────────────────────────────────────────────────────────────────

def cost_card(forecast: dict) -> dict:
    """Month to date, forecast and budget of the month (AI providers + AWS accounts), the one line the business owner
    reads. Pure."""
    prov = forecast.get("providers_total") or {}
    aws = forecast.get("aws_accounts") or []
    ai_mtd, ai_fc = float(prov.get("mtd_usd") or 0.0), float(prov.get("forecast_usd") or 0.0)
    aws_mtd = sum(float(a.get("mtd_usd") or 0.0) for a in aws)
    aws_fc = sum(float(a.get("forecast_usd") or 0.0) for a in aws)
    budgets = [float(p["budget_usd"]) for p in (forecast.get("providers") or []) if p.get("budget_usd") is not None]
    budgets += [float(a["budget_usd"]) for a in aws if a.get("budget_usd") is not None]
    budget = round(sum(budgets), 2) if budgets else None
    total_fc = round(ai_fc + aws_fc, 2)
    return {"month": forecast.get("month"), "spent_usd": round(ai_mtd + aws_mtd, 2), "ai_usd": round(ai_mtd, 2),
            "aws_usd": round(aws_mtd, 2), "forecast_usd": total_fc, "budget_usd": budget,
            "forecast_pct_of_budget": round(100.0 * total_fc / budget, 1) if budget else None,
            "headroom_usd": round(budget - total_fc, 2) if budget is not None else None,
            "complete_days": forecast.get("complete_days"), "days_in_month": forecast.get("days_in_month")}


async def cost(pool: Any, now: datetime) -> dict:
    from api.services import cost_ops

    f = await cost_ops.cost_forecast(pool, now=now)
    return ok(cost_card(f), 900)


# ── events ─────────────────────────────────────────────────────────────────────────────────────────────────────

BELLS_SQL = """
    SELECT id, event_type, entity_id, payload, is_read, created_at
      FROM shared.notifications
     WHERE event_type LIKE 'platform.ops_check.%' AND created_at >= $1 AND created_at < $2
     ORDER BY created_at DESC LIMIT 200
"""


def _payload(v: Any) -> dict:
    return store._json(v)


def event_rows(ops_events: list[dict], bells: list[dict], failures: list[dict]) -> list[dict]:
    """The timeline rows: lane deploy | alarm | check | job | service. Pure."""
    out = []
    for e in ops_events:
        lane = {"deploy": "deploy", "rollout_failed": "deploy", "alarm": "alarm", "service_down": "service"}.get(
            e["kind"], "service")
        out.append({"id": f"e{e['event_id']}", "t": e["occurred_at"].isoformat(),
                    "t2": e["ended_at"].isoformat() if e.get("ended_at") else None, "lane": lane, "kind": e["kind"],
                    "severity": e["severity"], "app": e.get("app") or "shared", "text": e["title"],
                    "detail": e.get("detail"), "open": e.get("ended_at") is None and e["kind"] != "deploy"})
    for b in bells:
        p = _payload(b["payload"])
        out.append({"id": f"n{b['id']}", "t": b["created_at"].isoformat(), "t2": None, "lane": "check",
                    "kind": "check", "severity": "sev3", "app": _check_app(str(b.get("entity_id") or "")),
                    "text": f"Ops check: {p.get('title') or b['entity_id']}", "detail": (p.get("message") or "")[:300],
                    "open": not b["is_read"], "check": b.get("entity_id")})
    for f in failures:
        out.append({"id": f"j{f['kind']}:{f['hour'].isoformat()}", "t": f["hour"].isoformat(), "t2": None,
                    "lane": "job", "kind": "job_failures", "severity": "sev3", "app": "cis",
                    "text": f"{f['n']} job{'s' if f['n'] != 1 else ''} failed: {f['kind']}",
                    "detail": f.get("last_error") or None, "open": False})
    return sorted(out, key=lambda r: r["t"], reverse=True)


def _check_app(check: str) -> str:
    for app in APPS:
        if f".{app}." in f".{check}." or check.startswith(f"{app}_"):
            return app
    return "cis"


async def event_sources(pool: Any, rng: TimeRange) -> tuple[list[dict], list[dict], list[dict]]:
    ev = await store.events(pool, rng)
    bells = [dict(r) for r in await pool.fetch(BELLS_SQL, rng.start, rng.end)]
    failures = [dict(r) for r in await pool.fetch(FAILURES_BY_HOUR_SQL, rng.start, rng.end)]
    return ev, bells, failures


# ── delivery and nightly guards ────────────────────────────────────────────────────────────────────────────────

PROBE_KPIS_SQL = """
    SELECT DISTINCT ON (kpi_id) kpi_id, day, value::float8 AS value, computed_at, data_as_of
      FROM shared.kpi_daily
     WHERE kpi_id = ANY($1::text[]) AND scope = 'all'
     ORDER BY kpi_id, day DESC
"""


def guard_rows(kpis: list[dict], now: datetime, stale_hours: float) -> list[dict]:
    """The nightly guards: the two probes CIS reads (passed / failed / did not run), the rest not measured. Pure."""
    from services.reports.probes import NOT_VISIBLE, PROBES

    by_id = {k["kpi_id"]: k for k in kpis}
    out = []
    for p in PROBES:
        k = by_id.get(p.kpi_id)
        if k is None:
            out.append({"key": p.key, "label": p.label, "state": "missing", "detail": "No result recorded yet"})
            continue
        seen = k.get("data_as_of") or k.get("computed_at")
        age_h = (now - seen).total_seconds() / 3600.0 if isinstance(seen, datetime) else None
        if age_h is not None and age_h > stale_hours:
            state = "missing"
        else:
            state = "passed" if float(k["value"]) == 0 else "failed"
        out.append({"key": p.key, "label": p.label, "state": state, "value": k["value"], "day": k["day"].isoformat(),
                    "seen_at": seen.isoformat() if isinstance(seen, datetime) else None,
                    "age_h": None if age_h is None else round(age_h, 1)})
    for name, _cron, why in NOT_VISIBLE:
        out.append({"key": name.lower().replace(" ", "_"), "label": name, "state": NOT_MEASURED, "detail": why})
    return out


async def delivery(pool: Any, now: datetime) -> dict:
    from api.services import delivery_metrics
    from services.reports.probes import PROBES

    repos: list[dict] = []
    summary_note = None
    try:
        summary = await delivery_metrics.load()
        th_d = await delivery_metrics.thresholds()
        for a in summary.get("apps") or []:
            if not isinstance(a, dict) or not a.get("id"):
                continue
            main = (a.get("ci") or {}).get("main") if isinstance(a.get("ci"), dict) else None
            repos.append({"id": a.get("id"), "label": a.get("label") or a.get("id"),
                          "main": delivery_metrics.main_state(a, th_d["main_red_hours"]),
                          "red_hours": (main or {}).get("red_hours") if isinstance(main, dict) else None})
        as_of = summary.get("generated_at")
    except Exception as e:  # noqa: BLE001 — 404 before the first publish, 503 storage
        summary_note = type(e).__name__
        as_of = None
    kpis = [dict(r) for r in await pool.fetch(PROBE_KPIS_SQL, [p.kpi_id for p in PROBES])]
    guards = guard_rows(kpis, now, th.guard_stale_hours())
    return ok({"repos": repos, "repos_as_of": as_of, "repos_note": summary_note, "guards": guards}, 3600)


async def run(name: str, coro) -> dict:
    """One section, never raising: a failure becomes ``unavailable: error``."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("ops_status_section_failed", section=name, error=repr(e)[:300])
        return {"status": "unavailable", "reason": "error", "message": "This section could not be loaded."}


def today_utc(now: datetime) -> date:
    return now.astimezone(timezone.utc).date()
