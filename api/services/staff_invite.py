"""AA-816 (S242) — the staff invitation e-mail: render, queue, rate limit, status.

C6 creates WORKFORCE users with ``MessageAction=SUPPRESS`` (Cognito's own invite carries a temporary password,
against email-code-only sign-in), so nobody told the new person. CIS sends its own invitation instead:

- **Queue**: :func:`enqueue_invite` adds one ``staff_invite_email`` job (shared.job) and the
  ``staff.invite_requested`` audit row on the caller's connection, i.e. inside the transaction that creates the
  account (``staff_directory.create_staff``) or accepts the resend (``staff_directory.resend_invite``). The
  request never talks to SES; the worker sends (``services/jobs/staff_invite_email_job.py``).
- **Idempotency**: key ``staff_invite_email:<staff_id>:<seq>``; ``seq`` is 1 + the highest seq already used for
  that account (read from the keys, so the 30-day job retention can never make a new key collide with a kept one).
  The target row is locked ``FOR UPDATE`` by the caller, so two resends at once serialize; the key is the
  backstop.
- **Send**: SES v2 through ``shared/aws_client/ses_mailer.py`` (AA-828): From ``SES_FROM_ADDRESS``
  (no-reply@lumiguides.it.com, the task role's ``ses:FromAddress`` condition), configuration set
  ``SES_CONFIGURATION_SET`` (aa-email-dev-transactional). Not configured -> nothing is queued.
- **Audit** (acp_shared.audit_log, resource_type ``admin_user``): ``staff.invite_requested`` (request),
  ``staff.invite_sent`` (worker, SES accepted), ``staff.invite_not_sent`` (worker skipped it, or the job ended
  without success). The staff list reads "last invited" from these rows (no column, no migration).
- **Rate limit** per account, from the ``staff.invite_requested`` rows (the create's own invite counts): at most
  one per :data:`MIN_INTERVAL_S` and :data:`MAX_PER_DAY` per rolling 24 h.

The message holds the sign-in page link only (``ADMIN_BASE_URL``/login) and the public logo: no code, token,
password or deep link carrying a secret. Display names are HTML-escaped and stripped of control characters.
"""
from __future__ import annotations

import html
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from services.acp_shared.audit_log import write_audit_log
from shared.aws_client.ses_mailer import Message, sender_config

KIND = "staff_invite_email"
MAX_ATTEMPTS = 5
RESOURCE_TYPE = "admin_user"

REASON_CREATED = "created"
REASON_RESEND = "resend"

MIN_INTERVAL_S = 10 * 60
MAX_PER_DAY = 5
DAY_S = 24 * 60 * 60

STAFF_EMAIL_DOMAIN = "adventure.asia"
SUBJECT = "You've been added to Adventure Asia staff admin"
LOGIN_PATH = "/login"
LOGO_PATH = "/assets/adventure-asia-logo.png"   # frontend/public/assets, served without auth (not /brand)
SIGN_IN_BUTTON = "Sign in with company email"     # the /login button label (frontend/app/login)

# AA brand (frontend/app/_brand/tokens.ts): cream page, gold primary button, ink text.
COLOR_BG = "#f4f1ec"
COLOR_CARD = "#ffffff"
COLOR_GOLD = "#db9628"
COLOR_INK = "#1f2933"
COLOR_BODY = "#33363d"
COLOR_MUTED = "#6b7380"
COLOR_LINE = "#e9e4db"

# What each role means, in plain words (api/core/staff_authz.py ROLE_PERMS; Booking roles: staff_app_grant).
CIS_ROLE_TEXT: dict[str, str] = {
    "admin": "Administrator (every page of the content admin, including settings and spend)",
    "reviewer": "Reviewer (tour content work and reviews)",
    "content": "Content team (tour content work)",
}
BOOKING_ROLE_TEXT: dict[str, str] = {
    "admin": "Booking administrator",
    "sales": "Sales",
    "contracting": "Contracting",
    "content_creator": "Content creator",
}
NO_BOOKING_TEXT = "No access to Booking"
UNKNOWN_ACTOR = "An Adventure Asia platform owner"


