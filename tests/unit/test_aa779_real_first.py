"""AA-779 follow-up (S245 r7 checkpoint) — off-meaning junk out, real questions first in the FAQ, the
admin-only SERP top-up, and a coverage measure that never counts junk as real.

The r7 checkpoint (8 blogs on the Khövsgöl / Tsaatan / Mörön route, 20/10/2026) gave every blog these four
"given" questions — two of them about the WORD 'moron', not the town Mörön — and the writer used a real
question for 3 of 30 FAQ questions.
"""
import ast
import json
import pathlib
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import generate, hygiene as h, quality_gates as qg
from services.acp_content_writing import service as writing_service
from services.acp_shared import question_topup as qt
from services.acp_shared import real_questions as rq
from services.seo_intelligence.dataforseo_client import DataForSEOClient
from shared.cost_guard import RunBudget

R7_GIVEN = [
    {"source": "segment_match", "question": "Does Mongolia have any volcanoes?"},
    {"source": "place_paa", "question": "What is the average altitude of Mongolia above sea level?"},
    {"source": "place_paa", "question": "Is moron a British word?"},
    {"source": "place_paa", "question": "What is moron in German?"},
]
WORD_QUESTIONS = [  # the r7 junk, then more of what Google asks for the word
    "Is moron a British word?", "What is moron in German?", "Is moron an American word?",
    "How do you spell moron?", "How do you say moron in Spanish?", "What is another word for moron?",
    "Is moron a slur?", "Is moron still used today?", "What is the plural of moron?",
    "Where does the word moron come from?", "Can you call someone a total moron?",
    "Is calling someone a moron illegal?",
]
TRAVEL_QUESTIONS = [
    "Does Mörön have an airport?", "How far is Moron from Lake Khovsgol?", "How cold is Mörön in winter?",
    "Is Mörön worth visiting?", "What is Mörön known for?", "What is the population of Mörön?",
    "Can you fly to Mörön?", "What is the history of Mörön?", "What language is spoken in Mörön?",
    "How deep is lake khovsgol?", "Is it rude to refuse tea in a ger?", "Does Mongolia have any volcanoes?",
    "Is the menu in English?", "Do I need a translator in Mongolia?",
]
MORON_SEED = (
    "Mörön — overnight in town before the lake\n"
    "Location: Mörön, Khövsgöl Province, Mongolia (do not place it in any other town, region or country)\n"
    "Source itinerary: Fly to Mörön; drive on to the lakeshore.\n"
    "Place background (checked facts about the place itself — you may use these):\n"
    "- Mörön has a small domestic airport with scheduled flights to Ulaanbaatar.\n"
    "- Mörön is the capital of Khövsgöl Province.\n"
)


# ---------------------------------------------------------------- (1) off-meaning

@pytest.mark.parametrize("q", WORD_QUESTIONS)
def test_a_place_name_used_as_a_word_is_off_meaning(q):
    assert rq.is_off_meaning(q, ["moron", "khovsgol"], lowercase_names=["moron"], countries=["Mongolia"])


@pytest.mark.parametrize("q", TRAVEL_QUESTIONS)
def test_real_travel_questions_stay(q):
    assert not rq.is_off_meaning(q, ["moron", "khovsgol", "tsaatan"], lowercase_names=["moron"],
                                 countries=["Mongolia"])


def test_lowercase_test_reads_only_the_given_place_names():
    # 'fly' opens a seed line ('Fly to Mörön') — it is not a place name, so lowercase 'fly' is fine
    assert not rq.is_off_meaning("Can you fly to Mörön?", ["fly", "moron"], lowercase_names=["moron"])
    # a generic place word written lowercase is never the 'word' case
    assert not rq.is_off_meaning("Is the lake frozen?", ["lake"], lowercase_names=["lake"])
    # no lowercase test unless the caller names single-word places (the default reads none)
    assert not rq.is_off_meaning("Is moron still used today?", ["moron"])
    assert rq.is_off_meaning("Is moron still used today?", ["moron"], lowercase_names=["moron"])


# S245 r7 review — real traveller questions the first lowercase rule dropped: lowercase text, tokens of
# multi-word names ('long' of Ha Long Bay, 'wall' of Great Wall) and place tokens that are English words.
REVIEW_PLACES = ["Uran Togoo", "Mörön", "Ha Long Bay", "Great Wall", "Flaming Cliffs", "Tiger's Nest",
                 "Golden Triangle", "Terelj", "Tsaatan", "Lake Khövsgöl"]
