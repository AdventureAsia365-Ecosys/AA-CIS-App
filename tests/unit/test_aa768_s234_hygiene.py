"""AA-768 (S234) — T9 hygiene: scope lexicon, in-place fixes, residual F12/F13, the splice repeat
guard and the FAQ dropper. Fixtures are trimmed copies of the S233 r1 Mongolia route seed and
pieces (.tmp-session/s233/pieces_r1.json)."""
import re

import pytest

from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr

TRIP = "Mongolia: Khövsgöl Lake and Gobi Desert — 15 Days"
SCOPE = ("SCOPE: this piece covers days 3–14 of the trip, in this order: Sod Camp → Uran Togoo → Moron → "
         "Lake Khövsgöl → Tsaatan reindeer herders' settlements → ruins of an ancient monastery.")
RUINS = """ruins of an ancient monastery — visit
Location: ruins of an ancient monastery, Mongolia (do not name any other country or region for it)
Day 14 of the trip
Source itinerary: visit the ruins of an ancient monastery destroyed during the political purges of 1937.
Why it matters: encounter historical loss and cultural memory
Other trips that include this same moment (for context only — never promise these details for this trip):
- A half-day tour of Ulaanbaatar includes a visit to Gandan Monastery.
- The primary site is Erdene Zuu Monastery, Mongolia's oldest Buddhist monastery, dating to the late 16th century. For centuries, thousands of monks lived on the grounds and contributed significantly to Mongolian culture."""
SOD = """Sod Camp — overnight in a standard ger camp
Location: Sod Camp, Khövsgöl Lake area, Khövsgöl Province, Mongolia (do not place it in any other town, region or country)
Naming: Sod Camp is a tourist ger camp on the shores of Lake Khövsgöl (also spelled Khubsugul or Хөвсгөл).
Day 3 of the trip
Source itinerary: Arrive at Sod Camp in the afternoon. Overnight in a standard ger camp.
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- The camp sits at the edge of the Khövsgöl National Park.
- The surrounding area is home to reindeer-herding Tsaatan (Dukha) communities.
- The nearest significant town is Khatgal, at the southern tip of Lake Khövsgöl.
- Permits to visit the Tsaatan camps are issued in Tsagaannuur."""
UT = """Uran Togoo — trek into the caldera of a dormant volcano
Location: Uran Togoo Volcano, Bulgan, Bulgan Province, Mongolia (do not place it in any other town, region or country)
Naming: Commonly written 'Uran Togoo' or 'Uran-Togoo'.
Day 3 of the trip
Source itinerary: Journey to Uran Togoo, a dormant volcano. The volcano sits within green steppes and forests."""
MORON = """Moron — stop for lunch
Location: Mörön, Khövsgöl Province, Mongolia (the itinerary calls it Moron) (do not place it in any other town, region or country)
Naming: The city is correctly spelled Mörön (also romanised as Murun or Muren).
Day 4 of the trip
Source itinerary: Stop for lunch near Moron, capital of Khövsgöl province."""
SEED = "\n\n".join([RUINS, SOD, UT, MORON])
BRAND = "Terra Family Expeditions"
CTA = "Ask Terra Family Expeditions to plan this trip for your family."


def _ctx(**kw):
    base = dict(seed=SEED, scope=SCOPE, trip_name=TRIP, brand_name=BRAND, cta=CTA)
    base.update(kw)
    return h.HygieneContext(**base)


# ---------------------------------------------------------------- lexicon

def test_scope_lexicon_flags_trip_and_sibling_places_only():
    lex = h.build_scope_lexicon(SEED, SCOPE, TRIP)
    assert {"gobi", "erdene zuu", "gandan"} <= set(lex.gazetteer)
    text = ("Khövsgöl, Khovsgol, Mörön, Murun, Uran Togoo, Tsaatan and Dukha are all on this route, "
            "and so is Khatgal.")
    assert h.places_named(lex, text) == []
    assert h.places_named(lex, "From the Gobi to Erdene Zuu.") == ["Erdene Zuu", "Gobi"]


