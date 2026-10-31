"""
AA-751 / AA-832 — in-process ops metrics sampler of the api task.

Every OPS_METRICS_INTERVAL_S (300 s) the api publishes two CloudWatch namespaces, one PutMetricData
call each, and logs one line `ops_metrics_sampled` with the values:

  AA/RDS, dimension Role=<rolname>, per LOGIN role with a CONNECTION LIMIT (AA-832):
    RoleConnections         Count    sessions of the role in pg_stat_activity
    RoleConnectionLimitPct  Percent  100 * RoleConnections / rolconnlimit
  AA/Jobs/CIS, no dimensions (AA-751, shared/jobs/backlog.py):
    OldestQueuedAgeSeconds  Seconds  capacity-aware: only starved due queued rows count
    OverdueRunningJobs      Count    running rows past 2 x their kind's expected_seconds

It replaces the AA-832 chain (EventBridge every 5 min -> ops-probe Lambda -> POST
/admin/job-runner/rds-role-connections/schedule -> a shared.job row -> CIS worker -> PutMetricData):
no API Gateway / WAF / Lambda traffic, no 288 job rows a day, and the job metrics do not depend on
the worker they measure. AA-CIS-Infra alarms on both namespaces (observability_ecosys.tf); missing
samples fire the `*-sampling-stopped` alarms, so a dead api (or a dead loop) is still visible.

- RDS roles are discovered on every tick (`rolcanlogin AND rolconnlimit > 0`), so a new app role or a
  changed limit needs no code change. `usename` is the session user, the one CONNECTION LIMIT counts.
  The main pool runs as aa_cis_admin, which sees other roles' rows (verified live 16/10/2026).
- boto3 runs in asyncio.to_thread with short timeouts and no retries, so a CloudWatch outage costs one
  thread for at most ~8 s per call, never the event loop. Each part of a tick (AA/RDS, retired-kind
  sweep, AA/Jobs/CIS) also runs under asyncio.wait_for(PART_TIMEOUT_S): the main pool has no
  command_timeout, so a hung acquire/query (half-open TCP to RDS) lands as a `*_error` TimeoutError
  and the next tick tries again instead of stalling the loop until the task restarts.
- Retired kinds (shared/jobs/registry.RETIRED_KINDS, kinds whose handler was removed): every tick
  cancels their queued/running rows before the backlog sample. No live worker can claim them, so a
  row left over from a rolling deploy (the old api enqueueing during the roll) would otherwise page
  `*-jobs-queue-age` as starved `unregistered` and block every later Deploy Dev. A kind is swept only
  while it is NOT registered.
- AA-987: each tick also sweeps the TripPlanner purge deliveries (services/jobs/tripplanner_purge_job.sweep): every
  tour lifecycle event of the last 2 h without a `tripplanner_purge` job gets one (one idempotent INSERT ... SELECT),
  so an after-commit enqueue lost to a crash still reaches TripPlanner a few minutes later. Field
  `tripplanner_purge_swept` (jobs created) or `tripplanner_purge_error`.
- AA-835: each tick also pulls one batch of the Booking supply feed (services/supply_content/pull.pull_once: the
  booking.v_supply_outbox events after the shared.supply_feed_cursor, applied to shared.supply_entity, one
  `supply_content_draft` job per applied facts version), DB only. Field `supply_pull_applied` (the batch counts, when
  it had events) or `supply_pull_error`. Off with SUPPLY_FEED_PULL=off; skipped while the draft kind is not
  registered.
- AA-917: each tick also queues the writing-memory fact filter (`writing_signal_fact_filter`, $0) once per UTC day
  while signals wait as `pending` (one count; idempotency key per day). The filter queues an automatic mining run only
  when its trigger holds (services/writing_memory/mining.decide_run). Field `writing_memory_fact_filter` (the job
  id) or `writing_memory_error`.
- AA-920: each tick also queues the writing-memory post-activation check (`writing_memory_post_activation`, $0) once
  per hour while an active release with content exists (one EXISTS; idempotency key per hour; release 0 is empty, so
  nothing is queued today). Field `writing_memory_post_activation` (the job id) or
  `writing_memory_post_activation_error`.
- AA-922: each tick also queues the weekly writing-memory KPI job (`writing_memory_kpi_weekly`, $0) for the last full
  ISO week (idempotency key per week: one job a week). Field `writing_memory_kpi_weekly` (the job id) or
  `writing_memory_kpi_weekly_error`.
- AA-913: each tick also reconciles the platform alert recipients with SNS
  (shared/alert_recipients.periodic_reconcile: one ListSubscriptionsByTopic, throttled like a page view; a no-op
  when ALERTS_TOPIC_ARN / ALERT_BOOTSTRAP_EMAIL are unset). An address removed on the admin page while its
  confirmation was pending, and confirmed afterwards, is unsubscribed again within one tick, not only when an admin
  opens the page. Its fields: `alert_recipients` (counts) or `alert_recipients_error`.
- AA-1003: each tick also queues the daily KPI snapshot (`kpi_snapshot_daily`, $0, DB reads only) for the ICT day the
  daily chain covers (services/reports/periods.snapshot_day: day D from 00:30 UTC of D + 1); idempotency key per day,
  so only the first tick of the day creates a row. The snapshot job then queues `report_generate` for the daily
  reports (EX-04, EX-06; R2: OPS-12, TP-03). Only while the kind is registered. Field `kpi_snapshot` (the job id,
  when created) or `reports_error`. R2: the same part queues the TP-03 inquiry handoff check once per UTC hour
  (`inquiry_handoff_check`, idempotency key per hour), field `inquiry_handoff_check`.
- AA-1002 (S10): each tick also queues the nightly LLM cost reconciliation (`llm_cost_reconcile`, $0) once per UTC day
  from 02:00 UTC (idempotency key per day), while the kind is registered and LLM_COST_RECONCILE is not off. Field
  `llm_cost_reconcile` (the job id, when created) or `llm_cost_reconcile_error`.
- AA-845: every JEV_CREDIT_CHECK_EVERY_S (shared/jev_credit.py, 30 min) the loop also runs the Jev low-credit
  check (estimated TypeSafe credit below the threshold -> throttled bell + operator e-mail). It runs after the
  tick, outside sample_once, under its own PART_TIMEOUT_S, and logs its own line (`jev_credit_low_checked` /
  `jev_credit_low_alert`, or `jev_credit_check_error` here). The schedule is a local of the loop (monotonic),
  so there is no module state to reset; the first check runs on the first tick.
- api only (not the worker service); started from api/main.py's lifespan after the main pool,
  cancelled before the pool closes. Off when ENVIRONMENT == "test". Never raises: a failing part is
  logged in the tick's line (`*_error`) and the loop goes on. With two api tasks (rolling deploy)
  both publish; the alarms use Maximum / SampleCount, so a duplicate datapoint changes nothing.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

OPS_METRICS_INTERVAL_S = 300.0
OPS_METRICS_FIRST_DELAY_S = 30.0   # let the app finish starting before the first sample
PART_TIMEOUT_S = 50.0              # bound on each part of a tick (6 parts -> <= 300 s, then the interval sleep)

NAMESPACE_RDS = "AA/RDS"
METRIC_CONNECTIONS = "RoleConnections"
METRIC_LIMIT_PCT = "RoleConnectionLimitPct"

NAMESPACE_JOBS = "AA/Jobs/CIS"
METRIC_OLDEST_QUEUED_AGE = "OldestQueuedAgeSeconds"
METRIC_OVERDUE_RUNNING = "OverdueRunningJobs"

ROLE_CONNECTIONS_SQL = """
    SELECT r.rolname, r.rolconnlimit, count(a.pid)::int AS connections
      FROM pg_roles r
      LEFT JOIN pg_stat_activity a ON a.usename = r.rolname
     WHERE r.rolcanlogin AND r.rolconnlimit > 0
     GROUP BY r.rolname, r.rolconnlimit
     ORDER BY r.rolname