class InviteAudit:
    """``acp_shared.audit_log.action`` values of the invitation (resource_type ``admin_user``)."""
    REQUESTED = "staff.invite_requested"
    SENT = "staff.invite_sent"
    NOT_SENT = "staff.invite_not_sent"


WORKER_ACTOR = f"job:{KIND}"


def mail_configured() -> bool:
    """True when the SES sender is set (SES_FROM_ADDRESS); without it nothing is queued."""
    return sender_config() is not None


def admin_base_url() -> str:
    return os.environ.get("ADMIN_BASE_URL", "https://aa-cis.lumiguides.it.com").rstrip("/")


def invite_key(staff_id: str, seq: int) -> str:
    return f"{KIND}:{staff_id}:{int(seq)}"


# ── Render ───────────────────────────────────────────────────────────────────────────────────────

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def clean_name(value: Any, limit: int = 120) -> str:
    """One line of visible text: control characters out, whitespace collapsed, capped."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]


def cis_role_text(role: Optional[str]) -> str:
    return CIS_ROLE_TEXT.get(role or "", clean_name(role) or "no role")


def booking_role_text(role: Optional[str]) -> str:
    if not role:
        return NO_BOOKING_TEXT
    return BOOKING_ROLE_TEXT.get(role, clean_name(role))


def render_invite(*, to: str, display_name: Any, actor_name: Any, role: Optional[str],
                  booking_role: Optional[str], base_url: Optional[str] = None) -> Message:
    """The invitation (plain text + HTML). ``to`` is the account's work address (already validated)."""
    base = (base_url or admin_base_url()).rstrip("/")
    login_url = f"{base}{LOGIN_PATH}"
    logo_url = f"{base}{LOGO_PATH}"
    name = clean_name(display_name)
    actor = clean_name(actor_name) or UNKNOWN_ACTOR
    cis_text = cis_role_text(role)
    booking_text = booking_role_text(booking_role)
    greeting = f"Hello {name}," if name else "Hello,"

    steps = [
        f"Open {login_url}",
        f'Click "{SIGN_IN_BUTTON}".',
        f"Enter this address: {to}",
        "Enter the one-time code we email you. It is valid for a few minutes.",
    ]
    text = "\n".join([
        greeting,
        "",
        f"{actor} added you to the Adventure Asia staff admin.",
        "",
        "Your access",
        f"- Content admin (CIS): {cis_text}",
        f"- Booking: {booking_text}",
        "",
        "How to sign in",
        *[f"{i}. {s}" for i, s in enumerate(steps, 1)],
        "",
        "There is no password. Each sign-in uses a new code sent to this address.",
        "",
        f"If you did not expect this e-mail, contact {actor} or the Adventure Asia platform owner. "
        "Replies to this address are not read.",
        "",
        "Adventure Asia",
    ])

    e = html.escape
    step_items = "".join(
        f'<li style="margin:0 0 6px 0">{s}</li>' for s in (
            f'Open <a href="{e(login_url)}" style="color:{COLOR_GOLD};font-weight:600">{e(login_url)}</a>',
            f"Click <strong>&ldquo;{e(SIGN_IN_BUTTON)}&rdquo;</strong>.",
            f"Enter this address: <strong>{e(to)}</strong>",
            "Enter the one-time code we email you. It is valid for a few minutes.",
        ))
    body = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(SUBJECT)}</title></head>'
        f'<body style="margin:0;padding:0;background:{COLOR_BG}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{COLOR_BG}">'
        '<tr><td align="center" style="padding:28px 12px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;'
        f'background:{COLOR_CARD};border:1px solid {COLOR_LINE};border-radius:12px;'
        f'font-family:Poppins,Arial,Helvetica,sans-serif;color:{COLOR_BODY};font-size:15px;line-height:1.55">'
        '<tr><td style="padding:28px 28px 8px 28px" align="left">'
        f'<img src="{e(logo_url)}" width="140" alt="Adventure Asia" style="display:block;border:0;width:140px;'
        'height:auto"></td></tr>'
        '<tr><td style="padding:8px 28px 0 28px">'
        f'<p style="margin:0 0 12px 0">{e(greeting)}</p>'
        f'<p style="margin:0 0 18px 0"><strong style="color:{COLOR_INK}">{e(actor)}</strong> added you to the '
        'Adventure Asia staff admin.</p>'
        f'<p style="margin:0 0 6px 0;font-weight:600;color:{COLOR_INK}">Your access</p>'
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 20px 0;font-size:14px">'
        f'<tr><td style="padding:3px 14px 3px 0;color:{COLOR_MUTED};white-space:nowrap">Content admin (CIS)</td>'
        f'<td style="padding:3px 0">{e(cis_text)}</td></tr>'
        f'<tr><td style="padding:3px 14px 3px 0;color:{COLOR_MUTED};white-space:nowrap">Booking</td>'
        f'<td style="padding:3px 0">{e(booking_text)}</td></tr></table>'
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 22px 0"><tr>'
        f'<td style="border-radius:999px;background:{COLOR_GOLD}">'
        f'<a href="{e(login_url)}" style="display:inline-block;padding:12px 26px;border-radius:999px;'
        f'background:{COLOR_GOLD};color:#ffffff;font-weight:600;text-decoration:none;letter-spacing:0.06em;'
        f'text-transform:uppercase;font-size:13px">Go to sign-in</a></td></tr></table>'
        f'<p style="margin:0 0 6px 0;font-weight:600;color:{COLOR_INK}">How to sign in</p>'
        f'<ol style="margin:0 0 16px 0;padding-left:20px">{step_items}</ol>'
        '<p style="margin:0 0 20px 0">There is no password. Each sign-in uses a new code sent to this address.</p>'
        '</td></tr>'
        f'<tr><td style="padding:16px 28px 26px 28px;border-top:1px solid {COLOR_LINE};font-size:12.5px;'
        f'color:{COLOR_MUTED}">If you did not expect this e-mail, contact {e(actor)} or the Adventure Asia platform '
        'owner. Replies to this address are not read.</td></tr>'
        '</table></td></tr></table></body></html>'
    )
    return Message(to=to, subject=SUBJECT, text=text, html=body)


