"""shared/alert_recipients.py — AA-913: platform alert recipients (shared.alert_recipient, migration 251).

Everyone on this list gets every platform alert by e-mail: the SNS alerts topic (CloudWatch alarms of CIS, Booking,
TripPlanner and shared; security EventBridge rules; cost anomalies; AWS Budgets after the AA-913 Infra change) and
the CIS spend e-mails of the ``ops_alert_email`` job (SES). The admin page Platform › People & access › Alert
recipients manages it (api/routers/admin_alert_recipients.py, api/services/alert_recipients.py).

Pieces (pure first, I/O thin):

- :func:`normalize_email` — one address, lower case, the same rule as the table's CHECK.
- :class:`TopicConfig` / :func:`topic_config` — the alerts topic and the Terraform bootstrap address, from the
  environment the task definition sets (``ALERTS_TOPIC_ARN``, ``ALERT_BOOTSTRAP_EMAIL``, ``AWS_REGION``). The topic
  ARN is never written in this public repo, never logged and never returned to the browser (the UI gets the topic
  *name*). ``ALERTS_TOPIC_ARN`` or ``ALERT_BOOTSTRAP_EMAIL`` empty -> None: the page is read-only "not configured"
  (fail closed: without the bootstrap address the app could not tell the locked Terraform subscription apart, and an
  add + remove of that address would unsubscribe it).
- SNS calls (:func:`list_email_subscriptions`, :func:`subscribe`, :func:`unsubscribe`, :func:`subscription_pending`)
  — boto3 in a thread (``asyncio.to_thread``), short timeouts; tests inject a stub client. The task role may call
  only Subscribe (e-mail protocol), ListSubscriptionsByTopic, Unsubscribe and GetSubscriptionAttributes, on this one
  topic (AA-CIS-Infra ``alert_recipients.tf``).
- :func:`plan_reconcile` (pure) and :func:`reconcile` — make the table match what SNS says: a confirmation clicked
  -> ``confirmed``; never confirmed within AWS's 3 days -> ``removed``/``expired``; unsubscribed through the link in
  an alert (or removed in the console) -> ``removed``/``unsubscribed``; an address the admin removed whose pending
  confirmation was clicked afterwards -> unsubscribed again. Throttled to once per :data:`RECONCILE_TTL_S`.
  :func:`periodic_reconcile` runs it from the api's 5-minute ops sampler (api/core/ops_metrics_sampler.py), so a
  removed address that confirms late is unsubscribed within one tick even when nobody opens the page.
- :func:`ops_recipients` — who the ``ops_alert_email`` job mails: the confirmed rows plus ``ALERT_EMAIL_TO`` (the
  Terraform fallback, always included, so the locked row on the page is true for both paths). Pending rows get
  nothing (no confirmation = no consent).

The Terraform bootstrap subscription (``ALERT_BOOTSTRAP_EMAIL``) is never stored in the table and never touched
here; it keeps receiving every alert if the app, the DB or this code is down. Every unsubscribe path checks the
subscription's SNS endpoint against it (remove: GetSubscriptionAttributes ``Endpoint``; reconcile: the listed
endpoint), and AA-CIS-Infra denies ``sns:Unsubscribe`` on that subscription ARN to the task role as well.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable, Mapping, Optional, Sequence

import structlog

logger = structlog.get_logger()

# ── Constants ────────────────────────────────────────────────────────────────────────────────────

MAX_ACTIVE = 10                 # pending + confirmed rows managed from the page
MAX_ADDS_PER_DAY = 10           # adds per rolling 24 h, all addresses together
RESEND_MIN_INTERVAL_S = 10 * 60
RESEND_MAX_PER_DAY = 3          # per row, the confirmation sent by the add included
TEST_MIN_INTERVAL_S = 5 * 60
PENDING_GRACE_S = 5 * 60        # SNS may not list a subscription right after Subscribe
DAY_S = 24 * 60 * 60
MAX_OPS_RECIPIENTS = 50         # SES: at most 50 recipients per message
MAX_EMAIL_LEN = 254
MAX_NOTE_LEN = 80
HISTORY_LIMIT = 20
RECONCILE_TTL_S = 60
RECONCILE_CACHE_KEY = "alert_recipients:reconcile"

RESOURCE_TYPE = "alert_recipient"
ACTOR_RECONCILE = "reconcile"
ACTOR_PERIODIC = "job:alert_recipients_reconcile"   # the api's ops sampler tick (removed_by / audit actor)

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REMOVED = "removed"
REASON_ADMIN = "admin"
REASON_UNSUBSCRIBED = "unsubscribed"
REASON_EXPIRED = "expired"
SEEN_CONFIRMED = "confirmed"
SEEN_PENDING = "pending"
SEEN_MISSING = "missing"

# What ListSubscriptionsByTopic puts in SubscriptionArn instead of an ARN.
SNS_PENDING = "PendingConfirmation"
SNS_DELETED = "Deleted"


class AlertAudit:
    """``acp_shared.audit_log.action`` values (resource_type ``alert_recipient``, resource_id the row id)."""
    ADDED = "alert_recipient.added"
    REMOVED = "alert_recipient.removed"
    CONFIRMATION_SENT = "alert_recipient.confirmation_sent"
    RECONCILED = "alert_recipient.reconciled"
    TEST_QUEUED = "alert_recipient.test_queued"


# ── E-mail ───────────────────────────────────────────────────────────────────────────────────────

# Same pattern as the table's alert_recipient_email_chk (migration 251).
_EMAIL_RE = re.compile(r'^[^@\s,;<>"]+@[^@\s,;<>"]+\.[^@\s,;<>"]+$')


def normalize_email(raw: Any) -> str:
    """One mailbox, stripped and lower-cased. Raises ValueError with a message for the admin."""
    if not isinstance(raw, str):
        raise ValueError("Enter an e-mail address")
    value = raw.strip().lower()
    if not value:
        raise ValueError("Enter an e-mail address")
    if len(value) > MAX_EMAIL_LEN:
        raise ValueError(f"The address is longer than {MAX_EMAIL_LEN} characters")
    if value.count("@") != 1:
        raise ValueError("Enter one e-mail address (exactly one @)")
    if not _EMAIL_RE.match(value):
        raise ValueError("Enter one e-mail address: no spaces, commas, semicolons, quotes or angle brackets, "
                         "and a domain with a dot")
    return value


def normalize_note(raw: Any) -> Optional[str]:
    """Optional free text (who this is, e.g. "on-call phone"); one line, at most MAX_NOTE_LEN characters."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("The note must be text")
    value = " ".join(raw.split())
    if not value:
        return None
    if len(value) > MAX_NOTE_LEN:
        raise ValueError(f"The note is longer than {MAX_NOTE_LEN} characters")
    return value