def test_romanisation_variant_of_an_own_place_is_not_out_of_scope():
    lex = h.build_scope_lexicon(SEED + "\nOther trips that include this same moment (x):\n- A day at Lake Hovsgol.",
                                SCOPE, None)
    assert "hovsgol" not in lex.gazetteer


def test_find_scope_violations_title_meta_body():
    lex = h.build_scope_lexicon(SEED, SCOPE, TRIP)
    out = h.find_scope_violations(lex, title="Khovsgol and Gobi Tour Mongolia Guide",
                                  meta="A family guide to Khövsgöl and the Gobi.",
                                  body="## Day 3\nThe camp faces the lake. Erdene Zuu is older. [R:a1]")
    assert {(v["where"], v["place"]) for v in out} == {("title", "Gobi"), ("meta", "Gobi"), ("body", "Erdene Zuu")}
    assert next(v for v in out if v["where"] == "body")["sentence"] == "Erdene Zuu is older."


def test_linkedin_style_seed_without_siblings_has_empty_gazetteer():
    seed = ("Tiger's Nest Monastery — hike to the cliffside monastery\nLocation: Paro Taktsang, Paro, Bhutan\n"
            "Source itinerary: Hike to Tiger's Nest above the Paro valley.")
    lex = h.build_scope_lexicon(seed, "SCOPE: this piece is about one moment (day 9): Tiger's Nest Monastery.", None)
    assert lex.gazetteer == {}


# ---------------------------------------------------------------- sentences

def test_sentence_spans_kinds_and_markers():
    text = ("## FAQ\n\n**Q: Is it cold?**\nA: The lake is cold. It freezes in winter. [R:a1]\n\n"
            "- First point. Second point.\n\n#Mongolia #Travel")
    spans = h.sentence_spans(text)
    kinds = [s["kind"] for s in spans]
    assert kinds == ["heading", "faq_q", "faq_a", "faq_a", "bullet", "bullet", "hashtags"]
    first_a = spans[2]
    assert text[first_a["start"]:first_a["end"]] == "The lake is cold."
    assert spans[3]["clean"] == "It freezes in winter." and spans[3]["text"].endswith("[R:a1]")
    assert all(s["in_faq"] for s in spans[:4])


def test_tidy_after_cut_drops_empty_sections_and_answers():
    text = "Intro.\n\n## Empty\n\n## Kept\nBody  text.\n\n## FAQ\n\n**Q: Why?**\nA:\n\n**Q: How?**\nA: Like so.\n"
    assert h.tidy_after_cut(text) == "Intro.\n\n## Kept\nBody text.\n\n## FAQ\n\n**Q: How?**\nA: Like so.\n"


def test_expand_to_sentence():
    text = "One sentence here. Second sentence is longer. Third."
    i = text.find("sentence is")
    assert text[slice(*h.expand_to_sentence(text, i, i + 3))] == "Second sentence is longer."


# ---------------------------------------------------------------- repeats (r1 calibration)

PHO_A = ("The Pho Chhu and the Mo Chhu — the Male River and the Female River — converge at a narrow peninsula "
         "in Punakha, and it is on that peninsula that the dzong was raised.")
PHO_B = ("Punakha Dzong was founded at the confluence of the Pho Chhu and Mo Chhu rivers, and from its "
         "earliest days served as both the administrative and religious centre.")
TEMPLES_A = "The main temple structures were built around that sacred cave in 1692."
TEMPLES_B = ("Four main temples, several sacred caves, and a series of staircases and bridges connect the "
             "structures along the cliff.")


def test_restates_catches_the_r1_repair_repeat_but_not_a_list_echo():
    assert h.restates(PHO_B, PHO_A)  # S233 #14
    assert not h.restates(TEMPLES_B, TEMPLES_A)  # #0/#7/#11 false positive before calibration


def test_brand_words_are_ignored_by_the_repeat_rules():
    a = "Terra Family Expeditions plans journeys like this with real care for active family days."
    b = "Ask Terra Family Expeditions to plan this trip for your family, and start with those questions."
    assert h.restates(b, a)
    assert not h.restates(b, a, h.brand_words(BRAND))


# ---------------------------------------------------------------- autofix

