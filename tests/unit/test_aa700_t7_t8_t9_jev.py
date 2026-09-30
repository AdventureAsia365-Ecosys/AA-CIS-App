"""AA-700 — T7 Debate prefilter, T8 angle-answers check, T9 fact relevance (Jev, shadow first)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.acp_angle_gate import ranking
from services.acp_content_writing import facts as facts_mod
from services.acp_shared import debate
from shared.llm_client.decide import Decision, Verdict


def _dec(key, zone, mode="enforce"):
    return Decision(verdicts={key: Verdict(key, mode, zone, probability=0.5)})


# ---------------------------------------------------------------- T8

ANGLES = [{"name": "Tiger's Nest at dawn", "why_it_works": "the climb"},
          {"name": "Paro market", "why_it_works": "food"}]


def _evidence():
    return [ranking.RankedAngle(idx=0, answers=["how long is the tiger's nest hike"]),
            ranking.RankedAngle(idx=1, answers=["what to eat in paro", "is paro safe"])]


def _verify(zones):
    async def fake(stage, key, state, qs, **kw):
        assert stage == ranking.T8_STAGE and kw["tenant_id"] == "t-1"
        return _dec(ranking.T8_Q, *zones[state["question"]])
    with patch("shared.llm_client.decide.decide", fake):
        return asyncio.run(ranking.verify_answers(ANGLES, _evidence(), "t-1"))


def test_t8_enforced_reject_drops_answer_and_reranks():
    ev, best, dropped = _verify({"how long is the tiger's nest hike": ("accept",),
                                 "what to eat in paro": ("reject",), "is paro safe": ("reject",)})
    assert dropped == 2 and ev[1].answers == [] and best == 0


def test_t8_shadow_changes_nothing():
    ev, best, dropped = _verify({q: ("reject", "shadow") for q in
                                 ["how long is the tiger's nest hike", "what to eat in paro", "is paro safe"]})
    assert dropped == 0 and len(ev[1].answers) == 2 and best == 1


def test_t8_no_claims_asks_nothing():
    with patch("shared.llm_client.decide.decide") as m:
        ev, best, dropped = asyncio.run(ranking.verify_answers(ANGLES, [ranking.RankedAngle(idx=0),
                                                                        ranking.RankedAngle(idx=1)], "t-1"))
    assert m.call_count == 0 and dropped == 0 and best == 0


# ---------------------------------------------------------------- T9

FACTS = [{"fact_id": 1, "title": "Visa", "body": "Bhutan requires a visa."},
         {"fact_id": 2, "title": "Laos budget", "body": "$100 goes far in Laos."}]


def _select(zones):
    async def fake(stage, key, state, qs, **kw):
        assert stage == facts_mod.T9_STAGE and key.startswith(f"t9:{'1' if 'Visa' in state['fact']['title'] else '2'}:")
        return _dec(facts_mod.T9_Q, *zones[state["fact"]["title"]])
    with patch("shared.llm_client.decide.decide", fake):
        return asyncio.run(facts_mod.select_relevant_facts(
            FACTS, moment="Hike to Tiger's Nest", angle={"name": "a", "why_it_works": "b"}, trip="Bhutan",
            tenant_id="t-1"))


def test_t9_enforced_reject_leaves_fact_out():
    kept, out = _select({"Visa": ("accept",), "Laos budget": ("reject",)})
    assert [f["fact_id"] for f in kept] == [1] and out == 1


def test_t9_shadow_or_skipped_keeps_all():
    kept, out = _select({"Visa": ("skipped", "shadow"), "Laos budget": ("reject", "shadow")})
    assert kept == FACTS and out == 0


def test_t9_no_facts():
    assert asyncio.run(facts_mod.select_relevant_facts([], moment="m", angle={}, trip=None, tenant_id="t")) == ([], 0)


# ---------------------------------------------------------------- T7

PROFILE = {"brand_core_idea": "quiet luxury", "brand_customer_segment": "execs", "brand_customer_mindset": "calm",
           "brand_voice_examples": ["measured"], "_version": 3}
TOPIC = {"place": "Paro", "action": "hike", "evidence": ["Hike to Tiger's Nest."]}


def _candidate():
    return SimpleNamespace(segment_id="seg_1", route_id=None, place="Paro", action="hike")


def _debate(zone, mode="enforce", luna_score=8.0):
    luna = SimpleNamespace(judge_score=luna_score, model_used="m", input_tokens=1, output_tokens=1, cost_usd=0.0,
                           stop_reason="end", account="acc3", fallback_used=False)
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    with patch.object(debate, "_fetch_brand_profile", AsyncMock(return_value=PROFILE)), \
         patch.object(debate, "has_brand_signals", return_value=True), \
         patch.object(debate, "_cached_brand_fit", AsyncMock(return_value=None)), \
         patch.object(debate, "_fetch_candidate_text", AsyncMock(return_value=TOPIC)), \
         patch.object(debate, "_store_brand_fit_cache", AsyncMock()), \
         patch.object(debate, "record_call", AsyncMock()), \
         patch.object(debate, "decide", AsyncMock(return_value=_dec(debate.T7_Q, zone, mode))) as jev, \
         patch.object(debate, "score_candidate_fit", return_value=luna) as m:
        out = asyncio.run(debate.apply_debate("t-1", [_candidate(), _candidate()], conn))
    return out, m.call_count, jev


def test_t7_enforced_accept_skips_luna():
    out, luna_calls, jev = _debate("accept")
    assert luna_calls == 0 and len(out) == 2
    stage, key, state, qs = jev.call_args.args
    assert stage == debate.T7_STAGE and key == "t7:3:seg_1" and state["topic"]["place"] == "Paro"
    assert jev.call_args.kwargs["tenant_id"] == "t-1"


def test_t7_enforced_reject_cuts_without_luna():
    out, luna_calls, _ = _debate("reject")
    assert luna_calls == 0 and len(out) < 2           # cut, within the MAX_CUT_FRACTION rail


def test_t7_shadow_keeps_luna_deciding():
    out, luna_calls, _ = _debate("reject", mode="shadow", luna_score=8.0)
    assert luna_calls == 2 and len(out) == 2
