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

AA-845: the Jev credit bells (low credit, shared/jev_credit.py; exhausted, shared/llm_client/decide.py) queue
the same job kind through `enqueue_jev_credit_email`, gated the same way: only when a new bell row is written,
and at most one e-mail per template per UTC day (a test send has its own key).

Not configured (SES_FROM_ADDRESS empty) -> nothing is queued; one log line. AA-913: the recipients are resolved
when the job runs (services/jobs/ops_alert_email_job.py): the confirmed rows of shared.alert_recipient, managed on
the admin page Platform › Alert recipients, plus ALERT_EMAIL_TO (shared/alert_recipients.ops_recipients). The
``test`` template is the page's "Send test e-mail" (api/services/alert_recipients.send_test).

AA-1018: the LLM breaker queues ``llm_circuit_open`` when a connection-level circuit opens (an account-wide refusal),
gated by its own bell throttle, at most one e-mail per connection, class and UTC day.
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
TEMPLATE_JEV_CREDIT_LOW = "jev_credit_low"                 # AA-845: estimated Jev credit below the threshold
TEMPLATE_JEV_CREDIT_EXHAUSTED = "jev_credit_exhausted"     # AA-845: Jev returned 402 billing_error
TEMPLATE_WRITING_MEMORY_ROLLBACK = "writing_memory_rollback"  # AA-920: a release failed its post-activation check
WRITING_MEMORY_PAGE_PATH = "/admin/lab/writing-memory"   # AA-1039 R6 slice 3 (was /admin/writing-memory)
JEV_TEMPLATES = (TEMPLATE_JEV_CREDIT_LOW, TEMPLATE_JEV_CREDIT_EXHAUSTED)
# AA-804 UI 2: the Jev credit ledger moved from Settings to AI providers › Providers › Jev credit. Mails already sent
# keep working: the old /admin/settings?tab=jev-credit link is sent on to this page in the browser.
JEV_CREDIT_PAGE_PATH = "/admin/ai-gateway?tab=providers&view=jev-credit"
PLACES_PAGE_PATH = "/admin/places"
ALERTS_PAGE_PATH = "/admin/platform/alerts"
THRESHOLD_STOP = "stop"
# Admin page with every spending cap (nav: AI & spend -> AI providers -> Budgets; AA-804 UI 2 moved it there from
# Cost Operations, whose old ?tab=budgets link is sent on to it in the browser).
SPEND_PAGE_PATH = "/admin/ai-gateway?tab=budgets"


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
        + f"<p><a href=\"{html.escape(link)}\">Open AI providers / Budgets</a></p>"
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


# ── AA-920 — writing memory: a release failed its post-activation check (rolled back automatically) ──────────

async def enqueue_writing_memory_rollback_email(conn, *, details: dict, now: Optional[datetime] = None
                                                ) -> Optional[str]:
    """AA-920 WM-26: queue the operator e-mail for a release the post-activation check moved back. One per release
    per UTC day (idempotency key). `details` holds ids, codes and counts only (never text). None when e-mail is not
    configured or today's e-mail for the release was queued already."""
    rid = str(details.get("release_id") or "?")
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", template=TEMPLATE_WRITING_MEMORY_ROLLBACK, release_id=rid)
        return None
    from shared.jobs import queue  # lazy: shared.jobs.worker imports shared.cost_guard

    day = utc_day(now)
    payload = {"template": TEMPLATE_WRITING_MEMORY_ROLLBACK, "period": day, **details}
    job, created = await queue.enqueue(conn, KIND, payload, max_attempts=MAX_ATTEMPTS,
                                       idempotency_key=f"{KIND}:{TEMPLATE_WRITING_MEMORY_ROLLBACK}:{rid}:{day}",
                                       created_by="writing_memory_post_activation")
    return job if created else None


