"""AA-900 (S271 W0, root ADR 0020 "Measurement") — job kind ``quality_funnel_watch``: the nightly quality watchdog.

Payload: {"run_id": <shared.report_run id of a CIS-08 run>, "dry_run": bool}.

Schedule: no clock of its own. The daily report chain renders CIS-08 for the closed ICT day (kpi_snapshot_daily ->
report_generate, from 00:30 UTC); when that scheduled run succeeds, ``report_jobs.run_report`` queues this kind for
the run (idempotency key ``quality_funnel_watch:<run_id>``). It reads the findings the run stored
(``output.watch.findings``, services/quality/funnel.watch_findings: cut rate, fallback share and served-model drift
per stage over the trailing 7 days) and raises only the NEW ones (not ``repeat``: the previous scheduled run did not
have them, ``funnel.mark_repeats``) as ONE ``qf_digest`` alert (``funnel.digest_finding``) through
``shared/ops_checks.alert_all`` (admin bell at most once per 20 h, one e-mail per UTC day). So the bell says exactly
what the stored snapshot says, once per transition, and an outage over several stages is one alert.

A failed e-mail (or a database error) raises a retryable error: ``raise_alert`` already removed the bell it wrote, so
the runner's retry bells and mails again; a retry after a delivered alert is a no-op (the bell is deduplicated).
A dry run returns the findings and raises nothing. $0 (database reads, a bell, at most one SNS e-mail).
max_attempts 3, background, deploy-gate exempt.
"""
from __future__ import annotations

import json
from typing import Optional

import structlog

from services.quality import funnel
from shared import ops_checks
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "quality_funnel_watch"
SCHEDULER = "scheduler:aa-900"
WATCHED_REPORTS = frozenset({"CIS-08"})

RUN_SQL = "SELECT report_id, status, period_end, output FROM shared.report_run WHERE id = $1::uuid"


class AlertNotDelivered(Exception):
    """The digest's bell or e-mail failed: retryable (the runner retries the job)."""


def findings_of(output) -> list[dict]:
    """The stored findings of a CIS-08 output (a JSON string or dict), each {check, summary, details, repeat}; an
    unknown check name (a renamed prefix) is dropped rather than failing the others."""
    out = json.loads(output) if isinstance(output, str) else (output or {})
    found = []
    for f in ((out.get("watch") or {}).get("findings") or []):
        try:
            ops_checks.check_for(f["check"])
        except KeyError:
            logger.warning("quality_funnel_watch_unknown_check", check=f.get("check"))
            continue
        found.append({"check": f["check"], "summary": f.get("summary") or "", "details": f.get("details") or {},
                      "repeat": bool(f.get("repeat"))})
    return found


async def enqueue_for_run(pool, run_id: str, *, parent_job_id: Optional[str] = None) -> dict:
    job_id, created = await enqueue(pool, KIND, {"run_id": run_id}, idempotency_key=f"{KIND}:{run_id}",
                                    created_by=SCHEDULER, parent_job_id=parent_job_id)
    return {"job_id": job_id, "created": created}


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=60, background=True, deploy_gate_exempt=True)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    run_id = payload.get("run_id")
    if not run_id:
        raise NonRetryable("payload has no run_id")
    dry_run = bool(payload.get("dry_run", False))
    row = await ctx.pool.fetchrow(RUN_SQL, run_id)
    if row is None:
        raise NonRetryable(f"report run {run_id} not found")
    if row["report_id"] not in WATCHED_REPORTS:
        raise NonRetryable(f"report {row['report_id']} has no watchdog")
    if row["status"] != "succeeded":
        raise NonRetryable(f"report run {run_id} is {row['status']}, not succeeded")
    findings = findings_of(row["output"])
    day = row["period_end"].isoformat()
    digest = funnel.digest_finding(findings, day=day)
    alerts = {} if dry_run or digest is None else await ops_checks.alert_all(ctx.pool, [digest])
    result = {"kind": KIND, "run_id": run_id, "day": day, "dry_run": dry_run,
              "findings": [{"check": f["check"], "summary": f["summary"], "repeat": f["repeat"]} for f in findings],
              "raised": digest.details["new"] if digest is not None else [],
              "digest": digest.summary if digest is not None else None, "alerts": alerts}
    logger.info("quality_funnel_watch_done", job_id=ctx.job_id, run_id=run_id, dry_run=dry_run,
                findings=[f["check"] for f in findings], raised=result["raised"], alerts=alerts)
    failed = {c: r for c, r in alerts.items() if "error" in r or r.get("email") == "failed"}
    if failed:
        raise AlertNotDelivered(f"alert not delivered, the runner retries: {json.dumps(failed, default=str)[:500]}")
    result = json.loads(json.dumps(result, default=str))
    ctx.set_result(result)
    return result
