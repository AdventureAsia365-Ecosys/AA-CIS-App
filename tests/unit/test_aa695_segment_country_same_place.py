"""AA-695 A3-7 — Segments never span countries; Jev can keep two differently-named places apart."""
from services.acp_contract import segment_matching as sm
from services.acp_contract.segment_matching import SegmentAtom, derive_segments, pairs_to_ask, place_pair


def _a(atom_id, place, action="explore", country="", tour="t1"):
    return SegmentAtom(atom_id, tour, 1, place, action, country)


def test_same_moment_in_two_countries_is_two_segments():
    atoms = [_a("a1", "local restaurant", "eat lunch", "China"), _a("a2", "local restaurant", "eat lunch", "Thailand")]
    segs = derive_segments(atoms)
    assert len(segs) == 2 and segs[0].id != segs[1].id


def test_unknown_country_still_joins():
    atoms = [_a("a1", "Swayambhunath", "visit", "Nepal"), _a("a2", "Swayambhunath Stupa", "visit", "")]
    assert len(derive_segments(atoms)) == 1


def test_apart_pair_keeps_places_separate():
    atoms = [_a("a1", "Bukchon Hanok Village", country="South Korea"),
             _a("a2", "Jeonju Hanok Village", country="South Korea")]
    assert len(derive_segments(atoms)) == 1                     # the rule alone over-merges them
    apart = frozenset({place_pair("Bukchon Hanok Village", "Jeonju Hanok Village")})
    assert len(derive_segments(atoms, apart)) == 2


def test_pairs_to_ask_only_differently_written_same_country_pairs_with_a_new_atom():
    atoms = [_a("n1", "Bukchon Hanok Village", country="South Korea"),
             _a("p1", "Jeonju Hanok Village", country="South Korea"),
             _a("p2", "Hanok Village", country="Japan"),
             _a("p3", "Bukchon Hanok Village", country="South Korea")]
    pairs = pairs_to_ask(atoms, {"n1"})
    assert list(pairs) == [place_pair("Bukchon Hanok Village", "Jeonju Hanok Village")]
    assert pairs_to_ask(atoms, set()) == {}


def test_mint_without_country_keeps_origin_formula():
    key = sm._key(_a("a", "Paro Dzong", "visit"))
    assert sm._mint(key) == sm._mint(key, "")
    assert sm._mint(key, "Bhutan") != sm._mint(key)
