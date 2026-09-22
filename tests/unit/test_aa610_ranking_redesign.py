"""AA-610 (Sub 2 redesign): question-embedding cache, PAA-landing market-independence, and the
per-Segment candidate cap. A live re-atomize test found land_questions_for_segment() never
completing within several minutes once real volume (many PAA candidates, run once per market)
hit it — this file tests the three fixes for that, each in isolation.

Pure-function/mocked-DB tests only — no real Bedrock call, no real DB."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract.atom_matching import _embed_question_cached, _question_hash
from services.acp_contract.atom_ranking import (
    _MAX_QUESTION_CANDIDATES_PER_SEGMENT,
    land_questions_for_segment,
    precompute_question_landings,
)


# ── _embed_question_cached (question-embedding cache, migration 162) ───────────────────────


@pytest.mark.asyncio
async def test_embed_question_cached_hit_never_calls_compute_embedding():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"embedding": "[0.1,0.2,0.3]"})
    with patch("services.acp_contract.atom_matching.compute_embedding") as m_embed:
        vector = await _embed_question_cached(conn, "What is Gandan Monastery")
    m_embed.assert_not_called()  # cache hit — no real Cohere Embed v4 call
    assert vector == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_question_cached_miss_calls_compute_embedding_and_writes_cache():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    with patch("services.acp_contract.atom_matching.compute_embedding",
               return_value=[0.1] * 1536) as m_embed:
        vector = await _embed_question_cached(conn, "What is Gandan Monastery")
    m_embed.assert_called_once_with("What is Gandan Monastery")
    assert vector == [0.1] * 1536
    conn.execute.assert_called_once()  # cache write
    insert_sql = conn.execute.call_args.args[0]
    assert "INSERT INTO acp_contract.question_embedding" in insert_sql


@pytest.mark.asyncio
async def test_embed_question_cached_miss_and_embedding_failure_writes_nothing():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    with patch("services.acp_contract.atom_matching.compute_embedding", return_value=None):
        vector = await _embed_question_cached(conn, "unembeddable")
    assert vector is None
    conn.execute.assert_not_called()  # nothing to cache on a failed embedding call


def test_question_hash_case_and_whitespace_insensitive():
    # Two questions differing only in case/spacing must share one cache row.
    assert _question_hash("What is Gandan Monastery") == _question_hash("what   is gandan monastery")
    assert _question_hash("What is Gandan Monastery") != _question_hash("What is Sukhbaatar Square")


# ── _MAX_QUESTION_CANDIDATES_PER_SEGMENT cap ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_land_questions_for_segment_caps_candidates_before_landing():
    """A Segment whose shortlist exceeds the cap must only pay a land_question_on_atom() call
    for the capped subset — not for every candidate the (uncapped) keyword shortlist found."""
    many_questions = [f"Is this a real question about place number {i}" for i in range(50)]
    paa_rows = [("sukhbaatar square", "US", many_questions)]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"atom_id": "atom-1", "place": "Sukhbaatar Square", "action": "visit"}])
    conn.execute = AsyncMock()

    landed_calls = []

    async def fake_land(_conn, question, _atom_ids):
        landed_calls.append(question)
        return None, None, "tokens"  # forces fallback path, which claim_by_name_fallback below handles

    with patch("services.acp_contract.atom_ranking.ensure_atom_embeddings", AsyncMock(return_value=True)), \
         patch("services.acp_contract.atom_ranking.land_question_on_atom", fake_land), \
         patch("services.acp_contract.atom_ranking.claim_by_name_fallback", return_value="atom-1"):
        await land_questions_for_segment(
            conn, "Sukhbaatar Square", "visit the revolution site", ["atom-1"], paa_rows,
        )

    assert len(landed_calls) == _MAX_QUESTION_CANDIDATES_PER_SEGMENT


@pytest.mark.asyncio
async def test_land_questions_for_segment_under_cap_lands_every_candidate():
    paa_rows = [("sukhbaatar square", "US", ["Is Sukhbaatar Square free", "What is Sukhbaatar Square"])]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"atom_id": "atom-1", "place": "Sukhbaatar Square", "action": "visit"}])
    conn.execute = AsyncMock()

    landed_calls = []

    async def fake_land(_conn, question, _atom_ids):
        landed_calls.append(question)
        return "atom-1", 0.1, "vector"

    with patch("services.acp_contract.atom_ranking.ensure_atom_embeddings", AsyncMock(return_value=True)), \
         patch("services.acp_contract.atom_ranking.land_question_on_atom", fake_land):
        count = await land_questions_for_segment(
            conn, "Sukhbaatar Square", "visit the revolution site", ["atom-1"], paa_rows,
        )

    assert len(landed_calls) == 2
    assert count == 2


# ── precompute_question_landings (market-independent, runs once) ───────────────────────────


@pytest.mark.asyncio
async def test_precompute_question_landings_runs_once_not_per_market():
    """The whole point of this function: land_questions_for_segment() gets called exactly once
    per Segment, period — not once per (Segment, market) the way it used to when this logic
    lived inside run_atom_ranking()'s own per-market loop."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    conn.fetch = AsyncMock(side_effect=[
        [  # segment_rows
            {"segment_id": "seg-1", "canonical_place": "Sukhbaatar Square",
             "canonical_action": "visit", "questions_count": None, "atom_ids": ["atom-1"]},
            {"segment_id": "seg-2", "canonical_place": "arrive at the airport",
             "canonical_action": "arrive", "questions_count": None,
             "atom_ids": ["atom-2"]},  # excluded: transit
        ],
        [],  # demand_rows (people_also_ask) — empty is fine, no candidates either way
    ])
    conn.executemany = AsyncMock()

    with patch("services.acp_contract.atom_ranking.land_questions_for_segment",
               AsyncMock(return_value=3)) as m_land:
        counts = await precompute_question_landings(pool)

    # Excluded Segment (seg-2, transit action) never gets landed at all.
    m_land.assert_awaited_once()
    assert counts == {"seg-1": 3}
    conn.executemany.assert_awaited_once()  # cache write for the one recomputed Segment


