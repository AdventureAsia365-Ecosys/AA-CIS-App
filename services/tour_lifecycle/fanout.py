"""AA-1039 R6 slice 2 (root ADR 0019 d20, clarifications TL-20): what a retired / restored tour does to tenant content.

The event is the CIS change record ``gold_aa_internal.tour_lifecycle_event`` (ADR 0015 decision 4, CIS 272; the
contract view ``catalog.v_tour_lifecycle_outbox``, "catalog.tour_liveness_changed" in ADR 0019 d20): the lifecycle
service writes it in the transaction of the change and is the only writer. One ``tour_retirement_fanout`` job per
``retired`` / ``restored`` event (services/jobs/tour_retirement_jobs.py; enqueued after commit, swept like the
TripPlanner purge) runs :func:`fan_out`:

``retired`` (only while the key is still retired when the job runs):
  - every live post of a piece about the key's tours (``publish_log`` published, not unpublished; the piece's request
    is about one of the tours) gets one ``tour_retired`` row in ``acp_shared.content_compliance_flag`` (CIS 360):
    WordPress ``draft_after_notice`` with ``draft_due_at`` = event time + the notice period
    (``acp_shared.tour_retirement_policy``, default 7 days), Facebook ``notice_only`` (nothing is deleted);
  - publish claims left ``requested`` past the stale window are closed as ``failed`` ("cancelled: ..."); new publishes
    of these pieces are already refused (409 ``tour_discontinued``, AA-986 L3), so nothing else is pending;
  - every tenant with a derivative (the tenants the service warned with ``tour.discontinued``, the bell) gets one
    ``acp_shared.tenant_tour_notice`` row: ``immediate`` when it still has live posts (a ``tour_notice_email`` job now),
    else ``digest`` (the daily mail).
  A second ``retired`` event of a key that is already retired (only the reason changed, e.g. the Master was trashed and
  then the source deleted) adds nothing: a post with an open ``tour_retired`` flag of an earlier event keeps it (its
  draft date stands), and a tenant whose latest notice for the tours is ``retired`` gets no second one.
``restored``: the open ``tour_retired`` flags of the key's tours are resolved (``restored``); every tenant whose latest
notice for them is ``retired`` (still told "no longer offered") gets a ``restored`` notice ("back on sale"), at once
when it had live posts flagged. A restore that follows a pause or a supersede, or a second restore, mails nobody.
  The WordPress pages the retirement this restore undoes moved from ``publish`` to draft (flags resolved ``drafted``
  of a ``retired`` event of the key since its previous ``restored`` event, with ``wordpress_before = 'publish'``) are
  marked ``detail.restore_republish`` and get one ``restored_page_republish`` job each, inserted in this transaction
  (:func:`republish_page`): the page is published again only when the tour, the piece and the same remote draft still
  allow it. A page drafted before that was recorded stays in draft and the tenant is told. The tenant's notice is then
  ``immediate`` but waits for those jobs (``summary.pages_returning``), so the mail says which pages are back.
Served means the liveness state ``served`` (:func:`served`); a key without a state is not served, in every step.
One fan-out of a key runs at a time (a transaction advisory lock on the key).
``paused`` and ``superseded`` do nothing here (ADR 0019 d20: a paused tour gets no mail; a supersede is invisible).

The WordPress move to draft runs at ``draft_due_at`` as a ``retired_page_draft`` job (:func:`move_to_draft`),
enqueued due-now by the sweep (:data:`DUE_DRAFTS_SQL`) so no row waits queued for days in the job table (the deploy
gate and the migration idle check count queued rows). It moves only a post WordPress says is ``publish``, and its
final write holds the key's fan-out lock (a restore that resolved the flag meanwhile gets the page published back).

Flags are admin data: the tenant sees the bell, the e-mail and its content, never a flag (Tenant content QA ›
Compliance). Booking and TripPlanner read the served state themselves (Booking 409 ``tour_not_served`` on quotes and
holds, TripPlanner the purge notice + ``catalog.v_tour``); the CRM marks lines "no longer offered" from its hourly
catalog sync. Before CIS 360 is applied :func:`fan_out` raises :class:`SchemaMissing` (the job retries, and the
sweep re-queues the jobs it failed once the tables exist, so no event is lost); the other functions are no-ops.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

import asyncpg
import structlog

from shared import tour_liveness as _tl

from . import derivatives as _d

logger = structlog.get_logger()

# The CIS migration that creates the flag, notice and policy tables (named in errors and in the admin 503).
MIGRATION = "360"
SCHEMA_MISSING = f"fanout_schema_missing: CIS {MIGRATION} (acp_shared.content_compliance_flag) is not applied"
DEFAULT_NOTICE_DAYS = 7
STALE_REQUESTED_SECONDS = 900      # a publish claim this old without an answer is abandoned (idempotency.py: 300 s)
ACTOR = "system:tour_retirement"
FLAG_KIND = "tour_retired"
# A restore puts back the WordPress pages its retirement moved to draft (one job per page, :func:`republish_page`).
REPUBLISH_KIND = "restored_page_republish"
REPUBLISH_ATTEMPTS = 4
REPUBLISH_MARK = "restore_republish"     # the flag's detail key: {event_key, state, queued_at, done_at, reason, ...}
# publish_log.channel of a WordPress post: 'blog' (the T11 channel); older rows say 'wordpress'.
WORDPRESS_CHANNELS = ("blog", "wordpress")

EVENT_SQL = """
    SELECT e.id AS event_id, e.source_id, e.tour_id::text AS tour_id, e.event, e.reason, e.occurred_at,
           e.replaced_by_tour_id::text AS replaced_by_tour_id, e.idempotency_key
      FROM gold_aa_internal.tour_lifecycle_event e
     WHERE e.idempotency_key = $1
"""

KEY_TOURS_SQL = """
    SELECT rt.tour_id::text AS tour_id
      FROM silver_aa_internal.raw_tours rt
     WHERE (COALESCE(rt.source_group_id, rt.tour_id))::text = $1
"""

STATE_SQL = "SELECT tl.state, tl.reason FROM gold_aa_internal.v_tour_liveness tl WHERE tl.source_id = $1"

TOUR_NAME_SQL = """
    SELECT COALESCE(
        (SELECT pt.aa_name FROM gold_aa_internal.published_tours pt WHERE pt.tour_id = $1::uuid),
        (SELECT rt.src_name FROM silver_aa_internal.raw_tours rt WHERE rt.tour_id = $1::uuid)) AS name
