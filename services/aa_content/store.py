"""Admin writes and reads of AA-authored content (AA-977) on gold_aa_internal.aa_content_item / aa_content_version.

Model (CIS 275). One item = one piece of content in one locale; the EN and VI copies share ``group_key`` (same kind).
An item has numbered versions: at most one draft (always the newest), and published versions that never change and
are never deleted (the CIS 275 trigger enforces it; this module never tries). Life of an item:

  create_item     item + draft v1
  save_draft      edits the draft, or starts draft n+1 (with the fields sent) after version n was published
  discard_draft   deletes the draft (a published version stays); refused when the draft is the only version
  publish         draft -> published: content_hash over the served AaContent body (reader.served_body), published_at
                  / published_by, in one transaction; the prep-pack visa rule (validate.publishable) runs first
  retire/restore  hides / shows the item in the served list; its published versions stay fetchable by version

Concurrency: every write takes the item row FOR UPDATE and compares the caller's ``expected_updated_at`` with the
item's ``updated_at`` (the value the editor last read); a mismatch is :class:`ContentConflict` ``stale`` (409, "someone
else saved"). Every write moves ``updated_at`` (clock_timestamp()).

Writers are labels (``admin:<shared.admin_users id>``, ``seed:AA-977``), at most 64 characters (CIS 275 CHECK).
The caller passes a connection of the main pool (role cis_app); each write runs in its own transaction.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from services.aa_content import reader, validate
from services.aa_content.validate import ContentInvalid

ITEM = "gold_aa_internal.aa_content_item"
VERSION = "gold_aa_internal.aa_content_version"
ADMIN_PAGE_MAX = 100
STATUSES = ("draft", "published", "retired")

_ITEM_COLS = "content_id, kind, locale, group_key, retired_at, retired_by, created_at, created_by, updated_at"
_VERSION_COLS = ("content_id, version, status, title, body_markdown, applies_to, content_hash, published_at, "
                 "published_by, created_at, created_by, updated_at")

ITEM_SQL = f"SELECT {_ITEM_COLS} FROM {ITEM} WHERE content_id = $1::uuid"
LOCK_ITEM_SQL = ITEM_SQL + " FOR UPDATE"
GROUP_SQL = f"SELECT {_ITEM_COLS} FROM {ITEM} WHERE group_key = $1 ORDER BY locale"
# Serializes creates in one group, so two concurrent creates of the EN and VI copies cannot pass the kind check with
# different kinds (SQL does not hold that rule; the lock is released at COMMIT).
GROUP_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('aa_content_group:' || $1::text))"
VERSIONS_SQL = f"SELECT {_VERSION_COLS} FROM {VERSION} WHERE content_id = $1::uuid ORDER BY version DESC"
ONE_VERSION_SQL = f"SELECT {_VERSION_COLS} FROM {VERSION} WHERE content_id = $1::uuid AND version = $2::int"
DRAFT_SQL = f"SELECT {_VERSION_COLS} FROM {VERSION} WHERE content_id = $1::uuid AND status = 'draft'"
NEWEST_SQL = f"SELECT COALESCE(max(version), 0) FROM {VERSION} WHERE content_id = $1::uuid"
PUBLISHED_COUNT_SQL = f"SELECT count(*) FROM {VERSION} WHERE content_id = $1::uuid AND status = 'published'"
TOUCH_SQL = f"UPDATE {ITEM} SET updated_at = clock_timestamp() WHERE content_id = $1::uuid"
INSERT_ITEM_SQL = (f"INSERT INTO {ITEM} (kind, locale, group_key, created_by) VALUES ($1, $2, $3, $4) "
                   "RETURNING content_id")
INSERT_DRAFT_SQL = (f"INSERT INTO {VERSION} (content_id, version, title, body_markdown, applies_to, created_by) "
                    "VALUES ($1::uuid, $2::int, $3, $4, $5::jsonb, $6)")
UPDATE_DRAFT_SQL = (f"UPDATE {VERSION} SET title = $2, body_markdown = $3, applies_to = $4::jsonb, "
                    "updated_at = clock_timestamp() WHERE content_id = $1::uuid AND status = 'draft'")
DISCARD_SQL = f"DELETE FROM {VERSION} WHERE content_id = $1::uuid AND status = 'draft'"
PUBLISH_SQL = (f"UPDATE {VERSION} SET status = 'published', content_hash = $3, published_at = $4, "
               "published_by = $5, updated_at = clock_timestamp() "
               "WHERE content_id = $1::uuid AND version = $2::int AND status = 'draft'")
RETIRE_SQL = (f"UPDATE {ITEM} SET retired_at = clock_timestamp(), retired_by = $2, updated_at = clock_timestamp() "
              "WHERE content_id = $1::uuid")
RESTORE_SQL = (f"UPDATE {ITEM} SET retired_at = NULL, retired_by = NULL, updated_at = clock_timestamp() "
               "WHERE content_id = $1::uuid")

# Admin list: one row per item with its state, newest title and the filters' inputs. state: retired | published
# (some version published, not retired) | draft (never published); has_draft: a draft is waiting.
_LIST_FROM = f"""
      FROM {ITEM} i
      LEFT JOIN LATERAL (
            SELECT max(v.version) FILTER (WHERE v.status = 'published') AS published_version,
                   max(v.version) FILTER (WHERE v.status = 'draft') AS draft_version,
                   max(v.version) AS newest_version
              FROM {VERSION} v WHERE v.content_id = i.content_id
           ) s ON true
      LEFT JOIN {VERSION} nv ON nv.content_id = i.content_id AND nv.version = s.newest_version
     WHERE ($1::text IS NULL OR i.kind = $1::text)
       AND ($2::text IS NULL OR i.locale = $2::text)
       AND ($3::text IS NULL
            OR ($3::text = 'draft' AND s.draft_version IS NOT NULL)
            OR ($3::text = 'published' AND i.retired_at IS NULL AND s.published_version IS NOT NULL)
            OR ($3::text = 'retired' AND i.retired_at IS NOT NULL))
       AND ($4::text IS NULL OR COALESCE(nv.applies_to -> 'country_codes', '[]'::jsonb) ? $4::text)
