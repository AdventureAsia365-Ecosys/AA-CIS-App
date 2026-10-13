"""AA-768 (S234 round 2 fix) — the FAQ droppers no longer contradict the F16_faq contract.

S234 r2: F16_faq held 7/8 blogs. drop_restated_faq dropped an answer when > 85% of its 5+-letter
words appeared ANYWHERE in the body; in a seed-bound blog every answerable question uses the body's
vocabulary, so it stripped 3-4 pairs per blog (r1 F7 flags '4/3/3 pairs removed') and most of the
one regenerated FAQ. Now an answer is dropped only when it COPIES body sentences (hygiene.copies:
≥ 80% of a sentence's content words, in order, in ONE body sentence). Fixture sentences are the
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


def test_copy_is_judged_per_sentence_against_one_body_sentence():
    sentences = [s["clean"] for s in h.sentence_spans(BODY) if s["kind"] == "prose"]
    # one copied sentence + a longer new one: most of the answer is new → kept
    mixed = (COPIED[1][1] + " The Tsaatan are ethnically and linguistically related to the Tuvan peoples of "
             "southern Siberia, and only a few hundred of them still herd reindeer in the taiga.")
    assert not qg.answer_copies_body(mixed, sentences)
    assert qg.answer_copies_body(COPIED[0][1], sentences)
    # words spread over the whole body are not a copy of any one sentence
    spread = "The crater floor and the taiga forest both sit near the reindeer camps of the Tsaatan."
    assert not qg.answer_copies_body(spread, sentences)
    # a short answer is never a copy
    assert not h.copies("Yes, at Sod Camp.", "The route starts at Sod Camp.")


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
