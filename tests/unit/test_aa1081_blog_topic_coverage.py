"""AA-1081 — every adopted tour gets a Blog topic when it has a free candidate, and a typed reason when it has none.

1. `_choose_slate(cover=...)`: a tour whose only Blog candidate would lose its place to the social Channels (the
   cross-Channel cap) still gets it; held / thin rules still apply; no cover = the round robin as before.
2. `blog_tour_coverage` / `_no_topic_reason`: each code of `NO_TOPIC_REASONS`, in order.
3. `tenant_blog_topics`: the tenant view keeps id, name, topic count and a known code only.
4. `propose_slate`: the job result carries `blog_coverage`; the coverage reads failing leaves the Slate as before.
5. The Blog minimum seed (`T7_BLOG_MIN_SEED_WORDS`): off by default (no read), on = a short seed is not proposed on
   Blog but stays on the social Channels, and the tour's reason is `seed_too_thin`.
6. `GET /v1/slate` returns `blog_topics`.

Invented tours and places only. No DB (fake connections).
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_shared import blog_seed, slate
from services.acp_shared.slate import (CHANNEL_BARS, NO_TOPIC_REASONS, Candidate, HeldSubjects, _choose_slate,
                                       blog_tour_coverage, tenant_blog_topics)

TOUR_A, TOUR_B, TOUR_C = "aaaaaaaa-0000-0000-0000-00000000000a", "bbbbbbbb-0000-0000-0000-00000000000b", \
    "cccccccc-0000-0000-0000-00000000000c"


def _seg(sid, place, score, tours, demand=2400, questions=5, said=0, flags=()):
    return Candidate(segment_id=sid, route_id=None, score=score, demand=demand, questions=questions, said=said,
                     place=place, tour_ids=tuple(tours), debate_flags=tuple(flags))


def _ids(chosen, channel):
    return [c.segment_id for c, _r in chosen[channel]]


# ── 1. the cover pass ────────────────────────────────────────────────────────────────────────────────────────────

def _starved():
    """Tour B's one Blog candidate sits at Quillon Ridge, the best social place; tour A has three stronger ones."""
    return [
        _seg("a1", "Lake Aurel", 1, [TOUR_A]), _seg("a2", "Mirsa Gorge", 2, [TOUR_A]),
        _seg("a3", "Corvin Pass", 3, [TOUR_A]),
        _seg("b1", "Quillon Ridge", 10, [TOUR_B], said=400, flags=("brand_fit",)),
    ]


def test_without_cover_the_social_channels_take_the_place_first():
    channels = ["blog", "linkedin", "facebook", "instagram"]
    chosen = _choose_slate(channels, _starved(), [], {})
    assert "b1" not in _ids(chosen, "blog")              # the place is on 3 Channels before Blog reaches it
    assert _ids(chosen, "linkedin") == _ids(chosen, "facebook") == _ids(chosen, "instagram") == ["b1"]


def test_cover_seats_the_uncovered_tour_first_and_keeps_the_cross_channel_cap():
    channels = ["blog", "linkedin", "facebook", "instagram"]
    chosen = _choose_slate(channels, _starved(), [], {}, cover={"blog": frozenset({TOUR_A, TOUR_B})})
    assert _ids(chosen, "blog") == ["a1", "b1", "a2", "a3"]   # a1 covers A, b1 covers B, then the round robin
    assert _ids(chosen, "linkedin") == _ids(chosen, "facebook") == ["b1"]
    assert _ids(chosen, "instagram") == []                    # Quillon Ridge is on 3 Channels already


