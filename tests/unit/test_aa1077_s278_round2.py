"""AA-1077 round 2 (S278 paid trial on an audit tenant, 3 blog pieces, t9_write v6): 1 of 3 approved.

Failure shape 1 (73e6a233): the first draft was short (3,957 chars); the repair round carried the F4 'lengthen' line
TOGETHER WITH 6 F17_trip_fact 'say it is optional or cut it', an F13_leak, an F13_repeat and an F9 cut; the cuts won
and the text shrank to 3,546 chars, held F4_extreme_length. Failure shape 2 (6610ac8d): the lengthen line said
'from the CONTENT SEED, its Place background and the FACTS lines' and the repaired text wrote 'The source notes name
deer, birds, leopards…', held F13_leak. And the writer's first pass landed at 690-840 words under a 950-word floor.

This file pins the round-2 fix without a paid call:
- t9_write v7: a section plan whose word budgets add up to the floor, from the prompt's own hashed symbols;
- no instruction text the model may echo names a source without the 'never mention where a fact comes from' rule,
  and F13_leak catches the meta-references the trial produced;
- a blog rewrite under the floor gets the cut-type fixes first (each told to fill the place it cuts) and the length
  line last, measured after them;
- with the extend switch on, a repair round that leaves a blog under the floor runs the expand step on the text the
  repair left (its deficit includes the cuts), sections the repair shortened first; the net length is recorded.

Invented places only (Kestrel Vale, Lake Ombra, Harrow Ford). The model is always mocked.
"""
import re
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import generate, length_contract as lc, length_extend as le, prompts
from services.acp_content_writing import hygiene
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service
from services.quality import registry as qr
from shared.llm_client.prompt_registry import load_manifest, newest, prompt_hash_of

# The labels the prompts use for the writer's inputs, and the meta-phrases the S278 trial copied into a post.
SOURCE_LABELS = ("CONTENT SEED", "content seed", "source notes", "FACTS lines", "Place background",
                 "the itinerary calls", "the itinerary says")
NEVER_MENTION = "never mention where a fact comes from"


# ---------------------------------------------------------------- (a) the writer's section plan (t9_write v7)

def test_the_plan_budgets_add_up_to_the_word_floor_and_stay_under_the_cap():
    plan = prompts._blog_length_plan()
    lo, hi = prompts._BLOG_WORDS
    assert plan["total"] == plan["opening"] + plan["each"] * (plan["sections"] - 1) + plan["richest"] + plan["faq"]
    assert lo <= plan["total"] <= hi < prompts._BLOG_WORDS_MAX       # the 1,600 ceiling is kept
    assert plan["sections"] < prompts._BLOG_SECTIONS_MAX
    assert plan["richest"] >= plan["each"] * prompts._BLOG_RICHEST_SHARE
    assert lo == lc.target_words()                                     # the floor is the length target (S278)


def test_the_plan_restates_only_numbers_the_format_bullets_already_give():
    """No new length literal: the FAQ low ends and the richest share are the format text's own numbers."""
    pairs, words = prompts._BLOG_FAQ_PLAN
    for fmt in (prompts._BLOG_FORMAT_INSTRUCTIONS, prompts._BLOG_FORMAT_INSTRUCTIONS_ROUTE):
        assert f"followed by {pairs}-4 Q/A pairs" in fmt
        assert f"{words}-60 words\n  per answer" in fmt
        assert "at least half as\n  long again as any other section" in fmt
    assert prompts._BLOG_RICHEST_SHARE == 1.5


def test_the_block_carries_the_plan_and_the_no_source_rule():
    block = prompts._BLOG_LENGTH_INSTRUCTIONS
    plan = prompts._blog_length_plan()
    assert f"the budgets add up to {plan['total']:,} words" in block
    assert f"the richest about {plan['richest']:,} words" in block and f"about {plan['each']:,};" in block
    assert NEVER_MENTION in block.lower()
    assert "—" not in block                       # AA-778
    assert "CONTENT SEED" not in block and "FACTS lines" not in block


def test_every_rewrite_prompt_says_never_to_name_a_source():
    from services.acp_angle_gate.channel_style import get_channel_style
    from services.acp_angle_gate.goals import get_goal
    for channel in ("blog", "linkedin"):
        p = prompts.build_user_prompt(
            content_seed="Kestrel Vale — walk\nLocation: Kestrel Vale\nSource itinerary: Walk the vale.",
            goal=get_goal("promotion"), channel_style=get_channel_style(channel),
            brand_audience={"brand_name": "Pine Lantern Trips", "voice": ["Clear"]},
            angle={"name": "A", "why_it_works": "w", "formula_fit": "f", "best_final_style": "Plain."},
            cta="Message us.", atom_id="a1", revision_feedback=["F17_trip_fact: x: 'The vale is quiet.'"])
        tail = p[p.index("PREVIOUS ATTEMPT FAILED QUALITY REVIEW"):]
        assert "Never mention where a fact comes from" in tail


