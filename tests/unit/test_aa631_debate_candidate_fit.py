"""AA-631 follow-up (S203) — Debate judges a candidate TOPIC from raw evidence, not a fake tour page.

28/09/2026: both fresh Debate rulings scored 1.0 ("repeated placeholder text") because the same
joined atom text was pasted into summary/highlights/itineraries and judged as a finished rewrite,
and the rewrite prompt's mission axis capped every raw candidate at 6.0 (< the 7.0 cut)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_shared import debate
from services.acp_shared.slate import Candidate
from services.content_generation import brand_fit

_PROFILE = {"brand_core_idea": "Slow, food-led travel", "brand_customer_segment": "couples 40+",
            "brand_customer_mindset": "wants to taste the place", "brand_voice_examples": ["warm"]}


def _llm(content: str):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=content, model_used="gpt-5.6-luna", cost_usd=0.0004,
                                           input_tokens=300, output_tokens=40, stop_reason="end_turn",
                                           satellite_account="acc3", fallback_used=False)
    return fake


@pytest.mark.asyncio
async def test_candidate_text_is_deduped_evidence_not_a_fake_page():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"text": "local café — rest at a local café"},
                                         {"text": "Local  café — rest at a local café"},
                                         {"text": None},
                                         {"text": "local café near the Mekong River — papaya salad"}])
    cand = Candidate(segment_id="seg-1", route_id=None, score=1.0, demand=None, questions=0, said=0,
                     place="Vientiane", action="eat")
    topic = await debate._fetch_candidate_text(conn, cand)
    assert topic == {"place": "Vientiane", "action": "eat",
                     "evidence": ["local café — rest at a local café",
                                  "local café near the Mekong River — papaya salad"]}


def test_candidate_prompt_judges_the_topic_with_raw_evidence():
    prompt = brand_fit._build_candidate_prompt(
        _PROFILE, {"place": "Vientiane", "action": "eat", "evidence": ["papaya salad by the Mekong"]})
    assert "Place: Vientiane" in prompt and "- papaya salad by the Mekong" in prompt
    assert "ITINERARIES" not in prompt and "mission_present" not in prompt


def test_candidate_fit_is_not_capped_by_the_mission_axis():
    content = '{"brand_fit_score": 8, "cross_brand_distinct": 7, "feedback": "food-led fits"}'
    with patch.object(brand_fit, "LLMClient", return_value=_llm(content)) as m_client:
        result = brand_fit.score_candidate_fit(_PROFILE, {"place": "Vientiane", "action": "eat",
                                                          "evidence": ["papaya salad"]})
    assert result.judge_score == 7.0 and result.mission_present is True
    request = m_client.return_value.generate.call_args.args[0]
    assert request.stage == "s1_judge" and request.system_prompt == brand_fit.CANDIDATE_JUDGE_SYSTEM


def test_rewrite_judge_keeps_its_mission_cap():
    content = '{"brand_fit_score": 9, "cross_brand_distinct": 9, "mission_present": false, "feedback": ""}'
    with patch.object(brand_fit, "LLMClient", return_value=_llm(content)):
        result = brand_fit.score_brand_fit(_PROFILE, {"name": "x"})
    assert result.judge_score == 6.0