def test_autofix_deletes_borrowed_out_of_scope_sentence_and_keeps_cta():
    body = ("## The ruins\nOn Day 14 the route reaches the ruins of an ancient monastery. [R:r1] "
            "The primary site of Erdene Zuu, Mongolia's oldest Buddhist monastery, dates to the late 16th "
            "century. [R:r1]\n\n" + CTA)
    text, log = h.autofix(body, _ctx())
    assert "Erdene Zuu" not in text and CTA in text
    assert log and log[0].startswith("deleted a sentence borrowed from another trip (names 'Erdene Zuu')")


def test_autofix_deletes_borrowed_leak_sentence():
    body = ("The content seed for this journey notes that thousands of monks lived on the grounds and "
            "contributed significantly to Mongolian culture. The ruins remain.")
    text, log = h.autofix(body, _ctx())
    assert text == "The ruins remain."
    assert "leaks the source notes" in log[0]


def test_autofix_trims_a_non_borrowed_leak_frame():
    text, _ = h.autofix("The content seed describes the lake as cold. The camp is quiet.", _ctx())
    assert text == "The lake is cold. The camp is quiet."
    text, _ = h.autofix("A: The content seed notes that the camp is on the shore.", _ctx())
    assert text == "A: The camp is on the shore."


def test_autofix_social_proof_deleted_only_when_the_seed_does_not_support_it():
    body = "This is the day that tends to sit longest in the mind afterward. Sod Camp faces the lake."
    text, log = h.autofix(body, _ctx())
    assert text == "Sod Camp faces the lake." and "social proof" in log[0]
    supported = "Families tend to stay with the reindeer-herding Tsaatan Dukha communities near Khövsgöl National Park."
    text, _ = h.autofix(supported, _ctx())
    assert text == supported
    assert any(v.startswith("social proof") for v in h.find_issues(text, _ctx())["hygiene"])


def test_autofix_deletes_the_later_near_verbatim_repeat():
    first = ("Optional activities also include boat rides on the water and horseback riding across "
             "surrounding terrain.")
    second = ("Boat rides, horseback riding across the surrounding terrain, and visits to local nomadic "
              "families are all available as optional activities.")
    text, log = h.autofix(f"{first} {second} The lake is deep.", _ctx())
    assert text == f"{first} The lake is deep." and "repeat" in log[0]


def test_autofix_empties_a_section_and_drops_its_heading():
    body = "Intro sentence about the lake.\n\n## Other monasteries\nThe primary site of Erdene Zuu, Mongolia's oldest Buddhist monastery, dates to the late 16th century.\n\n## Lake\nThe lake is deep."
    text, _ = h.autofix(body, _ctx())
    assert "## Other monasteries" not in text and "## Lake" in text


def test_autofix_trip_name_reads_as_this_journey():
    text, log = h.autofix(f"Day 3 through Day 14 of the *{TRIP}* itinerary. The {TRIP} trip starts.", _ctx())
    assert text == "Day 3 through Day 14 of this journey. This journey starts."
    assert log == [f"replaced the trip name '{TRIP}' with 'this journey'"]


def test_trip_name_without_out_of_scope_place_is_kept():
    text, log = h.autofix("A trip called Khövsgöl Lake Family Week starts here.",
                          _ctx(trip_name="Khövsgöl Lake Family Week"))
    assert "Khövsgöl Lake Family Week" in text and log == []


PARO_SEED = ("Paro Taktsang — hike to the cliffside monastery\n"
             "Location: Paro Taktsang, Paro, Bhutan (do not place it in any other town, region or country)\n"
             "Source itinerary: Hike to the Paro Taktsang monastery above the valley.")
PARO_SCOPE = "SCOPE: this piece is about one moment of the trip: Paro Taktsang."


@pytest.mark.parametrize("trip", ["Bhutan Cultural Discovery - 8 Days", "Best of Bhutan Adventure",
                                  "Classic Bhutan Highlights Tour", "Land of the Thunder Dragon — 10 Days"])
