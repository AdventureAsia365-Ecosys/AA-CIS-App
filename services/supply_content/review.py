"""AA-835 — the human review of supply content (used by /admin/supply-content and, for evidence runs, ECS exec).

Rules (the DB adds its own: CIS 278 version guard, one approved version per entity, composite FK):
  - an edit is a NEW ``human`` version written against the entity's current facts version; content never changes
    in place;
  - publish takes a draft (or re-publishes the entity's own last approved version after an unpublish). It is refused
    (409) when the entity is inactive, when the version was written against older facts (``outdated``: the facts
    changed, a new draft is coming), or when a photo of the version is not rights-cleared and copied
    (``photo_rights``: usage_rights off in Booking, removed, or not copied into the CIS photo store);
  - publishing supersedes the previous approved version and every other open draft of the entity;
  - reject closes one draft; unpublish hides the content from Booking (rows kept, the version stays approved);
  - nothing here calls an LLM: ``request_draft`` queues the supply_content_draft job.
Every write runs in one transaction with the entity row locked (FOR UPDATE).
"""
from __future__ import annotations

import difflib
import json
import uuid
from typing import Any, Optional

from services.supply_content import writer
from services.supply_content.pull import DRAFT_KIND, DRAFT_MAX_ATTEMPTS, draft_key

MAX_DESCRIPTION_CHARS = 2000
MAX_LIST = 50


