"""AA-828 — job kind `ops_alert_email`: send one operator alert e-mail through SES.

Queued by shared.ops_alerts.enqueue_spend_budget_email when the cost guard writes a spend budget bell
row (shared.cost_guard.maybe_alert). Payload: {"template": "spend_budget", "period", "threshold",
"alert_pct", "used_pct", "message", "budget": RunBudget.summary()}.

- ALERT_EMAIL_TO / SES_FROM_ADDRESS empty when the job runs -> succeeds as `skipped` with the reason.
- SES refusal a retry cannot fix (rejected address, sender not allowed, account paused) -> NonRetryable;
  throttling, network and 5xx errors are retried by the runner (backoff, MAX_ATTEMPTS).
- Delivery is at least once: a worker that dies after SES accepted the message but before the job is
  marked succeeded re-sends it when the reaper re-queues the job.
"""
from __future__ import annotations

import structlog

from shared import ops_alerts
from shared.aws_client import ses_mailer
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

_RENDERERS = {ops_alerts.TEMPLATE_SPEND_BUDGET: ops_alerts.render_spend_budget}


@job_kind(ops_alerts.KIND, concurrency=1, max_attempts=ops_alerts.MAX_ATTEMPTS, expected_seconds=60)
async def run(ctx: JobContext) -> dict:
    template = ctx.payload.get("template")
    render = _RENDERERS.get(template)
    if render is None:
        raise NonRetryable(f"unknown ops alert template {template!r}")
    cfg = ses_mailer.alert_mail_config()
    if cfg is None:
        logger.info("ops_alert_email_skipped", job_id=ctx.job_id, reason="ALERT_EMAIL_TO or SES_FROM_ADDRESS unset")
        return {"status": "skipped", "reason": "ALERT_EMAIL_TO or SES_FROM_ADDRESS unset"}
    message = render(ctx.payload, to=cfg.to)
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
                threshold=ctx.payload.get("threshold"), period=ctx.payload.get("period"))
    return {"status": "sent", "message_id": message_id, "to": cfg.to, "subject": message.subject,
            "configuration_set": cfg.configuration_set}