"""
LIST_SQL = f"""
    SELECT i.content_id, i.kind, i.locale, i.group_key, i.retired_at, i.updated_at,
           s.published_version, s.draft_version, nv.title, nv.applies_to
    {_LIST_FROM}
     ORDER BY i.group_key, i.locale
     LIMIT $5 OFFSET $6
"""
COUNT_SQL = f"SELECT count(*) {_LIST_FROM}"


class ContentNotFound(LookupError):
    """No item / version / draft with this id: 404. ``what`` names which ("item", "version" or "draft"); the API maps
    it to a fixed message and never sends the exception text."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


class ContentConflict(Exception):  # noqa: N818 - reads as an outcome
    """The item is not in a state that allows the write (stale, retired, no draft, duplicate, only version): 409
    ``{code}``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code, detail)
        self.code, self.detail = code, detail

    def __str__(self) -> str:
        return self.detail


# ── shapes ────────────────────────────────────────────────────────────────────

def _iso(value: Optional[datetime]) -> Optional[str]:
    return reader.iso_utc(value) if value is not None else None


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _version(row: Any) -> dict:
    return {
        "version": int(row["version"]), "status": row["status"], "title": row["title"],
        "body_markdown": row["body_markdown"], "applies_to": _json(row["applies_to"]) or {},
        "content_hash": row["content_hash"], "published_at": _iso(row["published_at"]),
        "published_by": row["published_by"], "created_at": _iso(row["created_at"]),
        "created_by": row["created_by"], "updated_at": _iso(row["updated_at"]),
    }


def _state(retired_at: Any, published_version: Optional[int]) -> str:
    if retired_at is not None:
        return "retired"
    return "published" if published_version is not None else "draft"


def _item(row: Any, versions: list[dict]) -> dict:
    published = [v["version"] for v in versions if v["status"] == "published"]
    draft = next((v["version"] for v in versions if v["status"] == "draft"), None)
    return {
        "content_id": str(row["content_id"]), "kind": row["kind"], "locale": row["locale"],
        "group_key": row["group_key"],
        "state": _state(row["retired_at"], max(published) if published else None),
        "published_version": max(published) if published else None, "draft_version": draft,
        "retired_at": _iso(row["retired_at"]), "retired_by": row["retired_by"],
        "created_at": _iso(row["created_at"]), "created_by": row["created_by"],
        "updated_at": _iso(row["updated_at"]),
        "versions": versions,
    }


def _list_row(row: Any) -> dict:
    applies = _json(row["applies_to"]) or {}
    return {
        "content_id": str(row["content_id"]), "kind": row["kind"], "locale": row["locale"],
        "group_key": row["group_key"], "title": row["title"],
        "state": _state(row["retired_at"], row["published_version"]),
        "published_version": row["published_version"], "draft_version": row["draft_version"],
        "country_codes": applies.get("country_codes", []), "updated_at": _iso(row["updated_at"]),
    }


# ── reads ─────────────────────────────────────────────────────────────────────

async def get_item(conn: Any, content_id: uuid.UUID) -> dict:
    row = await conn.fetchrow(ITEM_SQL, content_id)
    if row is None:
        raise ContentNotFound("item")
    return _item(row, [_version(v) for v in await conn.fetch(VERSIONS_SQL, content_id)])


async def get_group(conn: Any, group_key: str) -> list[dict]:
    """Every locale of one group (EN and VI side by side), oldest locale first; [] when the group is unknown."""
    rows = await conn.fetch(GROUP_SQL, group_key)
    return [_item(r, [_version(v) for v in await conn.fetch(VERSIONS_SQL, r["content_id"])]) for r in rows]


async def get_version(conn: Any, content_id: uuid.UUID, version: int) -> dict:
    """Any version (draft or published), for the editor's preview."""
    row = await conn.fetchrow(ONE_VERSION_SQL, content_id, version)
    if row is None:
        raise ContentNotFound("version")
    return {"content_id": str(content_id), **_version(row)}


