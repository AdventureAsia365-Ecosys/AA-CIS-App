"""AA-694 A3-6 (demand ownership) and A3-1 (atom grounding) Jev gates."""
from unittest.mock import patch

import pytest

from services.acp_contract import atom_ranking as ar
from services.acp_produce import tenant_pipeline as tp
from shared.llm_client.decide import Decision, Verdict


def _dec(key, reject, mode="enforce"):
    return Decision(verdicts={key: Verdict(key, mode, "reject" if reject else "grey")})


ROWS = [("kyoto", "US", 165000), ("kyoto incense making", "US", 90), ("kyoto incense", "US", 400),
        ("kyoto", "UK", 50000), ("tokyo", "US", 999999)]


def test_demand_candidates_best_first_one_market():
    cands = ar.demand_candidates("Kyoto", "incense making", ROWS, "US")
    # claimable words come from the place only, so every candidate fits "kyoto" once and volume decides:
    # the city-wide keyword is claimed first — the A3-6 problem.
    assert [k for k, _ in cands] == ["kyoto", "kyoto incense", "kyoto incense making"]
    assert ar.compute_demand("Kyoto", "incense making", ROWS)["US"] == cands[0][1]


@pytest.mark.asyncio
async def test_resolve_demand_walks_down_on_enforced_reject():
    async def fake(stage, subject, state, keys, **kw):
        return _dec(ar.DEMAND_Q, reject=state["keyword"] == "kyoto")

    with patch.object(ar, "decide", new=fake):
        out = await ar.resolve_demand(None, [("s1", "Kyoto", "incense making")], ROWS, "US")
    assert out == {"s1": {"US": 400}}


@pytest.mark.asyncio
async def test_resolve_demand_all_rejected_or_none_claimed_gives_empty():
    async def fake(*a, **kw):
        return _dec(ar.DEMAND_Q, reject=True)

    with patch.object(ar, "decide", new=fake):
        out = await ar.resolve_demand(None, [("s1", "Kyoto", "incense making"), ("s2", "Lhasa", "visit")],
                                      ROWS, "US")
    assert out == {"s1": {}, "s2": {}}


@pytest.mark.asyncio
async def test_resolve_demand_shadow_keeps_best():
    async def fake(*a, **kw):
        return _dec(ar.DEMAND_Q, reject=True, mode="shadow")

    with patch.object(ar, "decide", new=fake):
        out = await ar.resolve_demand(None, [("s1", "Kyoto", "incense making")], ROWS, "US")
    assert out == {"s1": {"US": 165000}}


@pytest.mark.asyncio
async def test_ground_day_atoms_drops_enforced_rejects_only():
    atoms = [{"place": "Paro Taktsang", "action": "hike"}, {"place": "Punakha Dzong", "action": "visit"}]
    day = {"title": "Tiger's Nest", "body": "Hike to Paro Taktsang."}
    asked = []

    async def fake(stage, subject, state, keys, **kw):
        asked.append((state["atom"], kw.get("tenant_id")))
        return _dec(tp.ATOM_Q, reject="Punakha" in state["atom"])

    with patch.object(tp, "decide", new=fake):
        kept = await tp.ground_day_atoms(atoms, day, "platform", "t1", 3, None)
    assert kept == [atoms[0]]
    assert len(asked) == 2 and all(t is None for _, t in asked)     # platform content → no tenant id

    async def shadow(*a, **kw):
        return _dec(tp.ATOM_Q, reject=True, mode="shadow")

    with patch.object(tp, "decide", new=shadow):
        assert await tp.ground_day_atoms(atoms, day, "platform", "t1", 3, None) == atoms
    assert await tp.ground_day_atoms([], day, "platform", "t1", 3, None) == []
