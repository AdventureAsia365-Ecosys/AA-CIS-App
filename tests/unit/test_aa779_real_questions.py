"""AA-779 (S245) item 2 (A) + item 3 — blogs take real questions from their own Segments and places first
(atom_matches, search_demand), the tour-level People Also Ask last; off-meaning PAA ('moron' → the insult,
the town is Mörön) is dropped; each piece records where its questions came from (admin only).

Questions are real Google PAA shapes: the Khövsgöl / Tsaatan / Mörön route of the S234 r4 / S235 r5 blogs,
and what Google asks for the bare keyword 'moron' (the DFS keyword of the town Mörön).
"""
import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_angle_gate import service as angle_service
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service as writing_service
from services.acp_shared import real_questions as rq
from services.acp_shared.dfs_relevance import SearchDemandSignal

MORON_PAA = [  # Google PAA for the keyword 'moron' (the insult, not the town)
    "Is moron a bad word?",
    "What does it mean if someone calls you a moron?",
    "What is the difference between a moron and an idiot?",
    "What is the origin of the word moron?",
    "What is a moron?",
    "Is moron offensive?",
]
MORON_PLACE = [
    "Does Mörön have an airport?",
    "How far is Moron from Lake Khovsgol?",
    "What is the mean temperature in Mörön in July?",
]
SEGMENT_QS = ["Who are the Tsaatan people?", "How deep is Lake Khövsgöl?", "Can you swim in Lake Khövsgöl?"]
PLACE_QS = ["Is Lake Khövsgöl worth visiting?", "How deep is lake khovsgol?"]  # 2nd = a segment one again
TOUR_QS = ["How many days do you need in Mongolia?", "Is Mongolia safe for tourists?"]

MORON_SEED = (
    "Mörön — overnight in town before the lake\n"
    "Location: Mörön, Khövsgöl Province, Mongolia (do not place it in any other town, region or country)\n"
    "Source itinerary: Fly to Mörön; drive on to the lakeshore.\n"
    "Place background (checked facts about the place itself — you may use these):\n"
    "- Mörön has a small domestic airport with scheduled flights to Ulaanbaatar.\n"
    "- Mörön is the capital of Khövsgöl Province.\n"
)


# ---------------------------------------------------------------- language + off-meaning

@pytest.mark.parametrize("q", MORON_PAA)
def test_word_sense_and_name_as_noun_questions_are_off_meaning(q):
    assert rq.is_off_meaning(q, ["moron"])


@pytest.mark.parametrize("q", MORON_PLACE + ["Is it rude to refuse tea in a ger?", "Who are the Tsaatan people?",
                                              "What is the mean depth of Lake Khövsgöl?"])
def test_place_questions_are_not_off_meaning(q):
    assert not rq.is_off_meaning(q, ["moron", "khovsgol", "tsaatan"])


def test_only_english_questions_count():
    assert rq.looks_english("What's the best month to visit Lake Khövsgöl?")
    assert rq.looks_english("Isn't Khövsgöl frozen in May?")
    for q in ("Wie kalt ist der Khövsgöl See?", "Was ist Khövsgöl?", "Wat is het Khövsgöl meer?",
              "Quel est le meilleur moment pour visiter le lac Khövsgöl?", "Lake Khövsgöl depth", ""):
        assert not rq.looks_english(q), q


# ---------------------------------------------------------------- the chain

def test_merge_keeps_tier_order_drops_duplicates_and_noise():
    out = rq.merge([(rq.SOURCE_SEGMENT, SEGMENT_QS), (rq.SOURCE_PLACE, PLACE_QS + ["Is moron a bad word?"]),
                    (rq.SOURCE_TOUR, TOUR_QS + ["Wie kalt ist es?", "Hi?", None, 7])])
    assert [(r.question, r.source) for r in out] == [
        ("Who are the Tsaatan people?", "segment_match"),
        ("How deep is Lake Khövsgöl?", "segment_match"),
        ("Can you swim in Lake Khövsgöl?", "segment_match"),
        ("Is Lake Khövsgöl worth visiting?", "place_paa"),
        ("How many days do you need in Mongolia?", "tour_paa"),
        ("Is Mongolia safe for tourists?", "tour_paa"),
    ]


def test_merge_caps_each_source_and_the_total():
    many = [f"What is there to do near Lake Khövsgöl on day {i}?" for i in range(40)]
    out = rq.merge([(rq.SOURCE_SEGMENT, many), (rq.SOURCE_TOUR, TOUR_QS)], per_source=5, limit=6)
    assert [r.source for r in out] == ["segment_match"] * 5 + ["tour_paa"]