def render_writing_memory_rollback(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                                   environment: Optional[str] = None) -> Message:
    """AA-920: a writing-memory release failed its post-activation check and the writer's pointer was moved back."""
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    link = f"{base_url or admin_base_url()}{WRITING_MEMORY_PAGE_PATH}"
    writer = str(payload.get("writer") or "?")
    codes = payload.get("codes") or {}
    subject = f"[AA-CIS {env}] Writing memory {writer}: release {payload.get('release_no')} rolled back"
    rows = [("Writer", writer), ("Scope", str(payload.get("scope") or "?")),
            ("Release", f"{payload.get('release_no')} ({payload.get('release_id')})"),
            ("Action", str(payload.get("action") or "-")),
            ("Problems", ", ".join(f"{k}: {v}" for k, v in sorted(codes.items())) or "-"),
            ("Writes checked under the release", str(payload.get("carried"))),
            ("Day", f"{payload.get('period')} (UTC)")]
    text = "\n".join(["A writing-memory release failed its post-activation check (a QA trace, another tenant's text "
                      "or a wrong fingerprint in a new write). The writer's pointer was moved back automatically.", ""]
                     + [f"{k}: {v}" for k, v in rows]
                     + ["", f"Review the release and the writes it names: {link} (Releases)"])
    cells = "".join(
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#555\">{html.escape(k)}</td>"
        f"<td style=\"padding:4px 0\">{html.escape(v)}</td></tr>" for k, v in rows)
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            "<p><strong>Writing memory release rolled back</strong>: it failed its post-activation check.</p>"
            f"<table style=\"border-collapse:collapse\">{cells}</table>"
            f"<p><a href=\"{html.escape(link)}\">Open Writing memory / Releases</a></p></div>")
    return Message(to=to, subject=subject, text=text, html=body)


# ── AA-845 — Jev (TypeSafe) credit: low estimate and exhausted (402) ─────────────────────────────

def jev_credit_key(template: str, test: bool, day: str) -> str:
    return f"{KIND}:{template}:{'test' if test else 'real'}:{day}"


async def enqueue_jev_credit_email(conn, *, template: str, details: dict, test: bool = False,
                                   now: Optional[datetime] = None) -> Optional[str]:
    """AA-845: queue the operator e-mail for a Jev credit bell that was just written. Returns the new job id,
    or None when e-mail is not configured or today's e-mail for this template (real or test) is queued already.
    The caller has already applied the bell's own throttle; this key only stops a second mail the same day."""
    if template not in JEV_TEMPLATES:
        raise ValueError(f"not a Jev credit template: {template!r}")
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", template=template)
        return None
    from shared.jobs import queue  # lazy: shared.jobs.worker imports shared.cost_guard

    day = utc_day(now)
    payload = {"template": template, "period": day, "test": bool(test), **details}
    job_id, created = await queue.enqueue(conn, KIND, payload, max_attempts=MAX_ATTEMPTS,
                                          idempotency_key=jev_credit_key(template, test, day),
                                          created_by="jev_credit")
    logger.info("ops_alert_email_queued" if created else "ops_alert_email_already_queued",
                job_id=job_id, template=template, test=bool(test), period=day)
    return job_id if created else None


def _jev_rows_html(rows) -> str:
    return "".join(
        f"<tr><td style=\"padding:4px 12px 4px 0;color:#555\">{html.escape(k)}</td>"
        f"<td style=\"padding:4px 0\">{html.escape(str(v))}</td></tr>" for k, v in rows)


def render_jev_credit(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                      environment: Optional[str] = None) -> Message:
    """AA-845: the operator e-mail for a Jev credit bell (template jev_credit_low or jev_credit_exhausted)."""
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    template = payload.get("template")
    test = bool(payload.get("test"))
    link = f"{base_url or admin_base_url()}{JEV_CREDIT_PAGE_PATH}"
    tag = "[TEST] " if test else ""
    if template == TEMPLATE_JEV_CREDIT_LOW:
        subject = (f"{tag}[AA-CIS {env}] Jev credit low: about {_usd(payload.get('estimated_left_usd'))} left "
                   f"(alert below {_usd(payload.get('threshold_usd'))})")
        headline = "Jev (TypeSafe) credit is running low"
        rows = [("Estimated left", _usd(payload.get("estimated_left_usd"))),
                ("Alert threshold", _usd(payload.get("threshold_usd"))),
                ("Topped up", f"{_usd(payload.get('total_topped_up_usd'))} since {payload.get('first_topup_on')}"),
                ("Jev spend since then", _usd(payload.get("spent_since_first_topup_usd"))),
                ("Checked by", str(payload.get("source") or "-")),
                ("Day", f"{payload.get('period')} (UTC)")]
        action = ("Top up the TypeSafe account and record the top-up in AI providers > Jev credit. The figure is an "
                  "estimate (recorded top-ups minus logged Jev spend); TypeSafe has no balance API.")
    else:
        subject = f"{tag}[AA-CIS {env}] Jev credit exhausted (TypeSafe 402)"
        headline = "Jev (TypeSafe) credit is exhausted"
        rows = [("Signal", "402 billing_error from TypeSafe"),
                ("Seen by", str(payload.get("source") or "-")),
                ("Day", f"{payload.get('period')} (UTC)")]
        action = ("Jev calls are failing open: stages run on their rules without Jev, and each api/worker process "
                  "stops calling TypeSafe for a cooldown after a 402. Top up the TypeSafe account, record the "
                  "top-up in AI providers > Jev credit; the next daily canary (or the next call after the cooldown) "
                  "closes the breaker.")
    note = str(payload.get("message") or "")
    footer = "The same alert is in the admin notification bell. At most one e-mail per alert type and UTC day."
    if test:
        footer = "This is a TEST send from POST /admin/decisions/jev-credit/check; no action is needed. " + footer
    text = "\n".join([f"{tag}{headline}.", ""] + [f"{k}: {v}" for k, v in rows]
                     + ([f"\nDetail: {note}"] if note else [])
                     + ["", action, "", f"Jev credit: {link}", footer])
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            f"<p><strong>{html.escape(tag + headline)}</strong></p>"
            f"<table style=\"border-collapse:collapse\">{_jev_rows_html(rows)}</table>"
            + (f"<p>Detail: {html.escape(note)}</p>" if note else "")
            + f"<p>{html.escape(action)}</p>"
            f"<p><a href=\"{html.escape(link)}\">Open AI providers / Jev credit</a></p>"
            f"<p style=\"color:#777;font-size:12px\">{html.escape(footer)}</p></div>")
    return Message(to=to, subject=subject, text=text, html=body)


