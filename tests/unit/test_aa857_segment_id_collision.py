"""AA-857 (S239) — Segment id collisions across countries.

A Segment id is the mint (`sha256(place|verb|country)`) of the moment that FIRST created it, while
its label and members move on. A Thailand tour re-deriving "hotel / check in" mints
`e11cd23f51829202`, an id an India Segment holds; the India Segment is not in the Thai tour's pool
(other country), so `reconcile_ids` never saw the clash and the write path put Thai atoms into the
India Segment and relabelled it. Fixtures are synthetic (no tenant / customer data).
"""
import hashlib
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from services.acp_contract import segment_matching as sm
from services.acp_contract.segment_matching import (
    _PSEUDO_PREFIX, MixedCountrySegmentError, RemintExhausted, Segment, avoid_foreign_ids,
    live_segment_labels, remint_id, resolve_segment_aliases,
)
from services.acp_shared import slate

INDIA_ID = "e11cd23f51829202"            # live: India's "hotel / check in" Segment
THAI_TOUR = "22222222-2222-2222-2222-222222222222"
THAI_ATOM = "atom-th-hotel-1"


def follow_alias(alias_map, segment_id, limit=20):
    """The test fakes' model of `_ALIAS_CHAIN_SQL` (old -> canonical, to the end; a cycle stops)."""
    seen = 0
    while segment_id in alias_map and seen < limit:
        nxt = alias_map[segment_id]
        if nxt == segment_id:
            break
        segment_id, seen = nxt, seen + 1
    return segment_id


def _expected_remint(segment_id, country, n):
    # The S239 repair script's formula, written out — the durable code must match it byte for byte.
    return hashlib.sha256(f"{segment_id}|{country}|aa857|{n}".encode()).hexdigest()[:16]


# ── pure rule ────────────────────────────────────────────────────────────────────────────────────

def test_thai_hotel_check_in_mints_the_india_segment_id():
    """The premise: the collision is real for the deployed `_mint`."""
    atom = sm.SegmentAtom(THAI_ATOM, THAI_TOUR, 1, "hotel", "check in", "Thailand", "stay")
    assert sm._mint(sm._key(atom), atom.country) == INDIA_ID
    assert sm.forms_segment(atom)


def test_remint_id_is_the_repair_formula():
    assert remint_id(INDIA_ID, "thailand", 2) == _expected_remint(INDIA_ID, "thailand", 2)
    assert remint_id(INDIA_ID, "thailand", 2) != remint_id(INDIA_ID, "thailand", 3)


def _thai_segment():
    return Segment(INDIA_ID, "hotel", "check in", (THAI_ATOM,))


def test_foreign_unclaimed_id_is_reminted():
    segs, aliases, remints, _ = avoid_foreign_ids(
        [_thai_segment()], {}, claimed=set(), own_country=" Thailand ",
        countries_of={INDIA_ID: ["india"]}, alias_old=set(), taken={INDIA_ID})
    new = _expected_remint(INDIA_ID, "thailand", 2)
    assert [s.id for s in segs] == [new]
    assert segs[0].atom_ids == (THAI_ATOM,)
    assert remints == {INDIA_ID: {"to": new, "reason": "foreign:india"}}


def test_taken_remint_moves_to_the_next_n():
    taken = {INDIA_ID, _expected_remint(INDIA_ID, "thailand", 2)}
    segs, _, remints, _ = avoid_foreign_ids(
        [_thai_segment()], {}, set(), "Thailand", {INDIA_ID: ["india"]}, set(), taken)
    assert segs[0].id == _expected_remint(INDIA_ID, "thailand", 3)
    assert remints[INDIA_ID]["to"] == segs[0].id


