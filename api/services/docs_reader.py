"""AA-898 — the docs reader behind the admin page /admin/docs (Platform › Developer › Docs).

The root repo workflow "Docs sync" builds a bundle from a CI checkout of the docs catalog (publication rule v1:
catalog pages and ADRs only, journals / audits / security pages never, account and resource IDs redacted) and
uploads it to the bronze bucket:

  <prefix>/manifest.json                 format 1: source repo + sha, counts, one entry per published page
  <prefix>/pages/<repo>/<path>.md        the redacted Markdown body of each page (front-matter stripped)

<prefix> is DOCS_READER_S3_PREFIX (default "docs-reader"); the bucket is shared/aws_client/bronze.py. The CIS task
role reads that prefix only (AA-CIS-Infra modules/ecs ReadDocsReader) and is denied writes there.

Access tiers (root ADR / engineering handbook N-3): an entry with access "owner" is listed and served only to a staff
member whose verified Cognito session carries the owner flag; every other admin sees the "staff" entries. Any other
access value is treated as owner (fail closed). Pages are only ever read by the `key` the manifest names, after the
key passed a shape check, never by a path the caller typed.

This repo is public: it ships the reader only, no page content.

load_manifest() is cached in process for CACHE_TTL_S; load_page() caches each body for CACHE_TTL_S too (bounded).
DocsMissing -> 404 (no manifest yet, or no such page for this caller), DocsUnavailable -> 503 (storage or format).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

SUPPORTED_FORMAT = 1
DEFAULT_PREFIX = "docs-reader"
CACHE_TTL_S = 300
MIN_REFRESH_S = 30                 # ?refresh=1 re-reads S3 at most this often per process
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024   # the builder skips pages over 1 MB; refuse anything absurd
PAGE_CACHE_MAX = 64

ACCESS_STAFF = "staff"
ACCESS_OWNER = "owner"

# What a page key may look like: pages/<repo>/<relative path>.md, no climbing, no absolute or odd characters.
_KEY = re.compile(r"^pages/[A-Za-z0-9._-]+/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.md$")
_PATH = re.compile(r"^(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.md$")

# Defence in depth behind the builder's redaction + guard (root tools/docs/build_docs_reader.py): a 12-digit run (an
# AWS account ID, also inside a bucket name or an ARN) is masked again when a page is served, and the event is logged,
# because a page that still carries one means the guard was bypassed or the bundle was written by something else.
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
ACCOUNT_ID_MARKER = "[redacted-account-id]"

# Fields copied from a manifest entry into the API response (anything else in the object is dropped).
_ENTRY_FIELDS = ("repo", "path", "id", "title", "description", "type", "status", "audience", "owner",
                 "last_reviewed", "generated", "access", "bytes")


class DocsMissing(Exception):
    """No manifest published yet, or the page is not in the manifest for this caller."""


class DocsUnavailable(Exception):
    """Storage failed or an object is not in the expected shape."""


def s3_prefix() -> str:
    return (os.environ.get("DOCS_READER_S3_PREFIX", "").strip() or DEFAULT_PREFIX).strip("/")


def _s3_client():
    import boto3

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))


def _bucket() -> str:
    from shared.aws_client.bronze import bronze_bucket

    return bronze_bucket()


def _is_missing(err: Exception) -> bool:
    response = getattr(err, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code in ("NoSuchKey", "404", "NotFound")


def _read_bytes(client, bucket: str, key: str, limit: int) -> Optional[bytes]:
    """The object's bytes; None when it does not exist. Raises on any other failure or when it is over `limit`."""
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except Exception as err:  # botocore ClientError, or a fake in tests
        if _is_missing(err):
            return None
        raise
    size = obj.get("ContentLength")
    if isinstance(size, int) and size > limit:
        raise ValueError(f"{key} is {size} bytes, over the {limit} byte limit")
    body = obj["Body"].read(limit + 1)
    if len(body) > limit:
        raise ValueError(f"{key} is over the {limit} byte limit")
    return body


def _valid_key(key: Any, repo: str, path: str) -> bool:
    return (isinstance(key, str) and bool(_KEY.match(key)) and ".." not in key.split("/")
            and key == f"pages/{repo}/{path}")


def _clean_entry(raw: Any) -> Optional[dict]:
    """A manifest entry reduced to the fields the API serves, or None when it is not usable."""
    if not isinstance(raw, dict):
        return None
    repo, path = raw.get("repo"), raw.get("path")
    if not (isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9._-]+", repo)):
        return None
    if not (isinstance(path, str) and _PATH.match(path) and ".." not in path.split("/")):
        return None
    if not _valid_key(raw.get("key"), repo, path):
        return None
    if not (isinstance(raw.get("id"), str) and isinstance(raw.get("title"), str)):
        return None
    entry = {k: raw.get(k) for k in _ENTRY_FIELDS}
    entry["access"] = ACCESS_STAFF if raw.get("access") == ACCESS_STAFF else ACCESS_OWNER
    entry["audience"] = [a for a in (raw.get("audience") or []) if isinstance(a, str)]
    entry["generated"] = bool(raw.get("generated"))
    entry["key"] = raw["key"]
    entry["sha256"] = raw.get("sha256") if isinstance(raw.get("sha256"), str) else None
    return entry


# ── manifest ────────────────────────────────────────────────────────────────────────────────────────

_cache: dict[str, Any] = {"value": None, "at": 0.0}
_lock = asyncio.Lock()
_pages: "OrderedDict[tuple[str, Optional[str]], tuple[float, str]]" = OrderedDict()