def test_the_manifest_v7_hashes_the_plan_symbols():
    entry = newest(load_manifest(), "t9_write")
    assert entry["version"] == "v7"
    assert {"_BLOG_FAQ_PLAN", "_BLOG_RICHEST_SHARE", "_blog_length_plan", "_PLAN",
            "_BLOG_LENGTH_INSTRUCTIONS", "build_user_prompt"} <= set(entry["symbols"])
    assert prompt_hash_of(entry["source_path"], entry["symbols"]) == entry["prompt_sha256"]


# ---------------------------------------------------------------- (c) no source label without the rule

def _instruction_texts() -> dict:
    ledger = [{**qg.gate_extreme_length("## A\n\nShort.", "blog"), "blocking": True}]
    return {
        "short_rewrite_target": lc.short_rewrite_target(),
        "replace_not_delete": lc.REPLACE_NOT_DELETE,
        "keep_not_cut": lc.KEEP_NOT_CUT,
        "blog_length_block": prompts._BLOG_LENGTH_INSTRUCTIONS,
        "extend_system": generate._EXTEND_SYSTEM,
        "f4_feedback": "\n".join(service._repair_feedback(ledger, "blog")),
    }


@pytest.mark.parametrize("name", sorted(_instruction_texts()))
def test_no_instruction_names_a_source_to_write_from(name):
    """A label is never offered as a place to write from (the model echoes it); the texts that ask for more text
    also carry the never-mention rule. A label may appear only inside that rule's own quoted examples."""
    text = _instruction_texts()[name]
    rule_free = re.sub(r'never mention where a fact comes from[^.;]*', "", text, flags=re.I)
    assert [lab for lab in SOURCE_LABELS if lab in rule_free] == [], name
    if name not in ("replace_not_delete", "keep_not_cut"):
        assert NEVER_MENTION in text.lower(), name


def test_the_old_lengthen_wording_is_gone():
    """S278 6610ac8d copied it: 'from the CONTENT SEED, its Place background and the FACTS lines'."""
    for text in _instruction_texts().values():
        assert "CONTENT SEED, its Place background and the FACTS lines" not in text


@pytest.mark.parametrize("sentence", [
    "The source notes name deer, birds and leopards in the forest around Harrow Ford.",
    "The itinerary says the boat crosses Lake Ombra before noon.",
    "The itinerary names the ridge above Kestrel Vale as the high point.",
    "Our sources say the chapel at Harrow Ford is very old.",
    "The notes mention a market on the shore of Lake Ombra.",
    "From the source facts, the ridge path is steep.",
    "The sources state that the bridge is the oldest in the valley.",
    "Our itinerary notes that the ridge path closes in deep snow.",
    "The FACTS lines put the lake at the head of the valley.",
])
def test_f13_leak_catches_a_meta_reference_to_the_sources(sentence):
    assert hygiene.LEAK_RE.search(sentence)


@pytest.mark.parametrize("sentence", [
    "The itinerary includes a day at Lake Ombra.",
    "The source of the river lies above Kestrel Vale.",
    "The notes of the chapel bell carry over Harrow Ford.",
    "Its source lies high in the hills.",
    # S278 review: reader-facing travel copy with a non-provenance verb is never a leak (each one would be a blocking
    # F13_leak, a borrowed-sentence delete or a refused expand paragraph: a hole in a blog that is already short)
    "The itinerary gives you a free afternoon in Harrow Ford.",
    "The itinerary adds a boat ride on Lake Ombra.",
    "Our itinerary lists three nights in Kestrel Vale.",
    "This itinerary describes a loop around the ridge.",
    "This itinerary adds a night at Harrow Ford.",
    "The itinerary shows the best of the valley road.",
    "The itinerary calls for an early start from Harrow Ford.",
    "Our notes give you a packing list for the ridge.",
    "The facts show the bridge at Harrow Ford dates to 1663.",
    "Explore the source material of local legends around Lake Ombra.",
    "The quick facts section below lists the opening hours of the chapel.",
])
def test_f13_leak_leaves_reader_facing_sentences_alone(sentence):
    assert not hygiene.LEAK_RE.search(sentence)


# ---------------------------------------------------------------- (b) the rewrite feedback order

def _f17(sentence: str) -> str:
    return (f"presents 'a boat ride' as part of the plan, but the source lists it as optional — say it is optional "
            f"or cut it: '{sentence}'")