def _lower_or_none(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    return value or None


# ── Configuration (environment set by Terraform) ─────────────────────────────────────────────────

@dataclass(frozen=True)
class TopicConfig:
    topic_arn: str
    topic_name: str
    bootstrap_email: Optional[str]
    region: str

    def __repr__(self) -> str:   # never print the ARN (it carries the account id)
        return f"TopicConfig(topic_name={self.topic_name!r}, region={self.region!r})"


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"


def bootstrap_email() -> Optional[str]:
    """The Terraform-managed subscription (var.alert_email), shown locked on the page."""
    return _lower_or_none(os.environ.get("ALERT_BOOTSTRAP_EMAIL"))


def ops_fallback_email() -> Optional[str]:
    """ALERT_EMAIL_TO (var.cis_ops_alert_email): always on the ops e-mail, whatever the table says."""
    return _lower_or_none(os.environ.get("ALERT_EMAIL_TO"))


def topic_config() -> Optional[TopicConfig]:
    """The alerts topic from ALERTS_TOPIC_ARN, or None when it or ALERT_BOOTSTRAP_EMAIL is unset (page read-only,
    writes 503, no reconcile). Fail closed on a missing bootstrap: every guard that keeps the Terraform subscription
    out of reach compares against that address."""
    arn = (os.environ.get("ALERTS_TOPIC_ARN") or "").strip()
    bootstrap = bootstrap_email()
    if not arn or not bootstrap:
        return None
    return TopicConfig(topic_arn=arn, topic_name=arn.rsplit(":", 1)[-1], bootstrap_email=bootstrap,
                       region=_region())


# ── SNS ──────────────────────────────────────────────────────────────────────────────────────────

class SnsError(Exception):
    """An SNS call failed. ``code`` is the AWS error code (or the exception class for a network error); the
    message never carries the ARN."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str) and code:
            return code
    return type(exc).__name__


@lru_cache(maxsize=4)
def sns_client(region: str) -> Any:
    """A boto3 SNS client with short timeouts (the api answers within the API Gateway budget)."""
    import boto3
    from botocore.config import Config

    return boto3.client("sns", region_name=region,
                        config=Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 2}))


async def _call(client: Any, op: str, **kwargs: Any) -> dict:
    try:
        return await asyncio.to_thread(getattr(client, op), **kwargs)
    except Exception as exc:  # noqa: BLE001 — botocore ClientError, EndpointConnectionError, timeouts
        code = _error_code(exc)
        logger.warning("alert_recipients_sns_error", op=op, code=code)
        raise SnsError(code) from None


@dataclass(frozen=True)
class SnsSub:
    """One e-mail subscription as ListSubscriptionsByTopic shows it. ``arn`` is a real ARN, or the literal
    ``PendingConfirmation`` / ``Deleted``."""
    arn: str
    endpoint: str

    @property
    def state(self) -> str:
        return sub_state(self.arn)


def sub_state(arn: Optional[str]) -> str:
    if arn == SNS_PENDING:
        return SEEN_PENDING
    if not arn or arn == SNS_DELETED or not arn.startswith("arn:"):
        return SEEN_MISSING
    return SEEN_CONFIRMED


async def list_email_subscriptions(client: Any, topic_arn: str) -> list[SnsSub]:
    """Every e-mail subscription of the topic (all pages), endpoint lower-cased."""
    subs: list[SnsSub] = []
    token: Optional[str] = None
    for _ in range(100):   # 100 per page: far beyond any real list
        kwargs: dict[str, Any] = {"TopicArn": topic_arn}
        if token:
            kwargs["NextToken"] = token
        page = await _call(client, "list_subscriptions_by_topic", **kwargs)
        for s in page.get("Subscriptions") or []:
            if s.get("Protocol") != "email":
                continue
            subs.append(SnsSub(arn=str(s.get("SubscriptionArn") or ""), endpoint=str(s.get("Endpoint") or "").lower()))
        token = page.get("NextToken")
        if not token:
            break
    return subs


async def subscribe(client: Any, topic_arn: str, email: str) -> str:
    """Subscribe ``email`` (AWS sends the confirmation e-mail). Returns the subscription ARN (with
    ReturnSubscriptionArn AWS returns it even while pending). For an address that is still pending, calling it
    again is the resend; for a confirmed one it returns the existing ARN and sends nothing."""
    out = await _call(client, "subscribe", TopicArn=topic_arn, Protocol="email", Endpoint=email,
                      ReturnSubscriptionArn=True)
    return str(out.get("SubscriptionArn") or "")


async def unsubscribe(client: Any, subscription_arn: str) -> None:
    await _call(client, "unsubscribe", SubscriptionArn=subscription_arn)


@dataclass(frozen=True)
class SubInfo:
    """GetSubscriptionAttributes of one subscription: pending or not, and its endpoint (lower case; None when SNS
    did not return it)."""
    pending: bool
    endpoint: Optional[str]


async def subscription_info(client: Any, subscription_arn: str) -> Optional[SubInfo]:
    """GetSubscriptionAttributes ``PendingConfirmation`` + ``Endpoint``; None when SNS no longer has it."""
    try:
        out = await _call(client, "get_subscription_attributes", SubscriptionArn=subscription_arn)
    except SnsError as exc:
        if exc.code == "NotFound":
            return None
        raise
    attrs = out.get("Attributes") or {}
    return SubInfo(pending=str(attrs.get("PendingConfirmation", "false")).lower() == "true",
                   endpoint=_lower_or_none(attrs.get("Endpoint")))


async def subscription_pending(client: Any, subscription_arn: str) -> Optional[bool]:
    """True / False from GetSubscriptionAttributes ``PendingConfirmation``; None when SNS no longer has it."""
    info = await subscription_info(client, subscription_arn)
    return None if info is None else info.pending


# ── Reconcile: plan (pure) ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RowUpdate:
    """What reconcile does to one active row. ``changed`` = the status moves."""
    id: str
    email: str
    from_status: str
    to_status: str
    seen: str
    removed_reason: Optional[str] = None
    subscription_arn: Optional[str] = None   # a newly learnt ARN (confirmed in SNS)

    @property
    def changed(self) -> bool:
        return self.from_status != self.to_status


@dataclass(frozen=True)
class Unsubscribe:
    """An address the admin removed whose confirmation was clicked afterwards: SNS has it confirmed again."""
    row_id: str
    email: str
    arn: str


@dataclass
class ReconcilePlan:
    updates: list[RowUpdate] = field(default_factory=list)
    unsubscribe: list[Unsubscribe] = field(default_factory=list)
    locked: Optional[dict] = None              # the bootstrap subscription: {email, state}
    unmanaged: list[dict] = field(default_factory=list)   # [{email, state}] added outside the page
    # [{email, state: pending}] removed on the page while unconfirmed: SNS still lists the pending subscription until
    # it expires (3 days); if it is confirmed first, the next reconcile unsubscribes it (``unsubscribe`` above).
    leaving: list[dict] = field(default_factory=list)

    @property
    def changes(self) -> list[RowUpdate]:
        return [u for u in self.updates if u.changed]


_STATE_RANK = {SEEN_CONFIRMED: 2, SEEN_PENDING: 1, SEEN_MISSING: 0}


def _best_subs(subs: Iterable[SnsSub]) -> dict[str, SnsSub]:
    """Per endpoint, the most useful subscription (confirmed over pending; Deleted ones are ignored)."""
    best: dict[str, SnsSub] = {}
    for s in subs:
        if s.state == SEEN_MISSING:
            continue
        cur = best.get(s.endpoint)
        if cur is None or _STATE_RANK[s.state] > _STATE_RANK[cur.state]:
            best[s.endpoint] = s
    return best


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def plan_reconcile(rows: Sequence[Mapping[str, Any]], subs: Sequence[SnsSub], bootstrap: Optional[str],
                   now: datetime) -> ReconcilePlan:
    """What to change so the table matches SNS.

    ``rows``: every active row (pending / confirmed) plus, per address, the latest removed row (the caller may pass
    more removed rows; only those without an active row for the same address count). ``subs``: the topic's e-mail
    subscriptions. ``bootstrap``: ALERT_BOOTSTRAP_EMAIL (lower case) or None. Without a bootstrap address nothing
    is planned for unsubscribe (fail closed: the locked subscription could not be told apart).
    """
    bootstrap = _lower_or_none(bootstrap)
    best = _best_subs(subs)
    plan = ReconcilePlan()
    active_emails: set[str] = set()
    removed_by_email: dict[str, Mapping[str, Any]] = {}

    for row in rows:
        email = str(row["email"]).lower()
        status = row["status"]
        if status == STATUS_REMOVED:
            removed_by_email.setdefault(email, row)
            continue
        active_emails.add(email)
        sub = best.get(email)
        if sub is not None and sub.state == SEEN_CONFIRMED:
            new_arn = sub.arn if sub.arn != row.get("subscription_arn") else None
            plan.updates.append(RowUpdate(str(row["id"]), email, status, STATUS_CONFIRMED, SEEN_CONFIRMED,
                                          subscription_arn=new_arn))
        elif sub is not None:   # pending in SNS
            plan.updates.append(RowUpdate(str(row["id"]), email, status, STATUS_PENDING, SEEN_PENDING))
        else:
            sent = _aware(row.get("last_confirmation_sent_at"))
            if sent is not None and (now - sent).total_seconds() < PENDING_GRACE_S:
                plan.updates.append(RowUpdate(str(row["id"]), email, status, status, SEEN_MISSING))
            else:
                reason = REASON_EXPIRED if status == STATUS_PENDING else REASON_UNSUBSCRIBED
                plan.updates.append(RowUpdate(str(row["id"]), email, status, STATUS_REMOVED, SEEN_MISSING,
                                              removed_reason=reason))

    for email, sub in best.items():
        if email in active_emails:
            continue
        if bootstrap and email == bootstrap:
            continue
        removed = removed_by_email.get(email)
        removed_here = removed is not None and removed.get("removed_reason") == REASON_ADMIN
        if removed_here and sub.state == SEEN_CONFIRMED and bootstrap:
            plan.unsubscribe.append(Unsubscribe(str(removed["id"]), email, sub.arn))
        elif removed_here and sub.state == SEEN_PENDING:
            plan.leaving.append({"email": email, "state": sub.state})
        else:
            plan.unmanaged.append({"email": email, "state": sub.state})
    plan.unmanaged.sort(key=lambda u: u["email"])
    plan.leaving.sort(key=lambda u: u["email"])

    if bootstrap:
        sub = best.get(bootstrap)
        plan.locked = {"email": bootstrap, "state": sub.state if sub is not None else SEEN_MISSING}
    return plan


# ── Reconcile: apply ─────────────────────────────────────────────────────────────────────────────

_ROW = ("id::text AS id, email, status, removed_reason, subscription_arn, confirmed_at, last_confirmation_sent_at, "
        "created_at")
SQL_ACTIVE_FOR_UPDATE = (f"SELECT {_ROW} FROM shared.alert_recipient WHERE status <> 'removed' "
                         "ORDER BY created_at FOR UPDATE")
SQL_LATEST_REMOVED = (f"SELECT DISTINCT ON (email) {_ROW} FROM shared.alert_recipient WHERE status = 'removed' "
                      "ORDER BY email, removed_at DESC")
# $1 id, $2 new status, $3 removed_reason, $4 removed_by, $5 newly learnt ARN (NULL = keep), $6 seen state
SQL_RECONCILE_UPDATE = (
    "UPDATE shared.alert_recipient SET status = $2::text, removed_reason = $3::text, "
    "removed_at = CASE WHEN $2::text = 'removed' THEN coalesce(removed_at, now()) ELSE NULL END, "
    "removed_by = CASE WHEN $2::text = 'removed' THEN $4::text ELSE NULL END, "
    "subscription_arn = coalesce($5::text, subscription_arn), "
    "confirmed_at = CASE WHEN $2::text = 'confirmed' THEN coalesce(confirmed_at, now()) ELSE confirmed_at END, "
    "last_seen_state = $6::text, last_reconciled_at = now(), updated_at = now() "
    "WHERE id = $1::uuid"
)
SQL_RECONCILE_SEEN = (
    "UPDATE shared.alert_recipient SET last_seen_state = $2::text, last_reconciled_at = now() WHERE id = $1::uuid"
)
SQL_CONFIRMED_EMAILS = "SELECT email FROM shared.alert_recipient WHERE status = 'confirmed' ORDER BY created_at"

# Reads for the page. created_by / removed_by are 'admin:<uuid>' (or reconcile / job:…); names come from the staff
# directory, never password_hash or the Cognito identity. subscription_arn is reduced to a boolean.
_PAGE_COLUMNS = (
    "r.id::text AS id, r.email, r.note, r.status, r.removed_reason, (r.subscription_arn IS NOT NULL) AS "
    "has_subscription, r.created_at, r.created_by, r.confirmed_at, r.removed_at, r.removed_by, "
    "r.last_confirmation_sent_at, r.last_seen_state, r.last_reconciled_at, "
    "coalesce(c.display_name, c.email, c.username) AS created_by_name, "
    "coalesce(d.display_name, d.email, d.username) AS removed_by_name"
)
_PAGE_FROM = (
    "FROM shared.alert_recipient r "
    "LEFT JOIN shared.admin_users c ON r.created_by = 'admin:' || c.id::text "
    "LEFT JOIN shared.admin_users d ON r.removed_by = 'admin:' || d.id::text"
)
SQL_LIST_ACTIVE = f"SELECT {_PAGE_COLUMNS} {_PAGE_FROM} WHERE r.status <> 'removed' ORDER BY r.created_at"
SQL_LIST_HISTORY = (f"SELECT {_PAGE_COLUMNS} {_PAGE_FROM} WHERE r.status = 'removed' "
                    f"ORDER BY r.removed_at DESC LIMIT {HISTORY_LIMIT}")
SQL_GET_ONE = f"SELECT {_PAGE_COLUMNS} {_PAGE_FROM} WHERE r.id = $1::uuid"


@dataclass
class ReconcileResult:
    reconciled_at: Optional[str] = None
    error: Optional[str] = None
    changes: list[dict] = field(default_factory=list)
    unsubscribed: list[str] = field(default_factory=list)
    locked: Optional[dict] = None
    unmanaged: list[dict] = field(default_factory=list)
    leaving: list[dict] = field(default_factory=list)
    cached: bool = False

    def summary(self) -> dict:
        """What the page shows; cached between reconciles (no ARN in it)."""
        return {"reconciled_at": self.reconciled_at, "locked": self.locked, "unmanaged": self.unmanaged,
                "leaving": self.leaving}


async def reconcile(pool: Any, client: Any, cfg: TopicConfig, *, actor: str = ACTOR_RECONCILE,
                    now: Optional[datetime] = None) -> ReconcileResult:
    """List the topic's e-mail subscriptions once, then apply :func:`plan_reconcile` in one transaction (active rows
    locked FOR UPDATE), one ``alert_recipient.reconciled`` audit row per status change. Re-unsubscribes run after
    the commit. Raises SnsError when SNS cannot be listed (nothing changed then)."""
    from services.acp_shared.audit_log import write_audit_log

    subs = await list_email_subscriptions(client, cfg.topic_arn)
    when = now or datetime.now(timezone.utc)
    # removed_by says what removed the row: SNS (seen by a status check), never the admin whose page view ran the
    # check; the audit row keeps that admin as the actor. A job keeps its own name.
    removed_by = actor if actor.startswith("job:") else ACTOR_RECONCILE
    async with pool.acquire() as conn:
        async with conn.transaction():
            active = await conn.fetch(SQL_ACTIVE_FOR_UPDATE)
            removed = await conn.fetch(SQL_LATEST_REMOVED)
            plan = plan_reconcile([dict(r) for r in active] + [dict(r) for r in removed], subs,
                                  cfg.bootstrap_email, when)
            for u in plan.updates:
                if u.changed or u.subscription_arn:
                    await conn.execute(SQL_RECONCILE_UPDATE, u.id, u.to_status, u.removed_reason,
                                       removed_by if u.to_status == STATUS_REMOVED else None, u.subscription_arn,
                                       u.seen)
                else:
                    await conn.execute(SQL_RECONCILE_SEEN, u.id, u.seen)
            for u in plan.changes:   # audit rows last (the audit chain trigger's lock, QRY-4)
                await write_audit_log(conn, tenant_id=None, actor=actor, action=AlertAudit.RECONCILED,
                                      resource_type=RESOURCE_TYPE, resource_id=u.id,
                                      details={"email": u.email, "from": u.from_status, "to": u.to_status,
                                               "reason": u.removed_reason})
    result = ReconcileResult(
        reconciled_at=when.isoformat(), locked=plan.locked, unmanaged=list(plan.unmanaged),
        leaving=list(plan.leaving),
        changes=[{"id": u.id, "email": u.email, "from": u.from_status, "to": u.to_status,
                  "reason": u.removed_reason} for u in plan.changes])
    for un in plan.unsubscribe:
        if not cfg.bootstrap_email or un.email == cfg.bootstrap_email:   # belt and braces: never the locked one
            continue
        try:
            await unsubscribe(client, un.arn)
            outcome = "unsubscribed"
        except SnsError as exc:
            outcome = "not_found" if exc.code == "NotFound" else f"error:{exc.code}"
        result.unsubscribed.append(un.email)
        await write_audit_log(pool, tenant_id=None, actor=actor, action=AlertAudit.RECONCILED,
                              resource_type=RESOURCE_TYPE, resource_id=un.row_id,
                              details={"email": un.email, "from": STATUS_REMOVED, "to": STATUS_REMOVED,
                                       "reason": "confirmed_after_removal", "sns": outcome})
    if result.changes or result.unsubscribed:
        logger.info("alert_recipients_reconciled", topic=cfg.topic_name, changes=len(result.changes),
                    unsubscribed=len(result.unsubscribed))
    return result


# Throttle + cache of the last summary: Redis when the caller has it (api app.state.redis), else this process.
_local_cache: dict[str, tuple[float, dict]] = {}


async def _cache_get(redis: Any) -> Optional[dict]:
    if redis is not None:
        try:
            raw = await redis.get(RECONCILE_CACHE_KEY)
            if raw:
                return json.loads(raw)
            return None
        except Exception as exc:  # noqa: BLE001 — Redis down: fall back to this process
            logger.warning("alert_recipients_cache_unavailable", error=type(exc).__name__)
    hit = _local_cache.get(RECONCILE_CACHE_KEY)
    if hit and time.monotonic() - hit[0] < RECONCILE_TTL_S:
        return hit[1]
    return None


async def _cache_set(redis: Any, summary: dict) -> None:
    _local_cache[RECONCILE_CACHE_KEY] = (time.monotonic(), summary)
    if redis is not None:
        try:
            await redis.set(RECONCILE_CACHE_KEY, json.dumps(summary), ex=RECONCILE_TTL_S)
        except Exception as exc:  # noqa: BLE001 — the cache is an optimisation
            logger.warning("alert_recipients_cache_unavailable", error=type(exc).__name__)


def clear_cache() -> None:
    """This process's copy only (tests). After a change use :func:`invalidate_cache`, which also drops Redis."""
    _local_cache.clear()