def test_cover_prefers_an_unflagged_candidate_and_one_seat_covers_every_tour_it_is_on():
    segs = [_seg("x1", "Lake Aurel", 1, [TOUR_A, TOUR_B]), _seg("x2", "Mirsa Gorge", 2, [TOUR_B], flags=("c",)),
            _seg("x3", "Selva Bay", 5, [TOUR_C])]
    chosen = _choose_slate(["blog"], segs, [], {}, cover={"blog": frozenset({TOUR_A, TOUR_B, TOUR_C})})
    assert _ids(chosen, "blog")[:2] == ["x1", "x3"]           # x1 covers A and B; x3 is C's only one
    assert set(_ids(chosen, "blog")) == {"x1", "x2", "x3"}    # the round robin still takes the rest


def test_cover_keeps_the_held_and_thin_rules():
    segs = [_seg("h1", "Lake Aurel", 1, [TOUR_A]), _seg("t1", "Mirsa Gorge", 2, [TOUR_A])]
    held = {"blog": HeldSubjects(frozenset(), frozenset({"lake aurel"}))}
    chosen = _choose_slate(["blog"], segs, [], {}, held=held, thin={"blog": frozenset({"t1"})},
                           cover={"blog": frozenset({TOUR_A})})
    assert _ids(chosen, "blog") == []


def test_cover_only_on_cover_each_tour_channels_and_none_is_the_old_behaviour():
    assert [c for c, s in CHANNEL_BARS.items() if s["cover_each_tour"]] == ["blog"]
    segs = _starved()
    channels = ["blog", "linkedin", "facebook", "instagram"]
    assert _choose_slate(channels, segs, [], {}, cover={"linkedin": frozenset({TOUR_B})}) == \
        _choose_slate(channels, segs, [], {})


# ── 2. the reason codes ──────────────────────────────────────────────────────────────────────────────────────────

def _cov(segments=(), unseeded=(), dropped=(), chosen=(), held=None, skip=frozenset(), held_topics=None,
         tours=None):
    tours = tours or {TOUR_A: "Valdoria lakes"}
    return blog_tour_coverage("blog", tours, list(segments), list(unseeded), list(dropped),
                              [(c, {}) for c in chosen], held, frozenset(skip), held_topics or {})


def _reason(**kw):
    rows = _cov(**kw)
    assert len(rows) == 1
    return rows[0]["reason"]


def test_no_moment_is_not_ready():
    assert _reason() == "not_ready"
    assert _reason(segments=[_seg("o1", "Brenn Moor", 1, [TOUR_B])]) == "not_ready"   # another tour's moment


def test_no_moment_clears_the_bar_is_low_search_interest():
    low = [_seg("l1", "Brenn Moor", 1, [TOUR_A], demand=1300, questions=2),
           _seg("l2", "Old Velk", 2, [TOUR_A], demand=None, questions=9)]
    assert _reason(segments=low) == "low_search_interest"
    assert _reason(segments=low, unseeded=[_seg("u0", "Selva Bay", 3, [TOUR_A], demand=900)]) == \
        "low_search_interest"


def test_cleared_without_a_seed_atom_is_no_place_match():
    low = [_seg("l1", "Brenn Moor", 1, [TOUR_A], demand=10)]
    assert _reason(segments=low, unseeded=[_seg("u1", "Selva Bay", 3, [TOUR_A])]) == "no_place_match"


def test_thin_or_short_seed_is_seed_too_thin():
    segs = [_seg("t1", "Mirsa Gorge", 1, [TOUR_A]), _seg("t2", "Lake Aurel", 2, [TOUR_A])]
    assert _reason(segments=segs, skip={"t1", "t2"}) == "seed_too_thin"
    # one held, the other too thin: still the seed is the reason the agency can act on
    held = HeldSubjects(frozenset(), frozenset({"lake aurel"}))
    assert _reason(segments=segs, held=held, skip={"t1"}) == "seed_too_thin"


def test_every_place_held_is_places_covered():
    segs = [_seg("p1", "Lake Aurel", 1, [TOUR_A])]
    assert _reason(segments=segs, held=HeldSubjects(frozenset({"p1"}), frozenset())) == "places_covered"