def test_place_words_skip_kinds_short_words_and_the_country():
    words = rq.place_words(["Lake Khövsgöl", "Mörön", "Khövsgöl Province", "Tsaatan camp", "Ger", "Mongolia"],
                           countries=["Mongolia"])
    assert words == {"khovsgol", "moron", "tsaatan"}


def _row(keyword, market, volume, paa, as_text=True):
    return {"keyword": keyword, "market": market, "search_volume": volume,
            "people_also_ask": json.dumps(paa) if as_text else paa}


def test_place_rows_rank_by_shared_words_volume_then_market():
    rows = [
        _row("khovsgol", "UK", 900, ["UK question about Khövsgöl?"]),
        _row("lake khovsgol moron", "US", 10, ["Two-word question about Khövsgöl and Mörön?"]),
        _row("khovsgol", "US", 900, ["US question about Khövsgöl?"], as_text=False),
        _row("khovsgolx tours", "US", 5000, ["Not a whole-word match?"]),  # ILIKE hit, not a place word
        _row("lake khovsgol", "AU", None, ["AU question about Khövsgöl?"]),
    ]
    assert rq.rank_place_rows(rows, {"khovsgol", "moron"}) == [
        "Two-word question about Khövsgöl and Mörön?", "US question about Khövsgöl?",
        "UK question about Khövsgöl?", "AU question about Khövsgöl?"]


class _Conn:
    """A fake asyncpg connection: answers by which query it is given."""

    def __init__(self, *, segments=(), atoms=(), matches=(), demand=(), fail=()):
        self.segments, self.atoms, self.matches, self.demand, self.fail = segments, atoms, matches, demand, fail
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        for name in self.fail:
            if name in sql:
                raise RuntimeError(f"{name} unavailable")
        if "acp_contract.atom_matches" in sql:
            return [{"query": q} for q in self.matches]
        if "acp_contract.search_demand" in sql:
            return list(self.demand)
        if "acp_contract.atom_segment_alias" in sql:
            return list(self.segments)
        if "acp_contract.tour_atoms" in sql:
            return list(self.atoms)
        raise AssertionError(sql)


