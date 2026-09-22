"""AA-630 — questions_count cache (migration 163, AA-610 Sub 2) never invalidated when
search_demand.people_also_ask changes for a keyword. Tests invalidate_questions_cache_for_
keyword() (atom_ranking.py) and its call from _store_paa() (segment_research.py).

Pure-mocked-DB tests only — no real DB, no real DFS call."""
from unittest.mock import AsyncMock, patch

import pytest

from services.acp_contract.atom_ranking import invalidate_questions_cache_for_keyword


def _segment_row(segment_id: str, place: str, action: str):
    return {"segment_id": segment_id, "canonical_place": place, "canonical_action": action}


@pytest.mark.asyncio
async def test_invalidate_sets_null_for_claiming_segment():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        _segment_row("seg-1", "Sukhbaatar Square", "visit"),
        _segment_row("seg-2", "Gandan Monastery", "visit"),
    ])
    conn.execute = AsyncMock()

    count = await invalidate_questions_cache_for_keyword(conn, "sukhbaatar square")

    assert count == 1
    conn.execute.assert_called_once()
    sql, stale_ids = conn.execute.call_args.args[0], conn.execute.call_args.args[1]
    assert "SET questions_count = NULL" in sql
    assert stale_ids == ["seg-1"]


@pytest.mark.asyncio
async def test_invalidate_no_claiming_segment_is_a_noop():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        _segment_row("seg-1", "Gandan Monastery", "visit"),
    ])
    conn.execute = AsyncMock()

    count = await invalidate_questions_cache_for_keyword(conn, "sukhbaatar square")

    assert count == 0
    conn.execute.assert_not_called()  # no UPDATE issued when nothing needs invalidating


@pytest.mark.asyncio
async def test_invalidate_only_queries_segments_with_a_cached_value():
    # The SQL itself filters WHERE questions_count IS NOT NULL — a never-computed Segment is
    # already "always recompute" per precompute_question_landings()'s own NULL semantic, so
    # there is nothing for this function to invalidate for it.
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()

    count = await invalidate_questions_cache_for_keyword(conn, "anything")

    assert count == 0
    fetch_sql = conn.fetch.call_args.args[0]
    assert "questions_count IS NOT NULL" in fetch_sql


@pytest.mark.asyncio
async def test_invalidate_multiple_claiming_segments():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        _segment_row("seg-1", "Sukhbaatar Square", "visit"),
        _segment_row("seg-2", "Sukhbaatar Square", "explore"),
        _segment_row("seg-3", "Gandan Monastery", "visit"),
    ])
    conn.execute = AsyncMock()

    count = await invalidate_questions_cache_for_keyword(conn, "sukhbaatar square")

    assert count == 2
    stale_ids = conn.execute.call_args.args[1]
    assert set(stale_ids) == {"seg-1", "seg-2"}


# ── _store_paa() calls the invalidator, best-effort ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_paa_invalidates_cache_after_writing_paa():
    from services.acp_contract.segment_research import _store_paa

    conn = AsyncMock()
    conn.execute = AsyncMock()

    with patch("services.acp_contract.atom_ranking.invalidate_questions_cache_for_keyword",
               new=AsyncMock(return_value=1)) as m_invalidate:
        await _store_paa(conn, "sukhbaatar square", "US", ["What is Sukhbaatar Square"])

    m_invalidate.assert_awaited_once_with(conn, "sukhbaatar square")


@pytest.mark.asyncio
async def test_store_paa_skips_invalidation_when_no_questions():
    # _store_paa() already early-returns on empty questions (nothing written to search_demand
    # either) — the invalidator must not be reached in that case.
    from services.acp_contract.segment_research import _store_paa

    conn = AsyncMock()
    conn.execute = AsyncMock()

    with patch("services.acp_contract.atom_ranking.invalidate_questions_cache_for_keyword",
               new=AsyncMock()) as m_invalidate:
        await _store_paa(conn, "sukhbaatar square", "US", [])

    m_invalidate.assert_not_awaited()
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_store_paa_invalidation_failure_does_not_raise():
    # A failure invalidating the cache must never break the harvest write itself — the fresh
    # PAA is already committed to search_demand by the time this runs.
    from services.acp_contract.segment_research import _store_paa

    conn = AsyncMock()
    conn.execute = AsyncMock()

    with patch("services.acp_contract.atom_ranking.invalidate_questions_cache_for_keyword",
               new=AsyncMock(side_effect=RuntimeError("db down"))):
        await _store_paa(conn, "sukhbaatar square", "US", ["A question"])

    conn.execute.assert_called_once()  # the search_demand UPDATE still happened