# ── AA-1003 — the Reports page (report e-mails link to it). The R2 TP-03 handoff-gap e-mail (template
# inquiry_handoff_gap) was removed by the R0 hotfix with TP-03 (root ADR 0019, B2B-only model). ────────────────────

REPORTS_PAGE_PATH = "/admin/reports"


# ── AA-1018 — an LLM connection circuit opened (account-wide refusal: no key, no credit, access denied) ────────────

TEMPLATE_LLM_CIRCUIT_OPEN = "llm_circuit_open"
# Admin page with the LLM connections, their circuits and the availability card (Settings > LLM Models).
LLM_PROVIDERS_PAGE_PATH = "/admin/settings?tab=models"


def llm_circuit_key(connection_key: str, error_class: str, day: str) -> str:
    return f"{KIND}:{TEMPLATE_LLM_CIRCUIT_OPEN}:{connection_key}:{error_class}:{day}"


async def enqueue_llm_circuit_email(conn, *, details: dict, now: Optional[datetime] = None) -> Optional[str]:
    """AA-1018: queue the operator e-mail for a connection circuit that just opened (shared/llm_client/breaker.py,
    called only when a new bell row was written). At most one e-mail per connection, class and UTC day. `details` holds
    the connection key, class, model keys and times (never a prompt or a key). None when e-mail is not configured or
    today's e-mail is queued already."""
    connection_key = str(details.get("connection_key") or "?")
    error_class = str(details.get("error_class") or "other")
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", template=TEMPLATE_LLM_CIRCUIT_OPEN, connection=connection_key)
        return None
    from shared.jobs import queue  # lazy: shared.jobs.worker imports shared.cost_guard

    day = utc_day(now)
    payload = {"template": TEMPLATE_LLM_CIRCUIT_OPEN, "period": day, **details}
    job_id, created = await queue.enqueue(conn, KIND, payload, max_attempts=MAX_ATTEMPTS,
                                          idempotency_key=llm_circuit_key(connection_key, error_class, day),
                                          created_by="llm_breaker")
    logger.info("ops_alert_email_queued" if created else "ops_alert_email_already_queued",
                job_id=job_id, template=TEMPLATE_LLM_CIRCUIT_OPEN, connection=connection_key, period=day)
    return job_id if created else None


_CIRCUIT_ACTIONS = {
    "credential": "The connection's key cannot be read. Check the secret named on the connection (or the task "
                  "environment variable), then run Test connection.",
    "quota_exhausted": "The provider refuses for lack of credit or a spend cap. Top up or raise the cap with the "
                       "provider, then run Test connection.",
    "access_denied": "The account is refused on several models at once (for Bedrock: model access or an account "
                     "block). Check the account with the provider, then run Test connection.",
}


