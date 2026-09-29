"""services/acp_contract/atom_matching.py — AA-610 (Sub 2), embedding-match landing for PAA
questions.

Ports Ms. Thư's `match_queries_to_atoms()` (aa-social-media src/aa_social/matching.py, ADR-0002/
0018): a PAA question is embedded and landed on the nearest Atom by cosine distance (k=1, no
threshold — a Segment either owns the nearest atom or it doesn't), then the match is fanned out
to every atom in that atom's Segment (`atom_ranking.py::compute_questions()`'s caller does the
fan-out, this module only finds the k=1 landing and records it).

**Two views per atom** (ADR-0018's own design, kept as-is): "place" alone, and "place + action" —
a query lands wherever it's closer, without blending the two into one vector. Embeddings are
written once per atom (idempotent upsert keyed on (atom_id, view)) and read many times as new
PAA questions arrive — "research pays the embedding cost once, score never pays it again"
(ADR-0018's own phrase, restated in atom_ranking.py's module docstring for `_demand()`/
`_questions()`'s word-overlap adaptation this migration starts to close).

**AA-610 redesign — question embeddings are cached by text hash** (`acp_contract.
question_embedding`, migration 162): the first live test found the SAME question text getting
re-embedded once per Segment that shortlists it (and once per market, before the market-loop was
also closed) — the question's own embedding never changes, so `_embed_question_cached()` looks
it up before ever calling `compute_embedding()`, at most one real Cohere Embed v4 call per
distinct question text, ever.

**AA-688 — batch paths.** `ensure_atom_embeddings_batch()` and `embed_questions_cached()` embed
everything a ranking run is missing in calls of up to 96 texts (`compute_embeddings()`), instead
of one paced 3.5 s call per text. On Dev (29/09/2026) a single a3_atomize job had ~16,000 atom
views and ~1,500 questions left to embed one at a time — about 16 hours; batched, it is roughly
200 calls. The one-at-a-time functions below stay for single-item callers.

**Soft-fail, matching services/acp_shared/content_embedding.py's own contract**: if
`compute_embedding()` returns None (any Bedrock failure), this module falls back to the existing
claim-by-name test (`ranking_reference.claimable_words()`/`keyword_words()`) rather than raising
— a Bedrock outage must never block ranking. The fallback's rows are written with
`matched_by='tokens'`, so which mechanism actually produced a given match is visible in the
data, not silently indistinguishable from a real vector match.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Callable, Iterable, Optional

import structlog

from services.acp_contract.ranking_reference import claimable_words, keyword_words
from services.acp_shared.content_embedding import (
    MAX_TEXTS_PER_CALL, compute_embedding, compute_embeddings, embedding_to_pgvector_literal,
)

logger = structlog.get_logger()

_VIEWS = ("place", "place_action")


def _question_hash(question: str) -> str:
    """Same normalise-then-hash shape this codebase already uses for atom_id/segment_id
    (atom_extraction.normalise() + content_hash_atom_id()) — lowercase, collapsed whitespace,
    so two questions differing only in case/spacing share one cache row and one embedding call."""
    normalised = " ".join(question.lower().split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


async def _embed_question_cached(conn, question: str) -> list[float] | None:
    """Looks up `acp_contract.question_embedding` by this question's text hash before ever
    calling `compute_embedding()` — a real Cohere Embed v4 call happens for a given question
    text at most ONCE, ever, no matter how many Segments/markets/ranking runs shortlist it
    afterward. Returns None (soft-fail, same contract as compute_embedding() itself) if this
    question has never been embedded AND a fresh embedding call also fails."""
    q_hash = _question_hash(question)
    row = await conn.fetchrow(
        "SELECT embedding FROM acp_contract.question_embedding WHERE question_hash = $1", q_hash,
    )
    if row is not None:
        # asyncpg has no built-in vector codec (content_embedding.py's own comment) — pgvector
        # returns its text literal '[0.1,0.2,...]' for a plain SELECT, parsed back to floats here
        # the same way embedding_to_pgvector_literal() serialises the other direction.
        return [float(x) for x in row["embedding"].strip("[]").split(",")]

    vector = compute_embedding(question)
    if vector is None:
        return None
    literal = embedding_to_pgvector_literal(vector)
    await conn.execute(
        """
        INSERT INTO acp_contract.question_embedding (question_hash, question_text, embedding)
        VALUES ($1, $2, $3::vector)
        ON CONFLICT (question_hash) DO NOTHING
        """,
        q_hash, question, literal,
    )
    return vector


def _parse_vector(literal: str) -> list[float]:
    return [float(x) for x in literal.strip("[]").split(",")]


async def embed_questions_cached(
    conn, questions: Iterable[str], progress: Optional[Callable[[dict], None]] = None,
) -> dict[str, list[float]]:
    """AA-688 — batch form of `_embed_question_cached()`: one cache read for every question, then
    the misses embedded `MAX_TEXTS_PER_CALL` at a time, each batch written to the cache as soon as
    it returns (a killed job keeps what it already paid for). Returns question -> vector for every
    question that has one; a question missing from the result had its embedding call fail
    (soft-fail — the caller falls back to claim-by-name for it)."""
    by_hash: dict[str, list[str]] = {}
    for q in questions:
        by_hash.setdefault(_question_hash(q), []).append(q)
    if not by_hash:
        return {}

    rows = await conn.fetch(
        "SELECT question_hash, embedding FROM acp_contract.question_embedding "
        "WHERE question_hash = ANY($1::text[])",
        list(by_hash),
    )
    vectors: dict[str, list[float]] = {r["question_hash"]: _parse_vector(r["embedding"]) for r in rows}
    missing = [h for h in by_hash if h not in vectors]

    for start in range(0, len(missing), MAX_TEXTS_PER_CALL):
        batch = missing[start:start + MAX_TEXTS_PER_CALL]
        texts = [by_hash[h][0] for h in batch]
        embedded = await asyncio.to_thread(compute_embeddings, texts)
        new_rows = []
        for h, text, vector in zip(batch, texts, embedded):
            if vector is None:
                continue
            vectors[h] = vector
            new_rows.append((h, text, embedding_to_pgvector_literal(vector)))
        if new_rows:
            await conn.executemany(
                """
                INSERT INTO acp_contract.question_embedding (question_hash, question_text, embedding)
                VALUES ($1, $2, $3::vector)
                ON CONFLICT (question_hash) DO NOTHING
                """,
                new_rows,
            )
        if progress:
            progress({"step": "embedding_questions",
                      "done": min(start + len(batch), len(missing)), "total": len(missing)})

    return {q: vectors[h] for h, qs in by_hash.items() if h in vectors for q in qs}


def _view_text(place: str, action: str, view: str) -> str:
    if view == "place":
        return place or ""
    return f"{place} {action}".strip() if place or action else ""


async def ensure_atom_embeddings(conn, atom_id: str, place: str, action: str) -> bool:
    """Writes both views' embeddings for one atom if they don't already exist — called once per
    atom (from `run_atom_ranking()`'s caller, before questions are landed), never re-embeds an
    atom that already has both rows. Returns True if both views ended up embedded (whether just
    now or already), False if at least one view's embedding call failed (soft-fail — caller
    should fall back to claim-by-name for this atom, not retry inline)."""
    existing = await conn.fetch(
        "SELECT view FROM acp_contract.atom_embedding WHERE atom_id = $1", atom_id,
    )
    have = {r["view"] for r in existing}
    missing = [v for v in _VIEWS if v not in have]
    if not missing:
        return True

    all_ok = True
    for view in missing:
        text = _view_text(place, action, view)
        if not text.strip():
            all_ok = False
            continue
        vector = compute_embedding(text)
        if vector is None:
            logger.warning("atom_embedding_failed", atom_id=atom_id, view=view)
            all_ok = False
            continue
        literal = embedding_to_pgvector_literal(vector)
        await conn.execute(
            """
            INSERT INTO acp_contract.atom_embedding (atom_id, view, embedding)
            VALUES ($1, $2, $3::vector)
            ON CONFLICT (atom_id, view) DO NOTHING
            """,
            atom_id, view, literal,
        )
    return all_ok


async def ensure_atom_embeddings_batch(
    conn, atoms: list[tuple[str, str, str]], progress: Optional[Callable[[dict], None]] = None,
) -> int:
    """AA-688 — batch form of `ensure_atom_embeddings()` for many atoms at once. `atoms` is a list
    of (atom_id, place, action). Reads which (atom_id, view) rows already exist in one query,
    embeds only the missing views `MAX_TEXTS_PER_CALL` at a time, and writes each batch as soon as
    it returns. Returns how many view embeddings were written. A view whose call failed is left
    missing (soft-fail — `land_question_on_atom()` then finds no vector for that atom and the
    caller falls back to claim-by-name, same as the single-atom path)."""
    if not atoms:
        return 0
    existing = await conn.fetch(
        "SELECT atom_id, view FROM acp_contract.atom_embedding WHERE atom_id = ANY($1::text[])",
        [a[0] for a in atoms],
    )
    have = {(r["atom_id"], r["view"]) for r in existing}
    todo: list[tuple[str, str, str]] = []  # (atom_id, view, text)
    seen: set[tuple[str, str]] = set()
    for atom_id, place, action in atoms:
        for view in _VIEWS:
            key = (atom_id, view)
            if key in have or key in seen:
                continue
            seen.add(key)
            text = _view_text(place, action, view)
            if text.strip():
                todo.append((atom_id, view, text))

    written = 0
    for start in range(0, len(todo), MAX_TEXTS_PER_CALL):
        batch = todo[start:start + MAX_TEXTS_PER_CALL]
        embedded = await asyncio.to_thread(compute_embeddings, [t for _, _, t in batch])
        new_rows = [
            (atom_id, view, embedding_to_pgvector_literal(vector))
            for (atom_id, view, _), vector in zip(batch, embedded) if vector is not None
        ]
        if len(new_rows) < len(batch):
            logger.warning("atom_embedding_batch_partial", embedded=len(new_rows), requested=len(batch))
        if new_rows:
            await conn.executemany(
                """
                INSERT INTO acp_contract.atom_embedding (atom_id, view, embedding)
                VALUES ($1, $2, $3::vector)
                ON CONFLICT (atom_id, view) DO NOTHING
                """,
                new_rows,
            )
            written += len(new_rows)
        if progress:
            progress({"step": "embedding_atoms",
                      "done": min(start + len(batch), len(todo)), "total": len(todo)})
    return written


async def land_question_on_atom(
    conn, query: str, candidate_atom_ids: list[str], vector: Optional[list[float]] = None,
) -> tuple[str | None, float | None, str]:
    """Finds the nearest atom (by either view's embedding) among `candidate_atom_ids` for one PAA
    question — restricted to a caller-supplied candidate list (every atom currently in the
    platform's Segment pool) rather than a global nearest-neighbour search, since a question only
    means anything landing on an atom that's actually part of ranking right now.

    `query`'s own embedding comes from `_embed_question_cached()` — a cache hit if this exact
    question text has ever been embedded before (any Segment, any market, any prior run), a
    real Cohere Embed v4 call only on the very first time.

    Returns (atom_id, distance, matched_by) — atom_id is None if `query` has no embedding
    (soft-fail) or no candidate has an embedding either (the caller then falls back to
    claim-by-name, same as `compute_questions()`'s pre-Sub-2 behaviour). matched_by is 'vector'
    when the embedding path produced a real nearest-neighbour result (distance is the real
    cosine distance, in [0, 2]), 'tokens' when it fell back (distance is None) — Bedrock
    failure, or query too short to embed meaningfully (same _MAX_INPUT_CHARS-guarded soft-fail
    content_embedding.py already has).

    AA-688 — `vector`, when given, is the question's embedding already fetched in bulk by
    `embed_questions_cached()`; the per-question cache lookup and embedding call are skipped."""
    if not candidate_atom_ids:
        return None, None, "tokens"

    if vector is None:
        vector = await _embed_question_cached(conn, query)
    if vector is not None:
        literal = embedding_to_pgvector_literal(vector)
        row = await conn.fetchrow(
            """
            SELECT ae.atom_id, ae.embedding <=> $1::vector AS distance
            FROM acp_contract.atom_embedding ae
            WHERE ae.atom_id = ANY($2::text[])
            ORDER BY ae.embedding <=> $1::vector
            LIMIT 1
            """,
            literal, candidate_atom_ids,
        )
        if row is not None:
            return row["atom_id"], float(row["distance"]), "vector"
        # Every candidate lacks an embedding (ensure_atom_embeddings() never ran for them, or
        # every call failed) — fall through to claim-by-name rather than reporting no match.

    return None, None, "tokens"


def claim_by_name_fallback(
    query: str, candidates: list[tuple[str, str, str]],
) -> str | None:
    """The pre-Sub-2 mechanism (`atom_ranking.py::compute_questions()`'s own word-overlap test),
    kept verbatim as the fallback path — `candidates` is a list of (atom_id, place, action).
    Returns the first candidate the query shares a claimable word with, or None."""
    query_words = keyword_words(query)
    for atom_id, place, action in candidates:
        shared = query_words & claimable_words(place, action)
        if shared:
            return atom_id
    return None


__all__ = [
    "ensure_atom_embeddings", "ensure_atom_embeddings_batch", "embed_questions_cached",
    "land_question_on_atom", "claim_by_name_fallback",
]
