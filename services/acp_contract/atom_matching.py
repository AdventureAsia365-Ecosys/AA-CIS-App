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

**Soft-fail, matching services/acp_shared/content_embedding.py's own contract**: if
`compute_embedding()` returns None (any Bedrock failure), this module falls back to the existing
claim-by-name test (`ranking_reference.claimable_words()`/`keyword_words()`) rather than raising
— a Bedrock outage must never block ranking. The fallback's rows are written with
`matched_by='tokens'`, so which mechanism actually produced a given match is visible in the
data, not silently indistinguishable from a real vector match.
"""
from __future__ import annotations

import hashlib

import structlog

from services.acp_contract.ranking_reference import claimable_words, keyword_words
from services.acp_shared.content_embedding import compute_embedding, embedding_to_pgvector_literal

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


async def land_question_on_atom(
    conn, query: str, candidate_atom_ids: list[str],
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
    content_embedding.py already has)."""
    if not candidate_atom_ids:
        return None, None, "tokens"

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
    "ensure_atom_embeddings", "land_question_on_atom", "claim_by_name_fallback",
]
