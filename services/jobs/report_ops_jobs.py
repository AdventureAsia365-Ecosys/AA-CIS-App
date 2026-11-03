"""AA-1003 R2 — the daily safety net's job kinds (root AA-1001 catalog: OPS-12; TP-03 retired by the R0 hotfix).

  inquiry_handoff_check  RETIRED (R0 hotfix, root ADR 0019 B2B-only model; services/reports/handoff.py). Nothing
                         queues it any more (the ops metrics sampler stopped); the kind stays registered as a no-op so
                         a check queued by an api task of the previous release finishes ({"status": "retired"})
                         instead of staying queued. It reads no view, raises no bell and sends no e-mail.
  report_email           payload {"run_id", "report_id"}
                         Sends a succeeded report run (its Markdown) through the existing SES path
                         (shared/aws_client/ses_mailer.py) to that report's opt-in list only, never to the alert
                         recipients: OPS-12 and (R3) FI-02 -> OPS_DIGEST_EMAIL_TO (comma-separated, set on the
                         worker task definition
                         by AA-CIS-Infra; empty = nobody, the job succeeds as skipped). Queued by report_generate after
                         a scheduled OPS-12 / FI-02 run succeeded (idempotency key report_email:<run id>).

Both are background kinds and cost $0 (view reads; SES within the existing sender).
"""
from __future__ import annotations

import html
import json
import os
import re
from typing import Optional

import structlog

from services.reports import handoff, store
from shared import alert_recipients, ops_alerts
from shared.aws_client import ses_mailer
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

CHECK_KIND = handoff.CHECK_KIND
EMAIL_KIND = "report_email"
SCHEDULER = "scheduler:aa-1003"
# Report -> the environment variable that lists its opt-in recipients (tech lead only today).
# R3: FI-02 (weekly spend digest) goes to the same list as OPS-12 (catalog decision 5: the tech lead gets both); give
# it its own variable when someone opts in to one of them only.
EMAILED_REPORTS: dict[str, str] = {"OPS-12": "OPS_DIGEST_EMAIL_TO", "FI-02": "OPS_DIGEST_EMAIL_TO"}
MAX_REPORT_RECIPIENTS = 10
_SPLIT = re.compile(r"[,;\s]+")


# ── TP-03 hourly check (retired) ──────────────────────────────────────────────────────────────────────────────


@job_kind(CHECK_KIND, concurrency=1, max_attempts=1, expected_seconds=5, background=True)
async def run_handoff_check(ctx: JobContext) -> dict:
    """No-op since the R0 hotfix: TP-03 is retired (the B2C inquiry hand-off is off)."""
    result = {"kind": CHECK_KIND, "status": "retired", "hour": (ctx.payload or {}).get("hour"),
              "reason": "TP-03 retired: the TripPlanner to Booking inquiry hand-off is off (B2B-only model)"}
    ctx.set_result(result)
    return result


# ── report e-mail ───────────────────────────────────────────────────────────────────────────────────────────────


def report_recipients(report_id: str, environ: Optional[dict] = None) -> tuple[list[str], list[str]]:
    """(valid addresses, refused entries) of the report's opt-in variable; [] when the report is not e-mailed."""
    env = os.environ if environ is None else environ
    var = EMAILED_REPORTS.get(report_id)
    raw = (env.get(var) or "") if var else ""
    valid: list[str] = []
    refused: list[str] = []
    for part in (p for p in _SPLIT.split(raw) if p):
        try:
            addr = alert_recipients.normalize_email(part)
        except ValueError:
            refused.append(part[:80])
            continue
        if addr not in valid:
            valid.append(addr)
    return valid[:MAX_REPORT_RECIPIENTS], refused


def _json(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def render_report_email(run: dict, *, to: tuple[str, ...], base_url: Optional[str] = None,
                        environment: Optional[str] = None) -> ses_mailer.Message:
    """Plain text = the run's Markdown; HTML = the same text pre-formatted (no Markdown renderer in the worker)."""
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    output = _json(run.get("output"))
    headline = output.get("headline") or run.get("summary") or run["report_id"]
    subject = f"[AA-CIS {env}] {headline}"[:200]
    link = f"{base_url or ops_alerts.admin_base_url()}{ops_alerts.REPORTS_PAGE_PATH}"
    markdown = run.get("output_markdown") or run.get("summary") or ""
    footer = (f"Report {run['report_id']}, run {run['id']} (Platform › Reports: {link}). You get this e-mail because "
              f"your address is on the report's opt-in list; ask the tech lead to remove it.")
    text = f"{markdown}\n\n--\n{footer}\n"
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            f"<pre style=\"font-family:Menlo,Consolas,monospace;font-size:12px;white-space:pre-wrap\">"
            f"{html.escape(markdown)}</pre>"
            f"<p style=\"color:#777;font-size:12px\">{html.escape(footer)}</p>"
            f"<p><a href=\"{html.escape(link)}\">Open Platform › Reports</a></p></div>")
    return ses_mailer.Message(to=to, subject=subject, text=text, html=body)


async def enqueue_report_email(pool, run_id: str, report_id: str, *,
                               parent_job_id: Optional[str] = None) -> dict:
    job_id, created = await enqueue(pool, EMAIL_KIND, {"run_id": run_id, "report_id": report_id},
                                    idempotency_key=f"{EMAIL_KIND}:{run_id}", max_attempts=5,
                                    created_by=SCHEDULER, parent_job_id=parent_job_id)
    return {"job_id": job_id, "created": created}


@job_kind(EMAIL_KIND, concurrency=1, max_attempts=5, expected_seconds=60, background=True)
async def run_report_email(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    run_id, report_id = payload.get("run_id"), payload.get("report_id")
    if not run_id or report_id not in EMAILED_REPORTS:
        raise NonRetryable(f"report {report_id!r} is not e-mailed (or no run_id)")
    run = await store.get_run(ctx.pool, run_id)
    if run is None or run["status"] != "succeeded" or run["report_id"] != report_id:
        raise NonRetryable(f"report run {run_id} is not a succeeded {report_id} run")
    cfg = ses_mailer.sender_config()
    if cfg is None:
        return {"status": "skipped", "reason": "SES_FROM_ADDRESS unset", "run_id": run_id}
    to, refused = report_recipients(report_id)
    notes = {"refused_entries": len(refused)} if refused else {}
    if not to:
        reason = f"no opt-in recipient ({EMAILED_REPORTS[report_id]} empty)"
        logger.info("report_email_skipped", run_id=run_id, report_id=report_id, reason=reason)
        return {"status": "skipped", "reason": reason, "run_id": run_id, **notes}
    message = render_report_email(run, to=tuple(to))
    client = ctx.resources.get("ses") or ses_mailer.ses_client(cfg.region)
    try:
        message_id = await ses_mailer.send(client, message, sender=cfg.sender,
                                           configuration_set=cfg.configuration_set)
    except ses_mailer.MailError as e:
        logger.warning("report_email_ses_error", run_id=run_id, code=e.code, permanent=e.permanent)
        if e.permanent:
            raise NonRetryable(str(e)) from e
        raise
    logger.info("report_email_sent", run_id=run_id, report_id=report_id, recipients=len(to), message_id=message_id)
    return {"status": "sent", "run_id": run_id, "report_id": report_id, "message_id": message_id,
            "recipients_count": len(to), "subject": message.subject, **notes}
