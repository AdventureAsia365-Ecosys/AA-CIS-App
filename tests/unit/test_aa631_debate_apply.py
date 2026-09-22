"""AA-631 — services/acp_shared/debate.py::apply_debate() and its helpers. Pure-mocked-DB/LLM
tests only — no live DB, no live LLM call."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_shared.slate import Candidate


def _seg_candidate(segment_id="seg-1", place="Sukhbaatar Square", action="visit", score=1.0):
    return Candidate(
        segment_id=segment_id, route_id=None, score=score, demand=1000,
        questions=3, said=200, place=place, action=action,
    )


def _route_candidate(route_id="route-1", hub_name="Ulaanbaatar", score=1.0):
    return Candidate(
        segment_id=None, route_id=route_id, score=score, demand=1000,
        questions=3, said=200, hub_name=hub_name,
    )


# ── apply_debate() — empty input / no-op paths ──────────────────────────────

@pytest.mark.asyncio
async def test_apply_debate_empty_candidates_returns_empty():
    from services.acp_shared.debate import apply_debate
    conn = AsyncMock()
    assert await apply_debate(uuid4(), [], conn) == []


@pytest.mark.asyncio
async def test_apply_debate_no_brand_profile_and_no_contested_passes_everyone():
    from services.acp_shared.debate import apply_debate
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"contested": None})  # never computed

    with patch("services.acp_shared.debate._fetch_brand_profile", new=AsyncMock(return_value=None)):
        candidates = [_seg_candidate("seg-1"), _seg_candidate("seg-2")]
        out = await apply_debate(uuid4(), candidates, conn)

    assert out == candidates  # nothing cut


# ── contested cut ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_debate_cuts_highly_contested_segment():
    """With a large enough candidate pool (so the MAX_CUT_FRACTION=0.5 safety rail has room to
    cut at least one), a single highly-contested Segment among otherwise-clean ones IS cut."""
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()

    async def fake_fetchrow(sql, segment_id=None):
        if segment_id == "seg-bad":
            return {"contested": 0.9}  # above CONTESTED_CUT_THRESHOLD
        return {"contested": 0.1}
    conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)

    with patch("services.acp_shared.debate._fetch_brand_profile", new=AsyncMock(return_value=None)):
        candidates = [_seg_candidate("seg-ok-1"), _seg_candidate("seg-bad"), _seg_candidate("seg-ok-2")]
        out = await apply_debate(uuid4(), candidates, conn)

    assert [c.segment_id for c in out] == ["seg-ok-1", "seg-ok-2"]


@pytest.mark.asyncio
async def test_apply_debate_single_candidate_safety_rail_never_cuts():
    """N=1: MAX_CUT_FRACTION=0.5 of 1 floors to 0 cuts allowed — a lone flagged candidate is
    reprieved rather than cut, since cutting it would be a 100% cut, not <=50%. This is the
    safety rail's own conservative floor behavior at the smallest possible list size, not a
    bug — documented here so it isn't "fixed" by someone assuming it should cut."""
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"contested": 0.9})

    with patch("services.acp_shared.debate._fetch_brand_profile", new=AsyncMock(return_value=None)):
        candidate = _seg_candidate("seg-1")
        out = await apply_debate(uuid4(), [candidate], conn)

    assert out == [candidate]


@pytest.mark.asyncio
async def test_apply_debate_keeps_low_contested_segment():
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"contested": 0.1})  # below threshold

    with patch("services.acp_shared.debate._fetch_brand_profile", new=AsyncMock(return_value=None)):
        candidate = _seg_candidate("seg-1")
        out = await apply_debate(uuid4(), [candidate], conn)

    assert out == [candidate]


@pytest.mark.asyncio
async def test_apply_debate_route_candidates_skip_contested_check():
    """Route candidates have no segment_id -> the contested lookup (keyed on atom_segment.
    segment_id) is skipped entirely for them, never cut by contested."""
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with patch("services.acp_shared.debate._fetch_brand_profile", new=AsyncMock(return_value=None)):
        candidate = _route_candidate("route-1")
        out = await apply_debate(uuid4(), [candidate], conn)

    assert out == [candidate]
    conn.fetchrow.assert_not_called()  # no segment_id -> no atom_segment.contested lookup at all


# ── contested read failure — advisory, never cuts on error ─────────────────

@pytest.mark.asyncio
async def test_apply_debate_contested_check_failure_does_not_cut():
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    with patch("services.acp_shared.debate._fetch_brand_profile", new=AsyncMock(return_value=None)):
        candidate = _seg_candidate("seg-1")
        out = await apply_debate(uuid4(), [candidate], conn)

    assert out == [candidate]  # failure counts as "passes", never cut


# ── brand-fit cut (cache hit / miss) ─────────────────────────────────────────

_BRAND_PROFILE = {
    "brand_core_idea": "Discreet executive adventure",
    "brand_customer_segment": "", "brand_customer_mindset": "", "brand_voice_examples": [],
    "brand_good_examples": "", "_version": 3,
}


@pytest.mark.asyncio
async def test_apply_debate_brand_fit_cache_hit_skips_llm_call():
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()

    async def fake_fetchrow(sql, *args):
        if "atom_segment.contested" in sql or "SELECT contested" in sql:
            return {"contested": None}
        if "debate_brand_fit_cache" in sql:
            return {"judge_score": 8.0, "feedback": ""}
        return None
    conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)

    with patch("services.acp_shared.debate._fetch_brand_profile",
               new=AsyncMock(return_value=_BRAND_PROFILE)), \
         patch("services.acp_shared.debate.score_brand_fit") as m_score:
        candidate = _seg_candidate("seg-1")
        out = await apply_debate(uuid4(), [candidate], conn)

    m_score.assert_not_called()  # cache HIT — zero LLM calls
    assert out == [candidate]  # judge_score 8.0 >= BRAND_FIT_CUT_THRESHOLD (7.0) -> passes