"""

# The pieces of the tours ($1 uuid[]): the request's trip, else its atom's tour (the derivative rule of the service),
# under the Subject rule (AA-1057 GAP 1, shared.tour_liveness.requests_about_tours_sql): a piece on a Segment Subject
# whose Segment still has a served member on another tour is not about this event (no flag, no cancelled claim, no
# mail); one whose representative tour left earlier is, when this event takes the last served member. Its flag then
# names a tour of this key (`tour_id`), so the key's restore resolves it and the move to draft re-reads this key.
_PIECES_CTE = f"""
    t AS (SELECT unnest($1::uuid[]) AS tour_id),
    piece AS (
        SELECT cp.piece_id, cp.tenant_id,
               CASE WHEN {_tl.request_tour_sql()} IN (SELECT tour_id FROM t) THEN {_tl.request_tour_sql()}
                    ELSE (SELECT tour_id FROM t ORDER BY tour_id LIMIT 1) END AS tour_id
          FROM acp_shared.content_piece cp
          JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id AND r.tenant_id = cp.tenant_id
          LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = r.atom_id
         WHERE {_tl.requests_about_tours_sql("SELECT tour_id FROM t")})"""

# The live posts of the tours ($1) without an open tour_retired flag of an earlier event ($2 = this event's key): a
# second retired event of a retired key (only the reason changed) never flags a post twice.
LIVE_POSTS_SQL = f"""
WITH {_PIECES_CTE}
SELECT pl.publish_id::text AS publish_id, pl.piece_id::text AS piece_id, pl.tenant_id::text AS tenant_id,
       pl.channel, pl.external_url, piece.tour_id::text AS tour_id
  FROM acp_shared.publish_log pl
  JOIN piece ON piece.piece_id = pl.piece_id AND piece.tenant_id = pl.tenant_id
 WHERE pl.status = 'published' AND pl.unpublished_at IS NULL
   AND pl.tenant_id <> '{_d.AA_TENANT}'::uuid
   AND NOT EXISTS (SELECT 1 FROM acp_shared.content_compliance_flag f
                    WHERE f.kind = '{FLAG_KIND}' AND f.state = 'open' AND f.publish_id = pl.publish_id
                      AND f.event_key <> $2)
 ORDER BY pl.tenant_id, pl.published_at, pl.publish_id
"""

CANCEL_STALE_SQL = f"""
WITH {_PIECES_CTE}
UPDATE acp_shared.publish_log pl
   SET status = 'failed', last_error = 'cancelled: Adventure Asia no longer offers this tour'
  FROM piece
 WHERE pl.piece_id = piece.piece_id AND pl.status = 'requested'
   AND pl.requested_at < now() - make_interval(secs => $2)
RETURNING pl.publish_id::text AS publish_id, pl.tenant_id::text AS tenant_id
"""

NOTICE_DAYS_SQL = """
    SELECT notice_days FROM acp_shared.tour_retirement_policy
     WHERE tenant_id = $1::uuid OR tenant_id IS NULL
     ORDER BY (tenant_id IS NULL) LIMIT 1
"""

INSERT_FLAG_SQL = f"""
    INSERT INTO acp_shared.content_compliance_flag
        (tenant_id, kind, stage, piece_id, publish_id, tour_id, channel, external_url, codes, detail,
         tenant_message, event_key, action, draft_due_at)
    VALUES ($1::uuid, '{FLAG_KIND}', 'retirement', $2::uuid, $3::uuid, $4::uuid, $5, $6, ARRAY['tour_retired'],
            $7::jsonb, $8, $9, $10, $11)
    ON CONFLICT (event_key, publish_id) WHERE kind = '{FLAG_KIND}' DO NOTHING
    RETURNING flag_id::text AS flag_id
"""

RESOLVE_RESTORED_SQL = f"""
    UPDATE acp_shared.content_compliance_flag
       SET state = 'resolved', resolution = 'restored', resolved_at = now(), resolved_by = $2, updated_at = now()
     WHERE kind = '{FLAG_KIND}' AND state = 'open' AND tour_id = ANY($1::uuid[])
    RETURNING tenant_id::text AS tenant_id
"""

INSERT_NOTICE_SQL = """
    INSERT INTO acp_shared.tenant_tour_notice
        (tenant_id, tour_id, source_id, event_key, event, tour_name, delivery, summary)
    VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::jsonb)
    ON CONFLICT (tenant_id, event_key) DO NOTHING
    RETURNING notice_id::text AS notice_id
"""

# The event's notices still to mail at once, whether this run or an earlier one wrote them: the mail jobs are keyed
# tour_notice_email:<notice_id>, so a replay after a lost enqueue (worker stop, deploy) enqueues them again.
# A notice that still waits for pages being put back (a restore, `summary.pages_returning`) is mailed by the last
# republish job instead, so its mail says what happened to each page.
UNMAILED_IMMEDIATE_SQL = """
    SELECT n.notice_id::text AS notice_id FROM acp_shared.tenant_tour_notice n
     WHERE n.event_key = $1 AND n.delivery = 'immediate' AND n.mailed_at IS NULL
       AND jsonb_array_length(COALESCE(n.summary -> 'pages_returning', '[]'::jsonb)) = 0
     ORDER BY n.tenant_id
"""

# The tenants whose latest notice for the tours ($1), other than this event's own ($2), is a 'retired' one: they were
# told "no longer offered" and not told since that it is back. The restore mails these only (a restore after a pause
# or a supersede, or a second restore, mails nobody); a retire skips them (no second "no longer offered"). "Latest" is
# the order of the lifecycle events (the change record's id), not of the notice rows: a fan-out job retried late
# writes its notices after a newer event's.
OUTSTANDING_RETIRED_SQL = """
    SELECT tenant_id FROM (
        SELECT DISTINCT ON (n.tenant_id) n.tenant_id::text AS tenant_id, n.event
          FROM acp_shared.tenant_tour_notice n
          LEFT JOIN gold_aa_internal.tour_lifecycle_event e ON e.idempotency_key = n.event_key
         WHERE n.tour_id = ANY($1::uuid[]) AND n.event_key <> $2
         ORDER BY n.tenant_id, e.id DESC NULLS LAST, n.created_at DESC, n.notice_id DESC) latest
     WHERE latest.event = 'retired'
