"""The served reads of AA-authored content (catalog.http.v1 ``/aa-content``, AA-977) and the served body.

Every read goes to ``gold_aa_internal.v_aa_content_published`` (CIS 275), which holds published versions only, so a
draft is never served (rule R) whatever the code asks. A published version never changes (CIS 275 trigger), so a
version the CRM pinned keeps answering with the same body and hash (rule E), also after a newer version is published
and after its item is retired; only the list hides a retired item.

The reads run on the CIS main pool (``app.state.pool``, role cis_app, which CIS 275 grants SELECT on the view), not on
the tenant pool AA-976 uses for the ``catalog`` views: these objects are in gold_aa_internal, where ``aa_app_user``
has no grant (AA-859 invariant), and the tenant pool's connection budget stays untouched.

``content_hash`` is ``sha256:<hex>`` over the canonical JSON of the served AaContent body without the hash
(``services.catalog_http.mapping.content_hash``, the AA-976 rule), computed once at publish and stored; the ETag of a
version is that hash.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import asyncpg

from services.catalog_http.mapping import content_hash

DEFAULT_PAGE = 20
MAX_PAGE = 50
ACQUIRE_TIMEOUT_S = 5.0

LIST_SQL = """
    SELECT DISTINCT ON (content_id)
           content_id, kind, locale, group_key, version, title, published_at, content_hash
      FROM gold_aa_internal.v_aa_content_published
     WHERE retired_at IS NULL
       AND ($1::uuid IS NULL OR content_id > $1::uuid)
       AND ($2::text IS NULL OR kind = $2::text)
       AND ($3::text IS NULL OR locale = $3::text)
     ORDER BY content_id, version DESC
     LIMIT $4
"""
VERSION_SQL = """
    SELECT content_id, kind, locale, group_key, version, title, body_markdown, applies_to, published_at, content_hash
      FROM gold_aa_internal.v_aa_content_published
     WHERE content_id = $1::uuid AND version = $2::int
"""

_POOL_ERRORS = (asyncpg.PostgresConnectionError, asyncpg.TooManyConnectionsError, asyncpg.InterfaceError,
                OSError, asyncio.TimeoutError)


class ContentUnavailable(Exception):  # noqa: N818 - reads as an outcome
    """No database connection right now (pool missing, at its limit, the database unreachable, or the CIS 275
    objects missing): 503."""


class InvalidRequest(ValueError):
    """A query parameter or cursor the contract does not accept: 400 ``invalid``."""


@asynccontextmanager
async def content_connection(pool: Any) -> AsyncIterator[Any]:
    """One READ ONLY transaction on a main-pool connection; :class:`ContentUnavailable` on a pool or connection
    error."""
    if pool is None:
        raise ContentUnavailable("pool not configured")
    try:
        conn = await pool.acquire(timeout=ACQUIRE_TIMEOUT_S)
    except _POOL_ERRORS as exc:
        raise ContentUnavailable(type(exc).__name__) from exc
    try:
        async with conn.transaction(readonly=True):
            yield conn
    except (*_POOL_ERRORS, asyncpg.QueryCanceledError, asyncpg.UndefinedTableError) as exc:
        # UndefinedTableError: CIS 275 is not applied on this database yet (code deployed before the migration);
        # answer 503 like any other storage outage instead of a bare 500.
        raise ContentUnavailable(type(exc).__name__) from exc
    finally:
        await pool.release(conn)


# ── served body ───────────────────────────────────────────────────────────────

def iso_utc(value: datetime) -> str:
    """RFC 3339 in UTC with a Z, microseconds kept (the stored value, so the hash is reproducible)."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def served_body(row: Any) -> dict:
    """The contract AaContent of one version row (view row, or the store's row at publish), without content_hash."""
    return {
        "content_id": str(row["content_id"]),
        "kind": row["kind"],
        "version": int(row["version"]),
        "locale": row["locale"],
        "group_key": row["group_key"],
        "title": row["title"],
        "body_markdown": row["body_markdown"],
        "applies_to": _json(row["applies_to"]) or {},
        "published_at": iso_utc(row["published_at"]),
    }


def hash_of(body: dict) -> str:
    return content_hash(body)


def summary(row: Any) -> dict:
    return {
        "content_id": str(row["content_id"]),
        "kind": row["kind"],
        "version": int(row["version"]),
        "locale": row["locale"],
        "group_key": row["group_key"],
        "title": row["title"],
        "published_at": iso_utc(row["published_at"]),
        "content_hash": row["content_hash"],
    }


# ── cursor ────────────────────────────────────────────────────────────────────

def _fingerprint(kind: Optional[str], locale: Optional[str], limit: int) -> str:
    return hashlib.sha256(f"aa-content|{kind or ''}|{locale or ''}|{limit}".encode()).hexdigest()[:8]


def encode_cursor(after: str, kind: Optional[str], locale: Optional[str], limit: int) -> str:
    """Opaque keyset cursor: the last content_id of the page, bound to the filters and limit it was made for
    (``v1.<b64>.<fingerprint>``, the AA-976 format, 60 characters)."""
    token = base64.urlsafe_b64encode(after.encode()).decode().rstrip("=")
    return f"v1.{token}.{_fingerprint(kind, locale, limit)}"


def decode_cursor(cursor: str, kind: Optional[str], locale: Optional[str], limit: int) -> uuid.UUID:
    parts = cursor.split(".")
    if len(cursor) > 200 or len(parts) != 3 or parts[0] != "v1":
        raise InvalidRequest("cursor is not one this API issued")
    if parts[2] != _fingerprint(kind, locale, limit):
        raise InvalidRequest("cursor was issued for other filters; start again without it")
    try:
        return uuid.UUID(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode())
    except ValueError as exc:  # binascii.Error, UnicodeDecodeError, a bad UUID
        raise InvalidRequest("cursor is not one this API issued") from exc


# ── reads ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContentPage:
    items: list[dict]
    next_after: Optional[str]


async def list_published(conn: Any, *, after: Optional[uuid.UUID], kind: Optional[str], locale: Optional[str],
                         limit: int) -> ContentPage:
    """The latest published version of each item that is not retired, ordered by content_id."""
    rows = await conn.fetch(LIST_SQL, after, kind, locale, limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    return ContentPage(items=[summary(r) for r in rows],
                       next_after=str(rows[-1]["content_id"]) if more and rows else None)


async def get_published_version(conn: Any, content_id: uuid.UUID, version: int) -> Optional[dict]:
    """The AaContent of one published version (also of a retired item), or None (draft, unknown, never published)."""
    row = await conn.fetchrow(VERSION_SQL, content_id, version)
    if row is None:
        return None
    body = served_body(row)
    body["content_hash"] = row["content_hash"]
    return body


__all__ = [
    "ContentPage", "ContentUnavailable", "DEFAULT_PAGE", "InvalidRequest", "MAX_PAGE", "content_connection",
    "decode_cursor", "encode_cursor", "get_published_version", "hash_of", "iso_utc", "list_published",
    "served_body", "summary",
]
