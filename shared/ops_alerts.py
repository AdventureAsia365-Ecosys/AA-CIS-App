"""shared/ops_alerts.py — AA-828: operator e-mail for spend budget alerts.

`shared.cost_guard.maybe_alert` writes the admin bell row (shared.notifications) when a spend budget
crosses its alert threshold or stops a run. At that same moment it calls
`enqueue_spend_budget_email`, which queues one `ops_alert_email` job; the worker renders the message
and sends it through SES (services/jobs/ops_alert_email_job.py, shared/aws_client/ses_mailer.py).
Nothing here talks to SES, so the caller (a job handler that just hit its budget) never waits on mail.

At most one e-mail per budget (provider + scope), per threshold, per UTC day: the job's idempotency key
(shared.job unique index) is `ops_alert_email:spend_budget:<provider>:<scope>:<threshold>:<day>`. On top
of that the bell's own throttle (one unread alert per provider per 24 h) gates the enqueue, so the
e-mail never fires more often than the bell.

Not configured (SES_FROM_ADDRESS empty) -> nothing is queued; one log line. AA-913: the recipients are resolved
when the job runs (services/jobs/ops_alert_email_job.py): the confirmed rows of shared.alert_recipient, managed on
the admin page Platform › Alert recipients, plus ALERT_EMAIL_TO (shared/alert_recipients.ops_recipients). The
``test`` template is the page's "Send test e-mail" (api/services/alert_recipients.send_test).
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from typing import Optional, Union

import structlog

from shared.aws_client.ses_mailer import Message, sender_config

logger = structlog.get_logger()

KIND = "ops_alert_email"
MAX_ATTEMPTS = 5
TEMPLATE_SPEND_BUDGET = "spend_budget"
TEMPLATE_TEST = "test"           # AA-913: "Send test e-mail" on Platform › Alert recipients
TEMPLATE_PLACE_DEDUPE_HELD = "place_dedupe_held"   # AA-847: the duplicate merge held a country (circuit breaker)
PLACES_PAGE_PATH = "/admin/places"
ALERTS_PAGE_PATH = "/admin/platform/alerts"
THRESHOLD_STOP = "stop"
# Admin page with the spend overview and the Budgets tab (nav: Operations -> External Spend).
SPEND_PAGE_PATH = "/admin/llm-usage"


def admin_base_url() -> str:
    return os.environ.get("ADMIN_BASE_URL", "https://aa-cis.lumiguides.it.com").rstrip("/")


def utc_day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()


def threshold_label(alert_pct: int, stopped: bool) -> str:
    """`stop` when a run was stopped by the budget, else `<alert_pct>pct` (the alert threshold)."""
    return THRESHOLD_STOP if stopped else f"{int(alert_pct)}pct"


def spend_budget_key(provider: str, scope: str, threshold: str, day: str) -> str:
    return f"{KIND}:{TEMPLATE_SPEND_BUDGET}:{provider}:{scope}:{threshold}:{day}"


async def enqueue_spend_budget_email(conn, *, summary: dict, alert_pct: int, stopped: bool,
                                     message: str, used_pct: Optional[float],
                                     now: Optional[datetime] = None) -> Optional[str]:
    """Queue the operator e-mail for one budget alert. Returns the new job id, or None when e-mail is
    not configured or this budget/threshold/day already has its e-mail job. `conn` is an asyncpg
    connection or pool (only fetchrow/fetchval are used)."""
    provider, scope = str(summary.get("provider", "")), str(summary.get("scope", ""))
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", provider=provider, scope=scope,
                    hint="set SES_FROM_ADDRESS on the task definition")
        return None
    from shared.jobs import queue  # lazy: shared.jobs.worker imports shared.cost_guard

    day = utc_day(now)
    threshold = threshold_label(alert_pct, stopped)
    payload = {
        "template": TEMPLATE_SPEND_BUDGET,
        "period": day,
        "threshold": threshold,
        "alert_pct": int(alert_pct),
        "used_pct": None if used_pct is None else round(float(used_pct), 1),
        "message": message,
        "budget": summary,
    }
    job_id, created = await queue.enqueue(conn, KIND, payload, max_attempts=MAX_ATTEMPTS,
                                          idempotency_key=spend_budget_key(provider, scope, threshold, day),
                                          created_by="cost_guard")
    logger.info("ops_alert_email_queued" if created else "ops_alert_email_already_queued",
                job_id=job_id, provider=provider, scope=scope, threshold=threshold, period=day)
    return job_id if created else None


def _usd(v) -> str:
    return "none" if v is None else f"${float(v):,.2f}"


Recipients = Union[str, tuple[str, ...]]


def render_spend_budget(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                        environment: Optional[str] = None) -> Message:
    """The operator e-mail for one spend budget alert (plain text + HTML)."""
    b = payload.get("budget") or {}
    provider, scope = b.get("provider", "?"), b.get("scope", "?")
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    stopped = payload.get("threshold") == THRESHOLD_STOP
    used = payload.get("used_pct")
    link = f"{base_url or admin_base_url()}{SPEND_PAGE_PATH}"
    crossed = ("hard stop: a run was stopped by this budget" if stopped
               else f"alert threshold {payload.get('alert_pct')}% of the daily limit")
    headline = "run stopped" if stopped else (f"{used:.0f}% of daily limit" if used is not None else "alert")
    subject = f"[AA-CIS {env}] Spend budget {provider} / {scope}: {headline}"
    rows = [
        ("Budget", f"{provider} / {scope}"),
        ("Period", f"{payload.get('period')} (UTC day)"),
        ("Spent today", f"{_usd(b.get('day_spent_usd'))} of daily limit {_usd(b.get('per_day_usd'))}"
                        + (f" ({used:.1f}%)" if used is not None else "")),
        ("This run", f"{_usd(b.get('run_spent_usd'))} of per-run limit {_usd(b.get('per_run_usd'))}"),
        ("Threshold crossed", crossed),
        ("Hard stop", f"per run {'on' if b.get('hard_stop') else 'off'}, "
                      f"daily {'on' if b.get('hard_stop_day') else 'off'}"),
    ]
    if b.get("global_day_usd") is not None:
        rows.append(("Provider-wide today", f"{_usd(b.get('global_day_spent_usd'))} of "
                                            f"{_usd(b.get('global_day_usd'))}"))
    note = str(payload.get("message") or "")
    text = "\n".join(
        ["Spend budget alert (shared.spend_budget, cost guard).", ""]
        + [f"{k}: {v}" for k, v in rows]
        + ([f"\nDetail: {note}"] if note else [])
        + ["", f"Budgets and spend: {link} (Budgets tab)",
           "The same alert is in the admin notification bell. One e-mail per budget, threshold and UTC day."]
    )
    cells = "".join(
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#555\">{html.escape(k)}</td>"
        f"<td style=\"padding:4px 0\">{html.escape(str(v))}</td></tr>" for k, v in rows)
    body = (
        "<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
        "<p><strong>Spend budget alert</strong> (shared.spend_budget, cost guard)</p>"
        f"<table style=\"border-collapse:collapse\">{cells}</table>"
        + (f"<p>Detail: {html.escape(note)}</p>" if note else "")
        + f"<p><a href=\"{html.escape(link)}\">Open External Spend / Budgets</a></p>"
        "<p style=\"color:#777;font-size:12px\">The same alert is in the admin notification bell. "
        "One e-mail per budget, threshold and UTC day.</p></div>"
    )
    return Message(to=to, subject=subject, text=text, html=body)


def render_test(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                environment: Optional[str] = None) -> Message:
    """AA-913: the test e-mail of Platform › Alert recipients (the CIS SES path only)."""
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    who = " ".join(str(payload.get("requested_by") or "").split())[:120] or "an administrator"
    link = f"{base_url or admin_base_url()}{ALERTS_PAGE_PATH}"
    subject = f"[AA-CIS {env}] Test alert e-mail"
    lines = [
        f"This test was sent from Platform > Alert recipients by {who}.",
        "",
        "It came through the CIS spend alert path (Amazon SES), the one that sends spend budget e-mails.",
        "AWS alarms, security alerts, cost anomalies and AWS Budgets arrive separately from AWS Notifications "
        "(no-reply@sns.amazonaws.com).",
        "",
        f"Recipients: {link}",
    ]
    body = (
        "<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
        f"<p><strong>Test alert e-mail</strong></p><p>This test was sent from Platform &gt; Alert recipients by "
        f"{html.escape(who)}.</p>"
        "<p>It came through the CIS spend alert path (Amazon SES), the one that sends spend budget e-mails. AWS "
        "alarms, security alerts, cost anomalies and AWS Budgets arrive separately from AWS Notifications "
        "(no-reply@sns.amazonaws.com).</p>"
        f"<p><a href=\"{html.escape(link)}\">Open Alert recipients</a></p></div>"
    )
    return Message(to=to, subject=subject, text="\n".join(lines), html=body)


async def enqueue_place_dedupe_held_email(conn, *, country: str, outside: int, limit: int, job_id: Optional[str],
                                          message: str, now: Optional[datetime] = None) -> Optional[str]:
    """AA-847: queue the operator e-mail for a country the place_dedupe circuit breaker held. One per country per
    UTC day (idempotency key). None when e-mail is not configured or today's e-mail was queued already."""
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", template=TEMPLATE_PLACE_DEDUPE_HELD, country=country)
        return None
    from shared.jobs import queue  # lazy: shared.jobs.worker imports shared.cost_guard

    day = utc_day(now)
    payload = {"template": TEMPLATE_PLACE_DEDUPE_HELD, "period": day, "country": country, "outside": int(outside),
               "limit": int(limit), "job_id": job_id, "message": message}
    job, created = await queue.enqueue(conn, KIND, payload, max_attempts=MAX_ATTEMPTS,
                                       idempotency_key=f"{KIND}:{TEMPLATE_PLACE_DEDUPE_HELD}:{country}:{day}",
                                       created_by="place_dedupe")
    return job if created else None