"""

# The WordPress pages the retirement this restore ($2) undoes moved to draft: the drafted tour_retired flags of the
# key's tours ($1) of a retired event of the same key after its previous restored event and before this one (a reason
# change writes a second retired event in one retirement). A page drafted by an older retire / restore cycle is never
# touched. Marked once (the mark is the idempotency: a replay marks nothing new). Only a page this system moved from
# `publish` to draft gets a job (`queued`): move_to_draft records `wordpress = 'drafted'`, `wordpress_before =
# 'publish'` and the post's `wordpress_modified_gmt`. A page drafted before those were recorded is `kept` (the tenant is
# told it stays in draft; we cannot tell it was public); anything else (not drafted by us) is `skipped`.
_PUBLIC_BEFORE_SQL = ("f.detail ->> 'wordpress' = 'drafted' AND f.detail ->> 'wordpress_before' = 'publish' "
                      "AND f.detail ->> 'wordpress_modified_gmt' IS NOT NULL")
MARK_REPUBLISH_SQL = f"""
    WITH ev AS (SELECT e.id, e.source_id FROM gold_aa_internal.tour_lifecycle_event e WHERE e.idempotency_key = $2),
    prev AS (SELECT COALESCE(max(p.id), 0) AS id FROM gold_aa_internal.tour_lifecycle_event p, ev
              WHERE p.source_id = ev.source_id AND p.event = 'restored' AND p.id < ev.id),
    cycle AS (SELECT r.idempotency_key FROM gold_aa_internal.tour_lifecycle_event r, ev, prev
               WHERE r.source_id = ev.source_id AND r.event = 'retired' AND r.id > prev.id AND r.id < ev.id)
    UPDATE acp_shared.content_compliance_flag f
       SET detail = f.detail || jsonb_build_object('{REPUBLISH_MARK}', jsonb_strip_nulls(jsonb_build_object(
               'event_key', $2::text, 'queued_at', now(),
               'state', CASE WHEN {_PUBLIC_BEFORE_SQL} THEN 'queued'
                             WHEN f.detail ->> 'wordpress' = 'drafted' THEN 'kept' ELSE 'skipped' END,
               'reason', CASE WHEN {_PUBLIC_BEFORE_SQL} THEN NULL
                              WHEN f.detail ->> 'wordpress' = 'drafted' THEN 'drafted_before_check'
                              ELSE 'not_drafted_by_us' END))),
           updated_at = now()
     WHERE f.kind = '{FLAG_KIND}' AND f.state = 'resolved' AND f.resolution = 'drafted'
       AND f.action = 'draft_after_notice' AND f.tour_id = ANY($1::uuid[])
       AND f.event_key IN (SELECT idempotency_key FROM cycle)
       AND NOT (f.detail ? '{REPUBLISH_MARK}')
    RETURNING f.flag_id::text AS flag_id, f.tenant_id::text AS tenant_id, f.external_url,
              f.detail -> '{REPUBLISH_MARK}' ->> 'state' AS state
"""

# One republish job per marked page of this restore ($1), in the fan-out transaction (no enqueue lost after the
# commit; the job key makes a replay a no-op), due now.
ENQUEUE_REPUBLISH_SQL = f"""
    WITH created AS (
    INSERT INTO shared.job (kind, payload, idempotency_key, max_attempts, created_by)
    SELECT '{REPUBLISH_KIND}', jsonb_build_object('flag_id', f.flag_id::text, 'event_key', $1::text,
                                                  'trigger', 'restored'),
           '{REPUBLISH_KIND}:' || f.flag_id::text, $2, 'system:{REPUBLISH_KIND}'
      FROM acp_shared.content_compliance_flag f
     WHERE f.kind = '{FLAG_KIND}' AND f.tour_id = ANY($3::uuid[])
       AND f.detail -> '{REPUBLISH_MARK}' ->> 'event_key' = $1
       AND f.detail -> '{REPUBLISH_MARK}' ->> 'state' = 'queued'
     ORDER BY f.flag_id
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
    RETURNING 1
    )
    SELECT count(*) FROM created
"""

# One fan-out of a key at a time (two events of one key, or a replay racing the first run): the outstanding checks
# above read what the other run writes.
KEY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('tour_retirement_fanout:' || $1, 0))"
# The catalog key of one tour (the lifecycle event's source_id), for the WordPress jobs that take the same lock.
TOUR_KEY_SQL = ("SELECT (COALESCE(rt.source_group_id, rt.tour_id))::text FROM silver_aa_internal.raw_tours rt "
                "WHERE rt.tour_id = $1::uuid")


def _key(source_id: Optional[str], tour_id: Optional[str]) -> str:
    return str(source_id or tour_id)


def served(state: Optional[str]) -> bool:
    """One rule for every step here: a key is served only when its liveness state says ``served``. A key without a
    state (no Master ever published, or its raw tour gone) is not: a restore of it mails nobody and puts no page back,
    and an open flag of it is drafted when due."""
    return state == "served"


async def _lock_tour_key(conn, tour_id: str) -> None:
    key = await conn.fetchval(TOUR_KEY_SQL, tour_id)
    await conn.execute(KEY_LOCK_SQL, _key(key, tour_id))

# The sweep's due drafts: open WordPress flags past their notice period with no job yet (one INSERT ... SELECT).
DUE_DRAFTS_SQL = f"""
    WITH created AS (
    INSERT INTO shared.job (kind, payload, idempotency_key, max_attempts, created_by)
    SELECT 'retired_page_draft', jsonb_build_object('flag_id', f.flag_id::text, 'trigger', 'sweep'),
           'retired_page_draft:' || f.flag_id::text, $1, 'system:retired_page_draft_sweep'
      FROM acp_shared.content_compliance_flag f
     WHERE f.kind = '{FLAG_KIND}' AND f.state = 'open' AND f.action = 'draft_after_notice'
       AND f.draft_due_at <= now()
       AND NOT EXISTS (SELECT 1 FROM shared.job j WHERE j.idempotency_key = 'retired_page_draft:' || f.flag_id::text)
     ORDER BY f.draft_due_at
     LIMIT 200
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
    RETURNING 1
    )
    SELECT count(*) FROM created