@pytest.mark.asyncio
async def test_apply_debate_brand_fit_cache_miss_calls_llm_and_stores():
    from services.acp_shared.debate import apply_debate
    from services.content_generation.brand_fit import BrandFitResult

    conn = AsyncMock()
    conn.execute = AsyncMock()

    async def fake_fetchrow(sql, *args):
        if "debate_brand_fit_cache" in sql:
            return None  # cache miss
        return {"contested": None}
    conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
    conn.fetch = AsyncMock(return_value=[{"text": "Board the private cruise at dawn."}])

    fake_result = BrandFitResult(
        brand_fit_score=8.0, cross_brand_distinct=8.0, mission_present=True, feedback="",
        judge_score=8.0, model_used="gpt-4.1", cost_usd=0.001,
        input_tokens=10, output_tokens=5, stop_reason="stop", account=None, fallback_used=None,
    )

    with patch("services.acp_shared.debate._fetch_brand_profile",
               new=AsyncMock(return_value=_BRAND_PROFILE)), \
         patch("services.acp_shared.debate.score_brand_fit", return_value=fake_result) as m_score:
        candidate = _seg_candidate("seg-1")
        out = await apply_debate(uuid4(), [candidate], conn)

    m_score.assert_called_once()
    conn.execute.assert_called_once()  # cache write
    assert out == [candidate]


@pytest.mark.asyncio
async def test_apply_debate_cuts_low_brand_fit_score():
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()

    async def fake_fetchrow(sql, *args):
        if "debate_brand_fit_cache" in sql:
            segment_id = args[1] if len(args) > 1 else None
            if segment_id == "seg-bad":
                return {"judge_score": 2.0, "feedback": "Too generic."}
            return {"judge_score": 9.0, "feedback": ""}
        return {"contested": None}
    conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)

    with patch("services.acp_shared.debate._fetch_brand_profile",
               new=AsyncMock(return_value=_BRAND_PROFILE)):
        candidates = [_seg_candidate("seg-ok-1"), _seg_candidate("seg-bad"), _seg_candidate("seg-ok-2")]
        out = await apply_debate(uuid4(), candidates, conn)

    # judge_score 2.0 < BRAND_FIT_CUT_THRESHOLD (7.0), the other two pass (9.0)
    assert [c.segment_id for c in out] == ["seg-ok-1", "seg-ok-2"]


@pytest.mark.asyncio
async def test_apply_debate_skips_brand_fit_when_no_signals():
    """A brand profile row exists but has_brand_signals() is False (e.g. only good_examples
    set) -> brand-fit is skipped entirely, same guard judge_node.py already uses."""
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"contested": None})

    empty_profile = {
        "brand_core_idea": "", "brand_customer_segment": "", "brand_customer_mindset": "",
        "brand_voice_examples": [], "brand_good_examples": "some example", "_version": 1,
    }
    with patch("services.acp_shared.debate._fetch_brand_profile",
               new=AsyncMock(return_value=empty_profile)), \
         patch("services.acp_shared.debate.score_brand_fit") as m_score:
        candidate = _seg_candidate("seg-1")
        out = await apply_debate(uuid4(), [candidate], conn)

    m_score.assert_not_called()
    assert out == [candidate]


# ── safety rail: never cut more than MAX_CUT_FRACTION of the list ──────────

@pytest.mark.asyncio
async def test_apply_debate_safety_rail_caps_cuts_at_half():
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()
    # All 4 candidates are highly contested (would all fail without the safety rail).
    conn.fetchrow = AsyncMock(return_value={"contested": 0.99})

    with patch("services.acp_shared.debate._fetch_brand_profile", new=AsyncMock(return_value=None)):
        candidates = [_seg_candidate(f"seg-{i}") for i in range(4)]
        out = await apply_debate(uuid4(), candidates, conn)

    # MAX_CUT_FRACTION=0.5 of 4 = 2 cut, 2 survive (reprieved, not silently dropped).
    assert len(out) == 2


@pytest.mark.asyncio
async def test_apply_debate_safety_rail_preserves_original_order():
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"contested": 0.99})

    with patch("services.acp_shared.debate._fetch_brand_profile", new=AsyncMock(return_value=None)):
        candidates = [_seg_candidate(f"seg-{i}") for i in range(4)]
        out = await apply_debate(uuid4(), candidates, conn)

    # Surviving candidates must appear in the SAME relative order as the input list.
    input_order = [c.segment_id for c in candidates]
    output_order = [c.segment_id for c in out]
    assert output_order == [sid for sid in input_order if sid in output_order]


# ── outer failure — advisory at the propose_slate() call site ──────────────

@pytest.mark.asyncio
async def test_apply_debate_brand_profile_fetch_failure_disables_brand_fit_not_the_whole_call():
    from services.acp_shared.debate import apply_debate

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"contested": None})

    with patch("services.acp_shared.debate._fetch_brand_profile",
               new=AsyncMock(side_effect=RuntimeError("db down"))):
        candidate = _seg_candidate("seg-1")
        out = await apply_debate(uuid4(), [candidate], conn)

    assert out == [candidate]  # brand profile fetch failed -> brand-fit skipped, contested still ran