REVIEW_COUNTRIES = ["Mongolia", "Vietnam", "China", "Bhutan", "Thailand"]
MUST_KEEP = [
    "Is uran togoo an active volcano?", "what is moron mongolia like", "How long should I spend in Hanoi?",
    "How long does it take to walk the great wall?", "Is the great wall crowded?", "What are the flaming cliffs?",
    "Is tiger's nest hard to climb?", "How many steps to tiger's nest?", "Is the golden triangle dangerous?",
    "what to pack for terelj", "is terelj expensive", "who are the tsaatan people",
    # global word cues that never name a place (finding 2)
    "How do you say hello in Mongolian?", "What is the Mongolian word for hello?",
    "What words should I know before visiting Mongolia?", "Is English translation available on Mongolia tours?",
]


def _review_filter():
    scope = rq.QuestionScope(segment_ids=(), places=tuple(REVIEW_PLACES), countries=tuple(REVIEW_COUNTRIES))
    return rq.chain_filter_names(scope)


@pytest.mark.parametrize("q", MUST_KEEP)
def test_review_travel_questions_are_kept_with_the_chain_names(q):
    f = _review_filter()
    assert "long" in f["names"] and "wall" not in f["lowercase_names"]  # the review's own setting
    assert not rq.is_off_meaning(q, f["names"], f["lowercase_names"], f["countries"])
    assert [c.question for c in rq.merge([(rq.SOURCE_PLACE, [q])], **f)] == [q]


def test_only_whole_single_word_places_feed_the_lowercase_test():
    assert rq.single_word_places(REVIEW_PLACES, REVIEW_COUNTRIES) == {"moron", "terelj", "tsaatan"}
    assert rq.single_word_places(["Mongolia", "Lake", "Golden"], ["Mongolia"]) == set()


def test_the_lowercase_word_case_still_drops_with_the_chain_names():
    f = _review_filter()
    for q in ("Is moron still used today?", "What is the plural of moron?", "Is moron a British word?",
              "Is moron a slur?", "What is a word for moron?", "What rhymes with moron?"):
        assert rq.is_off_meaning(q, f["names"], f["lowercase_names"], f["countries"]), q


def test_autocomplete_tier_skips_the_lowercase_test():
    f = _review_filter()
    q = "is moron still used today"
    assert [c.question for c in rq.merge([(rq.SOURCE_AUTOCOMPLETE, [q])], **f)] == [q]
    assert rq.merge([(rq.SOURCE_PLACE, [q])], **f) == []


def test_scope_filter_drops_the_r7_junk_and_keeps_travel_questions():
    out = qg.paa_for_scope([g["question"] for g in R7_GIVEN] + ["Can you fly to Mörön?", "Does Mörön have an airport?"],
                           MORON_SEED, None, out_of_scope=("ulaanbaatar",))
    assert "Is moron a British word?" not in out and "What is moron in German?" not in out
    assert "Can you fly to Mörön?" in out and "Does Mörön have an airport?" in out


def test_chain_merge_drops_the_r7_junk():
    chain = rq.merge([(rq.SOURCE_SEGMENT, ["Does Mongolia have any volcanoes?"]),
                      (rq.SOURCE_PLACE, [g["question"] for g in R7_GIVEN[1:]])], names=["moron", "khovsgol"])
    assert [c.question for c in chain] == ["Does Mongolia have any volcanoes?",
                                           "What is the average altitude of Mongolia above sea level?"]


# ---------------------------------------------------------------- (2) selection logic

def test_a_light_rewording_still_counts_as_the_real_question():
    # r7: one blog's writer wrote 'Does Mongolia have volcanoes?' and it was recorded as `writer`
    out = rq.faq_sources(["Does Mongolia have volcanoes?"], R7_GIVEN[:1])
    assert out == [{"question": "Does Mongolia have volcanoes?", "source": "segment_match"}]


def test_missing_real_questions_lists_given_ones_the_faq_does_not_ask():
    faq = ["Does Mongolia have volcanoes?", "Who are the Tsaatan people of northern Mongolia?"]
    assert rq.missing_real_questions(faq, R7_GIVEN[:2]) == [
        "What is the average altitude of Mongolia above sea level?"]
    assert rq.missing_real_questions(faq, [{"question": "Who are the Tsaatan?", "source": "writer"}]) == []
    assert rq.missing_real_questions(faq, []) == []


POOL = [{"question": "Does Mongolia have any volcanoes?", "source": "segment_match"},
        {"question": "How deep is Lake Khövsgöl?", "source": "place_paa"},
        {"question": "Who are the Tsaatan people?", "source": "serp_topup"}]
W = [("Who built the camp at Sod?", "a"), ("What can you do at Uran Togoo?", "b"),
     ("What forest surrounds Lake Khövsgöl?", "c"), ("What is special about the reindeer?", "d")]
R = [("Does Mongolia have volcanoes?", "r1"), ("How deep is Lake Khövsgöl?", "r2"),
     ("Who are the Tsaatan people?", "r3")]


