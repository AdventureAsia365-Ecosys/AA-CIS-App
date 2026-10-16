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
- api only (not the worker service); started from api/main.py's lifespan after the main pool,
  cancelled before the pool closes. Off when ENVIRONMENT == "test". Never raises: a failing part is
  logged in the tick's line (`*_error`) and the loop goes on. With two api tasks (rolling deploy)
  both publish; the alarms use Maximum / SampleCount, so a duplicate datapoint changes nothing.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

OPS_METRICS_INTERVAL_S = 300.0
OPS_METRICS_FIRST_DELAY_S = 30.0   # let the app finish starting before the first sample
PART_TIMEOUT_S = 60.0              # bound on each part of a tick (3 parts -> < 300 s interval)

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


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:300]}"


async def sample_once(pool, client, kinds: Optional[dict] = None,
                      part_timeout_s: float = PART_TIMEOUT_S) -> dict:
    """One tick: AA/RDS, the retired-kind sweep, then AA/Jobs/CIS, each part on its own and under
    part_timeout_s (one failing or hanging never blocks the others). Returns the fields of the
    `ops_metrics_sampled` line. Never raises (except CancelledError)."""
    ts = datetime.now(timezone.utc)
    out: dict[str, Any] = {"sampled_at": ts.isoformat()}

    try:
        await asyncio.wait_for(_rds_part(pool, client, ts, out), timeout=part_timeout_s)
    except Exception as e:   # asyncio.TimeoutError included
        out["rds_error"] = _err(e)

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
        await asyncio.wait_for(_jobs_part(pool, client, kinds, ts, out), timeout=part_timeout_s)
    except Exception as e:
        out["jobs_error"] = _err(e)
    return out


def _log(out: dict) -> None:
    if any(k in out for k in ("rds_error", "retired_error", "jobs_error")):
        logger.warning("ops_metrics_sampled", **out)
    else:
        logger.info("ops_metrics_sampled", **out)


async def run_ops_metrics_sampler(app, interval_s: float = OPS_METRICS_INTERVAL_S,
                                  first_delay_s: float = OPS_METRICS_FIRST_DELAY_S,
                                  client_factory: Callable[[], Any] = cloudwatch_client,
                                  part_timeout_s: float = PART_TIMEOUT_S) -> None:
    """Loops until cancelled. Reads app.state.pool on every tick, so it never holds a stale pool; the
    CloudWatch client is made once (in a thread), and again on the next tick if making it failed."""
    client = None
    await asyncio.sleep(first_delay_s)
    while True:
        try:
            pool = getattr(app.state, "pool", None)
            if pool is not None:
                if client is None:
                    client = await asyncio.to_thread(client_factory)
                _log(await sample_once(pool, client, part_timeout_s=part_timeout_s))
        except Exception as e:  # never kill the loop (CancelledError is not an Exception)
            logger.warning("ops_metrics_sampled", error=_err(e))
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
