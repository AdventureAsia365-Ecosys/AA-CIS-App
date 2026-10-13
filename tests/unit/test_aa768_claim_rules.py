"""AA-768 (S234, PR5) — F9 v2 claim rules: source-line typing, the rule table, quote verification,
tripwires, the violation format and the deterministic CTA check. Pure functions; nothing here is
wired into run_quality_gates yet."""
from __future__ import annotations

import inspect

from api.routers.admin_content_quality import highlights
from services.acp_content_writing import claim_rules as cr
from services.acp_content_writing import quality_gates, sentence_repair
from services.acp_content_writing.claim_rules import ClaimRow, RulesConfig
from services.acp_shared.atom_seed import compose_route_moment, compose_seed

CARD = {
    "card_id": "c1", "display_name": "Erdene Zuu Monastery", "locality": "Kharkhorin",
    "region": "Övörkhangai", "country": "Mongolia", "naming_note": "Also written 'Erdene-Zuu'.",
    "facts": [{"text": "Erdene Zuu was founded in 1585 by Abtai Sain Khan."},
              {"text": "Much of the monastery was destroyed in the 1937 purges."},
              {"text": "It is Mongolia's oldest surviving Buddhist monastery."}],
}


def _seed_with_card() -> str:
    return compose_seed("Erdene Zuu Monastery — visit", "Visit Erdene Zuu Monastery in the afternoon.",
                        "the first Buddhist centre of the empire", "Summer festivals in July", [],
                        place="Erdene Zuu Monastery", country="Mongolia", day=6, card=CARD)


def _seed_without_card() -> str:
    return compose_seed("Sod Camp — stay", "Overnight at Sod Camp. Optional activities: visit nomadic families.",
                        None, None, ["Overnight in a ger camp by the lake."],
                        place="Sod Camp", country="Mongolia", day=3, card=None)


def _grounding(seed: str, facts: str = "", brand: bool = True) -> str:
    parts = [seed, facts]
    if brand:
        parts += ["BRAND: Terra Family Expeditions", "BRAND CORE IDEA: Safe, active journeys for families.",
                  "AUDIENCE (who the brand writes for — not a fact about this trip): Families with children "
                  "aged 7–16", "TRIP: Mongolia: Gobi & Khovsgol", "DESTINATION: Mongolia"]
    return "\n\n".join(p for p in parts if p)


def _by_kind(lines):
    out = {}
    for ln in lines:
        out.setdefault(ln.kind, []).append(ln)
    return out


# ---------------------------------------------------------------- source lines

def test_number_source_lines_round_trips_compose_seed_with_card():
    facts = "[Fact id=f-1]\nGer etiquette: Do not step on the threshold."
    lines = cr.number_source_lines(_grounding(_seed_with_card(), facts))
    k = _by_kind(lines)
    assert [ln.id for ln in k["S"]] == ["S1", "S2", "S3", "S4", "S5"]
    assert k["S"][0].text == "Erdene Zuu Monastery — visit"
    assert k["S"][1].text == "Day 6 of the trip"
    assert k["S"][2].text.startswith("Source itinerary:")
    assert [ln.text.split(":")[0] for ln in k["L"]] == ["Location", "Naming"]
    assert len(k["P"]) == 3 and k["P"][1].text.startswith("Much of the monastery")
    assert all(ln.place.startswith("Erdene Zuu Monastery, Kharkhorin") for ln in k["P"])
    assert "do not" not in k["P"][0].place
    assert k["F"][0].fact_id == "f-1" and k["F"][0].text == "Ger etiquette: Do not step on the threshold."
    assert [ln.kind for ln in lines if ln.text.startswith(("BRAND", "AUDIENCE", "TRIP", "DESTINATION"))] == \
        ["B", "B", "A", "T", "T"]
    assert "O" not in k  # a rich card carries no sibling lines (S234)


def test_number_source_lines_without_card_has_sibling_and_location_lines():
    k = _by_kind(cr.number_source_lines(_seed_without_card()))
    assert k["O"][0].text == "Overnight in a ger camp by the lake."
    assert k["L"][0].text.startswith("Location: Sod Camp, Mongolia")
    assert k["O"][0].place == "Sod Camp, Mongolia"
    assert "P" not in k