def test_a_free_moment_without_a_seat_is_places_covered():
    assert _reason(segments=[_seg("f1", "Lake Aurel", 1, [TOUR_A])]) == "places_covered"


def test_debate_cut_is_no_fitting_topic():
    assert _reason(dropped=[_seg("d1", "Lake Aurel", 1, [TOUR_A])]) == "no_fitting_topic"


def test_topics_count_proposed_and_held_and_sort_no_topic_first():
    tours = {TOUR_A: "Valdoria lakes", TOUR_B: "Corvin passes", TOUR_C: "Aurel coast"}
    x = _seg("x1", "Lake Aurel", 1, [TOUR_A, TOUR_C])
    rows = _cov(segments=[x], chosen=[x], held_topics={TOUR_C: 2}, tours=tours)
    assert [(r["tour_name"], r["topics"], r["reason"]) for r in rows] == [
        ("Corvin passes", 0, "not_ready"), ("Aurel coast", 3, None), ("Valdoria lakes", 1, None)]
    assert set(rows[0]["detail"]) == {"segments", "cleared", "unseeded_cleared", "held", "thin", "debate_dropped",
                                      "free"}
    assert "detail" not in rows[1]


def test_every_code_is_known_and_exported():
    assert NO_TOPIC_REASONS == ("not_ready", "low_search_interest", "no_place_match", "seed_too_thin",
                                "no_fitting_topic", "places_covered")
    assert {"NO_TOPIC_REASONS", "blog_tour_coverage", "tenant_blog_topics"} <= set(slate.__all__)


# ── 3. the tenant view ───────────────────────────────────────────────────────────────────────────────────────────

def test_tenant_view_keeps_only_id_name_count_and_a_known_code():
    result = {"blog_coverage": {"status": "ok", "min_seed_words": 600, "short_seed": 2, "tours": [
        {"tour_id": TOUR_A, "tour_name": "Valdoria lakes", "topics": 0, "reason": "seed_too_thin",
         "detail": {"thin": 2, "debate_dropped": 0}},
        {"tour_id": TOUR_B, "tour_name": "Corvin passes", "topics": 0, "reason": "brand_fit_below_7"},
        {"tour_id": TOUR_C, "tour_name": "Aurel coast", "topics": 2, "reason": "places_covered"},
    ]}}
    view = tenant_blog_topics(result)
    assert view == {"total": 3, "with_topic": 1, "tours": [
        {"tour_id": TOUR_A, "tour_name": "Valdoria lakes", "topics": 0, "reason": "seed_too_thin"},
        {"tour_id": TOUR_B, "tour_name": "Corvin passes", "topics": 0, "reason": None},
        {"tour_id": TOUR_C, "tour_name": "Aurel coast", "topics": 2, "reason": None},
    ]}
    assert "min_seed_words" not in view and "detail" not in str(view)


@pytest.mark.parametrize("result", [None, {}, {"blog_coverage": {"status": "off"}},
                                    {"blog_coverage": {"status": "unavailable"}}, {"blog_coverage": "x"}])
def test_tenant_view_is_none_without_a_coverage(result):
    assert tenant_blog_topics(result) is None


# ── 4. propose_slate ─────────────────────────────────────────────────────────────────────────────────────────────

class _Conn:
    """Fake connection for propose_slate(): tenant_config, held subjects, the coverage reads, writes."""

    def __init__(self, tours, held_topic_rows=(), channels=("blog", "linkedin", "facebook", "instagram"),
                 fail_coverage=False):
        self.tours = tours
        self.held_topic_rows = list(held_topic_rows)
        self.channels = list(channels)
        self.fail_coverage = fail_coverage
        self.executed: list[tuple] = []

    async def fetchval(self, sql, *args):
        assert "acp_shared.tenant_config" in sql
        return self.channels

    async def fetch(self, sql, *args):
        if sql is slate._TENANT_TOURS_SQL:
            if self.fail_coverage:
                raise RuntimeError("coverage read failed")
            return [{"tour_id": t, "aa_name": n} for t, n in self.tours.items()]
        if sql is slate._HELD_TOPIC_TOURS_SQL:
            assert args[1] == "blog" and sorted(args[2]) == sorted(self.tours)
            return self.held_topic_rows
        return []                                     # held subjects, held via alias

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


