"""S218 — segment matching cost no longer grows with the whole catalog.

`_candidate_pseudo_atoms` keeps only existing Segments a new atom could join (same verb, a shared
place word); `derive_segments(..., involving=new_ids)` never compares two existing Segments with
each other. Together the per-tour pool is the tour plus its candidates, not every Segment."""
from services.acp_contract.segment_matching import (
    _PSEUDO_PREFIX, SegmentAtom, _candidate_pseudo_atoms, derive_segments,
)


def _new(i, place, action, country="Japan"):
    return SegmentAtom(f"atom{i}", "tour-new", 1, place, action, country)


def _old(sid, place, action, country="Japan"):
    return SegmentAtom(f"{_PSEUDO_PREFIX}{sid}", "", None, place, action, country)


NEW = [_new(1, "Fushimi Inari shrine", "walk through the torii gates"),
       _new(2, "Nishiki market", "eat street food")]
OLD = [_old("inari", "Fushimi Inari", "hike the torii gate trail"),
       _old("nishiki", "Nishiki market", "taste local snacks"),
       _old("tokyo", "Tsukiji outer market", "eat sushi"),          # same verb, no shared place word
       _old("osaka", "Dotonbori", "eat takoyaki"),
       _old("gion", "Gion district", "stroll the lanes")]


def _components_with_new(atoms, **kw):
    new_ids = {a.atom_id for a in NEW}
    return sorted(sorted(s.atom_ids) for s in derive_segments(atoms, **kw) if set(s.atom_ids) & new_ids)


def test_candidates_keep_only_joinable_segments():
    kept = {p.atom_id for p in _candidate_pseudo_atoms(NEW, OLD)}
    assert f"{_PSEUDO_PREFIX}inari" in kept and f"{_PSEUDO_PREFIX}nishiki" in kept
    assert f"{_PSEUDO_PREFIX}osaka" not in kept and f"{_PSEUDO_PREFIX}gion" not in kept


def test_filtering_does_not_change_what_new_atoms_join():
    full = _components_with_new(NEW + OLD)
    scoped = _components_with_new(NEW + _candidate_pseudo_atoms(NEW, OLD),
                                  involving=frozenset(a.atom_id for a in NEW))
    assert scoped == full


def test_keep_retains_the_tours_own_prior_segments():
    kept = {p.atom_id for p in _candidate_pseudo_atoms(NEW, OLD, keep={f"{_PSEUDO_PREFIX}gion"})}
    assert f"{_PSEUDO_PREFIX}gion" in kept


def test_two_existing_segments_merge_only_through_a_new_atom():
    a = _old("a", "Arashiyama bamboo grove", "walk the bamboo path")
    b = _old("b", "Arashiyama bamboo forest", "walk among the bamboo")
    lone = [s.atom_ids for s in derive_segments([a, b], involving=frozenset())]
    assert len(lone) == 2                       # not compared with each other
    bridge = _new(9, "Arashiyama bamboo", "walk the grove")
    merged = derive_segments([a, b, bridge], involving=frozenset({bridge.atom_id}))
    assert any({a.atom_id, b.atom_id, bridge.atom_id} <= set(s.atom_ids) for s in merged)