def test_marketing_trip_name_adds_no_place_and_is_kept(trip):
    """Review S234: keying the trip name's whole capitalised run made every trip name 'out of
    scope', so autofix replaced it on every channel."""
    lex = h.build_scope_lexicon(PARO_SEED, PARO_SCOPE, trip)
    assert lex.gazetteer == {}
    body = f"We love the {trip} itinerary. The thunder dragon is a symbol."
    ctx = h.HygieneContext(seed=PARO_SEED, scope=PARO_SCOPE, trip_name=trip)
    assert h.autofix(body, ctx) == (body, [])
    assert h.find_issues(body, ctx)["scope"] == []


def test_trip_name_places_are_runs_including_after_a_colon():
    assert h.trip_name_places("Mongolia: Gobi & Khovsgol") == [[("mongolia", "Mongolia")], [("gobi", "Gobi")],
                                                               [("khovsgol", "Khovsgol")]]
    assert h.trip_name_places("Bhutan Highlights: Paro, Punakha and Tiger's Nest — 8 Days") == [
        [("bhutan", "Bhutan")], [("paro", "Paro")], [("punakha", "Punakha")], [("tiger", "Tiger"), ("nest", "Nest")]]
    assert h.trip_name_places("Mongolia Gobi and Khovsgol Family Adventure") == [
        [("mongolia", "Mongolia"), ("gobi", "Gobi")], [("khovsgol", "Khovsgol")]]


@pytest.mark.parametrize("trip", ["Mongolia: Gobi & Khovsgol", "Mongolia Gobi and Khovsgol Family Adventure"])
def test_bare_gobi_is_flagged_for_a_khovsgol_piece(trip):
    lex = h.build_scope_lexicon(SEED, SCOPE, trip)
    assert lex.cap_only == {"gobi"} and "gobi" in lex.gazetteer
    assert h.places_named(lex, "The title says Gobi.") == ["Gobi"]
    # a trip-name word counts only when written as a name
    assert h.places_named(lex, "the gobi-style dunes") == []
    ctx = _ctx(trip_name=trip)
    text, log = h.autofix(f"The {trip} itinerary starts at Sod Camp.", ctx)
    assert text == "This journey starts at Sod Camp." and log[0].startswith("replaced the trip name")


PUNAKHA_SEED = ("Punakha Dzong — visit the fortress\n"
                "Location: Punakha Dzong, Punakha, Bhutan (do not place it in any other town, region or country)\n"
                "Source itinerary: Cross the cantilever bridge to the dzong at the confluence of two rivers.\n"
                "Other trips that include this same moment (for context only — never promise these details for this trip):\n"
                "- Visit Erdene-Zuu on the way.")


def test_places_named_ignores_apostrophes_hyphens_and_colon_starts():
    lex = h.build_scope_lexicon(PUNAKHA_SEED, None, "Bhutan Highlights: Paro, Punakha and Tiger's Nest — 8 Days")
    assert set(lex.gazetteer) == {"paro", "tiger nest", "erdene zuu"}
    assert h.places_named(lex, "Later the family hikes to Tiger's Nest.") == ["Tiger Nest"]
    assert h.places_named(lex, "Later the family hikes to Tiger’s Nest.") == ["Tiger Nest"]
    assert h.places_named(lex, "Erdene-Zuu was built long ago.") == ["Erdene-Zuu"]
    assert h.places_named(lex, "Erdene Zuu was built long ago.") == ["Erdene-Zuu"]
    assert h.places_named(lex, "Paro is an hour away.") == ["Paro"]
    assert h.names_place("tigers nest paro hike", "paro") and not h.names_place("paros", "paro")


TN_SEED = ("Tiger's Nest Monastery — hike to the cliffside monastery\n"
           "Location: Paro Taktsang, Paro, Bhutan (do not place it in any other town, region or country)\n"
           "Source itinerary: Hike to Tiger's Nest, the Paro Taktsang monastery, above the Paro valley.")


def test_hashtag_misspelling_fixed_plural_and_brand_untouched():
    ctx = h.HygieneContext(seed=TN_SEED, brand_name="Trail Pulse")
    text = "A hike.\n\n#PakoTaktsang #PajoTaktsang #TigersNest #TrailPulse #Bhutan"
    fixed, log = h.autofix(text, ctx)
    assert fixed.endswith("#ParoTaktsang #ParoTaktsang #TigersNest #TrailPulse #Bhutan")
    assert len(log) == 2


