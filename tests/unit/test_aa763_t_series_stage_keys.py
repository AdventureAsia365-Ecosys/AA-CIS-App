"""AA-763 (G4) — T-series calls route and log under their own stage keys (t2_*, t7_debate_judge),
copied from the A1 twins (migration 219), while A1 keeps its s1_* keys."""
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.content_generation import brand_fit, itinerary_utils
from services.content_generation.stages import T2_TWINS, T7_DEBATE_JUDGE, stage_for
from services.acp_shared.writing_progress import TOUR_STAGE_STEPS
from shared.llm_client.role_config import SAFE_DEFAULTS

MIGRATION = Path(__file__).resolve().parents[2] / "api" / "migrations" / "219_t_series_stage_keys.sql"
_PROFILE = {"brand_core_idea": "Slow travel", "brand_customer_segment": "couples",
            "brand_customer_mindset": "curious", "brand_voice_examples": ["warm"]}


def _llm(content='{"brand_fit_score": 8, "cross_brand_distinct": 7, "feedback": "fits"}'):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=content, model_used="gpt-5.6-luna", cost_usd=0.0004,
                                           input_tokens=300, output_tokens=40, stop_reason="end_turn",
                                           satellite_account="acc3", fallback_used=False)
    return fake


def test_stage_for():
    assert stage_for({"is_tenant_rewrite": True}, "s1_judge") == "t2_judge"
    assert stage_for({"is_tenant_rewrite": False}, "s1_judge") == "s1_judge"
    assert stage_for({}, "s1_flag_fix") == "s1_flag_fix"
    assert stage_for(None, "s1_brand_audit") == "s1_brand_audit"
    assert stage_for({"is_tenant_rewrite": True}, "s1_generate") == "s1_generate"  # not a twin


def test_safe_defaults_twins_match_a1_routes():
    pairs = list(T2_TWINS.items()) + [("s1_judge", T7_DEBATE_JUDGE)]
    for a1, t in pairs:
        a, b = SAFE_DEFAULTS[a1], SAFE_DEFAULTS[t]
        assert (b.role, b.provider, b.model_id, b.account_route, tuple(b.fallback_model_ids)) == \
            (a.role, a.provider, a.model_id, a.account_route, tuple(a.fallback_model_ids)), t
        assert b.stage == t


def test_migration_seeds_every_twin_from_its_a1_row():
    sql = MIGRATION.read_text()
    pairs = set(re.findall(r"\('(t\d_[a-z_]+)', '(s1_[a-z_]+)'\)", sql))
    assert pairs == set((t, a) for a, t in T2_TWINS.items()) | {(T7_DEBATE_JUDGE, "s1_judge")}
    assert "VALUES ('219'" in sql


def test_t2_progress_steps_mirror_s1():
    for a1, t in T2_TWINS.items():
        assert TOUR_STAGE_STEPS[t] == TOUR_STAGE_STEPS[a1]


def test_brand_fit_routes_on_given_stage_and_defaults_to_s1():
    with patch.object(brand_fit, "LLMClient", return_value=_llm()) as m:
        brand_fit.score_candidate_fit(_PROFILE, {"place": "P", "action": "a", "evidence": ["e"]},
                                      stage=T7_DEBATE_JUDGE)
    assert m.return_value.generate.call_args.args[0].stage == "t7_debate_judge"
    with patch.object(brand_fit, "LLMClient", return_value=_llm()) as m:
        brand_fit.score_candidate_fit(_PROFILE, {"place": "P", "action": "a", "evidence": ["e"]})
    assert m.return_value.generate.call_args.args[0].stage == "s1_judge"


def test_judge_node_uses_t2_judge_on_tenant_rewrite():
    from services.content_generation import judge_node
    result = MagicMock(brand_fit_score=8, cross_brand_distinct=7, mission_present=True, judge_score=7.5,
                       feedback="", model_used="m", input_tokens=1, output_tokens=1, cost_usd=0.0)
    state = {"is_tenant_rewrite": True, "tenant_id": "t", "generated": {"name": "x"},
             "quality_score": 7.0, "brand_core_idea": "Slow travel"}
    with patch.object(judge_node, "has_brand_signals", return_value=True), \
         patch.object(judge_node, "score_brand_fit", return_value=result) as score, \
         patch.object(judge_node, "record_call_sync") as rec:
        judge_node.judge_node(state)
    assert score.call_args.kwargs["stage"] == "t2_judge"
    assert rec.call_args.kwargs["stage"] == "t2_judge"


def test_itinerary_nudge_routes_on_given_stage():
    client = MagicMock()
    client.generate.return_value = MagicMock(content='{"title": "T", "body": "B"}', cost_usd=0.0)
    itinerary_utils.nudge_itinerary_day(client, "src", "t", "b", 50, stage="t2_itinerary_nudge")
    assert client.generate.call_args.args[0].stage == "t2_itinerary_nudge"
