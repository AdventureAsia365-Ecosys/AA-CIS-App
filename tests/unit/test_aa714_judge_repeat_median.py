"""AA-714 layer 2 — a reasoning judge (no seed) is scored 3x and the median kept when its first
score lands near MIN_QUALITY; a seeded model (gpt-4.1) or a clear score is scored once."""
from unittest.mock import patch

import pytest

from services.content_generation import brand_fit as bf
from services.content_generation.brand_fit import BrandFitResult, score_brand_fit

BRAND = {"brand_core_idea": "slow, private journeys", "brand_customer_mindset": "unhurried"}
GEN = {"name": "Kyoto Days", "subtitle": "Quiet temples", "summary": "Calm.", "highlights": ["Nanzen-ji"],
       "itineraries": "Day 1 — Arrive Kyoto.", "seo_title": "Kyoto", "seo_meta": "A calm Kyoto trip."}


def _res(bfit, distinct=9.0, mission=True, model="satellite-gpt-5.6-luna"):
    return BrandFitResult(brand_fit_score=bfit, cross_brand_distinct=distinct, mission_present=mission,
                          feedback=f"fb{bfit}", judge_score=min(bfit, distinct), model_used=model,
                          cost_usd=0.001, input_tokens=100, output_tokens=50, stop_reason="end_turn",
                          account="acc3", fallback_used=False)


def _unseeded(_key):
    class M:  # Luna: no temperature/seed
        supports_temperature = False
    return M()


def test_reasoning_judge_near_threshold_takes_median_of_three():
    with patch.object(bf, "_judge", side_effect=[_res(8.0), _res(7.0), _res(6.0)]) as j, \
            patch("shared.llm_client.catalog.get_model_sync", side_effect=_unseeded):
        out = score_brand_fit(BRAND, GEN)
    assert j.call_count == 3
    assert out.brand_fit_score == 7.0            # median(8,7,6)
    assert out.cost_usd == pytest.approx(0.003)  # all three billed


def test_reasoning_judge_clear_score_is_scored_once():
    with patch.object(bf, "_judge", side_effect=[_res(9.0)]) as j, \
            patch("shared.llm_client.catalog.get_model_sync", side_effect=_unseeded):
        out = score_brand_fit(BRAND, GEN)
    assert j.call_count == 1 and out.brand_fit_score == 9.0


def test_seeded_model_is_scored_once_even_near_threshold():
    # gpt-4.1 honours the seed → already reproducible, no repeat.
    with patch.object(bf, "_judge", side_effect=[_res(7.0, model="gpt-4.1")]) as j, \
            patch("shared.llm_client.catalog.get_model_sync", return_value=None):
        out = score_brand_fit(BRAND, GEN)
    assert j.call_count == 1 and out.brand_fit_score == 7.0


def test_median_mission_is_majority_vote():
    with patch.object(bf, "_judge",
                      side_effect=[_res(7.0, mission=False), _res(7.0, mission=False), _res(7.0, mission=True)]), \
            patch("shared.llm_client.catalog.get_model_sync", side_effect=_unseeded):
        out = score_brand_fit(BRAND, GEN)
    assert out.mission_present is False          # 2 of 3 false
    assert out.judge_score == 6.0                # mission-absent cap applied