def test_number_source_lines_route_blocks_and_old_sibling_header():
    route = [
        {"atom_id": "a1", "text": "Erdene Zuu Monastery — visit", "evidence": "Visit Erdene Zuu.", "day": 6,
         "place": "Erdene Zuu Monastery", "country": "Mongolia", "card": CARD},
        {"atom_id": "a2", "text": "Sod Camp — stay", "evidence": "Overnight at Sod Camp.", "day": 3,
         "place": "Sod Camp", "country": "Mongolia", "card": None},
        {"atom_id": "a3", "text": "Uran Togoo — hike", "evidence": "Hike the extinct volcano.", "day": 4,
         "place": "Uran Togoo", "country": "Mongolia", "card": None},
    ]
    seed = "\n\n".join(compose_route_moment([p]) for p in route)
    lines = cr.number_source_lines(seed)
    assert sorted({ln.block for ln in lines}) == [1, 2, 3]
    assert {ln.place.split(",")[0] for ln in lines} == {"Erdene Zuu Monastery", "Sod Camp", "Uran Togoo"}
    assert not any(ln.text.startswith("Thin moment") for ln in lines)
    old = ("Craft Bazaar — wander\nLocation: Craft Bazaar, Bhutan (do not name any other country or region for it)\n"
           "Source itinerary: Explore the Craft Bazaar.\nOther itineraries with the same moment:\n"
           "- Afternoon brings the Takin Zoo and Craft Bazaar.")
    k = _by_kind(cr.number_source_lines(old))
    assert k["O"][0].text == "Afternoon brings the Takin Zoo and Craft Bazaar."
    assert k["L"][0].place == "Craft Bazaar, Bhutan"


def test_extraction_prompt_hides_other_trip_and_audience_lines():
    lines = cr.number_source_lines(_grounding(_seed_without_card()))
    sents = cr.split_piece("A calm start. The lake is cold.", seo_title="Sod Camp with kids", meta="A stay.")
    prompt = cr.build_extraction_prompt(lines, sents)
    assert "O1:" not in prompt and "ger camp by the lake" not in prompt
    assert "Families with children" not in prompt
    assert "S1: Sod Camp — stay" in prompt and "[T] Sod Camp with kids" in prompt and "[M] A stay." in prompt
    schema = cr.extraction_schema()["schema"]
    assert schema["properties"]["rows"]["items"]["properties"]["t"]["enum"] == list(cr.CLAIM_TYPES)


def test_split_piece_numbers_body_skips_hashtags_and_adds_title_meta():
    text = "## Day six\nThe monastery is old [R:a1]. Pacing is built in.\n\n#Mongolia #ErdeneZuu"
    sents = cr.split_piece(text, seo_title="Erdene Zuu", meta="Old walls.")
    assert [(s.sid, s.text) for s in sents] == [
        ("1", "Day six"), ("2", "The monastery is old."), ("3", "Pacing is built in."),
        ("T", "Erdene Zuu"), ("M", "Old walls.")]


def test_mask_names_keeps_place_words_out_of_the_tripwires():
    lex = cr.scope_lexicon(cr.number_source_lines(_seed_without_card()))
    assert "trip" in cr.tripwires("We camp by the lake.")
    assert "trip" not in cr.tripwires(cr.mask_names("Sod Camp sits by the lake.", lexicon=lex))
    assert "trip" not in cr.tripwires(cr.mask_names("It sits near Paro.", ("Paro Private",)))
    assert "trip" in cr.tripwires(cr.mask_names("Pacing is built in.", lexicon=lex))


def test_mask_names_leaves_unknown_title_case_words_visible():
    """Review S234: every capitalised run used to be blanked, so a title-case SEO title or heading
    hid its trip words from the rules."""
    lex = cr.scope_lexicon(cr.number_source_lines(_seed_without_card()))
    title = "Sod Camp Stay With Private Guide, Hotel And Lunch Included"
    masked = cr.mask_names(title, lexicon=lex)
    assert "Sod" not in masked and "Camp" not in masked
    assert "Guide" in masked and "Lunch Included" in masked
    # a capitalised seed word that is only trip vocabulary ('Overnight', 'Dinner') is not a name
    lex2 = cr.scope_lexicon(cr.number_source_lines("Dinner at the camp — stay\nSource itinerary: Dinner at the camp."))
    assert "Dinner" in cr.mask_names("Dinner is served by the fire.", lexicon=lex2)


def test_trip_promise_in_title_case_title_and_heading_fails():
    v, _ = _run("Ladakh Tour With Private Guide, Hotel And Lunch Included", None, where_title=True)
    assert v.sid == "T" and v.type == "uncited_trip" and not v.ok and v.blocking
    v, _ = _run("## Private Guide And Hotel Transfers Included", None)
    assert v.type == "uncited_trip" and not v.ok


