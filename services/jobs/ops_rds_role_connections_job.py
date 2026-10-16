"""AA-832 — job kind `ops_rds_role_connections`: connections per LOGIN role vs its CONNECTION LIMIT.

RDS publishes only the instance-wide DatabaseConnections. The shared RDS gives each app its own LOGIN
role with a CONNECTION LIMIT (tripplanner 30, booking 15, aa_app_user 15 on 16/10/2026); a role that
hits its limit gets "too many connections for role" while the instance looks healthy. This kind
counts the sessions of every LOGIN role that has a limit and publishes them to CloudWatch, where
AA-CIS-Infra alarms on `RoleConnectionLimitPct` per role (observability_ecosys.tf).

  namespace AA/RDS, dimension Role=<rolname>, per role:
    RoleConnections         Count    sessions of the role in pg_stat_activity
    RoleConnectionLimitPct  Percent  100 * RoleConnections / rolconnlimit

- Roles are discovered on every run (`rolcanlogin AND rolconnlimit > 0`), so a new app role or a
  changed limit needs no code change. `usename` is the session user, the one CONNECTION LIMIT counts.
  The worker runs as aa_cis_admin, which sees other roles' rows (verified live 16/10/2026).
- One PutMetricData call per run (boto3 in a thread). A failure is logged and raised, never swallowed:
  the runner retries once, then the job is `failed` on the Jobs page.
- Enqueued every 5 minutes by `POST /admin/job-runner/rds-role-connections/schedule` (ops-probe Lambda,
  EventBridge Scheduler), at most one queued/running job at a time (`schedule`).
- Not background: a background kind waits behind any running background job (BACKGROUND_MAX_RUNNING),
  so behind a place batch or a retention run the samples would stop for hours, while the alarm treats
  missing data as OK. Exempt from the pre-deploy guard (`deploy_gate_exempt`): it reads only, and a
  deploy that interrupts it costs one sample.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from shared.jobs.registry import JobContext, enqueue, job_kind

logger = structlog.get_logger()

KIND = "ops_rds_role_connections"
NAMESPACE = "AA/RDS"
METRIC_CONNECTIONS = "RoleConnections"
METRIC_LIMIT_PCT = "RoleConnectionLimitPct"
CREATED_BY = "scheduler:aa-832"

ROLE_CONNECTIONS_SQL = """
    SELECT r.rolname, r.rolconnlimit, count(a.pid)::int AS connections
      FROM pg_roles r
      LEFT JOIN pg_stat_activity a ON a.usename = r.rolname
     WHERE r.rolcanlogin AND r.rolconnlimit > 0
     GROUP BY r.rolname, r.rolconnlimit
     ORDER BY r.rolname
"""

_PENDING_SQL = """
    SELECT id FROM shared.job
     WHERE kind = $1 AND status IN ('queued', 'running')
     ORDER BY created_at
     LIMIT 1
"""


def cloudwatch_region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"


def cloudwatch_client(region: str):
    import boto3  # lazy: only the worker path that publishes needs it

    return boto3.client("cloudwatch", region_name=region)


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
    """The PutMetricData `MetricData` list: two datums per role. Pure, unit-tested."""
    data = []
    for s in samples:
        dims = [{"Name": "Role", "Value": s["role"]}]
        data.append({"MetricName": METRIC_CONNECTIONS, "Dimensions": dims, "Timestamp": timestamp,
                     "Value": float(s["connections"]), "Unit": "Count"})
        data.append({"MetricName": METRIC_LIMIT_PCT, "Dimensions": dims, "Timestamp": timestamp,
                     "Value": float(s["pct"]), "Unit": "Percent"})
    return data


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=60, deploy_gate_exempt=True)
async def run(ctx: JobContext) -> dict:
    sampled_at = datetime.now(timezone.utc)
    samples = role_samples(await ctx.pool.fetch(ROLE_CONNECTIONS_SQL))
    by_role = {s["role"]: {"connections": s["connections"], "limit": s["limit"], "pct": s["pct"]}
               for s in samples}
    if not samples:
        # Not an error (no role has a limit), but every per-role alarm then sees missing data.
        logger.warning("ops_rds_role_connections_no_limited_roles", job_id=ctx.job_id)
        return {"sampled_at": sampled_at.isoformat(), "roles": {}, "published": 0}
    data = metric_data(samples, sampled_at)
    client = ctx.resources.get("cloudwatch") or cloudwatch_client(cloudwatch_region())
    try:
        await asyncio.to_thread(client.put_metric_data, Namespace=NAMESPACE, MetricData=data)
    except Exception as e:
        logger.warning("ops_rds_role_connections_put_failed", job_id=ctx.job_id, error=str(e)[:300])
        raise
    logger.info("ops_rds_role_connections_sampled", job_id=ctx.job_id, roles=by_role)
    return {"sampled_at": sampled_at.isoformat(), "roles": by_role, "published": len(data),
            "namespace": NAMESPACE}


async def schedule(pool, *, created_by: str = CREATED_BY) -> dict[str, Any]:
    """Called every 5 minutes by the ops scheduler. Enqueues one job unless one is still queued or
    running. Two racing calls can both enqueue; both run (concurrency 1) and publish a datapoint a
    few seconds apart, which the alarm's Maximum statistic absorbs."""
    pending: Optional[Any] = await pool.fetchval(_PENDING_SQL, KIND)
    if pending is not None:
        return {"enqueued": False, "reason": "already_pending", "job_id": str(pending)}
    job_id, _ = await enqueue(pool, KIND, {"trigger": "schedule"}, created_by=created_by)
    return {"enqueued": True, "job_id": job_id}