# ── Queue (inside the caller's transaction) ──────────────────────────────────────────────────────

SQL_MAX_SEQ = (
    "SELECT coalesce(max(substring(idempotency_key from '[0-9]+$')::int), 0) FROM shared.job "
    "WHERE idempotency_key LIKE $1"
)


async def enqueue_invite(conn: Any, *, staff_id: str, actor_id: str, reason: str) -> str:
    """Queue one invitation for ``staff_id`` and write its ``staff.invite_requested`` row, on ``conn`` (the
    caller's transaction, with the target row already locked). Returns the job id."""
    from shared.jobs import queue  # lazy: shared.jobs.worker imports shared.cost_guard

    seq = int(await conn.fetchval(SQL_MAX_SEQ, f"{KIND}:{staff_id}:%") or 0) + 1
    payload = {"staff_id": staff_id, "requested_by": actor_id, "reason": reason, "seq": seq}
    job_id, created = await queue.enqueue(conn, KIND, payload, idempotency_key=invite_key(staff_id, seq),
                                          max_attempts=MAX_ATTEMPTS, created_by=f"admin:{actor_id}")
    await write_audit_log(conn, tenant_id=None, actor=f"admin:{actor_id}", action=InviteAudit.REQUESTED,
                          resource_type=RESOURCE_TYPE, resource_id=staff_id,
                          details={"reason": reason, "job_id": job_id, "seq": seq, "new_job": created})
    return job_id


# ── Rate limit ───────────────────────────────────────────────────────────────────────────────────

SQL_RECENT_REQUESTS = (
    "SELECT count(*) AS n, min(created_at) AS first_at, max(created_at) AS last_at, now() AS now "
    "FROM acp_shared.audit_log "
    "WHERE action = $1 AND resource_type = $2 AND resource_id = $3 AND created_at > now() - make_interval(secs => $4)"
)


@dataclass(frozen=True)
class RateLimited:
    retry_after_s: int
    message: str


