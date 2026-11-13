"""
AA-751 / AA-832 / S263 (AA-1025) — in-process ops sampler of the api task.

Every OPS_METRICS_INTERVAL_S (300 s) the api reads, and logs in one line `ops_metrics_sampled`:

  per LOGIN role with a CONNECTION LIMIT (AA-832): sessions in pg_stat_activity and the % of the limit (`roles`);
  max(age(datfrozenxid)) over the databases (`xid_age`, S263);
  the shared.job backlog (AA-751, shared/jobs/backlog.py): the capacity-aware age of the oldest starved due queued
  row and the running rows past 2 x their kind's expected_seconds.

S263 alert diet: these values are no longer published to CloudWatch (they were AA/RDS and AA/Jobs/CIS custom metrics
with eleven alarms). The tick evaluates them itself (shared/ops_checks.py state checks: jobs_starved, jobs_overdue,
rds_role_connections, rds_xid_age) and flushes the log events the api process counted (lost cost-log writes,
tenant-pool fail-closed states, cis_app refused connections); a finding raises the admin bell and at most one e-mail
a day per check on the alerts SNS topic (field `ops_checks`, or `ops_checks_error`). A dead api is CloudWatch's
`aa-cis-dev-api-no-healthy-task`; a starved queue (dead worker included) is mailed from here, not through the worker.
The AA-832 chain (EventBridge -> ops-probe Lambda -> admin POST -> shared.job -> worker) is long gone.

- RDS roles are discovered on every tick (`rolcanlogin AND rolconnlimit > 0`), so a new app role or a
  changed limit needs no code change. `usename` is the session user, the one CONNECTION LIMIT counts.
  The main pool runs as aa_cis_admin, which sees other roles' rows (verified live 16/10/2026).
- boto3 (SNS publish of an alert) runs in asyncio.to_thread with short timeouts and no retries, so an SNS outage
  costs one thread for at most ~8 s per call, never the event loop. Each part of a tick (roles, retired-kind
  sweep, backlog, ops checks) also runs under asyncio.wait_for(PART_TIMEOUT_S): the main pool has no
  command_timeout, so a hung acquire/query (half-open TCP to RDS) lands as a `*_error` TimeoutError
  and the next tick tries again instead of stalling the loop until the task restarts.
- Retired kinds (shared/jobs/registry.RETIRED_KINDS, kinds whose handler was removed): every tick
  cancels their queued/running rows before the backlog sample. No live worker can claim them, so a
  row left over from a rolling deploy (the old api enqueueing during the roll) would otherwise show as starved
  `unregistered` (the jobs_starved check) and block every later Deploy Dev. A kind is swept only
  while it is NOT registered.
- AA-987: each tick also sweeps the TripPlanner purge deliveries (services/jobs/tripplanner_purge_job.sweep): every
  tour lifecycle event of the last 2 h without a `tripplanner_purge` job gets one (one idempotent INSERT ... SELECT),
  so an after-commit enqueue lost to a crash still reaches TripPlanner a few minutes later. Field
  `tripplanner_purge_swept` (jobs created) or `tripplanner_purge_error`.
- AA-1056: each tick also reads the lag of the tour on/off events (retired / paused / restored, last 30 days) to the
  first recompute that finished after them (services/recompute/lifecycle.lag_summary, field `recompute_lifecycle`;
  the ops check `recompute_lifecycle_lag` alerts on an event waiting over 30 min), and every 2 hours (the loop's
  monotonic schedule, like the Jev check) sweeps: one platform recompute for the events nothing covered (field
  `recompute_sweep`, or `recompute_lifecycle_error`).
- AA-1055: once a day (the loop's monotonic schedule, research_coverage.CHECK_EVERY_S; the first on the first tick) the
  tick also measures the share of the search demand research scope that is stale per market an active tenant targets
  and the places waiting for research after an atomize (services/acp_contract/research_coverage.measure, READ ONLY,
  bounded statements, $0). Field `research_stale` (the summary) or `research_stale_error`; the ops check
  `research_stale_share` alerts above 20 % in any market (bell once per 20 h, one e-mail a day).
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
  reports (EX-04, EX-06; R2: OPS-12). Only while the kind is registered. Field `kpi_snapshot` (the job id,
  when created) or `reports_error`. R2 also queued the TP-03 inquiry handoff check every UTC hour; the R0 hotfix
  (root ADR 0019, B2B-only model) retired TP-03, so nothing queues `inquiry_handoff_check` any more.
- AA-1002 (S10): each tick also queues the nightly LLM cost reconciliation (`llm_cost_reconcile`, $0) once per UTC day
  from 02:00 UTC (idempotency key per day), while the kind is registered and LLM_COST_RECONCILE is not off. Field
  `llm_cost_reconcile` (the job id, when created) or `llm_cost_reconcile_error`.
- AA-1026 (S267): the ops checks part also evaluates LLM stage coverage (shared/llm_client/stage_coverage.py: every
  active stage has a model that answered within 36 h, no refusal or open circuit since); a stage without one raises
  the ops check ``llm_stage_coverage.<stage>`` (bell once a day per stage, one e-mail per stage and UTC day). Fields
  `stage_coverage_uncovered` (the stages) or `stage_coverage_error`.
- AA-1042 (S268): each tick also queues the LLM route watch (`llm_route_watch`, $0) once per 15-min UTC bucket and its
  daily digest once per UTC day from 01:00 UTC (idempotency keys), while the kind is registered and LLM_ROUTE_WATCH is
  not off. Fields `llm_route_watch` / `llm_route_digest` (the job id, when created) or `llm_route_watch_error`.
- AA-1045: each tick also queues the usage rollup (`ai_usage_rollup`, $0, database only) once per quarter hour (the
  last hours re-rolled for late rows; idempotency key per quarter hour) and, once, its backfill of the raw window;
  only while the kind is registered, CIS 372 is applied and AI_USAGE_ROLLUP is not off. Field `ai_usage_rollup` (the
  jobs created) or `ai_usage_rollup_error`.
- AA-1093 part 3 (plan D11): each tick also queues the nightly backup of the pilot tenant (`tenant_backup`, $0,
  database and S3) once per UTC day from 20:00 UTC (idempotency key per day), only while the kind is registered,
  TENANT_BACKUP_PILOT=on (default off) and a tenant is the pilot. Field `tenant_backup` (the job id, when created) or
  `tenant_backup_error`.
- AA-1093 lane L2: each tick also makes sure every open tenant erasure request whose phase has work (scheduled from its
  latest not_before, dry_run, apply, verify) has one live `tenant_erasure_saga` job ($0; services/jobs/
  tenant_erasure_saga_job.ensure_jobs), only while the kind is registered and CIS 408 is applied. Field
  `tenant_erasure` (the jobs created) or `tenant_erasure_error`.
- AA-845: every JEV_CREDIT_CHECK_EVERY_S (shared/jev_credit.py, 30 min) the loop also runs the Jev low-credit
  check (estimated TypeSafe credit below the threshold -> throttled bell + operator e-mail). It runs after the
  tick, outside sample_once, under its own PART_TIMEOUT_S, and logs its own line (`jev_credit_low_checked` /
  `jev_credit_low_alert`, or `jev_credit_check_error` here). AA-1025 B5: right after it, the low-credit check of the
  vendor API connections (shared/provider_credit.py; `provider_credit_check_error` here). The schedule is a local of
  the loop (monotonic),
  so there is no module state to reset; the first check runs on the first tick.
- api only (not the worker service); started from api/main.py's lifespan after the main pool,
  cancelled before the pool closes. Off when ENVIRONMENT == "test". Never raises: a failing part is
  logged in the tick's line (`*_error`) and the loop goes on. With two api tasks (rolling deploy) both evaluate;
  the bell + e-mail dedupe is one advisory-locked transaction (shared/ops_checks.py), so an alert goes out once.
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
OPS_FLUSH_ON_STOP_TIMEOUT_S = 5.0  # S263 review: the last ops-event flush on shutdown (api stopTimeout is 90 s)

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


def alerts_client():
    """The SNS client the ops-check alerts are published with (shared/ops_checks.sns_client)."""
    from shared import ops_checks

    return ops_checks.sns_client()


# ── roles (moved from the removed services/jobs/ops_rds_role_connections_job.py) ───────────────

def role_samples(rows) -> list[dict]:
    """[{role, limit, connections, pct}] from the query rows. Pure, unit-tested."""
    out = []
    for r in rows:
        limit = int(r["rolconnlimit"])
        conns = int(r["connections"])
        out.append({"role": r["rolname"], "limit": limit, "connections": conns,
                    "pct": round(100.0 * conns / limit, 2)})
    return out


def _registry() -> dict:
    """Every registered kind (expected_seconds, background). Importing services.jobs registers them."""
    from shared.jobs.registry import kinds
    from shared.jobs.worker import load_kinds

    load_kinds()
    return kinds()


async def _rds_part(pool, out: dict) -> None:
    from shared.ops_checks import XID_AGE_SQL

    samples = role_samples(await pool.fetch(ROLE_CONNECTIONS_SQL))
    out["roles"] = {s["role"]: {"connections": s["connections"], "limit": s["limit"], "pct": s["pct"]}
                    for s in samples}
    xid = await pool.fetchval(XID_AGE_SQL)
    if xid is not None:
        out["xid_age"] = int(xid)


async def _retired_part(pool, kinds: dict, out: dict) -> None:
    from shared.jobs import queue
    from shared.jobs.registry import RETIRED_KINDS

    retired = {k: v for k, v in RETIRED_KINDS.items() if k not in kinds}
    if retired:
        n = await queue.cancel_retired_kinds(pool, retired)
        if n:
            out["retired_cancelled"] = n


async def _jobs_part(pool, kinds: dict, out: dict) -> None:
    from shared.jobs import backlog

    b = await backlog.sample(pool, kinds)
    out.update(oldest_queued_age_seconds=b["oldest_queued_age_seconds"],
               overdue_running_jobs=b["overdue_running_jobs"], starved=b["starved"], overdue=b["overdue"],
               queued_due=b["queued_due"], running=b["running"], live_workers=b["live_workers"],
               held_kinds=b.get("held", []))     # AA-1025 H2: kinds the capacity backpressure holds (not starved)


async def _ops_checks_part(pool, client, out: dict) -> None:
    """S263: the state checks of this tick plus the log events this process counted -> bell + e-mail."""
    from shared import ops_checks

    # AA-1025 H1b: + the app spend checks (TripPlanner against its app profile budget: the llm_spend_alert signal).
    findings = ops_checks.state_findings(out) + await ops_checks.app_spend_findings(pool)
    # AA-1026 (S267): + one check per active LLM stage without a model that answers ($0 registry reads)
    try:
        coverage = await ops_checks.stage_coverage_findings(pool)
    except Exception as e:  # noqa: BLE001 — the other checks still alert
        out["stage_coverage_error"] = _err(e)
        coverage = []
    if coverage:
        out["stage_coverage_uncovered"] = [f.details["stage"] for f in coverage]
    findings += coverage
    results = await ops_checks.alert_all(pool, findings, client)
    results.update(await ops_checks.flush_events(pool, client))
    if results:
        out["ops_checks"] = results


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


async def _recompute_lifecycle_part(pool, kinds: dict, out: dict, sweep: bool) -> None:
    """AA-1056: the lag of tour on/off events to a finished recompute (field `recompute_lifecycle`, which the ops check
    `recompute_lifecycle_lag` reads) every tick; when `sweep` (every lifecycle.SWEEP_EVERY_S, the loop's schedule) and
    the kind is registered, one platform recompute for the events nothing covered (field `recompute_sweep`)."""
    from services.recompute import lifecycle

    out["recompute_lifecycle"] = await lifecycle.lag_summary(pool)
    if sweep and "recompute" in kinds:
        swept = await lifecycle.sweep(pool)
        if swept.get("events"):
            out["recompute_sweep"] = swept


async def _research_stale_part(pool, out: dict) -> None:
    """AA-1055: the stale share of the research scope per targeted market and the places waiting after an atomize
    (services/acp_contract/research_coverage.measure, read only, $0; field `research_stale`, which the ops check
    `research_stale_share` reads). Called once a day on the loop's schedule; every statement is READ ONLY and
    bounded."""
    from services.acp_contract import research_coverage as rc

    out["research_stale"] = await rc.measure(rc.ReadOnlyPool(pool, rc.CHECK_STATEMENT_TIMEOUT))


async def _tour_retirement_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1039 R6 (root ADR 0019 d20): the fan-out sweep (retired / restored events of the last 2 h without a job), the
    WordPress pages whose notice period ended (one due-now job each) and the daily tenant e-mail digest (once a UTC
    day from 08:00). Only while the three kinds are registered."""
    from services.jobs.tour_retirement_jobs import DRAFT_KIND, FANOUT_KIND, MAIL_KIND, tick

    if not {FANOUT_KIND, DRAFT_KIND, MAIL_KIND} <= set(kinds):
        return
    counts = await tick(pool, now=ts)
    if counts:
        out["tour_retirement"] = counts


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
    # AA-1003 R0 hotfix: the TP-03 hourly inquiry handoff check is retired (services/reports/handoff.py).


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


async def _llm_model_lifecycle_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1024 D2: the nightly model lifecycle sync, once per UTC day from its hour (idempotency key per day; one
    indexed EXISTS a tick after that hour). Only while the kind is registered; LLM_MODEL_LIFECYCLE_SYNC=off stops it."""
    from services.jobs.llm_model_lifecycle_job import KIND, enqueue_nightly, schedule_enabled

    if KIND not in kinds or not schedule_enabled():
        return
    job_id, created, day = await enqueue_nightly(pool, now=ts)
    if created:
        out["llm_model_lifecycle_sync"] = {"job_id": job_id, "day": day.isoformat()}


async def _llm_route_watch_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1042: the route watch once per 15-min UTC bucket and the route digest once per UTC day from its hour
    (idempotency keys; one indexed EXISTS each a tick after the run exists). Only while the kind is registered;
    LLM_ROUTE_WATCH=off stops both."""
    from services.jobs.llm_route_watch_job import KIND, enqueue_digest, enqueue_watch, schedule_enabled

    if KIND not in kinds or not schedule_enabled():
        return
    job_id, created, key = await enqueue_watch(pool, now=ts)
    if created:
        out["llm_route_watch"] = {"job_id": job_id, "key": key}
    job_id, created, key = await enqueue_digest(pool, now=ts)
    if created:
        out["llm_route_digest"] = {"job_id": job_id, "key": key}


async def _tenant_account_tick_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1028 R1 slice 3: the hourly tenant lifecycle tick (trial end, expired overrides), once per UTC hour
    (idempotency key per hour; one indexed EXISTS a tick after the first). Only while the kind is registered;
    TENANT_ACCOUNT_TICK=off stops it."""
    from services.jobs.tenant_account_tick_job import KIND, enqueue_hourly, schedule_enabled

    if KIND not in kinds or not schedule_enabled():
        return
    job_id, created, key = await enqueue_hourly(pool, now=ts)
    if created:
        out["tenant_account_tick"] = {"job_id": job_id, "key": key}


async def _tenant_backup_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1093 part 3 (plan D11): the nightly backup of the pilot tenant, once per UTC day from its hour (idempotency
    key per day; one indexed EXISTS a tick after the first). Only while the kind is registered and
    TENANT_BACKUP_PILOT=on (default off)."""
    from services.jobs.tenant_backup_job import KIND, enqueue_pilot_nightly, schedule_enabled

    if KIND not in kinds or not schedule_enabled():
        return
    job_id, created, key = await enqueue_pilot_nightly(pool, now=ts)
    if created:
        out["tenant_backup"] = {"job_id": job_id, "key": key}


async def _tenant_erasure_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1093 lane L2: one live saga job per open erasure request with work to do (a handful of rows; nothing before
    CIS 408). Only while the kind is registered."""
    from services.jobs.tenant_erasure_saga_job import KIND, ensure_jobs

    if KIND not in kinds:
        return
    created = await ensure_jobs(pool, now=ts)
    if created:
        out["tenant_erasure"] = {"jobs": created}


async def _tenant_billing_tick_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1037 R4: the hourly billing tick (Booking's billing status -> the account state), once per UTC hour
    (idempotency key per hour). Only while the kind is registered; TENANT_BILLING_TICK=off stops it."""
    from services.jobs.tenant_billing_tick_job import KIND, enqueue_hourly, schedule_enabled

    if KIND not in kinds or not schedule_enabled():
        return
    job_id, created, key = await enqueue_hourly(pool, now=ts)
    if created:
        out["tenant_billing_tick"] = {"job_id": job_id, "key": key}


async def _ai_usage_rollup_part(pool, kinds: dict, ts: datetime, out: dict) -> None:
    """AA-1045: the usage rollup every quarter hour and its one-time backfill (idempotency keys; one to_regclass and at
    most two indexed EXISTS a tick). Only while the kind is registered; AI_USAGE_ROLLUP=off stops it."""
    from services.jobs.ai_usage_rollup_job import KIND, enqueue_scheduled, schedule_enabled

    if KIND not in kinds or not schedule_enabled():
        return
    created = await enqueue_scheduled(pool, now=ts)
    if created:
        out["ai_usage_rollup"] = created


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
                      part_timeout_s: float = PART_TIMEOUT_S, redis: Any = None,
                      recompute_sweep: bool = False, research_stale: bool = False) -> dict:
    """One tick: the role connections and XID age, the alert-recipient reconcile (AA-913), the retired-kind sweep,
    the TripPlanner purge sweep (AA-987), the Booking supply feed pull (AA-835), the daily writing-memory fact filter
    (AA-917), the shared.job backlog, then the S263 ops checks, each part on its own and under part_timeout_s (one
    failing or hanging never blocks the others). `client` is the SNS client of the alerts topic (None: made on the
    first alert). Returns the fields of the `ops_metrics_sampled` line. Never raises (except CancelledError)."""
    ts = datetime.now(timezone.utc)
    out: dict[str, Any] = {"sampled_at": ts.isoformat()}

    try:
        await asyncio.wait_for(_rds_part(pool, out), timeout=part_timeout_s)
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

    # AA-1039 R6: three idempotent INSERT ... SELECT a tick (indexed), the short budget of the AA-920 check
    try:
        await asyncio.wait_for(_tour_retirement_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["tour_retirement_error"] = _err(e)

    # AA-1056: one small aggregate a tick, plus the sweep (a few reads and one enqueue) every 2 h; own short budget
    try:
        await asyncio.wait_for(_recompute_lifecycle_part(pool, kinds, out, recompute_sweep),
                               timeout=min(10.0, part_timeout_s))
    except Exception as e:
        out["recompute_lifecycle_error"] = _err(e)

    # AA-1055: once a day (the loop's schedule), a few bounded reads; before the ops checks, which read its field
    if research_stale:
        from services.acp_contract.research_coverage import CHECK_TIMEOUT_S

        try:
            await asyncio.wait_for(_research_stale_part(pool, out), timeout=min(CHECK_TIMEOUT_S, part_timeout_s))
        except Exception as e:
            out["research_stale_error"] = _err(e)

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

    # AA-1024 D2: one idempotent INSERT a day (free control-plane reads), same short budget
    try:
        await asyncio.wait_for(_llm_model_lifecycle_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["llm_model_lifecycle_sync_error"] = _err(e)

    # AA-1042: at most two idempotent INSERTs a tick (database only), same short budget
    try:
        await asyncio.wait_for(_llm_route_watch_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["llm_route_watch_error"] = _err(e)

    # AA-1028 R1 slice 3: one idempotent INSERT an hour (database only), same short budget
    try:
        await asyncio.wait_for(_tenant_account_tick_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["tenant_account_tick_error"] = _err(e)

    # AA-1093 part 3: at most one idempotent INSERT a day, only with TENANT_BACKUP_PILOT=on, same short budget
    try:
        await asyncio.wait_for(_tenant_backup_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["tenant_backup_error"] = _err(e)

    # AA-1093 L2: one SELECT of the open erasure requests, an INSERT only when one has no live saga job
    try:
        await asyncio.wait_for(_tenant_erasure_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["tenant_erasure_error"] = _err(e)

    # AA-1037 R4: one idempotent INSERT an hour (database only), same short budget
    try:
        await asyncio.wait_for(_tenant_billing_tick_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["tenant_billing_tick_error"] = _err(e)

    # AA-1045: at most two idempotent INSERTs a quarter hour (database only), same short budget
    try:
        await asyncio.wait_for(_ai_usage_rollup_part(pool, kinds, ts, out), timeout=5.0)
    except Exception as e:
        out["ai_usage_rollup_error"] = _err(e)

    try:
        await asyncio.wait_for(_jobs_part(pool, kinds, out), timeout=part_timeout_s)
    except Exception as e:
        out["jobs_error"] = _err(e)

    # S263: after the backlog, so the job checks see this tick's sample (a failed part simply checks nothing)
    try:
        await asyncio.wait_for(_ops_checks_part(pool, client, out), timeout=part_timeout_s)
    except Exception as e:
        out["ops_checks_error"] = _err(e)
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


async def provider_credit_tick(pool, part_timeout_s: float = PART_TIMEOUT_S) -> Optional[list]:
    """AA-1025 B5: one low-credit check of the vendor API connections (shared/provider_credit.check_low_credit), on
    the Jev check's schedule. Never raises (except CancelledError); a failure or a hang (or migration 342 not applied)
    is one `provider_credit_check_error` warning and the next check retries."""
    from shared import provider_credit

    try:
        return await asyncio.wait_for(provider_credit.check_low_credit(pool, source="ops_sampler"),
                                      timeout=part_timeout_s)
    except Exception as e:   # asyncio.TimeoutError included
        logger.warning("provider_credit_check_error", error=_err(e))
        return None


def _log(out: dict) -> None:
    if any(k in out for k in ("rds_error", "retired_error", "jobs_error", "alert_recipients_error",
                              "tripplanner_purge_error", "recompute_lifecycle_error", "recompute_sweep",
                              "research_stale_error",
                              "supply_pull_error", "writing_memory_error",
                              "writing_memory_kpi_weekly_error",
                              "reports_error", "ops_checks_error")) or out.get("ops_checks"):
        logger.warning("ops_metrics_sampled", **out)
    else:
        logger.info("ops_metrics_sampled", **out)


async def run_ops_metrics_sampler(app, interval_s: float = OPS_METRICS_INTERVAL_S,
                                  first_delay_s: float = OPS_METRICS_FIRST_DELAY_S,
                                  client_factory: Callable[[], Any] = alerts_client,
                                  part_timeout_s: float = PART_TIMEOUT_S,
                                  jev_check_every_s: Optional[float] = None,
                                  recompute_sweep_every_s: Optional[float] = None,
                                  research_stale_every_s: Optional[float] = None) -> None:
    """Loops until cancelled. Reads app.state.pool on every tick, so it never holds a stale pool; the
    SNS client of the ops-check alerts is made once (in a thread), and again on the next tick if making it failed.
    AA-1056: every `recompute_sweep_every_s` (services/recompute/lifecycle.SWEEP_EVERY_S, 2 h; the first on the first
    tick with a pool) the tick also sweeps the tour on/off events nothing recomputed after.
    AA-1055: every `research_stale_every_s` (services/acp_contract/research_coverage.CHECK_EVERY_S, a day; the first on
    the first tick with a pool) the tick also measures the research stale share (ops check research_stale_share)."""
    if jev_check_every_s is None:
        from shared.jev_credit import JEV_CREDIT_CHECK_EVERY_S

        jev_check_every_s = JEV_CREDIT_CHECK_EVERY_S
    if recompute_sweep_every_s is None:
        from services.recompute.lifecycle import SWEEP_EVERY_S

        recompute_sweep_every_s = SWEEP_EVERY_S
    if research_stale_every_s is None:
        from services.acp_contract.research_coverage import CHECK_EVERY_S

        research_stale_every_s = CHECK_EVERY_S
    client = None
    next_jev_check = 0.0          # time.monotonic() deadline of the next AA-845 Jev credit check
    next_recompute_sweep = 0.0    # time.monotonic() deadline of the next AA-1056 lifecycle recompute sweep
    next_research_stale = 0.0     # time.monotonic() deadline of the next AA-1055 research stale-share measure
    await asyncio.sleep(first_delay_s)
    while True:
        try:
            pool = getattr(app.state, "pool", None)
            if pool is not None:
                if client is None:
                    client = await asyncio.to_thread(client_factory)
                sweep_due = time.monotonic() >= next_recompute_sweep
                if sweep_due:
                    next_recompute_sweep = time.monotonic() + recompute_sweep_every_s
                research_due = time.monotonic() >= next_research_stale
                if research_due:
                    next_research_stale = time.monotonic() + research_stale_every_s
                _log(await sample_once(pool, client, part_timeout_s=part_timeout_s,
                                       redis=getattr(app.state, "redis", None), recompute_sweep=sweep_due,
                                       research_stale=research_due))
        except Exception as e:  # never kill the loop (CancelledError is not an Exception)
            logger.warning("ops_metrics_sampled", error=_err(e))
        pool = getattr(app.state, "pool", None)
        if pool is not None and time.monotonic() >= next_jev_check:
            next_jev_check = time.monotonic() + jev_check_every_s
            await jev_credit_tick(pool, part_timeout_s)
            await provider_credit_tick(pool, part_timeout_s)       # AA-1025 B5, same schedule
        await asyncio.sleep(interval_s)


def start_ops_metrics_sampler(app, interval_s: float = OPS_METRICS_INTERVAL_S) -> Optional[asyncio.Task]:
    """None (nothing started) when ENVIRONMENT == "test"."""
    if not sampler_enabled():
        return None
    return asyncio.create_task(run_ops_metrics_sampler(app, interval_s), name="aa751-ops-metrics-sampler")


async def stop_ops_metrics_sampler(task: Optional[asyncio.Task], pool: Any = None,
                                   flush_timeout_s: float = OPS_FLUSH_ON_STOP_TIMEOUT_S) -> None:
    """Cancel and wait; swallows the CancelledError (and anything else) so shutdown goes on.

    S263 review: then, with the main pool, one last flush of the ops log events this process counted since the last
    tick (a lost cost-log write, a tenant-pool fail-closed state): without it a deploy or scale-in within 5 minutes of
    the event loses its alert. A flush cancelled mid-tick put its events back (shared/ops_checks.flush_events), so
    they go out here. Bounded by flush_timeout_s; never raises."""
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    if pool is None:
        return
    from shared import ops_checks

    try:
        results = await asyncio.wait_for(ops_checks.flush_events(pool), timeout=flush_timeout_s)
        if results:
            logger.warning("ops_checks_flushed_on_stop", ops_checks=results)
    except Exception as e:  # noqa: BLE001 — asyncio.TimeoutError included; shutdown goes on
        logger.warning("ops_checks_flush_on_stop_failed", error=_err(e))
