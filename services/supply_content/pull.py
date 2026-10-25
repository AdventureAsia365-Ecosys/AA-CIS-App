"""AA-835 — the CIS consumer of the Booking supply feed (contracts supply.hotel.v1 / supply.service.v1, ADR 0004a).

One pull (``pull_once``), run by the api's ops sampler every OPS_METRICS_INTERVAL_S (api/core/ops_metrics_sampler.py)
as cis_app, DB only:

  1. lock this consumer's row in shared.supply_feed_cursor (FOR UPDATE SKIP LOCKED: a second api task during a
     rolling deploy skips the tick instead of pulling the same events twice);
  2. read ``booking.v_supply_outbox WHERE feed_seq > cursor ORDER BY feed_seq LIMIT n``;
  3. read the CURRENT facts of those entities from booking.v_supply_hotels / v_supply_services (outbox rows carry
     typed ids only; the facts row says which supply_version it reflects, so a late read is safe);
  4. per event, in feed order: a key already in shared.supply_event_inbox is a duplicate (no row, no change).
     Otherwise the outcome is ``applied`` when the facts' supply_version is newer than the entity's
     applied_supply_version, ``skipped_older`` when it is not, ``missing`` when the entity left the facts view;
     the inbox row records it;
  5. an applied upsert stores the facts, syncs shared.supply_media and queues one ``supply_content_draft`` job
     (key supply_content_draft:<source_id>:<supply_version>); a deactivate (or a missing entity) sets active false
     and content_status unpublished, rows kept. Content that was published becomes ``stale`` (Booking bumps
     supply_version only on a material change, so every applied version is material). A media whose CIS copies
     exist but whose usage_rights went off, or which Booking removed (or whose entity left the facts view), gets one
     ``supply_media_purge`` job (key supply_media_purge:<media_id>:<copied_at>): the copies leave the photo bucket
     and the CDN (services/jobs/supply_media_purge_job.py, DM-7 row "supply_media");
  6. advance the cursor and stamp last_pull_at / last_success_at, all in the same transaction.

A failure rolls the batch back and records last_error / last_error_at (the admin status board shows them); the next
tick retries from the same cursor. Nothing here calls an LLM: the draft job does (background kind, own cap).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

CONSUMER = "cis_supply_content"
BATCH = 200
SOURCE = "booking"
CONTRACTS = {"hotel": "supply.hotel.v1", "service": "supply.service.v1"}
EVENT_TYPES = ("supply.upsert", "supply.deactivate")
DRAFT_KIND = "supply_content_draft"
DRAFT_MAX_ATTEMPTS = 2
PURGE_KIND = "supply_media_purge"
PURGE_MAX_ATTEMPTS = 3

ENSURE_CURSOR_SQL = """
    INSERT INTO shared.supply_feed_cursor (consumer) VALUES ($1) ON CONFLICT (consumer) DO NOTHING
"""
LOCK_CURSOR_SQL = """
    SELECT last_feed_seq FROM shared.supply_feed_cursor WHERE consumer = $1 FOR UPDATE SKIP LOCKED
"""
OUTBOX_SQL = """
    SELECT o.event_id::text AS event_id, o.event_type, o.entity_type, o.entity_id::text AS entity_id,
           o.supply_version, o.occurred_at, o.feed_seq
      FROM booking.v_supply_outbox o
     WHERE o.feed_seq > $1
     ORDER BY o.feed_seq
     LIMIT $2
