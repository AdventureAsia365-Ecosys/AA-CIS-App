"""AA-738 — deterministic forbidden-word strip on the writer output (no LLM).

Fixtures are the real sentences that sent 9 S217 Nepal tours to review with
"retried with sonnet, still FORBIDDEN_WORD" (all AA brand-list words, none from the core list).
"""
from services.content_generation.forbidden_strip import strip_forbidden
from services.content_generation.forbidden_words import all_forbidden, has_word

# A subset of shared.tenant_brand_rules 'default' v2 forbidden_words (the AA brand list).
AA_BRAND = ["deals", "cheap", "book now", "stunning", "iconic", "fun", "discover", "explore",
            "package", "vibrant", "nestled", "embark on", "act fast"]


def _clean(value, words=AA_BRAND) -> bool:
    text = value if isinstance(value, str) else " ".join(value)
    return not any(has_word(text, w) for w in all_forbidden(words))


def test_real_nepal_sentences_are_substituted():
    g = {
        "summary": "Annapurna Conservation Area permits and TIMS cards are included in the package.",
        "itineraries": (
            "Day 1 — Arrival in Kathmandu\n"
            "Check in and settle in for your first night in Nepal's vibrant capital.\n\n"
            "Day 2 — Kathmandu\n"
            "Rest at the hotel, explore the city at your own pace, or attend to personal needs. "
            "Namche is a hub, nestled in a bowl-shaped valley."
        ),
    }
    out, rep = strip_forbidden(g, AA_BRAND)
    assert out["summary"] == "Annapurna Conservation Area permits and TIMS cards are included in the trip."
    assert "Nepal's lively capital." in out["itineraries"]
    assert "visit the city at your own pace" in out["itineraries"]
    assert "set in a bowl-shaped valley" in out["itineraries"]
    assert _clean(out["summary"]) and _clean(out["itineraries"])
    assert rep["unresolved"] == [] and rep["dropped"] == []


def test_explore_intransitive_becomes_wander_and_keeps_case_and_plural():
    out, _ = strip_forbidden({"itineraries": "Day 3 — Pokhara\nRest, explore, or shop. "
                                             "Explore the lakeside. She explores further."}, AA_BRAND)
    it = out["itineraries"]
    assert "Rest, wander, or shop." in it
    assert "Visit the lakeside." in it
    assert "She wanders further." in it


def test_article_is_fixed_after_substitution():
    out, _ = strip_forbidden({"summary": "A pristine lake below an iconic peak."}, [])
    # core-list word "pristine" -> "unspoiled" needs "An"; brand list not passed for "iconic"
    assert out["summary"].startswith("An unspoiled lake")
    out, _ = strip_forbidden({"summary": "It sits below an iconic peak."}, AA_BRAND)
    assert out["summary"] == "It sits below a well-known peak."


def test_word_without_substitute_drops_the_sentence_in_prose():
    out, rep = strip_forbidden({"summary": "A ten-day trek through the Khumbu. Book now for spring."},
                               AA_BRAND)
    assert out["summary"] == "A ten-day trek through the Khumbu."
    assert ("summary", "book now") in rep["dropped"]


def test_day_marker_sentence_is_never_dropped():
    g = {"itineraries": "Day 1: Book now and arrive in Kathmandu.\nDay 2: Fly to Lukla."}
    out, rep = strip_forbidden(g, AA_BRAND)
    assert out["itineraries"] == g["itineraries"]  # left for the AA-736 gate
    assert ("itineraries", "book now") in rep["unresolved"]


def test_paragraph_left_empty_is_unresolved():
    out, rep = strip_forbidden({"summary": "Book now."}, AA_BRAND)
    assert out["summary"] == "Book now."
    assert rep["unresolved"] == [("summary", "book now")]


def test_short_fields_are_substitution_only():
    out, rep = strip_forbidden({"name": "Fun Days in Pokhara", "subtitle": "Nestled lakes"}, AA_BRAND)
    assert out["subtitle"] == "Set lakes"
    assert out["name"] == "Fun Days in Pokhara"  # "fun" has no safe substitute
    assert ("name", "fun") in rep["unresolved"]


