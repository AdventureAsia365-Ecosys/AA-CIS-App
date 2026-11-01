"""shared/ops_checks.py — S263 alert diet (AA-1025): in-app ops checks that replaced CloudWatch alarms.

CloudWatch now keeps only what the app cannot see (the root decision table is
AA-Ecosys docs/audits/2026-11-01-S263-alerting-cost/diet.md). Everything the CIS processes can see themselves is
checked here, at $0, and alerts through the admin bell (shared.notifications, roles ``admin``) plus one e-mail on the
alerts SNS topic (ALERTS_TOPIC_ARN, the topic the removed alarms used, so the recipients are the same: the
Terraform bootstrap subscription and the addresses managed on Platform > Alert recipients).

Two kinds of checks:

- **State checks**, evaluated on every api ops-sampler tick (api/core/ops_metrics_sampler.py, 5 minutes) from what
  the tick already read: ``jobs_starved`` (capacity-aware oldest starved queued row > 15 min, was
  ``aa-cis-dev-jobs-queue-age``), ``jobs_overdue`` (a running attempt past 2 x its kind's expected seconds, was
  ``-jobs-overdue-running``), ``rds_role_connections`` (a LOGIN role above 80 % of its CONNECTION LIMIT, was the six
  ``-rds-role-connections-*``), ``rds_xid_age`` (``max(age(datfrozenxid))`` above 1,000,000,000, was
  ``-rds-max-used-xid-high``). See :func:`state_findings`.
- **Log events** (were log metric filters on /ecs/aa-cis-dev): :func:`capture` is a structlog processor installed on
  the JSON log handler (shared/log_config.py), so it sees every line of the api and the worker. A line whose event is
  in :data:`EVENT_CHECKS`, a tenant route that ran on the admin pool (``used_tenant_pool`` false, not a known
  deliberate rollback), or a line that says ``too many connections for role "cis_app"`` is counted in a per-process
  buffer. :func:`flush_events` turns the buffer into alerts; the api flushes it on every sampler tick, the worker
  from its loop (shared/jobs/worker.py). Renaming one of these events in the code silently drops its check: the names
  are tested against the code (tests/unit/test_s263_ops_checks.py).

Dedupe (one advisory-locked transaction, so two api tasks never double up): a check writes a bell only when it has
none in the last :data:`BELL_EVERY` (6 h), and mails only for the first bell of the UTC day: at most one e-mail per
check per UTC day. A failed publish deletes the bell it just wrote, so the next tick tries both again. E-mail is
skipped (bell only, one log line) when ALERTS_TOPIC_ARN is unset.

The job checks e-mail from the api process on purpose: the CIS ``ops_alert_email`` job runs on the worker, which is
exactly what a starved queue may mean.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

EVENT_TYPE_PREFIX = "platform.ops_check."
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"   # the platform sentinel tenant of the other ops bells
BELL_ROLES = ["admin"]
BELL_EVERY = timedelta(hours=6)
_LOCK_KEY = 1_025_263_001

# Thresholds (unchanged from the CloudWatch alarms they replace)
JOBS_STARVED_AFTER_S = 900
ROLE_CONNECTIONS_PCT = 80.0
XID_AGE_LIMIT = 1_000_000_000

JOB_RUNNER_PAGE = "/admin/job-runner"
PLATFORM_PAGE = "/admin/platform/apps"
COST_PAGE = "/admin/ai-gateway"


@dataclass(frozen=True)
class Check:
    title: str
    action: str
    page: str = PLATFORM_PAGE


CHECKS: dict[str, Check] = {
    "jobs_starved": Check("Job queue starved", "A due shared.job row has waited more than 15 minutes (no live worker, "
                          "an unregistered kind, or a free slot that does not claim it). Check the Jobs page and the "
                          "worker logs.", JOB_RUNNER_PAGE),
    "jobs_overdue": Check("Job running too long", "A running job is past twice its kind's expected time and may hold a "
                          "worker slot. Check the running rows on the Jobs page.", JOB_RUNNER_PAGE),
    "rds_role_connections": Check("Database role near its connection limit", "A login role is above 80 % of its "
                                  "CONNECTION LIMIT; new connections fail at the limit. Check that app's pool sizes "
                                  "(data standard C2.2) before raising the limit."),
    "rds_xid_age": Check("Database transaction ID age high", "max(age(datfrozenxid)) is above 1,000,000,000: "
                         "autovacuum is not freezing some table (wraparound guard). Find it with age(relfrozenxid)."),
    "cis_app_too_many_connections": Check("cis_app connection refused", "The api or worker could not get a database "
                                          "connection (too many connections for role cis_app). Check pool sizes "
                                          "before raising the limit."),
    "llm_call_log_write_failed": Check("LLM cost row lost", "A billed LLM call was not recorded in "
                                       "shared.llm_call_log (cost under-reported). Check the ECS logs for the error.",
                                       COST_PAGE),
    "decision_log_write_failed": Check("Decision row lost", "A Jev decision was not recorded in shared.decision_log "
                                       "(cost under-reported, cache entry lost). Check the ECS logs.", COST_PAGE),
    "llm_shadow_log_write_failed": Check("Shadow row lost", "A shadow LLM call was not recorded in "
                                         "shared.llm_shadow_log. Check the ECS logs.", COST_PAGE),
    "dfs_call_log_write_failed": Check("DataForSEO cost row lost", "A billed DataForSEO call was not recorded in "
                                       "shared.dfs_call_log. Check the ECS logs.", COST_PAGE),
    "tenant_flag_read_failed": Check("Tenant-pool flag unreadable", "A tenant-pool route flag could not be read from "
                                     "Redis; the route stayed on the tenant pool (RLS, fail closed). Check Redis."),
    "tenant_flag_unrecognized": Check("Tenant-pool flag value unknown", "A tenant-pool route flag is neither ON nor "
                                      "OFF (treated as ON). Fix the Redis value."),
    "tenant_pool_unavailable": Check("Tenant pool unavailable", "A tenant route got no tenant-pool connection "
                                     "(fail-closed routes answer 503). Check the aa_app_user pool and RDS."),
    "tenant_guc_not_applied": Check("Tenant context not applied", "app.tenant_id did not take on a tenant-pool "
                                    "connection; the route answered 503. Check the aa_app_user pool setup."),
    "tenant_route_admin_pool": Check("Tenant route on the admin pool", "A tenant route ran on the admin pool "
                                     "(BYPASSRLS): a route flag is rolled back. Find who set it to false."),
}

# Log event name -> check. Both variants of a write failure (async pool and sync) map to one check.
EVENT_CHECKS: dict[str, str] = {
    "llm_call_log_write_failed": "llm_call_log_write_failed",
    "llm_call_log_write_failed_sync": "llm_call_log_write_failed",
    "decision_log_write_failed": "decision_log_write_failed",
    "llm_shadow_log_write_failed": "llm_shadow_log_write_failed",
    "dfs_call_log_write_failed": "dfs_call_log_write_failed",
    "dfs_call_log_write_failed_sync": "dfs_call_log_write_failed",
    "aa817_tenant_flag_read_failed": "tenant_flag_read_failed",
    "aa817_tenant_flag_unrecognized": "tenant_flag_unrecognized",
    "aa817_tenant_pool_unavailable": "tenant_pool_unavailable",
    "aa817_tenant_guc_not_applied": "tenant_guc_not_applied",
}

# Routes rolled back to the admin pool ON PURPOSE (their log event): left out of tenant_route_admin_pool. Keep it short
# and dated; remove an entry the moment its flag is back ON. publish_log_listed: GET /v1/publish-log, flag
# aa544:publish_log_list_pool pinned to "false" before the S241 D-12 merge (AA-874), moved here from AA-CIS-Infra
# tenant_pool_alarms.tf in S263.
KNOWN_ADMIN_POOL_ROLLBACKS = frozenset({"publish_log_listed"})

TOO_MANY_CONNECTIONS = "too many connections for role"
# Only these fields of a matched line go into the bell (ids and codes; the line was already scrubbed).
_SAMPLE_FIELDS = ("event", "reason", "flag_key", "route", "stage", "job_kind", "code", "error")


@dataclass(frozen=True)
class Finding:
    check: str
    summary: str
    details: dict = field(default_factory=dict)


# ── log events: the per-process buffer ─────────────────────────────────────────────────────────

class EventBuffer:
    """Counts of matched log events per check since the last flush. Thread-safe (sync code logs from threads)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def record(self, check: str, sample: dict, now: Optional[datetime] = None) -> None:
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            item = self._items.get(check)
            if item is None:
                self._items[check] = {"count": 1, "first_at": ts, "last_at": ts, "sample": sample}
            else:
                item["count"] += 1
                item["last_at"] = ts

    def drain(self) -> dict[str, dict]:
        with self._lock:
            items, self._items = self._items, {}
        return items

    def restore(self, items: dict[str, dict]) -> None:
        """Put back what a failed flush could not alert on (merged with what arrived meanwhile)."""
        with self._lock:
            for check, old in items.items():
                cur = self._items.get(check)
                if cur is None:
                    self._items[check] = old
                else:
                    cur["count"] += old["count"]
                    cur["first_at"] = min(cur["first_at"], old["first_at"])

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


