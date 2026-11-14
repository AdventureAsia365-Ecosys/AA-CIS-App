"""AA-1106 — the product catalogue behind the admin page Platform › Product map (/admin/platform/catalogue).

The root repo keeps the catalogue as two YAML files (docs/catalogue/features.yaml and integrations.yaml: every app's
features with their pages, API route groups and enabling conditions; every link between the apps and to outside
providers with its contract, auth, conditions, cadence, failure behaviour and kill switch; the business journeys). Its
"Docs sync" workflow checks them (tools/catalogue/catalogue.py: contracts, UAT scenarios, settings and routes all map)
and uploads one bundle next to the docs reader manifest:

  <prefix>/catalogue.json     format 1: source, apps, features, integrations, journeys, scenarios, settings, counts

<prefix> is the docs reader prefix (DOCS_READER_S3_PREFIX, default "docs-reader"; api/services/docs_reader.py), so the
CIS task role reads it under the same read-only grant and nothing new is provisioned. Read only: no table, no write
path. The bundle is cached in process like the docs manifest (docs_reader.CACHE_TTL_S, ?refresh=1 at most every
docs_reader.MIN_REFRESH_S) and refused above docs_reader.MAX_MANIFEST_BYTES.

Defence in depth behind the root guard (which refuses an account ID, an ARN or an e-mail address in the YAML and in
the bundle): only the known top-level sections are kept, only lists and objects of the expected shape pass, and any
12-digit run left in a string is masked again when served, with a log line, because one there means the bundle was
written by something else.

CatalogueMissing -> 404 (nothing published yet), CatalogueUnavailable -> 503 (storage or format).
This repo is public: it ships the reader only, no catalogue content.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import structlog

from api.services import docs_reader

logger = structlog.get_logger()

SUPPORTED_FORMAT = 1
OBJECT_NAME = "catalogue.json"
_LIST_SECTIONS = ("apps", "features", "integrations", "journeys")
_MAP_SECTIONS = ("scenarios", "settings", "counts")


class CatalogueMissing(Exception):
    """No catalogue bundle published yet (root repo Docs sync)."""


class CatalogueUnavailable(Exception):
    """Storage failed or the bundle is not in the expected shape."""


_cache: dict[str, Any] = {"value": None, "at": 0.0}
_lock = asyncio.Lock()


def reset_cache() -> None:
    _cache["value"] = None
    _cache["at"] = 0.0


def _mask(value: Any, hits: list[int]) -> Any:
    """`value` with every 12-digit run in a string replaced by the docs reader marker; counts the hits."""
    if isinstance(value, str):
        masked, n = docs_reader._ACCOUNT_ID.subn(docs_reader.ACCOUNT_ID_MARKER, value)
        hits[0] += n
        return masked
    if isinstance(value, list):
        return [_mask(v, hits) for v in value]
    if isinstance(value, dict):
        return {str(k): _mask(v, hits) for k, v in value.items()}
    return value


def _with_ids(rows: Any) -> list[dict]:
    """Only objects with a string id (a bad row is dropped, not served)."""
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]]


def _build(client_factory: Callable[[], Any]) -> dict:
    key = f"{docs_reader.s3_prefix()}/{OBJECT_NAME}"
    try:
        raw = docs_reader._read_bytes(client_factory(), docs_reader._bucket(), key, docs_reader.MAX_MANIFEST_BYTES)
    except Exception as err:
        logger.warning("product_catalogue_read_failed", key=key, error=type(err).__name__)
        raise CatalogueUnavailable("The product catalogue could not be read from storage") from err
    if raw is None:
        raise CatalogueMissing("No product catalogue has been published yet (root repo Docs sync)")
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise CatalogueUnavailable("The stored product catalogue is not JSON") from err
    if not isinstance(data, dict) or data.get("format") != SUPPORTED_FORMAT or not isinstance(data.get("features"),
                                                                                                list):
        raise CatalogueUnavailable(f"The stored product catalogue is not format {SUPPORTED_FORMAT}")
    out: dict[str, Any] = {name: _with_ids(data.get(name)) for name in _LIST_SECTIONS}
    dropped = sum(len(data.get(n) or []) - len(out[n]) for n in _LIST_SECTIONS if isinstance(data.get(n), list))
    if dropped:
        logger.warning("product_catalogue_rows_dropped", dropped=dropped)
    for name in _MAP_SECTIONS:
        out[name] = data.get(name) if isinstance(data.get(name), dict) else {}
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    out["source"] = {k: source.get(k) for k in ("repo", "sha", "ref")}
    out["generated_at"] = data.get("generated_at") if isinstance(data.get("generated_at"), str) else None
    hits = [0]
    out = _mask(out, hits)
    if hits[0]:
        logger.error("product_catalogue_account_id_masked", count=hits[0])
    out["format"] = SUPPORTED_FORMAT
    out["loaded_at"] = datetime.now(timezone.utc).isoformat()
    return out


async def load(refresh: bool = False, client_factory: Optional[Callable[[], Any]] = None) -> dict:
    """The catalogue bundle (cached). `refresh` re-reads storage when the cached copy is older than MIN_REFRESH_S."""
    now = time.monotonic()
    max_age = docs_reader.MIN_REFRESH_S if refresh else docs_reader.CACHE_TTL_S
    value, at = _cache["value"], _cache["at"]
    if value is not None and now - at < max_age:
        return value
    async with _lock:
        value, at = _cache["value"], _cache["at"]
        if value is not None and time.monotonic() - at < max_age:
            return value
        value = await asyncio.to_thread(_build, client_factory or docs_reader._s3_client)
        _cache["value"], _cache["at"] = value, time.monotonic()
        return value