"""


def sampler_enabled() -> bool:
    return os.environ.get("ENVIRONMENT", "").lower() != "test"


def cloudwatch_region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"


def cloudwatch_client(region: Optional[str] = None):
    """boto3 CloudWatch client: connect 3 s, read 5 s, one attempt (the next tick is the retry)."""
    import boto3  # lazy: tests and the worker never need it
    from botocore.config import Config

    cfg = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
    return boto3.client("cloudwatch", region_name=region or cloudwatch_region(), config=cfg)


# ── AA/RDS (moved from the removed services/jobs/ops_rds_role_connections_job.py) ──────────────

def role_samples(rows) -> list[dict]:
    """[{role, limit, connections, pct}] from the query rows. Pure, unit-tested."""
    out = []
    for r in rows:
        limit = int(r["rolconnlimit"])
        conns = int(r["connections"])
        out.append({"role": r["rolname"], "limit": limit, "connections": conns,
                    "pct": round(100.0 * conns / limit, 2)})
    return out


def metric_data(samples: list[dict], timestamp: datetime) -> list[dict]:
    """The AA/RDS PutMetricData `MetricData` list: two datums per role. Pure, unit-tested."""
    data = []
    for s in samples:
        dims = [{"Name": "Role", "Value": s["role"]}]
        data.append({"MetricName": METRIC_CONNECTIONS, "Dimensions": dims, "Timestamp": timestamp,
                     "Value": float(s["connections"]), "Unit": "Count"})
        data.append({"MetricName": METRIC_LIMIT_PCT, "Dimensions": dims, "Timestamp": timestamp,
                     "Value": float(s["pct"]), "Unit": "Percent"})
    return data


# ── AA/Jobs/CIS ─────────────────────────────────────────────────────────────────────────────────

def jobs_metric_data(sample: dict, timestamp: datetime) -> list[dict]:
    """The AA/Jobs/CIS `MetricData`: two datums, no dimensions. Pure, unit-tested."""
    return [
        {"MetricName": METRIC_OLDEST_QUEUED_AGE, "Timestamp": timestamp,
         "Value": float(sample["oldest_queued_age_seconds"]), "Unit": "Seconds"},
        {"MetricName": METRIC_OVERDUE_RUNNING, "Timestamp": timestamp,
         "Value": float(sample["overdue_running_jobs"]), "Unit": "Count"},
    ]


def _registry() -> dict:
    """Every registered kind (expected_seconds, background). Importing services.jobs registers them."""
    from shared.jobs.registry import kinds
    from shared.jobs.worker import load_kinds

    load_kinds()
    return kinds()


async def _put(client, namespace: str, data: list[dict]) -> None:
    await asyncio.to_thread(client.put_metric_data, Namespace=namespace, MetricData=data)


async def _rds_part(pool, client, ts: datetime, out: dict) -> None:
    samples = role_samples(await pool.fetch(ROLE_CONNECTIONS_SQL))
    out["roles"] = {s["role"]: {"connections": s["connections"], "limit": s["limit"], "pct": s["pct"]}
                    for s in samples}
    if samples:   # no limited role: nothing to publish (sampling-stopped will say so)
        await _put(client, NAMESPACE_RDS, metric_data(samples, ts))
    out["rds_published"] = 2 * len(samples)


async def _retired_part(pool, kinds: dict, out: dict) -> None:
    from shared.jobs import queue
    from shared.jobs.registry import RETIRED_KINDS

    retired = {k: v for k, v in RETIRED_KINDS.items() if k not in kinds}
    if retired:
        n = await queue.cancel_retired_kinds(pool, retired)
        if n:
            out["retired_cancelled"] = n


async def _jobs_part(pool, client, kinds: dict, ts: datetime, out: dict) -> None:
    from shared.jobs import backlog

    b = await backlog.sample(pool, kinds)
    out.update(oldest_queued_age_seconds=b["oldest_queued_age_seconds"],
               overdue_running_jobs=b["overdue_running_jobs"], starved=b["starved"], overdue=b["overdue"],
               queued_due=b["queued_due"], running=b["running"], live_workers=b["live_workers"])
    await _put(client, NAMESPACE_JOBS, jobs_metric_data(b, ts))
    out["jobs_published"] = 2


async def _tripplanner_purge_part(pool, kinds: dict, out: dict) -> None:
    """AA-987: enqueue a TripPlanner purge for every recent lifecycle event that has no job yet (an after-commit
    enqueue that was lost, a path that does not enqueue). One idempotent INSERT ... SELECT. Only while the kind is
    registered (a worker can run it), like the retired-kind rule the other way round."""
    from services.jobs.tripplanner_purge_job import KIND, sweep

    if KIND not in kinds:
        return
    n = await sweep(pool)
    if n:
        out["tripplanner_purge_swept"] = n


def supply_pull_enabled() -> bool:
    return (os.environ.get("SUPPLY_FEED_PULL") or "on").strip().lower() not in ("off", "0", "false", "no")


async def _supply_pull_part(pool, kinds: dict, out: dict) -> None:
    """AA-835: one batch of the Booking supply feed into the CIS supply content tables. Only while the draft kind
    is registered (the batch queues its jobs), and not with SUPPLY_FEED_PULL=off."""
    from services.supply_content.pull import DRAFT_KIND, pull_once

    if DRAFT_KIND not in kinds or not supply_pull_enabled():
        return
    result = await pull_once(pool)
    if result.get("events"):
        out["supply_pull_applied"] = result
_PENDING_SIGNALS_SQL = "SELECT count(*) FROM shared.writing_signal WHERE fact_status = 'pending'"


async def _writing_memory_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-917: one fact-filter job a UTC day while signals are pending (a new signal is captured `pending` on the
    edit path; the filter runs here, never on the request path). Only while the kind is registered."""
    from services.jobs.writing_memory_job import FACT_FILTER
    from shared.jobs.registry import enqueue

    if FACT_FILTER not in kinds:
        return
    pending = int(await pool.fetchval(_PENDING_SIGNALS_SQL) or 0)
    if not pending:
        return
    job_id, created = await enqueue(pool, FACT_FILTER, {"trigger": "daily"},
                                    idempotency_key=f"{FACT_FILTER}:day:{ts.date().isoformat()}",
                                    created_by="scheduler:aa-917")
    if created:
        out["writing_memory_fact_filter"] = job_id


