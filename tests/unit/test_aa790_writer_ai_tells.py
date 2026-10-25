"""AA-790 (S251) — the T9 writer-side AI-tell fix: the brief, the deterministic pass and the slate lead focus.

Fixtures are sentences from the S251 paid T9 pilot (n = 20; panel AI-tells 4.08, bar 5.5): each tell the panel
named is rewritten or cut, or routed to the existing F15_voice stock-line repair, and clean sentences (facts,
including seed-stated negations) come back byte for byte.
"""
import asyncio
import inspect

import pytest

from services.acp_content_writing import ai_tells, hygiene, prompts, service
from services.acp_content_writing.ai_tells import TellContext, census, lead_focus, residue, rewrite, seed_facts
from services.acp_content_writing.quality_gates import gate_voice
from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import GOALS

CTX = TellContext(cta="Message us to start planning your family trip.")
LEAD = "Lake Khövsgöl holds an estimated 70 percent of Mongolia's fresh water."


def _blog(*paragraphs: str, heading: str = "## Lake Khövsgöl") -> str:
    """A small blog: a lead paragraph, a heading, the paragraphs under test, then a plain closing paragraph."""
    return "\n\n".join([LEAD, heading, *paragraphs, "The day ends at the lakeshore camp."])


def _run(text: str, ctx: TellContext = CTX) -> tuple[str, list[str]]:
    return rewrite(text, ctx)


# ---------------------------------------------------------------- each pilot pattern is rewritten

@pytest.mark.parametrize("before, after", [
    # "not X. It is Y." (58716823, 631f8533, 18a7bbd3): the denial joins what the thing is
    ("In Bhutan, heritage is not only preserved. It remains in active use.",
     "In Bhutan, heritage remains in active use."),
    ("Eating puta in Ura is not a cultural add-on. It is a direct consequence of the valley's altitude.",
     "Eating puta in Ura is a direct consequence of the valley's altitude."),
    ("To visit their settlements is not to encounter a preserved exhibit. It is to meet a living community.",
     "To visit their settlements is to meet a living community."),
    ("For a traveller heading north, Kuang Si Falls is more than a famous landmark. It is a threshold.",
     "For a traveller heading north, Kuang Si Falls is a threshold."),
    ("The seasonal movement of the monk body is not a performance of tradition. It is tradition, still practised.",
     "The seasonal movement of the monk body is tradition, still practised."),
    # one sentence: "X is not A; it is B" (d99c621c)
    ("The elder may share local history. This is not a performance; it is an offer that depends on the evening.",
     "The elder may share local history. This is an offer that depends on the evening."),
    # "not because …, but because …" (be02eff4)
    ("This is one of the encounters the journey offers, not because it is remote, but because of what it "
     "represents.", "This is one of the encounters the journey offers because of what it represents."),
    # "not X but Y" after a copula, or on the same preposition (631f8533, dbf45319)
    ("The rhythm of their year is set not by a calendar but by the forest and the animals within it.",
     "The rhythm of their year is set by the forest and the animals within it."),
    ("Moving through Taktsang is not a single approach to a single building but a passage through thresholds.",
     "Moving through Taktsang is a passage through thresholds."),
    # ", not X." at the end (9f3df6dd, 6ff4a4a8, 58716823)
    ("The NHDC centre is a government enterprise, not a heritage attraction.",
     "The NHDC centre is a government enterprise."),
    ("But Kuang Si is an opening act, not the headline.", "But Kuang Si is an opening act."),
    # stock adverbs (58716823 "Crucially,", 631f8533 "genuinely", 41971759 "genuine")
    ("Crucially, the institute supplies skilled artisans for the restoration of dzongs.",
     "The institute supplies skilled artisans for the restoration of dzongs."),
    ("What makes Khövsgöl genuinely unusual within Mongolia is its ecological position.",
     "What makes Khövsgöl unusual within Mongolia is its ecological position."),
    ("Students work under master craftspeople until genuine mastery develops.",
     "Students work under master craftspeople until mastery develops."),
    ("The lake sits precisely at the boundary where steppe gives way to taiga.",
     "The lake sits at the boundary where steppe gives way to taiga."),
    # the article agrees with the word left after the cut
    ("Families get a genuine encounter with a herding community.",
     "Families get an encounter with a herding community."),
    # internal label (58716823)
    ("The route continues to the handloom centre in Khaling, in Trashigang District, referred to throughout as the "
     "Khaling National Handloom.", "The route continues to the handloom centre in Khaling, in Trashigang District."),
    # long flat sentence: a semicolon joining two clauses with their own subject (631f8533)
    ("Reindeer in Tsaatan culture are used for riding and for milk; the herds are rarely kept for their meat at all.",
     "Reindeer in Tsaatan culture are used for riding and for milk. The herds are rarely kept for their meat at all."),
])
def test_pilot_pattern_is_rewritten(before, after):
    text, log = _run(_blog(before))
    assert after in text, text
    assert log and all(line.startswith("ai_tells: ") for line in log)
    assert text == _blog(after)


