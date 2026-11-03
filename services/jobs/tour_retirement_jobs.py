"""AA-1039 R6 slice 2 (root ADR 0019 d20) — the three job kinds of the tour retirement fan-out ($0, no LLM):

- ``tour_retirement_fanout``  one per ``retired`` / ``restored`` lifecycle event (idempotency key
  ``tour_retirement_fanout:<event idempotency_key>``): services/tour_lifecycle/fanout.fan_out (flags, cancelled
  publish claims, notices), then one ``tour_notice_email`` per notice to send at once. Enqueued right after the
  lifecycle action commits (services/tour_lifecycle/effects.run_after_commit) and swept by the api ops sampler
  (:func:`sweep`, events of the last 2 h without a job), like ``tripplanner_purge``.
- ``retired_page_draft``  one per open WordPress flag whose notice period ended (key ``retired_page_draft:<flag>``),
  enqueued due now by the sweep (never queued days ahead: the deploy gate and the migration idle check count queued
  rows): fanout.move_to_draft.
- ``tour_notice_email``  the SES mail (services/tour_lifecycle/notice_mail.py): payload ``{"notice_id"}`` for one
  immediate notice, or ``{"digest_day"}`` for the daily digest (every unmailed notice of the last 14 days, one mail
  per tenant; queued once a UTC day from 08:00 by the sampler). SES not configured -> ``skipped``; no recipient or
  the partner pool unreadable -> the notices wait for the next digest (an immediate one turns into a digest one).
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from typing import Optional

import asyncpg
import structlog

from services.tour_lifecycle import fanout, notice_mail
from shared.aws_client import ses_mailer
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

FANOUT_KIND = "tour_retirement_fanout"
DRAFT_KIND = "retired_page_draft"
MAIL_KIND = "tour_notice_email"
FANOUT_ATTEMPTS = 5
DRAFT_ATTEMPTS = 4
MAIL_ATTEMPTS = 4
DIGEST_HOUR_UTC = 8
DIGEST_WINDOW_DAYS = 14
SWEEP_WINDOW_S = 7200
SWEEP_LAG_S = 30

SWEEP_SQL = f"""
    WITH created AS (
    INSERT INTO shared.job (kind, payload, idempotency_key, max_attempts, created_by)
    SELECT '{FANOUT_KIND}', jsonb_build_object('event_key', e.idempotency_key, 'trigger', 'sweep'),
           '{FANOUT_KIND}:' || e.idempotency_key, {FANOUT_ATTEMPTS}, 'system:{FANOUT_KIND}_sweep'
      FROM gold_aa_internal.tour_lifecycle_event e
     WHERE e.event IN ('retired', 'restored')
       AND e.occurred_at > now() - make_interval(secs => $1)
       AND e.occurred_at < now() - make_interval(secs => $2)
       AND NOT EXISTS (SELECT 1 FROM shared.job j WHERE j.idempotency_key = '{FANOUT_KIND}:' || e.idempotency_key)
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
    RETURNING 1
    )
    SELECT count(*) FROM created
"""

NOTICE_SQL = """
    SELECT n.notice_id::text AS notice_id, n.tenant_id::text AS tenant_id, n.event, n.tour_name, n.delivery,
           n.summary, n.mailed_at, n.created_at
      FROM acp_shared.tenant_tour_notice n
"""

MARK_MAILED_SQL = """
    UPDATE acp_shared.tenant_tour_notice
       SET mailed_at = now(), mail_job_id = $2::uuid, message_id = $3, skipped_reason = NULL
     WHERE notice_id = ANY($1::uuid[]) AND mailed_at IS NULL
"""

DEFER_SQL = """
    UPDATE acp_shared.tenant_tour_notice SET delivery = 'digest', skipped_reason = $2
     WHERE notice_id = ANY($1::uuid[]) AND mailed_at IS NULL