_CONTENT_RELEASE_SQL = """
    SELECT EXISTS (SELECT 1 FROM shared.writing_memory_release
                    WHERE status = 'active' AND (content_class <> 'empty' OR cardinality(rule_ids) > 0))
"""


async def _writing_memory_post_activation_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-920 WM-26: one post-activation check job an hour while a release with content is active ($0; release 0 is
    empty, so today this queues nothing). Only while the kind is registered."""
    from services.jobs.writing_memory_job import POST_ACTIVATION
    from services.writing_memory.config import POST_ACTIVATION_EVERY_S
    from shared.jobs.registry import enqueue

    if POST_ACTIVATION not in kinds:
        return
    if not await pool.fetchval(_CONTENT_RELEASE_SQL):
        return
    bucket = int(ts.timestamp()) // int(POST_ACTIVATION_EVERY_S)
    job_id, created = await enqueue(pool, POST_ACTIVATION, {"apply": True},
                                    idempotency_key=f"{POST_ACTIVATION}:{bucket}", created_by="scheduler:aa-920")
    if created:
        out["writing_memory_post_activation"] = job_id


async def _writing_memory_kpi_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-922 WM-30: one weekly KPI job per ISO week, for the week before ($0; idempotent per week). Only while the
    kind is registered."""
    from services.jobs.writing_memory_job import KPI_WEEKLY
    from services.writing_memory.kpi import last_full_week
    from shared.jobs.registry import enqueue

    if KPI_WEEKLY not in kinds:
        return
    week = last_full_week(ts)
    job_id, created = await enqueue(pool, KPI_WEEKLY, {"week_start": week.isoformat(), "apply": True,
                                                       "trigger": "schedule"},
                                    idempotency_key=f"{KPI_WEEKLY}:{week.isoformat()}", created_by="scheduler:aa-922")
    if created:
        out["writing_memory_kpi_weekly"] = job_id