"""


def _fmt_day(ts: Optional[datetime]) -> Optional[str]:
    return f"{ts.day} {ts:%B %Y}" if ts else None


def page_message(tour_name: str, draft_due_at: datetime) -> str:
    return (f"Adventure Asia no longer offers {tour_name}. This page will be moved to draft on "
            f"{_fmt_day(draft_due_at)}. You can rewrite it around another Adventure Asia tour, or remove it, before "
            "then.")


def post_message(tour_name: str) -> str:
    return (f"Adventure Asia no longer offers {tour_name}. Please edit or remove this post; nothing is deleted for "
            "you.")


class SchemaMissing(RuntimeError):
    """CIS 360 is not applied yet. Retryable on purpose: the fan-out job retries with backoff, and once the tables
    exist the sweep re-queues the jobs that failed on it (tour_retirement_jobs.REQUEUE_SCHEMA_MISSING_SQL), so a
    retire / restore that happens between the code deploy and the migration is never lost."""

    def __init__(self):
        super().__init__(SCHEMA_MISSING)


async def notice_days(conn, tenant_id: str) -> int:
    try:
        async with conn.transaction():      # a savepoint: a missing table never aborts the caller's transaction
            days = await conn.fetchval(NOTICE_DAYS_SQL, tenant_id)
    except asyncpg.UndefinedTableError:
        days = None
    return int(days) if days else DEFAULT_NOTICE_DAYS


async def _tour_name(conn, tour_id: str) -> str:
    return (await conn.fetchval(TOUR_NAME_SQL, tour_id)) or "this tour"


async def fan_out(pool, event_key: str) -> dict:
    """Apply one lifecycle event to tenant content. Idempotent (every write is keyed on the event); returns counts and
    the notice ids to mail now: every ``immediate`` notice of the event not mailed yet, also on a replay, so a mail
    job lost between this commit and its enqueue is enqueued again (the job key makes that safe)."""
    async with pool.acquire() as conn:
        event = await conn.fetchrow(EVENT_SQL, event_key)
        if event is None:
            return {"status": "skipped", "reason": "event_not_found"}
        kind = event["event"]
        if kind not in ("retired", "restored"):
            return {"status": "skipped", "reason": f"event_{kind}"}
        tour_ids = [r["tour_id"] for r in await conn.fetch(KEY_TOURS_SQL, event["source_id"])] or [event["tour_id"]]
        try:
            async with conn.transaction():
                await conn.execute(KEY_LOCK_SQL, _key(event["source_id"], event["tour_id"]))
                state = await conn.fetchrow(STATE_SQL, event["source_id"])     # read under the key lock
                now_state = state["state"] if state else None
                if kind == "retired":
                    if now_state != "retired":
                        return {"status": "skipped", "reason": f"key_is_{now_state or 'unknown'}_now"}
                    out = await _retired(conn, event, tour_ids)
                else:
                    if not served(now_state):
                        return {"status": "skipped", "reason": f"key_is_{now_state or 'unknown'}_now"}
                    out = await _restored(conn, event, tour_ids)
                out["new_immediate_notices"] = len(out["immediate_notices"])
                out["immediate_notices"] = [r["notice_id"] for r in await conn.fetch(
                    UNMAILED_IMMEDIATE_SQL, event["idempotency_key"])]
        except asyncpg.UndefinedTableError as exc:
            raise SchemaMissing() from exc
    logger.info("tour_retirement_fanout", event_key=event_key, lifecycle_event=kind,
                **{k: v for k, v in out.items() if k != "immediate_notices"})
    return {"status": "done", "event": kind, **out}


async def _retired(conn, event, tour_ids: list[str]) -> dict:
    tour_name = await _tour_name(conn, event["tour_id"])
    key = event["idempotency_key"]
    posts = await conn.fetch(LIVE_POSTS_SQL, tour_ids, key)
    told = {r["tenant_id"] for r in await conn.fetch(OUTSTANDING_RETIRED_SQL, tour_ids, key)}
    flags, per_tenant, days_cache = 0, {}, {}
    for p in posts:
        tid = p["tenant_id"]
        if tid not in days_cache:
            days_cache[tid] = await notice_days(conn, tid)
        if p["channel"] in WORDPRESS_CHANNELS:
            due = event["occurred_at"] + timedelta(days=days_cache[tid])
            action, message = "draft_after_notice", page_message(tour_name, due)
        else:
            due, action, message = None, "notice_only", post_message(tour_name)
        detail = {"tour_name": tour_name, "reason": event["reason"], "notice_days": days_cache[tid]}
        flag_id = await conn.fetchval(
            INSERT_FLAG_SQL, tid, p["piece_id"], p["publish_id"], p["tour_id"], p["channel"], p["external_url"],
            json.dumps(detail), message, event["idempotency_key"], action, due)
        flags += 1 if flag_id else 0
        entry = per_tenant.setdefault(tid, {"pages": [], "posts": []})
        if p["channel"] in WORDPRESS_CHANNELS:
            entry["pages"].append({"url": p["external_url"], "draft_on": _fmt_day(due)})
        else:
            entry["posts"].append({"url": p["external_url"], "channel": p["channel"]})
    cancelled = await conn.fetch(CANCEL_STALE_SQL, tour_ids, STALE_REQUESTED_SECONDS)
    for c in cancelled:
        per_tenant.setdefault(c["tenant_id"], {"pages": [], "posts": []})
    tenants = set((await _d.tenant_derivatives(conn, tour_ids)).keys()) | set(per_tenant)
    tenants.discard(_d.AA_TENANT)
    already = tenants & told - set(per_tenant)
    tenants -= already
    replacement = await _tour_name(conn, event["replaced_by_tour_id"]) if event["replaced_by_tour_id"] else None
    immediate = []
    for tid in sorted(tenants):
        entry = per_tenant.get(tid, {"pages": [], "posts": []})
        live = bool(entry["pages"] or entry["posts"])
        summary = {"stop_date": _fmt_day(event["occurred_at"]), "pages": entry["pages"], "posts": entry["posts"],
                   "publishes_cancelled": sum(1 for c in cancelled if c["tenant_id"] == tid),
                   "new_work_refused": True, "replacement": replacement}
        notice_id = await conn.fetchval(
            INSERT_NOTICE_SQL, tid, event["tour_id"], event["source_id"], event["idempotency_key"], "retired",
            tour_name, "immediate" if live else "digest", json.dumps(summary))
        if notice_id and live:
            immediate.append(notice_id)
    return {"flags_created": flags, "live_posts": len(posts), "publishes_cancelled": len(cancelled),
            "tenants_noticed": len(tenants), "tenants_already_told": len(already), "immediate_notices": immediate}


async def _restored(conn, event, tour_ids: list[str]) -> dict:
    tour_name = await _tour_name(conn, event["tour_id"])
    key = event["idempotency_key"]
    resolved = await conn.fetch(RESOLVE_RESTORED_SQL, tour_ids, ACTOR)
    had_live = {r["tenant_id"] for r in resolved}
    returning: dict[str, list] = {}       # queued: a job puts each page back
    kept: dict[str, list] = {}            # drafted before the public-status check: stays in draft, the tenant is told
    for m in await conn.fetch(MARK_REPUBLISH_SQL, tour_ids, key):
        if m["state"] == "queued":
            returning.setdefault(m["tenant_id"], []).append({"url": m["external_url"], "flag_id": m["flag_id"]})
        elif m["state"] == "kept":
            kept.setdefault(m["tenant_id"], []).append({"url": m["external_url"]})
    tenants = {r["tenant_id"] for r in await conn.fetch(OUTSTANDING_RETIRED_SQL, tour_ids, key)} | had_live
    tenants |= set(returning) | set(kept)
    tenants.discard(_d.AA_TENANT)
    immediate = []
    for tid in sorted(tenants):
        summary = {"back_on": _fmt_day(event["occurred_at"]), "flags_cleared": sum(
            1 for r in resolved if r["tenant_id"] == tid), "pages_returning": returning.get(tid, []),
            "pages_kept": kept.get(tid, [])}
        at_once = tid in had_live or tid in returning or tid in kept
        notice_id = await conn.fetchval(
            INSERT_NOTICE_SQL, tid, event["tour_id"], event["source_id"], key, "restored",
            tour_name, "immediate" if at_once else "digest", json.dumps(summary))
        if notice_id and at_once and tid not in returning:      # a waiting notice is mailed by its last page's job
            immediate.append(notice_id)
    jobs = await conn.fetchval(ENQUEUE_REPUBLISH_SQL, key, REPUBLISH_ATTEMPTS, tour_ids)
    return {"flags_resolved": len(resolved), "tenants_noticed": len(tenants), "immediate_notices": immediate,
            "pages_to_republish": sum(len(v) for v in returning.values()),
            "pages_kept_in_draft": sum(len(v) for v in kept.values()), "republish_jobs": int(jobs or 0)}


async def enqueue_due_drafts(pool, max_attempts: int) -> int:
    try:
        return int(await pool.fetchval(DUE_DRAFTS_SQL, max_attempts) or 0)
    except asyncpg.UndefinedTableError:
        return 0


# ── the WordPress move to draft (one flag) ────────────────────────────────────────────────────────────────────────

FLAG_SQL = f"""
    SELECT f.flag_id::text AS flag_id, f.tenant_id::text AS tenant_id, f.state, f.action, f.draft_due_at,
           f.resolution, f.detail,
           f.tour_id::text AS tour_id, f.publish_id::text AS publish_id, f.piece_id::text AS piece_id,
           pl.status AS publish_status, pl.unpublished_at, pl.external_id, pl.external_url, pl.channel
      FROM acp_shared.content_compliance_flag f
      LEFT JOIN acp_shared.publish_log pl ON pl.publish_id = f.publish_id
     WHERE f.flag_id = $1::uuid AND f.kind = '{FLAG_KIND}'
