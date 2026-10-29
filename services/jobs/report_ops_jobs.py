"""AA-1003 R2 — the daily safety net's job kinds (root AA-1001 catalog: OPS-12, TP-03).

  inquiry_handoff_check  payload {"hour": "YYYY-MM-DDTHH" (UTC)}
                         TP-03 hourly check (services/reports/handoff.py): TripPlanner inquiries sent vs received by
                         the Booking inbox over the last WINDOW_DAYS UTC days, from the two ops.overview.v1 contract
                         views (aggregates, no PII). The result is the check (window, sent, received, gap, per day);
                         a gap seen at this check AND at the previous hour's check raises the admin bell
                         platform.inquiry_handoff.gap (again only when the gap grew, within 24 h) and queues one ops
                         alert e-mail per UTC day (template inquiry_handoff_gap); INQUIRY_HANDOFF_ALERTS=off on the
                         worker switches both off (result alert.disabled). Queued by the api's ops metrics sampler once
                         per UTC hour (idempotency key inquiry_handoff_check:<hour>).
  report_email           payload {"run_id", "report_id"}
                         Sends a succeeded report run (its Markdown) through the existing SES path
                         (shared/aws_client/ses_mailer.py) to that report's opt-in list only, never to the alert
                         recipients: OPS-12 -> OPS_DIGEST_EMAIL_TO (comma-separated, set on the worker task definition
                         by AA-CIS-Infra; empty = nobody, the job succeeds as skipped). Queued by report_generate after
                         a scheduled OPS-12 run succeeded (idempotency key report_email:<run id>).

Both are background kinds and cost $0 (view reads; SES within the existing sender).
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
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
EMAILED_REPORTS: dict[str, str] = {"OPS-12": "OPS_DIGEST_EMAIL_TO"}
MAX_REPORT_RECIPIENTS = 10
_SPLIT = re.compile(r"[,;\s]+")


# ── TP-03 hourly check ──────────────────────────────────────────────────────────────────────────────────────────


def _parse_hour(raw) -> datetime:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise NonRetryable(f"hour {raw!r} is not YYYY-MM-DDTHH (UTC)")


async def _alert(pool, result: dict, hour: datetime) -> dict:
    """Bell + e-mail for a persistent gap; each part best effort and noted in the result."""
    out: dict = {}
    try:
        async with pool.acquire() as conn:
            out["bell"] = await handoff.maybe_raise_gap_bell(conn, result)
    except Exception as e:
        out["bell_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        logger.warning("inquiry_handoff_bell_failed", error=out["bell_error"])
    try:
        details = {k: result[k] for k in ("gap", "sent", "received", "parked", "failed", "window")}
        out["email_job"] = await ops_alerts.enqueue_inquiry_handoff_email(
            pool, details={**details, "checked_at": hour.isoformat()}, now=hour)
    except Exception as e:
        out["email_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        logger.warning("inquiry_handoff_email_enqueue_failed", error=out["email_error"])
    return out


@job_kind(CHECK_KIND, concurrency=1, max_attempts=2, expected_seconds=30, background=True)
async def run_handoff_check(ctx: JobContext) -> dict:
    hour = _parse_hour((ctx.payload or {}).get("hour"))
    today = hour.date()
    first = today - timedelta(days=handoff.WINDOW_DAYS - 1)
    async with ctx.pool.acquire() as conn:
        days = await handoff.read_days(conn, first, today)
        previous = await handoff.previous_result(conn, hour)
    result = {"kind": CHECK_KIND, "hour": hour.isoformat(), **handoff.evaluate(days, previous)}
    if result["persistent"]:
        if handoff.alerts_enabled():
            result["alert"] = await _alert(ctx.pool, result, hour)
        else:
            result["alert"] = {"disabled": f"{handoff.ALERTS_ENV} is off"}
    logger.info("inquiry_handoff_checked", hour=hour.isoformat(), gap=result["gap"], persistent=result["persistent"])
    ctx.set_result(result)
    return result


_CHECK_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


async def enqueue_hourly_check(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, datetime]:
    """This UTC hour's check (idempotent per hour). Called by the ops metrics sampler every tick: one indexed EXISTS
    first, so only the first tick of the hour INSERTs."""
    hour = handoff.hour_of(now)
    key = handoff.check_key(hour)
    if await pool.fetchval(_CHECK_EXISTS_SQL, key):
        return None, False, hour
    job_id, created = await enqueue(pool, CHECK_KIND, {"hour": f"{hour:%Y-%m-%dT%H}"}, idempotency_key=key,
                                    created_by=SCHEDULER)
    return job_id, created, hour


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