# ---------------------------------------------------------------- rule table

def _run(sentence, row_kwargs, seed=None, cta=None, config=RulesConfig(), where_title=False):
    lines = cr.number_source_lines(_grounding(seed or _seed_without_card()))
    sents = cr.split_piece(sentence) if not where_title else cr.split_piece("", seo_title=sentence)
    sid = sents[0].sid
    rows = {sid: ClaimRow(sid, **row_kwargs)} if row_kwargs is not None else {}
    return cr.apply_rules(rows, lines, sents, cta=cta, config=config)[0], lines


def _line(lines, kind, n=1):
    return [ln for ln in lines if ln.kind == kind][n - 1]


def test_trip_fact_citing_sibling_place_or_audience_fails():
    _, lines = _run("x.", None)
    o = _line(lines, "O")
    v, _ = _run("Each night is spent in a ger camp by the lake.",
                {"type": "trip_fact", "ref": o.id, "quote": "ger camp by the lake"})
    assert not v.ok and v.blocking and "another trip" in v.problem
    a = _line(lines, "A")
    v, _ = _run("The trip is built for children aged 7–16.",
                {"type": "trip_fact", "ref": a.id, "quote": "Families with children aged"})
    assert not v.ok and "audience" in v.problem
    v, lines2 = _run("You will visit the monastery founded in 1585.",
                     {"type": "trip_fact", "ref": "P1", "quote": "founded in 1585"}, seed=_seed_with_card())
    assert not v.ok and "place background" in v.problem


def test_trip_fact_citing_own_itinerary_with_real_quote_passes():
    v, _ = _run("The first night is spent at Sod Camp.",
                {"type": "trip_fact", "ref": "S3", "quote": "Overnight at Sod Camp"})
    assert v.ok and v.ref == "S3"


def test_fabricated_or_unrelated_quote_fails():
    v, _ = _run("The first night is spent at Sod Camp.",
                {"type": "trip_fact", "ref": "S3", "quote": "two nights at Sod Camp with a guide"})
    assert not v.ok and "quote is not in S3" in v.problem
    # the quote is real but shares no 5+-letter content word with the sentence
    v, _ = _run("Breakfast is served at dawn.", {"type": "trip_fact", "ref": "S3", "quote": "Overnight at Sod Camp"})
    assert not v.ok


def test_optional_activity_presented_as_included_fails():
    v, _ = _run("The day also includes visits to local nomadic families.",
                {"type": "trip_fact", "ref": "S3", "quote": "Optional activities: visit nomadic families"})
    assert not v.ok and "optional" in v.problem
    v, _ = _run("You can add a visit to local nomadic families.",
                {"type": "trip_fact", "ref": "S3", "quote": "Optional activities: visit nomadic families"})
    assert v.ok


def test_social_proof_is_forced_whatever_the_model_typed():
    v, _ = _run("This is the section that tends to stay with children longest.", {"type": "opinion"})
    assert v.type == "social_proof" and not v.ok and v.blocking
    v, _ = _run("Pawangka is not a stop that appears on every Ladakh itinerary.", {"type": "generic"})
    assert v.type == "social_proof" and not v.ok


def test_social_proof_fails_even_on_a_claim_type_with_a_verified_quote():
    """Review S234: a trip_fact / place_background row with a real quote (one shared word, e.g.
    the place name) used to let a rarity or social-proof sentence through."""
    for t in ("trip_fact", "place_background", "scene_detail"):
        v, _ = _run("Most travellers skip Sod Camp, but this trip stays there overnight.",
                    {"type": t, "ref": "S3", "quote": "Overnight at Sod Camp"})
        assert v.type == "social_proof" and not v.ok and v.blocking, t
    v, _ = _run("Sod Camp is rarely visited, and visitors never find their way here.",
                {"type": "place_background", "ref": "S3", "quote": "Overnight at Sod Camp"})
    assert v.type == "social_proof" and not v.ok


def test_exempt_type_with_trip_tripwire_fails_and_scene_only_warns():
    v, _ = _run("Pacing is built in.", {"type": "opinion"})
    assert v.type == "uncited_trip" and not v.ok and v.blocking
    v, _ = _run("Your private guide meets you at the gate.", {"type": "opinion"})
    assert v.type == "uncited_trip" and not v.ok
    v, _ = _run("The silence there is worth the effort.", {"type": "opinion"})
    assert v.ok and not v.blocking and "scene detail" in v.problem
    v, _ = _run("That is what makes it worth the effort.", {"type": "opinion"})
    assert v.ok and not v.problem


