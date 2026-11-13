"""AA-1077 (S279) — the T7 Blog seed floor sized from the F4 blog floor and the measured chars per seed word.

1. Settings (S279 thresholds L1): the settings cis.t7.blog_min_seed_words (600) and cis.t7.blog_chars_per_seed_word
   (5.8); their env vars are emergency overrides (a valid value wins, an invalid one is ignored); 0 = off / no sizing.
2. `seed_floor()`: max(flat, sized) while the flat Blog minimum seed is on; all 0 while it is off.
3. `skip_reason` / `blog_skipped_subjects`: per Subject why it was not proposed on Blog.
4. `_fetch_short_blog_seeds`: a seed that clears the flat 600 but not the sized floor is skipped on Blog only, and the
   measured words come back for the report.
5. `propose_slate`: `blog_coverage` carries the floor and the per-Subject skip list; the tenant view does not.

Invented tours and places only. No DB (fake connections).
"""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_shared import blog_seed, slate
from services.acp_shared.slate import tenant_blog_topics
from services.acp_shared.subject_seed import Member
from tests.unit.test_aa1081_blog_topic_coverage import TOUR_A, TOUR_B, _Conn, _inserted, _Pool, _seg


@pytest.fixture
def no_seed_env(monkeypatch):
    monkeypatch.delenv(blog_seed.MIN_SEED_ENV, raising=False)
    monkeypatch.delenv(blog_seed.RATIO_ENV, raising=False)
    return monkeypatch


# ── 1. settings ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_setting_default_is_one_word_per_seed_word():
    from services.acp_content_writing import length_contract as lc
    from shared import settings
    assert blog_seed.DEFAULT_CHARS_PER_SEED_WORD == settings.definition(blog_seed.RATIO_SETTING).default_value \
        == lc.CHARS_PER_WORD
    assert settings.definition(blog_seed.RATIO_SETTING).env_override == blog_seed.RATIO_ENV
    assert settings.definition(blog_seed.MIN_SEED_SETTING).env_override == blog_seed.MIN_SEED_ENV


@pytest.mark.parametrize("raw,expected", [(None, 5.8), ("", 5.8), ("6.5", 6.5), (" 7 ", 7.0), ("0", 0.0),
                                          ("-3", 5.8), ("abc", 5.8), ("inf", 5.8), ("nan", 5.8)])
def test_ratio_setting(no_seed_env, raw, expected):
    if raw is not None:
        no_seed_env.setenv(blog_seed.RATIO_ENV, raw)
    assert blog_seed.chars_per_seed_word() == expected


@pytest.mark.parametrize("ratio,expected", [(5.8, 811), (4.7, 1000), (7.375, 638), (0.0, 0), (-1.0, 0)])
def test_sized_floor_reaches_the_f4_floor(ratio, expected):
    assert blog_seed.sized_min_seed_words(ratio) == expected


# ── 2. seed_floor ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flat,ratio,expected", [
    (None, None, (811, 600, 811)),    # S279: the settings in force (600 flat, 5.8 chars per seed word)
    ("0", None, (0, 0, 0)),           # the Blog minimum seed off: no sizing either
    ("600", None, (811, 600, 811)),   # the sized floor binds
    ("900", None, (900, 900, 811)),   # the flat floor binds
    ("600", "0", (600, 600, 0)),      # sizing switched off: the flat floor alone
    ("600", "9.4", (600, 600, 500)),
])
def test_seed_floor(no_seed_env, flat, ratio, expected):
    if flat is not None:
        no_seed_env.setenv(blog_seed.MIN_SEED_ENV, flat)
    if ratio is not None:
        no_seed_env.setenv(blog_seed.RATIO_ENV, ratio)
    f = blog_seed.seed_floor()
    assert (f["min_seed_words"], f["flat"], f["sized"]) == expected
    assert f["floor_chars"] == 4700


# ── 3. per-Subject reasons ───────────────────────────────────────────────────────────────────────────────────────

FLOOR = {"min_seed_words": 811, "flat": 600, "sized": 811, "chars_per_seed_word": 5.8, "floor_chars": 4700}