async def _reports_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1003: the daily KPI snapshot (then the daily reports, chained by the job). Only while the kind is
    registered; one idempotent INSERT a tick (the first tick of the day creates the row)."""
    from services.jobs.report_jobs import SNAPSHOT_KIND, enqueue_scheduled_snapshot

    if SNAPSHOT_KIND in kinds:
        job_id, created, day = await enqueue_scheduled_snapshot(pool, now=ts)
        if created:
            out["kpi_snapshot"] = {"job_id": job_id, "day": day.isoformat()}
    # AA-1003 R2: the TP-03 inquiry handoff check, once per UTC hour (one indexed EXISTS a tick)
    from services.jobs.report_ops_jobs import CHECK_KIND, enqueue_hourly_check

    if CHECK_KIND in kinds:
        check_id, check_created, hour = await enqueue_hourly_check(pool, now=ts)
        if check_created:
            out["inquiry_handoff_check"] = {"job_id": check_id, "hour": hour.isoformat()}


async def _llm_cost_reconcile_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1002 (S10): the nightly LLM cost reconciliation, once per UTC day from 02:00 UTC (idempotency key per day;
    one indexed EXISTS a tick after that hour). Only while the kind is registered; LLM_COST_RECONCILE=off stops it."""
    from services.jobs.llm_cost_reconcile_job import KIND, enqueue_nightly, schedule_enabled

    if KIND not in kinds or not schedule_enabled():
        return
    job_id, created, day = await enqueue_nightly(pool, now=ts)
    if created:
        out["llm_cost_reconcile"] = {"job_id": job_id, "day": day.isoformat()}


