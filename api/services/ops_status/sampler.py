"""AA-1140 — the 5-minute status sampler: what CloudWatch does not keep for us, written to ``acp_shared.ops_sample``
and ``acp_shared.ops_event`` (CIS 475) by the job kind ``ops_status_sample`` (services/jobs/ops_status_sample_job.py).

One tick, each part on its own (a failing part is named in the result, the others still write):

- ``ecs``: one ECS DescribeServices of the shared cluster (the seven services of the four apps; free, existing grant)
  and, per task definition not seen before by this process, one DescribeTaskDefinition (revisions are immutable, so
  the image tag is cached). One ``ecs_service`` sample per service (running / desired / pending, rollout, revision,
  image tag) and the events: every deployment the service lists becomes a ``deploy`` event (dedupe
  ``deploy:<deployment id>``, at its creation time; ``ended_at`` when its rollout completed), a failed rollout a
  ``rollout_failed`` event (SEV2) until a later rollout of the service completed or ECS no longer lists the failed
  deployment, a service below its desired count a ``service_down`` event until it is back.
- ``alarms``: CloudWatch DescribeAlarms (free) grouped by App tag as the overview does; one ``alarms`` sample (names,
  apps, severities) and an ``alarm`` event per firing period (dedupe ``alarm:<name>:<state change time>``), closed
  (``ended_at``) on the first tick that no longer sees it firing.
- ``health``: the public health + readiness checks of CIS, Booking and the partner CRM (``platform_apps``, 2 s).
- ``queue``: the capacity-aware backlog of ``shared.job`` (``shared/jobs/backlog.py``, the jobs_starved rule).
- ``database``: the busiest login role against its CONNECTION LIMIT, ``max(age(datfrozenxid))``, all sessions
  against ``max_connections`` and the instance's allocated storage (RDS DescribeDBInstances, free, the existing
  ``rds:DescribeDBInstances`` grant): the free storage tile judges free space as a share of it (CIS 477).

Sample states: ok, warn, degraded, down, unknown (did not answer). For the uptime strip ok and warn count as up,
degraded and down as down, unknown as not measured. Never raises; $0 (free control-plane reads and DB queries).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from api.services.issue_refs import clean, clip_words

logger = structlog.get_logger()

MAX_VALUE_BYTES = 8000  # technical-constant: the ops_sample value CHECK is 8 KiB
# ECS services of the shared cluster: (app, role, name suffix). Names: aa-<app>-<env>-<suffix>.
SERVICES: tuple[tuple[str, str, str], ...] = (
    ("cis", "api", "aa-cis-{env}-api"), ("cis", "worker", "aa-cis-{env}-worker"),
    ("cis", "admin web", "aa-cis-{env}-admin"),
    ("booking", "api", "aa-booking-{env}-api"), ("booking", "admin web", "aa-booking-{env}-admin"),
    ("crm", "api", "aa-crm-{env}-api"), ("crm", "web", "aa-crm-{env}-web"),
)
HEALTH_APPS = ("cis", "booking", "crm")
UP_STATES = ("ok", "warn")
DOWN_STATES = ("degraded", "down")
_image_cache: dict[str, list[dict]] = {}


def cluster(env: str) -> str:
    return f"aa-cis-{env}-cluster"


def service_names(env: str) -> list[tuple[str, str, str]]:
    """[(app, role, service name)]."""
    return [(app, role, pattern.format(env=env)) for app, role, pattern in SERVICES]


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    return str(v)


def _ts(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _image(task_def: dict) -> list[dict]:
    from services.reports.delivery import _image_tag

    return [{"container": c.get("name"), **_image_tag(c.get("image"))} for c in task_def.get("containerDefinitions")
            or []]


def service_state(svc: Optional[dict]) -> tuple[str, str]:
    """(state, reason) of one DescribeServices entry. Pure."""
    if svc is None:
        return "unknown", "not found in the cluster"
    desired, running = int(svc.get("desiredCount") or 0), int(svc.get("runningCount") or 0)
    primary = next((d for d in svc.get("deployments") or [] if d.get("status") == "PRIMARY"), {})
    rollout = primary.get("rolloutState")
    if desired == 0:
        return "down", "scaled to zero"
    if running == 0:
        return "down", f"0 of {desired} tasks running"
    if rollout == "FAILED":
        return "degraded", "the last rollout failed"
    if running < desired:
        return "degraded", f"{running} of {desired} tasks running"
    if rollout == "IN_PROGRESS":
        return "warn", "a rollout is in progress"
    return "ok", f"{running} of {desired} tasks running"


def describe_services(env: str, client: Any = None) -> dict:
    """Sync (run it in a thread): {name: {...service, 'images': [...]}} plus 'missing'."""
    from api.services.ops_overview import cloudwatch

    client = client or cloudwatch.aws_client("ecs")
    names = [n for _a, _r, n in service_names(env)]
    resp = client.describe_services(cluster=cluster(env), services=names)
    out: dict[str, Any] = {}
    for s in resp.get("services") or []:
        td = s.get("taskDefinition") or ""
        if td and td not in _image_cache:
            d = client.describe_task_definition(taskDefinition=td).get("taskDefinition") or {}
            _image_cache[td] = _image(d)
        out[s.get("serviceName")] = {**s, "images": _image_cache.get(td, [])}
    out["__missing__"] = [str(f.get("arn", "")).rsplit("/", 1)[-1] for f in resp.get("failures") or []]
    return out


def service_sample(app: str, role: str, svc: Optional[dict]) -> dict:
    """The ``value`` of one ecs_service sample. Pure."""
    state, reason = service_state(svc)
    if svc is None:
        return {"app": app, "role": role, "state": state, "reason": reason}
    primary = next((d for d in svc.get("deployments") or [] if d.get("status") == "PRIMARY"), {})
    images = svc.get("images") or []
    tag = next((i.get("tag") for i in images if i.get("tag")), None)
    return {"app": app, "role": role, "state": state, "reason": reason,
            "desired": svc.get("desiredCount"), "running": svc.get("runningCount"), "pending": svc.get("pendingCount"),
            "rollout": primary.get("rolloutState"), "deployments": len(svc.get("deployments") or []),
            "task_definition": str(svc.get("taskDefinition") or "").rsplit("/", 1)[-1], "image_tag": tag}


def deploy_events(app: str, role: str, name: str, svc: dict) -> list[dict]:
    """The deploy / rollout_failed events of one service's current deployments. Pure."""
    out: list[dict] = []
    tag = next((i.get("tag") for i in svc.get("images") or [] if i.get("tag")), None)
    for d in svc.get("deployments") or []:
        did = str(d.get("id") or "")
        created = _ts(d.get("createdAt"))
        if not did or created is None:
            continue
        rev = str(d.get("taskDefinition") or "").rsplit(":", 1)[-1]
        rollout = d.get("rolloutState")
        ended = _ts(d.get("updatedAt")) if rollout in ("COMPLETED", "FAILED") else None
        title = f"Deploy {name}" + (f" {tag}" if tag and d.get("status") == "PRIMARY" else "") + f" (rev {rev})"
        out.append({"kind": "deploy", "severity": "info", "app": app, "subject": name, "title": title[:300],
                    "detail": f"{role}: rollout {str(rollout or 'unknown').lower()}", "occurred_at": created,
                    "ended_at": ended if ended and ended >= created else None, "dedupe_key": f"deploy:{did}"[:300],
                    "details": {"rollout": rollout, "revision": rev, "status": d.get("status"),
                                "image_tag": tag if d.get("status") == "PRIMARY" else None}})
        if rollout == "FAILED":
            out.append({"kind": "rollout_failed", "severity": "sev2", "app": app, "subject": name,
                        "title": f"Rollout failed: {name} (rev {rev})"[:300],
                        "detail": clip_words(str(d.get("rolloutStateReason") or ""), 1000) or None,
                        "occurred_at": ended or created, "ended_at": None,
                        "dedupe_key": f"rollout_failed:{did}"[:300], "details": {"revision": rev}})
    return out