@pytest.mark.parametrize("words,expected", [(None, None), (450, "seed_below_min"), (640, "seed_below_length_floor"),
                                            (811, None), (2000, None)])
def test_skip_reason(words, expected):
    assert blog_seed.skip_reason(words, FLOOR) == expected
    assert blog_seed.skip_reason(words, {"min_seed_words": 0, "flat": 0}) is None


def test_blog_skipped_subjects_names_each_reason_in_order():
    segs = [_seg("s1", "Corvane Fort", 1, [TOUR_A]), _seg("s2", "Lake Aurel", 2, [TOUR_A]),
            _seg("s3", "Mirsa Gorge", 3, [TOUR_B]), _seg("s4", "Selva Bay", 4, [TOUR_B]),
            _seg("s5", "Brenn Moor", 5, [TOUR_B], demand=10)]                 # misses the Bar: never listed
    rows = slate.blog_skipped_subjects("blog", segs, frozenset({"s3", "s5"}), frozenset({"s1", "s2"}),
                                       {"s1": 640, "s2": 450, "s4": 900}, FLOOR)
    assert [(r["segment_id"], r["reason"], r["seed_words"]) for r in rows] == [
        ("s3", "thin_walk", None), ("s2", "seed_below_min", 450), ("s1", "seed_below_length_floor", 640)]
    assert rows[2] == {"segment_id": "s1", "place": "Corvane Fort", "tour_ids": [TOUR_A],
                       "reason": "seed_below_length_floor", "seed_words": 640, "min_seed_words": 811}


# ── 4. _fetch_short_blog_seeds ───────────────────────────────────────────────────────────────────────────────────

async def _short(segs, words, held=None):
    members = {s.segment_id: [Member(atom_id=f"atom-{s.segment_id}", tour_id=TOUR_A, place=s.place,
                                     country="Valdoria", evidence="e", card_id=None)] for s in segs}
    measured: dict = {}

    async def _words(_conn, _tid, atom_id, _tour, _walk):
        return words[atom_id]

    with patch.object(slate, "_tenant_tour_ids", AsyncMock(return_value=[TOUR_A])), \
         patch.object(slate, "load_segment_members", AsyncMock(return_value=members)), \
         patch.object(slate, "load_alias_cards", AsyncMock(return_value={})), \
         patch.object(slate, "_segment_walk",
                      AsyncMock(side_effect=lambda _c, _t, sid, *_a: (f"atom-{sid}", TOUR_A, None))), \
         patch.object(blog_seed, "blog_seed_words", AsyncMock(side_effect=_words)):
        out = await slate._fetch_short_blog_seeds(uuid4(), MagicMock(), segs, ["blog", "linkedin"], held or {}, {},
                                                  measured_out=measured)
    return out, measured


@pytest.mark.asyncio
async def test_a_seed_that_clears_600_but_not_the_length_floor_is_skipped(no_seed_env):
    no_seed_env.setenv(blog_seed.MIN_SEED_ENV, "600")
    segs = [_seg("s1", "Corvane Fort", 1, [TOUR_A]), _seg("s2", "Lake Aurel", 2, [TOUR_A]),
            _seg("s3", "Mirsa Gorge", 3, [TOUR_A])]
    out, measured = await _short(segs, {"atom-s1": 640, "atom-s2": 450, "atom-s3": 900})
    assert out == {"blog": frozenset({"s1", "s2"})}
    assert measured == {"s1": 640, "s2": 450, "s3": 900}


@pytest.mark.asyncio
async def test_sizing_off_keeps_the_flat_floor_alone(no_seed_env):
    no_seed_env.setenv(blog_seed.MIN_SEED_ENV, "600")
    no_seed_env.setenv(blog_seed.RATIO_ENV, "0")
    out, _measured = await _short([_seg("s1", "Corvane Fort", 1, [TOUR_A])], {"atom-s1": 640})
    assert out == {"blog": frozenset()}