def render_llm_circuit_open(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                            environment: Optional[str] = None) -> Message:
    """AA-1018: an LLM connection was paused by its connection-level circuit. The last provider answer is masked again
    here (ARNs, account IDs), so a payload queued by an older build cannot carry one into the e-mail."""
    from shared.llm_client.errors import redact_ids  # lazy: keeps ops_alerts import-light

    env = environment or os.environ.get("ENVIRONMENT", "dev")
    link = f"{base_url or admin_base_url()}{LLM_PROVIDERS_PAGE_PATH}"
    key = str(payload.get("connection_key") or "?")
    cls = str(payload.get("error_class") or "other")
    models = payload.get("models") or []
    subject = f"[AA-CIS {env}] LLM connection {key} paused ({cls})"
    rows = [("Connection", key), ("Reason", cls), ("Models refused", ", ".join(map(str, models)) or "-"),
            ("Opened at", str(payload.get("opened_at") or "-")),
            ("Calls skip it until", str(payload.get("open_until") or "-")),
            ("Last provider answer", redact_ids(str(payload.get("last_error") or "")) or "-"),
            ("Day", f"{payload.get('period')} (UTC)")]
    action = _CIRCUIT_ACTIONS.get(cls, "Check the connection with the provider, then run Test connection.")
    footer = ("Routes fall back to their other models and connections where the stage allows it. The same alert is "
              "in the admin notification bell. At most one e-mail per connection, reason and UTC day.")
    text = "\n".join([f"LLM connection {key} was paused: every call to it is refused the same way.", ""]
                     + [f"{k}: {v}" for k, v in rows]
                     + ["", action, "", f"LLM connections: {link}", footer])
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            f"<p><strong>LLM connection {html.escape(key)} paused</strong>: every call to it is refused the same "
            "way.</p>"
            f"<table style=\"border-collapse:collapse\">{_jev_rows_html(rows)}</table>"
            f"<p>{html.escape(action)}</p>"
            f"<p><a href=\"{html.escape(link)}\">Open Settings / LLM Models</a></p>"
            f"<p style=\"color:#777;font-size:12px\">{html.escape(footer)}</p></div>")
    return Message(to=to, subject=subject, text=text, html=body)


# ── AA-642 (S263 lane L2, B4) — DataForSEO low balance, and the nightly LLM cost reconcile mismatch ─────────────────
# Both had an admin bell only (AA-627 /admin/dfs-balance/check; AA-1002 S10 llm_cost_reconcile). The e-mail is queued
# by the caller only when it just wrote a new bell row (the bell's own 24 h throttle gates it), and at most once per
# UTC day per template (the idempotency key).

TEMPLATE_DFS_BALANCE_LOW = "dfs_balance_low"
TEMPLATE_LLM_COST_MISMATCH = "llm_cost_mismatch"
DFS_SPEND_PAGE_PATH = "/admin/spend?tab=spend&view=dfs"   # AA-1039 R6 slice 3 (was /admin/cost-ops)
LLM_CONNECTIONS_PAGE_PATH = "/admin/ai-gateway?tab=providers&view=connections"
MISMATCH_ROWS_IN_MAIL = 10


def daily_template_key(template: str, day: str) -> str:
    return f"{KIND}:{template}:{day}"


async def _enqueue_daily(conn, template: str, details: dict, created_by: str,
                         now: Optional[datetime]) -> Optional[str]:
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", template=template)
        return None
    from shared.jobs import queue  # lazy: shared.jobs.worker imports shared.cost_guard

    day = utc_day(now)
    payload = {"template": template, "period": day, **details}
    job_id, created = await queue.enqueue(conn, KIND, payload, max_attempts=MAX_ATTEMPTS,
                                          idempotency_key=daily_template_key(template, day), created_by=created_by)
    logger.info("ops_alert_email_queued" if created else "ops_alert_email_already_queued",
                job_id=job_id, template=template, period=day)
    return job_id if created else None