def test_no_real_pair_leaves_the_faq_alone():
    assert rq.real_first_pairs(W, POOL, max_pairs=5) == (W, 0)
    assert rq.real_first_pairs(W, [], max_pairs=5) == (W, 0)


def test_one_real_pair_goes_first_and_every_writer_pair_stays():
    # S245 r7 review: a reorder only — r7 piece 8db691f7 (1 real + 4 writer) keeps all 5 pairs
    out, n = rq.real_first_pairs(W[:2] + R[:1] + W[2:], POOL, max_pairs=5)
    assert n == 1 and out == [R[0]] + W


def test_one_real_and_three_writer_pairs_stay_four():
    # r7 piece fac93f45 (1 real + 3 writer) stays at 4 pairs
    out, n = rq.real_first_pairs(W[:3] + R[:1], POOL, max_pairs=5)
    assert n == 1 and out == [R[0]] + W[:3]


def test_writer_pairs_go_only_over_the_cap():
    out, n = rq.real_first_pairs(W + R[:2], POOL, max_pairs=5)
    assert n == 2 and out == [R[0], R[1], W[0], W[1], W[2]]
    out, n = rq.real_first_pairs(W + R, POOL, max_pairs=5)
    assert n == 3 and out == R + W[:2]


def test_real_first_faq_reorders_the_section_and_logs_it():
    text = qg.replace_faq_section("## Lake\n\nBody. Ask Terra to plan this trip.\n", W + R[:1])
    out, log = writing_service.real_first_faq(text, {"search_questions": POOL[:1]})
    assert [q for q, _ in qg.faq_pairs(out)] == [R[0][0]] + [q for q, _ in W]
    assert log == ["FAQ puts its 1 real search question(s) first"]
    assert "Ask Terra to plan this trip." in out.partition("## FAQ")[0]
    assert writing_service.real_first_faq(text, {}) == (text, [])


def test_a_candidate_the_scope_filter_dropped_never_makes_a_writer_pair_real():
    # only the in-scope search_questions count; the chain's dropped candidates stay provenance-only
    text = qg.replace_faq_section("## Lake\n\nBody. Ask Terra to plan this trip.\n", W[:2] + R[:1])
    ctx = {"search_questions": [], "real_questions": [{"q": R[0][0], "source": "segment_match"}]}
    assert writing_service.real_first_faq(text, ctx) == (text, [])
    assert writing_service.faq_pair_stats(text, ctx) == {"pairs": 3, "real": 0}
    ctx["search_questions"] = [{"question": R[0][0], "source": "segment_match"}]
    assert writing_service.faq_pair_stats(text, ctx) == {"pairs": 3, "real": 1}


# ---------------------------------------------------------------- (2) write_faq + the contract step

def _llm(reply):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=reply, cost_usd=0.006, model_used="haiku", input_tokens=10,
                                           output_tokens=10, stop_reason="end_turn", satellite_account="acc3",
                                           fallback_used=False)
    return fake


def test_write_faq_asks_for_the_real_questions_first_and_is_unchanged_without_them():
    reply = '{"faq": [{"q": "How deep is Lake Khövsgöl?", "a": "Lake Khövsgöl is deep."}]}'
    with patch.object(generate, "LLMClient", return_value=_llm(reply)) as m, \
         patch.object(generate, "record_call_sync") as log:
        generate.write_faq(source="S", body="B", paa_questions=["Is it cold?"],
                           required_questions=["How deep is Lake Khövsgöl?"])
    req = m.return_value.generate.call_args.args[0]
    assert "REAL QUESTIONS (" in req.user_prompt and "- How deep is Lake Khövsgöl?" in req.user_prompt
    assert req.user_prompt.index("REAL QUESTIONS") < req.user_prompt.index("SEARCH QUESTIONS")
    assert "keep the question's own words" in req.user_prompt
    assert log.call_args.kwargs["quality_signal"]["required_questions"] == 1
    with patch.object(generate, "LLMClient", return_value=_llm(reply)) as a, patch.object(generate, "record_call_sync"):
        generate.write_faq(source="S", body="B", paa_questions=["Is it cold?"])
    with patch.object(generate, "LLMClient", return_value=_llm(reply)) as b, \
         patch.object(generate, "record_call_sync") as log2:
        generate.write_faq(source="S", body="B", paa_questions=["Is it cold?"], required_questions=[])
    pa, pb = a.return_value.generate.call_args.args[0], b.return_value.generate.call_args.args[0]
    assert pa.user_prompt == pb.user_prompt and pa.system_prompt == pb.system_prompt
    assert "REAL QUESTIONS" not in pa.user_prompt
    assert "required_questions" not in log2.call_args.kwargs["quality_signal"]