@pytest.mark.parametrize("cut", [
    "That's the opening.",                                   # cfd502a1
    "This is the section worth slowing down for.",           # 6ff4a4a8
    "That is a good second day.",                            # 6ff4a4a8
    "This is the day the trip opens up properly.",           # cfd502a1
    "These are not backdrop destinations.",                  # be02eff4 (a lone denial)
    "What the institute produces is not decorative.",        # 7114d650
])
def test_verdict_lines_and_lone_denials_are_cut(cut):
    para = f"The falls drop into turquoise pools. {cut} The water is suitable for swimming."
    text, log = _run(_blog(para))
    assert cut not in text
    assert "The falls drop into turquoise pools. The water is suitable for swimming." in text
    assert len(log) == 1


def test_denial_and_its_verdict_both_go():
    """e2a919e8: "That's not a warm-up. That's the journey." — the denial is cut, then the verdict it leaves."""
    para = "Four days in, you have cycled mountain roads and swum in limestone pools. That's not a warm-up. " \
           "That's the journey."
    text, log = _run(_blog(para))
    assert "Four days in, you have cycled mountain roads and swum in limestone pools." in text
    assert "warm-up" not in text and "That's the journey" not in text
    assert [ln.split(":")[1].strip() for ln in log] == ["denial before its affirmation", "verdict line"]


def test_pronoun_denial_before_its_affirmation_is_cut():
    para = "The dzong houses the sacred relic. It is not preserved as it was. It is maintained because it is needed."
    text, _ = _run(_blog(para))
    assert "The dzong houses the sacred relic. It is maintained because it is needed." in text


def test_angle_label_heading_loses_the_label():
    """8c6ffd08 / eb9c6332: the angle's name ('Mongolia's Family Anchor') as an H2."""
    ctx = TellContext(cta=CTX.cta, angle_name="Lake Khövsgöl as Mongolia's Family Anchor")
    text, log = _run(_blog("The lake sits at roughly 1,645 metres.",
                           heading="## Where Two Worlds Meet: Lake Khövsgöl as Mongolia's Family Anchor"), ctx)
    assert "## Where Two Worlds Meet\n" in text and "Family Anchor" not in text
    assert any("angle label as heading" in ln for ln in log)


def test_heading_that_is_only_the_angle_label_is_dropped():
    ctx = TellContext(cta=CTX.cta, angle_name="Mongolia's Family Anchor")
    text, _ = _run(_blog("The lake sits at roughly 1,645 metres.", heading="## Mongolia's Family Anchor"), ctx)
    assert "Family Anchor" not in text and "The lake sits at roughly 1,645 metres." in text


def test_heading_that_repeats_the_cta_is_dropped():
    """cfd502a1: '## Ready to Go?' and then the CTA 'Ready to go? Message us …'."""
    cta = "Ready to go? Message us and we'll plan your trip together."
    text = _blog("The route reaches Nong Khiaw on day four.", f"## Ready to Go?\n\nThe e-visa costs about $30. {cta}")
    out, log = _run(text, TellContext(cta=cta))
    assert "## Ready to Go?" not in out and cta in out
    assert any("heading repeats the CTA" in ln for ln in log)


def test_clause_restating_the_cta_is_cut():
    """dbf45319: the sentence before the CTA says it again in other words."""
    cta = "Speak with our travel designers to shape your private journey."
    para = ("Atlas & Hearth builds private cultural journeys for travellers who want this depth, and our travel "
            f"designers are ready to shape an itinerary around your interests. {cta}")
    out, log = _run(_blog(para), TellContext(cta=cta, ignore=hygiene.brand_words("Atlas & Hearth")))
    assert f"Atlas & Hearth builds private cultural journeys for travellers who want this depth. {cta}" in out
    assert any("clause restates the CTA" in ln for ln in log)


def test_citation_tags_ride_along():
    para = "Eating puta in Ura is not a cultural add-on. [R:a1] It is a direct consequence of the altitude. [R:a1]"
    out, _ = _run(_blog(para))
    assert "Eating puta in Ura is a direct consequence of the altitude. [R:a1]" in out


