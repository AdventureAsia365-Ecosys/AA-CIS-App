"""AA-1026 D5 — the re-embed text source of every registered vector column (``shared.embedding_column.source_key``).

A source rebuilds, for one row of the vector column's table, the exact text its writer embedded:

- ``atom_view``           acp_contract.atom_embedding: the atom's place ("place" view) or "place action"
                          ("place_action" view), from acp_contract.tour_atoms (atom_matching._view_text);
- ``question_text``       acp_contract.question_embedding: question_text (atom_matching._embed_question_cached);
- ``content_piece_text``  acp_shared.content_piece: content_text without citation tags
                          (acp_content_writing.service, compute_embedding(strip_citation_tags(...)));
- ``tour_place_text``     geo.tour_place: text_extract (places.pipeline).

``select_sql`` is the keyset page the re-embed reads: rows that have a vector in the serving column and none yet in
the shadow (a resumed run picks up where the last one stopped), after the last key of this run (so a row whose text is
blank cannot be read again in a loop), in key order. The SQL is built only from the registry row's identifiers
(quoted) and these fixed fragments, never from a payload string.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from shared.llm_client.embedding_space import EmbeddingColumn

# The writers' own input cap (content_embedding._MAX_INPUT_CHARS).
MAX_INPUT_CHARS = 25000


def _strip_citations(text: str) -> str:
    from services.acp_content_writing.quality_gates import strip_citation_tags
    return strip_citation_tags(text)


@dataclass(frozen=True)
class TextSource:
    key: str
    table: str                  # schema.table the source reads (must equal the registry row's)
    text_sql: str               # the text expression over alias t (and the joins)
    joins: str = ""
    transform: Optional[Callable[[str], str]] = None

    def text(self, raw: Optional[str]) -> str:
        out = raw or ""
        if self.transform is not None and out:
            out = self.transform(out)
        return out.strip()[:MAX_INPUT_CHARS]


SOURCES: dict[str, TextSource] = {s.key: s for s in (
    TextSource("atom_view", "acp_contract.atom_embedding",
               "CASE t.view WHEN 'place' THEN coalesce(a.place, '') "
               "ELSE btrim(coalesce(a.place, '') || ' ' || coalesce(a.action, '')) END",
               "LEFT JOIN acp_contract.tour_atoms a ON a.atom_id = t.atom_id"),
    TextSource("question_text", "acp_contract.question_embedding", "t.question_text"),
    TextSource("content_piece_text", "acp_shared.content_piece", "t.content_text", transform=_strip_citations),
    TextSource("tour_place_text", "geo.tour_place", "t.text_extract"),
)}


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def source_for(col: EmbeddingColumn) -> TextSource:
    """The column's text source; ValueError when it has none (cannot be re-embedded) or the source reads another
    table (a registry row edited by hand)."""
    if not col.source_key:
        raise ValueError(f"{col.qualified} has no re-embed text source (shared.embedding_column.source_key)")
    src = SOURCES.get(col.source_key)
    if src is None:
        raise ValueError(f"{col.qualified}: unknown text source {col.source_key!r}")
    if src.table != col.table:
        raise ValueError(f"{col.qualified}: source {src.key} reads {src.table}, not {col.table}")
    return src


def _key_list(col: EmbeddingColumn) -> str:
    return ", ".join(f"t.{_q(k)}" for k in col.key_columns)


def select_sql(col: EmbeddingColumn, target: str, *, after: bool) -> str:
    """The next page: keys AS k0.., the source text AS text. ``target`` is the column filled (the shadow). Params:
    $1 = page size, then (when ``after``) one per key column = the last key of the previous page."""
    src = source_for(col)
    keys = [f"t.{_q(k)} AS k{i}" for i, k in enumerate(col.key_columns)]
    where = [f"t.{_q(col.column_name)} IS NOT NULL", f"t.{_q(target)} IS NULL"]
    if after:
        params = ", ".join(f"${i + 2}" for i in range(len(col.key_columns)))
        where.append(f"({_key_list(col)}) > ({params})")
    return (f"SELECT {', '.join(keys)}, {src.text_sql} AS text "
            f"FROM {_q(col.table_schema)}.{_q(col.table_name)} t {src.joins} "
            f"WHERE {' AND '.join(where)} ORDER BY {_key_list(col)} LIMIT $1")


def estimate_sql(col: EmbeddingColumn, target: Optional[str]) -> str:
    """Rows still to embed and their text length (the dry run). Without a shadow (not prepared yet) every row that has
    a vector counts."""
    src = source_for(col)
    where = [f"t.{_q(col.column_name)} IS NOT NULL"]
    if target:
        where.append(f"t.{_q(target)} IS NULL")
    return (f"SELECT count(*)::bigint AS n_rows, "
            f"count(*) FILTER (WHERE length(btrim({src.text_sql})) = 0)::bigint AS n_blank, "
            f"coalesce(sum(least(length({src.text_sql}), {MAX_INPUT_CHARS})), 0)::bigint AS n_chars "
            f"FROM {_q(col.table_schema)}.{_q(col.table_name)} t {src.joins} WHERE {' AND '.join(where)}")


def update_sql(col: EmbeddingColumn, target: str, cast: str) -> str:
    """One row's new vector: $1 = the vector literal, $2.. = its key. ``cast`` is the target column's type."""
    keys = " AND ".join(f"{_q(k)} = ${i + 2}" for i, k in enumerate(col.key_columns))
    return (f"UPDATE {_q(col.table_schema)}.{_q(col.table_name)} SET {_q(target)} = $1::{cast} "
            f"WHERE {keys} AND {_q(target)} IS NULL")


__all__ = ["MAX_INPUT_CHARS", "SOURCES", "TextSource", "source_for", "select_sql", "estimate_sql", "update_sql"]