def test_ambient_trip_word_alone_warns_and_questions_pass():
    """Review S234 (S233 Set B): opinion lines about pacing and FAQ questions blocked as
    uncited_trip. An ambient word (pace, morning, private, access) now blocks only inside a
    promise frame; a question asserts nothing."""
    for sent in ("Pacing matters.", "They struggle with pacing.", "Active travel brands have a pacing problem."):
        v, _ = _run(sent, {"type": "opinion"})
        assert v.ok and not v.blocking and "trip-sounding" in v.problem, sent
        v, _ = _run(sent, None)
        assert v.ok, sent
    for sent in ("The pace is designed to let children rest.", "A hot stone bath follows in the afternoon.",
                 "This trip keeps a private pace."):
        v, _ = _run(sent, {"type": "opinion"})
        assert v.type == "uncited_trip" and not v.ok, sent
    lines = cr.number_source_lines(_grounding(_seed_without_card()))
    text = ("## FAQ\n\n**Q: Do I need a permit to visit the Tsaatan reindeer herders?**\n"
            "A: The guide arranges it.\n\nWhy is access to their settlements limited?")
    vs = cr.apply_rules({}, lines, cr.split_piece(text))
    by_text = {v.sentence: v for v in vs}
    assert by_text["Q: Do I need a permit to visit the Tsaatan reindeer herders?"].ok
    assert by_text["Why is access to their settlements limited?"].ok
    assert not by_text["The guide arranges it."].ok  # the answer still asserts


def test_brand_self_citing_its_brand_line_passes_unless_it_adds_a_service():
    seed = _grounding(_seed_without_card()).replace(
        "Safe, active journeys for families.", "Private cultural journeys with depth.")
    v, _ = _run("Terra Family Expeditions builds private cultural journeys for travellers who want depth.",
                {"type": "brand_self", "ref": "B2", "quote": "Private cultural journeys with depth"}, seed=seed)
    assert v.ok and not v.problem
    v, _ = _run("Terra Family Expeditions builds private cultural journeys with hotel transfers included.",
                {"type": "brand_self", "ref": "B2", "quote": "Private cultural journeys with depth"}, seed=seed)
    assert v.type == "uncited_trip" and not v.ok and "brand line B2" in v.problem


def test_model_cta_label_alone_does_not_exempt_a_trip_promise():
    v, _ = _run("Your private guide will arrange everything.", {"type": "cta"}, cta="Plan your Mongolia trip with us")
    assert not v.ok
    v, _ = _run("Plan your Mongolia trip with us today.", {"type": "cta"}, cta="Plan your Mongolia trip with us")
    assert v.ok and v.type == "cta"


def test_location_body_outside_source_fails_variants_and_gazetteer_do_not():
    v, _ = _run("The monastery sits above Luang Prabang.",
                {"type": "location", "places": ("Luang Prabang",)}, seed=_seed_with_card())
    assert not v.ok and "Luang Prabang" in v.problem
    v, _ = _run("Erdene Zuu stands in Kharkhorin.",
                {"type": "location", "places": ("Erdene Zuu", "Kharkhorin")}, seed=_seed_with_card())
    assert v.ok
    v, _ = _run("Övörkhangai holds it.", {"type": "location", "places": ("Ovorkhangai",)}, seed=_seed_with_card())
    assert v.ok  # accent-folded variant of an allowed place


def test_location_in_title_is_left_to_f12_scope():
    v, _ = _run("Gobi and Khovsgol with kids", {"type": "location", "places": ("Gobi",)}, where_title=True)
    assert v.ok and v.sid == "T"


def test_uncited_background_warns_only_without_a_card_and_without_numbers():
    v, _ = _run("Sod Camp sits in a region long used by herders.",
                {"type": "place_background", "places": ("Sod Camp",)})
    assert v.ok and not v.blocking and "uncited background" in v.problem
    v, _ = _run("Sod Camp sits in a region long used by herders.",
                {"type": "place_background", "places": ("Sod Camp",)}, config=RulesConfig(uncited_background="fail"))
    assert not v.ok
    v, _ = _run("Bhutan's visa fee is charged per night.", {"type": "place_background", "places": ("Bhutan",)})
    assert not v.ok  # regulatory word
    v, _ = _run("The monastery was rebuilt by monks.", {"type": "place_background", "places": ("Erdene Zuu",)},
                seed=_seed_with_card())
    assert not v.ok and "cite a P line" in v.problem  # the place has a card: cite it
    v, _ = _run("Much of it was lost in the 1937 purges.",
                {"type": "place_background", "ref": "P2", "quote": "destroyed in the 1937 purges",
                 "places": ("Erdene Zuu",)}, seed=_seed_with_card())
    assert v.ok


