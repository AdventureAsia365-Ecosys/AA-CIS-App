"""AA-1039 R6 slice 2 (root ADR 0019 d20, clarifications TL-20): what a retired / restored tour does to tenant content.

The event is the CIS change record ``gold_aa_internal.tour_lifecycle_event`` (ADR 0015 decision 4, CIS 272; the
contract view ``catalog.v_tour_lifecycle_outbox``, "catalog.tour_liveness_changed" in ADR 0019 d20): the lifecycle
service writes it in the transaction of the change and is the only writer. One ``tour_retirement_fanout`` job per
``retired`` / ``restored`` event (services/jobs/tour_retirement_jobs.py; enqueued after commit, swept like the
TripPlanner purge) runs :func:`fan_out`:

``retired`` (only while the key is still retired when the job runs):
  - every live post of a piece about the key's tours (``publish_log`` published, not unpublished; the piece's request
    is about one of the tours) gets one ``tour_retired`` row in ``acp_shared.content_compliance_flag`` (CIS 360):
    ``notice_only`` by default (tech lead decision S276, ADR 0019 d20 amended: Adventure Asia never changes an
    agency's site unless the agency asked). A WordPress page gets ``draft_after_notice`` with ``draft_due_at`` = event
    time + the notice period only when the agency opted in (``acp_shared.tour_retirement_policy.auto_draft``, CIS 397,
    Content studio Settings; notice period default 7 days). Facebook is always ``notice_only`` (nothing is deleted).
    The agency can move a listed WordPress page to draft itself with one click (:func:`move_to_draft` with the
    tenant as actor, ``POST /v1/publish-log/{publish_id}/move-to-draft``), and the bell gets one
    ``tour.pages_to_review`` row listing its pages;
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
when it had live posts flagged or pages the retirement moved to draft. A restore that follows a pause or a supersede,
or a second restore, mails nobody. A drafted page is never published again here (AA-1039 option A, S275): the notice
lists it (``summary.pages_drafted``) and the tenant's bell gets a ``tour.pages_to_put_back`` row with the same list, so
the tenant puts each page back with one click in the Content studio (services/acp_publish/put_back.py).
One fan-out of a key runs at a time (a transaction advisory lock on the key).
``paused`` and ``superseded`` do nothing here (ADR 0019 d20: a paused tour gets no mail; a supersede is invisible).

The automatic WordPress move to draft (opted-in agencies only) runs at ``draft_due_at`` as a ``retired_page_draft``
job (:func:`move_to_draft`), enqueued due-now by the sweep (:data:`DUE_DRAFTS_SQL`) so no row waits queued for days in
the job table (the deploy gate and the migration idle check count queued rows). The job re-reads the opt-in: an agency
that turned it off after the flag was written gets its flag turned into ``notice_only`` and its page left online.
Either way (job or the agency's click) the ``publish_log`` row is marked ``unpublished`` by
``system:tour_retirement`` and the flag resolved ``drafted``, so the restore notice and "Put back online" treat both
the same; the flag's ``resolved_by`` / ``detail.drafted_by`` and the audit row say who asked.

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

# The migration that adds the agency opt-in (tour_retirement_policy.auto_draft). Before it: everyone is notice only.
POLICY_MIGRATION = "397"
# The agency's notice period (its row, else the platform row) and its own opt-in: auto_draft counts only on the agency's
# row (the platform row can never opt everyone in; CIS 397 CHECK).
POLICY_SQL = """
    SELECT notice_days, (tenant_id IS NOT NULL AND auto_draft) AS auto_draft
      FROM acp_shared.tour_retirement_policy
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
UNMAILED_IMMEDIATE_SQL = """
    SELECT n.notice_id::text AS notice_id FROM acp_shared.tenant_tour_notice n
     WHERE n.event_key = $1 AND n.delivery = 'immediate' AND n.mailed_at IS NULL
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

# AA-1039 (option A): two states in which "Put back online" always refuses a drafted page (409), over its publish_log
# row `pl`. Shared by the restore notice below, the Content studio list and the click (services/acp_publish/put_back.py)
# so a page the tenant cannot put back is never offered. SUPERSEDED: its piece is not its request's latest version (a
# rewrite, a reopen). OTHER_LIVE: another post of the same request is live or in flight (published again since).
SUPERSEDED_SQL = """EXISTS (SELECT 1 FROM acp_shared.content_piece own
                     JOIN acp_shared.content_piece newer ON newer.angle_gate_request_id = own.angle_gate_request_id
                          AND newer.tenant_id = own.tenant_id AND newer.created_at > own.created_at
                    WHERE own.piece_id = pl.piece_id AND own.tenant_id = pl.tenant_id)"""
OTHER_LIVE_SQL = """EXISTS (SELECT 1 FROM acp_shared.content_piece own
                     JOIN acp_shared.content_piece op ON op.angle_gate_request_id = own.angle_gate_request_id
                          AND op.tenant_id = own.tenant_id
                     JOIN acp_shared.publish_log other ON other.piece_id = op.piece_id
                          AND other.tenant_id = pl.tenant_id
                    WHERE own.piece_id = pl.piece_id AND own.tenant_id = pl.tenant_id
                      AND other.publish_id <> pl.publish_id AND other.status IN ('published', 'requested'))"""

# AA-1039 (option A): the tours' ($1) WordPress pages the retirement moved to draft and nobody put back since: per page
# its latest drafted flag (WordPress did draft it), the publish_log row still the retirement's draft, no click noted,
# and one the tenant can put back (not superseded, no other live post of the request).
DRAFTED_PAGES_OF_TOURS_SQL = f"""
    WITH latest AS (
        SELECT DISTINCT ON (f.publish_id) f.publish_id, f.tenant_id, f.detail
          FROM acp_shared.content_compliance_flag f
         WHERE f.kind = '{FLAG_KIND}' AND f.resolution = 'drafted' AND f.tour_id = ANY($1::uuid[])
         ORDER BY f.publish_id, f.drafted_at DESC NULLS LAST, f.created_at DESC, f.flag_id)
    SELECT pl.tenant_id::text AS tenant_id, pl.publish_id::text AS publish_id, pl.external_url
      FROM latest
      JOIN acp_shared.publish_log pl ON pl.publish_id = latest.publish_id AND pl.tenant_id = latest.tenant_id
     WHERE COALESCE(latest.detail ->> 'wordpress', 'drafted') = 'drafted' AND NOT (latest.detail ? 'put_back')
       AND pl.status = 'unpublished' AND pl.unpublished_by = '{ACTOR}'
       AND NOT {SUPERSEDED_SQL} AND NOT {OTHER_LIVE_SQL}
     ORDER BY pl.tenant_id, pl.published_at, pl.publish_id