BUFFER = EventBuffer()


def _sample(event_dict: dict) -> dict:
    out = {}
    for k in _SAMPLE_FIELDS:
        v = event_dict.get(k)
        if v is not None:
            out[k] = str(v)[:200]
    return out


def match_event(event_dict: dict) -> Optional[str]:
    """The check a log line counts for, or None. Pure."""
    event = event_dict.get("event")
    name = event.split(" ", 1)[0] if isinstance(event, str) else ""
    check = EVENT_CHECKS.get(name)
    if check:
        return check
    if event_dict.get("used_tenant_pool") is False and name not in KNOWN_ADMIN_POOL_ROLLBACKS:
        return "tenant_route_admin_pool"
    for v in event_dict.values():
        if isinstance(v, str) and TOO_MANY_CONNECTIONS in v and "cis_app" in v:
            return "cis_app_too_many_connections"
    return None


def capture(_logger: Any, _method: str, event_dict: dict) -> dict:
    """structlog processor (on the JSON handler, after the scrub): count a matching line. Never raises, never changes
    the line."""
    try:
        check = match_event(event_dict)
        if check:
            BUFFER.record(check, _sample(event_dict))
    except Exception:  # noqa: BLE001 — logging must never fail because of a check
        pass
    return event_dict


# ── state checks ───────────────────────────────────────────────────────────────────────────────