def test_the_cta_sentence_is_never_edited():
    cta = "Message us to start planning your genuinely relaxed family trip."
    out, log = _run(_blog(cta), TellContext(cta=cta))
    assert cta in out and not log


def test_the_first_sentence_is_never_cut():
    text = "That's the opening.\n\n## Day 1\n\nThe ride starts in Luang Prabang. The town sits on the Mekong."
    assert _run(text) == (text, [])


# ---------------------------------------------------------------- clean text and facts are left alone

@pytest.mark.parametrize("clean", [
    "Lunch is not included on the third day.",
    "Swimming is allowed in the upper pools, not the lower ones.",
    "The road is not paved.",
    "This is Punakha Dzong.",
    "It is a 30 kilometre ride to the falls.",
    "The main upper fall drops precisely 50 metres into the pool.",
    "The weaving centre is not only a workshop but also a shop.",
    "The camp is not far from the lake.",
    "The community earns income from weaving.",
    "Visitors are not allowed inside the inner shrine, but the courtyard is open.",
    "This is an offer that depends on the elder and the evening.",
    "That is where the trek begins on Day 2 with the Paro guide.",
    # AA-790 review: a new clause after "but" is never folded ("The walk is it is long.")
    "The walk is not difficult but it is long.",
    "The water is not deep but it is very cold in June.",
    "The road is not paved but the jeeps manage it fine.",
    "The hall is not grand but families gather there every evening.",
    "The dzong is not a museum but the monks live there.",
    "The trail is not marked but we walk it with a local guide.",
    "The dzong is not a museum but the monks know what it is.",
])
def test_clean_sentence_unchanged(clean):
    text = _blog(f"The falls drop into turquoise pools. {clean}")
    assert _run(text) == (text, [])


@pytest.mark.parametrize("para", [
    # AA-790 review: effort, cost, payment and access facts a family needs are never cut or folded
    "The falls drop into turquoise pools. This is compulsory.",
    "The falls drop into turquoise pools. This is free.",
    "The falls drop into turquoise pools. That is extra.",
    "The falls drop into turquoise pools. It is not difficult.",
    "The falls drop into turquoise pools. It is not cheap.",
    "The falls drop into turquoise pools. The pass is not reachable by car, it is a two-hour walk.",
    "The falls drop into turquoise pools. The pass is not reachable by car; it is a two-hour walk.",
    "The falls drop into turquoise pools. The pass is not reachable by car. It is a two-hour walk.",
    "The falls drop into turquoise pools. Bring cash for the market, not cards.",
    "The falls drop into turquoise pools. The walk is not difficult but long.",
    "The falls drop into turquoise pools. We start early, not because the walk is difficult, but because the light "
    "is best at dawn.",
])
def test_practical_facts_are_kept(para):
    text = _blog(para)
    assert _run(text) == (text, [])


@pytest.mark.parametrize("before, after", [
    # the copula fold still fires on a parallel complement
    ("The hall is not grey but very green.", "The hall is very green."),
    ("The dzong is not a museum but a working monastery where monks still live.",
     "The dzong is a working monastery where monks still live."),
    # 631f8533: a wh-clause inside Y's noun phrase is not a new clause
    ("The convergence of landscapes here is not a sequence of separate attractions but a coherent argument about "
     "what this corner of the world is.", "The convergence of landscapes here is a coherent argument about what this "
     "corner of the world is."),
])
def test_parallel_copula_contrast_is_still_folded(before, after):
    text, log = _run(_blog(before))
    assert text == _blog(after) and log


def test_a_denial_the_seed_states_is_kept():
    """r2#17 shape: 'not raised primarily for meat' is the seed's own fact, not a set-up (the semicolon still
    splits the long sentence)."""
    para = "The reindeer are not raised primarily for meat; they are used for riding and as pack animals."
    ctx = TellContext(cta=CTX.cta, source="Source itinerary: the herds are not primarily raised for meat.")
    out, log = _run(_blog(para), ctx)
    assert "The reindeer are not raised primarily for meat. They are used for riding" in out
    assert not any("X is not A" in ln for ln in log)
    joined, _ = _run(_blog(para))  # without the seed it is a set-up: the denial goes
    assert "The reindeer are used for riding and as pack animals." in joined


def test_a_contrast_the_seed_states_is_kept():
    text = _blog("The falls drop into turquoise pools. The festival is held not in spring but in autumn.")
    ctx = TellContext(source="Source itinerary: the festival is held not in spring but in autumn.")
    assert _run(text, ctx) == (text, [])