def test_a_short_blog_rewrite_gets_the_cuts_first_each_told_to_fill_its_place_and_the_length_last():
    """S278 73e6a233: 6 F17 cut fixes + an F13_repeat + an F9 cut sent with the lengthen line."""
    f4 = {**qg.gate_extreme_length("## A\n\n" + "Kestrel Vale is green. " * 160, "blog"), "blocking": True}
    f17 = {"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
           "violations": [_f17(f"Sentence number {i} about Lake Ombra.") for i in range(6)]}
    rep = {"gate": qr.F13_REPEAT, "passed": False, "blocking": True, "repairable": True,
           "violations": ["repeats an earlier sentence — cut it or say something new: 'Lake Ombra is still.'"]}
    lines = service._repair_feedback([f4, f17, rep], "blog")
    assert len(lines) == 8 and lines[-1].startswith("F4_extreme_length: length_short")
    assert "after every fix above, the piece must still be at least 5,499 characters" in lines[-1]
    assert all(line.endswith(lc.REPLACE_NOT_DELETE) for line in lines[:-1])
    # another channel, or a blog in band, keeps the ledger order and no hint
    in_band = service._repair_feedback([f17, rep], "blog")
    assert in_band[0] == f"{qr.F17_TRIP_FACT}: {_f17('Sentence number 0 about Lake Ombra.')}"
    assert not any(lc.REPLACE_NOT_DELETE in line for line in in_band)


@pytest.mark.parametrize("violation, cut", [
    ("say it is optional or cut it: 'x'", True),
    ("cut it or say something new: 'x'", True),
    ("rewrite or cut it: 'x'", True),
    ("names a place outside the scope: 'x'", False),
])
def test_cut_type_violations(violation, cut):
    assert lc.is_cut_violation(violation) is cut


# ---------------------------------------------------------------- (b) the expand step after the repair

CTA = "Write to us to plan your own walk."
SOURCE = (
    "Kestrel Vale is a glacial valley whose meadows are grazed by hill sheep through the warm months.\n"
    "The stone bridge at Harrow Ford was built by the valley farmers to carry their carts over the river.\n"
    "Lake Ombra takes its dark colour from the peat beds that line its northern shore.\n"
    "The chapel at Harrow Ford keeps a carved wooden screen made by a village joiner.\n"
    "Fishermen on Lake Ombra still use flat-bottomed boats built from local larch.\n"
    "The ridge path above Kestrel Vale passes a ring of standing stones raised by early farmers.\n"
)
PAD = {
    "Kestrel Vale": ["The valley floor is wide and open, with a stream running down its middle.",
                     "Stone walls divide the lower fields into narrow strips.",
                     "Farm tracks lead up from the road towards the high pastures.",
                     "Small barns stand at the corners of several fields.",
                     "Alder trees crowd the banks wherever the stream bends.",
                     "Hay meadows on the western side are cut once each summer.",
                     "A drove road once brought cattle down from the northern hills.",
                     "The farmhouses sit low, with thick walls against the wind."],
    "Harrow Ford": ["Harrow Ford is a cluster of houses where the valley road meets the river.",
                    "A row of cottages faces the water along the main street.",
                    "The village green sits beside the old ford itself.",
                    "Gardens behind the houses run down to the riverbank.",
                    "A smithy at the end of the street has become a workshop for carvers.",
                    "The inn by the green keeps its old painted sign of a heron.",
                    "Lanes between the cottages are paved with rounded river cobbles.",
                    "Apple orchards climb the slope behind the chapel."],
    "Lake Ombra": ["Lake Ombra fills the head of the valley beyond the village.",
                   "Reed beds line the southern shore where the river enters.",
                   "A gravel path follows the western edge of the water.",
                   "Wooden jetties reach out from the shore near the boathouses.",
                   "Coots and grebes nest among the reeds in the shallow bays.",
                   "Birch woods come down to the water on the eastern bank.",
                   "An island of pines stands near the middle of the lake.",
                   "Boathouses of tarred timber line the cove below the road."],
    "The Ridge": ["The ridge rises steeply on the eastern side of the valley.",
                  "Heather and bracken cover its upper slopes.",
                  "A cairn marks the highest point of the path.",
                  "From the crest the land falls away towards the next valley.",
                  "Peat hags break the moor between the stones and the cairn.",
                  "Ravens nest on the crags below the northern end.",
                  "Old boundary posts follow the line of the crest.",
                  "A spring rises in a hollow just below the summit."],
    "The Valley Road": ["The valley road leaves the plain at a stone toll house now used as a barn.",
                        "Hedges of hawthorn and hazel line the lower miles of the road.",
                        "A packhorse bridge crosses a side stream halfway up the valley.",
                        "The road narrows where it squeezes between the river and a cliff.",
                        "Milestones carved with pointing hands still stand on the verges.",
                        "Beyond the narrows the valley opens into a bowl of fields.",
                        "A ruined mill sits where a weir once fed its wheel.",
                        "Lime kilns built into the bank once served the upland farms.",
                        "The last stretch of road runs straight along the river flats.",
                        "Rowan trees mark the gateways of the older farms along the way."],
}
CUTS = ["A boat ride across Lake Ombra is part of the plan here.",
        "A guided boat ride takes the group to the far shore of the lake.",
        "The boat ride continues past the boathouses to the reed beds.",
        "Another boat ride waits at the jetty below the chapel.",
        "A boat ride crosses from the ford to the island of pines.",
        "One more boat ride carries the group back to the village green."]


