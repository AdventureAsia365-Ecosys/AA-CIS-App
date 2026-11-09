"""AA-1042 (S268, AI gateway route control center: Alerts) — job kind ``llm_route_watch``: the route alerts and the
daily route digest, from the ledger the gateway already writes ($0: database reads, then bells / e-mails).

Payload: {"trigger": "schedule" | "manual", "mode": "watch" | "digest" (default watch), "dry_run": bool,
"requested_by": "admin:<id>"}.

What it does (shared/llm_client/route_watch.py has the rules): ``watch`` reads the last 60 min (and 24 h for the cost
shift) of ``shared.llm_call_log`` per stage and raises, through ``shared/ops_checks.raise_alert`` (admin bell + at
most one e-mail per check and UTC day), the fallback-rate, last-model, primary-back and cost-shift alerts of each
stage; ``digest`` builds the summary of the last 24 h and raises the ``route_digest`` alert when a stage used a
fallback model or lost requests. A dry run evaluates and returns the findings, raises nothing.

Schedule: queued by the api's ops metrics sampler (no cron in the api): one ``watch`` run per ``BUCKET_MINUTES``
UTC bucket (idempotency key ``llm_route_watch:<bucket start>``) and one ``digest`` run per UTC day from
``DIGEST_HOUR_UTC`` (``llm_route_digest:<day>``). Switch ``LLM_ROUTE_WATCH`` (ECS env, default ON: the run costs
nothing). max_attempts 2, background, deploy-gate exempt (a re-run is harmless: the bells are deduplicated).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Optional

import structlog

from shared import ops_checks
from shared.jobs.registry import JobContext, enqueue, job_kind
from shared.llm_client import route_watch

logger = structlog.get_logger()

KIND = "llm_route_watch"
SCHEDULER = "scheduler:aa-1042"
SWITCH_ENV = "LLM_ROUTE_WATCH"
BUCKET_MINUTES = 15
DIGEST_HOUR_UTC = 1
MODES = ("watch", "digest")


def schedule_enabled() -> bool:
    """``LLM_ROUTE_WATCH`` (default on): off / false / 0 / no stops the schedule."""
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def finding_view(f: ops_checks.Finding) -> dict:
    return {"check": f.check, "summary": f.summary, "details": f.details}


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=60, background=True, deploy_gate_exempt=True)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    mode = payload.get("mode") if payload.get("mode") in MODES else "watch"
    dry_run = bool(payload.get("dry_run", False))
    report = await route_watch.evaluate(ctx.pool, mode=mode)
    findings = report.pop("findings")
    alerts = {} if dry_run or not findings else await ops_checks.alert_all(ctx.pool, findings)
    result = {"kind": KIND, "trigger": payload.get("trigger", "manual"), "mode": mode, "dry_run": dry_run,
              **report, "findings": [finding_view(f) for f in findings], "alerts": alerts}
    logger.info("llm_route_watch_done", job_id=ctx.job_id, mode=mode, dry_run=dry_run,
                findings=[f.check for f in findings], alerts=alerts)
    result = json.loads(json.dumps(result, default=str))
    ctx.set_result(result)
    return result


def bucket_start(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return now.replace(minute=now.minute - now.minute % BUCKET_MINUTES, second=0, microsecond=0)


def watch_key(now: datetime) -> str:
    return f"{KIND}:{bucket_start(now).strftime('%Y-%m-%dT%H:%M')}"


def digest_key(day: date) -> str:
    return f"llm_route_digest:{day.isoformat()}"


_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


async def enqueue_watch(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, str]:
    """This bucket's watch run (idempotent per bucket). Called by the ops metrics sampler on every tick: one indexed
    EXISTS once the bucket's run exists."""
    now = now or datetime.now(timezone.utc)
    key = watch_key(now)
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, key
    job_id, created = await enqueue(pool, KIND, {"trigger": "schedule", "mode": "watch"}, idempotency_key=key,
                                    created_by=SCHEDULER)
    return job_id, created, key


async def enqueue_digest(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, Optional[str]]:
    """Today's (UTC) digest run, from DIGEST_HOUR_UTC on (idempotent per day). Before the hour nothing is read."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if now.hour < DIGEST_HOUR_UTC:
        return None, False, None
    key = digest_key(now.date())
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, key
    job_id, created = await enqueue(pool, KIND, {"trigger": "schedule", "mode": "digest"}, idempotency_key=key,
                                    created_by=SCHEDULER)
    return job_id, created, key


async def enqueue_manual(pool, *, mode: str, dry_run: bool, actor: str) -> tuple[str, bool]:
    """An admin's run (no idempotency key; the kind is concurrency 1)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    return await enqueue(pool, KIND, {"trigger": "manual", "mode": mode, "dry_run": dry_run,
                                      "requested_by": actor[:64]}, created_by=actor[:64])