XID_AGE_SQL = "SELECT max(age(datfrozenxid))::bigint FROM pg_database"


def state_findings(tick: dict) -> list[Finding]:
    """The state checks of one sampler tick, from the fields the tick logged (``ops_metrics_sampled``). Pure."""
    out: list[Finding] = []
    age = tick.get("oldest_queued_age_seconds")
    if age is not None and float(age) > JOBS_STARVED_AFTER_S:
        out.append(Finding("jobs_starved", f"oldest starved queued job waited {int(float(age))} s",
                           {"oldest_queued_age_seconds": int(float(age)), "starved": tick.get("starved") or {},
                            "live_workers": tick.get("live_workers"), "queued_due": tick.get("queued_due")}))
    overdue = tick.get("overdue_running_jobs")
    if overdue:
        out.append(Finding("jobs_overdue", f"{int(overdue)} running job(s) past twice their expected time",
                           {"overdue_running_jobs": int(overdue), "overdue": (tick.get("overdue") or [])[:10]}))
    roles = tick.get("roles") or {}
    hot = {r: v for r, v in roles.items() if float(v.get("pct") or 0) > ROLE_CONNECTIONS_PCT}
    if hot:
        out.append(Finding("rds_role_connections",
                           ", ".join(f"{r} {v['connections']}/{v['limit']}" for r, v in sorted(hot.items())),
                           {"roles": hot}))
    xid = tick.get("xid_age")
    if xid is not None and int(xid) > XID_AGE_LIMIT:
        out.append(Finding("rds_xid_age", f"max(age(datfrozenxid)) = {int(xid)}", {"xid_age": int(xid)}))
    return out


def event_findings(items: dict[str, dict]) -> list[Finding]:
    """Findings for drained log events. Pure."""
    return [Finding(check, f"{v['count']} line(s) between {v['first_at']} and {v['last_at']}",
                    {"count": v["count"], "first_at": v["first_at"], "last_at": v["last_at"],
                     "sample": v.get("sample") or {}})
            for check, v in sorted(items.items()) if check in CHECKS]


# ── alerting: bell + SNS ───────────────────────────────────────────────────────────────────────

# $2 = the evaluation time (the caller's clock; a parameter so the day boundary is testable at any hour).
LAST_BELL_SQL = """
    SELECT max(created_at) AS last,
           coalesce(bool_or(created_at >= date_trunc('day', $2::timestamptz AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'),
                    false) AS mailed_today
      FROM shared.notifications
     WHERE event_type = $1 AND created_at >= $2::timestamptz - interval '24 hours'
"""
BELL_SQL = """
    INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, 'ops_check', $3, $4::jsonb, $5::text[])
    RETURNING id
"""
DELETE_BELL_SQL = "DELETE FROM shared.notifications WHERE id = $1"


def topic_arn() -> Optional[str]:
    return (os.environ.get("ALERTS_TOPIC_ARN") or "").strip() or None


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"