"""

# One fan-out of a key at a time (two events of one key, or a replay racing the first run): the outstanding checks
# above read what the other run writes.
KEY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('tour_retirement_fanout:' || $1, 0))"

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
    """An opted-in agency's page (automatic draft on): the draft date it chose."""
    return (f"Adventure Asia no longer offers {tour_name}. As set in your Content studio, this page will be moved to "
            f"draft on {_fmt_day(draft_due_at)}. You can rewrite it around another Adventure Asia tour, or move it to "
            "draft now, before then.")


def page_notice_message(tour_name: str) -> str:
    """The default (tech lead decision S276): the page stays online; the agency decides."""
    return (f"Adventure Asia no longer offers {tour_name}. This page stays online: you can rewrite it around another "
            "Adventure Asia tour, move it to draft with one click in your Content studio, or keep it.")


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
    return (await policy(conn, tenant_id))[0]


async def policy(conn, tenant_id: str) -> tuple[int, bool]:
    """(notice days, auto_draft) of the agency. Not opted in when there is no row, when only the platform row exists,
    before CIS 397 (no column: the old notice-days read) and before CIS 360 (no table)."""
    try:
        async with conn.transaction():      # a savepoint: a missing table never aborts the caller's transaction
            row = await conn.fetchrow(POLICY_SQL, tenant_id)
        days, auto = (row["notice_days"], bool(row["auto_draft"])) if row else (None, False)
    except asyncpg.UndefinedColumnError:
        try:
            async with conn.transaction():
                days, auto = await conn.fetchval(NOTICE_DAYS_SQL, tenant_id), False
        except asyncpg.UndefinedTableError:
            days, auto = None, False
    except asyncpg.UndefinedTableError:
        days, auto = None, False
    return (int(days) if days else DEFAULT_NOTICE_DAYS), auto


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
        state = await conn.fetchrow(STATE_SQL, event["source_id"])
        now_state = state["state"] if state else None
        try:
            async with conn.transaction():
                await conn.execute(KEY_LOCK_SQL, event["source_id"] or event["tour_id"])
                if kind == "retired":
                    if now_state != "retired":
                        return {"status": "skipped", "reason": f"key_is_{now_state or 'unknown'}_now"}
                    out = await _retired(conn, event, tour_ids)
                else:
                    if now_state not in ("served", None):
                        return {"status": "skipped", "reason": f"key_is_{now_state}_now"}
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
    from services.notifications import EventType, NotificationService

    tour_name = await _tour_name(conn, event["tour_id"])
    key = event["idempotency_key"]
    posts = await conn.fetch(LIVE_POSTS_SQL, tour_ids, key)
    told = {r["tenant_id"] for r in await conn.fetch(OUTSTANDING_RETIRED_SQL, tour_ids, key)}
    flags, per_tenant, policies = 0, {}, {}
    for p in posts:
        tid = p["tenant_id"]
        if tid not in policies:
            policies[tid] = await policy(conn, tid)
        days, auto = policies[tid]
        wordpress = p["channel"] in WORDPRESS_CHANNELS
        if wordpress and auto:
            due = event["occurred_at"] + timedelta(days=days)
            action, message = "draft_after_notice", page_message(tour_name, due)
        elif wordpress:
            due, action, message = None, "notice_only", page_notice_message(tour_name)
        else:
            due, action, message = None, "notice_only", post_message(tour_name)
        detail = {"tour_name": tour_name, "reason": event["reason"], "notice_days": days, "auto_draft": auto}
        flag_id = await conn.fetchval(
            INSERT_FLAG_SQL, tid, p["piece_id"], p["publish_id"], p["tour_id"], p["channel"], p["external_url"],
            json.dumps(detail), message, event["idempotency_key"], action, due)
        flags += 1 if flag_id else 0
        entry = per_tenant.setdefault(tid, {"pages": [], "posts": []})
        if wordpress:
            entry["pages"].append({"url": p["external_url"], "publish_id": p["publish_id"],
                                   "draft_on": _fmt_day(due), "auto_draft": auto})
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
    immediate, review_told = [], 0
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
        if notice_id and entry["pages"]:    # written once with the notice: a replay inserts no notice, no second row
            await NotificationService(conn).emit(
                event_type=EventType.TOUR_PAGES_TO_REVIEW, entity_type="tour", entity_id=event["tour_id"],
                tenant_id=tid, payload={"tour_name": tour_name, "event_key": event["idempotency_key"],
                                        "pages": [{"publish_id": pg["publish_id"], "url": pg["url"],
                                                   "draft_on": pg["draft_on"]} for pg in entry["pages"]],
                                        "count": len(entry["pages"])})
            review_told += 1
    return {"flags_created": flags, "live_posts": len(posts), "publishes_cancelled": len(cancelled),
            "tenants_noticed": len(tenants), "tenants_already_told": len(already), "immediate_notices": immediate,
            "auto_draft_pages": sum(1 for t in per_tenant.values() for pg in t["pages"] if pg["auto_draft"]),
            "tenants_told_pages_to_review": review_told}


