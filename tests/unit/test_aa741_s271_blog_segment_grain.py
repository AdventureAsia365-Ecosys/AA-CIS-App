"""AA-741 S271 — Blog is a Segment Subject that carries its Route as the walk (option A).

Phase-1 finding (06/11, 4 audit tenants): every agency got one or two Blog Subjects ever. Blog was the one Route-grain
Channel: its pool was the current Routes of the tenant's tours (one or two per tour), and a used Route never came back
(`idx_subject_unique_route` + the `ON CONFLICT ... WHERE state = 'proposed'` upsert). The origin's Blog Subject is a
Segment carrying its Route as `walk` material. Now Blog proposes Segments under the same Bar (demand >= 1000,
questions >= 3, best market), keeps the place rule, never re-proposes a place the tenant already picked / used / cut
on Blog, and the pick fills `route_segment_ids` with the walk so T8 / T9 keep the journey spine.

Pure tests: no DB, no LLM. Invented places and ids only.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_shared import slate
from services.acp_shared.slate import CHANNEL_BARS, Candidate, HeldSubjects, _choose_slate
from services.acp_shared.subject_seed import Member

TOUR_A = "0a0a0a0a-0000-4000-8000-0000000000a1"
TOUR_B = "0b0b0b0b-0000-4000-8000-0000000000b2"
TOUR_C = "0c0c0c0c-0000-4000-8000-0000000000c3"
ALL_CHANNELS = list(CHANNEL_BARS)


def _seg(sid: str, place: str, score: float, demand: int | None = 2400, questions: int = 6,
         said: int = 300) -> Candidate:
    return Candidate(segment_id=sid, route_id=None, score=score, demand=demand, questions=questions, said=said,
                     place=place, action="visit")


def _ids(pairs) -> list[str]:
    return [c.segment_id or c.route_id for c, _ in pairs]


# ── the Channel row ────────────────────────────────────────────────────────────────────────────────

def test_blog_is_segment_grain_and_takes_its_route():
    spec = CHANNEL_BARS["blog"]
    assert spec["grain"] == "segment" and spec["takes_route"] is True and spec["one_piece_per_place"] is True
    assert (spec["needs_demand"], spec["needs_questions"]) == (1000, 3)
    # no other Channel walks a Route or holds a place
    for channel, other in CHANNEL_BARS.items():
        if channel != "blog":
            assert other["takes_route"] is False and other["one_piece_per_place"] is False
    assert all(s["grain"] == "segment" for s in CHANNEL_BARS.values())


# ── proposals: more than one Blog Subject per tenant ─────────────────────────────────────────────

def _three_tour_segments() -> list[Candidate]:
    """Three tours, two Bar-clearing moments each (six places), plus moments that only clear the social Bar."""
    blog_ready = [
        _seg("ta1", "Lake Aurel", 3), _seg("ta2", "Corvin Pass", 7),
        _seg("tb1", "Mirsa Gorge", 4), _seg("tb2", "Old Velk Market", 9),
        _seg("tc1", "Taluun Steppe", 5), _seg("tc2", "Ostra Monastery", 11),
    ]
    social_only = [_seg(f"s{i}", f"Village {i}", 20 + i, demand=120, questions=1) for i in range(6)]
    return blog_ready + social_only


def test_three_tours_give_at_least_three_blog_subjects_with_every_channel_on():
    chosen = _choose_slate(ALL_CHANNELS, _three_tour_segments(), [], hub_of={})
    assert len(chosen["blog"]) >= 3
    places = [c.place for c, _ in chosen["blog"]]
    assert len(places) == len(set(places))                     # one place once on Blog
    assert all(c.demand >= 1000 and c.questions >= 3 for c, _ in chosen["blog"])


def test_blog_bar_still_applies_to_each_segment():
    chosen = _choose_slate(["blog"], [_seg("x1", "Lake Aurel", 1, demand=999, questions=9),
                                      _seg("x2", "Corvin Pass", 2, demand=5000, questions=2),
                                      _seg("x3", "Mirsa Gorge", 3, demand=None, questions=9),
                                      _seg("x4", "Taluun Steppe", 4, demand=1000, questions=3)], [], hub_of={})
    assert _ids(chosen["blog"]) == ["x4"]


def test_blog_never_takes_routes_any_more():
    route = Candidate(segment_id=None, route_id="r1", score=0, demand=9000, questions=9, said=0, hub_id="h1")
    chosen = _choose_slate(["blog"], [_seg("ta1", "Lake Aurel", 3)], [route], hub_of={})
    assert _ids(chosen["blog"]) == ["ta1"]


# ── a place the tenant already holds on Blog is not proposed again ──────────────────────────────────

def test_a_used_place_is_not_reproposed_for_blog_under_another_segment_id():
    segments = [_seg("ta1-new", "Lake Aurel", 1), _seg("tb1", "Mirsa Gorge", 2)]
    held = {"blog": HeldSubjects(frozenset({"ta1"}), frozenset({"lake aurel"}))}
    chosen = _choose_slate(["blog", "linkedin"], segments, [], hub_of={}, held=held)
    assert _ids(chosen["blog"]) == ["tb1"]
    # the hold is Blog's own: LinkedIn still gets the place
    assert "ta1-new" in _ids(chosen["linkedin"])


def test_a_held_segment_takes_no_seat_and_no_cross_channel_slot():
    # Before S271 the used Segment kept winning its seat (its insert a no-op), so the next place never came.
    segments = [_seg("ta1", "Lake Aurel", 1), _seg("tb1", "Mirsa Gorge", 2)]
    held = {"blog": HeldSubjects(frozenset({"ta1"}), frozenset())}
    chosen = _choose_slate(["blog", "linkedin", "facebook", "instagram"], segments, [], hub_of={},
                           max_channels_per_place=3, held=held)
    assert _ids(chosen["blog"]) == ["tb1"]
    per_channel_lake = [c for c in ("linkedin", "facebook", "instagram") if "ta1" in _ids(chosen[c])]
    assert len(per_channel_lake) == 3        # Blog's hold used none of Lake Aurel's three seats


def test_held_place_match_ignores_case_and_spaces():
    held = HeldSubjects(frozenset(), frozenset({slate._place_key("  Lake   AUREL ")}))
    assert held.holds(_seg("z", "lake aurel", 1))
    assert not held.holds(_seg("z", "Lake Aurel North", 1))


class _HeldConn:
    """Fake connection for propose_slate(): tenant_config, the held-subject read, alias resolution, writes."""

    def __init__(self, held_rows=(), aliases=None, labels=None):
        self.held_rows = list(held_rows)
        self.aliases = dict(aliases or {})
        self.labels = dict(labels or {})
        self.executed: list[tuple] = []
        self.held_args = None

    async def fetchval(self, sql, *args):
        assert "acp_shared.tenant_config" in sql
        return ["blog", "linkedin"]

    async def fetch(self, sql, *args):
        if "WITH RECURSIVE chain" in sql:
            return [{"start_id": s, "segment_id": self.aliases.get(s, s)} for s in args[0]]
        if "FROM acp_contract.atom_segment" in sql and "canonical_action" in sql:
            return [{"segment_id": s, "canonical_place": self.labels[s], "canonical_action": "visit"}
                    for s in args[0] if s in self.labels]
        if "s.state <> 'proposed'" in sql and "s.channel = ANY" in sql:     # _HELD_SUBJECTS_SQL
            self.held_args = args
            return [r for r in self.held_rows if r["channel"] in args[1]]
        assert "FROM acp_shared.subject" in sql                            # _segments_held_via_alias
        return []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    @asynccontextmanager
    async def transaction(self):
        yield


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture(autouse=True)
def _blog_seed_floor_off(monkeypatch):
    """S279 thresholds L1: the Blog minimum seed is the setting cis.t7.blog_min_seed_words, seeded 600 (the Dev value);
    these tests predate it and run with the floor off."""
    from tests.unit.test_aa1081_blog_topic_coverage import set_min_seed
    set_min_seed(monkeypatch, 0)


async def _propose(conn, segments, thin=None):
    with patch.object(slate, "_tenant_market_codes", AsyncMock(return_value=(["US"], []))), \
         patch.object(slate, "_fetch_thin_walk_segments", AsyncMock(return_value=thin or {})), \
         patch.object(slate, "_fetch_segment_candidates", AsyncMock(return_value=segments)), \
         patch.object(slate, "_fetch_route_candidates", AsyncMock(return_value=[])) as routes, \
         patch.object(slate, "_fetch_segment_hub_map", AsyncMock(return_value={})), \
         patch("services.acp_shared.debate.apply_debate",
               AsyncMock(side_effect=lambda _tid, candidates, _conn: candidates)):
        result = await slate.propose_slate(uuid4(), _Pool(conn))
    return result, routes


def _inserted(conn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sql, args in conn.executed:
        if sql.lstrip().startswith("INSERT INTO acp_shared.subject"):
            out.setdefault(args[2], []).append(args[1])
    return out


@pytest.mark.asyncio
async def test_propose_skips_the_held_blog_place_and_reads_only_blog_holds():
    conn = _HeldConn(held_rows=[{"channel": "blog", "segment_id": "old-ta1", "canonical_place": "Lake Aurel"}])
    segments = [_seg("ta1", "Lake Aurel", 1), _seg("tb1", "Mirsa Gorge", 2), _seg("tc1", "Taluun Steppe", 3)]
    result, routes = await _propose(conn, segments)
    inserted = _inserted(conn)
    assert inserted["blog"] == ["tb1", "tc1"]
    assert "ta1" in inserted["linkedin"]
    assert conn.held_args[1] == ["blog"]           # only the one_piece_per_place Channels are read
    routes.assert_not_awaited()                    # no Route-grain Channel is enabled any more
    assert result["proposed_per_channel"]["blog"] == 2


@pytest.mark.asyncio
async def test_a_held_subject_on_a_merged_id_holds_the_live_segment_and_its_place():
    conn = _HeldConn(held_rows=[{"channel": "blog", "segment_id": "gone-1", "canonical_place": None}],
                     aliases={"gone-1": "live-1"}, labels={"live-1": "Corvin Pass"})
    held = await slate._fetch_held_subjects(uuid4(), conn, ["blog"])
    assert held["blog"].segment_ids == {"gone-1", "live-1"}
    assert held["blog"].places == {"corvin pass"}


@pytest.mark.asyncio
async def test_no_place_once_channel_means_no_held_read():
    conn = MagicMock()
    conn.fetch = AsyncMock()
    assert await slate._fetch_held_subjects(uuid4(), conn, []) == {}
    conn.fetch.assert_not_awaited()


# ── the pick fills the walk ─────────────────────────────────────────────────────────────────────────

def _member(atom_id: str, tour: str, place: str = "Lake Aurel") -> Member:
    return Member(atom_id=atom_id, tour_id=tour, place=place, country="Valdoria", evidence="e", card_id=None)


class _PickConn:
    def __init__(self, routes):
        self.routes = routes        # rows of _SEGMENT_ROUTES_SQL
        self.route_args = None

    async def fetchval(self, sql, *args):
        assert "canonical_place FROM acp_contract.atom_segment" in sql
        return "Lake Aurel"

    async def fetch(self, sql, *args):
        assert "ordered_segment_ids ?|" in sql and "superseded_at IS NULL" in sql
        self.route_args = args
        return [r for r in self.routes if r["tour_id"] in args[1]]


def _route(route_id, tour, ids, first_day=1):
    return {"route_id": route_id, "tour_id": tour, "first_day": first_day, "ordered_segment_ids": json.dumps(ids)}


def _resolve(conn, members, channel="blog", guard=None, tenant_tours=(TOUR_A, TOUR_B)):
    async def load_members(_conn, ids, _tenant):
        return {ids[0]: members}

    async def keep_all(_conn, _trip, ids, **_kw):
        return [s for s in json.loads(ids)] if isinstance(ids, str) else list(ids)

    with patch.object(slate, "load_segment_members", new=load_members), \
         patch.object(slate, "load_alias_cards", new=AsyncMock(return_value={})), \
         patch.object(slate, "_tenant_tour_ids", new=AsyncMock(return_value=list(tenant_tours))), \
         patch.object(slate, "trip_route_segment_ids", new=guard or keep_all):
        return asyncio.run(slate._resolve_representative_atom(conn, "tenant", "seg-lake", None, channel=channel))


def test_blog_pick_carries_the_route_on_the_seed_tour():
    conn = _PickConn([_route("ra", TOUR_A, ["seg-pass", "seg-lake", "seg-gorge"])])
    atom_id, trip_id, walk = _resolve(conn, [_member("atom-a", TOUR_A)])
    assert (atom_id, trip_id) == ("atom-a", TOUR_A)
    assert walk == ["seg-pass", "seg-lake", "seg-gorge"]
    assert conn.route_args[0] == "seg-lake" and set(conn.route_args[1]) == {TOUR_A, TOUR_B}


def test_blog_pick_with_no_route_is_a_single_place_piece():
    atom_id, trip_id, walk = _resolve(_PickConn([]), [_member("atom-a", TOUR_A)])
    assert (atom_id, trip_id, walk) == ("atom-a", TOUR_A, None)


def test_social_pick_never_reads_a_route():
    conn = _PickConn([_route("ra", TOUR_A, ["seg-pass", "seg-lake"])])
    assert _resolve(conn, [_member("atom-a", TOUR_A)], channel="linkedin") == ("atom-a", TOUR_A, None)
    assert conn.route_args is None


def test_longest_guarded_route_wins_and_a_route_without_the_segment_is_skipped():
    conn = _PickConn([_route("r1", TOUR_A, ["seg-lake", "seg-x"], first_day=1),
                      _route("r2", TOUR_A, ["seg-lake", "seg-y", "seg-z"], first_day=4),
                      _route("r3", TOUR_A, ["seg-lake", "seg-q", "seg-r", "seg-s"], first_day=8)])

    async def guard(_conn, _trip, ids, **_kw):
        ids = json.loads(ids)
        return [] if "seg-q" in ids else ids          # r3's Segments are not on the trip any more

    _atom, _trip, walk = _resolve(conn, [_member("atom-a", TOUR_A)], guard=guard)
    assert walk == ["seg-lake", "seg-y", "seg-z"]


def test_a_route_that_keeps_one_segment_is_no_walk():
    conn = _PickConn([_route("r1", TOUR_A, ["seg-lake", "seg-gone"])])

    async def guard(_conn, _trip, ids, **_kw):
        return ["seg-lake"]

    assert _resolve(conn, [_member("atom-a", TOUR_A)], guard=guard)[2] is None


def test_seed_moves_to_a_same_grade_member_on_a_tour_with_a_route():
    # The seed rule's member is on TOUR_A (lowest atom id among equal grades); only TOUR_B has a Route: the pick
    # writes from the TOUR_B member, so the trip and the walk agree.
    conn = _PickConn([_route("rb", TOUR_B, ["seg-lake", "seg-gorge"])])
    atom_id, trip_id, walk = _resolve(conn, [_member("atom-a", TOUR_A), _member("atom-b", TOUR_B)])
    assert (atom_id, trip_id, walk) == ("atom-b", TOUR_B, ["seg-lake", "seg-gorge"])


def test_seed_never_moves_to_a_looser_member_for_a_walk():
    # TOUR_B's member only names the place inside a longer name (a looser grade): keep the exact seed, no walk.
    conn = _PickConn([_route("rb", TOUR_B, ["seg-lake", "seg-gorge"])])
    members = [_member("atom-a", TOUR_A), _member("atom-b", TOUR_B, place="Lake Aurel Ferry Pier")]
    assert _resolve(conn, members) == ("atom-a", TOUR_A, None)


@pytest.mark.asyncio
async def test_pick_stores_the_walk_on_the_request():
    """pick_subject_on(): a Blog Segment Subject's request carries `route_segment_ids` (T8 / T9 read it)."""
    subject_id, tenant = uuid4(), uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"subject_id": subject_id, "channel": "blog", "state": "proposed", "segment_id": "seg-lake", "route_id": None},
        {"request_id": uuid4(), "tenant_id": tenant, "atom_id": "atom-a", "trip_id": TOUR_A, "channel": "blog",
         "status": "pending_goal", "created_at": None, "route_segment_ids": None},
    ])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    with patch.object(slate, "resolve_segment_aliases", new=AsyncMock(return_value={})), \
         patch.object(slate, "_resolve_representative_atom",
                      new=AsyncMock(return_value=("atom-a", TOUR_A, ["seg-pass", "seg-lake"]))) as resolve:
        out = await slate.pick_subject_on(conn, tenant, subject_id)
    assert resolve.await_args.kwargs == {"channel": "blog"}
    insert_args = conn.fetchrow.await_args_list[1].args
    assert json.loads(insert_args[6]) == ["seg-pass", "seg-lake"] and insert_args[4] == "blog"
    assert out["route_segment_ids"] == ["seg-pass", "seg-lake"]