def _section(name: str, extra: list[str]) -> str:
    sents = PAD[name][:4] + extra + PAD[name][4:]
    return f"## {name}\n\n" + " ".join(sents[:5]) + "\n\n" + " ".join(sents[5:])


def _text() -> str:
    intro = ("Kestrel Vale opens slowly as the road climbs from the plain. Few valleys in the region keep their "
             "old field pattern so clearly. Farms, a village, a lake and a moorland ridge sit within a short walk of "
             "each other. This guide follows them in the order the road reaches them.")
    body = "\n\n".join([_section("The Valley Road", []), _section("Kestrel Vale", CUTS[:2]),
                        _section("Harrow Ford", CUTS[2:4]), _section("Lake Ombra", CUTS[4:]),
                        _section("The Ridge", [])])
    return (f"{intro}\n\n{body}\n\n{CTA}\n\n## FAQ\n\n**Q: Where is Harrow Ford?**\n"
            "A: Harrow Ford lies where the valley road meets the river below Kestrel Vale, a cluster of cottages "
            "around a green beside the old crossing.\n\n**Q: What is Lake Ombra like?**\n"
            "A: Lake Ombra is a dark upland lake at the head of the valley, edged by reed beds, birch woods and a "
            "cove of tarred boathouses.\n")


def test_the_fixture_reproduces_the_trial_shape():
    text = _text()
    assert lc.measure(text) < lc.BLOG_FLOOR_CHARS
    assert all(c in text for c in CUTS)


def test_plan_after_repair_sizes_from_the_cut_text_and_fills_the_holes_first(monkeypatch):
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    before = _text()
    after = before
    for c in CUTS:
        after = after.replace(" " + c, "")
    plan, why = le.plan_after_repair(after, before=before, channel="blog", source=SOURCE)
    assert why == "expand" and plan is not None
    assert plan["deficit_chars"] == lc.extend_target_chars() - lc.measure(after)
    assert plan["hole_chars"] == lc.measure(before) - lc.measure(after)        # every cut char is a hole
    heads = {s["heading"]: s for s in plan["sections"]}
    # the three sections the cuts shortened come first, never the untouched one
    assert set(heads) == {"Kestrel Vale", "Harrow Ford", "Lake Ombra"}
    assert all(s["hole_chars"] > 0 for s in plan["sections"])


@pytest.mark.parametrize("case, why", [("off", "off"), ("linkedin", "not_blog"), ("long", "at_or_over_floor"),
                                       ("no_facts", "no_untold_facts"), ("no_sections", "no_sections")])
def test_plan_after_repair_says_why_not(case, why, monkeypatch):
    monkeypatch.setenv(le.EXTEND_ENV, "off" if case == "off" else "on")
    text, channel, source = _text(), "blog", SOURCE
    if case == "linkedin":
        channel = "linkedin"
    elif case == "long":
        text = text + ("\n\n" + "Kestrel Vale is green and wide. " * 60)
    elif case == "no_facts":
        source = ""
    elif case == "no_sections":
        text = "Kestrel Vale is green. " * 20
    plan, reason = le.plan_after_repair(text, before=None, channel=channel, source=source)
    assert plan is None and reason == why


def test_the_extend_refuses_a_paragraph_that_names_its_source(monkeypatch):
    """Failure shape 2 at the expand step: a paragraph that says where its facts come from never stands."""
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    plan, _ = le.plan_after_repair(_text(), before=None, channel="blog", source=SOURCE)

    def call(*, sections, facts, voice=None, **kw):
        return ["The source notes name the carved wooden screen in the chapel at Harrow Ford."] * len(sections), \
            0.01, "writer-model"
    text, _, entry = le.extend(_text(), plan, call=call, source=SOURCE, cta=CTA)
    assert entry["added"] == 0 and text == _text()
    assert all("names where facts come from" in r["why"] for r in entry["refused"])


def test_a_hole_section_is_marked_in_the_extend_request():
    client = MagicMock()
    client.generate.return_value = MagicMock(content='{"additions": ["One.", "Two."]}', model_used="m",
                                             cost_usd=0.01, input_tokens=1, output_tokens=1, stop_reason="end_turn")
    with patch.object(generate, "generate_with_budget_retry") as gen, patch.object(generate, "record_call_sync"):
        gen.return_value = MagicMock(resp=client.generate.return_value, still_cut=False, cost_usd=0.01,
                                     signal=lambda: {})
        generate.extend_sections(sections=[{"heading": "A", "text": "x", "words": 60, "hole_chars": 120},
                                           {"heading": "B", "text": "y", "words": 60}], facts=["A fact."])
    user = gen.call_args.args[1].user_prompt
    assert "SECTION 1 (add about 60 words; a repair cut text here)" in user
    assert "SECTION 2 (add about 60 words)\n" in user