@pytest.mark.asyncio
async def test_flat_floor_off_reads_nothing_whatever_the_ratio(no_seed_env):
    no_seed_env.setenv(blog_seed.MIN_SEED_ENV, "0")
    no_seed_env.setenv(blog_seed.RATIO_ENV, "5.8")
    conn = MagicMock()
    conn.fetch = AsyncMock()
    out = await slate._fetch_short_blog_seeds(uuid4(), conn, [_seg("s1", "Corvane Fort", 1, [TOUR_A])],
                                              ["blog"], {}, {})
    assert out == {}
    conn.fetch.assert_not_awaited()


# ── 5. propose_slate ─────────────────────────────────────────────────────────────────────────────────────────────

async def _propose(conn, segments, words: dict, short_ids: frozenset):
    async def _fetch(_tid, _pool, _markets, unseeded_out=None):
        return list(segments)

    async def _short_seeds(_tid, _conn, _segs, _channels, _held, _thin, measured_out=None, floor=None):
        assert floor == blog_seed.seed_floor()
        measured_out.update(words)
        return {"blog": short_ids}

    with patch.object(slate, "_tenant_market_codes", AsyncMock(return_value=(["US"], []))), \
         patch.object(slate, "_fetch_thin_walk_segments", AsyncMock(return_value={})), \
         patch.object(slate, "_fetch_short_blog_seeds", AsyncMock(side_effect=_short_seeds)), \
         patch.object(slate, "_fetch_segment_candidates", AsyncMock(side_effect=_fetch)), \
         patch.object(slate, "_fetch_route_candidates", AsyncMock(return_value=[])), \
         patch.object(slate, "_fetch_segment_hub_map", AsyncMock(return_value={})), \
         patch("services.acp_shared.debate.apply_debate",
               AsyncMock(side_effect=lambda _tid, candidates, _conn: candidates)):
        return await slate.propose_slate(uuid4(), _Pool(conn))


@pytest.mark.asyncio
async def test_slate_result_reports_the_floor_and_each_skipped_subject(no_seed_env):
    no_seed_env.setenv(blog_seed.MIN_SEED_ENV, "600")
    conn = _Conn({TOUR_A: "Valdoria coast", TOUR_B: "Corvin passes"}, channels=["blog", "linkedin"])
    segs = [_seg("s1", "Corvane Fort", 1, [TOUR_A], said=400), _seg("s2", "Lake Aurel", 2, [TOUR_B])]
    result = await _propose(conn, segs, {"s1": 640, "s2": 1200}, frozenset({"s1"}))
    inserted = _inserted(conn)
    assert inserted["blog"] == ["s2"] and "s1" in inserted["linkedin"]        # s1 stays on the social Channel
    cov = result["blog_coverage"]
    assert cov["min_seed_words"] == 811 and cov["short_seed"] == 1
    assert cov["seed_floor"] == {"min_seed_words": 811, "flat": 600, "sized": 811, "chars_per_seed_word": 5.8,
                                 "floor_chars": 4700}
    assert cov["skipped"] == [{"segment_id": "s1", "place": "Corvane Fort", "tour_ids": [TOUR_A],
                               "reason": "seed_below_length_floor", "seed_words": 640, "min_seed_words": 811}]
    tour_a = next(t for t in cov["tours"] if t["tour_id"] == TOUR_A)
    assert (tour_a["topics"], tour_a["reason"]) == (0, "seed_too_thin")
    view = tenant_blog_topics(result)                                            # the tenant never sees the list
    assert "skipped" not in str(view) and "seed_floor" not in str(view)


@pytest.mark.asyncio
async def test_slate_with_the_floor_off_lists_nothing(no_seed_env):
    no_seed_env.setenv(blog_seed.MIN_SEED_ENV, "0")
    conn = _Conn({TOUR_A: "Valdoria coast"}, channels=["blog"])
    result = await _propose(conn, [_seg("s1", "Corvane Fort", 1, [TOUR_A])], {}, frozenset())
    cov = result["blog_coverage"]
    assert cov["min_seed_words"] == 0 and cov["skipped"] == [] and cov["seed_floor"]["sized"] == 0