"""
# The facts object of the contract = the facts view row without its key columns (the view's columns are the
# contract fields, bk-023); to_jsonb renders times as HH:MM:SS, numerics as numbers.
FACTS_SQL = {
    "hotel": """
        SELECT v.id::text AS id, v.supply_version, (to_jsonb(v) - 'id' - 'supply_version')::text AS facts
          FROM booking.v_supply_hotels v
         WHERE v.id = ANY($1::uuid[])
    """,
    "service": """
        SELECT v.id::text AS id, v.supply_version, (to_jsonb(v) - 'id' - 'supply_version')::text AS facts
          FROM booking.v_supply_services v
         WHERE v.id = ANY($1::uuid[])
    """,
}
ENTITIES_SQL = """
    SELECT id::text AS id, source_id, entity_type, applied_supply_version, active, content_status,
           published_version_id::text AS published_version_id
      FROM shared.supply_entity
     WHERE source_system = 'booking' AND source_id = ANY($1::text[])
     FOR UPDATE
"""
INBOX_SEEN_SQL = """
    SELECT 1 FROM shared.supply_event_inbox WHERE source = 'booking' AND contract = $1 AND idempotency_key = $2::uuid
"""
INBOX_INSERT_SQL = """
    INSERT INTO shared.supply_event_inbox
        (contract, idempotency_key, source_id, event_type, supply_version, facts_supply_version, feed_seq, outcome)
    VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8)
"""
ENTITY_INSERT_SQL = """
    INSERT INTO shared.supply_entity
        (source_id, entity_type, service_type, title, country_code, facts, applied_supply_version, active,
         content_status)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
    RETURNING id::text
"""
ENTITY_UPDATE_SQL = """
    UPDATE shared.supply_entity
       SET service_type = $2, title = $3, country_code = $4, facts = $5::jsonb, applied_supply_version = $6,
           active = $7, content_status = $8, updated_at = now()
     WHERE id = $1::uuid
"""
ENTITY_UNPUBLISH_SQL = """
    UPDATE shared.supply_entity SET active = false, content_status = 'unpublished', updated_at = now()
     WHERE id = $1::uuid
"""
MEDIA_UPSERT_SQL = """
    INSERT INTO shared.supply_media (booking_media_id, entity_id, usage_rights, active, sha256, content_type, width,
                                     height)
    VALUES ($1::uuid, $2::uuid, $3, true, $4, $5, $6, $7)
    ON CONFLICT (booking_media_id) DO UPDATE
       SET entity_id = excluded.entity_id, usage_rights = excluded.usage_rights, active = true,
           sha256 = excluded.sha256, content_type = excluded.content_type, width = excluded.width,
           height = excluded.height, updated_at = now()
"""
MEDIA_DEACTIVATE_SQL = """
    UPDATE shared.supply_media SET active = false, updated_at = now()
     WHERE entity_id = $1::uuid AND active AND NOT (booking_media_id = ANY($2::uuid[]))
"""
MEDIA_DEACTIVATE_ALL_SQL = """
    UPDATE shared.supply_media SET active = false, updated_at = now() WHERE entity_id = $1::uuid AND active
"""
# Copies that must go: rights withdrawn or media removed in Booking. copied_at (epoch microseconds) names the copy, so
# a later re-copy of the same bytes that loses its rights again gets a new purge job, not the finished old one.
MEDIA_TO_PURGE_SQL = """
    SELECT booking_media_id::text AS media_id, copied_sha256,
           (extract(epoch FROM copied_at) * 1000000)::bigint AS copied_at_us
      FROM shared.supply_media
     WHERE entity_id = $1::uuid AND copied_sha256 IS NOT NULL AND NOT (usage_rights AND active)
"""
CURSOR_ADVANCE_SQL = """
    UPDATE shared.supply_feed_cursor
       SET last_feed_seq = $2, last_pull_at = now(), last_success_at = now(), last_error = NULL, last_error_at = NULL
     WHERE consumer = $1
"""
CURSOR_ERROR_SQL = """
    UPDATE shared.supply_feed_cursor SET last_pull_at = now(), last_error = $2, last_error_at = now()
     WHERE consumer = $1