"""

# The served state of one tour's key (strict, :func:`served`: no row = not served).
TOUR_STATE_SQL = """
    SELECT tl.state FROM silver_aa_internal.raw_tours rt
      JOIN gold_aa_internal.v_tour_liveness tl ON tl.source_id = (COALESCE(rt.source_group_id, rt.tour_id))::text
     WHERE rt.tour_id = $1::uuid
"""

# AA-1057 GAP 1: the piece's request is on a Segment Subject whose Segment has a served member again (another tour
# came back or was added since the flag): the Subject is alive, so the page is not moved (resolved 'restored').
PIECE_SUBJECT_ALIVE_SQL = f"""
    SELECT {_tl.segment_served_member_sql('r')} AS alive
      FROM acp_shared.content_piece cp
      JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id AND r.tenant_id = cp.tenant_id
     WHERE cp.piece_id = $1::uuid
"""

RESOLVE_ONE_SQL = """
    UPDATE acp_shared.content_compliance_flag
       SET state = 'resolved', resolution = $2, resolved_at = now(), resolved_by = $3, updated_at = now(),
           drafted_at = CASE WHEN $2 = 'drafted' THEN now() ELSE drafted_at END,
           detail = detail || $4::jsonb
     WHERE flag_id = $1::uuid AND state = 'open'
    RETURNING flag_id::text
"""

NOTE_SQL = """
    UPDATE acp_shared.content_compliance_flag SET detail = detail || $2::jsonb, updated_at = now()
     WHERE flag_id = $1::uuid
"""

MARK_UNPUBLISHED_SQL = """
    UPDATE acp_shared.publish_log SET status = 'unpublished', unpublished_at = now(), unpublished_by = $3
     WHERE publish_id = $1::uuid AND tenant_id = $2::uuid AND status = 'published'
"""

INTEGRATION_SQL = ("SELECT secret_key FROM shared.tenant_integrations "
                   "WHERE tenant_id = $1::uuid AND integration_type = 'wordpress'")

# A page moved to draft after the restore had already resolved its flag (a race with the restore fan-out): published
# again at once. The flag's detail key: {state: pending|done|failed, ...}; `pending` makes the draft job retry it.
DRAFT_REVERT = "draft_revert"


class DraftRefused(Exception):
    """The page cannot be moved on the tenant's behalf (no connection, another site, WordPress refused): the flag
    keeps the reason, staff and the tenant's mail already say what to do. `permanent` = a retry cannot help."""

    def __init__(self, reason: str, permanent: bool = True):
        super().__init__(reason, permanent)
        self.reason = reason
        self.permanent = permanent


def _detail(raw) -> dict:
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw or {})


def _now() -> str:
    return datetime.now().astimezone().isoformat()


async def _site(pool, flag, read_secret, adapter_for):
    """(adapter, None) for the tenant's connected WordPress site, or (None, reason): not connected, or the post lives
    on another site than the connected one (AA-815: post ids are per site)."""
    from services.acp_publish.dispatch import other_account

    async with pool.acquire() as conn:
        integ = await conn.fetchrow(INTEGRATION_SQL, flag["tenant_id"])
    if not integ or not integ["secret_key"]:
        return None, "wordpress_not_connected"
    creds = await read_secret(integ["secret_key"])
    other = other_account("blog", creds, str(flag["external_id"] or ""), flag["external_url"])
    if other:
        return None, f"other_site:{other}"
    return adapter_for(creds), None