def test_hashtag_cap_and_last_line_only_with_a_channel_spec():
    ctx = h.HygieneContext(seed=TN_SEED, hashtags_max=3)
    text = "#Bhutan #Hiking\nThe hike is steep.\n\n#Family #Paro"
    fixed, log = h.autofix(text, ctx)
    assert fixed == "The hike is steep.\n\n#Bhutan #Hiking #Family"
    assert log == ["kept 3 of 4 hashtags (channel cap 3)"]
    assert h.autofix(text, h.HygieneContext(seed=TN_SEED))[0] == text


def test_first_line_split_only_with_a_channel_spec():
    head = "The hike is steep. " + "It climbs through pine forest to a monastery on a cliff above the valley floor " * 2
    ctx = h.HygieneContext(seed=TN_SEED, first_line_max=140)
    fixed, log = h.autofix(head.strip() + "\nSecond line.", ctx)
    assert fixed.startswith("The hike is steep.\n\nIt climbs") and log
    assert h.autofix(head.strip(), h.HygieneContext(seed=TN_SEED))[0] == head.strip()


def test_find_issues_quotes_sentences_in_the_repairable_shape():
    found = h.find_issues("The Gobi is far. The content seed describes many things in detail here.",
                          _ctx(), seo_title="Khovsgol and Gobi Guide")
    assert found["scope"][0].startswith("title names 'Gobi'")
    body = found["scope"][1]
    assert sr._BRAND_QUOTE_RE.search(body).group(2) == "The Gobi is far."
    assert any("leaks the source-notes wording" in v for v in found["leak"])  # S234 round 2: F13_leak


# ---------------------------------------------------------------- sentence_repair

def test_splice_guard_turns_a_repeating_replacement_into_a_delete():
    text = f"{PHO_A} The courtyard is wide. A wrong sentence about rafting at dawn here. Book now."
    items = sr.plan(text, [{"gate": "F11_brand_name", "passed": False, "blocking": True,
                            "violations": ["names x: 'A wrong sentence about rafting at dawn here.'"]}])
    out = sr.splice(text, items, [PHO_B])
    assert out == f"{PHO_A} The courtyard is wide. Book now."
    assert items[0]["guard"] == "repeat"
    items = sr.plan(text, [{"gate": "F11_brand_name", "passed": False, "blocking": True,
                            "violations": ["names x: 'A wrong sentence about rafting at dawn here.'"]}])
    distinct = "The dzong hosts a festival in spring with masked dances in the courtyard."
    assert sr.splice(text, items, [distinct]) == f"{PHO_A} The courtyard is wide. {distinct} Book now."


def test_plan_parses_f12_body_violations_and_skips_title():
    body = "The camp is quiet. The Gobi lies far to the south of here. Book now."
    gate = {"gate": "F12_scope", "passed": False, "blocking": True, "violations": [
        "title names 'Gobi', outside this piece's scope: 'Gobi Guide'",
        "names 'Gobi', outside this piece's scope: 'The Gobi lies far to the south of here.'"]}
    items = sr.plan(body, [gate])
    assert [i["sentence"] for i in items] == ["The Gobi lies far to the south of here."]
    assert items[0]["problem"].startswith("names 'Gobi'")


def test_plan_title_meta_only_f12_needs_no_text_repair_and_no_rewrite():
    """Review S234: a blocking F12 whose violations are all in the title/meta is the SEO fit's —
    plan returns [] (no rewrite call, attempt 3 still allowed), not None (full rewrite)."""
    f12 = {"gate": "F12_scope", "passed": False, "blocking": True, "violations": [
        "title names 'Gobi', outside this piece's scope: 'Gobi Guide'",
        "meta names 'Gobi', outside this piece's scope: 'A guide to the Gobi.'"]}
    f4 = {"gate": "F4_seo_surface", "passed": False, "blocking": True, "violations": ["title too short"]}
    assert sr.plan("The lake is cold.", [f12]) == []
    assert sr.plan("The lake is cold.", [f12, f4]) == []
    assert sr.plan("The lake is cold.", [f4]) == []  # AA-776: F4 alone is the fit's too (was None)
    f1 = {"gate": "F3_structure", "passed": False, "blocking": True, "violations": ["no H2"]}
    assert sr.plan("The lake is cold.", [f12, f1]) is None  # anything not repairable still rewrites
    assert sr.plan("The lake is cold.", [{**f12, "blocking": False}]) is None