def test_a_verdict_that_names_a_seed_fact_is_kept():
    para = "The monks gather at dawn. This is a working monastery. Visitors walk the courtyard."
    text = _blog(para)
    assert _run(text, TellContext(source="Source itinerary: a working monastery in the valley."))[0] == text


def test_signature_words_are_kept():
    para = "Families find genuine warmth at the homestay table."
    text = _blog(para)
    assert _run(text, TellContext(keep_words=frozenset({"genuine"}))) == (text, [])


def test_text_without_tells_is_byte_identical():
    text = ("## Day 2: Kuang Si Falls\n\nYou cycle south from Luang Prabang on a mountain road. The falls cascade "
            "over travertine terraces. [R:a1]\n\nThe pools are suitable for swimming.\n\n## FAQ\n\n"
            "**Q: Can you swim at Kuang Si Falls?**\nA: Kuang Si Falls has pools that are suitable for swimming.\n")
    assert _run(text) == (text, [])


def test_an_edit_that_breaks_the_text_is_skipped_and_logged():
    """r1#14 shape: cutting the denial would open the paragraph on 'It' with nothing before it."""
    text = _blog("That's not downtime filler. It's a deliberate call to rest before the climb.")
    out, log = _run(text)
    assert out == text
    assert log and all("skipped" in ln for ln in log)


def test_the_pass_is_idempotent_and_clears_the_fixable_tells():
    text = _blog("In Bhutan, heritage is not only preserved. It remains in active use.",
                 "Crucially, the institute supplies artisans. That's the opening.",
                 "The NHDC centre is a government enterprise, not a heritage attraction.")
    once, _ = _run(text)
    assert census(text, CTX)["fixable_total"] >= 4
    assert census(once, CTX)["fixable_total"] == 0
    assert _run(once) == (once, [])


# ---------------------------------------------------------------- residue: the existing F15 stock-line routing

@pytest.mark.parametrize("sentence, phrase", [
    ("This is the day the route earns its reputation, with a long ride south.", "earns its reputation"),
    ("Northern Mongolia rewards those who arrive with patience and curiosity.", "rewards those"),
    ("There is a particular quality to the water at Kuang Si Falls.", "There is a particular quality"),
    ("History in Bhutan has a way of remaining in place.", "has a way of"),
    ("In Bhutan, a piece of cloth is rarely just cloth.", "cloth is rarely just cloth"),
    ("If there is one place along this route that holds it all, it is Punakha Dzong.", "If there is one place"),
    ("The calcium carbonate and the layered limestone are things worth taking time with.", "worth taking time with"),
    ("The caldera floor asks nothing of a child except to stand inside it.", "asks nothing of a"),
])
def test_residue_goes_to_the_f15_stock_line_repair(sentence, phrase):
    text = _blog(sentence)
    assert any(phrase.lower() in p.lower() for p, _ in residue(text))
    result = gate_voice(text, spec=None, stock_blocking=True)
    stock = [v for v in result["violations"] if v.startswith("stock line '")]
    assert stock and result["blocking"] and result["repairable"]


@pytest.mark.parametrize("sentence", [
    "The community earns income from weaving and herding.",
    "The loyalty programme rewards points on every booking.",
    "There is a lake beside the camp.",
    "The cloth is woven on backstrap looms.",
    # AA-790 review: history and way-of-life facts are not stock lines
    "The Tsaatan has a way of life built around the reindeer.",
    "Taktsang earned its name from the legend of the flying tigress.",
    "The dzong earned the title of the most beautiful in Bhutan.",
    "The valley earned its reputation for weaving centuries ago.",
])
def test_residue_patterns_leave_facts_alone(sentence):
    assert residue(_blog(sentence)) == []


# ---------------------------------------------------------------- the brief

GOAL = GOALS[0]
PACK = {"brand_name": "Terra Family Expeditions", "brand_type": "Premium family adventure travel brand",
        "core_idea": "Safe, active journeys for families.", "customer_segment": "Families with children aged 7-16",
        "voice": ["Warm", "Reassuring", "Family-aware"]}
ANGLE = {"name": "Mongolia's Family Anchor", "why_it_works": "w", "formula_fit": "f", "best_final_style": "s"}


def _prompt(channel: str, **kw) -> str:
    return prompts.build_user_prompt(content_seed="Zorig Chusum\nSource itinerary: A visit.", goal=GOAL,
                                     channel_style=get_channel_style(channel), brand_audience=PACK, angle=ANGLE,
                                     cta="Message us.", **kw)