def _pool(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn
    pool.acquire = acquire
    return pool


KHOVSGOL_CONN = dict(
    segments=[{"segment_id": "seg-lake", "canonical_place": "Lake Khövsgöl"},
              {"segment_id": "seg-tsaatan", "canonical_place": "Tsaatan camp"}],
    atoms=[{"place": "Khövsgöl Lake", "country": "Mongolia"}],
    matches=SEGMENT_QS,
    demand=[_row("lake khovsgol", "US", 1900, PLACE_QS)],
)


@pytest.mark.asyncio
async def test_collect_reads_segments_then_places_then_the_tour():
    conn = _Conn(**KHOVSGOL_CONN)
    out = await rq.collect_real_questions(_pool(conn), atom_ids=["atom-1"], segment_ids=["seg-old"],
                                          tour_paa=TOUR_QS)
    assert [r.source for r in out] == ["segment_match"] * 3 + ["place_paa"] + ["tour_paa"] * 2
    _scope_sql, scope_args = conn.calls[0]
    assert scope_args == (["atom-1"], ["seg-old"])  # merged Route Segment ids resolve through the alias
    match_args = next(a for s, a in conn.calls if "atom_matches" in s)
    assert match_args == (["seg-lake", "seg-tsaatan"],)
    demand_args = next(a for s, a in conn.calls if "search_demand" in s)
    assert demand_args == (["%khovsgol%", "%tsaatan%"],)  # 'lake', 'camp' and the country are not place words


@pytest.mark.asyncio
async def test_a_failing_source_is_skipped_and_the_tour_tier_stays():
    conn = _Conn(**KHOVSGOL_CONN, fail=("atom_matches",))
    out = await rq.collect_real_questions(_pool(conn), atom_ids=["atom-1"], tour_paa=TOUR_QS)
    assert [r.source for r in out] == ["place_paa", "place_paa", "tour_paa", "tour_paa"]
    broken = _Conn(fail=("atom_segment_alias",))
    out = await rq.collect_real_questions(_pool(broken), atom_ids=["atom-1"], tour_paa=TOUR_QS)
    assert [r.question for r in out] == TOUR_QS


@pytest.mark.asyncio
async def test_no_scope_reads_nothing_but_the_tour():
    conn = _Conn()
    out = await rq.collect_real_questions(_pool(conn), atom_ids=[], tour_paa=["Is Mongolia safe for tourists?"])
    assert [r.source for r in out] == ["tour_paa"] and conn.calls == []


def test_segment_match_sql_counts_only_the_last_landing_pass():
    sql = rq._SEGMENT_MATCH_SQL
    assert "rn <= questions_count" in sql and "PARTITION BY seg.segment_id ORDER BY am.matched_at DESC" in sql
    assert "am.kind = 'question'" in sql and "interval '1 day'" in sql


# ---------------------------------------------------------------- the scope filter (item 3)

def test_paa_filter_drops_the_insult_questions_for_moron():
    out = qg.paa_for_scope(MORON_PAA + MORON_PLACE, MORON_SEED, None, out_of_scope=("ulaanbaatar",))
    # the July temperature is a best-time topic the seed cannot answer (unanswerable_faq_topics), not off-meaning
    assert out == ["Does Mörön have an airport?", "How far is Moron from Lake Khovsgol?"]


# ---------------------------------------------------------------- the admin record

CANDIDATES = [{"q": q, "source": s} for q, s in [
    ("Who are the Tsaatan people?", "segment_match"), ("How deep is Lake Khövsgöl?", "segment_match"),
    ("Is Lake Khövsgöl worth visiting?", "place_paa"), ("Is Mongolia safe for tourists?", "tour_paa")]]


def test_label_and_counts():
    kept = rq.label_questions(["Who are the Tsaatan people?", "Is lake Khövsgöl worth visiting?", "Unknown one?"],
                              CANDIDATES)
    assert kept == [{"question": "Who are the Tsaatan people?", "source": "segment_match"},
                    {"question": "Is lake Khövsgöl worth visiting?", "source": "place_paa"},
                    {"question": "Unknown one?", "source": "tour_paa"}]  # pre-chain payload: from the snapshot
    assert rq.source_counts(CANDIDATES, kept) == {
        "candidates": {"segment_match": 2, "place_paa": 1, "tour_paa": 1},
        "kept": {"segment_match": 1, "place_paa": 1, "tour_paa": 1}}


def test_faq_sources_match_exact_or_close_else_writer():
    kept = rq.label_questions(["Who are the Tsaatan people?"], CANDIDATES)
    out = rq.faq_sources(["Who are the Tsaatan people", "How deep is the Lake Khövsgöl?",
                          "How deep is Lake Khövsgöl, really?",  # 3 of 4 content words = 0.75 < 0.8
                          "What should families pack for the lake?"], kept, CANDIDATES)
    assert [f["source"] for f in out] == ["segment_match", "segment_match", "writer", "writer"]


def test_writer_input_records_sources_for_a_blog_only():
    ctx = {"channel": "blog", "real_questions": CANDIDATES,
           "search_questions": rq.label_questions(["How deep is Lake Khövsgöl?"], CANDIDATES),
           "faq_questions": [{"question": "How deep is Lake Khövsgöl?", "source": "segment_match"}]}
    wi = writing_service.writer_input_snapshot(ctx)
    assert wi["search_questions"] == [{"question": "How deep is Lake Khövsgöl?", "source": "segment_match"}]
    assert wi["question_sources"]["kept"] == {"segment_match": 1}
    assert wi["faq_questions"][0]["source"] == "segment_match"
    for old in ({"channel": "blog"}, {"channel": "linkedin", "real_questions": []}):  # old payload / other channel
        keys = set(writing_service.writer_input_snapshot(old))
        assert not {"search_questions", "question_sources", "faq_questions"} & keys


def test_snapshot_tour_paa_reads_the_tour_list():
    assert writing_service.snapshot_tour_paa(None) == []
    assert writing_service.snapshot_tour_paa({"people_also_ask": TOUR_QS}) == TOUR_QS
    assert writing_service.snapshot_tour_paa({"people_also_ask": SEGMENT_QS + TOUR_QS,
                                              "tour_people_also_ask": [TOUR_QS[0], 3]}) == [TOUR_QS[0]]


# ---------------------------------------------------------------- the job wiring (T9)

FAQ_TEXT = qg.replace_faq_section(
    "Lake Khövsgöl holds most of Mongolia's fresh water, and the Tsaatan herd reindeer in the taiga.\n",
    [("Who are the Tsaatan people?", "The Tsaatan are reindeer herders of the northern Khövsgöl taiga."),
     ("How deep is Lake Khövsgöl?", "Lake Khövsgöl is up to 262 metres deep."),
     ("What should families pack for the lake?", "Lake Khövsgöl evenings are cold, so pack warm layers.")])
SEED = ("Lake Khövsgöl — trekking along the shore\n"
        "Location: Khövsgöl Lake, Khövsgöl Province, Mongolia (do not place it in any other town, region or "
        "country)\nSource itinerary: Trek the lakeshore; visit a Tsaatan reindeer camp.\n"
        "Place background (checked facts about the place itself — you may use these):\n"
        "- Lake Khövsgöl is up to 262 metres deep.\n- The Tsaatan herd reindeer in the northern taiga.\n")


def _job_ctx(**extra):
    ctx = {"atom_text": SEED, "goal": {"key": "promotion"}, "channel_style": {"channel": "blog"},
           "brand_audience": {}, "chosen": {}, "cta": "Ask Terra to plan this trip.", "destination": None,
           "trip_name": None, "brand_rubric_text": "", "channel": "blog", "atom_id": "a1",
           "people_also_ask": [c["q"] for c in CANDIDATES], "real_questions": CANDIDATES}
    ctx.update(extra)
    return ctx


@pytest.mark.asyncio
async def test_job_gives_the_writer_segment_questions_first_and_stores_their_sources():
    row = {"piece_id": str(uuid.uuid4()), "attempt_number": 1, "status": "approved"}
    passed = {"passed": True, "gate_ledger": [], "first_failure": None, "flags": []}
    enforce = AsyncMock(side_effect=lambda t, **k: (t, 0.0, []))
    with patch.object(writing_service, "write_content", return_value=(FAQ_TEXT, 0.01, {}, None)) as wc, \
         patch.object(writing_service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda m, *a, **k: (m, 0.0))), \
         patch.object(writing_service, "_enforce_faq_contract", new=enforce), \
         patch.object(writing_service, "run_quality_gates", return_value=passed), \
         patch.object(writing_service, "_finalize_piece", new=AsyncMock(return_value=row)) as fin:
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), _job_ctx(), pool=MagicMock())
    assert wc.call_args.kwargs["search_questions"][:2] == ["Who are the Tsaatan people?", "How deep is Lake Khövsgöl?"]
    wi = fin.call_args.kwargs["writer_input"]
    assert wi["search_questions"][0] == {"question": "Who are the Tsaatan people?", "source": "segment_match"}
    assert wi["question_sources"]["candidates"] == {"segment_match": 2, "place_paa": 1, "tour_paa": 1}
    assert [f["source"] for f in wi["faq_questions"]] == ["segment_match", "segment_match", "writer"]