# ── T8 / T9 read the walk of a Segment pick like a Route pick's ─────────────────────────────────────

def _blog_segment_request():
    return {"request_id": "req-1", "tenant_id": "t1", "atom_id": "atom-a", "trip_id": TOUR_A, "channel": "blog",
            "goal": None, "status": "pending_goal", "dfs_paa_snapshot": None, "subject_id": "subj-1",
            "route_segment_ids": json.dumps(["seg-pass", "seg-lake"])}


def test_t8_builds_the_route_digest_for_a_blog_segment_pick():
    """The walk reaches T8 around the Subject's own moment (the S271 review's focus; full cases in
    test_aa741_s271_walk_focus.py)."""
    from services.acp_angle_gate import service as ag
    req = _blog_segment_request()

    async def fetch(tenant_id, trip_id, segment_ids, pool, atoms_out=None, *, focus_segment_id=None,
                    focus_out=None):
        if focus_out is not None:
            focus_out.append(1 if focus_segment_id == "seg-lake" else None)
        return segments
    segments = [("atom-p", "Corvin Pass — cross\nLocation: Corvin Pass\nDay 2 of the trip\n"
                           "Source itinerary: Cross the pass on foot. Lunch at the hut. Descend by the old road."),
                ("atom-a", "Lake Aurel — visit\nLocation: Lake Aurel\nDay 3 of the trip\n"
                           "Source itinerary: Walk the shore of the lake. Boat to the island. Visit the chapel.")]
    with patch.object(ag, "request_route_segment_ids", new=AsyncMock(return_value=["seg-pass", "seg-lake"])), \
         patch.object(ag, "_fetch_atom_for_tenant", new=AsyncMock(return_value={"text": "single seed"})), \
         patch.object(ag, "fetch_brand_audience", new=AsyncMock(return_value={})), \
         patch("services.acp_content_writing.service._fetch_route_segments", new=fetch), \
         patch.object(ag.walk_focus, "subject_focus_segment", new=AsyncMock(return_value="seg-lake")), \
         patch.object(ag, "fetch_tenant_trips", new=AsyncMock(return_value=[])), \
         patch.object(ag, "fetch_search_demand_signal", new=AsyncMock(return_value=None)), \
         patch.object(ag, "_blog_search_demand", new=AsyncMock(return_value=(None, None))), \
         patch.object(ag, "fetch_piece_history", new=AsyncMock(return_value=[])):
        inputs = asyncio.run(ag.angle_inputs("t1", req, {"key": "promotion"}, MagicMock()))
    assert inputs.moments and len(inputs.moments) == 2
    assert inputs.content_seed.startswith("ROUTE (this piece covers these moments")
    assert "Lake Aurel" in inputs.content_seed and "Corvin Pass" in inputs.content_seed
    assert "every angle leads with it and gives it the most room): Lake Aurel" in inputs.content_seed
    assert "ANCHOR CANDIDATES" not in inputs.content_seed


def test_t9_writes_from_the_walk_of_a_blog_segment_pick():
    from services.acp_content_writing import service as cw
    req = {**_blog_segment_request(), "status": "approved", "angles": [{"chosen": True, "option_id": None}],
           "cta": "Ask us", "goal": "inspire", "route_segment_ids": ["seg-pass", "seg-lake"]}
    fetch = AsyncMock(side_effect=RuntimeError("stop"))
    with patch.object(cw.angle_gate_service, "fetch_request", new=AsyncMock(return_value=req)), \
         patch.object(cw, "fetch_brand_audience", new=AsyncMock(return_value={})), \
         patch.object(cw, "request_route_segment_ids", new=AsyncMock(return_value=["seg-pass", "seg-lake"])), \
         patch.object(cw.walk_focus, "subject_focus_segment", new=AsyncMock(return_value="seg-lake")), \
         patch.object(cw, "_fetch_route_segments", new=fetch), pytest.raises(RuntimeError):
        asyncio.run(cw.start_write("t1", "req-1", MagicMock()))
    assert fetch.await_args.args[1:3] == (TOUR_A, ["seg-pass", "seg-lake"])
    assert fetch.await_args.kwargs["focus_segment_id"] == "seg-lake"