async def move_to_draft(pool, flag_id: str, *, read_secret, adapter_for) -> dict:
    """Move the WordPress page of one open ``draft_after_notice`` flag to draft, after re-checking that it is still
    due: the tour is still not served, the piece's Segment Subject (if any) has no served member again (AA-1057 GAP 1),
    the post is still live. Only a post WordPress says is ``publish`` is moved (a private, pending, scheduled, draft
    or trashed one is left as the tenant set it, and the flag is resolved ``unpublished``); the flag records
    ``wordpress_before = 'publish'`` and the post's ``modified_gmt`` after the move, which the restore needs to put
    the page back. The final write holds the key's fan-out lock: when a restore resolved the flag meanwhile, the page
    is published again at once (``detail.draft_revert``). `read_secret(name) -> dict` and
    `adapter_for(creds) -> WordPressAdapter` are injected (Secrets Manager / safe_http in the job, fakes in tests)."""
    async with pool.acquire() as conn:
        flag = await conn.fetchrow(FLAG_SQL, flag_id)
        if flag is None:
            return {"status": "skipped", "reason": "flag_not_found"}
        if (_detail(flag["detail"]).get(DRAFT_REVERT) or {}).get("state") == "pending":
            revert = True
        else:
            revert = False
            if flag["state"] != "open" or flag["action"] != "draft_after_notice":
                return {"status": "skipped", "reason": f"flag_{flag['state']}"}
            if flag["draft_due_at"] and flag["draft_due_at"] > datetime.now(flag["draft_due_at"].tzinfo):
                return {"status": "skipped", "reason": "not_due"}
            if served(await conn.fetchval(TOUR_STATE_SQL, flag["tour_id"])):
                await conn.execute(RESOLVE_ONE_SQL, flag_id, "restored", ACTOR, "{}")
                return {"status": "resolved", "resolution": "restored"}
            if flag["piece_id"] and await conn.fetchval(PIECE_SUBJECT_ALIVE_SQL, flag["piece_id"]):
                await conn.execute(RESOLVE_ONE_SQL, flag_id, "restored", ACTOR, json.dumps({"subject_alive": True}))
                return {"status": "resolved", "resolution": "restored", "subject_alive": True}
            if flag["publish_status"] != "published" or flag["unpublished_at"] is not None:
                await conn.execute(RESOLVE_ONE_SQL, flag_id, "unpublished", ACTOR, "{}")
                return {"status": "resolved", "resolution": "unpublished"}
    if revert:
        return await _revert_draft(pool, flag, read_secret=read_secret, adapter_for=adapter_for)
    adapter, refused = await _site(pool, flag, read_secret, adapter_for)
    if refused == "wordpress_not_connected":
        await _note(pool, flag_id, "WordPress is not connected any more; the page was not moved")
        raise DraftRefused("wordpress_not_connected")
    if refused:
        await _note(pool, flag_id, f"the page lives on {refused.split(':', 1)[1]}, not on the connected site; it was "
                                   "not moved")
        raise DraftRefused("other_site")
    try:
        before = await adapter.post_status(flag["external_id"])
    except Exception as exc:  # noqa: BLE001 — recorded on the flag, the job decides on the retry
        await _note(pool, flag_id, str(exc)[:300])
        raise DraftRefused(str(exc)[:300], permanent=False) from exc
    if before.status != "publish":
        # not public now (the tenant made it private / pending / a draft, scheduled or trashed it, or it is gone):
        # never touched, so a restore never publishes what was not public
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _lock_tour_key(conn, flag["tour_id"])
                done = await conn.fetchval(RESOLVE_ONE_SQL, flag_id, "unpublished", ACTOR, json.dumps(
                    {"wordpress": "not_public", "wordpress_before": before.status}))
                if done:
                    await conn.execute(MARK_UNPUBLISHED_SQL, flag["publish_id"], flag["tenant_id"], ACTOR)
        return {"status": "resolved" if done else "skipped", "resolution": "unpublished",
                "wordpress_status": before.status}
    try:
        change = await adapter.draft_post(flag["external_id"])
    except Exception as exc:  # noqa: BLE001 — recorded on the flag, the job decides on the retry
        await _note(pool, flag_id, str(exc)[:300])
        raise DraftRefused(str(exc)[:300], permanent=False) from exc
    record = {"wordpress": change.outcome, "wordpress_before": "publish",
              "wordpress_modified_gmt": change.modified_gmt}
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_tour_key(conn, flag["tour_id"])    # a restore fan-out of the key waits for this, or ran before
            done = await conn.fetchval(RESOLVE_ONE_SQL, flag_id, "drafted", ACTOR, json.dumps(record))
            if done:
                await conn.execute(MARK_UNPUBLISHED_SQL, flag["publish_id"], flag["tenant_id"], ACTOR)
            elif change.outcome == "drafted":
                await conn.execute(NOTE_SQL, flag_id, json.dumps({**record, DRAFT_REVERT: {"state": "pending"}}))
    if done:
        return {"status": "drafted", "outcome": change.outcome, "publish_id": flag["publish_id"],
                "tenant_id": flag["tenant_id"], "piece_id": flag["piece_id"]}
    if change.outcome != "drafted":
        return {"status": "skipped", "reason": "flag_resolved_meanwhile"}
    # the restore resolved the flag while WordPress moved the page: it was public, so it goes back at once
    return await _revert_draft(pool, flag, read_secret=read_secret, adapter_for=adapter_for)


async def _revert_draft(pool, flag, *, read_secret, adapter_for) -> dict:
    """Publish again a page moved to draft after its flag was resolved (a restore won the race). Retryable; the
    outcome is kept in ``detail.draft_revert``."""
    flag_id = flag["flag_id"]
    adapter, refused = await _site(pool, flag, read_secret, adapter_for)
    if refused:
        await _note_revert(pool, flag_id, {"state": "failed", "reason": refused})
        return {"status": "revert_failed", "reason": refused}
    try:
        now = await adapter.post_status(flag["external_id"])
        if now.status != "draft":                           # the tenant changed it since: theirs now
            await _note_revert(pool, flag_id, {"state": "done", "wordpress_status": now.status, "at": _now()})
            return {"status": "reverted", "outcome": f"left_{now.status}"}
        outcome = await adapter.set_published(flag["external_id"])
    except Exception as exc:  # noqa: BLE001 — recorded, the job retries (the flag stays `pending`)
        await _note_revert(pool, flag_id, {"state": "pending", "error": str(exc)[:300]})
        raise DraftRefused(f"draft revert: {str(exc)[:280]}", permanent=False) from exc
    await _note_revert(pool, flag_id, {"state": "done", "wordpress": outcome, "at": _now()})
    return {"status": "reverted", "outcome": outcome, "flag_id": flag_id}


async def _note_revert(pool, flag_id: str, revert: dict) -> None:
    async with pool.acquire() as conn:
        await conn.execute(NOTE_SQL, flag_id, json.dumps({DRAFT_REVERT: revert}))