async def invalidate_cache(redis: Any = None) -> None:
    """Drop the cached summary everywhere, so the next page view reconciles (an add or remove changes what the
    ``unmanaged`` / ``leaving`` lists should say). A Redis error is logged; the key expires within the TTL anyway."""
    _local_cache.clear()
    if redis is not None:
        try:
            await redis.delete(RECONCILE_CACHE_KEY)
        except Exception as exc:  # noqa: BLE001 — the cache is an optimisation
            logger.warning("alert_recipients_cache_unavailable", error=type(exc).__name__)


async def reconcile_throttled(pool: Any, client: Any, cfg: TopicConfig, *, redis: Any = None, force: bool = False,
                              actor: str = ACTOR_RECONCILE) -> ReconcileResult:
    """:func:`reconcile` at most once per RECONCILE_TTL_S (``force`` skips the throttle). Never raises for SNS:
    the error code is in ``result.error`` and the table is left as it was."""
    if not force:
        cached = await _cache_get(redis)
        if cached is not None:
            return ReconcileResult(reconciled_at=cached.get("reconciled_at"), locked=cached.get("locked"),
                                   unmanaged=list(cached.get("unmanaged") or []),
                                   leaving=list(cached.get("leaving") or []), cached=True)
    try:
        result = await reconcile(pool, client, cfg, actor=actor)
    except SnsError as exc:
        return ReconcileResult(error=exc.code)
    await _cache_set(redis, result.summary())
    return result