def failing_rollout_keys(svc: dict) -> set[str]:
    """The ``rollout_failed`` dedupe keys of one service that are still a problem now: a deployment the service
    still lists as FAILED, unless a PRIMARY deployment created after it completed its rollout (a rollback or a later
    deploy that went through). A key not returned is closed by the tick (``ecs_part``). Pure."""
    deps = svc.get("deployments") or []
    primary = next((d for d in deps if d.get("status") == "PRIMARY"), {})
    primary_done = primary.get("rolloutState") == "COMPLETED"
    primary_at = _ts(primary.get("createdAt"))
    out = set()
    for d in deps:
        did = str(d.get("id") or "")
        if not did or d.get("rolloutState") != "FAILED":
            continue
        created = _ts(d.get("createdAt"))
        superseded = (primary_done and d is not primary and primary_at is not None and created is not None
                      and primary_at > created)
        if not superseded:
            out.add(f"rollout_failed:{did}"[:300])
    return out


def alarm_firing(alarms: dict) -> list[dict]:
    """[{name, app, severity, since, description}] of every firing alarm in the fetch_alarms buckets. Pure."""
    out = []
    for app, bucket in (alarms or {}).items():
        for f in bucket.get("firing") or []:
            out.append({"name": f.get("name"), "app": app, "severity": f.get("severity"), "since": f.get("since"),
                        "description": clean(f.get("description"), 1000) or ""})
    return sorted(out, key=lambda f: str(f.get("name")))