async def list_items(conn: Any, *, kind: Optional[str], locale: Optional[str], status: Optional[str],
                     country: Optional[str], limit: int, offset: int) -> dict:
    """One page of items for the admin list (server-side filters and paging), ordered by group_key, locale."""
    args = (kind, locale, status, country)
    total = await conn.fetchval(COUNT_SQL, *args)
    rows = await conn.fetch(LIST_SQL, *args, limit, offset)
    return {"items": [_list_row(r) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset}


# ── writes ────────────────────────────────────────────────────────────────────

async def _locked(conn: Any, content_id: uuid.UUID, expected_updated_at: datetime) -> Any:
    row = await conn.fetchrow(LOCK_ITEM_SQL, content_id)
    if row is None:
        raise ContentNotFound("item")
    if row["updated_at"] != expected_updated_at:
        raise ContentConflict("stale", "Someone else saved this item since you opened it; reload and try again")
    return row


def _not_retired(row: Any) -> None:
    if row["retired_at"] is not None:
        raise ContentConflict("retired", "This item is retired; restore it first")


async def create_item(conn: Any, *, kind: str, locale: str, group_key: str, title: str,
                      body_markdown: Optional[str], applies_to: Any, actor: str) -> dict:
    """A new item with draft v1. 409 ``duplicate`` when the group already has this locale; 422
    ``group_kind_mismatch`` when the group's other locale is another kind."""
    kind, locale = validate.kind(kind), validate.locale(locale)
    group_key, title = validate.group_key(group_key), validate.title(title)
    body = validate.body(body_markdown)
    applies = validate.applies_to(applies_to)
    async with conn.transaction():
        await conn.execute(GROUP_LOCK_SQL, group_key)
        for other in await conn.fetch(GROUP_SQL, group_key):
            if other["locale"] == locale:
                raise ContentConflict("duplicate", f"The group {group_key} already has a {locale} item")
            if other["kind"] != kind:
                raise ContentInvalid("group_kind_mismatch", "kind",
                                     f"The group {group_key} holds {other['kind']} items")
        try:
            content_id = await conn.fetchval(INSERT_ITEM_SQL, kind, locale, group_key, actor)
        except asyncpg.UniqueViolationError as exc:
            raise ContentConflict("duplicate", f"The group {group_key} already has a {locale} item") from exc
        await conn.execute(INSERT_DRAFT_SQL, content_id, 1, title, body, json.dumps(applies), actor)
    return await get_item(conn, content_id)


async def save_draft(conn: Any, content_id: uuid.UUID, *, title: str, body_markdown: Optional[str],
                     applies_to: Any, expected_updated_at: datetime, actor: str) -> dict:
    """Edit the draft, or start the next version's draft when the newest version is published."""
    title = validate.title(title)
    body = validate.body(body_markdown)
    applies = json.dumps(validate.applies_to(applies_to))
    async with conn.transaction():
        _not_retired(await _locked(conn, content_id, expected_updated_at))
        if await conn.fetchrow(DRAFT_SQL, content_id) is not None:
            await conn.execute(UPDATE_DRAFT_SQL, content_id, title, body, applies)
        else:
            newest = await conn.fetchval(NEWEST_SQL, content_id)
            await conn.execute(INSERT_DRAFT_SQL, content_id, int(newest) + 1, title, body, applies, actor)
        await conn.execute(TOUCH_SQL, content_id)
    return await get_item(conn, content_id)


async def discard_draft(conn: Any, content_id: uuid.UUID, *, expected_updated_at: datetime) -> dict:
    """Delete the draft. 409 ``only_version`` when nothing was ever published: the draft is the item's only version,
    and an item cannot be deleted (cis_app has no DELETE on it), so discarding would leave an empty item holding its
    (group_key, locale) slot; the editor saves over the draft or retires the item instead."""
    async with conn.transaction():
        await _locked(conn, content_id, expected_updated_at)
        if await conn.fetchrow(DRAFT_SQL, content_id) is None:
            raise ContentNotFound("draft")
        if not await conn.fetchval(PUBLISHED_COUNT_SQL, content_id):
            raise ContentConflict("only_version",
                                  "This draft is the item's only version; edit it, or retire the item instead")
        if await conn.execute(DISCARD_SQL, content_id) == "DELETE 0":
            raise ContentNotFound("draft")
        await conn.execute(TOUCH_SQL, content_id)
    return await get_item(conn, content_id)


async def publish(conn: Any, content_id: uuid.UUID, *, expected_updated_at: datetime, actor: str,
                  now: Optional[datetime] = None) -> dict:
    """Publish the draft: 409 ``no_draft`` / ``retired`` / ``stale``, 422 from :func:`validate.publishable`.
    Returns the item and the published version's served body."""
    async with conn.transaction():
        item = await _locked(conn, content_id, expected_updated_at)
        _not_retired(item)
        draft = await conn.fetchrow(DRAFT_SQL, content_id)
        if draft is None:
            raise ContentConflict("no_draft", "Nothing to publish: save a draft first")
        validate.publishable(item["kind"], draft["title"], draft["body_markdown"])
        published_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        body = reader.served_body({
            "content_id": content_id, "kind": item["kind"], "version": draft["version"], "locale": item["locale"],
            "group_key": item["group_key"], "title": draft["title"], "body_markdown": draft["body_markdown"],
            "applies_to": draft["applies_to"], "published_at": published_at,
        })
        body["content_hash"] = reader.hash_of(body)
        await conn.execute(PUBLISH_SQL, content_id, draft["version"], body["content_hash"], published_at, actor)
        await conn.execute(TOUCH_SQL, content_id)
    return {"item": await get_item(conn, content_id), "published": body}


async def retire(conn: Any, content_id: uuid.UUID, *, expected_updated_at: datetime, actor: str) -> dict:
    async with conn.transaction():
        row = await _locked(conn, content_id, expected_updated_at)
        if row["retired_at"] is not None:
            raise ContentConflict("already_retired", "This item is already retired")
        await conn.execute(RETIRE_SQL, content_id, actor)
    return await get_item(conn, content_id)


async def restore(conn: Any, content_id: uuid.UUID, *, expected_updated_at: datetime) -> dict:
    async with conn.transaction():
        row = await _locked(conn, content_id, expected_updated_at)
        if row["retired_at"] is None:
            raise ContentConflict("not_retired", "This item is not retired")
        await conn.execute(RESTORE_SQL, content_id)
    return await get_item(conn, content_id)


__all__ = [
    "ADMIN_PAGE_MAX", "STATUSES", "ContentConflict", "ContentInvalid", "ContentNotFound", "create_item",
    "discard_draft", "get_group", "get_item", "get_version", "list_items", "publish", "restore", "retire",
    "save_draft",
]