def test_alias_old_id_is_reminted_and_alias_targets_follow():
    old = "aaaaaaaaaaaaaaaa"
    seg = Segment(INDIA_ID, "hotel", "check in", (THAI_ATOM, f"{_PSEUDO_PREFIX}{old}"))
    segs, aliases, remints, _ = avoid_foreign_ids(
        [seg], {old: INDIA_ID}, claimed={old}, own_country="Thailand",
        countries_of={}, alias_old={INDIA_ID}, taken={INDIA_ID, old})
    new = _expected_remint(INDIA_ID, "thailand", 2)
    assert remints[INDIA_ID]["reason"] == "alias_old"
    assert segs[0].id == new and aliases == {old: new}


@pytest.mark.parametrize("claimed, own, countries", [
    ({INDIA_ID}, "Thailand", ["india"]),      # the run's own atoms already sit there: not a fresh mint
    (set(), "", ["india"]),                    # unknown tour country never mixes
    (set(), "Thailand", ["thailand"]),         # same country: the shared-moment feature (AA-545)
    (set(), "Thailand", []),                   # no known member country
])
def test_id_is_kept(claimed, own, countries):
    segs, _, remints, existing = avoid_foreign_ids(
        [_thai_segment()], {}, claimed, own, {INDIA_ID: countries}, set(), {INDIA_ID},
        has_members={INDIA_ID})
    assert segs[0].id == INDIA_ID and remints == {}
    # reported as a join of an existing Segment unless the run claimed it (the repair's report too)
    assert existing == ([] if claimed else [INDIA_ID])


def test_unclaimed_existing_lists_only_ids_with_members():
    _, _, _, existing = avoid_foreign_ids(
        [_thai_segment()], {}, set(), "Thailand", {INDIA_ID: ["thailand"]}, set(), {INDIA_ID}, has_members=set())
    assert existing == []
    _, _, remints, existing = avoid_foreign_ids(
        [_thai_segment()], {}, set(), "Thailand", {INDIA_ID: ["india"]}, set(), {INDIA_ID},
        has_members={INDIA_ID})
    assert existing == [] and INDIA_ID in remints          # re-minted, so not a join


def test_pseudo_only_segment_is_not_a_write():
    seg = Segment(INDIA_ID, "hotel", "check in", (f"{_PSEUDO_PREFIX}x",))
    segs, _, remints, _ = avoid_foreign_ids([seg], {}, set(), "Thailand", {INDIA_ID: ["india"]}, set(), set())
    assert segs[0].id == INDIA_ID and remints == {}


def test_exhausted_probe_raises():
    taken = {INDIA_ID} | {_expected_remint(INDIA_ID, "thailand", n) for n in range(2, 4)}
    with pytest.raises(RemintExhausted):
        avoid_foreign_ids([_thai_segment()], {}, set(), "Thailand", {INDIA_ID: ["india"]}, set(), taken,
                          max_n=3)


def test_alias_chain_stops_at_an_id_with_members():
    """A revived alias old id (members written into it before AA-857) is live, not followed."""
    sql = " ".join(sm._ALIAS_CHAIN_SQL.split())
    assert "NOT EXISTS (SELECT 1 FROM acp_contract.atom_segment_member m WHERE m.segment_id = c.segment_id)" in sql


# ── run_segment_matching against a fake DB ───────────────────────────────────────────────────────

class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.rolled_back = exc_type is not None
        return False