# ---------------------------------------------------------------- the loop: 6 cuts + short → cuts, then expand

REQUEST_ID = uuid.UUID("00000000-0000-4000-8000-000000001077")
PIECE_ID = uuid.UUID("00000000-0000-4000-8000-000000002077")

ADDS = {
    "Kestrel Vale": ("The meadows of this glacial valley are grazed by hill sheep through the warm months, and the "
                     "flocks drift between the walled strips as the grass grows. Shepherds move them up the farm "
                     "tracks when the lower fields are bare, so the slopes change colour field by field. The open "
                     "floor of the valley makes the pattern easy to read from the road, with each strip of grass "
                     "kept in turn and the stream marking the line between the grazed and the fallow ground. Above the "
                     "fields, the ridge path passes a ring of standing stones raised by early farmers, a reminder "
                     "that people have grazed and cleared this valley for a very long time."),
    "Harrow Ford": ("The stone bridge here was built by the valley farmers to carry their carts over the river, and "
                    "it still links the cottages to the fields on the far bank. Inside the chapel, a carved wooden "
                    "screen made by a village joiner divides the nave from the altar, its panels cut with leaves and "
                    "vines. Both are the work of the people who farmed this valley, built for daily use rather than "
                    "for show, and both have outlasted the families who made them. The bridge is narrow, wide enough "
                    "for a single cart, and its parapet stones are worn smooth where hands have rested on them. "
                    "The screen in the chapel is the finer of the two, its carving deep and clean."),
    "Lake Ombra": ("The dark colour of the water comes from the peat beds that line its northern shore, which "
                   "stain the lake the shade of strong tea. Fishermen still work it from flat-bottomed boats built "
                   "from local larch, a shape suited to the shallow margins and the reed beds. The boats lie on "
                   "the jetties when they are not out, their timber weathered grey by the wind that comes down the "
                   "valley. Larch is the wood of choice because it grows in the woods above the lake and stands up "
                   "to the wet, so the boats are made close to where they are used."),
}


def _call(*, sections, facts, voice=None, **kw):
    _call.seen = {"sections": sections, "facts": facts}
    return [ADDS.get(s["heading"], "") for s in sections], 0.02, "writer-model"


def _ctx():
    return {"atom_text": SOURCE, "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "AIDA",
                                          "marketing_term": "AIDA"},
            "channel_style": {"key": "blog", "tone": "warm"}, "brand_audience": {"voice": ["warm"]},
            "chosen": {"idx": 0, "name": "A", "why_it_works": "w", "formula_fit": "AIDA",
                       "best_final_style": "warm", "recommended": True, "chosen": True},
            "cta": CTA, "destination": None, "trip_name": None, "brand_rubric_text": "rubric", "channel": "blog",
            "atom_id": "atom_x"}


def _outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


@pytest.fixture
def _loop_stubs():
    with patch.object(service.hygiene, "ensure_cta", side_effect=lambda text, cta: (text, [])), \
            patch.object(service.text_integrity, "final_pass", side_effect=lambda text, hyg, **kw: (text, [], [])), \
            patch.object(service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda seo, kw, **k: (seo, 0.0))), \
            patch.object(service, "_enforce_faq_contract",
                         new=AsyncMock(side_effect=lambda text, **k: (text, 0.0, []))), \
            patch.object(service, "_apply_faq_answer_fit",
                         new=AsyncMock(side_effect=lambda text, flags, **k: (text, flags, 0.0))), \
            patch.object(service, "_recheck_length", side_effect=lambda t, led, st, hr, **kw: (led, st, hr, {})):
        yield


def _attempt_one_ledger(text):
    f4 = {**qg.gate_extreme_length(qg.strip_citation_tags(text), "blog"), "blocking": True}
    f17 = {"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
           "violations": [_f17(c) for c in CUTS]}
    assert not f4["passed"]
    return [f4, f17]


def _gates_on_the_text(calls):
    """run_quality_gates stand-in: attempt 1 → the trial shape; later → F4 measured on the real text."""
    def run(**kw):
        calls.append(kw["content_text"])
        if len(calls) == 1:
            return _outcome(_attempt_one_ledger(kw["content_text"]))
        f4 = {**qg.gate_extreme_length(qg.strip_citation_tags(kw["content_text"]), "blog"), "blocking": True}
        return _outcome([f4])
    return run


