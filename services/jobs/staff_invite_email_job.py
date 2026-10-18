"""AA-816 (S242) — job kind `staff_invite_email`: send one staff invitation e-mail through SES.

Queued by api/services/staff_invite.enqueue_invite inside the transaction that creates the account
(staff_directory.create_staff, send_invite) or accepts a resend (staff_directory.resend_invite).
Payload: {"staff_id", "requested_by" (admin user id), "reason": "created" | "resend", "seq"} — ids only; the
handler re-reads the row at send time, so the e-mail shows the current roles and goes to the current address.
Idempotency key `staff_invite_email:<staff_id>:<seq>` (one job per request).

- SES_FROM_ADDRESS unset -> NonRetryable (the job fails; on_terminal writes `staff.invite_not_sent`).
- Row gone, disabled, break-glass, without an email, or an address outside @adventure.asia -> succeeds as
  `skipped` with the reason, and a `staff.invite_not_sent` row (so the Staff page does not show "queued" forever).
- A `staff.invite_sent` row for this job already exists -> done without sending (a worker that wrote it but died
  before marking the job succeeded).
- SES refusal a retry cannot fix (rejected address, sender not allowed, account paused, sandbox recipient) ->
  NonRetryable; throttling, network and 5xx are retried by the runner (backoff, MAX_ATTEMPTS).
- Sent -> `staff.invite_sent` (job id + SES message id). The result holds no address (the job-runner API is
  open to every staff role).
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

import structlog

from api.services import staff_invite as si
from api.services.staff_directory import is_breakglass
from services.acp_shared.audit_log import write_audit_log
from shared.aws_client import ses_mailer
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = si.KIND


async def _audit(pool: Any, action: str, staff_id: str, details: dict) -> None:
    await write_audit_log(pool, tenant_id=None, actor=si.WORKER_ACTOR, action=action,
                          resource_type=si.RESOURCE_TYPE, resource_id=staff_id, details=details)


def _staff_id(payload: Any) -> str:
    raw = (payload or {}).get("staff_id") if isinstance(payload, dict) else None
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, TypeError):
        raise NonRetryable(f"payload.staff_id is not a uuid: {raw!r}") from None


def _skip_reason(row: Optional[dict]) -> Optional[str]:
    if row is None:
        return "account_not_found"
    if is_breakglass(row.get("username"), row.get("email")):
        return "breakglass"
    if not row.get("is_active"):
        return "account_disabled"
    if not row.get("email"):
        return "no_email"
    if not si.is_staff_address(row.get("email")):
        return "not_a_staff_address"
    return None


async def _actor_name(pool: Any, actor_id: Any) -> Optional[str]:
    try:
        aid = str(uuid.UUID(str(actor_id)))
    except (ValueError, TypeError):
        return None
    row = await pool.fetchrow(si.SQL_ACTOR_NAME, aid)
    if row is None:
        return None
    return row["display_name"] or row["username"]


async def mark_not_sent(pool: Any, job_row: dict) -> None:
    """on_terminal: the job failed or was cancelled, so the Staff page must not keep showing "queued"."""
    payload = job_row.get("payload") or {}
    staff_id = payload.get("staff_id") if isinstance(payload, dict) else None
    if not staff_id:
        return
    await _audit(pool, si.InviteAudit.NOT_SENT, str(staff_id), {
        "job_id": str(job_row.get("id")), "reason": f"job_{job_row.get('status')}",
        "error": (job_row.get("error") or "")[:300],
    })
    logger.warning("staff_invite_not_sent", job_id=str(job_row.get("id")), staff_id=staff_id,
                   job_status=job_row.get("status"))


@job_kind(KIND, concurrency=2, max_attempts=si.MAX_ATTEMPTS, expected_seconds=60, on_terminal=mark_not_sent)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    staff_id = _staff_id(payload)
    base = {"staff_id": staff_id, "reason": payload.get("reason"), "seq": payload.get("seq")}
    cfg = ses_mailer.sender_config()
    if cfg is None:
        raise NonRetryable("SES_FROM_ADDRESS is unset: invitation e-mail is not configured")

    if await ctx.pool.fetchval(si.SQL_ALREADY_SENT, si.InviteAudit.SENT, si.RESOURCE_TYPE, staff_id, ctx.job_id):
        logger.info("staff_invite_already_sent", job_id=ctx.job_id, staff_id=staff_id)
        return {**base, "status": "already_sent"}

    row = await ctx.pool.fetchrow(si.SQL_INVITE_TARGET, staff_id)
    row = dict(row) if row is not None else None
    reason = _skip_reason(row)
    if reason is not None:
        await _audit(ctx.pool, si.InviteAudit.NOT_SENT, staff_id, {"job_id": ctx.job_id, "reason": reason})
        logger.info("staff_invite_skipped", job_id=ctx.job_id, staff_id=staff_id, reason=reason)
        return {**base, "status": "skipped", "skip_reason": reason}

    actor = await _actor_name(ctx.pool, payload.get("requested_by"))
    message = si.render_invite(to=row["email"].strip().lower(), display_name=row.get("display_name"),
                               actor_name=actor, role=row.get("role"), booking_role=row.get("booking_role"))
    client = ctx.resources.get("ses") or ses_mailer.ses_client(cfg.region)
    try:
        message_id = await ses_mailer.send(client, message, sender=cfg.sender,
                                           configuration_set=cfg.configuration_set)
    except ses_mailer.MailError as e:
        logger.warning("staff_invite_ses_error", job_id=ctx.job_id, staff_id=staff_id, code=e.code,
                       permanent=e.permanent)
        # The job error is shown on the job-runner API (every staff role): the SES code only, never its message,
        # which can quote the recipient address.
        if e.permanent:
            raise NonRetryable(f"SES refused the invitation ({e.code})") from e
        raise RuntimeError(f"SES send failed ({e.code}); the runner retries") from e
    await _audit(ctx.pool, si.InviteAudit.SENT, staff_id, {
        "job_id": ctx.job_id, "message_id": message_id, "reason": payload.get("reason"), "seq": payload.get("seq"),
        "requested_by": payload.get("requested_by"), "configuration_set": cfg.configuration_set,
    })
    logger.info("staff_invite_sent", job_id=ctx.job_id, staff_id=staff_id, message_id=message_id,
                reason=payload.get("reason"))
    return {**base, "status": "sent", "message_id": message_id, "configuration_set": cfg.configuration_set}