def test_hypothetical_policy_flag():
    v, _ = _run("Imagine standing at the gate at dawn.", {"type": "hypothetical"})
    assert v.ok and not v.blocking and v.problem
    v, _ = _run("Imagine standing at the gate at dawn.", {"type": "hypothetical"},
                config=RulesConfig(hypothetical="fail"))
    assert not v.ok
    v, _ = _run("The gate glows at dawn.", {"type": "hypothetical"})
    assert not v.ok and v.type == "scene_detail"  # no conditional marker: a plain scene claim


def test_hypothetical_cannot_carry_a_trip_promise():
    """Review S234: 'hypothetical' + a 'could' / 'if you' marker returned WARN before any
    tripwire, so a promise could pass by being typed hypothetical."""
    for sent in ("You could stay in a private lodge with all meals included.",
                 "If you join us, a private guide and hotel transfers are included."):
        v, _ = _run(sent, {"type": "hypothetical"})
        assert v.type == "uncited_trip" and not v.ok and v.blocking, sent
    v, _ = _run("You could spend the first night at Sod Camp.",
                {"type": "hypothetical", "ref": "S3", "quote": "Overnight at Sod Camp"})
    assert v.ok  # no concrete service word: the G2 flag decides
    v, _ = _run("Imagine the gate at dawn.", {"type": "hypothetical"})
    assert v.ok and "imagined scene" in v.problem


def test_scene_detail_needs_a_quoted_line():
    v, _ = _run("Worn stone steps lead to the gate.", {"type": "scene_detail"})
    assert not v.ok and v.type == "scene_detail"


def test_spelled_out_numbers_not_in_source_fail():
    sent = "You need a thirty-day e-visa costing thirty to forty-five US dollars."
    assert cr.find_novel_word_numbers(sent, ["Visa required."]) == ["thirty-day", "thirty", "forty-five US dollars"]
    assert cr.find_novel_word_numbers("A three-day stay.", ["Day 1-3: 3 days at the lake"]) == []
    assert cr.find_novel_word_numbers("Two monasteries and one day.", ["nothing"]) == []
    v, _ = _run(sent, {"type": "opinion"})
    assert v.type == "number" and not v.ok


def test_compound_spelled_out_numbers_match_the_source_digits():
    """Review S234: 'twenty thousand' was read as 20 and 'thousand years' as 1000, so a correct
    paraphrase of '20,000–24,000 years' blocked."""
    src = ["It last erupted approximately 20,000–24,000 years ago.", "Altitude 3,000 m.",
           "A climb of 200 steps to the gate.", "Built in 1,350 by monks."]
    assert cr.find_novel_word_numbers(
        "It last erupted approximately twenty thousand to twenty-four thousand years ago.", src) == []
    assert cr.find_novel_word_numbers("It sits at three thousand metres.", src) == []
    assert cr.find_novel_word_numbers("Climb two hundred steps to the gate.", src) == []
    assert cr.find_novel_word_numbers("Built one thousand three hundred and fifty years ago.", src) == []
    assert cr.find_novel_word_numbers("It sits at four thousand metres.", src) == ["four thousand metres"]
    assert cr.find_novel_word_numbers("It erupted thirty thousand years ago.", ["no figures"]) == \
        ["thirty thousand years"]
    assert cr.word_numbers("twenty-four thousand years")[0][1] == 24000


def test_day_index_ranges_and_ages_are_not_figures():
    for sent in ("By day two or three, people are running on fumes.", "On day four and five the pace drops.",
                 "Even a twelve-year-old manages the climb.", "It suits a curious eleven-year-old."):
        assert cr.find_novel_word_numbers(sent, ["no figures"]) == [], sent


def test_novel_number_with_a_verified_digit_citation_warns():
    seed = compose_seed("Uran Togoo — hike", "Hike the extinct volcano, last active 8,000 years ago.", None, None,
                        [], place="Uran Togoo", country="Mongolia", day=4, card=None)
    v, _ = _run("The volcano went quiet about eight thousand years ago — some say nine thousand years.",
                {"type": "place_background", "ref": "S3", "quote": "extinct volcano, last active 8,000 years ago"},
                seed=seed)
    assert v.ok and not v.blocking and v.type == "number" and "check it against S3" in v.problem
    v, _ = _run("The volcano went quiet nine thousand years ago.", {"type": "place_background"}, seed=seed)
    assert not v.ok and v.type == "number"


