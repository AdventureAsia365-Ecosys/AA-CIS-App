"""AA-828 — job kind `ops_alert_email`: send one operator alert e-mail through SES.

Queued by shared.ops_alerts.enqueue_spend_budget_email when the cost guard writes a spend budget bell
row (shared.cost_guard.maybe_alert). Payload: {"template": "spend_budget", "period", "threshold",
"alert_pct", "used_pct", "message", "budget": RunBudget.summary()}. AA-913: also {"template": "test",
"requested_by"} from "Send test e-mail" on Platform › Alert recipients (api/services/alert_recipients.py).

Recipients (AA-913, shared/alert_recipients.ops_recipients): the confirmed rows of shared.alert_recipient plus
ALERT_EMAIL_TO, lower case, deduplicated; all in the To of one message (internal staff). When ALERTS_TOPIC_ARN is
set the job first reconciles the table with SNS (one ListSubscriptionsByTopic; a failure is logged and noted in the
result, never fatal), so an address that unsubscribed through an alert's link stops getting this mail too.

- SES_FROM_ADDRESS empty, or nobody to mail (no confirmed row and ALERT_EMAIL_TO empty) when the job runs ->
  succeeds as `skipped` with the reason.
- SES refusal a retry cannot fix (rejected address, sender not allowed, account paused) -> NonRetryable;
  throttling, network and 5xx errors are retried by the runner (backoff, MAX_ATTEMPTS).
- Delivery is at least once: a worker that dies after SES accepted the message but before the job is
  marked succeeded re-sends it when the reaper re-queues the job.
"""
from __future__ import annotations

from typing import Optional

import structlog

from shared import alert_recipients, ops_alerts
from shared.aws_client import ses_mailer
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

_RENDERERS = {
    ops_alerts.TEMPLATE_SPEND_BUDGET: ops_alerts.render_spend_budget,
    ops_alerts.TEMPLATE_TEST: ops_alerts.render_test,
}
WORKER_ACTOR = f"job:{ops_alerts.KIND}"


async def _reconcile(ctx: JobContext) -> Optional[str]:
    """Best effort: bring the table in line with SNS before reading it. Returns the error code, or None."""
    cfg = alert_recipients.topic_config()
    if cfg is None:
        return None
    client = ctx.resources.get("sns") or alert_recipients.sns_client(cfg.region)
    try:
        await alert_recipients.reconcile(ctx.pool, client, cfg, actor=WORKER_ACTOR)
    except alert_recipients.SnsError as exc:
        logger.warning("ops_alert_email_reconcile_failed", job_id=ctx.job_id, code=exc.code)
        return exc.code
    except Exception as exc:  # noqa: BLE001 — DB / unexpected: the mail still goes to whoever the table already lists
        logger.warning("ops_alert_email_reconcile_failed", job_id=ctx.job_id, code=type(exc).__name__)
        return type(exc).__name__
    return None


@job_kind(ops_alerts.KIND, concurrency=1, max_attempts=ops_alerts.MAX_ATTEMPTS, expected_seconds=60)
async def run(ctx: JobContext) -> dict:
    template = ctx.payload.get("template")
    render = _RENDERERS.get(template)
    if render is None:
        raise NonRetryable(f"unknown ops alert template {template!r}")
    cfg = ses_mailer.sender_config()
    if cfg is None:
        logger.info("ops_alert_email_skipped", job_id=ctx.job_id, reason="SES_FROM_ADDRESS unset")
        return {"status": "skipped", "reason": "SES_FROM_ADDRESS unset"}
    reconcile_error = await _reconcile(ctx)
    to, read_error = await alert_recipients.ops_recipients(ctx.pool)
    notes = {k: v for k, v in (("reconcile_error", reconcile_error), ("recipients_error", read_error)) if v}
    if not to:
        reason = "no confirmed recipient and ALERT_EMAIL_TO unset"
        logger.info("ops_alert_email_skipped", job_id=ctx.job_id, reason=reason)
        return {"status": "skipped", "reason": reason, **notes}
    message = render(ctx.payload, to=tuple(to))
    client = ctx.resources.get("ses") or ses_mailer.ses_client(cfg.region)
    try:
        message_id = await ses_mailer.send(client, message, sender=cfg.sender,
                                           configuration_set=cfg.configuration_set)
    except ses_mailer.MailError as e:
        logger.warning("ops_alert_email_ses_error", job_id=ctx.job_id, code=e.code, permanent=e.permanent)
        if e.permanent:
            raise NonRetryable(str(e)) from e
        raise
    logger.info("ops_alert_email_sent", job_id=ctx.job_id, template=template, message_id=message_id,
                recipients=len(to), threshold=ctx.payload.get("threshold"), period=ctx.payload.get("period"))
    return {"status": "sent", "message_id": message_id, "to": list(to), "recipients_count": len(to),
            "subject": message.subject, "configuration_set": cfg.configuration_set, **notes}