# ---------------------------------------------------------------- T8 (angles)

@pytest.mark.asyncio
async def test_t8_blog_signal_puts_real_questions_before_the_tour():
    tour = SearchDemandSignal(relevance="HIGH", people_also_ask=TOUR_QS, related_keywords=["khovsgol tour"])
    chain = rq.merge([(rq.SOURCE_SEGMENT, SEGMENT_QS), (rq.SOURCE_TOUR, TOUR_QS)])
    req = {"atom_id": "atom-1", "route_segment_ids": json.dumps(["seg-lake", "seg-tsaatan"])}
    with patch.object(angle_service, "collect_real_questions", new=AsyncMock(return_value=chain)) as c:
        signal, tour_paa = await angle_service._blog_search_demand(req, tour, pool=MagicMock())
    assert c.call_args.kwargs == {"atom_ids": ["atom-1"], "segment_ids": ["seg-lake", "seg-tsaatan"],
                                  "tour_paa": TOUR_QS}
    assert signal.people_also_ask == SEGMENT_QS + TOUR_QS and tour_paa == TOUR_QS
    assert (signal.relevance, signal.related_keywords) == ("HIGH", ["khovsgol tour"])


@pytest.mark.asyncio
async def test_t8_blog_without_any_question_keeps_the_old_signal():
    with patch.object(angle_service, "collect_real_questions", new=AsyncMock(return_value=[])):
        assert await angle_service._blog_search_demand(
            {"atom_id": "a", "route_segment_ids": None}, None, pool=MagicMock()) == (None, None)
    chain = rq.merge([(rq.SOURCE_PLACE, PLACE_QS)])
    with patch.object(angle_service, "collect_real_questions", new=AsyncMock(return_value=chain)):
        signal, tour_paa = await angle_service._blog_search_demand(
            {"atom_id": "a", "route_segment_ids": None}, None, pool=MagicMock())
    assert signal.people_also_ask == PLACE_QS and tour_paa == [] and signal.related_keywords == []