class _Conn:
    """Dispatches the queries run_segment_matching sends; `segments` is the DB state of the ids the
    collision guard asks about: {segment_id: {"countries": [...], "alias_old": bool}}."""

    def __init__(self, *, assigned=(), existing=(), segments=None, mixed=(), mixed_before=()):
        self.assigned = list(assigned)
        self.existing = list(existing)
        self.segments = segments or {}
        self.mixed = list(mixed)                  # answer of the mixed check AFTER the writes
        self.mixed_before = list(mixed_before)    # ... and BEFORE them
        self.mixed_calls = 0
        self.calls = []
        self.facts_queries = 0
        self.rolled_back = None
        self.checked_ids = None

    async def fetch(self, sql, *args):
        if "ta.tour_id = $1" in sql:
            return [{"atom_id": THAI_ATOM, "tour_id": THAI_TOUR, "itinerary_day": 1, "place": "hotel",
                     "action": "check in", "activity_type": "stay", "country": "Thailand"}]
        if "SELECT atom_id, segment_id FROM acp_contract.atom_segment_member" in sql:
            return self.assigned
        if "FROM acp_contract.atom_segment asg" in sql:
            return self.existing
        if "AS x(segment_id)" in sql:
            self.facts_queries += 1
            return [{"segment_id": i, "taken": i in self.segments,
                     "alias_old": self.segments.get(i, {}).get("alias_old", False),
                     "has_members": self.segments.get(i, {}).get(
                         "has_members", bool(self.segments.get(i, {}).get("countries"))),
                     "countries": self.segments.get(i, {}).get("countries", [])} for i in args[0]]
        if "HAVING count(DISTINCT" in sql:
            self.checked_ids = list(args[0])
            self.mixed_calls += 1
            return self.mixed_before if self.mixed_calls == 1 else self.mixed
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
        return await sm.run_segment_matching(THAI_TOUR, _Pool(conn))


@pytest.mark.asyncio
async def test_thai_tour_does_not_land_in_or_relabel_the_india_segment():
    conn = _Conn(segments={INDIA_ID: {"countries": ["india"]}})
    out = await _run(conn)
    new = _expected_remint(INDIA_ID, "thailand", 2)
    upserts = conn.rows("INSERT INTO acp_contract.atom_segment (")
    members = conn.rows("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES")
    assert [r[0] for r in upserts] == [new]                     # India's row is never upserted
    assert upserts[0][3] is True                                # a fresh id: no row, no members yet
    assert members == [(new, THAI_ATOM)]
    assert all(INDIA_ID not in r for r in upserts + members)
    assert out["reminted"] == 1 and conn.rolled_back is False
    assert conn.checked_ids == [new]


@pytest.mark.asyncio
async def test_second_run_keeps_the_reminted_id():
    """Stability (ADR 0002): once the Thai atom sits in the re-minted Segment, a rerun claims it."""
    new = _expected_remint(INDIA_ID, "thailand", 2)
    conn = _Conn(
        assigned=[{"atom_id": THAI_ATOM, "segment_id": new}],
        existing=[{"segment_id": new, "canonical_place": "hotel", "canonical_action": "check in",
                   "country": "Thailand", "member_types": ["stay"]}],
        segments={INDIA_ID: {"countries": ["india"]}, new: {"countries": ["thailand"]}})
    out = await _run(conn)
    upserts = conn.rows("INSERT INTO acp_contract.atom_segment (")
    assert [(r[0], r[3]) for r in upserts] == [(new, True)]     # claimed: its label may follow its atoms
    assert conn.rows("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES") == [
        (new, THAI_ATOM)]
    assert out["reminted"] == 0 and conn.facts_queries == 0     # nothing unclaimed to look up


@pytest.mark.asyncio
async def test_same_country_unclaimed_id_is_joined_but_not_relabelled():
    conn = _Conn(segments={INDIA_ID: {"countries": ["thailand"]}})
    out = await _run(conn)
    upserts = conn.rows("INSERT INTO acp_contract.atom_segment (")
    assert [(r[0], r[3]) for r in upserts] == [(INDIA_ID, False)]
    assert "WHERE $4::boolean" in next(sql for sql, _ in conn.calls
                                       if sql.startswith("INSERT INTO acp_contract.atom_segment ("))
    assert out["reminted"] == 0
    assert out["unclaimed_existing"] == [INDIA_ID]              # the join is visible in the run log