class ReviewError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: Optional[dict] = None):
        super().__init__(status, code, message, extra)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}

    def detail(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


ENTITY_SQL = """
    SELECT id::text AS id, source_system, source_id, entity_type, service_type, title, country_code,
           facts::text AS facts, applied_supply_version, active, content_status,
           published_version_id::text AS published_version_id, created_at, updated_at
      FROM shared.supply_entity WHERE id = $1::uuid
"""
VERSIONS_SQL = """
    SELECT id::text AS id, content_version, supply_version, origin, description, highlights, good_to_know,
           photos::text AS photos, status, based_on_version_id::text AS based_on_version_id,
           llm_job_id::text AS llm_job_id, created_by, created_at, reviewed_by, reviewed_at, review_note
      FROM shared.supply_content_version WHERE entity_id = $1::uuid ORDER BY content_version DESC
"""
MEDIA_SQL = """
    SELECT booking_media_id::text AS media_id, usage_rights, active, content_type, width, height, url_600, url_1600,
           copied_at
      FROM shared.supply_media WHERE entity_id = $1::uuid ORDER BY updated_at, booking_media_id
"""


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _entity(row: Any) -> dict:
    out = {k: _iso(row[k]) for k in row.keys() if k != "facts"}
    out["facts"] = json.loads(row["facts"])
    return out


def _version(row: Any) -> dict:
    out = {k: _iso(row[k]) for k in row.keys() if k != "photos"}
    out["highlights"] = list(row["highlights"] or [])
    out["good_to_know"] = list(row["good_to_know"] or [])
    out["photos"] = json.loads(row["photos"]) if row["photos"] else []
    return out


def booking_path(entity_type: str, source_id: str) -> str:
    """Path of the record in the Booking staff admin (prefix the Booking admin origin)."""
    return f"/hotels?hotel={source_id}" if entity_type == "hotel" else f"/services/{source_id}"


def content_text(v: Optional[dict]) -> list[str]:
    if not v:
        return []
    lines = [v["description"], ""]
    lines += [f"* {h}" for h in v.get("highlights") or []]
    lines += [f"i {g}" for g in v.get("good_to_know") or []]
    lines += [f"photo {p.get('media_id')}" for p in v.get("photos") or []]
    return lines


def diff(base: Optional[dict], draft: Optional[dict]) -> list[str]:
    if not draft:
        return []
    return list(difflib.unified_diff(content_text(base), content_text(draft),
                                     fromfile=f"v{base['content_version']}" if base else "(none)",
                                     tofile=f"v{draft['content_version']}", lineterm=""))


async def list_entities(pool, *, status: Optional[str] = None, entity_type: Optional[str] = None,
                        limit: int = 50, offset: int = 0) -> dict:
    rows = await pool.fetch(
        """
        SELECT e.id::text AS id, e.source_id, e.entity_type, e.service_type, e.title, e.country_code,
               e.applied_supply_version, e.active, e.content_status, e.updated_at,
               (SELECT count(*) FROM shared.supply_content_version v
                 WHERE v.entity_id = e.id AND v.status = 'draft') AS open_drafts,
               count(*) OVER () AS total
          FROM shared.supply_entity e
         WHERE ($1::text IS NULL OR e.content_status = $1) AND ($2::text IS NULL OR e.entity_type = $2)
         ORDER BY e.updated_at DESC, e.id
         LIMIT $3 OFFSET $4
        """, status, entity_type, limit, offset)
    counts = await pool.fetch("SELECT content_status, count(*) AS n FROM shared.supply_entity GROUP BY 1")
    return {"items": [{k: _iso(r[k]) for k in r.keys() if k != "total"} for r in rows],
            "total": int(rows[0]["total"]) if rows else 0,
            "by_status": {r["content_status"]: int(r["n"]) for r in counts}}


async def feed_status(pool) -> dict:
    """The feed cursor, its lag behind booking.v_supply_outbox and the last error (observability rule)."""
    from services.supply_content.pull import CONSUMER

    cur = await pool.fetchrow("SELECT last_feed_seq, last_pull_at, last_success_at, last_error, last_error_at "
                              "FROM shared.supply_feed_cursor WHERE consumer = $1", CONSUMER)
    cursor = int(cur["last_feed_seq"]) if cur else 0
    out: dict[str, Any] = {"consumer": CONSUMER, "cursor": cursor,
                           **({k: _iso(cur[k]) for k in cur.keys() if k != "last_feed_seq"} if cur else {})}
    try:
        lag = await pool.fetchrow(
            "SELECT max(feed_seq) AS head, count(*) FILTER (WHERE feed_seq > $1) AS pending, "
            "min(occurred_at) FILTER (WHERE feed_seq > $1) AS oldest_pending_at FROM booking.v_supply_outbox", cursor)
        out.update(feed_available=True, head=lag["head"], pending=int(lag["pending"] or 0),
                   oldest_pending_at=_iso(lag["oldest_pending_at"]))
    except Exception as exc:   # bk-023 not applied / no grant: the board says so instead of failing
        out.update(feed_available=False, feed_error=type(exc).__name__)
    jobs = await pool.fetch("SELECT status, count(*) AS n FROM shared.job WHERE kind = $1 "
                            "AND created_at > now() - interval '7 days' GROUP BY 1", DRAFT_KIND)
    out["draft_jobs_7d"] = {r["status"]: int(r["n"]) for r in jobs}
    out["llm_cost_today_usd"] = float(await pool.fetchval(
        "SELECT coalesce(sum(cost_usd), 0) FROM shared.llm_call_log WHERE stage = $1 "
        "AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'", writer.STAGE) or 0)
    return out


async def get_detail(pool, entity_id: str) -> dict:
    row = await pool.fetchrow(ENTITY_SQL, entity_id)
    if row is None:
        raise ReviewError(404, "not_found", "no supply entity with this id")
    entity = _entity(row)
    versions = [_version(r) for r in await pool.fetch(VERSIONS_SQL, entity_id)]
    published = next((v for v in versions if v["id"] == entity["published_version_id"]), None)
    draft = next((v for v in versions if v["status"] == "draft"), None)
    base = published or next((v for v in versions if v["status"] != "draft" and v is not draft), None)
    return {"entity": entity, "versions": versions, "current_draft": draft, "published": published,
            "diff_base_version": base["content_version"] if base else None, "diff": diff(base, draft),
            "media": [{k: _iso(r[k]) for k in r.keys()} for r in await pool.fetch(MEDIA_SQL, entity_id)],
            "edit_in_booking_path": booking_path(entity["entity_type"], entity["source_id"])}


async def _lock(conn, entity_id: str) -> Any:
    row = await conn.fetchrow(ENTITY_SQL + " FOR UPDATE", entity_id)
    if row is None:
        raise ReviewError(404, "not_found", "no supply entity with this id")
    return row


def _texts(values: Any, cap: int, name: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > cap:
        raise ReviewError(422, "invalid", f"{name}: a list of at most {cap} items")
    out = [" ".join(str(v).split()) for v in values]
    if any(not v or len(v) > writer.MAX_ITEM_CHARS for v in out):
        raise ReviewError(422, "invalid", f"{name}: items must be 1-{writer.MAX_ITEM_CHARS} characters")
    return out


async def _photos(conn, entity_id: str, photos: Any) -> list[dict]:
    if photos is None:
        return []
    if not isinstance(photos, list) or len(photos) > MAX_LIST:
        raise ReviewError(422, "invalid", f"photos: a list of at most {MAX_LIST} items")
    known = {r["media_id"] for r in await conn.fetch(
        "SELECT booking_media_id::text AS media_id FROM shared.supply_media WHERE entity_id = $1::uuid", entity_id)}
    out = []
    for p in photos:
        media_id = str((p or {}).get("media_id") or "") if isinstance(p, dict) else ""
        if media_id not in known:
            raise ReviewError(422, "unknown_media", "photos: every media_id must be a photo of this entity",
                              {"media_id": media_id})
        out.append({"media_id": media_id, "caption": _opt(p.get("caption")), "alt": _opt(p.get("alt"))})
    return out


def _opt(v: Any) -> Optional[str]:
    text = " ".join(str(v).split())[:writer.MAX_ITEM_CHARS] if v is not None else ""
    return text or None


async def _insert_version(conn, entity: Any, *, origin: str, content: dict, created_by: str,
                          based_on: Optional[str] = None, llm_job_id: Optional[str] = None) -> dict:
    number = await conn.fetchval("SELECT coalesce(max(content_version), 0) + 1 FROM shared.supply_content_version "
                                 "WHERE entity_id = $1::uuid", entity["id"])
    row = await conn.fetchrow(
        """
        INSERT INTO shared.supply_content_version
            (entity_id, content_version, supply_version, origin, description, highlights, good_to_know, photos,
             based_on_version_id, llm_job_id, created_by)
        VALUES ($1::uuid, $2, $3, $4, $5, $6::text[], $7::text[], $8::jsonb, $9::uuid, $10::uuid, $11)
        RETURNING id::text AS id, content_version, supply_version, origin, description, highlights, good_to_know,
                  photos::text AS photos, status, based_on_version_id::text AS based_on_version_id,
                  llm_job_id::text AS llm_job_id, created_by, created_at, reviewed_by, reviewed_at, review_note
        """,
        entity["id"], number, int(entity["applied_supply_version"]), origin, content["description"],
        content["highlights"], content["good_to_know"], json.dumps(content["photos"]), based_on, llm_job_id,
        created_by)
    if entity["content_status"] == "draft":
        await conn.execute("UPDATE shared.supply_entity SET content_status = 'in_review', updated_at = now() "
                           "WHERE id = $1::uuid", entity["id"])
    return _version(row)


async def create_human_version(conn, entity_id: str, body: dict, *, actor: str) -> dict:
    """An edit: a new human draft against the current facts."""
    async with conn.transaction():
        entity = await _lock(conn, entity_id)
        if not entity["active"]:
            raise ReviewError(409, "inactive", "the entity is inactive in Booking")
        description = " ".join(str(body.get("description") or "").split())
        if not description or len(description) > MAX_DESCRIPTION_CHARS:
            raise ReviewError(422, "invalid", f"description: 1-{MAX_DESCRIPTION_CHARS} characters")
        based_on = body.get("based_on_version_id")
        if based_on and not await conn.fetchval("SELECT 1 FROM shared.supply_content_version WHERE id = $1::uuid "
                                                "AND entity_id = $2::uuid", str(based_on), entity_id):
            raise ReviewError(422, "invalid", "based_on_version_id is not a version of this entity")
        content = {"description": description,
                   "highlights": _texts(body.get("highlights"), writer.MAX_HIGHLIGHTS, "highlights"),
                   "good_to_know": _texts(body.get("good_to_know"), writer.MAX_GOOD_TO_KNOW, "good_to_know"),
                   "photos": await _photos(conn, entity_id, body.get("photos"))}
        return await _insert_version(conn, entity, origin="human", content=content, created_by=actor,
                                     based_on=str(based_on) if based_on else None)


async def insert_llm_draft(conn, entity_id: str, supply_version: int, content: dict, *, job_id: Optional[str],
                           created_by: str) -> Optional[dict]:
    """The draft job's write. None when the facts moved on meanwhile (the newer version's job writes its own) or
    the entity was deactivated. Older open llm drafts are superseded."""
    async with conn.transaction():
        entity = await _lock(conn, entity_id)
        if not entity["active"] or int(entity["applied_supply_version"]) != supply_version:
            return None
        await conn.execute("UPDATE shared.supply_content_version SET status = 'superseded' "
                           "WHERE entity_id = $1::uuid AND status = 'draft' AND supply_version < $2",
                           entity_id, supply_version)
        return await _insert_version(conn, entity, origin="llm", content=content, created_by=created_by,
                                     llm_job_id=job_id)


async def _photo_problems(conn, entity_id: str, photos: list) -> list[str]:
    ids = [str(p.get("media_id")) for p in photos if isinstance(p, dict)]
    if not ids:
        return []
    ok = {r["media_id"] for r in await conn.fetch(
        "SELECT booking_media_id::text AS media_id FROM shared.supply_media WHERE entity_id = $1::uuid "
        "AND booking_media_id::text = ANY($2::text[]) AND usage_rights AND active AND url_1600 IS NOT NULL",
        entity_id, ids)}
    return [i for i in ids if i not in ok]


async def publish(conn, entity_id: str, version_id: str, *, actor: str, note: Optional[str] = None) -> dict:
    async with conn.transaction():
        entity = await _lock(conn, entity_id)
        v = await conn.fetchrow("SELECT id::text AS id, status, supply_version, photos::text AS photos "
                                "FROM shared.supply_content_version WHERE id = $1::uuid AND entity_id = $2::uuid "
                                "FOR UPDATE", version_id, entity_id)
        if v is None:
            raise ReviewError(404, "not_found", "no version with this id for this entity")
        republish = (v["status"] == "approved" and v["id"] == entity["published_version_id"]
                     and entity["content_status"] in ("unpublished", "stale"))
        if v["status"] != "draft" and not republish:
            raise ReviewError(409, "not_a_draft", f"version is {v['status']}")
        if not entity["active"]:
            raise ReviewError(409, "inactive", "the entity is inactive in Booking")
        if int(v["supply_version"]) != int(entity["applied_supply_version"]):
            raise ReviewError(409, "outdated",
                              "the facts changed after this version was written; review the new draft or write an edit",
                              {"supply_version": int(v["supply_version"]),
                               "current_supply_version": int(entity["applied_supply_version"])})
        bad = await _photo_problems(conn, entity_id, json.loads(v["photos"] or "[]"))
        if bad:
            raise ReviewError(409, "photo_rights", "a photo has no usage rights in Booking or is not copied yet",
                              {"media_ids": bad})
        if not republish:
            await conn.execute("UPDATE shared.supply_content_version SET status = 'superseded' "
                               "WHERE entity_id = $1::uuid AND status = 'approved'", entity_id)
            await conn.execute("UPDATE shared.supply_content_version SET status = 'superseded' "
                               "WHERE entity_id = $1::uuid AND status = 'draft' AND id <> $2::uuid",
                               entity_id, version_id)
            await conn.execute("UPDATE shared.supply_content_version SET status = 'approved', reviewed_by = $2, "
                               "reviewed_at = now(), review_note = $3 WHERE id = $1::uuid", version_id, actor,
                               _opt(note))
        await conn.execute("UPDATE shared.supply_entity SET content_status = 'published', published_version_id = "
                           "$2::uuid, updated_at = now() WHERE id = $1::uuid", entity_id, version_id)
    return {"entity_id": entity_id, "version_id": version_id, "content_status": "published",
            "republished": republish}


async def reject(conn, entity_id: str, version_id: str, *, actor: str, note: Optional[str] = None) -> dict:
    async with conn.transaction():
        entity = await _lock(conn, entity_id)
        n = await conn.execute("UPDATE shared.supply_content_version SET status = 'rejected', reviewed_by = $3, "
                               "reviewed_at = now(), review_note = $4 WHERE id = $1::uuid AND entity_id = $2::uuid "
                               "AND status = 'draft'", version_id, entity_id, actor, _opt(note))
        if n != "UPDATE 1":
            raise ReviewError(409, "not_a_draft", "no open draft with this id for this entity")
        open_drafts = await conn.fetchval("SELECT count(*) FROM shared.supply_content_version "
                                          "WHERE entity_id = $1::uuid AND status = 'draft'", entity_id)
        status = entity["content_status"]
        if status == "in_review" and not open_drafts:
            status = "draft"
            await conn.execute("UPDATE shared.supply_entity SET content_status = 'draft', updated_at = now() "
                               "WHERE id = $1::uuid", entity_id)
    return {"entity_id": entity_id, "version_id": version_id, "status": "rejected", "content_status": status}


async def unpublish(conn, entity_id: str, *, actor: str) -> dict:
    async with conn.transaction():
        entity = await _lock(conn, entity_id)
        if entity["content_status"] not in ("published", "stale"):
            raise ReviewError(409, "not_published", f"content is {entity['content_status']}")
        await conn.execute("UPDATE shared.supply_entity SET content_status = 'unpublished', updated_at = now() "
                           "WHERE id = $1::uuid", entity_id)
    return {"entity_id": entity_id, "content_status": "unpublished", "by": actor}


async def request_draft(conn, entity_id: str, *, actor: str) -> dict:
    """Queue a new LLM draft for the current facts (a regenerate). A paid call: the job's cap and budget apply."""
    from shared.jobs import queue

    entity = await conn.fetchrow(ENTITY_SQL, entity_id)
    if entity is None:
        raise ReviewError(404, "not_found", "no supply entity with this id")
    if not entity["active"]:
        raise ReviewError(409, "inactive", "the entity is inactive in Booking")
    version = int(entity["applied_supply_version"])
    key = f"{draft_key(entity['source_id'], version)}:manual:{uuid.uuid4().hex[:12]}"
    job_id, _ = await queue.enqueue(conn, DRAFT_KIND, {"source_id": entity["source_id"], "supply_version": version,
                                                       "trigger": "manual"},
                                    idempotency_key=key, max_attempts=DRAFT_MAX_ATTEMPTS, created_by=actor)
    return {"entity_id": entity_id, "job_id": job_id}