async def _note(pool, flag_id: str, text: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(NOTE_SQL, flag_id, json.dumps({"draft_error": text}))


# ── the WordPress page put back after a restore (one drafted flag) ─────────────────────────────────────────────────

REPUBLISH_FLAG_SQL = f"""
    SELECT f.flag_id::text AS flag_id, f.tenant_id::text AS tenant_id, f.state, f.resolution, f.detail,
           f.tour_id::text AS tour_id, f.publish_id::text AS publish_id, f.piece_id::text AS piece_id,
           f.external_url AS flag_url,
           pl.status AS publish_status, pl.unpublished_by, pl.external_id, pl.external_url, pl.channel,
           cp.status AS piece_status, cp.angle_gate_request_id::text AS request_id,
           EXISTS (SELECT 1 FROM acp_shared.content_piece newer
                    WHERE newer.angle_gate_request_id = cp.angle_gate_request_id AND newer.tenant_id = cp.tenant_id
                      AND newer.created_at > cp.created_at) AS superseded
      FROM acp_shared.content_compliance_flag f
      LEFT JOIN acp_shared.publish_log pl ON pl.publish_id = f.publish_id
      LEFT JOIN acp_shared.content_piece cp ON cp.piece_id = f.piece_id AND cp.tenant_id = f.tenant_id
     WHERE f.flag_id = $1::uuid AND f.kind = '{FLAG_KIND}'
"""

# Another live or in-flight WordPress post of the same request ($1, any of its pieces: one piece row per write
# attempt, v1_publish._PENDING_SQL): the tenant published it again from the studio; putting the old post back would
# show two copies.
OTHER_LIVE_POST_SQL = """
    SELECT EXISTS (SELECT 1 FROM acp_shared.publish_log o
                     JOIN acp_shared.content_piece op ON op.piece_id = o.piece_id AND op.tenant_id = o.tenant_id
                    WHERE op.angle_gate_request_id = $1::uuid AND o.tenant_id = $2::uuid
                      AND o.channel = ANY($3::text[]) AND o.publish_id <> $4::uuid
                      AND o.status IN ('published', 'requested'))
"""

# The AA-subject gate holds the piece (AA-1039 d21, services/acp_shared/subject_gate.py): it would not publish now.
OPEN_GATE_SQL = """
    SELECT EXISTS (SELECT 1 FROM acp_shared.content_compliance_flag g
                    WHERE g.piece_id = $1::uuid AND g.kind = 'subject_gate' AND g.state = 'open')
"""

# The claim, before WordPress is asked: the row this system took down is live again in publish_log (so a retire
# fan-out that runs meanwhile flags the page). The reverse of MARK_UNPUBLISHED_SQL; published_at keeps the first date.
MARK_REPUBLISHED_SQL = """
    UPDATE acp_shared.publish_log SET status = 'published', unpublished_at = NULL, unpublished_by = NULL
     WHERE publish_id = $1::uuid AND tenant_id = $2::uuid AND status = 'unpublished' AND unpublished_by = $3
"""

# WordPress refused after the claim: the row goes back to what this system recorded at the draft.
UNCLAIM_SQL = """
    UPDATE acp_shared.publish_log SET status = 'unpublished', unpublished_at = now(), unpublished_by = $3
     WHERE publish_id = $1::uuid AND tenant_id = $2::uuid AND status = 'published'
"""

# The restore mark's outcome ($2 merged into detail.restore_republish), once: only while it is still 'queued'.
REPUBLISH_DONE_SQL = f"""
    UPDATE acp_shared.content_compliance_flag
       SET detail = jsonb_set(detail, '{{{REPUBLISH_MARK}}}', (detail -> '{REPUBLISH_MARK}') || $2::jsonb),
           updated_at = now()
     WHERE flag_id = $1::uuid AND detail -> '{REPUBLISH_MARK}' ->> 'state' = 'queued'
    RETURNING detail -> '{REPUBLISH_MARK}' ->> 'event_key' AS event_key
"""

REPUBLISH_NOTE_SQL = f"""
    UPDATE acp_shared.content_compliance_flag
       SET detail = jsonb_set(detail, '{{{REPUBLISH_MARK}}}', (detail -> '{REPUBLISH_MARK}') || $2::jsonb),
           updated_at = now()
     WHERE flag_id = $1::uuid AND detail ? '{REPUBLISH_MARK}'
"""

# The restored notice of the tenant ($1, event $2): the page ($3 = flag id, $4 = url) leaves `pages_returning` and,
# when $5 names a list (`pages_back` / `pages_kept`), joins it. Returns whether the notice still waits for pages.
NOTICE_PAGE_SQL = """
    UPDATE acp_shared.tenant_tour_notice n
       SET summary = n.summary
           || jsonb_build_object('pages_returning', COALESCE((
                  SELECT jsonb_agg(x) FROM jsonb_array_elements(COALESCE(n.summary -> 'pages_returning', '[]'::jsonb)) x
                   WHERE x ->> 'flag_id' IS DISTINCT FROM $3::text), '[]'::jsonb))
           || CASE WHEN $5::text IS NULL THEN '{}'::jsonb
                   ELSE jsonb_build_object($5::text, COALESCE(n.summary -> $5::text, '[]'::jsonb)
                                                     || jsonb_build_array(jsonb_build_object('url', $4::text))) END
     WHERE n.tenant_id = $1::uuid AND n.event_key = $2 AND n.event = 'restored'
    RETURNING n.notice_id::text AS notice_id, n.delivery, n.mailed_at,
              jsonb_array_length(COALESCE(n.summary -> 'pages_returning', '[]'::jsonb)) AS waiting
"""

# What WordPress says the post is now, when it is not a draft any more: nothing is changed.
REMOTE_SKIP = {"missing": "removed_on_site", "trash": "removed_on_site", "publish": "already_published",
               "future": "scheduled_on_site"}
# The blockers the tenant is told about ("still in draft: we could not put it back"); the others (a deleted piece,
# the tour off sale again, a newer post of it already live, a record changed by someone else) say nothing.
KEPT_REASONS = frozenset({"wordpress_not_connected", "other_site", "content_needs_change", "piece_not_publishable",
                          "newer_version", "changed_on_site", "wordpress_error"})


class RepublishRefused(DraftRefused):
    """WordPress could not be asked or refused (`permanent` = a retry cannot help); the job retries the others."""


async def _republish_blocker(conn, flag) -> Optional[str]:
    """Why the page must stay as it is, re-read now; None = it may be published again."""
    if flag["state"] != "resolved" or flag["resolution"] != "drafted":
        return "flag_changed"
    if flag["channel"] not in WORDPRESS_CHANNELS:
        return "not_wordpress"
    if flag["publish_status"] != "unpublished" or flag["unpublished_by"] != ACTOR:
        return "publish_record_changed"
    if not served(await conn.fetchval(TOUR_STATE_SQL, flag["tour_id"])):
        return "tour_not_served"
    if flag["piece_status"] is None:                          # the piece was deleted
        return "piece_deleted"
    if flag["piece_status"] not in ("approved", "held"):     # the publish route's rule (AA-770)
        return "piece_not_publishable"
    if flag["superseded"]:                                    # the publish route's AA-802 rule: only the latest
        return "newer_version"
    if flag["request_id"]:                                    # AA-986 L3: the piece's own tour is served
        tours = await _tl.request_tours(conn, flag["tenant_id"], [flag["request_id"]])
        try:
            await _tl.ensure_served(conn, tours.get(flag["request_id"]), "publish a new post")
        except _tl.TourNotServedError:
            return "tour_not_served"
        if await conn.fetchval(OTHER_LIVE_POST_SQL, flag["request_id"], flag["tenant_id"],
                               list(WORDPRESS_CHANNELS), flag["publish_id"]):
            return "published_again_from_studio"
    if await conn.fetchval(OPEN_GATE_SQL, flag["piece_id"]):
        return "content_needs_change"
    return None


async def _finish(conn, flag, state: str, reason: Optional[str], extra: Optional[dict] = None) -> dict:
    """Record the outcome on the flag (once) and on the tenant's restored notice; returns the job result with the
    notice to mail now (no page of it is still waiting)."""
    outcome = {"state": state, "done_at": _now(), **(extra or {})}
    if reason:
        outcome["reason"] = reason
    out = {"status": state, "flag_id": flag["flag_id"], "tenant_id": flag["tenant_id"], "piece_id": flag["piece_id"],
           "publish_id": flag["publish_id"], **({"reason": reason} if reason else {})}
    event_key = await conn.fetchval(REPUBLISH_DONE_SQL, flag["flag_id"], json.dumps(outcome, default=str))
    if event_key is None:                                    # another run recorded it first
        return {**out, "status": "skipped", "reason": "already_done"}
    page_list = {"republished": "pages_back", "kept": "pages_kept"}.get(state)
    notice = await conn.fetchrow(NOTICE_PAGE_SQL, flag["tenant_id"], event_key, flag["flag_id"],
                                 flag["external_url"] or flag["flag_url"], page_list)
    if notice and notice["delivery"] == "immediate" and notice["mailed_at"] is None and not notice["waiting"]:
        out["mail_notice_id"] = notice["notice_id"]
    return out


async def _finish_pool(pool, flag, state: str, reason: Optional[str], extra: Optional[dict] = None) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _finish(conn, flag, state, reason, extra)


async def _republish_note(pool, flag_id: str, note: dict) -> None:
    async with pool.acquire() as conn:
        await conn.execute(REPUBLISH_NOTE_SQL, flag_id, json.dumps(note))


def _queued(flag) -> Optional[str]:
    """None when the restore mark is there and still queued, else why the flag is not touched."""
    mark = _detail(flag["detail"]).get(REPUBLISH_MARK) or {}
    if not mark:
        return "not_marked"
    if mark.get("state") != "queued":
        return f"already_{mark.get('state')}"
    return None


async def _claim(pool, flag_id: str):
    """Under the key's fan-out lock: re-check the flag (still queued, nothing blocks it now: tour served, no other
    live post) and mark its publish_log row live again before WordPress is asked. Returns (flag, None) when claimed,
    else (None, the finished result)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            flag = await conn.fetchrow(REPUBLISH_FLAG_SQL, flag_id)
            await _lock_tour_key(conn, flag["tour_id"])
            flag = await conn.fetchrow(REPUBLISH_FLAG_SQL, flag_id)          # re-read under the lock
            not_queued = _queued(flag)
            if not_queued:
                return None, {"status": "skipped", "reason": not_queued}
            blocker = await _republish_blocker(conn, flag)
            if blocker:
                return None, await _finish(conn, flag, "kept" if blocker in KEPT_REASONS else "skipped", blocker)
            try:
                async with conn.transaction():      # a savepoint: a studio publish of the same revision keeps its row
                    claimed = await conn.execute(MARK_REPUBLISHED_SQL, flag["publish_id"], flag["tenant_id"], ACTOR)
            except asyncpg.UniqueViolationError:
                return None, await _finish(conn, flag, "kept", "published_again_from_studio")
            if claimed != "UPDATE 1":
                return None, await _finish(conn, flag, "skipped", "publish_record_changed")
            await conn.execute(REPUBLISH_NOTE_SQL, flag_id, json.dumps({"claimed_at": _now()}))
    return flag, None


async def _unclaim(pool, flag, error: str) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_tour_key(conn, flag["tour_id"])
            await conn.execute(UNCLAIM_SQL, flag["publish_id"], flag["tenant_id"], ACTOR)
            await conn.execute(REPUBLISH_NOTE_SQL, flag["flag_id"], json.dumps({"claimed_at": None, "error": error}))


async def republish_page(pool, flag_id: str, *, read_secret, adapter_for) -> dict:
    """Publish again the WordPress page of one ``tour_retired`` flag the retirement moved to draft, after a restore
    marked it (``detail.restore_republish`` queued). Only a page this system moved from ``publish`` to draft (the
    restore's mark checks that) and that WordPress still has as that same draft (status ``draft``, ``modified_gmt``
    unchanged since the move) is published; a page the tenant removed, published, scheduled or edited is left alone.
    Claim first (:func:`_claim`, under the key's fan-out lock), then WordPress; a refusal reverts the claim. Every
    ending is recorded once on the flag and on the tenant's restored notice (``pages_back`` / ``pages_kept``).
    Transient WordPress errors raise :class:`RepublishRefused` (retryable)."""
    async with pool.acquire() as conn:
        flag = await conn.fetchrow(REPUBLISH_FLAG_SQL, flag_id)
        if flag is None:
            return {"status": "skipped", "reason": "flag_not_found"}
        not_queued = _queued(flag)
        if not_queued:
            return {"status": "skipped", "reason": not_queued}
        blocker = await _republish_blocker(conn, flag)                     # cheap first pass, re-checked at the claim
        if blocker:
            async with conn.transaction():
                return await _finish(conn, flag, "kept" if blocker in KEPT_REASONS else "skipped", blocker)
    adapter, refused = await _site(pool, flag, read_secret, adapter_for)
    if refused:
        reason, _, site = refused.partition(":")
        return await _finish_pool(pool, flag, "kept", reason, {"site": site} if site else None)
    try:
        now = await adapter.post_status(flag["external_id"])
    except Exception as exc:  # noqa: BLE001 — recorded on the flag, the job decides on the retry
        await _republish_note(pool, flag_id, {"error": str(exc)[:300]})
        raise RepublishRefused(str(exc)[:300], permanent=False) from exc
    if now.status != "draft":
        return await _finish_pool(pool, flag, "skipped", REMOTE_SKIP.get(now.status, "changed_on_site"),
                                  {"wordpress_status": now.status})
    recorded = _detail(flag["detail"]).get("wordpress_modified_gmt")
    if not recorded or now.modified_gmt != recorded:                       # the tenant edited the draft since
        return await _finish_pool(pool, flag, "kept", "changed_on_site",
                                  {"wordpress_modified_gmt_now": now.modified_gmt})
    flag, done = await _claim(pool, flag_id)
    if done is not None:
        return done
    try:
        outcome = await adapter.set_published(flag["external_id"])
    except Exception as exc:  # noqa: BLE001 — the answer was lost or refused: ask WordPress what it did
        try:
            published = (await adapter.post_status(flag["external_id"])).status == "publish"
        except Exception:  # noqa: BLE001
            published = False
        if not published:
            await _unclaim(pool, flag, str(exc)[:300])
            raise RepublishRefused(str(exc)[:300], permanent=False) from exc
        outcome = "published"
    return await _finish_pool(pool, flag, "republished", None, {"wordpress": outcome, "republished_at": _now()})


async def give_up_republish(pool, flag_id: str, reason: str) -> dict:
    """The job's last attempt failed: the page stays in draft (a claim left behind is reverted) and the tenant is told
    (``pages_kept``)."""
    async with pool.acquire() as conn:
        flag = await conn.fetchrow(REPUBLISH_FLAG_SQL, flag_id)
    if flag is None:
        return {"status": "skipped", "reason": "flag_not_found"}
    not_queued = _queued(flag)
    if not_queued:
        return {"status": "skipped", "reason": not_queued}
    if (_detail(flag["detail"]).get(REPUBLISH_MARK) or {}).get("claimed_at"):
        await _unclaim(pool, flag, reason[:300])
    return await _finish_pool(pool, flag, "kept", "wordpress_error", {"error": reason[:300]})


def immediate_ids(result: dict[str, Any]) -> list[str]:
    return list(result.get("immediate_notices") or [])


__all__ = ["DEFAULT_NOTICE_DAYS", "DraftRefused", "MIGRATION", "REPUBLISH_ATTEMPTS", "REPUBLISH_KIND",
           "RepublishRefused", "SchemaMissing", "enqueue_due_drafts", "fan_out", "give_up_republish",
           "immediate_ids", "move_to_draft", "notice_days", "page_message", "post_message", "republish_page",
           "served"]