def test_neighbours_gives_two_each_side():
    text = "One a b c. Two a b c. Three a b c. Four a b c. Five a b c."
    i = text.find("Three")
    item = {"start": i, "end": i + len("Three a b c.")}
    assert sr.neighbours(text, item) == ["One a b c.", "Two a b c.", "Four a b c.", "Five a b c."]


def test_sentence_repair_reexports_primitives():
    assert sr.tidy_after_cut is h.tidy_after_cut and sr.expand_to_sentence is h.expand_to_sentence


# ---------------------------------------------------------------- FAQ

FAQ_BODY = ("Body about the lake and the camp.\n\n## FAQ\n\n"
            "**Q: What age of children is this route best suited to?**\nA: Ages 7 to 16.\n\n"
            "**Q: How physically demanding is the trekking?**\nA: Moderate.\n\n"
            "**Q: Do families need permits to visit the Tsaatan?**\nA: Yes, issued in Tsagaannuur.\n\n"
            "**Q: What does the tour price typically reflect?**\nA: Private guiding.\n\n"
            "**Q: Where does the route start?**\nA: At Sod Camp.\n")


def test_drop_unanswerable_faq_keeps_supported_pairs():
    own, _ = h.split_seed(SEED)
    text, log = qg.drop_unanswerable_faq(FAQ_BODY, own)
    assert "age of children" not in text and "physically demanding" not in text and "price" not in text
    assert "permits to visit the Tsaatan" in text and "Where does the route start" in text
    assert len(log) == 3 and "(age)" in log[0]


def test_audience_line_never_counts_as_support():
    own, _ = h.split_seed(SEED)
    grounding = own + "\n\nAUDIENCE (who the brand writes for — not a fact about this trip): families with children aged 7 to 16"
    assert qg.unanswerable_faq_topics("What age range is it for?", own) == ["age"]
    # the caller must pass the own seed lines; the grounding text would wrongly support it
    assert qg.unanswerable_faq_topics("What age range is it for?", grounding) == []


def test_lone_answerable_pair_is_kept():
    """S234 round 2 fix — the FAQ is required (F16_faq), so one answerable pair stays for the
    contract step; only a FAQ with no answerable pair loses its heading."""
    body = ("Body.\n\n## FAQ\n\n**Q: Is it suitable for kids?**\nA: Yes.\n\n"
            "**Q: Do families need permits to visit the Tsaatan?**\nA: Yes.\n")
    own, _ = h.split_seed(SEED)
    text, log = qg.drop_unanswerable_faq(body, own)
    assert "suitable for kids" not in text and "permits to visit the Tsaatan" in text and len(log) == 1
    only_bad = "Body.\n\n## FAQ\n\n**Q: Is it suitable for kids?**\nA: Yes.\n"
    assert qg.drop_unanswerable_faq(only_bad, own)[0] == "Body.\n"


PUNAKHA_OWN = ("Punakha Dzong — visit the fortress\nLocation: Punakha Dzong, Punakha, Bhutan\n"
               "Source itinerary: Cross the cantilever bridge to the dzong at the confluence. Monks live here.")


@pytest.mark.parametrize("q", [
    "What will kids see at the reindeer camp?", "How do children ride the horses?",
    "What will children notice first at the dzong?", "How do kids cross the cantilever bridge?",
    "What does the visit include?", "What is included in the horse ride?",
    "When should families arrive at the dzong?", "Why do the monks fit so many rooms inside?",
])
def test_faq_question_naming_children_or_include_is_not_a_topic(q):
    """Review S234: a family brand names children in most questions — only age suitability,
    effort, price/inclusions and timing wording are topics."""
    assert qg.unanswerable_faq_topics(q, PUNAKHA_OWN) == []


