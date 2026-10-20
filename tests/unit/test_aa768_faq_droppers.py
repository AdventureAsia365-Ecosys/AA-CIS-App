"""AA-768 (S234 round 2 fix) — the FAQ droppers no longer contradict the F16_faq contract.

S234 r2: F16_faq held 7/8 blogs. drop_restated_faq dropped an answer when > 85% of its 5+-letter
words appeared ANYWHERE in the body; in a seed-bound blog every answerable question uses the body's
vocabulary, so it stripped 3-4 pairs per blog (r1 F7 flags '4/3/3 pairs removed') and most of the
one regenerated FAQ. Now only answer sentences that COPY the body are cut (hygiene.copies: ≥ 80% of
the sentence's words in order in one body sentence or two adjacent ones, or ≥ 80% of such a body
sentence inside it; number words = digits), and a pair goes when too little is left. Fixture sentences are the
real r2 body sentences and the real regenerated answers (8 Haiku calls, stage adhoc_aa768_faq).
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest

from services.acp_content_writing import generate, hygiene as h, quality_gates as qg
from services.acp_content_writing import service as writing_service

BODY = (
    "## Where the Steppe Meets the Taiga\n\n"
    "The crater is roughly 600 metres wide and 50 to 60 metres deep, and the caldera floor is accessible on "
    "foot — meaning families can actually walk down into it, not merely look from a rim.\n\n"
    "Lake Khövsgöl sits at one of the more consequential ecological boundaries in Asia — the transition "
    "between Central Asian steppe grasslands and Siberian taiga forest.\n\n"
    "Their settlements are not permanent villages but seasonal camps of teepee-style dwellings called urts, "
    "moved several times a year as the reindeer require.\n\n"
    "Reindeer in Tsaatan life are used for riding and as pack animals, and for milk — the herds are not "
    "primarily raised for meat.\n\n"
    # a long real post carries the answers' other words in OTHER sentences — the S233 rule counted them
    "Below the rim, the walkable floor drops away gently; about an hour is enough to cross it. Across this "
    "boundary, two entirely different climatic systems meet, creating unusually high biodiversity. The herds "
    "graze freely, which shapes the whole rhythm of the camps.\n\n"
    "Ask Terra Family Expeditions to plan this trip for your family.\n"
)

# Real regenerated answers that retell a body fact in other words (the S233 rule dropped each one:
# 0.88–0.93 of their words are in the body).
RESTATED = [
    ("How deep is the volcanic crater at Uran Togoo?",
     "The crater drops roughly 50 to 60 metres below the rim and is about 600 metres across, with a walkable "
     "caldera floor."),
    ("What makes Lake Khövsgöl ecologically significant?",
     "The lake sits at the boundary between Central Asian steppe grasslands and Siberian taiga forest, creating "
     "an area of unusually high biodiversity where two entirely different climatic systems meet."),
    ("What kind of dwellings do the Tsaatan live in?",
     "They live in seasonal camps of teepee-style dwellings called urts, which are moved several times a year "
     "as the reindeer graze across the taiga."),
]
# A real regenerated answer that copies the body's crater sentence with light edits, and a verbatim copy.
COPIED = [
    ("How big is the crater?",
     "The crater is roughly 600 metres wide and between 50 to 60 metres deep, with a caldera floor that's "
     "accessible on foot so you can walk down into it."),
    ("What are the reindeer used for?",
     "Reindeer in Tsaatan life are used for riding and as pack animals, and for milk — the herds are not "
     "primarily raised for meat."),
]


def _old_rule_drops(answer: str, body: str) -> bool:
    """The S233 test (5+-letter words of the answer found anywhere in the body > 85%) — kept here only
    to show these fixtures are the pairs it wrongly removed."""
    import re
    tok = re.compile(r"[a-z]{5,}")
    a, b = set(tok.findall(answer.lower())), set(tok.findall(body.lower()))
    return bool(a) and len(a & b) / len(a) > 0.85


def test_fixtures_are_what_the_old_rule_dropped():
    assert all(_old_rule_drops(a, BODY) for _, a in RESTATED + COPIED)


def test_restated_body_facts_survive_and_fill_the_contract():
    text = qg.replace_faq_section(BODY, RESTATED)
    out, dropped = qg.drop_restated_faq(text)
    assert dropped == 0 and out == text
    assert qg.faq_pair_count(out) == 3 and qg.gate_faq_contract(out)["passed"]
    assert qg.gate_faq_dedup(out)["passed"]


def test_copied_answers_are_still_dropped():
    text = qg.replace_faq_section(BODY, RESTATED + COPIED)
    out, dropped = qg.drop_restated_faq(text)
    assert dropped == 2
    assert "How big is the crater?" not in out and "What are the reindeer used for?" not in out
    assert all(q in out for q, _ in RESTATED)
    gate = qg.gate_faq_dedup(text)
    assert not gate["passed"] and len(gate["violations"]) == 2 and "copies a body sentence" in gate["violations"][0]


def _body():
    return qg._body_sentences(BODY.partition("## FAQ")[0])


def test_a_copied_sentence_is_cut_from_a_mixed_answer():
    """S234 r2 review: '<verbatim body sentence> <longer new sentence>' kept the verbatim sentence
    under the old half-of-the-answer rule. Now the copy is cut and the new sentence answers alone."""
    new_part = ("The Tsaatan are ethnically and linguistically related to the Tuvan peoples of southern Siberia, "
                "and only a few hundred of them still herd reindeer in the taiga.")
    mixed = COPIED[1][1] + " " + new_part
    assert qg.answer_copies_body(mixed, _body())
    assert qg.strip_copied_sentences(mixed, _body()) == (new_part, 1)
    text = qg.replace_faq_section(BODY, RESTATED + [("What are the reindeer used for?", mixed)])
    out, changed = qg.drop_restated_faq(text)
    assert changed == 1 and qg.faq_pair_count(out) == 4
    assert "primarily raised for meat" not in out.partition("## FAQ")[2] and new_part in out
    assert qg.gate_faq_dedup(out)["passed"]


def test_whats_left_must_still_answer_and_stand_alone():
    # only 'Yes.' left → the pair goes
    assert qg.strip_copied_sentences("Yes. " + COPIED[1][1], _body()) == (None, 1)
    # the kept sentence leans on the cut one ('It also…') → the pair goes
    assert qg.strip_copied_sentences(COPIED[1][1] + " It also gives the family a steady supply of yoghurt.",
                                     _body())[0] is None


@pytest.mark.parametrize("answer", [
    # lead-in + a verbatim body sentence (S234 r2 review: the answer's own share stayed low)
    "Yes, and it is worth knowing ahead of the family trip that reindeer in Tsaatan life are used for riding "
    "and as pack animals, and for milk — the herds are not primarily raised for meat.",
    # a verbatim body sentence + an appended clause (real e5842181 shape)
    "Their settlements are not permanent villages but seasonal camps of teepee-style dwellings called urts, "
    "moved several times a year as the reindeer require, and the ride there crosses rivers and boggy forest.",
    # two adjacent body sentences merged into one
    "Their settlements are not permanent villages but seasonal camps of teepee-style dwellings called urts, "
    "moved several times a year as the reindeer require, and reindeer in Tsaatan life are used for riding and as "
    "pack animals, and for milk.",
])
def test_containment_and_merged_copies_are_cut(answer):
    assert qg.answer_copies_body(answer, _body())
    out, changed = qg.drop_restated_faq(qg.replace_faq_section(BODY, [("Q?", answer)]))
    assert changed == 1 and "## FAQ" not in out


def test_restated_facts_and_spread_words_are_not_copies():
    sentences = _body()
    for _, a in RESTATED:
        assert not qg.answer_copies_body(a, sentences)
    # words spread over the whole body are not a copy of any one sentence
    spread = "The crater floor and the taiga forest both sit near the reindeer camps of the Tsaatan."
    assert not qg.answer_copies_body(spread, sentences)
    # a short answer is never a copy; a short body line never makes a long answer a copy
    assert not h.copies("Yes, at Sod Camp.", "The route starts at Sod Camp.")
    assert not h.copies("Ask us about the camp and the long ride north to the lake shore.", "Ask us.")


def test_number_words_and_digits_compare_equal():
    """S234 r2 review, real 75313b8c: the body spells the numbers, the answer uses digits."""
    body = ("The crater itself is roughly six hundred metres wide and fifty to sixty metres deep, and the caldera "
            "floor is accessible on foot.")
    answer = ("The crater is roughly 600 metres wide and between 50 to 60 metres deep, with a caldera floor that you "
              "can walk across on foot.")
    assert h.copies(answer, body) and h.copies(answer.replace("600", "six hundred"), body.replace("six hundred", "600"))
    assert h.copy_words("sixteen hundred and forty-five metres, 1,645 metres") == ["1645", "metre", "1645", "metre"]
    assert h.copy_words("one of the lakes") == ["lake"]  # a lone 'one' is a word
    assert h.copy_words("two thousand and twelve, five lakes") == ["2012", "5", "lake"]
    assert h.copy_words("lakes five six") == ["lake", "5", "6"]


SEED = (
    "Mörön — lunch stop\n"
    "Location: Mörön, Khövsgöl Province, Mongolia (do not place it in any other town, region or country)\n"
    "Day 4 of the trip\n"
    "Source itinerary: Stop for lunch near Moron, capital of Khövsgöl province, before continuing to the lake.\n"
    "Place background (checked facts about the place itself — you may use these):\n"
    "- Mörön is the capital city of Khövsgöl Province, the northernmost aimag (province) of Mongolia.\n"
    "- Mörön has a small domestic airport with scheduled flights connecting it to Ulaanbaatar, making it the "
    "primary air gateway to the region.\n"
    "- The lake freezes solidly in winter, typically from January through April, and was historically used "
    "as an ice road.\n"
)
MORON_BODY = ("The route on day four passes through Mörön, the capital city of Khövsgöl Province, the northernmost "
              "province of Mongolia, for a lunch stop before the lake.\n\nAsk us.\n")


def test_unused_source_facts_lists_what_the_body_does_not_tell():
    unused = qg.unused_source_facts(SEED, MORON_BODY)
    assert any(f.startswith("Mörön has a small domestic airport") for f in unused)
    assert any(f.startswith("The lake freezes solidly") for f in unused)
    assert not any("capital city" in f for f in unused)  # the body tells it
    assert not any(f.startswith(("Location", "Day 4", "Place background")) for f in unused)
    assert qg.unused_source_facts(SEED, MORON_BODY, limit=1) == unused[:1]
    assert qg.unused_source_facts("", MORON_BODY) == []


def _llm(content):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=content, model_used="haiku", cost_usd=0.006, input_tokens=10,
                                           output_tokens=10, stop_reason="end_turn", satellite_account="acc3",
                                           fallback_used=False)
    return fake


def test_write_faq_prompt_carries_unused_facts_and_bans_copies():
    reply = '{"faq": [{"q": "Q: Is there an airport?", "a": "A: Yes, with flights to Ulaanbaatar."}]}'
    with patch.object(generate, "LLMClient", return_value=_llm(reply)) as m, \
         patch.object(generate, "record_call_sync") as log:
        pairs, cost = generate.write_faq(source=SEED, body=MORON_BODY, unused_facts=["Mörön has a small airport."])
    req = m.return_value.generate.call_args.args[0]
    assert "UNUSED FACTS (from the SOURCE; the POST does not tell these yet):\n- Mörön has a small airport." \
        in req.user_prompt
    assert req.user_prompt.index("UNUSED FACTS") < req.user_prompt.index("POST (do not repeat")
    assert "Never copy or closely paraphrase a sentence of the POST" in req.system_prompt
    assert "keep its wording close" not in req.system_prompt
    assert log.call_args.kwargs["quality_signal"]["unused_facts"] == 1
    assert pairs == [("Is there an airport?", "Yes, with flights to Ulaanbaatar.")] and cost == 0.006


@pytest.mark.asyncio
async def test_contract_step_passes_the_unused_facts():
    ctx = {"atom_text": SEED, "facts_text": "", "brand_audience": {"brand_name": "Terra"}, "tenant_id": None,
           "cta": "Ask us."}
    pairs = [("Is there an airport near Mörön?", "Yes — a small domestic airport with flights to Ulaanbaatar."),
             ("What happens to the lake in winter?", "It freezes solidly from January through April."),
             ("Why stop in Mörön?", "It is the provincial capital, so the route breaks there for lunch.")]
    with patch.object(writing_service, "write_faq", return_value=(pairs, 0.006)) as wf:
        out, _, log = await writing_service._enforce_faq_contract(
            MORON_BODY, context=ctx, hyg=h.HygieneContext(seed=SEED), request_id=uuid.uuid4(),
            brand_ignore=frozenset())
    unused = wf.call_args.kwargs["unused_facts"]
    assert any("domestic airport" in f for f in unused) and not any("capital city" in f for f in unused)
    assert qg.faq_pair_count(out) == 3 and "0 → 3 pairs" in log[0]


@pytest.mark.asyncio
async def test_contract_merges_the_writers_surviving_pairs_with_the_regeneration():
    """S234 r2 review: the regeneration replaced the whole FAQ, so 2 surviving writer pairs + 2
    surviving regenerated pairs ended on 2 (held F16). Now they merge; a repeated question goes."""
    writer = qg.replace_faq_section(BODY, RESTATED[:2])
    regen = [COPIED[0], COPIED[1],  # both copies → dropped
             (RESTATED[0][0], "It is a crater you can walk into."),  # same question as the writer's → goes
             RESTATED[2],
             ("Is there an airport near Mörön?", "Yes, a small domestic airport with flights to Ulaanbaatar.")]
    ctx = {"atom_text": SEED, "facts_text": "", "brand_audience": {"brand_name": "Terra"}, "tenant_id": None,
           "cta": "Ask Terra Family Expeditions to plan this trip for your family."}
    with patch.object(writing_service, "write_faq", return_value=(regen, 0.006)):
        out, _, log = await writing_service._enforce_faq_contract(
            writer, context=ctx, hyg=h.HygieneContext(seed=SEED), request_id=uuid.uuid4(), brand_ignore=frozenset())
    assert [q for q, _ in qg.faq_pairs(out)] == [RESTATED[0][0], RESTATED[1][0], RESTATED[2][0],
                                                 "Is there an airport near Mörön?"]
    assert "2 → 4 pairs" in log[0] and qg.gate_faq_contract(out)["passed"] and qg.gate_faq_dedup(out)["passed"]


@pytest.mark.asyncio
async def test_contract_caps_the_merged_faq():
    writer = qg.replace_faq_section(BODY, RESTATED[:2])
    # AA-779: every answer names its question's subject, or drop_subjectless_faq takes it out first
    regen = [("What is the market like?", "The market's stalls sell dried curd, felt slippers and fresh bread."),
             ("Where do boats leave from?", "Small wooden launches depart from the Khatgal jetty each morning."),
             ("Is there a monastery nearby?", "The nearest monastery is the Dayan Derkh cave shrine."),
             ("What fish live in the lake?", "Lenok, grayling and the endemic Hovsgol grayling swim in the lake."),
             ("When does the ice melt?", "The ice usually melts in late May or early June."),
             ("Can you swim?", "Brave visitors swim in August, though the water stays bracingly cold.")]
    with patch.object(writing_service, "write_faq", return_value=(regen, 0.006)):
        out, _, _ = await writing_service._enforce_faq_contract(
            writer, context={"atom_text": SEED}, hyg=h.HygieneContext(seed=SEED), request_id=uuid.uuid4(),
            brand_ignore=frozenset())
    assert qg.faq_pair_count(out) == qg.FAQ_MAX_PAIRS and qg.faq_pairs(out)[0][0] == RESTATED[0][0]


def test_faq_pairs_reads_the_section_without_the_trailing_cta():
    text = BODY.rstrip() + "\n\n## FAQ\n\n**Q: One?**\nA: First answer.\n\n**Q: Two?**\nA: Second answer.\n\nAsk us now.\n"
    assert qg.faq_pairs(text) == [("One?", "First answer."), ("Two?", "Second answer.")]
    assert qg.faq_pairs(BODY) == []


# ---------------------------------------------------------------- a sentence repair that empties a FAQ answer

REPAIR_BLOG = ("## Lake\n\nThe lake is deep and cold all year. Ask Terra to plan this trip.\n\n## FAQ\n\n"
               "**Q: Is there an airport?**\nA: Yes, Mörön has flights to Ulaanbaatar.\n\n"
               "**Q: When does the lake freeze?**\nA: It freezes solidly from January through April at 900 metres.\n\n"
               "**Q: What fish live there?**\nA: Lenok and grayling swim in the lake.\n")
F1_FAQ = {"gate": "F1_grounding", "passed": False, "blocking": True, "repairable": True, "violations": [
    "sentence states ['900'] not present in the content seed: "
    "'It freezes solidly from January through April at 900 metres'"]}


def test_an_emptied_answer_is_not_a_pair():
    emptied = REPAIR_BLOG.replace("It freezes solidly from January through April at 900 metres.", "")
    out, changed = qg.drop_restated_faq(emptied)
    assert changed == 1 and qg.faq_pair_count(out) == 2 and "When does the lake freeze?" not in out


def _blog_ctx():
    return {"atom_text": "Lake Khövsgöl freezes. Mörön has flights.", "goal": {"key": "promotion"},
            "channel_style": {"channel": "blog"}, "brand_audience": {}, "chosen": {}, "cta": "Ask Terra to plan this trip.",
            "destination": None, "trip_name": None, "brand_rubric_text": "", "channel": "blog", "atom_id": "a1"}


def _outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


@pytest.mark.asyncio
async def test_a_repair_that_cuts_the_faq_below_the_contract_regenerates_once():
    """S234 r2 review: attempt 1 ends on exactly 3 pairs; the attempt-2 sentence repair deletes a FAQ
    answer (2 pairs left). F16_faq is not repairable, so without a regeneration the blog was held."""
    from unittest.mock import AsyncMock
    row = {"piece_id": str(uuid.uuid4()), "attempt_number": 3, "status": "held"}
    calls = []

    async def enforce(text, **_):
        calls.append(qg.faq_pair_count(text))
        return text, 0.0, []

    with patch.object(writing_service, "write_content", return_value=(REPAIR_BLOG, 0.01, {}, None)), \
         patch.object(writing_service, "repair_sentences", return_value=([""], 0.002)), \
         patch.object(writing_service, "rewrite_with_feedback") as rw, \
         patch.object(writing_service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda m, *a, **k: (m, 0.0))), \
         patch.object(writing_service, "_enforce_faq_contract", side_effect=enforce), \
         patch.object(writing_service, "run_quality_gates",
                      side_effect=[_outcome([F1_FAQ]), _outcome([F1_FAQ]), _outcome([F1_FAQ])]), \
         patch.object(writing_service, "_finalize_piece", new=AsyncMock(return_value=row)):
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), _blog_ctx(), pool=MagicMock())
    rw.assert_not_called()
    # attempt 1 (fresh text, 3 pairs → no call inside), attempt 2 (repair cut it to 2 → one more);
    # attempt 3 never regenerates again
    assert calls == [3, 2]