async def _propose(conn, segments, unseeded=(), short=None):
    async def _fetch(_tid, _pool, _markets, unseeded_out=None):
        if unseeded_out is not None:
            unseeded_out.extend(unseeded)
        return list(segments)

    with patch.object(slate, "_tenant_market_codes", AsyncMock(return_value=(["US"], []))), \
         patch.object(slate, "_fetch_thin_walk_segments", AsyncMock(return_value={})), \
         patch.object(slate, "_fetch_short_blog_seeds", AsyncMock(return_value=short or {})), \
         patch.object(slate, "_fetch_segment_candidates", AsyncMock(side_effect=_fetch)), \
         patch.object(slate, "_fetch_route_candidates", AsyncMock(return_value=[])), \
         patch.object(slate, "_fetch_segment_hub_map", AsyncMock(return_value={})), \
         patch("services.acp_shared.debate.apply_debate",
               AsyncMock(side_effect=lambda _tid, candidates, _conn: candidates)):
        return await slate.propose_slate(uuid4(), _Pool(conn))


def _inserted(conn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sql, args in conn.executed:
        if sql.lstrip().startswith("INSERT INTO acp_shared.subject"):
            out.setdefault(args[2], []).append(args[1])
    return out


TOURS = {TOUR_A: "Valdoria lakes", TOUR_B: "Corvin passes", TOUR_C: "Aurel coast"}


@pytest.mark.asyncio
async def test_propose_covers_the_starved_tour_and_reports_the_others():
    conn = _Conn(TOURS)
    unseeded = [_seg("u1", "Selva Bay", 4, [TOUR_C])]
    result = await _propose(conn, _starved(), unseeded=unseeded)
    assert "b1" in _inserted(conn)["blog"]
    cov = result["blog_coverage"]
    assert cov["status"] == "ok" and cov["min_seed_words"] == 0 and cov["short_seed"] == 0
    by_tour = {t["tour_id"]: t for t in cov["tours"]}
    assert (by_tour[TOUR_A]["topics"], by_tour[TOUR_B]["topics"]) == (3, 1)
    assert (by_tour[TOUR_C]["topics"], by_tour[TOUR_C]["reason"]) == (0, "no_place_match")


@pytest.mark.asyncio
async def test_a_tour_with_a_picked_blog_topic_needs_no_new_seat():
    conn = _Conn({TOUR_B: "Corvin passes"}, held_topic_rows=[{"tour_id": TOUR_B, "n": 1}])
    result = await _propose(conn, _starved())
    assert "b1" not in _inserted(conn)["blog"]            # B is covered by its used topic, the cap stands
    assert result["blog_coverage"]["tours"] == [
        {"tour_id": TOUR_B, "tour_name": "Corvin passes", "topics": 1, "reason": None}]


@pytest.mark.asyncio
async def test_coverage_reads_failing_leave_the_slate_as_before():
    conn = _Conn(TOURS, fail_coverage=True)
    result = await _propose(conn, _starved())
    assert "b1" not in _inserted(conn)["blog"]
    assert result["blog_coverage"] == {"status": "unavailable"}
    assert result["proposed_per_channel"]["blog"] == 3


@pytest.mark.asyncio
async def test_blog_off_means_no_coverage():
    conn = _Conn(TOURS, channels=["linkedin"])
    result = await _propose(conn, _starved())
    assert result["blog_coverage"] == {"status": "off"}


@pytest.mark.asyncio
async def test_a_short_blog_seed_stays_on_social_and_names_the_reason():
    conn = _Conn({TOUR_B: "Corvin passes"}, channels=["blog", "linkedin"])
    result = await _propose(conn, [_seg("b1", "Quillon Ridge", 1, [TOUR_B], said=400)],
                            short={"blog": frozenset({"b1"})})
    assert _inserted(conn) == {"linkedin": ["b1"]}
    tour = result["blog_coverage"]["tours"][0]
    assert (tour["topics"], tour["reason"], tour["detail"]["thin"]) == (0, "seed_too_thin", 1)


# ── 5. the Blog minimum seed ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [(None, 0), ("", 0), ("0", 0), ("-5", 0), ("abc", 0), ("600", 600),
                                          (" 1000 ", 1000), ("6.5", 0)])
