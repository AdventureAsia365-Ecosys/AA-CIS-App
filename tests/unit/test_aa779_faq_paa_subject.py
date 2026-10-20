"""AA-779 (S245) — FAQ: People Also Ask questions first, and every answer names its question's subject.

Fixture pairs are the real S234 r4 / S235 r5 blog FAQ pairs (Mongolia route, Atlas & Hearth and Terra
Family Expeditions). The five "broken" ones are the pairs the r4 / r5 panels called non-answers; the
clean ones are pairs the panels did not flag. A replay over all 62 r4 + r5 blog FAQ pairs gave 5 flags,
exactly those five, and 0 on the other 57 (the S245 notes file has the method).
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import generate, hygiene as h, prompts, quality_gates as qg
from services.acp_content_writing import service as writing_service

# ---------------------------------------------------------------- real pairs

BROKEN = [
    # r4 #5 Q1 (panel: "FAQ Q1 'Who are the Tsaatan?' not answered")
    ("Who are the Tsaatan, and where do they live?",
     "Their seasonal camps — called urts, teepee-style dwellings — are moved several times a year as the "
     "reindeer require.", "no_subject"),
    # r4 #19 Q5 (panel: "not answered") — names the Tsaatan, but only says what is NOT the case
    ("What do the Tsaatan use their reindeer for?",
     "Unlike many other reindeer-herding traditions, the Tsaatan do not primarily raise them for meat.",
     "negative_only"),
    # r5 #5 (panel: "Broken FAQ … without naming them")
    ("What can we do at Lake Khövsgöl beyond trekking?",
     "These activities are available to choose from based on what interests your family most.", "no_subject"),
    # r5 #15 (panel: "dangling 'here'")
    ("What is the role of Mörön on this route?",
     "The route stops here for lunch on day 4 before continuing to the lakeshore.", "no_subject"),
    # r5 #18 (panel: "Broken FAQ … non-answer"; the gate approved the piece)
    ("What animals do the Tsaatan use their reindeer for?",
     "Unlike many other reindeer-herding cultures, meat production is not the primary purpose.", "no_subject"),
]

CLEAN = [
    ("What is the ecological significance of Lake Khövsgöl's location?",
     "The lake sits at the transition zone between Central Asian steppe grasslands to the south and Siberian "
     "taiga boreal forest to the north, giving the surrounding area unusually high biodiversity."),
    ("How deep is the crater at Uran Togoo?",
     "The crater is roughly 50 to 60 metres deep, with a width of about 600 metres."),
    ("Does Mörön have an airport?",
     "Yes. Mörön has a small domestic airport with scheduled flights connecting it to Ulaanbaatar."),
    ("Who are the Tsaatan, and where do they live?",
     "The Tsaatan (also known as the Dukha) are a small semi-nomadic indigenous community of reindeer herders "
     "living in the remote taiga of northern Khövsgöl Province."),
    ("What happens to Lake Khövsgöl in winter?",
     "The lake freezes solidly from January through April, and historically the ice was thick enough to be "
     "used as an ice road."),
    ("Can I walk down into the volcanic crater at Uran Togoo?",
     "Yes — the caldera floor is accessible on foot, and you can trek across the volcanic terrain including "
     "walking down into the crater and around its rim."),
    # 'not just … but' adds: not a negation-only answer (r4 #15 Q1)
    ("What does Lake Khövsgöl's freshwater significance actually mean in practical terms?",
     "Lake Khövsgöl holds an estimated 70 percent of Mongolia's fresh water — figures that reflect not just "
     "the lake's volume but the relative scarcity of surface freshwater globally."),
    # a negation followed by what IS the case (r5 #14)
    ("Who are the Tsaatan, and how do they differ from other reindeer-herding communities?",
     "Unlike many reindeer-herding traditions elsewhere, the Tsaatan do not raise their reindeer primarily for "
     "meat — instead, the animals are used for riding, as pack animals, and for their milk."),
    ("What kind of dwellings do the Tsaatan live in?",
     "They live in seasonal camps of teepee-style dwellings called urts."),
    ("Is Mörön far from Lake Khövsgöl?",
     "Mörön serves as the main hub and gateway town for travellers heading to Lake Khövsgöl, located roughly "
     "100 kilometres to the north."),
]

# r5 #17 Q3: 'The volcano' stands for Uran Togoo because the seed says so
VOLCANO_Q = "How long ago did Uran Togoo last erupt?"
VOLCANO_A = ("The volcano last erupted approximately 20,000 to 24,000 years ago, making it geologically recent "
             "in volcanic terms despite its dormant state today.")
VOLCANO_SEED = "- Uran Togoo is considered the largest of several dormant volcanoes in the surrounding valleys."


@pytest.mark.parametrize("q,a,why", BROKEN)
def test_the_panel_non_answers_are_caught(q, a, why):
    assert qg.faq_answer_problem(q, a) == why


@pytest.mark.parametrize("q,a", CLEAN)
def test_panel_clean_answers_pass(q, a):
    assert qg.faq_answer_problem(q, a) is None


def test_a_class_noun_names_the_subject_only_when_the_source_ties_them():
    assert qg.faq_answer_problem(VOLCANO_Q, VOLCANO_A) == "no_subject"
    assert qg.faq_answer_problem(VOLCANO_Q, VOLCANO_A, VOLCANO_SEED) is None
    # a trip word never stands for a name, even when a sentence has both
    assert qg.faq_answer_problem(BROKEN[3][0], BROKEN[3][1],
                                 "The route stops near Mörön for lunch.") == "no_subject"


def test_subject_tokens():
    subject, names = qg.faq_subject_tokens("What is the ecological significance of Lake Khövsgöl's location?")
    assert {"lake", "khovsgol"} <= subject and "significance" not in subject and names == {"lake", "khovsgol"}
    subject, names = qg.faq_subject_tokens("What animals do the Tsaatan use their reindeer for?")
    assert subject == {"animal", "tsaatan"} and names == {"tsaatan"}  # 'their reindeer' is not the subject
    assert qg.faq_subject_tokens("How deep is the crater at Uran Togoo?")[0] == {"crater", "uran", "togoo"}
    assert qg.faq_subject_tokens("Can you swim?") == (set(), set())


def test_a_question_without_a_subject_and_a_yes_no_negation_pass():
    assert qg.faq_answer_problem("Can you swim?", "Brave visitors swim in August.") is None
    assert qg.faq_answer_problem("Is Lake Khövsgöl open in winter?", "No, the lake is frozen then.") is None
    assert qg.faq_answer_problem("Who are the Tsaatan?", "") is None


# ---------------------------------------------------------------- the dropper

BODY = "## Lake Khövsgöl\n\nThe lake is deep and cold. Ask Terra to plan this trip.\n"


def _blog(pairs):
    return qg.replace_faq_section(BODY, pairs)


def test_dropper_drops_while_more_than_three_remain():
    text = _blog(CLEAN[:3] + [BROKEN[0][:2], BROKEN[2][:2]])
    out, log, short = qg.drop_subjectless_faq(text)
    assert [q for q, _ in qg.faq_pairs(out)] == [q for q, _ in CLEAN[:3]]
    assert short == 0 and len(log) == 2 and all(line.startswith("dropped FAQ pair") for line in log)
    assert "Ask Terra to plan this trip." in out.partition("## FAQ")[0]


def test_dropper_keeps_failing_pairs_at_the_minimum_and_reports_them():
    text = _blog(CLEAN[:2] + [BROKEN[0][:2], BROKEN[4][:2]])
    out, log, short = qg.drop_subjectless_faq(text)
    assert qg.faq_pair_count(out) == 3 and short == 1
    assert qg.faq_pairs(out)[2][0] == BROKEN[4][0]  # reading order: the first failing pair went
    assert log[1].startswith("kept FAQ pair at the 3-pair minimum")


def test_dropper_is_a_no_op_without_a_faq_or_a_failure():
    assert qg.drop_subjectless_faq(BODY) == (BODY, [], 0)
    clean = _blog(CLEAN[:4])
    assert qg.drop_subjectless_faq(clean) == (clean, [], 0)


def test_dropper_reads_the_source_for_the_alias():
    text = _blog(CLEAN[:3] + [(VOLCANO_Q, VOLCANO_A)])
    assert qg.faq_pair_count(qg.drop_subjectless_faq(text)[0]) == 3
    assert qg.faq_pair_count(qg.drop_subjectless_faq(text, VOLCANO_SEED)[0]) == 4


# ---------------------------------------------------------------- People Also Ask filter

PAA_SEED = (
    "Lake Khövsgöl — trekking along the shore\n"
    "Location: Khövsgöl Lake, Khövsgöl Province, Mongolia (do not place it in any other town, region or country)\n"
    "Source itinerary: Trek the lakeshore; boat trips and horse riding are offered.\n"
    "Place background (checked facts about the place itself — you may use these):\n"
    "- Lake Khövsgöl holds an estimated 70 percent of Mongolia's fresh water.\n"
    "- The lake freezes solidly in winter, typically from January through April.\n"
)


def test_paa_filter_keeps_in_scope_answerable_questions():
    paa = ["how deep is lake khovsgol", "How deep is lake Khövsgöl?",  # duplicate after folding and '?'
           "What is the best way to get from Khatgal to the Gobi desert?",  # out of scope
           "How much does a Khövsgöl lake tour cost?",  # price: the seed cannot answer
           "Is Mongolia safe for tourists?",  # the country only
           "Why is the freshwater of the lake frozen in winter?",  # 2 seed content words, no name
           "Hi?", 42, None]
    out = qg.paa_for_scope(paa, PAA_SEED, None, out_of_scope=("gobi", "khatgal"))
    assert out == ["How deep is lake khovsgol?", "Why is the freshwater of the lake frozen in winter?"]


def test_paa_filter_lets_facts_answer_a_topic_and_caps():
    q = "What is the best time to visit Lake Khövsgöl?"
    assert qg.paa_for_scope([q], PAA_SEED.replace("freezes solidly in winter", "freezes solidly"), None) == []
    assert qg.paa_for_scope([q], PAA_SEED, "Season: June to August is the main season.") == [q]
    many = [f"Question {i} about Lake Khövsgöl?" for i in range(10)]
    assert len(qg.paa_for_scope(many, PAA_SEED)) == qg.PAA_MAX_QUESTIONS
    assert qg.paa_for_scope(None, PAA_SEED) == []


# ---------------------------------------------------------------- prompts

def _llm(reply):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=reply, cost_usd=0.006, model_used="haiku", input_tokens=10,
                                           output_tokens=10, stop_reason="end_turn", satellite_account="acc3",
                                           fallback_used=False)
    return fake


def test_write_faq_carries_search_questions_and_the_subject_rule():
    reply = '{"faq": [{"q": "How deep is Lake Khövsgöl?", "a": "Lake Khövsgöl is deep."}]}'
    with patch.object(generate, "LLMClient", return_value=_llm(reply)) as m, \
         patch.object(generate, "record_call_sync") as log:
        generate.write_faq(source=PAA_SEED, body=BODY, paa_questions=["How deep is Lake Khövsgöl?"])
    req = m.return_value.generate.call_args.args[0]
    assert "SEARCH QUESTIONS (what readers search for; use one only when a SOURCE fact answers it):\n" \
           "- How deep is Lake Khövsgöl?" in req.user_prompt
    assert req.user_prompt.index("SEARCH QUESTIONS") < req.user_prompt.index("POST (do not repeat")
    assert "When SEARCH QUESTIONS are given, prefer them" in req.system_prompt
    assert "Every answer names the subject of its question" in req.system_prompt
    assert "40-60 words per answer" in req.system_prompt and "One or two sentences" not in req.system_prompt
    assert "Each question is one a reader" not in req.system_prompt
    assert log.call_args.kwargs["quality_signal"]["paa_questions"] == 1
    with patch.object(generate, "LLMClient", return_value=_llm(reply)) as m, patch.object(generate, "record_call_sync"):
        generate.write_faq(source=PAA_SEED, body=BODY)
    assert "SEARCH QUESTIONS" not in m.return_value.generate.call_args.args[0].user_prompt


def _prompt(channel, **kw):
    style = {"channel": channel, "display_name": channel, "use_when": "u", "structure": "s", "style": "st",
             "avoid": "a"}
    return prompts.build_user_prompt(
        content_seed=PAA_SEED, goal={"name": "g", "description": "d", "logic": "l"}, channel_style=style,
        brand_audience={"brand_name": "Terra"}, angle={"name": "n", "why_it_works": "w", "best_final_style": "b"},
        cta="Ask Terra.", destination="Mongolia", trip_name="Mongolia", scope="SCOPE: days 5-8", **kw)


def test_blog_writer_prompt_gets_the_search_questions_and_the_faq_rule():
    p = _prompt("blog", search_questions=["How deep is Lake Khövsgöl?"])
    assert "SEARCH QUESTIONS (what readers search for about these places" in p
    assert "- How deep is Lake Khövsgöl?" in p
    assert p.index("CONTENT SEED") < p.index("SEARCH QUESTIONS") < p.index("BLOG-SPECIFIC FORMAT")
    assert "FAQ: 3-4 questions" in p and "names the subject of its question" in p and "40-60 words" in p
    # the DESTINATION / TRIP / SCOPE lines still sit right before the CONTENT SEED
    assert p.index("SCOPE: days 5-8") < p.index("CONTENT SEED")
    assert "SEARCH QUESTIONS (" not in _prompt("blog")
    assert "SEARCH QUESTIONS (" not in _prompt("linkedin", search_questions=["How deep is Lake Khövsgöl?"])


def test_route_blog_format_has_the_same_faq_rule():
    assert "names the subject of its question" in prompts._BLOG_FORMAT_INSTRUCTIONS_ROUTE
    assert "names the subject of its question" in prompts._BLOG_FORMAT_INSTRUCTIONS


# ---------------------------------------------------------------- the contract step (one regeneration)

def _ctx(**extra):
    return {"atom_text": PAA_SEED, "facts_text": "", "brand_audience": {"brand_name": "Terra"}, "tenant_id": None,
            "cta": "Ask Terra to plan this trip.", **extra}


@pytest.mark.asyncio
async def test_fix_subjects_regenerates_a_full_faq_once_and_replaces_the_failing_pair():
    writer = _blog(CLEAN[:2] + [BROKEN[2][:2]])
    regen = [("How cold does Lake Khövsgöl get?",
              "Lake Khövsgöl freezes solidly in winter, typically from January through April.")]
    with patch.object(writing_service, "write_faq", return_value=(regen, 0.006)) as wf:
        out, cost, log = await writing_service._enforce_faq_contract(
            writer, context=_ctx(paa_questions=["How deep is Lake Khövsgöl?"]), hyg=h.HygieneContext(seed=PAA_SEED),
            request_id=uuid.uuid4(), brand_ignore=frozenset(), fix_subjects=True)
    assert wf.call_count == 1 and wf.call_args.kwargs["paa_questions"] == ["How deep is Lake Khövsgöl?"]
    assert [q for q, _ in qg.faq_pairs(out)] == [CLEAN[0][0], CLEAN[1][0], regen[0][0]]
    assert cost == 0.006 and "3 → 3 pairs" in log[0]


@pytest.mark.asyncio
async def test_fix_subjects_keeps_the_old_faq_when_the_regeneration_does_not_help():
    writer = _blog(CLEAN[:2] + [BROKEN[2][:2]])
    regen = [("What else is there?", "These are lovely too.")]
    with patch.object(writing_service, "write_faq", return_value=(regen, 0.006)):
        out, _, log = await writing_service._enforce_faq_contract(
            writer, context=_ctx(), hyg=h.HygieneContext(seed=PAA_SEED), request_id=uuid.uuid4(),
            brand_ignore=frozenset(), fix_subjects=True)
    assert out == writer and log[0].startswith("FAQ regeneration not kept")


@pytest.mark.asyncio
async def test_a_full_faq_without_fix_subjects_makes_no_call():
    writer = _blog(CLEAN[:3])
    with patch.object(writing_service, "write_faq") as wf:
        out, cost, log = await writing_service._enforce_faq_contract(
            writer, context=_ctx(), hyg=h.HygieneContext(seed=PAA_SEED), request_id=uuid.uuid4(),
            brand_ignore=frozenset())
    wf.assert_not_called()
    assert (out, cost, log) == (writer, 0.0, [])


# ---------------------------------------------------------------- the job wiring

def _blog_ctx():
    return {"atom_text": PAA_SEED, "goal": {"key": "promotion"}, "channel_style": {"channel": "blog"},
            "brand_audience": {}, "chosen": {}, "cta": "Ask Terra to plan this trip.", "destination": None,
            "trip_name": None, "brand_rubric_text": "", "channel": "blog", "atom_id": "a1",
            "people_also_ask": ["How deep is Lake Khövsgöl?", "Where is the Gobi desert?"]}


def _outcome_pass():
    return {"passed": True, "gate_ledger": [], "first_failure": None, "flags": []}


@pytest.mark.asyncio
async def test_job_sends_paa_to_the_writer_and_regenerates_for_a_kept_non_answer():
    """A fresh writer text whose 3-pair FAQ has one non-answer: the pair cannot be dropped (minimum),
    so the one FAQ regeneration runs with fix_subjects=True; the writer got the in-scope PAA."""
    text = _blog(CLEAN[:2] + [BROKEN[2][:2]])
    row = {"piece_id": str(uuid.uuid4()), "attempt_number": 1, "status": "approved"}
    calls = []

    async def enforce(t, **kw):
        calls.append((qg.faq_pair_count(t), kw.get("fix_subjects"), kw["context"].get("paa_questions")))
        return t, 0.0, []

    ctx = _blog_ctx()
    with patch.object(writing_service, "write_content", return_value=(text, 0.01, {}, None)) as wc, \
         patch.object(writing_service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda m, *a, **k: (m, 0.0))), \
         patch.object(writing_service, "_enforce_faq_contract", side_effect=enforce), \
         patch.object(writing_service, "run_quality_gates", return_value=_outcome_pass()) as gates, \
         patch.object(writing_service, "_finalize_piece", new=AsyncMock(return_value=row)):
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    assert wc.call_args.kwargs["search_questions"] == ["How deep is Lake Khövsgöl?"]
    assert calls == [(3, True, ["How deep is Lake Khövsgöl?"])]
    flags = gates.return_value["flags"]
    f7 = next(f for f in flags if f["gate"] == "F7_faq_autofix")
    assert any("kept FAQ pair at the 3-pair minimum" in v for v in f7["violations"])


@pytest.mark.asyncio
async def test_job_reports_only_what_the_regenerated_faq_still_has():
    text = _blog(CLEAN[:2] + [BROKEN[2][:2]])
    fixed = _blog(CLEAN[:3])
    row = {"piece_id": str(uuid.uuid4()), "attempt_number": 1, "status": "approved"}

    async def enforce(t, **kw):
        return fixed, 0.006, ["regenerated the FAQ section (3 → 3 pairs)"]

    with patch.object(writing_service, "write_content", return_value=(text, 0.01, {}, None)), \
         patch.object(writing_service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda m, *a, **k: (m, 0.0))), \
         patch.object(writing_service, "_enforce_faq_contract", side_effect=enforce), \
         patch.object(writing_service, "run_quality_gates", return_value=_outcome_pass()) as gates, \
         patch.object(writing_service, "_finalize_piece", new=AsyncMock(return_value=row)):
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), _blog_ctx(), pool=MagicMock())
    assert BROKEN[2][0] not in gates.call_args.kwargs["content_text"]
    f7 = next(f for f in gates.return_value["flags"] if f["gate"] == "F7_faq_autofix")
    assert f7["violations"] == ["regenerated the FAQ section (3 → 3 pairs)"]
