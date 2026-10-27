"""AA-988 (S253) — every derived Segment gets its own id.

`_mint` hashes `place|verb|country`, coarser than the join rule (`_looks_like_one_moment` also needs
`_about_the_same_thing`). Live on Dev: "Hutong / walk through to see the local lifestyle of old
Beijing" (a new atom) and the existing Segment "the Hutong / stroll through" are two moments with one
mint (`cf8f846e8ae2fd08`); `reconcile_ids` kept only the last Segment per id, so the atom got no
member row (227 atoms in 157 tours). With the atom id sorting after the pseudo prefix, the atom's
Segment won instead: glued into the existing Segment and relabelled. Fixtures are synthetic text in
the shape of the live rows (no tenant / customer data).
"""
from unittest.mock import AsyncMock, patch

import pytest

from services.acp_contract import segment_matching as sm
from services.acp_contract.segment_matching import (
    _PSEUDO_PREFIX, DuplicateSegmentIdError, Segment, SegmentAtom, SegmentCoverageError, _key, _mint,
    _plan_writes, assert_each_atom_in_one_segment, derive_segments, reconcile_ids,
)

HUTONG_ID = "cf8f846e8ae2fd08"                 # the existing Segment's id (= its own mint)
HUTONG_PSEUDO = f"{_PSEUDO_PREFIX}{HUTONG_ID}"
TARGET = "291a2ca8deb4b991"                    # sorts BEFORE the pseudo prefix: used to be dropped
REVERSE = "e91a2ca8deb4b991"                   # sorts AFTER the pseudo prefix: used to be glued in
TOUR = "33333333-3333-3333-3333-333333333333"
WALK = "walk through to see the local lifestyle of old Beijing"


def _target(atom_id=TARGET):
    return SegmentAtom(atom_id, TOUR, 3, "Hutong", WALK, "China", "culture")


def _pseudo(segment_id=HUTONG_ID):
    return SegmentAtom(f"{_PSEUDO_PREFIX}{segment_id}", "", None, "the Hutong", "stroll through", "China")


def _run_pure(pool, assigned=None, involving=None):
    """derive -> reconcile -> plan, the pure part of `run_segment_matching`."""
    if assigned is None:
        assigned = {a.atom_id: a.atom_id[len(_PSEUDO_PREFIX):] for a in pool if a.atom_id.startswith(_PSEUDO_PREFIX)}
    derived = derive_segments(pool, involving=involving)
    segments, aliases = reconcile_ids(derived, assigned)
    return derived, segments, aliases, _plan_writes(segments, aliases)


def _holding(segments, atom_id):
    return [s for s in segments if atom_id in s.atom_ids]


# ── the premise ─────────────────────────────────────────────────────────────────────────────────

def test_hutong_premise_one_mint_two_moments():
    target, pseudo = _key(_target()), _key(_pseudo())
    assert _mint(target, "China") == _mint(pseudo, "China") == HUTONG_ID
    assert not sm._looks_like_one_moment(target, pseudo)


# ── 1. unique ids ───────────────────────────────────────────────────────────────────────────────

def test_hutong_target_and_pseudo_get_two_ids_and_both_are_kept():
    derived, segments, aliases, (to_write, alias_rows, segment_rows) = _run_pure(
        [_target(), _pseudo()], involving=frozenset({TARGET}))
    assert len(derived) == 2 and len({s.id for s in derived}) == 2
    assert len(segments) == 2 and aliases == {}
    [existing] = _holding(segments, HUTONG_PSEUDO)
    [mine] = _holding(segments, TARGET)
    assert existing.id == HUTONG_ID                   # the pseudo keeps its own id
    assert (existing.place, existing.action) == ("the Hutong", "stroll through")
    assert mine.id != HUTONG_ID
    assert [s.atom_ids for s in to_write] == [(TARGET,)]
    assert alias_rows == [] and HUTONG_ID not in segment_rows    # the existing row is not touched
    assert_each_atom_in_one_segment(to_write, [TARGET])