async def enqueue_dfs_balance_low_email(conn, *, details: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Queue the operator e-mail for a DataForSEO low-balance bell that was just written (``details``: balance_usd,
    threshold_usd, currency, fetched_at). None when e-mail is not configured or today's e-mail is queued already."""
    return await _enqueue_daily(conn, TEMPLATE_DFS_BALANCE_LOW, details, "dfs_balance_check", now)


async def enqueue_llm_cost_mismatch_email(conn, *, details: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Queue the operator e-mail for a reconcile mismatch bell that was just written (``details``: window, mismatches
    = [{connection_key, day, logged_usd, provider_usd, diff_usd, source}], at most MISMATCH_ROWS_IN_MAIL kept). None
    when e-mail is not configured or today's e-mail is queued already."""
    rows = list(details.get("mismatches") or [])
    details = {**details, "mismatches": rows[:MISMATCH_ROWS_IN_MAIL], "mismatch_count": len(rows)}
    return await _enqueue_daily(conn, TEMPLATE_LLM_COST_MISMATCH, details, "llm_cost_reconcile", now)


def render_dfs_balance_low(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                           environment: Optional[str] = None) -> Message:
    """AA-642 (S263 L2, B4): the DataForSEO balance is below its alert threshold."""
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    link = f"{base_url or admin_base_url()}{DFS_SPEND_PAGE_PATH}"
    subject = (f"[AA-CIS {env}] DataForSEO balance low: {_usd(payload.get('balance_usd'))} "
               f"(alert below {_usd(payload.get('threshold_usd'))})")
    rows = [("Balance", f"{_usd(payload.get('balance_usd'))} {payload.get('currency') or ''}".strip()),
            ("Alert threshold", _usd(payload.get("threshold_usd"))),
            ("Read at", str(payload.get("fetched_at") or "-")),
            ("Day", f"{payload.get('period')} (UTC)")]
    action = ("Top up the DataForSEO account before it runs out: an empty balance answers 402 and every SEO fetch "
              "stops (the run keeps its existing keywords). The threshold is the DataForSEO connection's low-balance "
              "value in AI providers.")
    footer = "The same alert is in the admin notification bell. At most one e-mail per UTC day."
    text = "\n".join(["The DataForSEO balance is below its alert threshold.", ""] + [f"{k}: {v}" for k, v in rows]
                     + ["", action, "", f"DataForSEO spend: {link}", footer])
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            "<p><strong>DataForSEO balance low</strong></p>"
            f"<table style=\"border-collapse:collapse\">{_jev_rows_html(rows)}</table>"
            f"<p>{html.escape(action)}</p>"
            f"<p><a href=\"{html.escape(link)}\">Open Cost operations / DataForSEO</a></p>"
            f"<p style=\"color:#777;font-size:12px\">{html.escape(footer)}</p></div>")
    return Message(to=to, subject=subject, text=text, html=body)


def render_llm_cost_mismatch(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                             environment: Optional[str] = None) -> Message:
    """AA-642 (S263 L2, B4): the nightly reconcile found logged LLM cost that differs from the provider's figure."""
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    link = f"{base_url or admin_base_url()}{LLM_CONNECTIONS_PAGE_PATH}"
    rows_in = payload.get("mismatches") or []
    count = int(payload.get("mismatch_count") or len(rows_in))
    keys = sorted({str(m.get("connection_key")) for m in rows_in})
    subject = f"[AA-CIS {env}] LLM cost reconcile: {count} connection-day(s) differ ({', '.join(keys) or '-'})"
    window = payload.get("window") or {}
    rows = [(f"{m.get('connection_key')} {m.get('day')}",
             f"logged {_usd(m.get('logged_usd'))}, provider {_usd(m.get('provider_usd'))}, "
             f"difference {_usd(m.get('diff_usd'))}") for m in rows_in]
    rows.append(("Window", f"{window.get('start', '-')} to {window.get('end', '-')} (UTC days)"))
    more = count - len(rows_in)
    action = ("Compare the connection's calls in the ledger with the provider's usage page: a missing ledger row is "
              "unlogged spend, a provider figure above the ledger may be a call outside the gateway.")
    footer = ("Settled days only (a day Cost Explorer may still revise is never mailed). The same alert is in the "
              "admin notification bell. At most one e-mail per UTC day.")
    text = "\n".join(["The logged LLM cost differs from the provider's own figure.", ""]
                     + [f"{k}: {v}" for k, v in rows]
                     + ([f"... and {more} more"] if more > 0 else [])
                     + ["", action, "", f"LLM connections: {link}", footer])
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            "<p><strong>LLM cost reconcile mismatch</strong></p>"
            f"<table style=\"border-collapse:collapse\">{_jev_rows_html(rows)}</table>"
            + (f"<p>... and {more} more</p>" if more > 0 else "")
            + f"<p>{html.escape(action)}</p>"
            f"<p><a href=\"{html.escape(link)}\">Open AI providers / Connections</a></p>"
            f"<p style=\"color:#777;font-size:12px\">{html.escape(footer)}</p></div>")
    return Message(to=to, subject=subject, text=text, html=body)