def reset_cache() -> None:
    _cache["value"] = None
    _cache["at"] = 0.0
    _pages.clear()


def _build_manifest(client_factory: Callable[[], Any]) -> dict:
    client = client_factory()
    key = f"{s3_prefix()}/manifest.json"
    try:
        raw = _read_bytes(client, _bucket(), key, MAX_MANIFEST_BYTES)
    except Exception as err:
        logger.warning("docs_reader_manifest_read_failed", key=key, error=type(err).__name__)
        raise DocsUnavailable("The docs manifest could not be read from storage") from err
    if raw is None:
        raise DocsMissing("No docs have been published yet (root repo Docs sync)")
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise DocsUnavailable("The stored docs manifest is not JSON") from err
    if not isinstance(data, dict) or data.get("format") != SUPPORTED_FORMAT or not isinstance(data.get("pages"), list):
        raise DocsUnavailable(f"The stored docs manifest is not format {SUPPORTED_FORMAT}")
    entries, dropped = [], 0
    seen: set[tuple[str, str]] = set()
    for raw_entry in data["pages"]:
        entry = _clean_entry(raw_entry)
        if entry is None or (entry["repo"], entry["path"]) in seen:
            dropped += 1
            continue
        seen.add((entry["repo"], entry["path"]))
        entries.append(entry)
    if dropped:
        logger.warning("docs_reader_manifest_entries_dropped", dropped=dropped)
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    return {
        "generated_at": data.get("generated_at"),
        "rule": data.get("rule"),
        "source": {k: source.get(k) for k in ("repo", "sha", "ref")},
        "entries": entries,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }


async def _manifest(refresh: bool, client_factory: Optional[Callable[[], Any]]) -> dict:
    now = time.monotonic()
    value, at = _cache["value"], _cache["at"]
    max_age = MIN_REFRESH_S if refresh else CACHE_TTL_S
    if value is not None and now - at < max_age:
        return value
    async with _lock:
        value, at = _cache["value"], _cache["at"]
        if value is not None and time.monotonic() - at < max_age:
            return value
        value = await asyncio.to_thread(_build_manifest, client_factory or _s3_client)
        _cache["value"], _cache["at"] = value, time.monotonic()
        return value


def _public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in ("key", "sha256")}


def _visible(entries: list[dict], is_owner: bool) -> list[dict]:
    return [e for e in entries if e["access"] == ACCESS_STAFF or is_owner]


async def load_manifest(is_owner: bool, refresh: bool = False,
                        client_factory: Optional[Callable[[], Any]] = None) -> dict:
    """The manifest as this caller may see it: owner-only entries are left out unless `is_owner`."""
    m = await _manifest(refresh, client_factory)
    pages = [_public(e) for e in _visible(m["entries"], is_owner)]
    hidden = len(m["entries"]) - len(pages)
    return {
        "generated_at": m["generated_at"],
        "rule": m["rule"],
        "source": m["source"],
        "pages": pages,
        "counts": {"shown": len(pages), "owner_only_shown": sum(1 for p in pages if p["access"] == ACCESS_OWNER),
                   "hidden_owner_only": hidden},
        "is_owner": is_owner,
        "loaded_at": m["loaded_at"],
        "cache_ttl_s": CACHE_TTL_S,
    }


# ── pages ───────────────────────────────────────────────────────────────────────────────────────────

def _read_page(client_factory: Callable[[], Any], key: str, expected_sha: Optional[str]) -> str:
    client = client_factory()
    full_key = f"{s3_prefix()}/{key}"
    try:
        raw = _read_bytes(client, _bucket(), full_key, MAX_PAGE_BYTES)
    except Exception as err:
        logger.warning("docs_reader_page_read_failed", key=full_key, error=type(err).__name__)
        raise DocsUnavailable("The page could not be read from storage") from err
    if raw is None:
        # Listed but not there: a sync in progress, or a removed page under an older manifest.
        raise DocsMissing("The page is listed but not stored yet; try again in a minute")
    if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
        # The manifest goes up after the pages, so a newer body under an older manifest is expected during a sync.
        logger.info("docs_reader_page_sha_mismatch", key=full_key)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        raise DocsUnavailable("The stored page is not UTF-8 text") from err
    text, hits = _ACCOUNT_ID.subn(ACCOUNT_ID_MARKER, text)
    if hits:
        logger.warning("docs_reader_page_redacted_on_serve", key=full_key, hits=hits)
    return text


async def load_page(repo: str, path: str, is_owner: bool,
                    client_factory: Optional[Callable[[], Any]] = None) -> dict:
    """One page by (repo, path), only when the manifest lists it for this caller. 404 otherwise, owner pages
    included (a non-owner can not tell an owner page from a missing one)."""
    m = await _manifest(False, client_factory)
    entry = next((e for e in _visible(m["entries"], is_owner) if e["repo"] == repo and e["path"] == path), None)
    if entry is None:
        raise DocsMissing("No such page")
    cache_key = (entry["key"], entry["sha256"])
    hit = _pages.get(cache_key)
    if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_S:
        _pages.move_to_end(cache_key)
        markdown = hit[1]
    else:
        markdown = await asyncio.to_thread(_read_page, client_factory or _s3_client, entry["key"], entry["sha256"])
        _pages[cache_key] = (time.monotonic(), markdown)
        _pages.move_to_end(cache_key)
        while len(_pages) > PAGE_CACHE_MAX:
            _pages.popitem(last=False)
    return {"page": _public(entry), "markdown": markdown, "source": m["source"], "generated_at": m["generated_at"]}
