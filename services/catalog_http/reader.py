"""Reads of catalog.http.v1 over the catalog contract views (AA-976).

Every read goes to the views of schema ``catalog`` (CIS 225 / 228 / 274, contract catalog.tour.v1.3), never to the
pipeline tables, so CIS serves the CRM exactly what Booking and TripPlanner read: the same keys and the same liveness
rule (``catalog._live_tour``, root ADR 0015 decision 1). ``catalog.v_tour`` holds served tours only; the served state
of a key that left it is in ``catalog.v_tour_retired`` (``retired`` -> 410, ``paused`` -> 404, no row -> 404).

The views are read as ``aa_app_user`` on the CIS tenant pool (``app.state.tenant_pool``): that role already holds
``USAGE`` on ``catalog`` and ``SELECT`` on every view (CIS 225, 228, 274), while the CIS runtime ``cis_app`` has
neither (ADR 0015 decision 3), so no new grant is needed. The contract views do not depend on ``app.tenant_id``
(they run with ``catalog_owner``'s rights); each read still runs in a READ ONLY transaction with the GUC set to ''
so a pooled connection never carries a tenant into it. See :func:`catalog_connection`.

All statements filter ``source_id`` with ``=`` for one tour and ``= ANY($1::text[])`` for a page (lesson S238:
``v_tour_place`` handles both since CIS 235's per-tour LATERAL).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Optional

import asyncpg

from services.catalog_http.geo import spellings
from services.catalog_http.mapping import SOURCE_SYSTEM, tour_body

# At most this many catalog reads hold a tenant-pool connection at once per api task: the pool has 2 connections
# per task (aa544_tenant_pool.TENANT_POOL_MAX_SIZE, role CONNECTION LIMIT 15), shared with every tenant route, so a
# CRM sync can never take both.
MAX_CONCURRENT_READS = 1
ACQUIRE_TIMEOUT_S = 5.0

DEFAULT_PAGE = 20
MAX_PAGE = 50

_TOUR_COLS = ("source_id, title, summary, country, duration_days, activities, inclusions, exclusions, updated_at")

LIST_TOURS_SQL = f"""
    SELECT {_TOUR_COLS}
      FROM catalog.v_tour
     WHERE source_system = 'cis'
       AND ($1::text IS NULL OR source_id > $1::text)
       AND ($2::timestamptz IS NULL OR updated_at >= $2::timestamptz)
       AND ($3::text[] IS NULL OR lower(country) = ANY($3::text[]))
     ORDER BY source_id
     LIMIT $4
"""
GET_TOUR_SQL = f"SELECT {_TOUR_COLS} FROM catalog.v_tour WHERE source_system = 'cis' AND source_id = $1::text"
RETIRED_SQL = """
    SELECT state, reason, changed_at
      FROM catalog.v_tour_retired
     WHERE source_system = 'cis' AND source_id = $1::text
"""
_DETAIL_SQL = {
    "days": "SELECT source_id, day_number, title, body FROM catalog.v_tour_day WHERE {cond}",
    "places": ("SELECT source_id, day_number, position, place_name, place_display_name, destination_id, "
               "destination_name, country, lat, lng, precision FROM catalog.v_tour_place WHERE {cond}"),
    "photos": "SELECT source_id, photo_id, path, place_label, position FROM catalog.v_tour_photo WHERE {cond}",
    "components": ("SELECT DISTINCT source_id, activity, intensity_level FROM catalog.v_tour_component "
                   "WHERE {cond}"),
}
_ONE = "source_system = 'cis' AND source_id = $1::text"
_MANY = "source_system = 'cis' AND source_id = ANY($1::text[])"
DESTINATION_SQL = """
    SELECT destination_id, name, country, lat, lng
      FROM catalog.v_destination
     WHERE destination_id = $1::uuid
