"""AA-694 A3-2 — Jev picks transit vs experience where the verb rule and the atoms' activity_type disagree."""
from unittest.mock import patch

import pytest

from services.acp_contract import atom_ranking as ar
from shared.llm_client.decide import Decision, Verdict


def _pick(choice, mode="enforce"):
    return Decision(verdicts={ar.TYPE_Q: Verdict(ar.TYPE_Q, mode, "accept", probability=0.95, choice=choice)})


def _row(sid, place, action, types):
    return {"segment_id": sid, "canonical_place": place, "canonical_action": action, "activity_types": types}


def test_atoms_say_transit_is_a_typed_majority():
    assert ar.atoms_say_transit(["transit", "transit", "trek"])
    assert not ar.atoms_say_transit(["transit", "trek"])            # a tie is not a majority
    assert not ar.atoms_say_transit([None, None])                   # untyped atoms do not vote
    assert ar.atoms_say_transit([None, "transit"])
    assert not ar.atoms_say_transit(None)


def test_exclusion_after_pick():
    assert ar.exclusion_after_pick("Paro", "transit", "experience") is None
    assert ar.exclusion_after_pick("the trail", "transit", "experience") == "unnamed_place"
    assert ar.exclusion_after_pick("Kathmandu to Pokhara", None, "transit") == "transit"
    assert ar.exclusion_after_pick("Paro", "transit", None) == "transit"     # no trusted pick → rule stands


@pytest.mark.asyncio
async def test_asks_only_disputed_moments_and_applies_enforced_pick():
    rows = [
        _row("agree-transit", "Paro", "arrive", ["transit"]),
        _row("agree-exp", "Punakha Dzong", "visit", ["culture"]),
        _row("rule-misses", "Kathmandu to Pokhara", "cycle", ["transit", "transit"]),
        _row("rule-overreach", "Thimphu", "drive", ["culture", "other"]),
        _row("same-moment", "Thimphu", "drive", ["culture"]),
    ]
    asked = []

    async def fake(stage, subject, state, keys, **kw):
        asked.append(state["moment"])
        return _pick("transit" if "Pokhara" in state["moment"] else "experience")

    with patch.object(ar, "decide", new=fake):
        out = await ar.resolve_exclusions(None, rows)
    assert sorted(asked) == ["Kathmandu to Pokhara — cycle", "Thimphu — drive"]   # one Verdict per moment
    assert out == {"agree-transit": "transit", "agree-exp": None, "rule-misses": "transit",
                   "rule-overreach": None, "same-moment": None}


@pytest.mark.asyncio
async def test_shadow_keeps_the_rule():
    rows = [_row("rule-misses", "Kathmandu to Pokhara", "cycle", ["transit"]),
            _row("rule-overreach", "Thimphu", "drive", ["culture"])]

    async def shadow(*a, **kw):
        return _pick("experience", mode="shadow")

    with patch.object(ar, "decide", new=shadow):
        out = await ar.resolve_exclusions(None, rows)
    assert out == {"rule-misses": None, "rule-overreach": "transit"}


@pytest.mark.asyncio
async def test_unnamed_place_is_never_asked():
    async def boom(*a, **kw):
        raise AssertionError("must not ask")

    with patch.object(ar, "decide", new=boom):
        out = await ar.resolve_exclusions(None, [_row("s", "the village", "explore", ["transit"])])
    assert out == {"s": "unnamed_place"}


@pytest.mark.asyncio
async def test_untyped_segment_has_no_second_opinion():
    async def boom(*a, **kw):
        raise AssertionError("must not ask")

    with patch.object(ar, "decide", new=boom):
        out = await ar.resolve_exclusions(None, [_row("s", "Paro", "arrive", [None]),
                                                 {"segment_id": "t", "canonical_place": "Paro",
                                                  "canonical_action": "arrive"}])
    assert out == {"s": "transit", "t": "transit"}