@pytest.mark.asyncio
async def test_precompute_question_landings_excludes_transit_and_unnamed_place():
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    conn.fetch = AsyncMock(side_effect=[
        [
            {"segment_id": "seg-transit", "canonical_place": "Kyoto Station",
             "canonical_action": "arrive at the station", "questions_count": None,
             "atom_ids": ["atom-1"]},
            {"segment_id": "seg-unnamed", "canonical_place": "a nearby hot spring",
             "canonical_action": "relax", "questions_count": None, "atom_ids": ["atom-2"]},
        ],
        [],
    ])
    conn.executemany = AsyncMock()

    with patch("services.acp_contract.atom_ranking.land_questions_for_segment",
               AsyncMock(return_value=5)) as m_land:
        counts = await precompute_question_landings(pool)

    m_land.assert_not_awaited()  # both segments excluded — no landing work for either
    assert counts == {}
    conn.executemany.assert_not_awaited()  # nothing recomputed, nothing to cache


# ── segment_ids scope (AA-610 Sub 2 scope fix) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_precompute_question_landings_out_of_scope_segment_reads_cache_not_relanded():
    """A Segment outside segment_ids, with an already-cached questions_count, must be served
    that cached value — land_questions_for_segment() must never be called for it. This is the
    fix for the live re-test finding a single tour-triggered recompute re-landing PAA questions
    for every OTHER platform Segment too."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    conn.fetch = AsyncMock(side_effect=[
        [
            {"segment_id": "seg-in-scope", "canonical_place": "Sukhbaatar Square",
             "canonical_action": "visit", "questions_count": None, "atom_ids": ["atom-1"]},
            {"segment_id": "seg-out-of-scope", "canonical_place": "Gandan Monastery",
             "canonical_action": "visit", "questions_count": 7, "atom_ids": ["atom-2"]},
        ],
        [],
    ])
    conn.executemany = AsyncMock()

    with patch("services.acp_contract.atom_ranking.land_questions_for_segment",
               AsyncMock(return_value=3)) as m_land:
        counts = await precompute_question_landings(pool, segment_ids={"seg-in-scope"})

    m_land.assert_awaited_once()  # only the in-scope Segment gets relanded
    assert counts == {"seg-in-scope": 3, "seg-out-of-scope": 7}  # out-of-scope served from cache
    # Cache write only covers the recomputed Segment, never the one served from cache.
    written_rows = conn.executemany.call_args.args[1]
    written_ids = [segment_id for segment_id, _count in written_rows]
    assert written_ids == ["seg-in-scope"]


@pytest.mark.asyncio
async def test_precompute_question_landings_never_caches_none_regardless_of_scope():
    """A Segment with questions_count IS NULL (never computed — brand new) must always be
    recomputed this call, even if segment_ids does not name it — it has no cache value to
    serve, so serving one would be a crash (KeyError) or a silent wrong 0, not a real skip."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    conn.fetch = AsyncMock(side_effect=[
        [
            {"segment_id": "seg-brand-new", "canonical_place": "Erdene Zuu Monastery",
             "canonical_action": "visit", "questions_count": None, "atom_ids": ["atom-3"]},
        ],
        [],
    ])
    conn.executemany = AsyncMock()

    with patch("services.acp_contract.atom_ranking.land_questions_for_segment",
               AsyncMock(return_value=1)) as m_land:
        # segment_ids names some OTHER Segment entirely — seg-brand-new is not in it.
        counts = await precompute_question_landings(pool, segment_ids={"some-other-segment"})

    m_land.assert_awaited_once()  # recomputed anyway — never-computed always wins the scope check
    assert counts == {"seg-brand-new": 1}


@pytest.mark.asyncio
async def test_precompute_question_landings_segment_ids_none_recomputes_everything():
    """segment_ids=None (the default) keeps the ORIGINAL platform-wide behavior — every Segment
    recomputed, even ones with an existing cache value."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    conn.fetch = AsyncMock(side_effect=[
        [
            {"segment_id": "seg-1", "canonical_place": "Sukhbaatar Square",
             "canonical_action": "visit", "questions_count": 7, "atom_ids": ["atom-1"]},
            {"segment_id": "seg-2", "canonical_place": "Gandan Monastery",
             "canonical_action": "visit", "questions_count": 9, "atom_ids": ["atom-2"]},
        ],
        [],
    ])
    conn.executemany = AsyncMock()

    with patch("services.acp_contract.atom_ranking.land_questions_for_segment",
               AsyncMock(return_value=3)) as m_land:
        counts = await precompute_question_landings(pool)

    assert m_land.await_count == 2  # both recomputed — segment_ids=None ignores any cache value
    assert counts == {"seg-1": 3, "seg-2": 3}
