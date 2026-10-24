"""AA-917 WM-11 / WM-12 — the fact filter, the masking and the rule lint (pure functions, fixtures only: the repo is
public, no real tenant text).

WM-11 Done-when: "lunch" -> "lunch included" fails; pure style edits pass.
WM-12 Done-when: the lint rejects "include X".
"""
import pytest

from services.writing_memory import fact_filter as ff
from services.writing_memory import lint as L
from services.writing_memory.masking import contains_phrase, excerpt, mask, removed_phrases


# ── fact filter ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_lunch_to_lunch_included_fails():
    v = ff.classify("We stop for lunch by the lake.", "We stop for lunch included by the lake.", "Day 2: lunch stop.")
    assert v.status == ff.FAIL and v.code == ff.ADDS_UNSOURCED_CLAIM and "included" in v.reasons[0]


def test_inclusion_in_the_source_is_not_new():
    v = ff.classify("We stop for lunch.", "Lunch is included.", "Meals: lunch included on day 2.")
    assert v.status == ff.PASS


@pytest.mark.parametrize("before,after", [
    ("The temple is nestled in lush green hills.", "The temple sits in green hills."),
    ("A truly unforgettable journey awaits you.", "The trip takes you through the valley."),
    ("Discover the vibrant markets.", "Walk through the markets."),
])
def test_pure_style_edits_pass(before, after):
    assert ff.classify(before, after, "").status == ff.PASS


@pytest.mark.parametrize("after", ["We stop for lunch at 12:30.", "We stop for lunch for 2 hours.",
                                   "We stop for lunch, transfer by private car."])
def test_added_number_time_or_promise_fails(after):
    assert ff.classify("We stop for lunch.", after, "").status == ff.FAIL


def test_a_number_from_the_source_passes():
    assert ff.classify("We walk to the lake.", "We walk 3 km to the lake.", "A 3 km walk to the lake.").status \
        == ff.PASS


def test_added_name_is_grey():
    v = ff.classify("We visit the temple.", "We visit the temple near Lantern Bridge.", "the temple")
    assert v.status == ff.GREY and v.code == ff.ADDS_NAME


def test_cut_and_reason_only_pass_admin_fact_not_in_source_fails():
    assert ff.classify("A sentence.", "", "").status == ff.PASS
    assert ff.classify(None, None, "").status == ff.PASS
    v = ff.classify("Old.", "New.", "", reason_code="fact_not_in_source")
    assert v.status == ff.FAIL and v.code == "admin_fact_not_in_source"


# ── masking ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_mask_hides_specifics_and_names_but_not_sentence_starts():
    m = mask("Visit Lantern Bridge at 7:30am for $25 over 3 days with Fable Tours. Lunch is simple.")
    assert m == "Visit [NAME] at [TIME] for [PRICE] over [N] days with [NAME]. Lunch is simple."
    assert mask(m) == m
    # a sentence-initial name is masked when it recurs mid-sentence; an ordinary opener stays
    assert mask("Lantern is busy. We walk to Lantern at dusk.") == "[NAME] is busy. We walk to [NAME] at dusk."
    assert mask("Discover the market.") == "Discover the market."


def test_excerpt_keeps_the_change_with_context():
    before = " ".join(["word"] * 40) + " truly breathtaking views " + " ".join(["end"] * 40)
    after = " ".join(["word"] * 40) + " views " + " ".join(["end"] * 40)
    b, a = excerpt(before, after, context=3)
    assert "truly breathtaking" in b and "truly" not in a and len(b.split()) < 15
    assert excerpt("same", "same") == ("", "")


def test_removed_phrases_and_contains_phrase():
    assert {"nestled", "lush"} <= removed_phrases("The temple is nestled in lush green hills.",
                                                 "The temple sits in green hills.")
    assert removed_phrases("Keep it.", "Keep it.") == set()
    assert contains_phrase("A village nestled in hills", "nestled") and not contains_phrase("A nest", "nestled")
    assert contains_phrase("Meet at Lantern Bridge.", "at [NAME]")


# ── lint ────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rule", ["Include the lunch in every itinerary.", "Mention the free pick-up.",
                                  "Always highlight the sunset view.", "Cut stock openers; add a hotel name.",
                                  "Promise a guide on every day."])
def test_lint_rejects_rules_that_add_content(rule):
    assert not L.constraint_only(rule)["ok"]


@pytest.mark.parametrize("rule", ["Do not mention breakfast unless the source does.", "Never promise a view.",
                                  "Cut sentences that mention free pick-up.", "Replace 'nestled in' with 'in'.",
                                  "Avoid opening with a rhetorical question."])
def test_lint_accepts_constraints(rule):
    assert L.constraint_only(rule)["ok"]


def test_lexicon_replacement_may_add_no_specific():
    assert not L.lexicon_specifics("lunch", "lunch included")["ok"]
    assert not L.lexicon_specifics("a walk", "a 3 km walk")["ok"]
    assert not L.lexicon_specifics("the bridge", "the [NAME] bridge")["ok"]
    assert L.lexicon_specifics("nestled in", "in")["ok"]
    assert L.lexicon_specifics("breathtaking", None)["ok"]          # a ban


def test_five_gram_overlap_and_names():
    corpus = L.ngrams(mask("Our riverside lodge serves warm noodle soup at dawn every morning."))
    hit = L.five_gram_overlap(["Do not write serves warm noodle soup at dawn."], corpus, [])
    assert not hit["ok"] and "noodle" in hit["detail"]
    hashed = {hash(g) for g in corpus}                               # the miner keeps hashes
    assert not L.five_gram_overlap(["Do not write serves warm noodle soup at dawn."], hashed, [])["ok"]
    assert L.five_gram_overlap(["Cut stock openers from every post."], corpus, [])["ok"]
    assert not L.five_gram_overlap(["Never call it mekong dreams travel."], set(), ["Mekong Dreams"])["ok"]


def test_predict_needs_two_explained_held_out_edits():
    held = [{"before_text": "A village nestled in hills.", "after_text": "A village in hills."},
            {"before_text": "Nestled by the river.", "after_text": "By the river."},
            {"before_text": "Plain text.", "after_text": "Other text."}]
    assert L.predict(["nestled"], held)["ok"]
    assert not L.predict(["nestled"], held[1:])["ok"]
    assert not L.predict(["nowhere"], held)["ok"]


def test_run_lint_shape_and_overall():
    held = [{"before_text": "Truly breathtaking views.", "after_text": "Views."}] * 2
    ok = L.run_lint(kind="lexicon", rule_text="breathtaking", replacement=None, match_phrases=["breathtaking"],
                    scope="tenant", held_out=held, corpus_ngrams=set(), names=[])
    assert ok["ok"] and ok["scope"] == "tenant"
    long_rule = " ".join(["cut"] * 26)
    bad = L.run_lint(kind="writer_instruction", rule_text=long_rule, replacement=None, match_phrases=["x"],
                     scope="platform", held_out=held, corpus_ngrams=set(), names=[])
    assert not bad["ok"] and not bad["shape"]["ok"] and not bad["predict"]["ok"]
