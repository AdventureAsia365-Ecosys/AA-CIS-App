"""AA-1133 — full-text search of the /admin/docs reader (tech lead S283 c-s283-docs-search: server-side FTS5).

The root repo workflow "Docs sync" builds a SQLite FTS5 index of every published section at publish time (root
tools/docs/docs_search_index.py, format 1) and uploads it next to the pages as ``<prefix>/search/index.sqlite``; the
manifest names it with its sha256 (``search``, cleaned by docs_reader._clean_search). This module:

  * reads that object under the same read-only prefix grant as the pages (no other AWS access, no table, no write),
    refuses it when its sha256 differs from the manifest (a sync in progress: 503, try again) or when it is over
    docs_reader.MAX_SEARCH_BYTES, and opens it in memory read only (``sqlite3.Connection.deserialize`` +
    ``PRAGMA query_only``), cached per manifest hash (one index in memory, about 6 MB);
  * turns the typed text into a safe FTS5 query: word characters only, each a quoted prefix term, all required
    (AND), at most MAX_TERMS terms; nothing the caller types reaches FTS5 syntax;
  * ranks with bm25 (title, heading, body, the "đ"-folded copy), keeps the hits of pages the manifest lists FOR THIS
    CALLER (owner-only sections are dropped in SQL unless the caller may read owner pages, and again by the manifest
    filter, fail closed), groups them per page (best section first, at most SECTIONS_PER_PAGE) and returns at most
    ``limit`` pages;
  * returns each snippet as segments ``[{text, hit}]`` (no markup to render), with 12-digit runs masked like a page.

Errors: DocsMissing (no index published yet: 404, the reader falls back to its title filter), DocsUnavailable
(storage, hash or format: 503). This repo is public: no page content lives here; the tests build their own index.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import threading
from typing import Any, Callable, Optional

import structlog

from api.services import docs_reader

logger = structlog.get_logger()

MAX_TERMS = 8                      # technical-constant: query terms used (the rest are ignored)
TERM_CHARS = 64                    # technical-constant: a longer word is cut (FTS5 prefix term)
MAX_HITS = 200                     # technical-constant: sections ranked before grouping per page
SECTIONS_PER_PAGE = 3              # technical-constant: sections shown under one page result
SNIPPET_TOKENS = 14                # technical-constant: words around a hit in a snippet
_HIT_START, _HIT_END = "", ""
_TERM = re.compile(r"\w+", re.UNICODE)

_cache: dict[str, Any] = {"sha": None, "conn": None}
_load_lock = asyncio.Lock()
_query_lock = threading.Lock()     # one sqlite3 connection, used from worker threads one at a time


def reset_cache() -> None:
    conn = _cache.get("conn")
    if conn is not None:
        conn.close()
    _cache["sha"], _cache["conn"] = None, None


def terms(text: str) -> list[str]:
    """The search terms of what was typed: word characters only, lower case, at most MAX_TERMS, each cut to
    TERM_CHARS, duplicates dropped."""
    out: list[str] = []
    for t in _TERM.findall(text.lower()):
        t = t[:TERM_CHARS]
        if t not in out:
            out.append(t)
        if len(out) >= MAX_TERMS:
            break
    return out


def fts_query(words: list[str]) -> str:
    """Every term as a quoted prefix term, all required. Terms hold word characters only, so no quote can appear."""
    return " ".join(f'"{w}"*' for w in words)


def snippet_segments(snippet: str) -> list[dict]:
    """The snippet as [{text, hit}] segments (hit = a matched term); 12-digit runs masked."""
    text = docs_reader._ACCOUNT_ID.sub(docs_reader.ACCOUNT_ID_MARKER, snippet or "")
    out: list[dict] = []
    for i, part in enumerate(re.split(f"[{_HIT_START}{_HIT_END}]", text)):
        if part:
            out.append({"text": part, "hit": i % 2 == 1})
    return out


def _open(data: bytes) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        conn.deserialize(data)
        conn.execute("PRAGMA query_only = ON")
        fmt = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
        if not fmt or fmt[0] != str(docs_reader.SEARCH_FORMAT):
            raise ValueError("unsupported search index format")
        conn.execute("SELECT count(*) FROM sections WHERE sections MATCH 'x' LIMIT 1").fetchone()
    except Exception:
        conn.close()
        raise
    return conn


def _load_index(client_factory: Callable[[], Any], ref: dict) -> sqlite3.Connection:
    key = f"{docs_reader.s3_prefix()}/{ref['key']}"
    try:
        raw = docs_reader._read_bytes(client_factory(), docs_reader._bucket(), key, docs_reader.MAX_SEARCH_BYTES)
    except Exception as err:
        logger.warning("docs_search_index_read_failed", key=key, error=type(err).__name__)
        raise docs_reader.DocsUnavailable("The search index could not be read from storage") from err
    if raw is None:
        raise docs_reader.DocsUnavailable("The search index is listed but not stored yet; try again in a minute")
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        # The index goes up before the manifest that names it: during a sync the hashes differ for a moment.
        logger.info("docs_search_index_sha_mismatch", key=key)
        raise docs_reader.DocsUnavailable("The search index is being updated; try again in a minute")
    try:
        return _open(raw)
    except (sqlite3.Error, ValueError) as err:
        logger.warning("docs_search_index_refused", key=key, error=type(err).__name__)
        raise docs_reader.DocsUnavailable("The stored search index is not in the expected format") from err


async def _index(m: dict, client_factory: Optional[Callable[[], Any]]) -> sqlite3.Connection:
    ref = m.get("search")
    if ref is None:
        raise docs_reader.DocsMissing("No search index has been published yet (root repo Docs sync)")
    if _cache["sha"] == ref["sha256"] and _cache["conn"] is not None:
        return _cache["conn"]
    async with _load_lock:
        if _cache["sha"] == ref["sha256"] and _cache["conn"] is not None:
            return _cache["conn"]
        conn = await asyncio.to_thread(_load_index, client_factory or docs_reader._s3_client, ref)
        old = _cache.get("conn")
        _cache["sha"], _cache["conn"] = ref["sha256"], conn
        if old is not None:
            with _query_lock:
                old.close()
        return conn


_SQL = (
    "SELECT repo, path, lang, access, slug, heading, "
    f"snippet(sections, 2, '{_HIT_START}', '{_HIT_END}', '…', {SNIPPET_TOKENS}) AS snip, "
    "bm25(sections, 8.0, 4.0, 1.0, 1.0) AS rank "
    "FROM sections WHERE sections MATCH ? AND (access = 'staff' OR ?) ORDER BY rank LIMIT ?"
)


def _run(conn: sqlite3.Connection, query: str, owner_read: bool) -> list[tuple]:
    with _query_lock:
        return conn.execute(_SQL, (query, 1 if owner_read else 0, MAX_HITS)).fetchall()


async def search(text: str, owner_read: bool, limit: int,
                 client_factory: Optional[Callable[[], Any]] = None) -> dict:
    """The pages matching ``text`` for this caller, best first: {query, terms, results: [{repo, path, lang, id, title,
    type, nav, access, sections: [{heading, slug, snippet: [{text, hit}]}]}], total_pages, source_sha}."""
    words = terms(text)
    m = await docs_reader.cached_manifest(client_factory)
    if not words:
        return {"query": text, "terms": [], "results": [], "total_pages": 0, "source_sha": m["source"].get("sha")}
    conn = await _index(m, client_factory)
    try:
        rows = await asyncio.to_thread(_run, conn, fts_query(words), owner_read)
    except sqlite3.Error as err:
        logger.warning("docs_search_query_failed", error=type(err).__name__)
        raise docs_reader.DocsUnavailable("The search could not run") from err
    visible = {(e["repo"], e["path"]): e for e in docs_reader.visible_entries(m, owner_read)}
    pages: dict[tuple[str, str], dict] = {}
    for repo, path, lang, access, slug, heading, snip, _rank in rows:
        entry = visible.get((repo, path))
        if entry is None or (access != docs_reader.ACCESS_STAFF and not owner_read):
            continue   # not listed for this caller (owner tier, or gone from the manifest): fail closed
        page = pages.get((repo, path))
        if page is None:
            page = {"repo": repo, "path": path, "lang": entry["lang"], "id": entry["id"], "title": entry["title"],
                    "type": entry["type"], "nav": entry["nav"], "access": entry["access"], "sections": []}
            pages[(repo, path)] = page
        if len(page["sections"]) < SECTIONS_PER_PAGE:
            page["sections"].append({"heading": heading, "slug": slug, "snippet": snippet_segments(snip)})
    results = list(pages.values())
    return {"query": text, "terms": words, "results": results[:limit], "total_pages": len(results),
            "source_sha": m["source"].get("sha")}