@pytest.mark.parametrize("q,topic", [
    ("What age range is this route typically suited to?", "age"),
    ("Is this itinerary suitable for younger children as well as teenagers?", "age"),
    ("How old do kids need to be for the hike?", "age"),
    ("Is the dzong safe for young children?", "age"),
    ("How fit do families need to be?", "fitness"),
    ("Is the climb challenging?", "fitness"),
    ("What is included in the tour price?", "price"),
    ("What does the trip include?", "price"),
    ("When is the best time to visit?", "best_time"),
])
def test_faq_topic_questions_still_dropped_without_support(q, topic):
    assert qg.unanswerable_faq_topics(q, PUNAKHA_OWN) == [topic]


def test_family_faq_survives_when_the_seed_answers_it():
    body = ("Body.\n\n## FAQ\n\n**Q: What will kids see at the dzong?**\nA: The cantilever bridge.\n\n"
            "**Q: How do children cross the bridge?**\nA: On foot.\n\n"
            "**Q: Who lives in the dzong?**\nA: Monks.\n")
    assert qg.drop_unanswerable_faq(body, PUNAKHA_OWN) == (body, [])


def test_faq_untouched_when_all_answerable():
    body = "Body.\n\n## FAQ\n\n**Q: Where is the camp?**\nA: On the shore.\n"
    assert qg.drop_unanswerable_faq(body, "x") == (body, [])


# ---------------------------------------------------------------- F12 / F13 gates

def test_f12_warns_and_f13_reports_residue():
    ctx = _ctx()
    f12 = qg.gate_scope("The Gobi is far from here.", ctx, "Gobi and Khovsgol Guide", None)
    assert not f12["passed"] and f12["blocking"] is qg.F12_SCOPE_BLOCKING is False and f12["repairable"]
    f13 = qg.gate_hygiene("Most families say the lake is the best part of the trip for them.", ctx)
    assert not f13["passed"] and not f13["blocking"]
    # S234 round 2 — a source-notes leak is F13_leak now: blocking, sentence-repairable
    leak = qg.gate_leak("The content seed describes many things in great detail here.", ctx)
    assert not leak["passed"] and leak["blocking"] and leak["repairable"]
    assert qg.gate_scope("The Gobi is far.", None)["passed"] and qg.gate_hygiene("x", None)["passed"]


def test_run_quality_gates_with_hygiene_context_never_holds_on_f12():
    from unittest.mock import patch
    f9 = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
          "failure_codes": [], "flagged_phrases": [], "notes": ""}

    def raw(**d):
        import json
        return {"text": json.dumps(d)}

    with patch.object(qg, "invoke_judge", side_effect=[raw(**f9)]):
        out = qg.run_quality_gates(content_text="Sod Camp faces the Gobi. Book now.", atom_text=SEED, cta="Book now",
                                   brand_rubric_text="r", channel="linkedin",
                                   hygiene_ctx=_ctx(cta="Book now"))
    f12 = next(g for g in out["gate_ledger"] if g["gate"] == "F12_scope")
    assert not f12["passed"] and f12 in out["flags"]
    # AA-780 — the 7-word LinkedIn post is under the 120-word floor: F14 blocks on attempt 1 (one rewrite,
    # never a hold); F12 is still only a flag
    assert out["first_failure"]["gate"] == "F14_social_shape"
    assert qg.hold_failure(out["gate_ledger"]) is None


def test_lexicon_regexes_are_public():
    assert h.LEAK_RE.search("The content seed says so") and h.SOCIAL_PROOF_RE.search("it is popular with families")
    assert not h.SOCIAL_PROOF_RE.search("the trail is rated moderate") and not h.SOCIAL_PROOF_RE.search("it varies")
    assert isinstance(h.COMMON_CAPS, frozenset) and re.compile(h.LEAK_RE.pattern)


@pytest.mark.parametrize("w", ["Khövsgöl", "KHÖVSGÖL"])
def test_fold(w):
    assert h.fold(w) == "khovsgol"