"""


@dataclass
class EntityState:
    id: str
    applied_supply_version: int
    active: bool
    content_status: str
    published_version_id: Optional[str]


def draft_key(source_id: str, supply_version: int) -> str:
    return f"{DRAFT_KIND}:{source_id}:{supply_version}"


def purge_key(media_id: str, copied_at_us: int) -> str:
    return f"{PURGE_KIND}:{media_id}:{copied_at_us}"


def outcome_for(entity: Optional[EntityState], facts_version: Optional[int]) -> str:
    """applied | skipped_older | missing for one (non-duplicate) event."""
    if facts_version is None:
        return "missing"
    if entity is not None and facts_version <= entity.applied_supply_version:
        return "skipped_older"
    return "applied"


def status_after_apply(entity: Optional[EntityState], active: bool) -> str:
    """content_status after newer facts were applied.

    inactive -> unpublished (rows kept). Active: content that was ever published (published_version_id) goes stale,
    so Booking shows no text until a version written against the new facts is approved; anything else waits for
    the new draft (draft)."""
    if not active:
        return "unpublished"
    if entity is not None and entity.published_version_id is not None:
        return "stale"
    return "draft"


def _title(entity_type: str, facts: dict) -> str:
    raw = facts.get("name") if entity_type == "hotel" else facts.get("working_title")
    return (str(raw or "").strip() or "(untitled)")[:200]


def _media(facts: dict) -> list[dict]:
    out = []
    for m in facts.get("media") or []:
        if isinstance(m, dict) and m.get("media_id") and m.get("sha256"):
            out.append(m)
    return out


async def _apply(conn, entity: Optional[EntityState], *, entity_type: str, source_id: str, facts: dict,
                 version: int) -> tuple[EntityState, bool]:
    """Store newer facts; returns (state, active)."""
    active = facts.get("status") == "active"
    status = status_after_apply(entity, active)
    service_type = facts.get("service_type") if entity_type == "service" else None
    args = (service_type, _title(entity_type, facts), facts.get("country_code"), json.dumps(facts, default=str),
            version, active, status)
    if entity is None:
        entity_id = await conn.fetchval(ENTITY_INSERT_SQL, source_id, entity_type, *args)
        entity = EntityState(entity_id, version, active, status, None)
    else:
        await conn.execute(ENTITY_UPDATE_SQL, entity.id, *args)
        entity = EntityState(entity.id, version, active, status, entity.published_version_id)
    media = _media(facts)
    for m in media:
        await conn.execute(MEDIA_UPSERT_SQL, m["media_id"], entity.id, bool(m.get("usage_rights")), m["sha256"],
                           m.get("content_type") or "image/jpeg", m.get("width"), m.get("height"))
    await conn.execute(MEDIA_DEACTIVATE_SQL, entity.id, [m["media_id"] for m in media])
    return entity, active


async def _enqueue_draft(conn, source_id: str, version: int) -> bool:
    from shared.jobs import queue

    _, created = await queue.enqueue(conn, DRAFT_KIND, {"source_id": source_id, "supply_version": version,
                                                        "trigger": "feed"},
                                     idempotency_key=draft_key(source_id, version), max_attempts=DRAFT_MAX_ATTEMPTS,
                                     created_by="system:supply_feed")
    return created


async def _enqueue_purges(conn, entity_id: str) -> int:
    """One purge job per CIS copy that lost its rights or its media (idempotent: the key names the copy)."""
    from shared.jobs import queue

    n = 0
    for r in await conn.fetch(MEDIA_TO_PURGE_SQL, entity_id):
        _, created = await queue.enqueue(conn, PURGE_KIND, {"media_id": r["media_id"],
                                                            "copied_sha256": r["copied_sha256"],
                                                            "copied_at_us": int(r["copied_at_us"])},
                                         idempotency_key=purge_key(r["media_id"], int(r["copied_at_us"])),
                                         max_attempts=PURGE_MAX_ATTEMPTS, created_by="system:supply_feed")
        n += int(bool(created))
    return n


async def _pull_batch(conn, limit: int, enqueue_jobs: bool) -> dict[str, Any]:
    await conn.execute(ENSURE_CURSOR_SQL, CONSUMER)
    cursor = await conn.fetchval(LOCK_CURSOR_SQL, CONSUMER)
    if cursor is None:
        return {"busy": True}
    events = await conn.fetch(OUTBOX_SQL, cursor, limit)
    counts = {"events": len(events), "applied": 0, "skipped_older": 0, "missing": 0, "duplicate": 0,
              "drafts_queued": 0, "unpublished": 0, "purges_queued": 0}
    if not events:
        await conn.execute(CURSOR_ADVANCE_SQL, CONSUMER, cursor)
        return {**counts, "cursor": cursor}

    facts: dict[str, tuple[int, dict]] = {}
    for entity_type in CONTRACTS:
        ids = sorted({e["entity_id"] for e in events if e["entity_type"] == entity_type})
        if ids:
            for r in await conn.fetch(FACTS_SQL[entity_type], ids):
                facts[r["id"]] = (int(r["supply_version"]), json.loads(r["facts"]))
    entities = {r["source_id"]: EntityState(r["id"], int(r["applied_supply_version"]), r["active"],
                                            r["content_status"], r["published_version_id"])
                for r in await conn.fetch(ENTITIES_SQL, sorted({e["entity_id"] for e in events}))}

    for e in events:
        entity_type, source_id = e["entity_type"], e["entity_id"]
        contract = CONTRACTS.get(entity_type)
        if contract is None or e["event_type"] not in EVENT_TYPES:
            raise ValueError(f"supply feed event {e['event_id']} has entity_type {entity_type!r} / event_type "
                             f"{e['event_type']!r} outside the contract")
        if await conn.fetchval(INBOX_SEEN_SQL, contract, e["event_id"]):
            counts["duplicate"] += 1
            continue
        current = facts.get(source_id)
        entity = entities.get(source_id)
        outcome = outcome_for(entity, current[0] if current else None)
        if outcome == "applied":
            version, data = current
            entity, active = await _apply(conn, entity, entity_type=entity_type, source_id=source_id, facts=data,
                                          version=version)
            entities[source_id] = entity
            if not active:
                counts["unpublished"] += 1
            elif enqueue_jobs and await _enqueue_draft(conn, source_id, version):
                counts["drafts_queued"] += 1
            if enqueue_jobs:
                counts["purges_queued"] += await _enqueue_purges(conn, entity.id)
        elif outcome == "missing" and entity is not None:
            if entity.active or entity.content_status != "unpublished":
                await conn.execute(ENTITY_UNPUBLISH_SQL, entity.id)
                entities[source_id] = EntityState(entity.id, entity.applied_supply_version, False, "unpublished",
                                                  entity.published_version_id)
                counts["unpublished"] += 1
            # the entity left the facts view, so its media did too
            await conn.execute(MEDIA_DEACTIVATE_ALL_SQL, entity.id)
            if enqueue_jobs:
                counts["purges_queued"] += await _enqueue_purges(conn, entity.id)
        await conn.execute(INBOX_INSERT_SQL, contract, e["event_id"], source_id, e["event_type"],
                           int(e["supply_version"]), current[0] if current else None, int(e["feed_seq"]), outcome)
        counts[outcome] += 1

    last = int(events[-1]["feed_seq"])
    await conn.execute(CURSOR_ADVANCE_SQL, CONSUMER, last)
    return {**counts, "cursor": last}


async def pull_once(pool, *, limit: int = BATCH, enqueue_jobs: bool = True) -> dict[str, Any]:
    """One batch of the feed (see the module docstring). Returns the counts; raises after recording last_error."""
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                return await _pull_batch(conn, limit, enqueue_jobs)
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            try:
                await conn.execute(CURSOR_ERROR_SQL, CONSUMER, message)
            except Exception as inner:  # the cursor table itself is unreadable (278 not applied): log only
                logger.warning("supply_pull_error_not_recorded", error=type(inner).__name__)
            raise