def test_same_tour_dinner_and_breakfast_are_two_segments_both_kept():
    pool = [SegmentAtom("h1", TOUR, 1, "Hanoi", "have dinner", "Vietnam"),
            SegmentAtom("h2", TOUR, 2, "Hanoi", "eat breakfast", "Vietnam")]
    base = _mint(_key(pool[0]), "Vietnam")
    assert base == _mint(_key(pool[1]), "Vietnam")    # the collision is real
    derived, segments, _, (to_write, _, _) = _run_pure(pool, assigned={})
    assert sorted(s.atom_ids for s in segments) == [("h1",), ("h2",)]
    assert len({s.id for s in segments}) == 2 and base not in {s.id for s in segments}
    assert_each_atom_in_one_segment(to_write, ["h1", "h2"])

    # Same atoms in, same ids out; and the next run (atoms assigned, their Segments in the pool as
    # pseudo atoms with their stored label) keeps both ids without aliases (ADR 0002).
    assert [s.id for s in derive_segments(pool)] == [s.id for s in derived]
    assigned = {a: s.id for s in segments for a in s.atom_ids}
    rerun = pool + [SegmentAtom(f"{_PSEUDO_PREFIX}{s.id}", "", None, s.place, s.action, "Vietnam") for s in segments]
    for s in segments:
        assigned[f"{_PSEUDO_PREFIX}{s.id}"] = s.id
    _, again, aliases, (to_write2, _, _) = _run_pure(rerun, assigned=assigned, involving=frozenset({"h1", "h2"}))
    assert {s.id: tuple(a for a in s.atom_ids if not a.startswith(_PSEUDO_PREFIX)) for s in again} == {
        s.id: s.atom_ids for s in segments}
    assert aliases == {}
    assert_each_atom_in_one_segment(to_write2, ["h1", "h2"])


def test_reverse_order_atom_is_not_given_the_existing_id():
    """Atom id `e...` sorts after `__existing_segment__`: its Segment used to survive under the
    existing id (written into it, the existing label replaced by the atom's)."""
    _, segments, aliases, (to_write, alias_rows, segment_rows) = _run_pure(
        [_target(REVERSE), _pseudo()], involving=frozenset({REVERSE}))
    [existing] = _holding(segments, HUTONG_PSEUDO)
    [mine] = _holding(segments, REVERSE)
    assert existing.id == HUTONG_ID
    assert (existing.place, existing.action) == ("the Hutong", "stroll through")
    assert mine.id != HUTONG_ID and existing is not mine
    assert [(s.id, s.atom_ids) for s in to_write] == [(mine.id, (REVERSE,))]
    assert HUTONG_ID not in segment_rows and alias_rows == [] and aliases == {}


def test_pseudo_with_an_old_formula_id_gets_its_stored_id_back():
    """An existing Segment whose stored id is not its own mint (pre-AA-545 / AA-857 re-mint) owns no
    derived id: both colliding Segments are re-minted and the pseudo reclaims its id via `_claims`."""
    stored = "0123456789abcdef"
    _, segments, aliases, (to_write, _, segment_rows) = _run_pure(
        [_target(), _pseudo(stored)], involving=frozenset({TARGET}))
    [existing] = _holding(segments, f"{_PSEUDO_PREFIX}{stored}")
    [mine] = _holding(segments, TARGET)
    assert existing.id == stored
    assert mine.id not in (stored, HUTONG_ID)
    assert aliases == {} and stored not in segment_rows
    assert_each_atom_in_one_segment(to_write, [TARGET])


def _own_pseudo(place, action, country="China"):
    """A pseudo atom whose stored id is its own `_mint` (an AA-545+ Segment)."""
    stored = _mint(_key(SegmentAtom("x", "", None, place, action, country)), country)
    return stored, SegmentAtom(f"{_PSEUDO_PREFIX}{stored}", "", None, place, action, country)


def _assert_lone_atom_not_given_the_stored_id(pool, stored, lone):
    """The pseudo joins a new atom with a shorter canonical key (its component mints another id),
    and `lone` — not that moment — mints the stored id on its own. It used to keep it: the stored id
    was aliased to the joined Segment and `lone` was written into it, relabelled (review S253)."""
    news = frozenset(a.atom_id for a in pool if not a.atom_id.startswith(_PSEUDO_PREFIX))
    derived, segments, aliases, (to_write, alias_rows, segment_rows) = _run_pure(pool, involving=news)
    [mine] = _holding(derived, lone)
    assert mine.atom_ids == (lone,) and mine.id != stored
    [existing] = _holding(segments, f"{_PSEUDO_PREFIX}{stored}")
    assert existing.id == stored and lone not in existing.atom_ids
    assert aliases == {} and alias_rows == []
    assert not any(old == mine.id or new == mine.id for old, new in alias_rows)
    [written] = [s for s in to_write if lone in s.atom_ids]
    assert written.id == mine.id != stored
    assert_each_atom_in_one_segment(to_write, sorted(news))
    return mine, existing, segment_rows


def test_lone_atom_does_not_take_a_pseudo_id_its_joined_segment_minted_away():
    stored, pseudo = _own_pseudo("Old Hutong", "stroll through the lanes")
    pool = [pseudo,
            SegmentAtom("n1", TOUR, 1, "Hutong", "stroll through the lanes", "China"),
            SegmentAtom("h", TOUR, 2, "Old Hutong", "walk to see the market", "China")]
    assert _mint(_key(pool[2]), "China") == stored                    # the premise: H mints B alone
    assert _mint(_key(pool[1]), "China") != stored                    # {N1, pseudo} mints another id
    mine, existing, _ = _assert_lone_atom_not_given_the_stored_id(pool, stored, "h")
    assert (mine.place, mine.action) == ("Old Hutong", "walk to see the market")
    assert existing.atom_ids == (pseudo.atom_id, "n1")