"""

_POOL_ERRORS = (asyncpg.PostgresConnectionError, asyncpg.TooManyConnectionsError, asyncpg.InterfaceError,
                OSError, asyncio.TimeoutError)

_gate: Optional[asyncio.Semaphore] = None


class CatalogUnavailable(Exception):  # noqa: N818 - reads as an outcome
    """No catalog connection right now (tenant pool missing, at its limit, or the database unreachable): 503."""


class InvalidRequest(ValueError):
    """A query parameter or cursor the contract does not accept: 400 ``invalid``."""


def _semaphore() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(MAX_CONCURRENT_READS)
    return _gate


def reset_gate() -> None:
    """Drop the per-process gate (tests run each case on a new event loop)."""
    global _gate
    _gate = None


@asynccontextmanager
async def catalog_connection(pool: Any) -> AsyncIterator[Any]:
    """One READ ONLY transaction on a tenant-pool connection with ``app.tenant_id`` = ''.

    Raises :class:`CatalogUnavailable` when there is no pool, the gate or the pool does not hand out a connection
    within :data:`ACQUIRE_TIMEOUT_S`, the connection drops, or a statement hits the pool's statement_timeout."""
    if pool is None:
        raise CatalogUnavailable("tenant pool not configured")
    gate = _semaphore()
    try:
        await asyncio.wait_for(gate.acquire(), timeout=ACQUIRE_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise CatalogUnavailable("catalog reads busy") from exc
    try:
        try:
            conn = await pool.acquire(timeout=ACQUIRE_TIMEOUT_S)
        except _POOL_ERRORS as exc:
            raise CatalogUnavailable(type(exc).__name__) from exc
        try:
            async with conn.transaction(readonly=True):
                await conn.execute("SELECT set_config('app.tenant_id', '', true)")
                yield conn
        except (*_POOL_ERRORS, asyncpg.QueryCanceledError) as exc:
            # a dropped connection or the pool's 15 s statement_timeout: temporary, the caller retries
            raise CatalogUnavailable(type(exc).__name__) from exc
        finally:
            await pool.release(conn)
    finally:
        gate.release()


# ── cursor ────────────────────────────────────────────────────────────────────

def _fingerprint(updated_since: Optional[datetime], country: Optional[str]) -> str:
    text = f"{updated_since.isoformat() if updated_since else ''}|{country or ''}"
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def encode_cursor(after: str, updated_since: Optional[datetime], country: Optional[str]) -> str:
    """Opaque keyset cursor: the last source_id of the page, bound to the filters it was made for (<= 184 chars
    for a 128-character source_id, contract maxLength 200)."""
    token = base64.urlsafe_b64encode(after.encode()).decode().rstrip("=")
    return f"v1.{token}.{_fingerprint(updated_since, country)}"


def decode_cursor(cursor: str, updated_since: Optional[datetime], country: Optional[str]) -> str:
    parts = cursor.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise InvalidRequest("cursor is not one this API issued")
    if parts[2] != _fingerprint(updated_since, country):
        raise InvalidRequest("cursor was issued for other filters; start again without it")
    try:
        after = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidRequest("cursor is not one this API issued") from exc
    if not after or len(after) > 128:
        raise InvalidRequest("cursor is not one this API issued")
    return after


def country_names(code: str) -> list[str]:
    """Lower-case raw_tours.country spellings of one ISO code ('CN' -> ['china', 'tibet'])."""
    return spellings(code)


# ── reads ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TourPage:
    items: list[tuple[dict, Any]]          # (Tour body, v_tour.updated_at)
    next_after: Optional[str]


def _group(rows: list[Any]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        record = dict(row)
        out.setdefault(str(record["source_id"]), []).append(record)
    return out


async def _details(conn: Any, source_ids: list[str]) -> dict[str, dict[str, list[dict]]]:
    one = len(source_ids) == 1
    arg: Any = source_ids[0] if one else source_ids
    details: dict[str, dict[str, list[dict]]] = {sid: {} for sid in source_ids}
    for name, sql in _DETAIL_SQL.items():
        rows = await conn.fetch(sql.format(cond=_ONE if one else _MANY), arg)
        for sid, group in _group(rows).items():
            if sid in details:
                details[sid][name] = group
    return details


def _assemble(tour_row: Any, detail: dict[str, list[dict]], photo_base: str) -> Optional[dict]:
    return tour_body(dict(tour_row), days=detail.get("days", ()), places=detail.get("places", ()),
                     photos=detail.get("photos", ()), components=detail.get("components", ()),
                     photo_base=photo_base)


async def list_tours(conn: Any, *, after: Optional[str], updated_since: Optional[datetime],
                     country: Optional[str], limit: int, photo_base: str) -> TourPage:
    names = country_names(country) if country else None
    if names is not None and not names:
        return TourPage(items=[], next_after=None)
    rows = await conn.fetch(LIST_TOURS_SQL, after, updated_since, names, limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    details = await _details(conn, [str(r["source_id"]) for r in rows]) if rows else {}
    items = []
    for row in rows:
        body = _assemble(row, details.get(str(row["source_id"]), {}), photo_base)
        if body is not None:
            items.append((body, row["updated_at"]))
    return TourPage(items=items, next_after=str(rows[-1]["source_id"]) if more and rows else None)


@dataclass(frozen=True)
class TourLookup:
    body: Optional[dict]          # the Tour when served
    state: Optional[str]          # v_tour_retired.state when not served (retired | paused | restored)
    reason: Optional[str]
    changed_at: Any = None


async def get_tour(conn: Any, source_id: str, *, photo_base: str) -> TourLookup:
    row = await conn.fetchrow(GET_TOUR_SQL, source_id)
    if row is not None:
        detail = (await _details(conn, [source_id])).get(source_id, {})
        body = _assemble(row, detail, photo_base)
        if body is not None:
            return TourLookup(body=body, state=None, reason=None)
    tomb = await conn.fetchrow(RETIRED_SQL, source_id)
    if tomb is None:
        return TourLookup(body=None, state=None, reason=None)
    return TourLookup(body=None, state=tomb["state"], reason=tomb["reason"], changed_at=tomb["changed_at"])


async def get_destination(conn: Any, destination_id: str) -> Optional[dict]:
    row = await conn.fetchrow(DESTINATION_SQL, destination_id)
    return dict(row) if row is not None else None


__all__ = [
    "CatalogUnavailable", "InvalidRequest", "TourLookup", "TourPage", "SOURCE_SYSTEM", "DEFAULT_PAGE", "MAX_PAGE",
    "catalog_connection", "country_names", "decode_cursor", "encode_cursor", "get_destination", "get_tour",
    "list_tours", "reset_gate",
]