# ── AA-1025 B3 — a non-LLM provider circuit opened (Jev, DataForSEO, Geoapify) ──────────────────────────────────────
# Queued by shared/provider_circuit.alert_open only when it just wrote the bell (one unread per connection per 24 h);
# at most one e-mail per connection, class and UTC day (the idempotency key).

TEMPLATE_PROVIDER_CIRCUIT_OPEN = "provider_circuit_open"
PROVIDERS_PAGE_PATH = "/admin/ai-gateway?tab=providers"


def provider_circuit_key(connection_key: str, error_class: str, day: str) -> str:
    return f"{KIND}:{TEMPLATE_PROVIDER_CIRCUIT_OPEN}:{connection_key}:{error_class}:{day}"


async def enqueue_provider_circuit_email(conn, *, details: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Queue the operator e-mail for a provider circuit that just opened (``details``: connection_key, label,
    error_class, times, the masked last error; never a key or a request body). None when e-mail is not configured or
    today's e-mail for this connection and class is queued already."""
    connection_key = str(details.get("connection_key") or "?")
    error_class = str(details.get("error_class") or "other")
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", template=TEMPLATE_PROVIDER_CIRCUIT_OPEN,
                    connection=connection_key)
        return None
    from shared.jobs import queue  # lazy: shared.jobs.worker imports shared.cost_guard

    day = utc_day(now)
    payload = {"template": TEMPLATE_PROVIDER_CIRCUIT_OPEN, "period": day, **details}
    job_id, created = await queue.enqueue(conn, KIND, payload, max_attempts=MAX_ATTEMPTS,
                                          idempotency_key=provider_circuit_key(connection_key, error_class, day),
                                          created_by="provider_circuit")
    logger.info("ops_alert_email_queued" if created else "ops_alert_email_already_queued",
                job_id=job_id, template=TEMPLATE_PROVIDER_CIRCUIT_OPEN, connection=connection_key, period=day)
    return job_id if created else None


_PROVIDER_CIRCUIT_ACTIONS = {
    "access_denied": "The provider refuses the key. Check the secret named on the connection, then switch the "
                     "provider off and on again in AI providers once it is fixed.",
    "quota_exhausted": "The provider refuses for lack of credit. Top up the account with the provider.",
    "throttled": "The provider keeps answering 429. Lower the rate of the jobs that call it, or wait.",
    "other": "The provider is down or failing. Check its status page; the circuit probes it again on its own.",
}


def render_provider_circuit_open(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                                 environment: Optional[str] = None) -> Message:
    """AA-1025 B3: a provider circuit opened; every api and worker task skips the provider until the probe passes.
    The last error is masked again here (ARNs, account IDs)."""
    from shared.llm_client.errors import redact_ids  # lazy: keeps ops_alerts import-light

    env = environment or os.environ.get("ENVIRONMENT", "dev")
    link = f"{base_url or admin_base_url()}{PROVIDERS_PAGE_PATH}"
    key = str(payload.get("connection_key") or "?")
    label = str(payload.get("label") or key)
    cls = str(payload.get("error_class") or "other")
    subject = f"[AA-CIS {env}] {label} paused by its circuit ({cls})"
    rows = [("Provider", f"{label} ({key})"), ("Reason", cls),
            ("Failed calls in a row", str(payload.get("consecutive_failures") or "-")),
            ("Opened at", str(payload.get("opened_at") or "-")),
            ("Calls skip it until", str(payload.get("open_until") or "-")),
            ("Last provider answer", redact_ids(str(payload.get("last_error") or "")) or "-"),
            ("Day", f"{payload.get('period')} (UTC)")]
    action = _PROVIDER_CIRCUIT_ACTIONS.get(cls, _PROVIDER_CIRCUIT_ACTIONS["other"])
    footer = ("After the open period one call probes the provider; its success closes the circuit for every task. "
              "The same alert is in the admin notification bell. At most one e-mail per provider, reason and UTC day.")
    text = "\n".join([f"{label} was paused: its calls failed the same way in a row.", ""]
                     + [f"{k}: {v}" for k, v in rows]
                     + ["", action, "", f"AI providers: {link}", footer])
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            f"<p><strong>{html.escape(label)} paused</strong>: its calls failed the same way in a row.</p>"
            f"<table style=\"border-collapse:collapse\">{_jev_rows_html(rows)}</table>"
            f"<p>{html.escape(action)}</p>"
            f"<p><a href=\"{html.escape(link)}\">Open AI providers / Providers</a></p>"
            f"<p style=\"color:#777;font-size:12px\">{html.escape(footer)}</p></div>")
    return Message(to=to, subject=subject, text=text, html=body)