async def _restored(conn, event, tour_ids: list[str]) -> dict:
    from services.notifications import EventType, NotificationService

    tour_name = await _tour_name(conn, event["tour_id"])
    resolved = await conn.fetch(RESOLVE_RESTORED_SQL, tour_ids, ACTOR)
    had_live = {r["tenant_id"] for r in resolved}
    tenants = {r["tenant_id"] for r in await conn.fetch(OUTSTANDING_RETIRED_SQL, tour_ids,
                                                         event["idempotency_key"])} | had_live
    drafted: dict[str, list[dict]] = {}
    for r in await conn.fetch(DRAFTED_PAGES_OF_TOURS_SQL, tour_ids):
        drafted.setdefault(r["tenant_id"], []).append({"publish_id": r["publish_id"], "url": r["external_url"]})
    immediate, put_back_told = [], 0
    for tid in sorted(tenants):
        pages = drafted.get(tid, [])
        urgent = tid in had_live or bool(pages)
        summary = {"back_on": _fmt_day(event["occurred_at"]), "flags_cleared": sum(
            1 for r in resolved if r["tenant_id"] == tid), "pages_drafted": pages}
        notice_id = await conn.fetchval(
            INSERT_NOTICE_SQL, tid, event["tour_id"], event["source_id"], event["idempotency_key"], "restored",
            tour_name, "immediate" if urgent else "digest", json.dumps(summary))
        if notice_id and urgent:
            immediate.append(notice_id)
        if notice_id and pages:     # written once with the notice: a replay inserts no notice, so no second bell row
            await NotificationService(conn).emit(
                event_type=EventType.TOUR_PAGES_TO_PUT_BACK, entity_type="tour", entity_id=event["tour_id"],
                tenant_id=tid, payload={"tour_name": tour_name, "event_key": event["idempotency_key"],
                                        "pages": pages, "count": len(pages)})
            put_back_told += 1
    return {"flags_resolved": len(resolved), "tenants_noticed": len(tenants), "immediate_notices": immediate,
            "pages_drafted": sum(len(drafted.get(t, [])) for t in tenants), "tenants_told_put_back": put_back_told}


