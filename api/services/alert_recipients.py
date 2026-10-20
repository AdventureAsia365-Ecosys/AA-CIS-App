"""AA-913 — the admin page's work on shared.alert_recipient: page view, add, remove, resend, test e-mail.

The router (api/routers/admin_alert_recipients.py) checks who may call; this module does the work. Shared pieces
(SNS calls, reconcile, e-mail rule, constants) are in shared/alert_recipients.py.

- **Add**: the address is validated (422), must not be the bootstrap or already active (409), the list must have
  room (MAX_ACTIVE, 409) and the day's adds must be under MAX_ADDS_PER_DAY (429). The topic's subscriptions are
  listed first, outside any lock, to know whether SNS already has the address confirmed. Then one transaction:
  ``pg_advisory_xact_lock`` (serializes adds), re-check count + duplicate, ``Subscribe`` (the only AWS call under
  the lock), INSERT, audit ``added`` (+ ``confirmation_sent`` when pending). An SNS error rolls everything back:
  502 ``{detail, code}`` and no row.
- **Remove**: the row is locked FOR UPDATE; when it has a subscription ARN, SNS is asked whether it still exists
  and the subscription is removed (``NotFound`` = already gone; a pending one SNS refuses to delete is left to
  expire after 3 days, and reconcile — the api's 5-minute ops sampler tick at the latest — unsubscribes it if it
  is confirmed first). A subscription whose SNS endpoint (or row e-mail) is the bootstrap address is never
  unsubscribed (``sns = bootstrap_kept``). Any other SNS error: 502, row unchanged. The row becomes
  ``removed``/``admin``; audit ``removed`` says what SNS did.
- **Resend**: pending rows only (409); at most once per RESEND_MIN_INTERVAL_S and RESEND_MAX_PER_DAY per row,
  counted from the ``confirmation_sent`` audit rows (the add's own confirmation counts), 429 + Retry-After. It
  calls ``Subscribe`` again for the address (SNS re-sends the confirmation of a pending subscription).
- **Test e-mail**: one ``ops_alert_email`` job with the ``test`` template (SES to the confirmed rows +
  ALERT_EMAIL_TO), at most once per TEST_MIN_INTERVAL_S (429). It tests the CIS SES path only; SNS delivery is
  checked by putting an alarm in ALARM (``aws cloudwatch set-alarm-state``), since the task role has no
  ``sns:Publish`` on purpose.

Errors are :class:`AlertRecipientError` (HTTP status, detail, code, headers); nothing here returns or logs the
topic or subscription ARN.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from services.acp_shared.audit_log import write_audit_log
from shared import alert_recipients as ar
from shared import ops_alerts
from shared.aws_client.ses_mailer import sender_config

logger = structlog.get_logger()


class AlertRecipientError(Exception):
    """A refused or failed change. ``status`` is the HTTP status; ``detail`` the message shown on the page."""

    def __init__(self, status: int, detail: str, code: str, headers: Optional[dict[str, str]] = None) -> None:
        super().__init__(status, detail, code, headers)
        self.status = status
        self.detail = detail
        self.code = code
        self.headers = headers


NOT_CONFIGURED = AlertRecipientError(
    503, "Alert recipients are not set up on this environment (ALERTS_TOPIC_ARN or ALERT_BOOTSTRAP_EMAIL is not set)",
    "not_configured")


def _sns_failed(exc: ar.SnsError, doing: str) -> AlertRecipientError:
    return AlertRecipientError(502, f"AWS SNS refused to {doing} ({exc.code}). Nothing was changed; try again "
                                    "in a minute.", exc.code)


# ── SQL ──────────────────────────────────────────────────────────────────────────────────────────

SQL_LOCK = "SELECT pg_advisory_xact_lock(hashtext('shared.alert_recipient'))"
SQL_COUNT_ACTIVE = "SELECT count(*) FROM shared.alert_recipient WHERE status <> 'removed'"
SQL_ACTIVE_BY_EMAIL = "SELECT id::text FROM shared.alert_recipient WHERE status <> 'removed' AND email = $1"
# $1 email, $2 note, $3 status (pending / confirmed), $4 subscription ARN, $5 created_by
SQL_INSERT = (
    "INSERT INTO shared.alert_recipient (email, note, status, subscription_arn, created_by, confirmed_at, "
    "last_confirmation_sent_at) VALUES ($1, $2, $3::text, $4, $5, "
    "CASE WHEN $3::text = 'confirmed' THEN now() END, CASE WHEN $3::text = 'pending' THEN now() END) "
    "RETURNING id::text"
)
SQL_ROW_FOR_UPDATE = (
    "SELECT id::text AS id, email, status, subscription_arn FROM shared.alert_recipient WHERE id = $1::uuid FOR UPDATE"
)
SQL_MARK_REMOVED = (
    "UPDATE shared.alert_recipient SET status = 'removed', removed_reason = 'admin', removed_at = now(), "
    "removed_by = $2, updated_at = now() WHERE id = $1::uuid"
)
SQL_MARK_RESENT = (
    "UPDATE shared.alert_recipient SET last_confirmation_sent_at = now(), "
    "subscription_arn = coalesce($2::text, subscription_arn), updated_at = now() WHERE id = $1::uuid"
)
# Rate limits read acp_shared.audit_log (like api/services/staff_invite.py).
SQL_RECENT_ACTION = (
    "SELECT count(*) AS n, min(created_at) AS first_at, max(created_at) AS last_at, now() AS now "
    "FROM acp_shared.audit_log WHERE action = $1 AND resource_type = $2 "
    "AND created_at > now() - make_interval(secs => $3)"
)
SQL_RECENT_ACTION_FOR = SQL_RECENT_ACTION + " AND resource_id = $4"


# ── Rate limits (pure + SQL) ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RateLimited:
    retry_after_s: int
    message: str


def _minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


def rate_limit(n: int, first_at: Optional[datetime], last_at: Optional[datetime], now: datetime, *,
               min_interval_s: Optional[int], max_per_day: Optional[int], what: str) -> Optional[RateLimited]:
    """None when the action may happen now; else how long to wait and why (the 429)."""
    if min_interval_s and last_at is not None:
        elapsed = (now - last_at).total_seconds()
        if elapsed < min_interval_s:
            wait = min_interval_s - elapsed
            return RateLimited(math.ceil(wait), f"{what} was done less than {min_interval_s // 60} minutes ago. "
                                                f"Try again in {_minutes(wait)} minute(s).")
    if max_per_day and n >= max_per_day and first_at is not None:
        allowed_at = first_at + timedelta(seconds=ar.DAY_S)
        wait = max((allowed_at - now).total_seconds(), 1)
        return RateLimited(math.ceil(wait), f"{what}: the limit is {max_per_day} in 24 hours. Try again after "
                                            f"{allowed_at.strftime('%H:%M')} UTC ({_minutes(wait)} minute(s)).")
    return None


async def _recent(conn: Any, action: str, resource_id: Optional[str] = None) -> tuple[int, Any, Any, datetime]:
    if resource_id is None:
        row = await conn.fetchrow(SQL_RECENT_ACTION, action, ar.RESOURCE_TYPE, float(ar.DAY_S))
    else:
        row = await conn.fetchrow(SQL_RECENT_ACTION_FOR, action, ar.RESOURCE_TYPE, float(ar.DAY_S), resource_id)
    if row is None:
        return 0, None, None, datetime.now(timezone.utc)
    return int(row["n"] or 0), row["first_at"], row["last_at"], row["now"]


def _too_many(limited: RateLimited, code: str) -> AlertRecipientError:
    return AlertRecipientError(429, limited.message, code, headers={"Retry-After": str(limited.retry_after_s)})


async def adds_today(conn: Any) -> int:
    n, *_ = await _recent(conn, ar.AlertAudit.ADDED)
    return n


# ── Page view ────────────────────────────────────────────────────────────────────────────────────

_TIME_FIELDS = ("created_at", "confirmed_at", "removed_at", "last_confirmation_sent_at", "last_reconciled_at")


def _who(raw: Optional[str], name: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    if name:
        return name
    if raw == ar.ACTOR_RECONCILE:
        return "Status check (SNS)"
    if raw.startswith("job:"):
        return "Background job"
    return "Unknown staff"


def page_row(row: Any) -> dict:
    """One recipient for the browser. Never the subscription ARN (only whether one is known)."""
    d = dict(row)
    out = {
        "id": d["id"], "email": d["email"], "note": d.get("note"), "status": d["status"],
        "removed_reason": d.get("removed_reason"), "has_subscription": bool(d.get("has_subscription")),
        "created_by": _who(d.get("created_by"), d.get("created_by_name")),
        "removed_by": _who(d.get("removed_by"), d.get("removed_by_name")),
        "last_seen_state": d.get("last_seen_state"),
    }
    for key in _TIME_FIELDS:
        value = d.get(key)
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    sent = d.get("last_confirmation_sent_at")
    out["resend_available_at"] = (
        (sent + timedelta(seconds=ar.RESEND_MIN_INTERVAL_S)).isoformat()
        if d["status"] == ar.STATUS_PENDING and isinstance(sent, datetime) else None)
    return out


async def page(pool: Any, client: Any, *, redis: Any = None, refresh: bool = False,
               actor: str = ar.ACTOR_RECONCILE) -> dict:
    """GET body: reconcile first (throttled; ``refresh`` forces it), then the table."""
    cfg = ar.topic_config()
    reconciled: Optional[ar.ReconcileResult] = None
    if cfg is not None and client is not None:
        reconciled = await ar.reconcile_throttled(pool, client, cfg, redis=redis, force=refresh, actor=actor)
    async with pool.acquire() as conn:
        active = [page_row(r) for r in await conn.fetch(ar.SQL_LIST_ACTIVE)]
        history = [page_row(r) for r in await conn.fetch(ar.SQL_LIST_HISTORY)]
        added_today = await adds_today(conn)
    bootstrap = ar.bootstrap_email()
    fallback = ar.ops_fallback_email()
    locked_state = (reconciled.locked or {}).get("state") if reconciled is not None and reconciled.locked else None
    return {
        "configured": cfg is not None,
        "topic_name": cfg.topic_name if cfg is not None else None,
        "bootstrap": {"email": bootstrap, "locked": True, "state": locked_state} if bootstrap else None,
        "ops_fallback_email": fallback if fallback and fallback != bootstrap else None,
        "ops_mail_configured": sender_config() is not None,
        "recipients": active,
        "unmanaged": (reconciled.unmanaged if reconciled is not None else []),
        "leaving": (reconciled.leaving if reconciled is not None else []),
        "history": history,
        "limits": {
            "max_active": ar.MAX_ACTIVE,
            "active": len(active),
            "adds_left_today": max(ar.MAX_ADDS_PER_DAY - added_today, 0),
            "max_note": ar.MAX_NOTE_LEN,
        },
        "reconciled_at": reconciled.reconciled_at if reconciled is not None else None,
        "reconcile_error": reconciled.error if reconciled is not None else None,
    }


async def read_one(pool: Any, recipient_id: str) -> Optional[dict]:
    row = await pool.fetchrow(ar.SQL_GET_ONE, recipient_id)
    return page_row(row) if row is not None else None


# ── Add ──────────────────────────────────────────────────────────────────────────────────────────

async def add(pool: Any, client: Any, *, actor: str, email: Any, note: Any, redis: Any = None) -> dict:
    cfg = ar.topic_config()
    if cfg is None:
        raise NOT_CONFIGURED
    try:
        address = ar.normalize_email(email)
        clean_note = ar.normalize_note(note)
    except ValueError as exc:
        raise AlertRecipientError(422, str(exc), "invalid") from None
    if cfg.bootstrap_email and address == cfg.bootstrap_email:
        raise AlertRecipientError(409, "This is the bootstrap recipient set in Terraform; it always receives every "
                                       "alert and is not managed here", "bootstrap")
    try:
        subs = await ar.list_email_subscriptions(client, cfg.topic_arn)
    except ar.SnsError as exc:
        raise _sns_failed(exc, "list the topic's subscriptions") from None
    already_confirmed = any(s.endpoint == address and s.state == ar.SEEN_CONFIRMED for s in subs)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(SQL_LOCK)
            if await conn.fetchval(SQL_ACTIVE_BY_EMAIL, address) is not None:
                raise AlertRecipientError(409, f"{address} is already on the list", "duplicate")
            if int(await conn.fetchval(SQL_COUNT_ACTIVE) or 0) >= ar.MAX_ACTIVE:
                raise AlertRecipientError(409, f"The list is full ({ar.MAX_ACTIVE} recipients). Remove one first.",
                                          "full")
            n, first_at, last_at, now = await _recent(conn, ar.AlertAudit.ADDED)
            limited = rate_limit(n, first_at, last_at, now, min_interval_s=None,
                                 max_per_day=ar.MAX_ADDS_PER_DAY, what="Adding recipients")
            if limited is not None:
                raise _too_many(limited, "rate_limited")
            try:
                arn = await ar.subscribe(client, cfg.topic_arn, address)
            except ar.SnsError as exc:
                raise _sns_failed(exc, "add the subscription") from None
            status = ar.STATUS_CONFIRMED if already_confirmed else ar.STATUS_PENDING
            rid = await conn.fetchval(SQL_INSERT, address, clean_note, status, arn or None, actor)
            sns_state = "confirmed" if already_confirmed else "pending"
            await write_audit_log(conn, tenant_id=None, actor=actor, action=ar.AlertAudit.ADDED,
                                  resource_type=ar.RESOURCE_TYPE, resource_id=rid,
                                  details={"email": address, "note": clean_note, "sns_state": sns_state,
                                           "topic": cfg.topic_name})
            if status == ar.STATUS_PENDING:
                await write_audit_log(conn, tenant_id=None, actor=actor, action=ar.AlertAudit.CONFIRMATION_SENT,
                                      resource_type=ar.RESOURCE_TYPE, resource_id=rid,
                                      details={"email": address, "reason": "added"})
    logger.info("alert_recipient_added", topic=cfg.topic_name, recipient_id=rid, sns_state=sns_state)
    await ar.invalidate_cache(redis)
    row = await read_one(pool, rid)
    return {"recipient": row, "sns_state": sns_state}


# ── Remove ───────────────────────────────────────────────────────────────────────────────────────

async def remove(pool: Any, client: Any, *, actor: str, recipient_id: str, redis: Any = None) -> dict:
    cfg = ar.topic_config()
    if cfg is None:
        raise NOT_CONFIGURED
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(SQL_ROW_FOR_UPDATE, recipient_id)
            if row is None or row["status"] == ar.STATUS_REMOVED:
                raise AlertRecipientError(404, "Recipient not found (or already removed)", "not_found")
            arn = row["subscription_arn"]
            sns = "no_subscription"
            if arn and str(row["email"]).lower() == cfg.bootstrap_email:
                sns = "bootstrap_kept"
            elif arn:
                try:
                    info = await ar.subscription_info(client, arn)
                    pending = info.pending if info is not None else None
                    if info is None:
                        sns = "not_found"
                    elif await _is_bootstrap_subscription(client, cfg, arn, info):
                        sns = "bootstrap_kept"
                    else:
                        try:
                            await ar.unsubscribe(client, arn)
                            sns = "unsubscribed"
                        except ar.SnsError as exc:
                            if exc.code == "NotFound":
                                sns = "not_found"
                            elif pending and exc.code in ("InvalidParameter", "InvalidParameterException"):
                                sns = "pending_left_to_expire"
                            else:
                                raise
                except ar.SnsError as exc:
                    raise _sns_failed(exc, "remove the subscription") from None
            await conn.execute(SQL_MARK_REMOVED, recipient_id, actor)
            await write_audit_log(conn, tenant_id=None, actor=actor, action=ar.AlertAudit.REMOVED,
                                  resource_type=ar.RESOURCE_TYPE, resource_id=recipient_id,
                                  details={"email": row["email"], "was": row["status"], "sns": sns,
                                           "topic": cfg.topic_name})
    log = logger.warning if sns == "bootstrap_kept" else logger.info
    log("alert_recipient_removed", topic=cfg.topic_name, recipient_id=recipient_id, sns=sns)
    await ar.invalidate_cache(redis)
    return {"recipient": await read_one(pool, recipient_id), "sns": sns}


async def _is_bootstrap_subscription(client: Any, cfg: ar.TopicConfig, arn: str, info: ar.SubInfo) -> bool:
    """Is this ARN the locked Terraform subscription? GetSubscriptionAttributes normally names the endpoint; when it
    does not, the topic listing does (confirmed subscriptions are listed with their ARN). Fail closed: an SNS error
    here propagates (502, nothing changed)."""
    if info.endpoint is not None:
        return info.endpoint == cfg.bootstrap_email
    subs = await ar.list_email_subscriptions(client, cfg.topic_arn)
    return any(s.arn == arn and s.endpoint == cfg.bootstrap_email for s in subs)


# ── Resend ───────────────────────────────────────────────────────────────────────────────────────

async def resend(pool: Any, client: Any, *, actor: str, recipient_id: str) -> dict:
    cfg = ar.topic_config()
    if cfg is None:
        raise NOT_CONFIGURED
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(SQL_ROW_FOR_UPDATE, recipient_id)
            if row is None or row["status"] == ar.STATUS_REMOVED:
                raise AlertRecipientError(404, "Recipient not found (or already removed)", "not_found")
            if row["status"] != ar.STATUS_PENDING:
                raise AlertRecipientError(409, "Only a recipient waiting for confirmation can get the e-mail again",
                                          "not_pending")
            n, first_at, last_at, now = await _recent(conn, ar.AlertAudit.CONFIRMATION_SENT, recipient_id)
            limited = rate_limit(n, first_at, last_at, now, min_interval_s=ar.RESEND_MIN_INTERVAL_S,
                                 max_per_day=ar.RESEND_MAX_PER_DAY, what="Sending the confirmation e-mail")
            if limited is not None:
                raise _too_many(limited, "rate_limited")
            try:
                arn = await ar.subscribe(client, cfg.topic_arn, row["email"])
            except ar.SnsError as exc:
                raise _sns_failed(exc, "send the confirmation again") from None
            await conn.execute(SQL_MARK_RESENT, recipient_id, arn or None)
            await write_audit_log(conn, tenant_id=None, actor=actor, action=ar.AlertAudit.CONFIRMATION_SENT,
                                  resource_type=ar.RESOURCE_TYPE, resource_id=recipient_id,
                                  details={"email": row["email"], "reason": "resend"})
    logger.info("alert_recipient_confirmation_resent", topic=cfg.topic_name, recipient_id=recipient_id)
    return {"recipient": await read_one(pool, recipient_id)}


# ── Test e-mail ──────────────────────────────────────────────────────────────────────────────────

def ops_test_key(now: datetime) -> str:
    return f"{ops_alerts.KIND}:{ops_alerts.TEMPLATE_TEST}:{now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M')}"


async def send_test(pool: Any, *, actor: str, requested_by: Optional[str]) -> dict:
    if sender_config() is None:
        raise AlertRecipientError(503, "E-mail is not set up on this environment (SES_FROM_ADDRESS is not set)",
                                  "ses_not_configured")
    recipients, _ = await ar.ops_recipients(pool)
    if not recipients:
        raise AlertRecipientError(503, "Nobody would receive it: no confirmed recipient and ALERT_EMAIL_TO is not "
                                       "set", "no_recipient")
    from shared.jobs import queue

    async with pool.acquire() as conn:
        n, first_at, last_at, now = await _recent(conn, ar.AlertAudit.TEST_QUEUED)
        limited = rate_limit(n, first_at, last_at, now, min_interval_s=ar.TEST_MIN_INTERVAL_S, max_per_day=None,
                             what="A test e-mail")
        if limited is not None:
            raise _too_many(limited, "rate_limited")
        async with conn.transaction():
            job_id, created = await queue.enqueue(
                conn, ops_alerts.KIND, {"template": ops_alerts.TEMPLATE_TEST, "requested_by": requested_by},
                idempotency_key=ops_test_key(now), max_attempts=ops_alerts.MAX_ATTEMPTS, created_by=actor)
            await write_audit_log(conn, tenant_id=None, actor=actor, action=ar.AlertAudit.TEST_QUEUED,
                                  resource_type=ar.RESOURCE_TYPE, resource_id="test-email",
                                  details={"job_id": job_id, "created": created, "recipients": len(recipients)})
    return {"job_id": job_id, "created": created, "recipients_count": len(recipients)}