async def periodic_reconcile(pool: Any, *, redis: Any = None, client: Any = None) -> Optional[ReconcileResult]:
    """One scheduled reconcile (the api's ops sampler, every OPS_METRICS_INTERVAL_S): None when the topic is not
    configured. Throttled like a page view, so a tick right after a page view costs nothing. Without it, an address
    removed while pending that confirms later would keep getting alerts until someone opened the page."""
    cfg = topic_config()
    if cfg is None:
        return None
    if client is None:
        client = await asyncio.to_thread(sns_client, cfg.region)
    return await reconcile_throttled(pool, client, cfg, redis=redis, actor=ACTOR_PERIODIC)


# ── Ops e-mail recipients ────────────────────────────────────────────────────────────────────────

def merge_recipients(confirmed: Iterable[str], fallback: Optional[str]) -> list[str]:
    """Confirmed rows in order, then ALERT_EMAIL_TO; lower case, no duplicates, at most MAX_OPS_RECIPIENTS."""
    out: list[str] = []
    for email in [*confirmed, fallback]:
        value = _lower_or_none(email)
        if value and value not in out:
            out.append(value)
    return out[:MAX_OPS_RECIPIENTS]


async def ops_recipients(pool: Any) -> tuple[list[str], Optional[str]]:
    """Who the ops_alert_email job mails, and the read error (None when the table was read). A table that cannot
    be read (DB down, migration 251 not applied yet) still yields ALERT_EMAIL_TO."""
    try:
        rows = await pool.fetch(SQL_CONFIRMED_EMAILS)
        confirmed = [str(r["email"]) for r in rows]
        error = None
    except Exception as exc:  # noqa: BLE001 — any read failure: fall back to ALERT_EMAIL_TO
        logger.warning("alert_recipients_read_failed", error=type(exc).__name__)
        confirmed, error = [], type(exc).__name__
    return merge_recipients(confirmed, ops_fallback_email()), error