async def enqueue_due_drafts(pool, max_attempts: int) -> int:
    try:
        return int(await pool.fetchval(DUE_DRAFTS_SQL, max_attempts) or 0)
    except asyncpg.UndefinedTableError:
        return 0


# ── the WordPress move to draft (one flag) ────────────────────────────────────────────────────────────────────────

FLAG_SQL = f"""
    SELECT f.flag_id::text AS flag_id, f.tenant_id::text AS tenant_id, f.state, f.action, f.draft_due_at,
           f.created_at, f.detail ->> 'tour_name' AS tour_name,
           f.tour_id::text AS tour_id, f.publish_id::text AS publish_id, f.piece_id::text AS piece_id,
           pl.status AS publish_status, pl.unpublished_at, pl.external_id, pl.external_url, pl.channel
      FROM acp_shared.content_compliance_flag f
      LEFT JOIN acp_shared.publish_log pl ON pl.publish_id = f.publish_id
     WHERE f.flag_id = $1::uuid AND f.kind = '{FLAG_KIND}'
"""

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

# The job's re-read of the opt-in (CIS 397): the agency turned automatic drafts off after this flag was written ($2 =
# the flag's created_at). A flag of an agency that never had a row, or whose row is older, keeps its draft date (open
# draft_after_notice flags from before S276 are left as they are, tech lead decision S276).
OPTED_OUT_SINCE_SQL = """
    SELECT EXISTS (SELECT 1 FROM acp_shared.tour_retirement_policy
                    WHERE tenant_id = $1::uuid AND NOT auto_draft AND updated_at > $2) AS opted_out
"""

TO_NOTICE_ONLY_SQL = """
    UPDATE acp_shared.content_compliance_flag
       SET action = 'notice_only', draft_due_at = NULL, tenant_message = $2, updated_at = now(),
           detail = detail || jsonb_build_object('auto_draft_off_at', now())
     WHERE flag_id = $1::uuid AND state = 'open' AND action = 'draft_after_notice'
"""

MARK_UNPUBLISHED_SQL = """
    UPDATE acp_shared.publish_log SET status = 'unpublished', unpublished_at = now(), unpublished_by = $3
     WHERE publish_id = $1::uuid AND tenant_id = $2::uuid AND status = 'published'
"""

INTEGRATION_SQL = ("SELECT secret_key FROM shared.tenant_integrations "
                   "WHERE tenant_id = $1::uuid AND integration_type = 'wordpress'")


class DraftRefused(Exception):
    """The page cannot be moved on the tenant's behalf (no connection, another site, WordPress refused): the flag
    keeps the reason, staff and the tenant's mail already say what to do. `permanent` = a retry cannot help."""

    def __init__(self, reason: str, permanent: bool = True):
        super().__init__(reason, permanent)
        self.reason = reason
        self.permanent = permanent