@pytest.mark.asyncio
async def test_empty_existing_segment_is_relabelled():
    """An existing row with no members (emptied by the AA-695 excluded-atom DELETE) has nothing to
    protect: the run that fills it gives it its own label, as the old upsert did."""
    conn = _Conn(segments={INDIA_ID: {"countries": [], "has_members": False}})
    out = await _run(conn)
    upserts = conn.rows("INSERT INTO acp_contract.atom_segment (")
    assert [(r[0], r[1], r[3]) for r in upserts] == [(INDIA_ID, "hotel", True)]
    assert out["reminted"] == 0 and out["unclaimed_existing"] == []


@pytest.mark.asyncio
async def test_already_mixed_segment_does_not_block_a_run_that_adds_no_country():
    """Damage that predates the run (repair not applied yet) is reported, not raised."""
    before = [{"segment_id": INDIA_ID, "countries": ["india", "thailand"]}]
    conn = _Conn(segments={INDIA_ID: {"countries": ["india", "thailand"]}},
                 assigned=[{"atom_id": THAI_ATOM, "segment_id": INDIA_ID}],   # the Thai atom already sits there
                 mixed_before=before, mixed=before)
    out = await _run(conn)
    assert conn.rolled_back is False and conn.mixed_calls == 2
    assert out["already_mixed"] == [INDIA_ID]


@pytest.mark.asyncio
async def test_already_mixed_segment_that_gains_a_country_still_raises():
    before = [{"segment_id": INDIA_ID, "countries": ["india", "nepal"]}]
    after = [{"segment_id": INDIA_ID, "countries": ["india", "nepal", "thailand"]}]
    conn = _Conn(segments={}, assigned=[{"atom_id": THAI_ATOM, "segment_id": INDIA_ID}],
                 mixed_before=before, mixed=after)
    with pytest.raises(MixedCountrySegmentError, match="thailand"):
        await _run(conn)
    assert conn.rolled_back is True


def test_segment_upsert_rows_are_written_in_id_order():
    """The upsert row-locks every written id; one global order keeps two concurrent runs from
    deadlocking on overlapping ids."""
    import inspect
    src = inspect.getsource(sm.run_segment_matching)
    assert "sorted(segment_rows.values(), key=lambda row: row[0])" in src


@pytest.mark.asyncio
async def test_mixed_country_write_raises_and_rolls_back():
    conn = _Conn(segments={}, mixed=[{"segment_id": INDIA_ID, "countries": ["india", "thailand"]}])
    with pytest.raises(MixedCountrySegmentError, match=INDIA_ID):
        await _run(conn)
    assert conn.rolled_back is True
    assert conn.checked_ids == [INDIA_ID]          # the check ran over the ids just written


# ── Subjects on alias old ids ────────────────────────────────────────────────────────────────────

class _AliasConn:
    def __init__(self, alias, labels=None, subjects=()):
        self.alias = alias            # old -> canonical
        self.labels = labels or {}
        self.subjects = list(subjects)
        self.executed = []

    async def fetch(self, sql, *args):
        if "FROM acp_shared.subject" in sql:      # first: since AA-986 L3 the Slate query names atom_segment_member
            return self.subjects
        if "atom_segment_alias" in sql:
            return [{"start_id": i, "segment_id": follow_alias(self.alias, i)}
                    for i in args[0] if i in self.alias]
        if "FROM acp_contract.atom_segment" in sql:
            return [{"segment_id": i, "canonical_place": self.labels[i][0], "canonical_action": self.labels[i][1]}
                    for i in args[0] if i in self.labels]
        if "FROM acp_shared.subject" in sql:
            return self.subjects
        raise AssertionError(sql[:80])

    async def fetchval(self, sql, *args):
        return None                    # no channel config: every Channel enabled

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    def transaction(self):
        return _Tx(self)


@pytest.mark.asyncio
async def test_resolve_segment_aliases_follows_the_chain():
    conn = _AliasConn({"old1": "mid", "mid": "live"})
    assert await resolve_segment_aliases(conn, ["old1", "live", None, ""]) == {"old1": "live", "live": "live"}
    assert await resolve_segment_aliases(conn, [None]) == {}