SEED = (
    "Lake Khövsgöl — trekking along the shore\n"
    "Location: Khövsgöl Lake, Khövsgöl Province, Mongolia (do not place it in any other town, region or country)\n"
    "Source itinerary: Trek the lakeshore; boat trips and horse riding are offered.\n"
    "Place background (checked facts about the place itself — you may use these):\n"
    "- Lake Khövsgöl is up to 262 metres deep.\n"
    "- Lake Khövsgöl holds an estimated 70 percent of Mongolia's fresh water.\n"
    "- The lake freezes solidly in winter, typically from January through April.\n"
)
BODY = "## Lake Khövsgöl\n\nThe lake is wide and cold. Ask Terra to plan this trip.\n"
WRITER_FAQ = [
    ("When does Lake Khövsgöl freeze?",
     "Lake Khövsgöl freezes solidly in winter, typically from January through April, when the surface turns "
     "to thick ice across its whole length."),
    ("What share of Mongolia's fresh water does Lake Khövsgöl hold?",
     "Lake Khövsgöl holds an estimated 70 percent of Mongolia's fresh water, which makes it the country's "
     "single most important freshwater reserve."),
    ("What activities are offered on Lake Khövsgöl?",
     "Lake Khövsgöl offers boat trips and horse riding along the shore, alongside trekking on the lakeshore "
     "trails."),
]
REAL_PAIR = ("How deep is Lake Khövsgöl?",
             "Lake Khövsgöl is up to 262 metres deep, making it one of the deepest lakes in this part of Asia.")


def _ctx(**extra):
    return {"atom_text": SEED, "facts_text": "", "brand_audience": {"brand_name": "Terra"}, "tenant_id": None,
            "cta": "Ask Terra to plan this trip.",
            "search_questions": [{"question": "How deep is Lake Khövsgöl?", "source": "place_paa"}],
            "paa_questions": ["How deep is Lake Khövsgöl?"], **extra}


@pytest.mark.asyncio
async def test_full_faq_without_the_real_question_regenerates_once_and_puts_it_first():
    writer = qg.replace_faq_section(BODY, WRITER_FAQ)
    with patch.object(writing_service, "write_faq", return_value=([REAL_PAIR], 0.006)) as wf:
        out, cost, log = await writing_service._enforce_faq_contract(
            writer, context=_ctx(), hyg=h.HygieneContext(seed=SEED), request_id=uuid.uuid4(),
            brand_ignore=frozenset(), required=["How deep is Lake Khövsgöl?"])
    assert wf.call_count == 1
    assert wf.call_args.kwargs["required_questions"] == ["How deep is Lake Khövsgöl?"]
    assert "paa_questions" not in wf.call_args.kwargs  # the required one is not listed twice
    assert [q for q, _ in qg.faq_pairs(out)] == [REAL_PAIR[0]] + [q for q, _ in WRITER_FAQ]
    assert cost == 0.006 and "real questions 0 → 1" in log[0]


@pytest.mark.asyncio
async def test_an_off_meaning_required_question_is_not_sent_to_the_regeneration():
    writer = qg.replace_faq_section(BODY, WRITER_FAQ)
    seed = SEED.replace("- The lake freezes", "- Mörön has a small airport.\n- The lake freezes")
    with patch.object(writing_service, "write_faq", return_value=([REAL_PAIR], 0.006)) as wf:
        await writing_service._enforce_faq_contract(
            writer, context=_ctx(atom_text=seed), hyg=h.HygieneContext(seed=seed), request_id=uuid.uuid4(),
            brand_ignore=frozenset(), required=["Is moron a British word?", "How deep is Lake Khövsgöl?"])
    assert wf.call_args.kwargs["required_questions"] == ["How deep is Lake Khövsgöl?"]
    with patch.object(writing_service, "write_faq") as wf2:
        out, cost, log = await writing_service._enforce_faq_contract(
            writer, context=_ctx(atom_text=seed), hyg=h.HygieneContext(seed=seed), request_id=uuid.uuid4(),
            brand_ignore=frozenset(), required=["Is moron a British word?"])
    wf2.assert_not_called()
    assert (out, cost, log) == (writer, 0.0, [])


@pytest.mark.asyncio
async def test_regeneration_that_adds_no_real_question_is_not_kept():
    writer = qg.replace_faq_section(BODY, WRITER_FAQ)
    other = ("Can you ride horses at Lake Khövsgöl?",
             "Lake Khövsgöl offers horse riding along the shore as one of its activities for visitors.")
    with patch.object(writing_service, "write_faq", return_value=([other], 0.006)):
        out, _, log = await writing_service._enforce_faq_contract(
            writer, context=_ctx(), hyg=h.HygieneContext(seed=SEED), request_id=uuid.uuid4(),
            brand_ignore=frozenset(), required=["How deep is Lake Khövsgöl?"])
    assert out == writer and log[0].startswith("FAQ regeneration not kept")


