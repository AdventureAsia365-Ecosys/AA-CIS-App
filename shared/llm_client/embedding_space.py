"""AA-1026 D5 — the embedding space registry (migration 328): which vector space every vector column is in.

A space is one model at one dimension (``cohere-embed-v4/1536/v1``): vectors of two spaces are never stored in one
column or compared in one query. ``shared.embedding_space`` lists the spaces (the catalog keys that produce vectors in
it, e.g. cohere-embed-v4 on acc2 and cohere-embed-v4-acc3 on acc3; the dimension; the input type for stored text and
for search text); ``shared.embedding_column`` gives each vector column its space, its swap group (columns compared
with each other or written by one stage: they always share a space and swap together), its key columns and its
re-embed text source.

Writers and searches ask ``space_for(column)`` and pass the space to ``embed.embed(..., space=...)``: the gateway then
tries only the space's models (the stage route filtered to them, else the space's own models), at the space's
dimension, and the response carries ``space_key`` for the ledger row (``ledger_signal``). Moving a column to another
space is data: a re-embed into ``<column>__shadow`` (job ``embedding_reembed``), then ``shared.embedding_swap`` (CIS
329) changes the registry row in the same transaction as the column rename, so the next registry read embeds in the
new space with no code change.

Like ``connections.py``: two tiny tables, read in one round trip and cached in process (60 s; a failed read is kept
30 s); the read never raises. ``BUILTIN_SPACES`` / ``BUILTIN_COLUMNS`` mirror the 328 seed field for field (tests
compare them with the SQL) and answer when the registry cannot be read (DB down, before 328), which is today's
single space.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

import structlog

from . import connections

logger = structlog.get_logger()

_CACHE_TTL_SECONDS = 60.0
_FAILURE_TTL_SECONDS = 30.0

ATOM_EMBEDDING = "acp_contract.atom_embedding.embedding"
QUESTION_EMBEDDING = "acp_contract.question_embedding.embedding"
CONTENT_PIECE_EMBEDDING = "acp_shared.content_piece.content_embedding"
TOUR_PLACE_EMBEDDING = "geo.tour_place.embedding"
PUBLISHED_TOUR_EMBEDDING = "gold_aa_internal.published_tours.content_embedding"


@dataclass(frozen=True)
class EmbeddingSpace:
    space_key: str
    model_family: str
    model_id: str
    model_keys: tuple[str, ...]
    dimension: int
    document_input_type: Optional[str]
    query_input_type: Optional[str]
    version: int
    status: str

    def input_type(self, role: str) -> Optional[str]:
        """The model's input type for ``role`` = "document" (stored text) or "query" (search text); None when the
        model has none (Titan)."""
        if role == "document":
            return self.document_input_type
        if role == "query":
            return self.query_input_type
        raise ValueError(f"unknown embedding role {role!r} (document | query)")


@dataclass(frozen=True)
class EmbeddingColumn:
    table_schema: str
    table_name: str
    column_name: str
    space_key: str
    swap_group: str
    key_columns: tuple[str, ...]
    writer_stages: tuple[str, ...] = ()
    source_key: Optional[str] = None
    shadow_column: Optional[str] = None
    shadow_space_key: Optional[str] = None
    previous_column: Optional[str] = None
    previous_space_key: Optional[str] = None

    @property
    def qualified(self) -> str:
        return f"{self.table_schema}.{self.table_name}.{self.column_name}"

    @property
    def table(self) -> str:
        return f"{self.table_schema}.{self.table_name}"


COHERE_V4_1536 = "cohere-embed-v4/1536/v1"
TITAN_V2_1024 = "titan-embed-text-v2/1024/v1"

# The 328 seed, field for field (tests/unit/test_aa1026_embedding_space.py compares them with the SQL).
BUILTIN_SPACES: dict[str, EmbeddingSpace] = {
    COHERE_V4_1536: EmbeddingSpace(COHERE_V4_1536, "cohere", "cohere.embed-v4:0",
                                   ("cohere-embed-v4", "cohere-embed-v4-acc3"), 1536, "search_document",
                                   "search_query", 1, "active"),
    TITAN_V2_1024: EmbeddingSpace(TITAN_V2_1024, "amazon", "amazon.titan-embed-text-v2:0", ("titan-embed-text-v2",),
                                  1024, None, None, 1, "standby"),
}
BUILTIN_COLUMNS: dict[str, EmbeddingColumn] = {c.qualified: c for c in (
    EmbeddingColumn("acp_contract", "atom_embedding", "embedding", COHERE_V4_1536, "f10_embed",
                    ("atom_id", "view"), ("f10_embed",), "atom_view"),
    EmbeddingColumn("acp_contract", "question_embedding", "embedding", COHERE_V4_1536, "f10_embed",
                    ("question_hash",), ("f10_embed",), "question_text"),
    EmbeddingColumn("acp_shared", "content_piece", "content_embedding", COHERE_V4_1536, "f10_embed",
                    ("piece_id",), ("f10_embed",), "content_piece_text"),
    EmbeddingColumn("geo", "tour_place", "embedding", COHERE_V4_1536, "tour_place",
                    ("component_id",), ("a3_place_embed",), "tour_place_text"),
    EmbeddingColumn("gold_aa_internal", "published_tours", "content_embedding", COHERE_V4_1536, "published_tours",
                    ("id",), (), None),
)}


_SPACES_SQL = """
    SELECT space_key, model_family, model_id, model_keys, dimension, document_input_type, query_input_type, version,
           status
      FROM shared.embedding_space