def sns_client() -> Any:
    """The SNS client the alerts are published with (short timeouts; the next tick is the retry)."""
    import boto3  # lazy
    from botocore.config import Config

    return boto3.client("sns", region_name=_region(),
                        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}))


def admin_base_url() -> str:
    return os.environ.get("ADMIN_BASE_URL", "https://aa-cis.lumiguides.it.com").rstrip("/")


def render(finding: Finding, environment: Optional[str] = None) -> tuple[str, str]:
    """(subject, text) of the e-mail. Subject <= 100 characters (SNS limit), ASCII."""
    chk = CHECKS[finding.check]
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    subject = f"[AA-CIS {env}] Ops check: {chk.title}"[:100]
    lines = [f"{chk.title}: {finding.summary}.", "", chk.action, ""]
    lines += [f"{k}: {json.dumps(v, default=str) if isinstance(v, (dict, list)) else v}"
              for k, v in finding.details.items()]
    lines += ["", f"Open: {admin_base_url()}{chk.page}",
              "The same alert is in the admin notification bell. At most one e-mail per check and UTC day "
              f"(check {finding.check}, S263 in-app check; it replaced a CloudWatch alarm)."]
    return subject.encode("ascii", "replace").decode("ascii"), "\n".join(lines)


async def raise_alert(pool, finding: Finding, client: Any = None, *,
                      client_factory: Callable[[], Any] = sns_client, now: Optional[datetime] = None) -> dict:
    """Bell (at most one per check per BELL_EVERY) and, for the first bell of the UTC day, one SNS e-mail.
    Returns what was done. Raises only on a database error (the caller logs it). ``now`` is for tests."""
    now = now or datetime.now(timezone.utc)
    chk = CHECKS[finding.check]
    event_type = EVENT_TYPE_PREFIX + finding.check
    payload = json.dumps({"title": chk.title, "message": f"{finding.summary}. {chk.action}", "check": finding.check,
                          "details": finding.details}, default=str)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK_KEY)
            row = await conn.fetchrow(LAST_BELL_SQL, event_type, now)
            last = row["last"] if row else None
            if last is not None and last >= now - BELL_EVERY:
                return {"bell": False}
            bell_id = await conn.fetchval(BELL_SQL, PLATFORM_TENANT_ID, event_type, finding.check, payload,
                                          BELL_ROLES)
    mailed_today = bool(row["mailed_today"]) if row else False
    if mailed_today:
        return {"bell": True, "email": "already_today"}
    arn = topic_arn()
    if not arn:
        logger.info("ops_check_email_not_configured", check=finding.check, hint="set ALERTS_TOPIC_ARN")
        return {"bell": True, "email": "not_configured"}
    subject, text = render(finding)
    try:
        sns = client if client is not None else await asyncio.to_thread(client_factory)
        await asyncio.to_thread(sns.publish, TopicArn=arn, Subject=subject, Message=text)
    except Exception as e:  # noqa: BLE001 — retry on the next tick: remove the bell that gates the e-mail
        logger.warning("ops_check_email_failed", check=finding.check, error=type(e).__name__)
        try:
            await pool.execute(DELETE_BELL_SQL, bell_id)
        except Exception as de:  # noqa: BLE001
            logger.warning("ops_check_bell_rollback_failed", check=finding.check, error=type(de).__name__)
        return {"bell": False, "email": "failed"}
    logger.warning("ops_check_alert", check=finding.check, summary=finding.summary)
    return {"bell": True, "email": "sent"}


async def alert_all(pool, findings: list[Finding], client: Any = None, *,
                    client_factory: Callable[[], Any] = sns_client) -> dict:
    """raise_alert for each finding; one failing finding never stops the others. {check: result}."""
    out: dict[str, Any] = {}
    for f in findings:
        try:
            out[f.check] = await raise_alert(pool, f, client, client_factory=client_factory)
        except Exception as e:  # noqa: BLE001
            out[f.check] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return out


async def flush_events(pool, client: Any = None, *, buffer: EventBuffer = BUFFER,
                       client_factory: Callable[[], Any] = sns_client) -> dict:
    """Alert on the log events counted since the last flush. Events whose alert failed go back to the buffer."""
    items = buffer.drain()
    if not items:
        return {}
    try:
        results = await alert_all(pool, event_findings(items), client, client_factory=client_factory)
    except BaseException:   # cancelled or timed out mid-flush: nothing is lost, the next flush retries
        buffer.restore(items)
        raise
    failed = {c: items[c] for c, r in results.items() if "error" in r or r.get("email") == "failed"}
    if failed:
        buffer.restore(failed)
    return results
