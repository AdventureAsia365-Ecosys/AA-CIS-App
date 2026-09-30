"""AA-694 — PAA questions about another country, or about no country in particular, stop counting."""
from unittest.mock import patch

import pytest

from services.acp_contract import atom_ranking as ar
from shared.llm_client.decide import Decision, Verdict


def _dec(foreign=("grey", "shadow"), here=("grey", "shadow")):
    return Decision(verdicts={
        ar.SCOPE_FOREIGN_Q: Verdict(ar.SCOPE_FOREIGN_Q, foreign[1], foreign[0]),
        ar.SCOPE_HERE_Q: Verdict(ar.SCOPE_HERE_Q, here[1], here[0]),
    })


def test_drop_rule():
    assert ar.scope_dropped(_dec(foreign=("accept", "enforce")))       # names somewhere else
    assert ar.scope_dropped(_dec(here=("reject", "enforce")))          # generic / not about here
    assert not ar.scope_dropped(_dec(foreign=("accept", "shadow")))    # shadow never acts
    assert not ar.scope_dropped(_dec(here=("grey", "enforce")))


@pytest.mark.asyncio
async def test_filter_removes_dropped_pairs_per_country_set_and_asks_once():
    rows = [{"segment_id": "s1", "countries": ["Bhutan"]}, {"segment_id": "s2", "countries": ["Bhutan"]},
            {"segment_id": "s3", "countries": ["Nepal", "Bhutan"]}, {"segment_id": "s4", "countries": []}]
    cands = {"s1": {"Is Paro worth it?", "What is Yosemite?"}, "s2": {"What is Yosemite?"},
             "s3": {"What is Yosemite?"}, "s4": {"What is Yosemite?"}}
    asked = []

    async def fake(stage, subject, state, keys, **kw):
        asked.append((state["countries"], state["question"]))
        return _dec(foreign=("accept", "enforce")) if "Yosemite" in state["question"] else _dec()

    with patch.object(ar, "decide", new=fake):
        stats = await ar.filter_candidates_by_country_scope(None, rows, cands)
    assert cands == {"s1": {"Is Paro worth it?"}, "s2": set(), "s3": set(), "s4": {"What is Yosemite?"}}
    # one call per distinct (country set, question); s4 has no country → not asked
    assert sorted(asked) == sorted([("Bhutan", "Is Paro worth it?"), ("Bhutan", "What is Yosemite?"),
                                    ("Bhutan, Nepal", "What is Yosemite?")])
    assert stats == {"scope_pairs": 3, "scope_dropped": 2}



def _land(rej=False, mode="enforce"):
    return Decision(verdicts={ar.LANDING_Q: Verdict(ar.LANDING_Q, mode, "reject" if rej else "grey")})


@pytest.mark.asyncio
async def test_landing_filter_drops_rejected_per_moment_and_asks_once():
    rows = [{"segment_id": "s1", "canonical_place": "Paro Taktsang", "canonical_action": "hike"},
            {"segment_id": "s2", "canonical_place": "Paro Taktsang", "canonical_action": "hike"},
            {"segment_id": "s3", "canonical_place": "Punakha Dzong", "canonical_action": "visit"}]
    cands = {"s1": {"How long is the Tiger's Nest hike?", "What is the oldest dzong?"},
             "s2": {"What is the oldest dzong?"}, "s3": {"What is the oldest dzong?"}}
    asked = []

    async def fake(stage, subject, state, keys, **kw):
        asked.append((state["moment"], state["query"]))
        return _land(rej=(state["moment"].startswith("Paro") and "dzong" in state["query"]))

    with patch.object(ar, "decide", new=fake):
        stats = await ar.filter_candidates_by_landing(None, rows, cands)
    assert cands == {"s1": {"How long is the Tiger's Nest hike?"}, "s2": set(), "s3": {"What is the oldest dzong?"}}
    assert len(asked) == 3                      # (Paro, hike) pairs shared by s1/s2
    assert stats == {"landing_pairs": 3, "landing_rejected": 1}


@pytest.mark.asyncio
async def test_landing_filter_shadow_reject_keeps_question():
    rows = [{"segment_id": "s1", "canonical_place": "Paro", "canonical_action": "visit"}]
    cands = {"s1": {"q"}}

    async def fake(*a, **kw):
        return _land(rej=True, mode="shadow")

    with patch.object(ar, "decide", new=fake):
        await ar.filter_candidates_by_landing(None, rows, cands)
    assert cands == {"s1": {"q"}}


@pytest.mark.asyncio
async def test_land_questions_uses_the_filtered_candidates():
    """A question dropped by a Jev gate must not count again through the claim-by-name fallback."""
    class Conn:
        async def fetch(self, *a):
            return [{"atom_id": "a1", "place": "Paro Taktsang", "action": "hike"}]

        async def execute(self, *a):
            return None

    paa = [("paro taktsang hike", "US", ["How long is the Paro Taktsang hike?", "Is the Paro Taktsang hike hard?"])]
    n = await ar.land_questions_for_segment(Conn(), "Paro Taktsang", "hike", ["a1"], paa, question_vectors={},
                                            atoms_embedded=True, candidates=set())
    assert n == 0
    n = await ar.land_questions_for_segment(Conn(), "Paro Taktsang", "hike", ["a1"], paa, question_vectors={},
                                            atoms_embedded=True,
                                            candidates={"How long is the Paro Taktsang hike?"})
    assert n == 1