@pytest.mark.asyncio
async def test_six_cuts_on_a_short_blog_are_followed_by_an_expand_that_clears_the_floor(_loop_stubs, monkeypatch):
    """Failure shape 1, switch on: the sentence repair applies the 6 cuts (each problem told to keep its sentence if
    it can), the expand then runs on the CUT text with the shortened sections first, and the net length the loop
    records ends over the floor; the F4 gate re-reads the final text and passes."""
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    calls: list[str] = []
    with patch.object(service, "write_content", return_value=(_text(), 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback") as rewrite, \
            patch.object(service, "repair_sentences", return_value=([""] * len(CUTS), 0.01)) as repair, \
            patch.object(service, "extend_sections", side_effect=_call) as ext, \
            patch.object(service, "run_quality_gates", side_effect=_gates_on_the_text(calls)), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(REQUEST_ID, PIECE_ID, _ctx(), pool=MagicMock())
    rewrite.assert_not_called()
    problems = [it["problem"] for it in repair.call_args.kwargs["sentences"]]
    assert len(problems) == len(CUTS) and all(p.endswith(lc.KEEP_NOT_CUT) for p in problems)
    ext.assert_called_once()
    asked = {s["heading"] for s in _call.seen["sections"]}
    assert asked == {"Kestrel Vale", "Harrow Ford", "Lake Ombra"}                     # the holes, not The Ridge
    kw = fin.call_args.kwargs
    entry = next(e for e in kw["repair_log"] if e.get("length_net"))
    net = entry["length_net"]
    assert entry["repair_mode"] == f"sentences+{le.EXTEND_MODE}"
    assert net["after_repair"] < net["before"] < lc.BLOG_FLOOR_CHARS               # the cuts shrank it
    assert net["after_expand"] >= lc.BLOG_FLOOR_CHARS and not net["shrank_under_floor"]
    assert entry[le.EXTEND_MODE]["added"] == 3
    assert kw["status"] == "approved" and lc.measure(calls[-1]) >= lc.BLOG_FLOOR_CHARS
    assert not any(c in kw["content_text"] for c in CUTS)


@pytest.mark.asyncio
async def test_the_same_shape_with_the_switch_off_is_main(_loop_stubs, monkeypatch):
    """Default (switch unset): the full rewrite as on main, no expand, no length record; the rewrite feedback has the
    cuts first and the length last."""
    monkeypatch.delenv(le.EXTEND_ENV, raising=False)
    calls: list[str] = []
    with patch.object(service, "write_content", return_value=(_text(), 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback", return_value=(_text(), 0.05, {}, None)) as rewrite, \
            patch.object(service, "extend_sections") as ext, \
            patch.object(service, "run_quality_gates", side_effect=_gates_on_the_text(calls)), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(REQUEST_ID, PIECE_ID, _ctx(), pool=MagicMock())
    ext.assert_not_called()
    fb = rewrite.call_args.kwargs["revision_feedback"]
    assert fb[-1].startswith("F4_extreme_length: length_short") and all(
        line.endswith(lc.REPLACE_NOT_DELETE) for line in fb[:-1])
    assert not any(SOURCE_LABELS[0] in line or "FACTS lines" in line for line in fb)
    assert not any("length_net" in e for e in fin.call_args.kwargs["repair_log"])


@pytest.mark.asyncio
async def test_a_repair_that_leaves_nothing_to_expand_from_is_recorded_as_shrunk(_loop_stubs, monkeypatch):
    """No untold fact after the cuts: the expand cannot run; the round is recorded (shrank_under_floor) and F4 holds
    the piece, never a silent short pass."""
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    calls: list[str] = []
    real_plan_for = le.plan_for
    with patch.object(service, "write_content", return_value=(_text(), 0.05, {}, None)), \
            patch.object(service.length_extend, "plan_for", side_effect=real_plan_for), \
            patch.object(service.length_extend, "unused_source_facts", side_effect=[["x" * 40], []]), \
            patch.object(service, "repair_sentences", return_value=([""] * len(CUTS), 0.01)), \
            patch.object(service, "extend_sections") as ext, \
            patch.object(service, "run_quality_gates", side_effect=_gates_on_the_text(calls)), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(REQUEST_ID, PIECE_ID, _ctx(), pool=MagicMock())
    ext.assert_not_called()
    kw = fin.call_args.kwargs
    net = next(e for e in kw["repair_log"] if e.get("length_net"))["length_net"]
    assert net["expand"] == "no_untold_facts" and net["after_expand"] is None and net["shrank_under_floor"]
    assert kw["status"] == "held"


def test_the_round_two_texts_name_no_internal_label_a_reader_could_see():
    """The new user-visible-risk strings never carry an issue id or an em-dash."""
    for text in (lc.REPLACE_NOT_DELETE, lc.KEEP_NOT_CUT, lc.NO_SOURCE_MENTION):
        assert not re.search(r"AA-\d", text) and "—" not in text


# ---------------------------------------------------------------- S278 review: repeats are cut, attempt 3 keeps the floor

def _item(sentence: str, problem: str, gate: str) -> dict:
    return {"sentence": sentence, "problem": problem, "gate": gate, "gates": [gate]}


@pytest.mark.parametrize("problem, gate, keep", [
    (_f17("A boat ride waits."), qr.F17_TRIP_FACT, True),
    ("states 3 hours, which the source does not contain — remove that number: 'x'", qr.F1_GROUNDING, True),
    ("repeats an earlier sentence — cut it or say something new: 'Lake Ombra is still.'", qr.F13_REPEAT, False),
    # a folded item whose problems include a repeat is cut like one
    (_f17("A boat ride waits.") + "; ALSO: repeats an earlier sentence — cut it or say something new", qr.F17_TRIP_FACT,
     False),
    ("names a place outside the scope: 'x'", qr.F12_SCOPE, False),
])
def test_keep_not_cut_never_applies_to_a_repeat(problem, gate, keep):
    assert lc.keeps_not_cut(_item("x", problem, gate)) is keep


def test_cut_projection_counts_every_cut_type_sentence():
    text = "## A\n\n" + "Kestrel Vale is green. " * 10
    items = [_item("Kestrel Vale is green.", _f17("Kestrel Vale is green."), qr.F17_TRIP_FACT),
             _item("Kestrel Vale is green.", "repeats an earlier sentence — cut it or say something new: 'x'",
                   qr.F13_REPEAT),
             _item("Kestrel Vale is green.", "names a place outside the scope: 'x'", qr.F12_SCOPE)]
    assert lc.cut_projection(text, items) == lc.measure(text) - 2 * (len("Kestrel Vale is green.") + 1)


def _at_floor_text() -> str:
    """The trial fixture lifted just over the floor (an attempt-2 expand to about the floor), CUTS still in."""
    return _text().replace(CTA, ADDS["Kestrel Vale"] + "\n\n" + ADDS["Harrow Ford"] + "\n\n" + CTA)


def test_the_last_attempt_puts_back_the_cuts_that_would_take_a_blog_under_the_floor():
    text = _at_floor_text()
    assert lc.BLOG_FLOOR_CHARS <= lc.measure(text) < lc.BLOG_FLOOR_CHARS + sum(len(c) for c in CUTS)
    items = sentence_items(text)
    reps = [""] * len(items)
    reps[0] = "A boat ride across Lake Ombra can be added on request."     # a rewrite still splices
    kept_items, kept_reps = service._floor_kept_cuts(text, items, reps, cta=CTA)
    assert [it.get("guard") for it in items[1:]] == [service.FLOOR_KEPT] * (len(CUTS) - 1)
    assert kept_items == items[:1] and kept_reps == reps[:1]
    out = service.sentence_repair.splice_checked(text, kept_items, kept_reps, cta=CTA)
    assert lc.measure(out) >= lc.BLOG_FLOOR_CHARS and all(c in out for c in CUTS[1:])


def test_a_last_attempt_cut_that_stays_over_the_floor_stands():
    text = _at_floor_text() + "\n\n" + " ".join(PAD["The Ridge"])
    items = sentence_items(text)
    kept_items, kept_reps = service._floor_kept_cuts(text, items, [""] * len(items), cta=CTA)
    assert kept_items == items and not any(it.get("guard") for it in items)


def sentence_items(text: str) -> list[dict]:
    ledger = [{"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
               "violations": [_f17(c) for c in CUTS]}]
    items = service.sentence_repair.plan(text, ledger)
    assert items and len(items) == len(CUTS)
    return items


# The attempt-2 → attempt-3 shape the review named: attempt 2 expands a short blog to just over the floor, its gates
# then flag 6 F17 sentences and a scope sentence, and the attempt-3 repair returns "" for every F17 item (and rewrites
# the scope one). Before: the 6 deletes fixed F17, the rewrite fixed F12, F4 broke, and the AA-528 guard kept that
# text (1 failing gate < 2): held on F4_extreme_length. Now: the cuts are put back (the piece holds on F17, at full
# length), the rewrite stands and the round is recorded.
R1 = "Stone walls divide the lower fields into narrow strips."
LEAKY = "The source notes name herons on the shallow bays of Lake Ombra."
SCOPE = "Hedges of hawthorn and hazel line the lower miles of the road."
SCOPE_FIX = "Hawthorn and hazel grow thick beside the lower road."
REWRITES = {LEAKY: "Herons hunt along the shallow bays.", SCOPE: SCOPE_FIX}


def _attempt_three_text() -> str:
    text = _text()
    text = text.replace("A cairn marks the highest point of the path.",
                        f"A cairn marks the highest point of the path. {R1}")
    return text.replace("Coots and grebes nest among the reeds in the shallow bays.",
                        f"Coots and grebes nest among the reeds in the shallow bays. {LEAKY}")


def _three_attempt_gates(calls):
    def f4(text):
        return {**qg.gate_extreme_length(qg.strip_citation_tags(text), "blog"), "blocking": True}

    def run(**kw):
        text = kw["content_text"]
        calls.append(text)
        if len(calls) == 1:
            return _outcome([f4(text), {"gate": qr.F13_REPEAT, "passed": False, "blocking": True, "repairable": True,
                                        "violations": [f"repeats an earlier sentence — cut it or say something new: "
                                                       f"'{R1}'"]},
                             {"gate": qr.F13_LEAK, "passed": False, "blocking": True, "repairable": True,
                              "violations": [f"names where the facts come from — rewrite it: '{LEAKY}'"]}])
        ledger = [f4(text)]
        cuts = [c for c in CUTS if c in text]
        ledger.append({"gate": qr.F17_TRIP_FACT, "passed": not cuts, "blocking": True, "repairable": True,
                       "violations": [_f17(c) for c in cuts]})
        ledger.append({"gate": qr.F12_SCOPE, "passed": SCOPE not in text, "blocking": True, "repairable": True,
                       "violations": [f"names a place outside the scope: '{SCOPE}'"] if SCOPE in text else []})
        return _outcome(ledger)
    return run


def _repair(*, sentences, **kw):
    _repair.calls.append([dict(it) for it in sentences])
    return [REWRITES.get(it["sentence"], "") for it in sentences], 0.01


def _two_adds(*, sections, facts, voice=None, **kw):
    adds = [ADDS["Kestrel Vale"], ADDS["Harrow Ford"]]
    return [adds[i] if i < len(adds) else "" for i in range(len(sections))], 0.02, "writer-model"


@pytest.mark.asyncio
async def test_attempt_three_cuts_never_take_a_just_expanded_blog_back_under_the_floor(_loop_stubs, monkeypatch):
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    monkeypatch.setenv(service.LAST_ATTEMPT_MODE_ENV, service.LEGACY_LAST_ATTEMPT)
    calls: list[str] = []
    _repair.calls = []
    with patch.object(service, "write_content", return_value=(_attempt_three_text(), 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback") as rewrite, \
            patch.object(service, "repair_sentences", side_effect=_repair), \
            patch.object(service, "extend_sections", side_effect=_two_adds), \
            patch.object(service, "run_quality_gates", side_effect=_three_attempt_gates(calls)), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(REQUEST_ID, PIECE_ID, _ctx(), pool=MagicMock())
    rewrite.assert_not_called()
    assert len(calls) == 3 and len(_repair.calls) == 2
    # attempt 2: the repeat was cut with no keep hint, then the expand lifted the blog over the floor (CUTS still in)
    second = _repair.calls[0]
    assert all(lc.KEEP_NOT_CUT not in it["problem"] for it in second if it["gate"] == qr.F13_REPEAT)
    assert lc.measure(calls[1]) >= lc.BLOG_FLOOR_CHARS
    # attempt 3: the F17 problems were told to keep their sentence (6 deletes would take it under the floor)
    third = _repair.calls[1]
    f17_items = [it for it in third if it["gate"] == qr.F17_TRIP_FACT]
    assert len(f17_items) == len(CUTS) and all(it["problem"].endswith(lc.KEEP_NOT_CUT) for it in f17_items)
    # the deletes were put back, the scope rewrite stood: held on F17 at full length, never on length
    kw = fin.call_args.kwargs
    assert lc.measure(kw["content_text"]) >= lc.BLOG_FLOOR_CHARS and all(c in kw["content_text"] for c in CUTS)
    assert SCOPE_FIX in kw["content_text"] and SCOPE not in kw["content_text"]
    assert kw["status"] == "held" and kw["held_reason"].startswith(qr.F17_TRIP_FACT)
    entry = next(e for e in kw["repair_log"] if (e.get("length_net") or {}).get("expand") == "attempt_3_no_expand")
    assert entry["length_floor_kept"] == len(CUTS) and entry["length_net"]["floor_kept"] == len(CUTS)
    assert not entry["length_net"]["shrank_under_floor"]


@pytest.mark.asyncio
async def test_attempt_three_cuts_with_the_switch_off_are_main(_loop_stubs, monkeypatch):
    """Switch off: no keep hint, no put-back, no length record (main's loop byte for byte)."""
    monkeypatch.delenv(le.EXTEND_ENV, raising=False)
    monkeypatch.setenv(service.LAST_ATTEMPT_MODE_ENV, service.LEGACY_LAST_ATTEMPT)
    _repair.calls = []
    with patch.object(service, "write_content", return_value=(_attempt_three_text(), 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback", return_value=(_attempt_three_text(), 0.05, {}, None)), \
            patch.object(service, "repair_sentences", side_effect=_repair), \
            patch.object(service, "extend_sections") as ext, \
            patch.object(service, "run_quality_gates", side_effect=_three_attempt_gates([])), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(REQUEST_ID, PIECE_ID, _ctx(), pool=MagicMock())
    ext.assert_not_called()
    assert all(lc.KEEP_NOT_CUT not in it["problem"] for call in _repair.calls for it in call)
    log = fin.call_args.kwargs["repair_log"]
    assert not any("length_net" in e or "length_floor_kept" in e for e in log)