def test_lone_atom_does_not_take_the_id_of_a_pseudo_that_joined_a_shorter_key():
    stored, pseudo = _own_pseudo("Hutong alley", "stroll through")
    pool = [pseudo,
            SegmentAtom("n1", TOUR, 1, "Hutong", "stroll", "China"),
            SegmentAtom("b", TOUR, 2, "Hutong alley", WALK, "China")]
    assert _mint(_key(pool[2]), "China") == stored
    _assert_lone_atom_not_given_the_stored_id(pool, stored, "b")


def test_remint_skips_another_pseudos_stored_id():
    """A stored id may itself be an earlier `_mint_specific`; a re-mint must not land on it."""
    stored, pseudo = _own_pseudo("Old Hutong", "stroll through the lanes")
    lone = SegmentAtom("h", TOUR, 2, "Old Hutong", "walk to see the market", "China")
    taken = sm._mint_specific(_key(lone), "China", 0)
    other = SegmentAtom(f"{_PSEUDO_PREFIX}{taken}", "", None, "Temple of Heaven", "visit the altar", "China")
    pool = [pseudo, other, SegmentAtom("n1", TOUR, 1, "Hutong", "stroll through the lanes", "China"), lone]
    mine, _, _ = _assert_lone_atom_not_given_the_stored_id(pool, stored, "h")
    assert mine.id not in (stored, taken)
    assert mine.id == sm._mint_specific(_key(lone), "China", 1)


def test_empty_action_atoms_at_one_place_get_distinct_ids():
    """Identical full keys that never join (no verb) are told apart by `n`, deterministically."""
    pool = [SegmentAtom("a1", "t", 1, "the market", ""), SegmentAtom("a2", "t", 1, "the market", "")]
    first = derive_segments(pool)
    assert len({s.id for s in first}) == 2
    assert [(s.id, s.atom_ids) for s in derive_segments(list(reversed(pool)))] == [
        (s.id, s.atom_ids) for s in first]


# ── determinism: non-colliding ids are the plain mint, byte for byte ───────────────────────────

GOLDEN = [   # computed on main before AA-988 S253 (the same fixtures as test_aa509 / test_aa857)
    ([("a1", "tour1", 1, "Magome", "walk to Tsumago", ""), ("a2", "tour1", 2, "Halong Bay", "kayak the caves", "")],
     [("d7a5e2bacaa2f13b", ("a1",)), ("e20cfe14ad0c10d0", ("a2",))]),
    ([("a1", "tour1", 1, "the ryokan", "eat dinner", ""), ("a2", "tour2", 1, "the ryokan", "have dinner", "")],
     [("8f616ae5e350aae3", ("a1", "a2"))]),
    ([("a1", "tour1", 1, "the old temple", "visit", ""), ("a2", "tour1", 1, "the old temple", "eat lunch", "")],
     [("5a93b392feba9649", ("a1",)), ("6db9feef58d3e2f6", ("a2",))]),
    ([("x", "T", 1, "hotel", "check in", "Thailand")], [("e11cd23f51829202", ("x",))]),
]


@pytest.mark.parametrize("atoms,expected", GOLDEN)
def test_non_colliding_ids_are_unchanged(atoms, expected):
    pool = [SegmentAtom(*a) for a in atoms]
    assert [(s.id, s.atom_ids) for s in derive_segments(pool)] == expected


# ── 2. guard in reconcile_ids ───────────────────────────────────────────────────────────────────

def test_reconcile_ids_raises_on_duplicate_ids():
    derived = [Segment("dup", "Hutong", "walk", ("a1",)), Segment("dup", "the Hutong", "stroll", ("a2",))]
    with pytest.raises(DuplicateSegmentIdError, match="dup"):
        reconcile_ids(derived, {})
    assert issubclass(DuplicateSegmentIdError, ValueError)


# ── 3. coverage invariant ───────────────────────────────────────────────────────────────────────

def test_coverage_invariant_raises_on_missing_and_repeated_atoms():
    with pytest.raises(SegmentCoverageError, match="missing 1"):
        assert_each_atom_in_one_segment([Segment("s1", "p", "a", ("a1",))], ["a1", "a2"])
    with pytest.raises(SegmentCoverageError, match="in several 1"):
        assert_each_atom_in_one_segment(
            [Segment("s1", "p", "a", ("a1",)), Segment("s2", "p", "b", ("a1",))], ["a1"])


def _a(atom_id, place, action, country="", tour="t1", day=1):
    return SegmentAtom(atom_id, tour, day, place, action, country)