def render_place_dedupe_held(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                             environment: Optional[str] = None) -> Message:
    """AA-847: the automatic duplicate merge held a country (changes outside the approved dry run above the limit)."""
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    country = str(payload.get("country") or "?")
    link = f"{base_url or admin_base_url()}{PLACES_PAGE_PATH}"
    subject = f"[AA-CIS {env}] Place merge held: {country}"
    rows = [("Country", country), ("Changes outside the approved dry run", str(payload.get("outside"))),
            ("Limit", str(payload.get("limit"))), ("Job", str(payload.get("job_id") or "-")),
            ("Day", f"{payload.get('period')} (UTC)")]
    text = "\n".join(["The automatic duplicate-place merge applied nothing for this country (circuit breaker).", ""]
                     + [f"{k}: {v}" for k, v in rows]
                     + ["", f"Review the dry run and approve it again: {link} (Merges)"])
    cells = "".join(
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#555\">{html.escape(k)}</td>"
        f"<td style=\"padding:4px 0\">{html.escape(v)}</td></tr>" for k, v in rows)
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            "<p><strong>Place merge held</strong>: the automatic duplicate-place merge applied nothing for this "
            "country (circuit breaker).</p>"
            f"<table style=\"border-collapse:collapse\">{cells}</table>"
            f"<p><a href=\"{html.escape(link)}\">Open Places / Merges</a></p></div>")
    return Message(to=to, subject=subject, text=text, html=body)
