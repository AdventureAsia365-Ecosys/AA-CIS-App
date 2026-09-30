"""AA-699 — T-series Jev: T3 numeric hits cleared by `a1_claim_supported`, T2 judge tie-break per tenant."""
import asyncio
from unittest.mock import patch

from services.acp_produce import tenant_pipeline as tp
from services.content_generation import judge_node as jn
from shared.llm_client.decide import Decision, Verdict

TOUR = {"name": "Paro Valley", "summary": "Hike 5 km to Taktsang at 3,120 m.", "itineraries": "Day 1 — Paro"}
HITS = [
    {"field": "summary", "sentence": "The hike covers 3.1 miles.", "novel_numbers": ["3.1"]},
    {"field": "summary", "sentence": "The monastery dates from 1692.", "novel_numbers": ["1692"]},
]


def _dec(zone, mode="enforce", p=0.5):
    return Decision(verdicts={tp.T3_JEV_Q: Verdict(tp.T3_JEV_Q, mode, zone, probability=p)})


def _clear(zones, tenant_id="t-1"):
    calls = []

    async def fake(stage, key, state, qs, **kw):
        calls.append((stage, state["sentence"], kw.get("tenant_id")))
        return zones[state["sentence"]]

    with patch.object(tp, "decide", fake):
        kept, cleared = asyncio.run(tp.t3_jev_clear(HITS, TOUR, tenant_id))
    return kept, cleared, calls


def test_enforced_accept_clears_only_that_hit():
    kept, cleared, calls = _clear({HITS[0]["sentence"]: _dec("accept", p=0.95),
                                   HITS[1]["sentence"]: _dec("grey", p=0.6)})
    assert [g["sentence"] for g in cleared] == [HITS[0]["sentence"]]
    assert [g["sentence"] for g in kept] == [HITS[1]["sentence"]]
    assert all(c[0] == "t3_grounding" and c[2] == "t-1" for c in calls)


def test_shadow_accept_or_reject_never_changes_the_hits():
    kept, cleared, _ = _clear({HITS[0]["sentence"]: _dec("accept", mode="shadow", p=0.99),
                               HITS[1]["sentence"]: _dec("reject", mode="enforce", p=0.01)})
    assert cleared == [] and kept == HITS          # no reject floor: Jev never adds or drops a hit by rejecting


def test_skipped_tenant_asks_once_and_keeps_everything():
    kept, cleared, calls = _clear({s["sentence"]: _dec("skipped") for s in HITS})
    assert len(calls) == 1 and kept == HITS and cleared == []


def test_no_hits_or_no_tenant_asks_nothing():
    with patch.object(tp, "decide") as m:
        assert asyncio.run(tp.t3_jev_clear([], TOUR, "t-1")) == ([], [])
        assert asyncio.run(tp.t3_jev_clear(HITS, TOUR, None)) == (HITS, [])
    assert m.call_count == 0


def test_decide_error_fails_open():
    async def boom(*a, **k):
        raise RuntimeError("x")
    with patch.object(tp, "decide", boom):
        assert asyncio.run(tp.t3_jev_clear(HITS, TOUR, "t-1")) == (HITS, [])


class _TenantResult:
    brand_fit_score = 9.0
    cross_brand_distinct = 6.0
    mission_present = True
    judge_score = 6.8                                # combined score: what gates a tenant rewrite
    feedback = "More restraint."
    model_used = "m"
    input_tokens = output_tokens = 1
    cost_usd = 0.0
    stop_reason = "end"
    account = "acc3"
    fallback_used = False


def test_tenant_tiebreak_uses_t2_stage_and_tenant_id():
    state = {"quality_score": 9.6, "generated": {"name": "N", "summary": "S", "highlights": ["h"]},
             "tour": {"tour_id": "t1"}, "retry_count": 1, "is_tenant_rewrite": True, "tenant_id": "ten-9"}
    dec = Decision(verdicts={jn.TIE_Q: Verdict(jn.TIE_Q, "shadow", "grey", probability=0.7)})
    with patch.object(jn, "score_brand_fit", return_value=_TenantResult()), \
         patch.object(jn, "has_brand_signals", return_value=True), \
         patch.object(jn, "record_call_sync"), \
         patch("shared.llm_client.decide.decide_sync", return_value=dec) as m:
        out = jn.judge_node(state)
    args, kwargs = m.call_args
    assert args[0] == jn.T2_TIE_STAGE and args[1] == "t2_judge:ten-9:t1:1" and kwargs["tenant_id"] == "ten-9"
    assert out["quality_score"] == 6.8               # shadow: unchanged