def alarm_severity(tag: Optional[str]) -> str:
    """The Infra Severity tag (page / ticket) as a page severity: page = SEV2, anything else SEV3."""
    return "sev2" if (tag or "").lower() == "page" else "sev3"


def fit(value: dict) -> dict:
    """A sample value within the 8 KiB CHECK: lists are cut until it fits (``truncated`` says so). Pure."""
    text = json.dumps(value, default=str)
    if len(text.encode()) <= MAX_VALUE_BYTES:
        return value
    out = dict(value)
    for k, v in value.items():
        if isinstance(v, list):
            keep = list(v)
            while keep and len(json.dumps({**out, k: keep}, default=str).encode()) > MAX_VALUE_BYTES - 64:
                keep = keep[: max(0, len(keep) // 2)]
            out[k] = keep
            out["truncated"] = True
    return out


def queue_state(oldest_wait_s: Optional[float], live_workers: Optional[int], warn_min: float,
                crit_min: float) -> str:
    """ok / warn / degraded / down of the shared.job backlog. Pure."""
    if live_workers == 0:
        return "down"
    if oldest_wait_s is None:
        return "unknown"
    minutes = oldest_wait_s / 60.0
    if minutes >= crit_min:
        return "degraded"
    if minutes >= warn_min:
        return "warn"
    return "ok"


def pct_state(value: Optional[float], warn: float, crit: float) -> str:
    if value is None:
        return "unknown"
    return "degraded" if value >= crit else "warn" if value >= warn else "ok"


# ── SQL ────────────────────────────────────────────────────────────────────────────────────────────────────────

INSERT_SAMPLE_SQL = """
    INSERT INTO acp_shared.ops_sample (sampled_at, kind, subject, app, state, value, job_id)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::uuid)
    ON CONFLICT (kind, subject, sampled_at) DO NOTHING
"""
INSERT_EVENT_SQL = """
    INSERT INTO acp_shared.ops_event (occurred_at, ended_at, kind, severity, app, subject, title, detail, details,
                                      dedupe_key, job_id)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11::uuid)
    ON CONFLICT (dedupe_key) DO UPDATE
       SET ended_at = coalesce(acp_shared.ops_event.ended_at, EXCLUDED.ended_at),
           detail = EXCLUDED.detail, details = EXCLUDED.details, updated_at = now()
     WHERE acp_shared.ops_event.ended_at IS NULL
"""
OPEN_EVENTS_SQL = """
    SELECT event_id, subject, dedupe_key FROM acp_shared.ops_event WHERE kind = $1 AND ended_at IS NULL
"""
CLOSE_EVENT_SQL = """
    UPDATE acp_shared.ops_event SET ended_at = greatest($2::timestamptz, occurred_at), updated_at = now()
     WHERE event_id = $1 AND ended_at IS NULL
"""
DB_SESSIONS_SQL = """
    SELECT (SELECT count(*) FROM pg_catalog.pg_stat_activity)::int AS sessions,
           current_setting('max_connections')::int AS max_connections
"""


async def _write_samples(pool, rows: list[tuple], job_id: Optional[str]) -> int:
    if not rows:
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(INSERT_SAMPLE_SQL, [(*r[:5], json.dumps(fit(r[5]), default=str), job_id)
                                                   for r in rows])
    return len(rows)


async def write_events(pool, events: list[dict], job_id: Optional[str]) -> int:
    if not events:
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(INSERT_EVENT_SQL, [
            (e["occurred_at"], e.get("ended_at"), e["kind"], e["severity"], e.get("app"), e["subject"][:255],
             e["title"][:300], (e.get("detail") or None), json.dumps(e.get("details") or {}, default=str)[:4000],
             e["dedupe_key"], job_id) for e in events])
    return len(events)


async def _close_missing(pool, kind: str, still_open: set[str], at: datetime,
                         keep_subjects: frozenset[str] = frozenset()) -> int:
    """Close the open events of ``kind`` whose dedupe key is not in ``still_open`` (events of a subject in
    ``keep_subjects``, one this tick could not see, stay as they are)."""
    rows = await pool.fetch(OPEN_EVENTS_SQL, kind)
    n = 0
    for r in rows:
        if r["dedupe_key"] not in still_open and r["subject"] not in keep_subjects:
            await pool.execute(CLOSE_EVENT_SQL, r["event_id"], at)
            n += 1
    return n


# ── parts ──────────────────────────────────────────────────────────────────────────────────────────────────────

async def ecs_part(pool, env: str, at: datetime, job_id: Optional[str], out: dict, client: Any = None) -> None:
    described = await asyncio.to_thread(describe_services, env, client)
    samples, events = [], []
    bad_now: dict[str, tuple[str, str]] = {}
    failing: set[str] = set()
    unseen: set[str] = set()
    for app, role, name in service_names(env):
        svc = described.get(name)
        value = service_sample(app, role, svc)
        samples.append((at, "ecs_service", name, app, value["state"], value))
        if svc is not None:
            events.extend(deploy_events(app, role, name, svc))
            failing |= failing_rollout_keys(svc)
        else:
            unseen.add(name)
        if value["state"] in DOWN_STATES:
            bad_now[name] = (app, value["reason"])
    out["ecs_samples"] = await _write_samples(pool, samples, job_id)
    # service_down: one open event per service while it stays below its desired count
    open_rows = await pool.fetch(OPEN_EVENTS_SQL, "service_down")
    open_by_subject = {r["subject"]: r["dedupe_key"] for r in open_rows}
    for name, (app, reason) in bad_now.items():
        if name not in open_by_subject:
            events.append({"kind": "service_down", "severity": "sev1" if "0 of" in reason or "zero" in reason
                           else "sev2", "app": app, "subject": name, "title": f"{name}: {reason}"[:300],
                           "detail": None, "occurred_at": at, "ended_at": None,
                           "dedupe_key": f"service_down:{name}:{at.isoformat()}", "details": {}})
            open_by_subject[name] = f"service_down:{name}:{at.isoformat()}"
    out["ecs_events"] = await write_events(pool, events, job_id)
    keep = {key for name, key in open_by_subject.items() if name in bad_now}
    out["ecs_closed"] = await _close_missing(pool, "service_down", keep, at)
    # rollout_failed: closed once a later rollout completed or ECS no longer lists the failed deployment
    out["rollout_closed"] = await _close_missing(pool, "rollout_failed", failing, at, frozenset(unseen))
    if described.get("__missing__"):
        out["ecs_missing"] = described["__missing__"]


async def alarms_part(pool, env: str, at: datetime, job_id: Optional[str], out: dict,
                      client: Any = None, tagging: Any = None) -> None:
    from api.services.ops_overview import cloudwatch

    def fetch() -> dict:
        cw = client or cloudwatch.aws_client("cloudwatch")
        tags = None
        try:
            tg = tagging if tagging is not None else (None if client is not None
                                                      else cloudwatch.aws_client("resourcegroupstaggingapi"))
            tags = cloudwatch.read_alarm_tags(tg) if tg is not None else None
        except Exception:  # noqa: BLE001 — group by name prefix, as the overview does
            tags = None
        alarms, _calls = cloudwatch.fetch_alarms(cw, env, tags)
        return alarms

    alarms = await asyncio.to_thread(fetch)
    firing = alarm_firing(alarms)
    state = "degraded" if any(alarm_severity(f["severity"]) == "sev2" for f in firing) else (
        "warn" if firing else "ok")
    out["alarm_samples"] = await _write_samples(
        pool, [(at, "alarms", "all", "shared", state, {"firing": firing, "total": sum(
            b.get("total", 0) for b in alarms.values())})], job_id)
    events, keys = [], set()
    for f in firing:
        since = _ts(f.get("since")) or at
        key = f"alarm:{f['name']}:{since.isoformat()}"[:300]
        keys.add(key)
        events.append({"kind": "alarm", "severity": alarm_severity(f.get("severity")), "app": f["app"],
                       "subject": str(f["name"])[:255], "title": f"Alarm: {f['name']}"[:300],
                       "detail": f.get("description") or None, "occurred_at": since, "ended_at": None,
                       "dedupe_key": key, "details": {"severity_tag": f.get("severity")}})
    out["alarm_events"] = await write_events(pool, events, job_id)
    out["alarm_closed"] = await _close_missing(pool, "alarm", keys, at)


async def health_part(pool, at: datetime, job_id: Optional[str], out: dict, client: Any = None) -> None:
    from api.core.app_registry import CIS_HEALTH_PATHS, cis_api_origin
    from api.services import platform_apps

    async def one(app: str) -> dict:
        if app == "cis":
            return await platform_apps.http_health_card("cis", client, base=cis_api_origin(), paths=CIS_HEALTH_PATHS)
        return await platform_apps.http_health_card(app, client)

    cards = await asyncio.gather(*(one(a) for a in HEALTH_APPS), return_exceptions=True)
    rows = []
    for app, card in zip(HEALTH_APPS, cards):
        if isinstance(card, BaseException):
            rows.append((at, "app_health", app, app, "unknown", {"reason": type(card).__name__}))
            continue
        st = card.get("status") if card.get("status") in ("ok", "degraded", "down", "unknown") else "unknown"
        rows.append((at, "app_health", app, app, st, {
            "reason": (card.get("probe") or {}).get("reason"), "git_sha": card.get("git_sha"),
            "schema_version": (card.get("ready") or {}).get("schema_version")}))
    out["health_samples"] = await _write_samples(pool, rows, job_id)


async def queue_part(pool, at: datetime, job_id: Optional[str], out: dict) -> None:
    from shared import ops_status_thresholds as th
    from shared.jobs import backlog
    from shared.jobs.registry import kinds
    from shared.jobs.worker import load_kinds

    load_kinds()
    b = await backlog.sample(pool, kinds())
    wait = float(b.get("oldest_queued_age_seconds") or 0.0)
    q = th.queue_wait_min()
    value = {"oldest_wait_s": round(wait, 1), "queued_due": b.get("queued_due"), "running": b.get("running"),
             "live_workers": b.get("live_workers"), "overdue_running": b.get("overdue_running_jobs")}
    state = queue_state(wait, b.get("live_workers"), q["warn"], q["crit"])
    out["queue_samples"] = await _write_samples(pool, [(at, "queue", "shared.job", "cis", state, value)], job_id)


def rds_storage(env: str, client: Any = None) -> dict:
    """Sync (run it in a thread): {allocated_gib, max_allocated_gib} of the shared instance from one
    DescribeDBInstances (free); None values when RDS does not answer (the tile then uses the GiB fallback)."""
    from api.services.ops_overview import cloudwatch
    from api.services.ops_status.cw_series import db_instance

    try:
        client = client or cloudwatch.aws_client("rds")
        db = client.describe_db_instances(DBInstanceIdentifier=db_instance(env))["DBInstances"][0]
    except Exception as e:  # noqa: BLE001 — the database sample is written without the size
        logger.warning("ops_status_rds_describe_failed", error=type(e).__name__)
        return {"allocated_gib": None, "max_allocated_gib": None}
    return {"allocated_gib": db.get("AllocatedStorage"), "max_allocated_gib": db.get("MaxAllocatedStorage")}


async def database_part(pool, at: datetime, job_id: Optional[str], out: dict, env: str = "dev",
                        rds_client: Any = None) -> None:
    from api.core.ops_metrics_sampler import ROLE_CONNECTIONS_SQL, role_samples
    from shared import ops_status_thresholds as th
    from shared.ops_checks import XID_AGE_SQL

    roles = role_samples(await pool.fetch(ROLE_CONNECTIONS_SQL))
    xid = await pool.fetchval(XID_AGE_SQL)
    sess = await pool.fetchrow(DB_SESSIONS_SQL)
    busiest = max(roles, key=lambda r: r["pct"], default=None)
    role_th, xid_th = th.role_connections_pct(), th.xid_age()
    states = [pct_state(busiest["pct"] if busiest else None, role_th["warn"], role_th["crit"]),
              pct_state(float(xid) if xid is not None else None, xid_th["warn"], xid_th["crit"])]
    rank = {"unknown": -1, "ok": 0, "warn": 1, "degraded": 2}
    state = max(states, key=lambda s: rank[s])
    value = {"busiest_role": busiest["role"] if busiest else None,
             "busiest_role_pct": busiest["pct"] if busiest else None,
             "busiest_role_connections": busiest["connections"] if busiest else None,
             "busiest_role_limit": busiest["limit"] if busiest else None,
             "roles": {r["role"]: r["pct"] for r in roles},
             "xid_age": int(xid) if xid is not None else None,
             "sessions": sess["sessions"] if sess else None,
             "max_connections": sess["max_connections"] if sess else None,
             **await asyncio.to_thread(rds_storage, env, rds_client)}
    out["database_samples"] = await _write_samples(pool, [(at, "database", "rds", "shared", state, value)], job_id)


async def tick(pool, at: datetime, job_id: Optional[str] = None, *, env: Optional[str] = None,
               ecs_client: Any = None, cw_client: Any = None, http_client: Any = None, rds_client: Any = None,
               part_timeout_s: float = 30.0) -> dict:
    """One sampler tick at ``at`` (the 5-minute bucket). Never raises: ``<part>_error`` names a failed part."""
    from api.services.ops_overview import cloudwatch

    env = env or cloudwatch.environment()
    out: dict[str, Any] = {"sampled_at": at.isoformat()}
    parts = {
        "ecs": lambda: ecs_part(pool, env, at, job_id, out, ecs_client),
        "alarms": lambda: alarms_part(pool, env, at, job_id, out, cw_client),
        "health": lambda: health_part(pool, at, job_id, out, http_client),
        "queue": lambda: queue_part(pool, at, job_id, out),
        "database": lambda: database_part(pool, at, job_id, out, env, rds_client),
    }
    for name, part in parts.items():
        try:
            await asyncio.wait_for(part(), timeout=part_timeout_s)
        except Exception as e:  # noqa: BLE001 — one part never sinks the tick
            denied = cloudwatch.is_denied(e)
            out[f"{name}_error"] = "not_permitted" if denied else f"{type(e).__name__}: {str(e)[:160]}"
            logger.warning("ops_status_sample_part_failed", part=name, error=out[f"{name}_error"])
    return out
