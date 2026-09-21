"""AA-610 (Sub 2): services.acp_contract.atom_matching — PAA question landing by embedding-match,
with soft-fall-back to the pre-Sub-2 claim-by-name test. Mocks compute_embedding()/DB — no real
Bedrock call, no real DB, matching this repo's existing convention for LLM-adjacent unit tests
(e.g. test_aa508_atom_content_hash.py mocking invoke_claude)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract.atom_matching import (
    claim_by_name_fallback,
    ensure_atom_embeddings,
    land_question_on_atom,
)


# ── claim_by_name_fallback (pure, no DB/LLM) ────────────────────────────────────────────────


def test_claim_by_name_fallback_finds_shared_word():
    candidates = [
        ("atom-1", "Sukhbaatar Square", "visit the site"),
        ("atom-2", "Gandan Monastery", "tour the monastery"),
    ]
    assert claim_by_name_fallback("What is Sukhbaatar Square known for", candidates) == "atom-1"


def test_claim_by_name_fallback_no_match_returns_none():
    candidates = [("atom-1", "Sukhbaatar Square", "visit the site")]
    assert claim_by_name_fallback("unrelated question about nothing", candidates) is None


def test_claim_by_name_fallback_empty_candidates_returns_none():
    assert claim_by_name_fallback("any question", []) is None


# ── land_question_on_atom (mocked embedding + DB) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_land_question_on_atom_no_candidates_falls_back_to_tokens():
    conn = AsyncMock()
    atom_id, distance, matched_by = await land_question_on_atom(conn, "any question", [])
    assert (atom_id, distance, matched_by) == (None, None, "tokens")
    conn.fetchrow.assert_not_called()  # never even tries the DB with an empty candidate list


@pytest.mark.asyncio
async def test_land_question_on_atom_embedding_failure_falls_back_to_tokens():
    with patch("services.acp_contract.atom_matching.compute_embedding", return_value=None):
        conn = AsyncMock()
        atom_id, distance, matched_by = await land_question_on_atom(
            conn, "any question", ["atom-1", "atom-2"],
        )
    assert (atom_id, distance, matched_by) == (None, None, "tokens")
    conn.fetchrow.assert_not_called()  # no embedding, no point querying the vector column


@pytest.mark.asyncio
async def test_land_question_on_atom_real_vector_match():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"atom_id": "atom-2", "distance": 0.13})
    with patch("services.acp_contract.atom_matching.compute_embedding", return_value=[0.1] * 1536):
        atom_id, distance, matched_by = await land_question_on_atom(
            conn, "What is Gandan Monastery", ["atom-1", "atom-2"],
        )
    assert atom_id == "atom-2"
    assert distance == 0.13
    assert matched_by == "vector"


@pytest.mark.asyncio
async def test_land_question_on_atom_no_embedded_candidates_falls_back_to_tokens():
    # Embedding call succeeds for the query, but every candidate atom lacks its own embedding
    # (ensure_atom_embeddings() never ran, or every call failed) — fetchrow finds nothing.
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    with patch("services.acp_contract.atom_matching.compute_embedding", return_value=[0.1] * 1536):
        atom_id, distance, matched_by = await land_question_on_atom(
            conn, "any question", ["atom-1"],
        )
    assert (atom_id, distance, matched_by) == (None, None, "tokens")


# ── ensure_atom_embeddings (mocked embedding + DB) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_atom_embeddings_skips_already_embedded_views():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"view": "place"}, {"view": "place_action"}])
    with patch("services.acp_contract.atom_matching.compute_embedding") as m_embed:
        ok = await ensure_atom_embeddings(conn, "atom-1", "Sukhbaatar Square", "visit")
    assert ok is True
    m_embed.assert_not_called()  # both views already had rows — no embedding call needed
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_atom_embeddings_writes_missing_views():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])  # neither view embedded yet
    conn.execute = AsyncMock()
    with patch("services.acp_contract.atom_matching.compute_embedding", return_value=[0.1] * 1536):
        ok = await ensure_atom_embeddings(conn, "atom-1", "Sukhbaatar Square", "visit")
    assert ok is True
    assert conn.execute.call_count == 2  # one INSERT per view (place, place_action)


@pytest.mark.asyncio
async def test_ensure_atom_embeddings_soft_fails_on_embedding_error():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    with patch("services.acp_contract.atom_matching.compute_embedding", return_value=None):
        ok = await ensure_atom_embeddings(conn, "atom-1", "Sukhbaatar Square", "visit")
    assert ok is False  # caller should know to fall back to claim-by-name for this atom
    conn.execute.assert_not_called()  # never writes a row for a failed embedding call
