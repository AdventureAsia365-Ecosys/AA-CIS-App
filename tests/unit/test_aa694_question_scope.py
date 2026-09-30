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