# ── AA-1025 B5 — the estimated credit of a vendor API connection (top-ups minus spend) is low ─────────────────────
# Queued by shared/provider_credit.check_low_credit only when it just wrote the bell (one real bell per connection per
# 24 h); at most one e-mail per connection and UTC day (a test send has its own key).

TEMPLATE_PROVIDER_CREDIT_LOW = "provider_credit_low"
PROVIDER_CREDIT_PAGE_PATH = "/admin/ai-gateway?tab=providers&view=credit"


def provider_credit_key(connection_key: str, test: bool, day: str) -> str:
    return f"{KIND}:{TEMPLATE_PROVIDER_CREDIT_LOW}:{connection_key}:{'test' if test else 'real'}:{day}"


async def enqueue_provider_credit_email(conn, *, details: dict, test: bool = False,
                                        now: Optional[datetime] = None) -> Optional[str]:
    """Queue the operator e-mail for a low-credit bell that was just written (``details``: connection_key, label,
    estimated_left_usd, threshold_usd, the ledger figures). None when e-mail is not configured or today's e-mail for
    this connection (real or test) is queued already."""
    connection_key = str(details.get("connection_key") or "?")
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", template=TEMPLATE_PROVIDER_CREDIT_LOW, connection=connection_key)
        return None
    from shared.jobs import queue  # lazy: shared.jobs.worker imports shared.cost_guard

    day = utc_day(now)
    payload = {"template": TEMPLATE_PROVIDER_CREDIT_LOW, "period": day, "test": bool(test), **details}
    job_id, created = await queue.enqueue(conn, KIND, payload, max_attempts=MAX_ATTEMPTS,
                                          idempotency_key=provider_credit_key(connection_key, test, day),
                                          created_by="provider_credit")
    logger.info("ops_alert_email_queued" if created else "ops_alert_email_already_queued",
                job_id=job_id, template=TEMPLATE_PROVIDER_CREDIT_LOW, connection=connection_key, test=bool(test),
                period=day)
    return job_id if created else None


def render_provider_credit_low(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                               environment: Optional[str] = None) -> Message:
    """AA-1025 B5: the estimated credit of a provider connection is below its low-balance threshold."""
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    link = f"{base_url or admin_base_url()}{PROVIDER_CREDIT_PAGE_PATH}"
    key = str(payload.get("connection_key") or "?")
    label = str(payload.get("label") or key)
    tag = "[TEST] " if payload.get("test") else ""
    subject = (f"{tag}[AA-CIS {env}] {label} credit low: about {_usd(payload.get('estimated_left_usd'))} left "
               f"(alert below {_usd(payload.get('threshold_usd'))})")
    rows = [("Connection", f"{label} ({key})"),
            ("Estimated left", _usd(payload.get("estimated_left_usd"))),
            ("Alert threshold", _usd(payload.get("threshold_usd"))),
            ("Balance recorded", _usd(payload.get("recorded_usd"))),
            ("Spent since", f"{_usd(payload.get('spent_usd'))} since {payload.get('since') or '-'}"),
            ("Day", f"{payload.get('period')} (UTC)")]
    action = ("Top up the account with the provider and record the top-up (or a fresh balance read from its console) "
              "in AI providers › Credit. The estimate is the recorded balance minus the spend logged since then; the "
              "provider has no balance API the gateway may call.")
    footer = "The same alert is in the admin notification bell. At most one e-mail per connection and UTC day."
    text = "\n".join([f"{tag}The estimated credit of {label} is below its alert threshold.", ""]
                     + [f"{k}: {v}" for k, v in rows]
                     + ["", action, "", f"Credit ledger: {link}", footer])
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            f"<p><strong>{html.escape(tag)}{html.escape(label)} credit low</strong></p>"
            f"<table style=\"border-collapse:collapse\">{_jev_rows_html(rows)}</table>"
            f"<p>{html.escape(action)}</p>"
            f"<p><a href=\"{html.escape(link)}\">Open AI providers / Credit</a></p>"
            f"<p style=\"color:#777;font-size:12px\">{html.escape(footer)}</p></div>")
    return Message(to=to, subject=subject, text=text, html=body)