@pytest.mark.asyncio
async def test_a_full_faq_that_already_asks_the_real_questions_makes_no_call():
    writer = qg.replace_faq_section(BODY, [REAL_PAIR] + WRITER_FAQ[:2])
    with patch.object(writing_service, "write_faq") as wf:
        out, cost, log = await writing_service._enforce_faq_contract(
            writer, context=_ctx(), hyg=h.HygieneContext(seed=SEED), request_id=uuid.uuid4(),
            brand_ignore=frozenset(), required=[])
    wf.assert_not_called()
    assert (out, cost, log) == (writer, 0.0, [])


@pytest.mark.asyncio
async def test_job_asks_the_regeneration_for_the_real_questions_the_writer_skipped():
    writer = qg.replace_faq_section(BODY, WRITER_FAQ)
    row = {"piece_id": str(uuid.uuid4()), "attempt_number": 1, "status": "approved"}
    passed = {"passed": True, "gate_ledger": [], "first_failure": None, "flags": []}
    ctx = {"atom_text": SEED, "goal": {"key": "promotion"}, "channel_style": {"channel": "blog"},
           "brand_audience": {}, "chosen": {}, "cta": "Ask Terra to plan this trip.", "destination": None,
           "trip_name": None, "brand_rubric_text": "", "channel": "blog", "atom_id": "a1",
           "people_also_ask": ["How deep is Lake Khövsgöl?"],
           "real_questions": [{"q": "How deep is Lake Khövsgöl?", "source": "place_paa"}]}
    with patch.object(writing_service, "write_content", return_value=(writer, 0.01, {}, None)), \
         patch.object(writing_service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda m, *a, **k: (m, 0.0))), \
         patch.object(writing_service, "write_faq", return_value=([REAL_PAIR], 0.006)) as wf, \
         patch.object(writing_service, "run_quality_gates", return_value=passed), \
         patch.object(writing_service, "_finalize_piece", new=AsyncMock(return_value=row)) as fin:
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    assert wf.call_args.kwargs["required_questions"] == ["How deep is Lake Khövsgöl?"]
    wi = fin.call_args.kwargs["writer_input"]
    faq = wi["faq_questions"]
    assert faq[0] == {"question": "How deep is Lake Khövsgöl?", "source": "place_paa"}
    assert [f["source"] for f in faq[1:]] == ["writer", "writer", "writer"]
    # the line-2 checkpoint can see the real pair was added, no writer pair removed
    assert wi["faq_pairs_before"] == {"pairs": 3, "real": 0}
    assert wi["faq_pairs_after"] == {"pairs": 4, "real": 1}


# ---------------------------------------------------------------- (4) the measure never counts junk

def test_piece_stats_never_count_off_meaning_questions():
    wi = {"search_questions": R7_GIVEN,
          "faq_questions": [{"question": "Does Mongolia have volcanoes?", "source": "segment_match"},
                            {"question": "Is moron a British word?", "source": "place_paa"},
                            {"question": "Who are the Tsaatan people?", "source": "writer"}]}
    assert rq.piece_question_stats(wi, ["moron", "khovsgol"]) == {
        "given_real": 2, "given_off_meaning": 2, "faq_total": 3, "faq_real": 1}
    assert rq.count_real(R7_GIVEN, ["moron"]) == 2


# ---------------------------------------------------------------- (3) SERP top-up

def test_topup_keyword_is_folded_place_and_country():
    assert rq.topup_keyword("Mörön", "Mongolia") == "moron mongolia"
    assert rq.topup_keyword("Lake Khövsgöl", "Mongolia") == "lake khovsgol mongolia"
    scope = rq.QuestionScope(segment_ids=(), places=("Mörön", "Uran Togoo"), countries=("Mongolia",))
    assert rq.topup_keywords(scope) == {"moron mongolia", "uran togoo mongolia"}