def test_min_seed_setting(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(blog_seed.MIN_SEED_ENV, raising=False)
    else:
        monkeypatch.setenv(blog_seed.MIN_SEED_ENV, raw)
    assert blog_seed.min_seed_words() == expected


@pytest.mark.asyncio
async def test_min_seed_off_reads_nothing(monkeypatch):
    monkeypatch.delenv(blog_seed.MIN_SEED_ENV, raising=False)
    conn = MagicMock()
    conn.fetch = AsyncMock()
    out = await slate._fetch_short_blog_seeds(uuid4(), conn, _starved(), ["blog", "linkedin"], {}, {})
    assert out == {}
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_min_seed_on_measures_free_blog_candidates_only(monkeypatch):
    from services.acp_shared.subject_seed import Member
    monkeypatch.setenv(blog_seed.MIN_SEED_ENV, "600")
    segs = [_seg("s1", "Lake Aurel", 1, [TOUR_A]), _seg("s2", "Mirsa Gorge", 2, [TOUR_A]),
            _seg("s3", "Corvin Pass", 3, [TOUR_A]), _seg("s4", "Selva Bay", 4, [TOUR_A]),
            _seg("s5", "Brenn Moor", 5, [TOUR_A], demand=10)]                 # misses the Bar: not measured
    members = {s.segment_id: [Member(atom_id=f"atom-{s.segment_id}", tour_id=TOUR_A, place=s.place,
                                     country="Valdoria", evidence="e", card_id=None)] for s in segs}
    words = {"atom-s1": 450, "atom-s2": 900}
    measured = []

    async def _words(_conn, _tid, atom_id, _tour, _walk):
        measured.append(atom_id)
        if atom_id == "atom-s4":
            raise RuntimeError("seed read failed")
        return words[atom_id]

    held = {"blog": HeldSubjects(frozenset({"s3"}), frozenset())}
    with patch.object(slate, "_tenant_tour_ids", AsyncMock(return_value=[TOUR_A])), \
         patch.object(slate, "load_segment_members", AsyncMock(return_value=members)), \
         patch.object(slate, "load_alias_cards", AsyncMock(return_value={})), \
         patch.object(slate, "_segment_walk",
                      AsyncMock(side_effect=lambda _c, _t, sid, *_a: (f"atom-{sid}", TOUR_A, None))), \
         patch.object(blog_seed, "blog_seed_words", AsyncMock(side_effect=_words)):
        out = await slate._fetch_short_blog_seeds(uuid4(), MagicMock(), segs, ["blog", "linkedin"], held, {})
    assert out == {"blog": frozenset({"s1"})}          # s2 is long enough; s3 held; s4 unmeasured is kept
    assert sorted(measured) == ["atom-s1", "atom-s2", "atom-s4"]


def test_walk_atoms_day_order_and_excluded_moments():
    t = datetime(2026, 11, 1, tzinfo=timezone.utc)
    rows = [
        {"segment_id": "w3", "atom_id": "a3", "text": "Night camp", "itinerary_day": 2, "created_at": t,
         "excluded": False},
        {"segment_id": "w1", "atom_id": "a1", "text": "Lake walk", "itinerary_day": 1, "created_at": t,
         "excluded": False},
        {"segment_id": "w2", "atom_id": "a2", "text": "Transfer", "itinerary_day": 1, "created_at": t,
         "excluded": True},
        {"segment_id": "w4", "atom_id": "a4", "text": "Ridge", "itinerary_day": None, "created_at": t,
         "excluded": False},
    ]
    assert blog_seed.walk_atoms(rows, ["w1", "w2", "w3", "w4"]) == [("a1", "Lake walk"), ("a3", "Night camp"),
                                                                    ("a4", "Ridge")]
    only_excluded = [dict(rows[2])]
    assert blog_seed.walk_atoms(only_excluded, ["w2"]) == [("a2", "Transfer")]   # nothing else left: kept


def test_seed_words_uses_the_audit_word_rule():
    assert blog_seed.seed_words("Lake Aurel's shore — a 3-day walk, 1,200 m up.") == 10   # "1,200" is two words
    assert blog_seed.seed_words(None) == 0


@pytest.mark.asyncio
async def test_blog_seed_words_adds_the_walk_moments():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"segment_id": "w1", "atom_id": "a1", "text": "Lake walk",
                                          "itinerary_day": 1, "created_at": None, "excluded": False}])
    with patch.object(blog_seed, "atom_seed_text", AsyncMock(return_value="one two three")), \
         patch.object(blog_seed, "route_moment_seeds", AsyncMock(return_value=[("a1", "four five"), ("a2", "six")])):
        assert await blog_seed.blog_seed_words(conn, uuid4(), "a0", TOUR_A, ["w1", "w2"]) == 6
        assert conn.fetch.await_args.args[0] is blog_seed._WALK_ATOMS_SQL
        assert await blog_seed.blog_seed_words(conn, uuid4(), "a0", TOUR_A, None) == 3
    assert conn.fetch.await_count == 1                  # no walk, no walk read


