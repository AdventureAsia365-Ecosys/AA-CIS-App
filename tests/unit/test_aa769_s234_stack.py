"""AA-769 stacked on AA-768 (S234): one set of sentence primitives (hygiene.py) shared by the T9
hygiene and the admin cut / rewrite, and the admin flow on the new F12_scope / F13_hygiene
highlights."""
from services.acp_content_writing import hygiene as h
from services.acp_content_writing import sentence_repair as sr
from api.routers import admin_content_quality as cq

SEED = """ruins of an ancient monastery — visit
Location: ruins of an ancient monastery, Mongolia
Source itinerary: visit the ruins of an ancient monastery destroyed in 1937.
Other trips that include this same moment (for context only — never promise these details for this trip):
- The primary site is Erdene Zuu Monastery, Mongolia's oldest Buddhist monastery."""
WI = {"seed": SEED, "scope": None, "trip": None, "cta": "Ask Terra Family Expeditions to plan it.",
      "brand_pack": {"brand_name": "Terra Family Expeditions"}}


# ── one implementation ───────────────────────────────────────────────────────────────────────

def test_sentence_primitives_have_one_implementation():
    for name in ("is_sentence_end", "sentence_tail_end", "expand_to_sentence", "tidy_after_cut"):
        assert getattr(sr, name) is getattr(h, name), name


def test_sentence_spans_respect_abbreviations_and_decimals():
    text = "We hike to Mt. Fuji at dawn. It is 12.5 km long. Is it hard? No. Room No. 5 is ours."
    assert [s["clean"] for s in h.sentence_spans(text)] == [
        "We hike to Mt. Fuji at dawn.", "It is 12.5 km long.", "Is it hard?", "No.", "Room No. 5 is ours."]


def test_is_sentence_end_no_only_before_a_number():
    assert h.is_sentence_end("Is it hard? No. Then", 14)
    assert not h.is_sentence_end("Room No. 5 is ours.", 7)


# ── markers, FAQ and hashtags ────────────────────────────────────────────────────────────────

def test_expand_keeps_the_faq_answer_marker_and_cut_drops_the_pair():
    text = "Intro here.\n\n## FAQ\n\n**Q: Is it busy?**\nA: Travellers love it here.\n\n**Q: How far?**\nA: Ten km."
    i = text.find("Travellers")
    s, e = sr.expand_to_sentence(text, i, i + 5)
    assert text[s:e] == "Travellers love it here."
    out = sr.apply_edit(text, s, e, "")
    assert out == "Intro here.\n\n## FAQ\n\n**Q: How far?**\nA: Ten km."


def test_rewrite_of_a_faq_answer_keeps_its_marker():
    text = "**Q: Is it busy?**\nA: Travellers love it here.\n"
    i = text.find("Travellers")
    s, e = sr.expand_to_sentence(text, i, i + 5)
    assert sr.apply_edit(text, s, e, "It is quiet in the morning.") == "**Q: Is it busy?**\nA: It is quiet in the morning.\n"


def test_bullet_cut_leaves_no_empty_marker():
    text = "Intro.\n- a hidden gem list item\n- Second item.\nEnd."
    i = text.find("hidden gem")
    s, e = sr.expand_to_sentence(text, i, i + 10)
    assert sr.apply_edit(text, s, e, "") == "Intro.\n- Second item.\nEnd."


def test_hashtag_highlight_acts_on_the_token_only():
    text = "Body text here.\n\n#Bhutan #PakoTaktsang #Travel"
    f13 = {"gate": "F13_hygiene", "passed": False, "blocking": False,
           "violations": ["misspelt hashtag '#PakoTaktsang' (did you mean '#ParoTaktsang'?): '#PakoTaktsang'"]}
    [hl] = cq.highlights(text, [f13], [])
    assert text[hl["sentence_start"]:hl["sentence_end"]] == "#PakoTaktsang"
    [item] = cq.flagged_sentences(text, [hl])
    assert sr.apply_edit(text, item["start"], item["end"], "") == "Body text here.\n\n#Bhutan #Travel"
    assert sr.apply_edit(text, item["start"], item["end"], "#ParoTaktsang") == \
        "Body text here.\n\n#Bhutan #ParoTaktsang #Travel"


def test_f12_body_highlight_is_a_whole_sentence_and_title_hits_are_skipped():
    text = "The ruins stand quiet. Erdene Zuu is older than these walls. [R:a1] Bye now."
    f12 = {"gate": "F12_scope", "passed": False, "blocking": False, "violations": [
        "names 'Erdene Zuu', outside this piece's scope: 'Erdene Zuu is older than these walls.'",
        "title names 'Gobi', outside this piece's scope: 'Gobi and more'"]}
    [hl] = cq.highlights(text, [f12], [])
    assert text[hl["sentence_start"]:hl["sentence_end"]] == "Erdene Zuu is older than these walls. [R:a1]"


def test_apply_edit_keeps_the_trailing_newline_state():
    assert sr.apply_edit("A one.\n\nCut me.", 8, 15, "") == "A one."
    assert sr.apply_edit("A one.\n\nCut me.\n", 8, 15, "") == "A one.\n"


# ── replacement checks for the new gates ─────────────────────────────────────────────────────

def test_check_replacement_runs_scope_and_hygiene_with_writer_input():
    gates = lambda s, **kw: [f["gate"] for f in sr.check_replacement(s, SEED, WI["brand_pack"],  # noqa: E731
                                                                      writer_input=WI, **kw)]
    assert gates("The ruins stand above the valley.") == []
    assert gates("Erdene Zuu stands above the valley.") == ["F12_scope"]
    assert gates("The content seed notes the ruins are old.") == ["F13_hygiene"]
    assert gates("Families often love the quiet ruins.") == ["F13_hygiene"]
    # without writer_input (a piece written before AA-769) only the AA-769 gates run
    assert sr.check_replacement("Erdene Zuu stands above the valley.", SEED, None) == []


def test_check_replacement_refuses_a_restated_neighbour_instead_of_a_silent_cut():
    text = ("The monastery ruins were destroyed during the purges of 1937 and remain quiet. "
            "Flagged sentence here now. The valley is green.")
    i = text.find("Flagged")
    span = (i, i + len("Flagged sentence here now."))
    rep = "The monastery ruins were destroyed in the purges of 1937 and remain quiet today."
    failed = sr.check_replacement(rep, text + " 1937", None, text=text, span=span)
    assert [f["gate"] for f in failed] == ["F13_hygiene"] and "restates" in failed[0]["violations"][0]
    # apply_edit never turns a rewrite into a cut (splice guard off for the admin path)
    assert rep in sr.apply_edit(text, *span, rep)
    # the pipeline splice keeps its guard
    items = [{"start": span[0], "end": span[1]}]
    assert rep not in sr.splice(text, items, [rep]) and items[0]["guard"] == "repeat"