class _Conn:
    def __init__(self, demand=(), matches=(), prior=0.0):
        self.demand, self.matches, self.calls, self.executed = list(demand), list(matches), [], []
        self.prior = prior

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "atom_matches" in sql:
            return [{"query": q} for q in self.matches]
        if "shared.dfs_call_log" in sql:
            assert args[0] == "serp_advanced_paa_deep"
            return [{"spent": self.prior}]
        if "SELECT keyword FROM acp_contract.search_demand" in sql:  # _BOUGHT_SQL
            return [{"keyword": r["keyword"]} for r in self.demand
                    if r["market"] == args[0] and r["keyword"] in args[1]]
        if "acp_contract.search_demand" in sql:
            return list(self.demand)
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _pool(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn
    pool.acquire = acquire
    return pool


TOP = rq.TOPUP_MARKET


def _row(keyword, qs, market="US"):
    return {"keyword": keyword, "market": market, "search_volume": None, "people_also_ask": json.dumps(qs)}


@pytest.mark.asyncio
async def test_topup_rows_carry_their_own_label_and_place_paa_leaves_them():
    scope = rq.QuestionScope(segment_ids=(), places=("Mörön",), countries=("Mongolia",))
    conn = _Conn(demand=[_row("moron", ["Is moron a bad word?"]),
                         _row("moron mongolia", ["Does Mörön have an airport?", "How cold is Mörön in winter?"],
                              market=TOP)])
    assert await rq.PlacePaaSource().fetch(conn, scope) == ["Is moron a bad word?"]
    sql, args = conn.calls[-1]
    assert "market <> $2" in sql and args[1] == TOP
    assert await rq.SerpTopupSource().fetch(conn, scope) == ["Does Mörön have an airport?",
                                                             "How cold is Mörön in winter?"]
    sql, args = conn.calls[-1]
    assert "market = $2" in sql and args == (["moron mongolia"], TOP)
    assert [s.name for s in rq.DB_SOURCES] == ["segment_match", "place_paa", "serp_topup"]


@pytest.mark.asyncio
async def test_a_research_row_with_the_topup_key_stays_place_paa():
    # S245 r7 review: Segment research may buy the exact '<place> <country>' keyword (market US); that
    # row is its own (place_paa) and the top-up's row lives under TOPUP_MARKET — no clash either way
    scope = rq.QuestionScope(segment_ids=(), places=("Lake Khövsgöl",), countries=("Mongolia",))
    conn = _Conn(demand=[_row("lake khovsgol mongolia", ["How deep is Lake Khövsgöl?"]),
                         _row("lake khovsgol mongolia", ["Can you swim in Lake Khövsgöl?"], market=TOP)])
    assert await rq.PlacePaaSource().fetch(conn, scope) == ["How deep is Lake Khövsgöl?"]
    assert await rq.SerpTopupSource().fetch(conn, scope) == ["Can you swim in Lake Khövsgöl?"]
    assert TOP not in ("US", "UK", "AU", "DE", "FR", "NL")


def test_topup_store_sql_uses_its_own_market_and_refreshes_retrieved_on():
    assert "retrieved_on = excluded.retrieved_on" in qt._STORE_SQL
    assert qt.SERP_PAA_DEEP_ENDPOINT == __import__(
        "services.seo_intelligence.dataforseo_client", fromlist=["x"]).SERP_PAA_DEEP_ENDPOINT
    from shared.cost_guard import DFS_CALL_ESTIMATE_USD
    assert DFS_CALL_ESTIMATE_USD[qt.SERP_PAA_DEEP_ENDPOINT] == qt.TOPUP_CALL_ESTIMATE_USD


def test_collect_needs_groups_by_place_and_country_and_skips_generic_places():
    needs = qt.collect_needs([("Mörön", "Mongolia", "s1"), ("Mörön", "Mongolia", "s2"), ("Lake", "Mongolia", "s3"),
                              ("Uran Togoo", "Mongolia", "s4"), ("Mörön", "", "s5")])
    assert sorted(needs) == ["moron mongolia", "uran togoo mongolia"]
    assert needs["moron mongolia"].segment_ids == ["s1", "s2"] and needs["moron mongolia"].used_by == 2
    assert needs["moron mongolia"].query == "Mörön Mongolia"


def _need(key, real_now=0, used_by=1, topped=False):
    return qt.PlaceNeed(place=key, country="Mongolia", keyword=key, query=key, real_now=real_now,
                        used_by=used_by, topped_up=topped)


def test_plan_orders_targets_and_never_plans_past_the_cap():
    needs = [_need("a", real_now=2), _need("b", real_now=0, used_by=3), _need("c", real_now=5),
             _need("d", topped=True), _need("e", real_now=1)]
    p = qt.plan(needs)
    assert [t["keyword"] for t in p["targets"]] == ["b", "e", "a"]
    assert p["places_needing_serp"] == 3 and p["places_enough"] == 2
    assert p["estimated_cost_usd"] == pytest.approx(3 * qt.TOPUP_CALL_ESTIMATE_USD)
    many = [_need(f"p{i:03d}") for i in range(400)]
    big = qt.plan(many, cap_usd=10.0)  # a higher cap is clamped to the $3 hard cap
    assert big["cap_usd"] == 3.0 and big["planned_calls"] == 300 and big["estimated_cost_usd"] <= 3.0
    assert len(big["left_out_by_cap"]) == 100


def _fake_needs(*keys):
    return AsyncMock(return_value=[_need(k) for k in keys])


@pytest.mark.asyncio
async def test_dry_run_buys_nothing_and_writes_nothing():
    client = MagicMock()
    conn = _Conn(prior=0.4)
    with patch.object(qt, "find_needs", _fake_needs("moron mongolia")):
        out = await qt.run_topup(_pool(conn), client=client)
    assert out["dry_run"] is True and out["planned_calls"] == 1
    assert out["prior_spent_usd"] == 0.4 and out["left_usd"] == pytest.approx(2.6)
    client.fetch_people_also_ask_deep.assert_not_called()
    assert conn.executed == []


@pytest.mark.asyncio
async def test_cap_above_three_dollars_is_refused():
    with pytest.raises(ValueError):
        await qt.run_topup(MagicMock(), dry_run=False, cap_usd=3.5)


def _buying_client(budget, cost=0.012, empty=()):
    client = MagicMock()
    client.budget = budget

    async def buy(query, **kw):
        budget.charge(cost)
        return ([] if query in empty else [f"Does {query} have an airport?"]), ["example.com"]
    client.fetch_people_also_ask_deep = AsyncMock(side_effect=buy)
    return client


def _budget():
    return RunBudget(provider="dfs", scope="job:aa779_serp_topup", per_run_usd=None, per_day_usd=None)


@pytest.mark.asyncio
async def test_the_three_dollar_cap_counts_earlier_runs():
    # $2.95 already spent: what is left ($0.05) caps this run, so the total stays within $3
    conn = _Conn(prior=2.95)
    budget = _budget()
    client = _buying_client(budget)
    with patch.object(qt, "find_needs", _fake_needs(*[f"p{i}" for i in range(20)])):
        out = await qt.run_topup(_pool(conn), dry_run=False, cap_usd=3.0, client=client, budget=budget)
    assert out["planned_calls"] == 5 and out["prior_spent_usd"] == 2.95
    assert client.fetch_people_also_ask_deep.call_count == 4  # the 5th estimate would pass $0.05
    assert out["total_spent_usd"] <= qt.TOPUP_CAP_USD and out["stopped"]


@pytest.mark.asyncio
async def test_no_call_when_the_cap_is_already_spent():
    for prior in (2.995, 3.0, 3.4):
        conn = _Conn(prior=prior)
        budget = _budget()
        client = _buying_client(budget)
        with patch.object(qt, "find_needs", _fake_needs("a", "b")):
            out = await qt.run_topup(_pool(conn), dry_run=False, cap_usd=3.0, client=client, budget=budget)
        client.fetch_people_also_ask_deep.assert_not_called()
        assert out["planned_calls"] == 0 and conn.executed == []
        if prior >= 3.0:
            assert "already spent" in out["refused"]


@pytest.mark.asyncio
async def test_a_place_with_no_question_is_marked_bought_and_skipped_next_time():
    conn = _Conn()
    budget = _budget()
    client = _buying_client(budget, empty=("b",))
    with patch.object(qt, "find_needs", _fake_needs("a", "b")):
        out = await qt.run_topup(_pool(conn), dry_run=False, cap_usd=1.0, client=client, budget=budget)
    assert out["stored_places"] == 1 and out["empty_places"] == 1
    stored = {args[0]: (args[1], json.loads(args[2])) for _, args in conn.executed}
    assert stored == {"a": (TOP, ["Does a have an airport?"]), "b": (TOP, [])}
    # the rerun's count sees the empty marker row as bought
    need = _need("uran togoo mongolia")
    need.place = "Uran Togoo"
    marker = _Conn(demand=[_row("uran togoo mongolia", [], market=TOP)])
    real, need.topped_up = await qt._count_real(marker, need)
    assert real == 0 and need.topped_up is True and need.needs_serp is False


@pytest.mark.asyncio
async def test_real_run_stores_each_place_and_stops_at_the_budget():
    conn = _Conn()
    budget = RunBudget(provider="dfs", scope="job:aa779_serp_topup", per_run_usd=None, per_day_usd=None)
    client = MagicMock()
    client.budget = budget

    async def buy(query, **kw):
        assert kw["click_depth"] == 2 and kw["location_code"] == 2840
        budget.charge(0.012)  # what DataForSEO billed
        return [f"Does {query} have an airport?"], ["example.com"]
    client.fetch_people_also_ask_deep = AsyncMock(side_effect=buy)
    with patch.object(qt, "find_needs", _fake_needs("a", "b", "c")):
        out = await qt.run_topup(_pool(conn), dry_run=False, cap_usd=0.03, client=client, budget=budget)
    # 0.012 + 0.012 spent; a third call (estimate 0.01) would pass 0.03 → stopped before buying it
    assert [b["keyword"] for b in out["bought"]] == ["a", "b"] and out["stored_places"] == 2
    assert out["stopped"] and "per-run budget" in out["stopped"] and out["spent_usd"] == pytest.approx(0.024)
    sql, args = conn.executed[0]
    assert "INSERT INTO acp_contract.search_demand" in sql and args[0] == "a" and args[1] == TOP
    assert json.loads(args[2]) == ["Does a have an airport?"]


@pytest.mark.asyncio
async def test_count_real_uses_the_chain_filters_and_sees_an_earlier_topup():
    need = _need("moron mongolia")
    need.place = "Mörön"
    conn = _Conn(demand=[_row("moron", ["Is moron a British word?", "What is moron in German?",
                                        "Is moron still used today?"]),
                         _row("moron mongolia", ["Does Mörön have an airport?"], market=TOP)])
    real, topped = await qt._count_real(conn, need)
    assert real == 1 and topped is True
    fresh = _Conn(demand=[_row("moron", ["Is moron a British word?"])])
    assert await qt._count_real(fresh, need) == (0, False)


@pytest.mark.asyncio
async def test_serp_payload_carries_click_depth_only_when_asked():
    sent = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": []}]}]}

    class _Http:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, auth=None, json=None):
            sent.append(json)
            return _Resp()

    client = DataForSEOClient(login="l", password="p")
    with patch("services.seo_intelligence.dataforseo_client.httpx.AsyncClient", _Http), \
         patch("services.seo_intelligence.dataforseo_client.record_dfs_call_sync") as log:
        await client.fetch_people_also_ask("moron")
        await client.fetch_people_also_ask_deep("Mörön Mongolia", click_depth=2)
    # the top-up's calls carry their own dfs_call_log label (its $3 cap is summed across runs)
    assert [c.kwargs["endpoint"] for c in log.call_args_list] == ["serp_advanced", "serp_advanced_paa_deep"]
    assert "people_also_ask_click_depth" not in sent[0][0]
    assert sent[1][0]["people_also_ask_click_depth"] == 2 and sent[1][0]["keyword"] == "Mörön Mongolia"