async def move_to_draft(pool, flag_id: str, *, read_secret, adapter_for, actor: str = ACTOR,
                        tenant_id: Optional[str] = None) -> dict:
    """Move the WordPress page of one open ``tour_retired`` flag to draft, after re-checking that it still should be:
    the tour is still not served, the piece's Segment Subject (if any) has no served member again (AA-1057 GAP 1),
    the post is still live. `read_secret(name) -> dict` and `adapter_for(creds) -> WordPressAdapter` are injected
    (Secrets Manager / safe_http in the job and the route, fakes in tests).

    Two callers. The ``retired_page_draft`` job (``actor`` = the system, ``tenant_id`` None): only a
    ``draft_after_notice`` flag past its ``draft_due_at``, and only while the agency has not turned automatic drafts
    off since the flag was written (else the flag becomes ``notice_only``). The agency's own click (``actor`` = the
    tenant, ``tenant_id`` its id; tech lead decision S276): any open WordPress flag of that tenant, at once. Either way
    the ``publish_log`` row is marked unpublished by ``system:tour_retirement`` (the retirement's draft, which the
    restore notice and "Put back online" read) and the flag is resolved ``drafted`` by ``actor``. The flag is resolved
    first in the final transaction (``state = 'open'``), so of two concurrent runs only one records the draft and
    audits it; the other answers ``already_drafted``."""
    from services.acp_publish.dispatch import other_account

    by_tenant = tenant_id is not None
    async with pool.acquire() as conn:
        flag = await conn.fetchrow(FLAG_SQL, flag_id)
        if flag is None or (by_tenant and flag["tenant_id"] != str(tenant_id)):
            return {"status": "skipped", "reason": "flag_not_found"}
        if flag["state"] != "open":
            return {"status": "skipped", "reason": f"flag_{flag['state']}"}
        if by_tenant:
            if flag["channel"] not in WORDPRESS_CHANNELS:
                return {"status": "skipped", "reason": "not_wordpress"}
        else:
            if flag["action"] != "draft_after_notice":
                return {"status": "skipped", "reason": f"flag_{flag['action']}"}
            if flag["draft_due_at"] and flag["draft_due_at"] > datetime.now(flag["draft_due_at"].tzinfo):
                return {"status": "skipped", "reason": "not_due"}
            if await _opted_out_since(conn, flag["tenant_id"], flag["created_at"]):
                await conn.execute(TO_NOTICE_ONLY_SQL, flag_id,
                                   page_notice_message(flag["tour_name"] or "this tour"))
                return {"status": "skipped", "reason": "auto_draft_off"}
        if (await conn.fetchval(TOUR_STATE_SQL, flag["tour_id"])) in ("served", None):
            await conn.execute(RESOLVE_ONE_SQL, flag_id, "restored", ACTOR, "{}")
            return {"status": "resolved", "resolution": "restored"}
        if flag["piece_id"] and await conn.fetchval(PIECE_SUBJECT_ALIVE_SQL, flag["piece_id"]):
            await conn.execute(RESOLVE_ONE_SQL, flag_id, "restored", ACTOR, json.dumps({"subject_alive": True}))
            return {"status": "resolved", "resolution": "restored", "subject_alive": True}
        if flag["publish_status"] != "published" or flag["unpublished_at"] is not None:
            await conn.execute(RESOLVE_ONE_SQL, flag_id, "unpublished", ACTOR, "{}")
            return {"status": "resolved", "resolution": "unpublished"}
        integ = await conn.fetchrow(INTEGRATION_SQL, flag["tenant_id"])
    if not integ or not integ["secret_key"]:
        await _note(pool, flag_id, "WordPress is not connected any more; the page was not moved")
        raise DraftRefused("wordpress_not_connected")
    creds = await read_secret(integ["secret_key"])
    other = other_account("blog", creds, str(flag["external_id"] or ""), flag["external_url"])
    if other:
        await _note(pool, flag_id, f"the page lives on {other}, not on the connected site; it was not moved")
        raise DraftRefused("other_site")
    try:
        outcome = await adapter_for(creds).set_draft(flag["external_id"])
    except Exception as exc:  # noqa: BLE001 — recorded on the flag, the job decides on the retry
        await _note(pool, flag_id, str(exc)[:300])
        raise DraftRefused(str(exc)[:300], permanent=False) from exc
    note = {"wordpress": outcome, "drafted_by": "tenant" if by_tenant else "auto_draft"}
    async with pool.acquire() as conn:
        async with conn.transaction():
            resolved = await conn.fetchval(RESOLVE_ONE_SQL, flag_id, "drafted", actor, json.dumps(note))
            if resolved:
                await conn.execute(MARK_UNPUBLISHED_SQL, flag["publish_id"], flag["tenant_id"], ACTOR)
    if not resolved:     # another run (the job, a second click) recorded it between the read and here
        return {"status": "already_drafted", "publish_id": flag["publish_id"], "tenant_id": flag["tenant_id"],
                "piece_id": flag["piece_id"]}
    return {"status": "drafted", "outcome": outcome, "publish_id": flag["publish_id"],
            "tenant_id": flag["tenant_id"], "piece_id": flag["piece_id"]}


async def _opted_out_since(conn, tenant_id: str, since) -> bool:
    """True when the agency turned automatic drafts off after ``since`` (False before CIS 397 / 360)."""
    try:
        async with conn.transaction():
            return bool(await conn.fetchval(OPTED_OUT_SINCE_SQL, tenant_id, since))
    except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
        return False


async def _note(pool, flag_id: str, text: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(NOTE_SQL, flag_id, json.dumps({"draft_error": text}))


def immediate_ids(result: dict[str, Any]) -> list[str]:
    return list(result.get("immediate_notices") or [])


__all__ = ["DEFAULT_NOTICE_DAYS", "DraftRefused", "MIGRATION", "POLICY_MIGRATION", "SchemaMissing",
           "enqueue_due_drafts", "fan_out", "immediate_ids", "move_to_draft", "notice_days", "page_message",
           "page_notice_message", "policy", "post_message"]