def test_deterministic_only_mode_reads_missing_rows_as_generic():
    lines = cr.number_source_lines(_seed_without_card())
    sents = cr.split_piece("Pacing is built in. The lake is lovely. Most families say it is the best day.")
    vs = cr.apply_rules({}, lines, sents)
    assert [(v.ok, v.type) for v in vs] == [(False, "uncited_trip"), (True, "generic"), (False, "social_proof")]
    assert all(v.declared == "" for v in vs)


def test_parse_rows_ignores_unknown_ids_types_and_reports_missing():
    sents = cr.split_piece("One. Two sentences here. Three.")
    rows, missing = cr.parse_rows({"rows": [
        {"i": "1", "t": "trip_fact", "ref": "S1", "q": "x", "p": ["Sod Camp"]},
        {"i": "1", "t": "opinion", "ref": "", "q": "", "p": []},
        {"i": "9", "t": "opinion", "ref": "", "q": "", "p": []},
        {"i": "[3]", "t": "made_up", "ref": "", "q": "", "p": []},
    ]}, sents)
    assert rows["1"].type == "trip_fact" and rows["1"].places == ("Sod Camp",)
    assert rows["3"].type == "generic"
    assert missing == ["2"]


# ---------------------------------------------------------------- violation format

def test_violations_parse_with_sentence_repair_and_admin_highlights():
    text = "Pacing is built in (really). The lake is lovely; it is calm. Most families say it is the best day."
    lines = cr.number_source_lines(_seed_without_card())
    vs = cr.apply_rules({}, lines, cr.split_piece(text))
    viol = cr.format_violations(vs)
    assert len(viol) == 1 and viol[0].startswith("audit flagged: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): ")
    parsed = sentence_repair._F9_CLAIM_RE.findall(viol[0].split("unsupported claim(s):", 1)[1])
    assert [p[0] for p in parsed] == ["Pacing is built in (really).", "Most families say it is the best day."]
    assert [p[1] for p in parsed] == ["uncited_trip", "social_proof"]
    ledger = [{"gate": "F9_cta_fact", "passed": False, "blocking": True, "violations": viol}]
    items = sentence_repair.plan(text, ledger)
    assert items is not None and len(items) == 2
    hl = highlights(text, ledger, [])
    assert len(hl) == 2 and all(h["gate"] == "F9_cta_fact" for h in hl)
    assert cr.format_violations(vs, blocking=False) == []


def test_warn_verdicts_format_separately():
    lines = cr.number_source_lines(_seed_without_card())
    vs = cr.apply_rules({}, lines, cr.split_piece("The silence there is worth it."))
    assert cr.format_violations(vs) == []
    assert "scene detail" in cr.format_violations(vs, blocking=False)[0]


# ---------------------------------------------------------------- deterministic CTA

def test_cta_reworded_passes():
    cta = "Contact Terra Family Expeditions to plan your Mongolia trip"
    text = ("Para one about the lake.\n\nPara two about the monastery.\n\n"
            "When you are ready, contact Terra Family Expeditions and we will start planning this Mongolia trip "
            "for you.")
    assert cr.check_cta(text, cta, "linkedin").ok


def test_cta_cut_off_or_only_after_faq_fails():
    cta = "Contact Terra Family Expeditions to plan your Mongolia trip"
    assert not cr.check_cta("A piece about a lake that just ends here.", cta, "linkedin").ok
    blog = ("# Title\n\n" + "Body text about the lake. " * 40 + "\n\n## FAQ\n\n**Q: How?**\n"
            "A: Contact Terra Family Expeditions to plan your Mongolia trip.")
    r = cr.check_cta(blog, cta, "blog")
    assert not r.ok and r.repairable and "missing" in r.problem


def test_cta_empty_is_not_repairable():
    r = cr.check_cta("Anything.", "", "blog")
    assert not r.ok and not r.repairable


def test_hashtags_do_not_count_for_cta():
    cta = "Plan your Bhutan journey"
    text = "Body.\n\nMore body.\n\n#PlanYourBhutanJourney #Bhutan"
    assert not cr.check_cta(text, cta, "linkedin").ok


# ---------------------------------------------------------------- not wired (S234)

def test_claim_rules_is_not_wired_into_run_quality_gates_yet():
    assert "claim_rules" not in inspect.getsource(quality_gates)