"""
_COLUMNS_SQL = """
    SELECT table_schema, table_name, column_name, space_key, swap_group, key_columns, writer_stages, source_key,
           shadow_column, shadow_space_key, previous_column, previous_space_key
      FROM shared.embedding_column
"""


def _space_from_row(r) -> EmbeddingSpace:
    return EmbeddingSpace(r["space_key"], r["model_family"], r["model_id"], tuple(r["model_keys"] or ()),
                          int(r["dimension"]), r["document_input_type"], r["query_input_type"], int(r["version"]),
                          r["status"])


def _column_from_row(r) -> EmbeddingColumn:
    return EmbeddingColumn(r["table_schema"], r["table_name"], r["column_name"], r["space_key"], r["swap_group"],
                           tuple(r["key_columns"] or ()), tuple(r["writer_stages"] or ()), r["source_key"],
                           r["shadow_column"], r["shadow_space_key"], r["previous_column"], r["previous_space_key"])


async def fetch_registry(conn) -> tuple[dict[str, EmbeddingSpace], dict[str, EmbeddingColumn]]:
    """Both tables on ``conn`` (asyncpg); raises when they are missing (before 328)."""
    spaces = {s.space_key: s for s in map(_space_from_row, await conn.fetch(_SPACES_SQL))}
    columns = {c.qualified: c for c in map(_column_from_row, await conn.fetch(_COLUMNS_SQL))}
    return spaces, columns


async def _fetch_all():
    conn = await connections._connect()
    try:
        return await fetch_registry(conn)
    finally:
        await conn.close()


_spaces: dict[str, EmbeddingSpace] = {}
_columns: dict[str, EmbeddingColumn] = {}
_loaded_at = 0.0
_failed_at = 0.0
_load_lock = threading.Lock()


def invalidate() -> None:
    """Forget the cached registry (tests; an admin action that just changed it)."""
    global _spaces, _columns, _loaded_at, _failed_at
    with _load_lock:
        _spaces, _columns, _loaded_at, _failed_at = {}, {}, 0.0, 0.0


def _fresh(now: float) -> bool:
    return bool((_loaded_at and now - _loaded_at < _CACHE_TTL_SECONDS)
                or (_failed_at and now - _failed_at < _FAILURE_TTL_SECONDS))


def _ensure_loaded() -> None:
    global _spaces, _columns, _loaded_at, _failed_at
    if _fresh(time.monotonic()):
        return
    with _load_lock:
        now = time.monotonic()
        if _fresh(now):
            return
        try:
            spaces, columns = connections.run_sync(_fetch_all)
            if not spaces or not columns:
                raise RuntimeError("embedding space registry is empty")
            _spaces, _columns, _loaded_at, _failed_at = spaces, columns, now, 0.0
        except Exception as e:  # noqa: BLE001 — never raises: before 328, or the DB is down -> the built-in mirror
            _failed_at = now
            logger.warning("embedding_space_registry_read_failed", error=str(e)[:300],
                           hint="built-in mirror of migration 328 used")


def registry_sync() -> tuple[dict[str, EmbeddingSpace], dict[str, EmbeddingColumn]]:
    """(spaces, columns): the loaded registry, else the built-in mirror. Never raises."""
    _ensure_loaded()
    if _spaces and _columns:
        return dict(_spaces), dict(_columns)
    return dict(BUILTIN_SPACES), dict(BUILTIN_COLUMNS)


def column(qualified: str) -> EmbeddingColumn:
    """The registry row of ``schema.table.column``; KeyError for a column that is not a registered vector column."""
    _, columns = registry_sync()
    if qualified in columns:
        return columns[qualified]
    if qualified in BUILTIN_COLUMNS:
        return BUILTIN_COLUMNS[qualified]
    raise KeyError(f"{qualified} is not a registered vector column (shared.embedding_column)")


def space(space_key: str) -> EmbeddingSpace:
    """A space by key; KeyError when it is unknown."""
    spaces, _ = registry_sync()
    if space_key in spaces:
        return spaces[space_key]
    if space_key in BUILTIN_SPACES:
        return BUILTIN_SPACES[space_key]
    raise KeyError(f"unknown embedding space {space_key!r}")


def space_for(qualified: str) -> EmbeddingSpace:
    """The space the vectors of ``qualified`` are in: what a writer must embed with, and what a search on that
    column must embed its query with."""
    return space(column(qualified).space_key)


def ledger_signal(resp) -> dict:
    """The quality_signal fields an embed ledger row adds: the space of the vectors (``embedding_space``)."""
    key = getattr(resp, "space_key", None)
    return {"embedding_space": key} if isinstance(key, str) and key else {}


__all__ = ["ATOM_EMBEDDING", "QUESTION_EMBEDDING", "CONTENT_PIECE_EMBEDDING", "TOUR_PLACE_EMBEDDING",
           "PUBLISHED_TOUR_EMBEDDING", "COHERE_V4_1536", "TITAN_V2_1024", "EmbeddingSpace", "EmbeddingColumn",
           "BUILTIN_SPACES", "BUILTIN_COLUMNS", "fetch_registry", "registry_sync", "column", "space", "space_for",
           "ledger_signal", "invalidate"]