async def _llm_probe_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1018: the daily LLM connection probe, once per UTC day from its hour (idempotency key per day; one indexed
    EXISTS a tick after that hour). Only while the kind is registered and LLM_DAILY_PROBE=on (default off)."""
    from services.jobs.llm_probe_job import KIND, enqueue_daily, schedule_enabled

    if KIND not in kinds or not schedule_enabled():
        return
    job_id, created, day = await enqueue_daily(pool, now=ts)
    if created:
        out["llm_connection_probe"] = {"job_id": job_id, "day": day.isoformat()}


async def _alert_recipients_part(pool, redis, out: dict) -> None:
    from shared import alert_recipients

    result = await alert_recipients.periodic_reconcile(pool, redis=redis)
    if result is None:   # not configured on this environment
        return
    if result.error:
        out["alert_recipients_error"] = f"SnsError: {result.error}"
        return
    out["alert_recipients"] = {"cached": result.cached, "changes": len(result.changes),
                               "unsubscribed": len(result.unsubscribed)}


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:300]}"


async def sample_once(pool, client, kinds: Optional[dict] = None,
                      part_timeout_s: float = PART_TIMEOUT_S, redis: Any = None) -> dict:
    """One tick: AA/RDS, the alert-recipient reconcile (AA-913), the retired-kind sweep, the TripPlanner purge sweep
    (AA-987), the Booking supply feed pull (AA-835), the daily writing-memory fact filter (AA-917), then
    AA/Jobs/CIS, each part on its own and under part_timeout_s (one failing or hanging never blocks the others).
    Returns the fields of the `ops_metrics_sampled` line. Never raises (except CancelledError)."""
    ts = datetime.now(timezone.utc)
    out: dict[str, Any] = {"sampled_at": ts.isoformat()}

    try:
        await asyncio.wait_for(_rds_part(pool, client, ts, out), timeout=part_timeout_s)
    except Exception as e:   # asyncio.TimeoutError included
        out["rds_error"] = _err(e)

    try:
        await asyncio.wait_for(_alert_recipients_part(pool, redis, out), timeout=part_timeout_s)
    except Exception as e:
        out["alert_recipients_error"] = _err(e)

    try:
        if kinds is None:
            kinds = await asyncio.wait_for(asyncio.to_thread(_registry), timeout=part_timeout_s)
    except Exception as e:
        out["jobs_error"] = _err(e)
        return out

    try:
        await asyncio.wait_for(_retired_part(pool, kinds, out), timeout=part_timeout_s)
    except Exception as e:
        out["retired_error"] = _err(e)

    try:
        await asyncio.wait_for(_tripplanner_purge_part(pool, kinds, out), timeout=part_timeout_s)
    except Exception as e:
        out["tripplanner_purge_error"] = _err(e)

    try:
        await asyncio.wait_for(_supply_pull_part(pool, kinds, out), timeout=part_timeout_s)
    except Exception as e:
        out["supply_pull_error"] = _err(e)

    try:
        await asyncio.wait_for(_writing_memory_part(pool, kinds, ts, out), timeout=part_timeout_s)
    except Exception as e:
        out["writing_memory_error"] = _err(e)
    # AA-920: a single EXISTS + at most one idempotent INSERT an hour; it shares the writing-memory part's budget, so a
    # tick keeps its 6 timed parts (<= 300 s)
    try:
        await asyncio.wait_for(_writing_memory_post_activation_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["writing_memory_post_activation_error"] = _err(e)
    try:
        await asyncio.wait_for(_writing_memory_kpi_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:  # noqa: BLE001 — one part never stops the tick
        out["writing_memory_kpi_weekly_error"] = _err(e)

    # AA-1003: one idempotent INSERT a tick, inside the same short budget as the AA-920 check above
    try:
        await asyncio.wait_for(_reports_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["reports_error"] = _err(e)

    # AA-1002 (S10): one idempotent INSERT a day, same short budget
    try:
        await asyncio.wait_for(_llm_cost_reconcile_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["llm_cost_reconcile_error"] = _err(e)

    # AA-1018: one idempotent INSERT a day when LLM_DAILY_PROBE=on, same short budget
    try:
        await asyncio.wait_for(_llm_probe_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["llm_connection_probe_error"] = _err(e)

    try:
        await asyncio.wait_for(_jobs_part(pool, client, kinds, ts, out), timeout=part_timeout_s)
    except Exception as e:
        out["jobs_error"] = _err(e)
    return out


async def jev_credit_tick(pool, part_timeout_s: float = PART_TIMEOUT_S) -> Optional[dict]:
    """AA-845: one Jev low-credit check (shared/jev_credit.check_low_credit). Never raises (except
    CancelledError); a failure or a hang is one `jev_credit_check_error` warning and the next check retries."""
    from shared import jev_credit

    try:
        return await asyncio.wait_for(jev_credit.check_low_credit(pool, source="ops_sampler"),
                                      timeout=part_timeout_s)
    except Exception as e:   # asyncio.TimeoutError included
        logger.warning("jev_credit_check_error", error=_err(e))
        return None


def _log(out: dict) -> None:
    if any(k in out for k in ("rds_error", "retired_error", "jobs_error", "alert_recipients_error",
                              "tripplanner_purge_error", "supply_pull_error", "writing_memory_error",
                              "writing_memory_kpi_weekly_error",
                              "reports_error")):
        logger.warning("ops_metrics_sampled", **out)
    else:
        logger.info("ops_metrics_sampled", **out)


async def run_ops_metrics_sampler(app, interval_s: float = OPS_METRICS_INTERVAL_S,
                                  first_delay_s: float = OPS_METRICS_FIRST_DELAY_S,
                                  client_factory: Callable[[], Any] = cloudwatch_client,
                                  part_timeout_s: float = PART_TIMEOUT_S,
                                  jev_check_every_s: Optional[float] = None) -> None:
    """Loops until cancelled. Reads app.state.pool on every tick, so it never holds a stale pool; the
    CloudWatch client is made once (in a thread), and again on the next tick if making it failed."""
    if jev_check_every_s is None:
        from shared.jev_credit import JEV_CREDIT_CHECK_EVERY_S

        jev_check_every_s = JEV_CREDIT_CHECK_EVERY_S
    client = None
    next_jev_check = 0.0          # time.monotonic() deadline of the next AA-845 Jev credit check
    await asyncio.sleep(first_delay_s)
    while True:
        try:
            pool = getattr(app.state, "pool", None)
            if pool is not None:
                if client is None:
                    client = await asyncio.to_thread(client_factory)
                _log(await sample_once(pool, client, part_timeout_s=part_timeout_s,
                                       redis=getattr(app.state, "redis", None)))
        except Exception as e:  # never kill the loop (CancelledError is not an Exception)
            logger.warning("ops_metrics_sampled", error=_err(e))
        pool = getattr(app.state, "pool", None)
        if pool is not None and time.monotonic() >= next_jev_check:
            next_jev_check = time.monotonic() + jev_check_every_s
            await jev_credit_tick(pool, part_timeout_s)
        await asyncio.sleep(interval_s)


def start_ops_metrics_sampler(app, interval_s: float = OPS_METRICS_INTERVAL_S) -> Optional[asyncio.Task]:
    """None (nothing started) when ENVIRONMENT == "test"."""
    if not sampler_enabled():
        return None
    return asyncio.create_task(run_ops_metrics_sampler(app, interval_s), name="aa751-ops-metrics-sampler")


async def stop_ops_metrics_sampler(task: Optional[asyncio.Task]) -> None:
    """Cancel and wait; swallows the CancelledError (and anything else) so shutdown goes on."""
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