def test_highlights_substitute_then_drop_item_if_three_remain():
    hl = ["A Gurung village nestled in forest", "A fun day", "Lake view", "Monastery visit", "Sunrise"]
    out, rep = strip_forbidden({"highlights": hl}, AA_BRAND)
    assert out["highlights"] == ["A Gurung village set in forest", "Lake view", "Monastery visit", "Sunrise"]
    assert ("highlights", "fun") in rep["dropped"]


def test_highlights_item_kept_when_dropping_would_leave_too_few():
    hl = ["A fun day", "Lake view", "Monastery visit"]
    out, rep = strip_forbidden({"highlights": hl}, AA_BRAND)
    assert out["highlights"] == hl
    assert ("highlights", "fun") in rep["unresolved"]


def test_substitute_that_is_itself_forbidden_is_not_used():
    # A brand that also forbids "visit" — "explore" must not become "visit".
    out, rep = strip_forbidden({"summary": "Trekkers explore the valley. It is quiet."},
                               ["explore", "visit"])
    assert out["summary"] == "It is quiet."
    assert ("summary", "explore") in rep["dropped"]


def test_seo_keywords_used_is_never_rewritten():
    g = {"seo_keywords_used": ["nepal trek package"], "summary": "A quiet trek."}
    out, rep = strip_forbidden(g, AA_BRAND)
    assert out["seo_keywords_used"] == ["nepal trek package"]
    assert rep == {"replaced": [], "dropped": [], "unresolved": []}


_META = ("Trek the Annapurna region with permits, guides and lodges arranged in one package, "
         "plus flights between Kathmandu and Pokhara on {}trip.")


def test_seo_meta_substitution_kept_when_still_in_band():
    meta = _META.format("this one ")  # 143 chars; "package" -> "trip" = 140, still in band
    assert len(meta) == 143
    out, rep = strip_forbidden({"seo_meta": meta}, AA_BRAND)
    assert out["seo_meta"] == _META.format("this one ").replace("package", "trip")
    assert ("seo_meta", "package") in rep["replaced"]


def test_seo_meta_substitution_rejected_when_it_breaks_the_band():
    meta = _META.format("a set ")  # 140 chars; "package" -> "trip" = 137, out of band -> keep original
    assert len(meta) == 140
    out, rep = strip_forbidden({"seo_meta": meta}, AA_BRAND)
    assert out["seo_meta"] == meta
    assert ("seo_meta", "package") in rep["unresolved"]
    assert ("seo_meta", "package") not in rep["replaced"]


def test_input_is_not_mutated():
    g = {"summary": "Included in the package."}
    strip_forbidden(g, AA_BRAND)
    assert g == {"summary": "Included in the package."}


def test_graph_helper_cleans_generated_in_place():
    from services.content_generation.graph import _apply_forbidden_strip
    gen = {"name": "Mardi Himal Trek", "summary": "Rest or explore Pokhara."}
    _apply_forbidden_strip(gen, {"brand_forbidden_words": AA_BRAND, "tour_id": "t-1"})
    assert gen["summary"] == "Rest or visit Pokhara."


def test_revalidate_strips_words_a_repair_reintroduced(monkeypatch):
    # S218 Sri Lanka: flag_fix rewrote seo_meta back to "Explore ..." after the write-time strip.
    from services.content_generation import graph
    seen = {}

    def fake_validate(state):
        seen["meta"] = state["generated"]["seo_meta"]
        return {**state, "quality_score": 8.0, "failure_codes": []}

    monkeypatch.setattr(graph, "validate_node", fake_validate)
    monkeypatch.setattr(graph, "judge_node", lambda s: s)
    monkeypatch.setattr(graph, "_apply_grounding_recheck", lambda s: s)
    state = {"fix_pass_applied": True, "brand_forbidden_words": AA_BRAND, "tour": {"name": "T"},
             "generated": {"seo_meta": "Explore the temples of Kandy and the tea hills of Ella."}}
    out = graph.revalidate_node(state)
    assert seen["meta"] == "Visit the temples of Kandy and the tea hills of Ella."
    assert out["revalidate_passed"] is True