def _minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


def rate_limit(n: int, first_at: Optional[datetime], last_at: Optional[datetime],
               now: datetime) -> Optional[RateLimited]:
    """None when another invitation may be queued now; else when, and why (for the 429)."""
    if last_at is not None:
        elapsed = (now - last_at).total_seconds()
        if elapsed < MIN_INTERVAL_S:
            wait = MIN_INTERVAL_S - elapsed
            return RateLimited(math.ceil(wait), (
                f"An invitation was already sent to this person in the last {MIN_INTERVAL_S // 60} minutes. "
                f"You can send another in {_minutes(wait)} minute(s)."))
    if n >= MAX_PER_DAY and first_at is not None:
        allowed_at = first_at + timedelta(seconds=DAY_S)
        wait = max((allowed_at - now).total_seconds(), 1)
        return RateLimited(math.ceil(wait), (
            f"This person already got {MAX_PER_DAY} invitations in the last 24 hours. You can send another after "
            f"{allowed_at.strftime('%H:%M')} UTC ({_minutes(wait)} minute(s) from now)."))
    return None


async def check_rate_limit(conn: Any, staff_id: str) -> Optional[RateLimited]:
    row = await conn.fetchrow(SQL_RECENT_REQUESTS, InviteAudit.REQUESTED, RESOURCE_TYPE, staff_id, float(DAY_S))
    if row is None:
        return None
    return rate_limit(int(row["n"] or 0), row["first_at"], row["last_at"], row["now"])


# ── Status on the staff list ─────────────────────────────────────────────────────────────────────

def _latest_sql(action: str, alias: str) -> str:
    return (f"(SELECT max(a.created_at) FROM acp_shared.audit_log a WHERE a.action = '{action}' "
            f"AND a.resource_type = '{RESOURCE_TYPE}' AND a.resource_id = u.id::text) AS {alias}")


# Appended to the admin_platform staff SELECT (``u`` = shared.admin_users). Constants only, never user input.
SELECT_INVITE_COLUMNS = ", ".join((
    _latest_sql(InviteAudit.SENT, "last_invited_at"),
    _latest_sql(InviteAudit.REQUESTED, "last_invite_requested_at"),
    _latest_sql(InviteAudit.NOT_SENT, "last_invite_not_sent_at"),
))

STATUS_SENT = "sent"
STATUS_QUEUED = "queued"
STATUS_NOT_SENT = "not_sent"


def invite_status(sent: Optional[datetime], requested: Optional[datetime],
                  not_sent: Optional[datetime]) -> Optional[str]:
    """``queued`` while the latest request has no outcome yet, else the latest outcome; None if never asked."""
    def after(a: Optional[datetime], *others: Optional[datetime]) -> bool:
        return a is not None and all(o is None or a > o for o in others)

    if after(requested, sent, not_sent):
        return STATUS_QUEUED
    if after(not_sent, sent):
        return STATUS_NOT_SENT
    if sent is not None:
        return STATUS_SENT
    return None


# ── Worker reads ─────────────────────────────────────────────────────────────────────────────────

SQL_INVITE_TARGET = (
    "SELECT u.id::text AS id, u.username, u.email, u.display_name, u.role::text AS role, u.is_active, "
    "g.role AS booking_role FROM shared.admin_users u "
    "LEFT JOIN shared.staff_app_grant g ON g.admin_user_id = u.id AND g.app = 'booking' "
    "WHERE u.id = $1::uuid"
)
SQL_ACTOR_NAME = "SELECT display_name, username FROM shared.admin_users WHERE id = $1::uuid"
SQL_ALREADY_SENT = (
    "SELECT 1 FROM acp_shared.audit_log WHERE action = $1 AND resource_type = $2 AND resource_id = $3 "
    "AND details->>'job_id' = $4 LIMIT 1"
)


def is_staff_address(email: Optional[str]) -> bool:
    value = (email or "").strip().lower()
    local, sep, domain = value.rpartition("@")
    return bool(sep and local and domain == STAFF_EMAIL_DOMAIN)