FIXTURE_POOLS = [
    [_a("a1", "the old temple", "visit"), _a("a2", "the old temple", "eat lunch")],
    [_a("a1", "the ryokan", "eat dinner"), _a("a2", "the ryokan", "have dinner", tour="t2")],
    [_a("a1", "Magome", "walk the Nakasendo trail"), _a("a2", "Narai", "walk the village street", day=2)],
    [_a("a1", "the grand old temple", "visit"), _a("a2", "the sacred temple ruins", "visit", tour="t2"),
     _a("a3", "temple", "visit", tour="t3")],
    [_a("a1", "the market", ""), _a("a2", "the market", ""), _a("a3", "the market", "browse the stalls")],
    [_a("h1", "Hanoi", "have dinner", "Vietnam"), _a("h2", "Hanoi", "eat breakfast", "Vietnam", day=2)],
    [_target(), _pseudo()],
    [_target(REVERSE), _pseudo()],
    [_target(), _target(REVERSE), _pseudo(), _a("k1", "Gyeongju", "visit", "Korea"),
     _a("k2", "Gyeongju", "visit", "Bhutan", tour="t2")],
]


@pytest.mark.parametrize("pool", FIXTURE_POOLS)
def test_every_real_atom_lands_in_exactly_one_segment(pool):
    real = [a.atom_id for a in pool if not a.atom_id.startswith(_PSEUDO_PREFIX)]
    derived, _, _, (to_write, _, _) = _run_pure(pool, involving=frozenset(real))
    assert len({s.id for s in derived}) == len(derived)
    assert_each_atom_in_one_segment(to_write, real)


# ── run_segment_matching against a fake DB ──────────────────────────────────────────────────────

class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.rolled_back = exc_type is not None
        return False


class _Conn:
    def __init__(self, atom_id):
        self.atom_id = atom_id
        self.calls = []
        self.rolled_back = None

    async def fetch(self, sql, *args):
        if "ta.tour_id = $1" in sql:
            return [{"atom_id": self.atom_id, "tour_id": TOUR, "itinerary_day": 3, "place": "Hutong",
                     "action": WALK, "activity_type": "culture", "country": "China"}]
        if "SELECT atom_id, segment_id FROM acp_contract.atom_segment_member" in sql:
            return []
        if "FROM acp_contract.atom_segment asg" in sql:
            return [{"segment_id": HUTONG_ID, "canonical_place": "the Hutong", "canonical_action": "stroll through",
                     "country": "China", "member_types": ["culture"]}]
        if "AS x(segment_id)" in sql:
            return [{"segment_id": i, "taken": i == HUTONG_ID, "alias_old": False, "has_members": i == HUTONG_ID,
                     "countries": ["china"] if i == HUTONG_ID else []} for i in args[0]]
        if "HAVING count(DISTINCT" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql[:80]}")

    async def executemany(self, sql, rows):
        self.calls.append((" ".join(sql.split()), list(rows)))

    async def execute(self, sql, *args):
        self.calls.append((" ".join(sql.split()), [args]))

    def transaction(self):
        return _Tx(self)

    def rows(self, prefix):
        return [r for sql, rows in self.calls if sql.startswith(prefix) for r in rows]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


async def _run(conn):
    with patch.object(sm, "_apart_pairs", AsyncMock(return_value=(frozenset(), 0))):
        return await sm.run_segment_matching(TOUR, _Pool(conn))


MEMBER_INSERT = "INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES"
SEGMENT_UPSERT = "INSERT INTO acp_contract.atom_segment ("


@pytest.mark.asyncio
@pytest.mark.parametrize("atom_id", [TARGET, REVERSE])
async def test_run_writes_the_atom_and_leaves_the_existing_segment_alone(atom_id):
    conn = _Conn(atom_id)
    out = await _run(conn)
    members = conn.rows(MEMBER_INSERT)
    upserts = conn.rows(SEGMENT_UPSERT)
    assert len(members) == 1 and members[0][1] == atom_id
    new_id = members[0][0]
    assert new_id != HUTONG_ID
    assert [r[0] for r in upserts] == [new_id]                  # the Hutong row is never upserted
    assert upserts[0][1:3] == ("Hutong", WALK)
    assert out["segments_written"] == 1 and conn.rolled_back is False


@pytest.mark.asyncio
async def test_run_raises_and_rolls_back_when_an_atom_would_be_lost():
    conn = _Conn(TARGET)
    with patch.object(sm, "reconcile_ids", lambda derived, assigned: ([], {})):
        with pytest.raises(SegmentCoverageError, match=TARGET):
            await _run(conn)
    assert conn.rolled_back is True
    assert conn.rows(MEMBER_INSERT) == [] and conn.rows(SEGMENT_UPSERT) == []
