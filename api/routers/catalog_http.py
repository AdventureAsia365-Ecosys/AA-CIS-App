"""catalog.http.v1 — CIS published tour content over HTTP for the partner CRM (AA-976, design card CRM-B4).

Contract: root ``docs/contracts/catalog.http.v1.openapi.yaml`` (copy in ``docs/contracts/`` of this repo, checked by
``tests/unit/test_aa976_catalog_http_contract.py``). Root ADR 0010 decision 4: the CRM holds no grant on any CIS
schema (ADR 0008a decision 1), so it reads published content here.

  GET /catalog/v1/tours                                  served tours, keyset cursor, updated_since, country_code
  GET /catalog/v1/tours/{source_system}/{source_id}      one tour: 200 served, 410 retired, 404 paused / never served
  GET /catalog/v1/destinations/{destination_id}          one destination with its IANA time zone
  GET /catalog/v1/aa-content                             AA-authored content: latest published version of each item
                                                         that is not retired (kind, locale, cursor, limit)
  GET /catalog/v1/aa-content/{content_id}/versions/{v}   one published version (also after a newer publish or retire)

``updated_since`` filters ``catalog.v_tour.updated_at`` only (the published Master row). Place, time-zone, photo and
component changes move ``content_hash`` without moving it, so the contract tells an incremental CRM sync to also run
a periodic full pass and compare ``content_hash``.

AA-authored content (AA-977, CRM-B5, contract 1.2): prep packs, starter templates and campaign blocks that AA staff
write and publish in CIS (``/admin/aa-content``). Read from ``gold_aa_internal.v_aa_content_published`` on the main
pool (cis_app), which holds published versions only: a draft, an unknown version or an item never published is a 404
``not_found`` without reason (rule R). A published version never changes (CIS 275 trigger), so the CRM keeps fetching
the version it uses after a newer publish and after the item is retired (rule E); only the list hides a retired item.
The version's ETag is its stored ``content_hash``. See ``services/aa_content/reader.py``.

Auth: an M2M client-credentials token of the ``aa-m2m-<env>`` pool, client = AA-Partner (CRM), scope
``cis.catalog/read`` (root ADR 0006b decision 7; ``api/core/m2m_auth.py``). 401 no / bad token, 403 another client or
no scope, 503 key set or configuration unavailable. Nothing else reaches these routes: ``/catalog`` is not a staff
path (``StaffBearerMiddleware``) and not ``/v1`` (the tenant rate limiter).

Liveness (root ADR 0015 decision 5, AA-Partner row): a served key is in ``catalog.v_tour``; a key whose tombstone in
``catalog.v_tour_retired`` says ``retired`` answers **410 gone** (the CRM marks its proposal lines "no longer
offered"); ``paused`` (Master deactivated, or a supersede waiting for its Master) answers 404 with the reason, so the
CRM hides the tour without marking lines; a key never served is a plain 404.

Caching: every 200 carries a strong ``ETag`` (the body's ``content_hash`` for a tour, a hash of the body otherwise),
``Cache-Control: private, max-age=60`` and ``Vary: Authorization``; ``If-None-Match`` with a matching tag answers 304
with no body. Errors use the contract ``Problem`` body ``{error, message[, reason]}``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from api.core import m2m_auth
from api.core.identity_verifier import AuthUnavailable, InvalidToken
from api.schemas.public_api import (  # AA-899: doc only
    CatalogAaContent, CatalogAaContentPage, CatalogDestination, CatalogTour, CatalogTourPage,
)
from services.aa_content import reader as aa_reader
from services.aa_content import validate as aa_validate
from services.catalog_http import reader
from services.catalog_http.mapping import destination_body, tour_summary
from services.photos.sync import public_base

logger = structlog.get_logger()
router = APIRouter(prefix="/catalog/v1", tags=["catalog-http"])

CACHE_CONTROL = "private, max-age=60"
MAX_SOURCE_ID = 128


class CatalogProblem(Exception):
    """An error answered with the contract Problem body (registered in api/main.py)."""

    def __init__(self, status: int, error: str, message: str, reason: Optional[str] = None,
                 headers: Optional[dict[str, str]] = None) -> None:
        super().__init__(status, error, message, reason, headers)
        self.status, self.error, self.message, self.reason = status, error, message, reason
        self.headers = headers or {}


def problem_response(exc: CatalogProblem) -> JSONResponse:
    body: dict[str, Any] = {"error": exc.error, "message": exc.message[:500]}
    if exc.reason is not None:
        body["reason"] = exc.reason
    return JSONResponse(status_code=exc.status, content=body, headers=exc.headers)


async def catalog_problem_handler(_request: Request, exc: CatalogProblem) -> JSONResponse:
    return problem_response(exc)


# ── auth ──────────────────────────────────────────────────────────────────────

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Bearer realm="catalog.http.v1"'}


def _bearer(request: Request) -> Optional[str]:
    value = (request.headers.get("authorization") or "").strip()
    if value[:7].lower() != "bearer ":
        return None
    token = value[7:].strip()
    return token or None


async def require_catalog_reader(request: Request) -> m2m_auth.M2MCaller:
    """The verified CRM caller with scope cis.catalog/read, or a Problem (401 / 403 / 503)."""
    token = _bearer(request)
    if token is None:
        raise CatalogProblem(401, "unauthorized", "An M2M bearer token is required.", headers=_UNAUTHORIZED_HEADERS)
    try:
        verifier = m2m_auth.get_m2m_verifier()
    except ValueError as exc:
        logger.error("catalog_http_m2m_config_invalid", error=str(exc))
        raise CatalogProblem(503, "unavailable", "Token verification is not configured.") from exc
    try:
        caller = await m2m_auth.verify_caller(verifier, token)
    except m2m_auth.CallerRefused as exc:
        logger.info("catalog_http_refused", reason=str(exc))
        raise CatalogProblem(403, "forbidden", "This client may not read the catalog.") from exc
    except InvalidToken as exc:
        logger.info("catalog_http_unauthorized", reason=str(exc))
        raise CatalogProblem(401, "unauthorized", "The bearer token is not valid.",
                             headers=_UNAUTHORIZED_HEADERS) from exc
    except AuthUnavailable as exc:
        logger.error("catalog_http_jwks_unavailable")
        raise CatalogProblem(503, "unavailable", "Token verification is unavailable; retry later.",
                             headers={"Retry-After": "5"}) from exc
    if not caller.has(m2m_auth.SCOPE_CATALOG_READ):
        logger.info("catalog_http_refused", reason="missing scope", client_id=caller.client_id)
        raise CatalogProblem(403, "forbidden", f"The token lacks the scope {m2m_auth.SCOPE_CATALOG_READ}.")
    return caller


# ── helpers ───────────────────────────────────────────────────────────────────

def _etag_of(value: str) -> str:
    return f'"{value}"'


def _body_hash(body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _matches(if_none_match: Optional[str], etag: str) -> bool:
    """RFC 9110 weak comparison of If-None-Match against our strong tag (``*`` matches any current body)."""
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _cached(request: Request, body: Any, etag_value: str) -> Response:
    etag = _etag_of(etag_value)
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL, "Vary": "Authorization"}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=body, headers=headers)


def _pool(request: Request) -> Any:
    return getattr(request.app.state, "tenant_pool", None)


def _unavailable(exc: Exception) -> CatalogProblem:
    logger.error("catalog_http_db_unavailable", reason=str(exc))
    return CatalogProblem(503, "unavailable", "The catalog is unavailable; retry later.",
                          headers={"Retry-After": "5"})


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if value is None or value == "":
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise reader.InvalidRequest("updated_since must be an RFC 3339 date-time") from exc
    if parsed.tzinfo is None:
        raise reader.InvalidRequest("updated_since needs a time zone offset (e.g. 2026-10-23T00:00:00Z)")
    return parsed.astimezone(timezone.utc)


def _parse_country(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    if len(value) != 2 or not value.isascii() or not value.isalpha() or not value.isupper():
        raise reader.InvalidRequest("country_code must be two upper-case letters (ISO 3166-1 alpha-2)")
    return value


def _parse_limit(value: Optional[str]) -> int:
    if value is None or value == "":
        return reader.DEFAULT_PAGE
    if not value.isdigit() or not 1 <= int(value) <= reader.MAX_PAGE:
        raise reader.InvalidRequest(f"limit must be an integer from 1 to {reader.MAX_PAGE}")
    return int(value)


def _invalid(exc: reader.InvalidRequest) -> CatalogProblem:
    return CatalogProblem(400, "invalid", str(exc))


# ── routes ────────────────────────────────────────────────────────────────────

@router.get("/tours", responses={200: {"model": CatalogTourPage}})
async def list_tours(request: Request, updated_since: Optional[str] = None, country_code: Optional[str] = None,
                     cursor: Optional[str] = None, limit: Optional[str] = None,
                     caller: m2m_auth.M2MCaller = Depends(require_catalog_reader)) -> Response:
    try:
        since = _parse_since(updated_since)
        country = _parse_country(country_code)
        page_size = _parse_limit(limit)
        if cursor is not None and len(cursor) > 200:
            raise reader.InvalidRequest("cursor is not one this API issued")
        after = reader.decode_cursor(cursor, since, country) if cursor else None
    except reader.InvalidRequest as exc:
        raise _invalid(exc) from exc
    try:
        async with reader.catalog_connection(_pool(request)) as conn:
            page = await reader.list_tours(conn, after=after, updated_since=since, country=country,
                                           limit=page_size, photo_base=public_base())
    except reader.CatalogUnavailable as exc:
        raise _unavailable(exc) from exc
    body = {
        "items": [tour_summary(tour, updated_at) for tour, updated_at in page.items],
        "next_cursor": reader.encode_cursor(page.next_after, since, country) if page.next_after else None,
    }
    logger.info("catalog_http_served", op="listTours", client_id=caller.client_id, items=len(body["items"]),
                more=body["next_cursor"] is not None)
    return _cached(request, body, _body_hash(body))


@router.get("/tours/{source_system}/{source_id}", responses={200: {"model": CatalogTour}})
async def get_tour(source_system: str, source_id: str, request: Request,
                   caller: m2m_auth.M2MCaller = Depends(require_catalog_reader)) -> Response:
    if source_system != reader.SOURCE_SYSTEM or not source_id or len(source_id) > MAX_SOURCE_ID:
        raise CatalogProblem(404, "not_found", "No published tour has this key.")
    try:
        async with reader.catalog_connection(_pool(request)) as conn:
            found = await reader.get_tour(conn, source_id, photo_base=public_base())
    except reader.CatalogUnavailable as exc:
        raise _unavailable(exc) from exc
    logger.info("catalog_http_served", op="getTour", client_id=caller.client_id, source_id=source_id,
                state="served" if found.body else (found.state or "none"))
    if found.body is not None:
        return _cached(request, found.body, found.body["content_hash"])
    if found.state == "retired":
        raise CatalogProblem(410, "gone", "Adventure Asia no longer offers this tour.", reason=found.reason)
    if found.state == "paused":
        raise CatalogProblem(404, "not_found", "This tour is temporarily unavailable.", reason=found.reason)
    raise CatalogProblem(404, "not_found", "No published tour has this key.")


@router.get("/destinations/{destination_id}", responses={200: {"model": CatalogDestination}})
async def get_destination(destination_id: str, request: Request,
                          caller: m2m_auth.M2MCaller = Depends(require_catalog_reader)) -> Response:
    try:
        key = str(uuid.UUID(destination_id))
    except ValueError as exc:
        raise CatalogProblem(404, "not_found", "No destination has this id.") from exc
    try:
        async with reader.catalog_connection(_pool(request)) as conn:
            row = await reader.get_destination(conn, key)
    except reader.CatalogUnavailable as exc:
        raise _unavailable(exc) from exc
    logger.info("catalog_http_served", op="getDestination", client_id=caller.client_id, found=row is not None)
    if row is None:
        raise CatalogProblem(404, "not_found", "No destination has this id.")
    body = destination_body(row)
    return _cached(request, body, _body_hash(body))


# ── AA-authored content (AA-977) ──────────────────────────────────────────────

def _aa_unavailable(exc: Exception) -> CatalogProblem:
    logger.error("catalog_http_aa_content_unavailable", reason=str(exc))
    return CatalogProblem(503, "unavailable", "AA content is unavailable; retry later.", headers={"Retry-After": "5"})


def _aa_filter(value: Optional[str], allowed: tuple[str, ...], name: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if value not in allowed:
        raise reader.InvalidRequest(f"{name} must be one of {', '.join(allowed)}")
    return value


@router.get("/aa-content", responses={200: {"model": CatalogAaContentPage}})
async def list_aa_content(request: Request, kind: Optional[str] = None, locale: Optional[str] = None,
                          cursor: Optional[str] = None, limit: Optional[str] = None,
                          caller: m2m_auth.M2MCaller = Depends(require_catalog_reader)) -> Response:
    try:
        kind_value = _aa_filter(kind, aa_validate.KINDS, "kind")
        locale_value = _aa_filter(locale, aa_validate.LOCALES, "locale")
        page_size = _parse_limit(limit)
        after = aa_reader.decode_cursor(cursor, kind_value, locale_value, page_size) if cursor else None
    except (reader.InvalidRequest, aa_reader.InvalidRequest) as exc:
        raise CatalogProblem(400, "invalid", str(exc)) from exc
    try:
        async with aa_reader.content_connection(getattr(request.app.state, "pool", None)) as conn:
            page = await aa_reader.list_published(conn, after=after, kind=kind_value, locale=locale_value,
                                                  limit=page_size)
    except aa_reader.ContentUnavailable as exc:
        raise _aa_unavailable(exc) from exc
    body = {
        "items": page.items,
        "next_cursor": (aa_reader.encode_cursor(page.next_after, kind_value, locale_value, page_size)
                        if page.next_after else None),
    }
    logger.info("catalog_http_served", op="listAaContent", client_id=caller.client_id, items=len(body["items"]),
                more=body["next_cursor"] is not None)
    return _cached(request, body, _body_hash(body))


@router.get("/aa-content/{content_id}/versions/{version}", responses={200: {"model": CatalogAaContent}})
async def get_aa_content_version(content_id: str, version: str, request: Request,
                                 caller: m2m_auth.M2MCaller = Depends(require_catalog_reader)) -> Response:
    try:
        key = uuid.UUID(content_id)
    except ValueError as exc:
        raise CatalogProblem(400, "invalid", "content_id must be a UUID") from exc
    # One spelling per version (no leading zero: '0001' would be a second cache key for version 1).
    if (len(version) > 10 or not version.isascii() or not version.isdigit() or version.startswith("0")
            or not 1 <= int(version) <= 2_147_483_647):
        raise CatalogProblem(400, "invalid", "version must be a positive integer")
    try:
        async with aa_reader.content_connection(getattr(request.app.state, "pool", None)) as conn:
            body = await aa_reader.get_published_version(conn, key, int(version))
    except aa_reader.ContentUnavailable as exc:
        raise _aa_unavailable(exc) from exc
    logger.info("catalog_http_served", op="getAaContentVersion", client_id=caller.client_id, content_id=str(key),
                version=int(version), found=body is not None)
    if body is None:
        raise CatalogProblem(404, "not_found", f"No published version {int(version)} of this content.")
    return _cached(request, body, body["content_hash"])