"""


def _summary(raw) -> dict:
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw or {})


# ── enqueue ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def enqueue_for_events(db, events, *, trigger: str = "after_commit") -> int:
    """One fan-out job per ``retired`` / ``restored`` event of a lifecycle outcome (best-effort; the sweep backs it)."""
    n = 0
    for e in events or []:
        if e.get("event") not in ("retired", "restored") or not e.get("idempotency_key"):
            continue
        _, created = await enqueue(db, FANOUT_KIND, {"event_key": e["idempotency_key"], "trigger": trigger},
                                   idempotency_key=f"{FANOUT_KIND}:{e['idempotency_key']}",
                                   created_by=f"system:{FANOUT_KIND}")
        n += 1 if created else 0
    return n


async def sweep(pool) -> int:
    return int(await pool.fetchval(SWEEP_SQL, SWEEP_WINDOW_S, SWEEP_LAG_S) or 0)


async def enqueue_due_drafts(pool) -> int:
    return await fanout.enqueue_due_drafts(pool, DRAFT_ATTEMPTS)


def digest_key(day: date) -> str:
    return f"{MAIL_KIND}:digest:{day.isoformat()}"


async def enqueue_daily_digest(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool]:
    now = now or datetime.now(timezone.utc)
    if now.hour < DIGEST_HOUR_UTC:
        return None, False
    key = digest_key(now.date())
    if await pool.fetchval("SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)", key):
        return None, False
    return await enqueue(pool, MAIL_KIND, {"digest_day": now.date().isoformat(), "trigger": "schedule"},
                         idempotency_key=key, created_by=f"scheduler:{MAIL_KIND}")


async def tick(pool, now: Optional[datetime] = None) -> dict:
    """The api ops sampler's part: the fan-out sweep, the due WordPress drafts, the daily digest. Each step on its
    own; the counts that changed are returned for the `ops_metrics_sampled` line."""
    out: dict = {}
    swept = await sweep(pool)
    if swept:
        out["fanout_swept"] = swept
    drafts = await enqueue_due_drafts(pool)
    if drafts:
        out["drafts_due"] = drafts
    job_id, created = await enqueue_daily_digest(pool, now)
    if created:
        out["digest_job"] = job_id
    return out


# ── handlers ───────────────────────────────────────────────────────────────────────────────────────────────────────

@job_kind(FANOUT_KIND, concurrency=2, max_attempts=FANOUT_ATTEMPTS, expected_seconds=120)
async def run_fanout(ctx: JobContext) -> dict:
    key = (ctx.payload or {}).get("event_key")
    if not key:
        raise NonRetryable("payload.event_key is required")
    result = await fanout.fan_out(ctx.pool, str(key))
    mails = []
    for notice_id in fanout.immediate_ids(result):
        job_id, _ = await enqueue(ctx.pool, MAIL_KIND, {"notice_id": notice_id, "trigger": FANOUT_KIND},
                                  idempotency_key=f"{MAIL_KIND}:{notice_id}", parent_job_id=str(ctx.job_id),
                                  created_by=f"job:{FANOUT_KIND}")
        mails.append(job_id)
    result = {k: v for k, v in result.items() if k != "immediate_notices"}
    result.update({"event_key": key, "mail_jobs": len(mails), "trigger": (ctx.payload or {}).get("trigger")})
    ctx.set_result(result)
    return result


async def _read_secret(name: str) -> dict:
    from api.routers.v1_publish import _get_secret
    return await asyncio.to_thread(_get_secret, name)


def _wordpress(creds: dict):
    from services.acp_s4_blog.cms.wordpress import WordPressAdapter
    return WordPressAdapter(wp_url=creds["wp_url"], username=creds["username"], app_password=creds["app_password"])


@job_kind(DRAFT_KIND, concurrency=2, max_attempts=DRAFT_ATTEMPTS, expected_seconds=120)
async def run_draft(ctx: JobContext) -> dict:
    flag_id = (ctx.payload or {}).get("flag_id")
    if not flag_id:
        raise NonRetryable("payload.flag_id is required")
    read_secret = ctx.resources.get("read_secret") or _read_secret
    adapter_for = ctx.resources.get("wordpress") or _wordpress
    try:
        result = await fanout.move_to_draft(ctx.pool, str(flag_id), read_secret=read_secret, adapter_for=adapter_for)
    except fanout.DraftRefused as exc:
        if exc.permanent:
            raise NonRetryable(f"page not moved: {exc.reason}") from exc
        raise RuntimeError(f"page not moved yet: {exc.reason}") from exc
    if result.get("status") == "drafted":
        from services.acp_shared.audit_log import TenantAuditAction, write_audit_log
        try:
            await write_audit_log(
                ctx.pool, tenant_id=result["tenant_id"], actor=fanout.ACTOR,
                action=TenantAuditAction.CONTENT_UNPUBLISHED, resource_type="content_piece",
                resource_id=result["piece_id"],
                details={"channel": "blog", "publish_id": result["publish_id"], "reason": "tour_retired",
                         "wordpress": result.get("outcome"), "flag_id": str(flag_id)})
        except Exception:  # noqa: BLE001 — the page moved; a lost audit row is logged only
            logger.warning("retired_page_draft_audit_failed", flag_id=str(flag_id), exc_info=True)
    ctx.set_result(result)
    return result


async def _load_notices(pool, *, notice_id: Optional[str] = None) -> list[dict]:
    async with pool.acquire() as conn:
        if notice_id:
            rows = await conn.fetch(NOTICE_SQL + " WHERE n.notice_id = $1::uuid", notice_id)
        else:
            rows = await conn.fetch(
                NOTICE_SQL + " WHERE n.mailed_at IS NULL AND n.created_at > now() - make_interval(days => $1)"
                             " ORDER BY n.tenant_id, n.created_at", DIGEST_WINDOW_DAYS)
    return [{**dict(r), "summary": _summary(r["summary"])} for r in rows]


async def _send_tenant(ctx: JobContext, cfg, tenant_id: str, notices: list[dict], ses) -> dict:
    ids = [n["notice_id"] for n in notices]
    agency = await notice_mail.tenant_name(ctx.pool, tenant_id)
    if agency is None:
        await ctx.pool.execute(DEFER_SQL, ids, "tenant not active")
        return {"tenant_id": tenant_id, "status": "skipped", "reason": "tenant_not_active"}
    try:
        to = await notice_mail.recipients(ctx.pool, tenant_id, client=ctx.resources.get("cognito"))
    except notice_mail.RecipientsUnavailable as exc:
        await ctx.pool.execute(DEFER_SQL, ids, f"recipients unavailable: {exc}"[:300])
        return {"tenant_id": tenant_id, "status": "deferred", "reason": "recipients_unavailable"}
    if not to:
        await ctx.pool.execute(DEFER_SQL, ids, "no owner or content member with a verified address")
        return {"tenant_id": tenant_id, "status": "deferred", "reason": "no_recipient"}
    message = notice_mail.render(notices, to=tuple(to), agency=agency)
    try:
        message_id = await ses_mailer.send(ses, message, sender=cfg.sender, configuration_set=cfg.configuration_set)
    except ses_mailer.MailError as exc:
        if exc.permanent:
            await ctx.pool.execute(DEFER_SQL, ids, f"ses refused: {exc.code}"[:300])
            return {"tenant_id": tenant_id, "status": "refused", "code": exc.code}
        raise
    await ctx.pool.execute(MARK_MAILED_SQL, ids, str(ctx.job_id), message_id)
    return {"tenant_id": tenant_id, "status": "sent", "notices": len(ids), "recipients": len(to),
            "message_id": message_id}


@job_kind(MAIL_KIND, concurrency=1, max_attempts=MAIL_ATTEMPTS, expected_seconds=120)
async def run_mail(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    cfg = ses_mailer.sender_config()
    if cfg is None:
        result = {"status": "skipped", "reason": "SES_FROM_ADDRESS unset"}
        ctx.set_result(result)
        return result
    try:
        if payload.get("notice_id"):
            notices = [n for n in await _load_notices(ctx.pool, notice_id=str(payload["notice_id"]))
                       if n["mailed_at"] is None]
            mode = "immediate"
        elif payload.get("digest_day"):
            notices = [n for n in await _load_notices(ctx.pool) if n["delivery"] == "digest"]
            mode = "digest"
        else:
            raise NonRetryable("payload needs notice_id or digest_day")
    except asyncpg.UndefinedTableError as exc:
        raise NonRetryable("CIS 362 not applied") from exc
    by_tenant: dict[str, list[dict]] = {}
    for n in notices:
        by_tenant.setdefault(n["tenant_id"], []).append(n)
    ses = ctx.resources.get("ses") or ses_mailer.ses_client(cfg.region)
    sent = [await _send_tenant(ctx, cfg, tid, items, ses) for tid, items in sorted(by_tenant.items())]
    result = {"status": "done", "mode": mode, "tenants": len(sent),
              "sent": sum(1 for s in sent if s["status"] == "sent"), "results": sent[:50]}
    ctx.set_result(result)
    return result


__all__ = ["DRAFT_KIND", "FANOUT_KIND", "MAIL_KIND", "enqueue_daily_digest", "enqueue_due_drafts",
           "enqueue_for_events", "run_draft", "run_fanout", "run_mail", "sweep", "tick"]