def test_system_prompt_carries_the_ai_tell_rules_and_no_stock_word_of_its_own():
    assert ai_tells.PROMPT_RULES in prompts.SYSTEM_PROMPT
    outside = prompts.SYSTEM_PROMPT.replace(ai_tells.PROMPT_RULES, "")
    assert "genuinely" not in outside and "precisely" not in outside


def test_writer_prompt_version_moves_off_the_pilot_version():
    assert service.WRITER_PROMPT_VERSION != "662d4aa5"  # the S251 pilot's writer_prompt_version


def test_blog_prompts_get_the_heading_rule():
    assert ai_tells.BLOG_HEADING_RULE in _prompt("blog", atom_id="a1")
    assert ai_tells.BLOG_HEADING_RULE in _prompt("blog", route_segments=[("a1", "Lake"), ("a2", "Ruins")])
    assert ai_tells.BLOG_HEADING_RULE not in _prompt("linkedin")


def test_social_prompt_gets_the_audience_rule_and_the_lead_fact():
    p = _prompt("linkedin", lead_focus="Students enrol in a multi-year programme.")
    assert ai_tells.SOCIAL_AUDIENCE_RULE in p
    assert "LEAD WITH THIS FACT FROM THE CONTENT SEED" in p and "multi-year programme" in p
    blog = _prompt("blog", atom_id="a1", lead_focus="Students enrol in a multi-year programme.")
    assert "LEAD WITH THIS FACT" not in blog and ai_tells.SOCIAL_AUDIENCE_RULE not in blog
    assert "LEAD WITH THIS FACT" not in _prompt("linkedin")


# ---------------------------------------------------------------- LinkedIn slate diversity

SEED = ("National Institute for Zorig Chusum — visit\nLocation: Thimphu, Bhutan\nDay 6 of the trip\n"
        "Source itinerary: The institute teaches Bhutan's thirteen traditional arts. Students enrol in a "
        "multi-year programme under master craftspeople. Graduates restore and decorate dzongs and monasteries.\n"
        "Why it matters: Craft passed on by hand.\n"
        "Place background (widely known facts about this place; never promises for this trip):\n"
        "- The thirteen arts include thangka painting, wood carving and weaving.\n"
        "Other trips that include this same moment (for context only — never promise these details for this trip):\n"
        "- Visitors watch students paint thangkas in the morning session.")


def test_seed_facts_are_the_own_source_and_background_lines():
    facts = seed_facts(SEED)
    assert facts[0].startswith("The institute teaches") and len(facts) == 4
    assert not any("morning session" in f for f in facts)  # 'Other trips' lines are never this trip's facts


def test_repeated_writes_lead_with_different_facts():
    leads = [lead_focus(SEED, i) for i in range(5)]
    assert leads[0] is None  # the first write keeps its free choice
    assert len(set(leads[1:4])) == 3 and all(leads[1:])
    assert lead_focus(SEED, 2) == lead_focus(SEED, 2)  # deterministic: writes started together still differ
    assert lead_focus("Pawangka Monastery — visit", 3) is None  # a label-only seed has nothing to rotate


def test_request_write_index_is_zero_on_error_or_without_tenant():
    class _Boom:
        def acquire(self):
            raise RuntimeError("no pool")
    assert asyncio.run(service._request_write_index(None, "r", "p", _Boom())) == 0
    assert asyncio.run(service._request_write_index("t", "r", "p", _Boom())) == 0


def test_writer_input_records_the_lead_fact():
    assert service.writer_input_snapshot({"lead_focus": "Students enrol."})["lead_focus"] == "Students enrol."
    assert "lead_focus" not in service.writer_input_snapshot({})


# ---------------------------------------------------------------- wiring (no new gate)

def test_the_pass_runs_before_hygiene_autofix_under_the_step_guard():
    src = inspect.getsource(service.run_write_background)
    i = src.index('step_guard("ai_tells.rewrite"')
    assert src.index("ai_tells.rewrite(content_text, tell_ctx)") < i < src.index("hygiene.autofix(content_text, hyg)")


def test_no_gate_module_imports_the_pass():
    """The 4-week gate freeze (S250 go/no-go 6b): the pass is a writer step, never a gate or a registry entry."""
    from services.acp_content_writing import quality_gates
    from services.quality import registry
    assert "ai_tells" not in inspect.getsource(quality_gates)
    assert "ai_tells" not in inspect.getsource(registry)