def test_deep_paa_parse_reads_nested_expanded_questions():
    data = {"tasks": [{"result": [{"items": [
        {"type": "organic", "title": "not a question", "domain": "x.com"},
        {"type": "people_also_ask", "items": [
            {"type": "people_also_ask_element", "title": "Does Mörön have an airport?",
             "expanded_element": [{"type": "people_also_ask_expanded_element", "title": "Mörön Airport"}],
             "items": [{"type": "people_also_ask_element", "title": "How far is Mörön from Lake Khövsgöl?"}]},
            {"type": "people_also_ask_element", "title": "Does Mörön have an airport?"},
            {"type": "people_also_ask_element", "title": "What is Mörön known for?"}]}]}]}]}
    out = DataForSEOClient(login="l", password="p")._parse_paa_deep(data)
    assert out == ["Does Mörön have an airport?", "How far is Mörön from Lake Khövsgöl?", "What is Mörön known for?"]


_DEEP = {"fetch_people_also_ask_deep", "people_also_ask_click_depth"}


def _deep_serp_hits(tree) -> list[int]:
    """Lines that import question_topup, call fetch_people_also_ask_deep or pass / name
    people_also_ask_click_depth."""
    hits = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.keyword) and node.arg:
            names = [node.arg]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]
        if any(n.endswith("question_topup") or n in _DEEP for n in names):
            hits.append(getattr(node, "lineno", 0))
    return hits


