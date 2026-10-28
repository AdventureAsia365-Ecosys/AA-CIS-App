"""AA-898 — the docs reader behind the admin page /admin/docs (Platform › Developer › Docs).

The root repo workflow "Docs sync" builds a bundle from a CI checkout of the docs catalog (publication rule v1:
catalog pages and ADRs only, journals / audits / security pages never, account and resource IDs redacted) and
uploads it to the bronze bucket:

  <prefix>/manifest.json                 format 1: source repo + sha, counts, one entry per published page
  <prefix>/pages/<repo>/<path>.md        the redacted Markdown body of each page (front-matter stripped)

<prefix> is DOCS_READER_S3_PREFIX (default "docs-reader"); the bucket is shared/aws_client/bronze.py. The CIS task
role reads that prefix only (AA-CIS-Infra modules/ecs ReadDocsReader) and is denied writes there.

Access tiers (root ADR / engineering handbook N-3): an entry with access "owner" is listed and served only to a caller
the route lets read owner pages (``owner_read``: a verified Cognito owner, or the Dev-only E2E CIS admin, see
api/core/staff_authz.can_read_owner_docs); every other admin sees the "staff" entries. Any other access value is
treated as owner (fail closed). Pages are only ever read by the `key` the manifest names, after the key passed a shape
check, never by a path the caller typed.

Translations (root build_docs_reader.py rule 8, S255): a Vietnamese copy is its own entry with ``lang`` "vi" and
``translation_of`` (the English entry's path); an English entry lists its copies in ``translations``
([{lang, path, key, title}]). The reader re-checks the links: an item is kept only when its path and key pass the same
shape checks as an entry, a listed entry with that path names this page in ``translation_of`` with the same ``lang``,
and the language is not English (one item per language). A translation is never less protected than its English page:
an owner original makes its translation owner too. A manifest without the fields (older builder) reads as English
pages without translations.

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
                 "last_reviewed", "generated", "access", "bytes", "lang", "translation_of", "translations")

DEFAULT_LANG = "en"
_LANG = re.compile(r"^[a-z]{2}$")
MAX_TRANSLATIONS = 8               # per page; the builder emits one per language


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


def _valid_path(path: Any) -> bool:
    return isinstance(path, str) and bool(_PATH.match(path)) and ".." not in path.split("/")


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
    if not _valid_path(path):
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
    lang = raw.get("lang")
    entry["lang"] = lang if isinstance(lang, str) and _LANG.match(lang) else DEFAULT_LANG
    of = raw.get("translation_of")
    entry["translation_of"] = (of if entry["lang"] != DEFAULT_LANG and _valid_path(of) and of != path else None)
    is_english = entry["lang"] == DEFAULT_LANG
    entry["translations"] = _clean_translations(raw.get("translations"), repo, path) if is_english else []
    return entry


def _clean_translations(raw: Any, repo: str, path: str) -> list[dict]:
    """The `translations` items of an English entry that have the right shape (links are checked later)."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        lang, tpath, title = item.get("lang"), item.get("path"), item.get("title")
        if not (isinstance(lang, str) and _LANG.match(lang) and lang != DEFAULT_LANG and lang not in seen):
            continue
        if not (_valid_path(tpath) and tpath != path and _valid_key(item.get("key"), repo, tpath)):
            continue
        seen.add(lang)
        out.append({"lang": lang, "path": tpath, "key": item["key"],
                    "title": title if isinstance(title, str) else None})
        if len(out) >= MAX_TRANSLATIONS:
            break
    return sorted(out, key=lambda t: t["lang"])


def _link_translations(entries: list[dict]) -> None:
    """Keep only translation links both ends agree on (module docstring, "Translations"), in place."""
    by_path = {(e["repo"], e["path"]): e for e in entries}
    linked: set[tuple[str, str]] = set()
    for e in entries:
        kept = []
        for t in e["translations"]:
            other = by_path.get((e["repo"], t["path"]))
            if (other is None or other["translation_of"] != e["path"] or other["lang"] != t["lang"]
                    or other["key"] != t["key"]):
                continue
            kept.append(t)
            linked.add((e["repo"], t["path"]))
            if e["access"] == ACCESS_OWNER:
                other["access"] = ACCESS_OWNER
        if len(kept) != len(e["translations"]):
            logger.warning("docs_reader_translations_dropped", path=e["path"],
                           dropped=len(e["translations"]) - len(kept))
        e["translations"] = kept
    for e in entries:
        if e["translation_of"] is None:
            continue
        orig = by_path.get((e["repo"], e["translation_of"]))
        if orig is not None and orig["access"] == ACCESS_OWNER:
            e["access"] = ACCESS_OWNER   # fail closed even when the original does not list this copy
        if (e["repo"], e["path"]) not in linked:
            e["translation_of"] = None   # dangling or one-sided: stays unlisted (lang is not English), no toggle


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
    _link_translations(entries)
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


def _public(entry: dict, visible_paths: set[tuple[str, str]]) -> dict:
    out = {k: v for k, v in entry.items() if k not in ("key", "sha256")}
    out["translations"] = [{"lang": t["lang"], "path": t["path"], "title": t["title"]} for t in entry["translations"]
                           if (entry["repo"], t["path"]) in visible_paths]
    return out


def _visible(entries: list[dict], owner_read: bool) -> list[dict]:
    return [e for e in entries if e["access"] == ACCESS_STAFF or owner_read]


async def load_manifest(owner_read: bool, refresh: bool = False,
                        client_factory: Optional[Callable[[], Any]] = None, *, is_owner: Optional[bool] = None) -> dict:
    """The manifest as this caller may see it: owner-only entries are left out unless `owner_read`. `is_owner` is the
    caller's real owner flag, echoed as is (defaults to `owner_read`); the Dev-only E2E admin reads owner pages with
    is_owner False."""
    m = await _manifest(refresh, client_factory)
    visible = _visible(m["entries"], owner_read)
    paths = {(e["repo"], e["path"]) for e in visible}
    pages = [_public(e, paths) for e in visible]
    # Counted in pages a reader would list (English), so a hidden page and its translation count once.
    hidden = sum(1 for e in m["entries"] if e["access"] != ACCESS_STAFF and e["lang"] == DEFAULT_LANG) - \
        sum(1 for p in pages if p["access"] != ACCESS_STAFF and p["lang"] == DEFAULT_LANG)
    return {
        "generated_at": m["generated_at"],
        "rule": m["rule"],
        "source": m["source"],
        "pages": pages,
        "counts": {"shown": len(pages), "owner_only_shown": sum(1 for p in pages if p["access"] == ACCESS_OWNER),
                   "hidden_owner_only": hidden},
        "is_owner": owner_read if is_owner is None else is_owner,
        "owner_read": owner_read,
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


async def load_page(repo: str, path: str, owner_read: bool,
                    client_factory: Optional[Callable[[], Any]] = None) -> dict:
    """One page by (repo, path), only when the manifest lists it for this caller. 404 otherwise, owner pages
    included (a caller without owner_read can not tell an owner page from a missing one)."""
    m = await _manifest(False, client_factory)
    visible = _visible(m["entries"], owner_read)
    entry = next((e for e in visible if e["repo"] == repo and e["path"] == path), None)
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
    paths = {(e["repo"], e["path"]) for e in visible}
    return {"page": _public(entry, paths), "markdown": markdown, "source": m["source"],
            "generated_at": m["generated_at"]}