@pytest.mark.asyncio
async def test_live_segment_labels_only_for_moved_ids():
    conn = _AliasConn({"old1": "live1", "old2": "gone"}, labels={"live1": ("Delhi", "explore")})
    assert await live_segment_labels(conn, ["old1", "old2", "same", None]) == {
        "old1": ("live1", "Delhi", "explore"), "old2": ("gone", None, None)}
    assert await live_segment_labels(conn, ["same"]) == {}


def _subject_row(segment_id, state="picked", channel="facebook"):
    return {"subject_id": uuid4(), "channel": channel, "state": state, "score": 1.0,
            "cleared_bar_reason": "{}", "segment_id": segment_id, "route_id": None, "created_at": None,
            "canonical_place": "old label", "canonical_action": "visit", "hub_name": None,
            "ordered_segment_ids": None, "tour_id": None}


@pytest.mark.asyncio
async def test_fetch_slate_shows_a_subject_on_its_live_id():
    conn = _AliasConn({"old1": "live1"}, labels={"live1": ("Delhi", "explore")},
                      subjects=[_subject_row("old1"), _subject_row("other", state="proposed")])
    out = await slate.fetch_slate(uuid4(), _Pool(conn))
    subjects = {s["segment_id"]: s for s in out["facebook"]["subjects"]}
    assert set(subjects) == {"live1", "other"}
    assert (subjects["live1"]["place"], subjects["live1"]["action"]) == ("Delhi", "explore")
    assert subjects["other"]["place"] == "old label"


@pytest.mark.asyncio
async def test_pick_subject_seeds_from_the_live_id():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"subject_id": uuid4(), "channel": "facebook", "state": "proposed", "segment_id": "old1", "route_id": None},
        {"request_id": uuid4(), "tenant_id": uuid4(), "atom_id": "a1", "trip_id": None, "channel": "facebook",
         "status": "pending_goal", "created_at": None, "route_segment_ids": None},
    ]
    conn.fetch.side_effect = lambda sql, ids: [{"start_id": "old1", "segment_id": "live1"}]
    conn.transaction = lambda: _Tx(conn)
    resolve = AsyncMock(return_value=("a1", None, None))
    with patch.object(slate, "_resolve_representative_atom", resolve), \
         patch.object(slate, "write_audit_log", AsyncMock()):
        await slate.pick_subject(uuid4(), uuid4(), _Pool(conn), selected_by="tenant:x")
    assert resolve.await_args.args[2] == "live1"


@pytest.mark.asyncio
async def test_propose_slate_does_not_duplicate_a_subject_held_on_an_old_id():
    conn = _AliasConn({"old1": "seg_1"}, subjects=[{"channel": "facebook", "segment_id": "old1"}])
    candidate = slate.Candidate(segment_id="seg_1", route_id=None, score=1.0, demand=None, questions=0,
                                said=200, place="Place 1", action="visit")
    with patch.object(slate, "_tenant_market_codes", AsyncMock(return_value=(["US"], []))), \
         patch.object(slate, "_tenant_enabled_channels", AsyncMock(return_value=["facebook", "linkedin"])), \
         patch.object(slate, "_fetch_segment_candidates", AsyncMock(return_value=[candidate])), \
         patch.object(slate, "_fetch_route_candidates", AsyncMock(return_value=[])), \
         patch.object(slate, "_fetch_segment_hub_map", AsyncMock(return_value={})), \
         patch("services.acp_shared.debate.apply_debate", AsyncMock(side_effect=lambda _t, c, _c: c)):
        await slate.propose_slate(uuid4(), _Pool(conn))
    inserted = [(args[2], args[1]) for sql, args in conn.executed
                if sql.lstrip().startswith("INSERT INTO acp_shared.subject")]
    assert ("facebook", "seg_1") not in inserted     # held via old1 on facebook
    assert ("linkedin", "seg_1") in inserted         # another Channel is still free