def test_nothing_but_the_admin_script_reaches_the_paid_deep_serp():
    """AA-646 — no router, job handler or tenant path may reach the paid top-up: no import of
    question_topup, and no call to fetch_people_also_ask_deep nor a people_also_ask_click_depth
    argument (or string) outside question_topup.py and the DFS client itself."""
    root = pathlib.Path(__file__).resolve().parents[2]
    allowed = {"question_topup.py", "dataforseo_client.py"}
    offenders = []
    for folder in ("api", "worker", "services", "shared", "portal", "frontend"):
        for path in (root / folder).rglob("*.py"):
            if path.name in allowed:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            offenders += [f"{path.relative_to(root)}:{n}" for n in _deep_serp_hits(tree)]
    assert offenders == []


@pytest.mark.parametrize("src", [
    "from services.acp_shared import question_topup\n",
    "async def f(c):\n    return await c.fetch_people_also_ask_deep('x')\n",
    "async def f(c):\n    return await c._serp_advanced('x', people_also_ask_click_depth=2)\n",
    "PAYLOAD = {'people_also_ask_click_depth': 2}\n",
])
def test_the_guard_catches_every_way_to_the_deep_serp(src):
    assert _deep_serp_hits(ast.parse(src))
    assert not _deep_serp_hits(ast.parse("async def f(c):\n    return await c.fetch_people_also_ask('x')\n"))