# ── S279 thresholds L1 — the settings registry (shared/settings, CIS 407) ────────────────────────────────────────────
# ``setting_self_approved``: the administrator approved their own change of a high-impact setting (dev-phase rule,
# tech lead S279 Q2): the tech lead hears about it; one e-mail per change (the value row id is the key).
# ``setting_out_of_bounds``: the reader refused a stored value that breaks its definition and serves the default; at
# most one e-mail per key and UTC day.

TEMPLATE_SETTING_SELF_APPROVED = "setting_self_approved"
TEMPLATE_SETTING_OUT_OF_BOUNDS = "setting_out_of_bounds"
SETTINGS_PAGE_PATH = "/admin/platform/settings"


async def enqueue_setting_self_approved_email(conn, *, details: dict) -> Optional[str]:
    """Queue the tech-lead e-mail for a self-approved high-impact setting change (``details``: key, label, value_id,
    before, after, reason, actor, effective_from). None when e-mail is not configured or already queued."""
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", template=TEMPLATE_SETTING_SELF_APPROVED)
        return None
    from shared.jobs import queue

    payload = {"template": TEMPLATE_SETTING_SELF_APPROVED, "period": utc_day(), **details}
    job_id, created = await queue.enqueue(
        conn, KIND, payload, max_attempts=MAX_ATTEMPTS, created_by="settings_registry",
        idempotency_key=f"{KIND}:{TEMPLATE_SETTING_SELF_APPROVED}:{details.get('value_id')}")
    return job_id if created else None


async def enqueue_setting_alert_email(conn, *, details: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Queue the operator e-mail for a refused stored setting value (``details``: key, problem, version, value)."""
    if sender_config() is None:
        logger.info("ops_alert_email_not_configured", template=TEMPLATE_SETTING_OUT_OF_BOUNDS)
        return None
    from shared.jobs import queue

    day = utc_day(now)
    payload = {"template": TEMPLATE_SETTING_OUT_OF_BOUNDS, "period": day, **details}
    job_id, created = await queue.enqueue(
        conn, KIND, payload, max_attempts=MAX_ATTEMPTS, created_by="settings_reader",
        idempotency_key=f"{KIND}:{TEMPLATE_SETTING_OUT_OF_BOUNDS}:{details.get('key')}:{day}")
    return job_id if created else None


def _setting_message(to: Recipients, subject: str, intro: str, rows: list, action: str, link: str) -> Message:
    text = "\n".join([intro, ""] + [f"{k}: {v}" for k, v in rows] + ["", action, "", f"Settings: {link}"])
    body = ("<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">"
            f"<p><strong>{html.escape(intro)}</strong></p>"
            f"<table style=\"border-collapse:collapse\">{_jev_rows_html(rows)}</table>"
            f"<p>{html.escape(action)}</p>"
            f"<p><a href=\"{html.escape(link)}\">Open Platform / Settings</a></p></div>")
    return Message(to=to, subject=subject, text=text, html=body)


def render_setting_self_approved(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                                 environment: Optional[str] = None) -> Message:
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    link = f"{base_url or admin_base_url()}{SETTINGS_PAGE_PATH}"
    key = str(payload.get("key") or "?")
    rows = [("Setting", f"{payload.get('label') or key} ({key})"),
            ("Before", str(payload.get("before"))), ("After", str(payload.get("after"))),
            ("Effective from", str(payload.get("effective_from") or "-")),
            ("Changed by", str(payload.get("actor") or "-")), ("Reason", str(payload.get("reason") or "-"))]
    return _setting_message(
        to, f"[AA-CIS {env}] Setting self-approved: {key}", "A high-impact setting was changed and self-approved.",
        rows, "Dev-phase rule: the administrator may approve their own change with a reason; this e-mail is the "
              "second pair of eyes. Every change is in the page history and the audit log.", link)


def render_setting_out_of_bounds(payload: dict, *, to: Recipients, base_url: Optional[str] = None,
                                 environment: Optional[str] = None) -> Message:
    env = environment or os.environ.get("ENVIRONMENT", "dev")
    link = f"{base_url or admin_base_url()}{SETTINGS_PAGE_PATH}"
    key = str(payload.get("key") or "?")
    rows = [("Setting", key), ("Problem", str(payload.get("problem") or "-")),
            ("Stored value", str(payload.get("value") or "-")), ("Version", str(payload.get("version") or "-")),
            ("Day", f"{payload.get('period')} (UTC)")]
    return _setting_message(
        to, f"[AA-CIS {env}] Setting value refused: {key}", "A stored setting value breaks its definition.", rows,
        "The reader serves the default value instead. Propose a valid value on the Settings page.", link)