# ── 6. GET /v1/slate ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_slate_returns_the_tenant_view_of_the_last_proposal():
    from api.routers import v1_planning
    from tests.unit.test_aa859_wave4_pools import TENANT_ID, _request, _step
    from api.core import aa544_tenant_pool as tp
    from services.acp_planning.tenant_config import TenantPlanningConfig

    request, events = _request(tp.SLATE_READS_FLAG)
    last = {"finished_at": None, "result": {"blog_coverage": {"status": "ok", "tours": [
        {"tour_id": TOUR_A, "tour_name": "Valdoria lakes", "topics": 0, "reason": "low_search_interest",
         "detail": {"segments": 22}}]}}}
    with patch.object(v1_planning, "request_slate_proposal", new=AsyncMock(return_value=None)), \
            patch.object(v1_planning, "_resolve_config", new=AsyncMock(
                return_value=TenantPlanningConfig(markets=["US"], channels=["blog"], capacity_posts_per_week=3))), \
            patch.object(v1_planning, "fetch_slate_on", new=_step(events, "read", {})), \
            patch.object(v1_planning, "last_proposal", new=AsyncMock(return_value=last)):
        out = await v1_planning.get_slate(request, tenant={"sub": TENANT_ID})
    assert out["blog_topics"] == {"total": 1, "with_topic": 0, "tours": [
        {"tour_id": TOUR_A, "tour_name": "Valdoria lakes", "topics": 0, "reason": "low_search_interest"}]}
    with patch.object(v1_planning, "request_slate_proposal", new=AsyncMock(return_value=None)), \
            patch.object(v1_planning, "_resolve_config", new=AsyncMock(
                return_value=TenantPlanningConfig(markets=["US"], channels=["blog"], capacity_posts_per_week=3))), \
            patch.object(v1_planning, "fetch_slate_on", new=_step(events, "read", {})), \
            patch.object(v1_planning, "last_proposal", new=AsyncMock(return_value=None)):
        out = await v1_planning.get_slate(_request(tp.SLATE_READS_FLAG)[0], tenant={"sub": TENANT_ID})
    assert out["blog_topics"] is None
