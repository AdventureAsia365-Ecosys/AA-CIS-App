"""AA-778 (S238) — em-dash: the deterministic hygiene step (hygiene.fix_dashes, run inside autofix), the
F2 residue guard and the admin sentence clean-up. Sentences are real ones from the S234 r4
(.tmp-session/s234/pieces_r4.json), S235 r5 (.tmp-session/s235/r5/pieces_r5.json) and S233/S234 r1-r3
pieces; the offline check over every r4 / r5 body is in docs/implementation-notes/AA-778-offline-check.md."""
import json
from unittest.mock import patch

import pytest

from services.acp_content_writing import authenticity
from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import trip_facts as tf

EM, EN = "—", "–"
SEED = (f"Uran Togoo {EM} trek into the caldera of a dormant volcano\n"
        "Location: Uran Togoo Volcano, Bulgan Province, Mongolia\n"
        f"Source itinerary: Journey to Uran Togoo, a dormant volcano. Its formal name is \"Uran Togoo {EM} "
        "Tulga Uul\".")
TRIP = f"Ho Chi Minh Trail {EM} 14 Days"


def _ctx(**kw):
    return h.HygieneContext(**{"seed": SEED, "trip_name": TRIP, **kw})


def fix(text, ctx=None):
    return h.fix_dashes(text, ctx)[0]


# ---------------------------------------------------------------- pairs

@pytest.mark.parametrize("before, after", [
    (f"Mörön {EM} the capital of Khövsgöl Province and the main gateway for travellers heading north {EM} serves "
     "as a practical midpoint on day four.",
     "Mörön, the capital of Khövsgöl Province and the main gateway for travellers heading north, serves as a "
     "practical midpoint on day four."),
    # X has its own commas: brackets, not a run of commas
    (f"Knowing these details in advance {EM} what to leave behind, how to dress, the monastery's history {EM} "
     "helps families arrive prepared rather than caught out.",
     "Knowing these details in advance (what to leave behind, how to dress, the monastery's history) helps "
     "families arrive prepared rather than caught out."),
    # 'and' after the pair: commas would read as three people
    (f"The Je Khenpo {EM} the Chief Abbot of Bhutan {EM} and the Central Monk Body still relocate here from "
     "Thimphu each winter.",
     "The Je Khenpo (the Chief Abbot of Bhutan) and the Central Monk Body still relocate here from Thimphu each "
     "winter."),
    (f"Days spent here can include time on the water, along the shoreline, and {EM} optionally {EM} in the "
     "company of local nomadic families.",
     "Days spent here can include time on the water, along the shoreline, and, optionally, in the company of "
     "local nomadic families."),
    # unspaced em-dashes are dashes too
    (f"Punakha Dzong sits on a peninsula between two rivers{EM}the Pho Chhu and Mo Chhu{EM}a choice of ground "
     "with meaning.",
     "Punakha Dzong sits on a peninsula between two rivers, the Pho Chhu and Mo Chhu, a choice of ground with "
     "meaning."),
])
def test_pair_is_a_parenthesis(before, after):
    assert fix(before) == after


# ---------------------------------------------------------------- a single dash

@pytest.mark.parametrize("before, after", [
    # a noun phrase: colon
    (f"The road north passes through Mörön, capital of Khövsgöl Province {EM} the northernmost aimag of Mongolia.",
     "The road north passes through Mörön, capital of Khövsgöl Province: the northernmost aimag of Mongolia."),
    (f"It holds an estimated 70 percent of Mongolia's fresh water {EM} figures that give the landscape a weight "
     "well beyond the merely scenic.",
     "It holds an estimated 70 percent of Mongolia's fresh water: figures that give the landscape a weight well "
     "beyond the merely scenic."),
    # a clause of its own after a clause: full stop, capitalised
    (f"Mörön is not the destination {EM} it is the frame.", "Mörön is not the destination. It is the frame."),
    (f"These aren't inconveniences {EM} they're the shape of the place itself.",
     "These aren't inconveniences. They're the shape of the place itself."),
    (f"Unlike many reindeer-herding traditions elsewhere, the Tsaatan do not raise their reindeer primarily for "
     f"meat {EM} instead, the animals are used for riding.",
     "Unlike many reindeer-herding traditions elsewhere, the Tsaatan do not raise their reindeer primarily for "
     "meat. Instead, the animals are used for riding."),
    # X continues the sentence: comma
    (f"This is the section of the journey that requires the most patience {EM} and it repays both.",
     "This is the section of the journey that requires the most patience, and it repays both."),
    (f"Planning a trip well means building in rest before you need it {EM} not scrambling to add it later.",
     "Planning a trip well means building in rest before you need it, not scrambling to add it later."),
    (f"Summers on the lake are short and cool {EM} which is worth knowing for families planning time on the water.",
     "Summers on the lake are short and cool, which is worth knowing for families planning time on the water."),
    (f"The caldera floor is accessible on foot {EM} meaning families can actually walk down into it.",
     "The caldera floor is accessible on foot, meaning families can actually walk down into it."),
    (f"Their settlements are seasonal camps called urts {EM} moved several times a year as the reindeer graze.",
     "Their settlements are seasonal camps called urts, moved several times a year as the reindeer graze."),
])
def test_single_dash(before, after):
    assert fix(before) == after


@pytest.mark.parametrize("before, after", [
    # no finite verb before the dash: a full stop would leave the list as a fragment
    (f"The courtyards, the utse at the centre, the temples with their elaborate woodcarving and gilded roofs {EM} "
     "these are not decorative choices.",
     "The courtyards, the utse at the centre, the temples with their elaborate woodcarving and gilded roofs: "
     "these are not decorative choices."),
    # a noun with a reduced relative ('the moment the route has …') is not a clause
    (f"The journey then continues toward Day 14 and the monastery ruins {EM} the moment the route has, in a quiet "
     "way, been building toward.",
     "The journey then continues toward Day 14 and the monastery ruins: the moment the route has, in a quiet "
     "way, been building toward."),
    # a gerund subject: a comma would splice two clauses
    (f"At Tiger's Nest, cameras stay at the checkpoint {EM} knowing the rules ahead makes the visit smoother.",
     "At Tiger's Nest, cameras stay at the checkpoint: knowing the rules ahead makes the visit smoother."),
    # a 'what …, what …' list is not a clause
    (f"The quality of the experience depends on how the journey is structured {EM} what is given time, what is "
     "approached with the right guidance.",
     "The quality of the experience depends on how the journey is structured: what is given time, what is "
     "approached with the right guidance."),
    # an -ing noun in a list keeps the colon
    (f"Terra plans trips where days like this are thought through from the start {EM} pacing, cultural "
     "expectations, and all.",
     "Terra plans trips where days like this are thought through from the start: pacing, cultural expectations, "
     "and all."),
])
def test_never_a_fragment_or_a_comma_splice(before, after):
    assert fix(before) == after


def test_a_sentence_that_already_has_a_colon_gets_a_comma():
    assert fix(f"One rule: the camp sits by the lake {EM} a short walk from the shore.") == \
        "One rule: the camp sits by the lake, a short walk from the shore."


def test_heading_faq_and_bullet():
    text = (f"## Day 3 {EM} Uran Togoo\n\nThe crater is wide.\n\n- **Boat rides** {EM} a slow way to see the lake\n\n"
            f"## FAQ\n\n**Q: Is the crater {EM} the caldera {EM} open to walkers?**\n"
            f"A: Yes {EM} the caldera floor is accessible on foot.")
    assert fix(text) == ("## Day 3: Uran Togoo\n\nThe crater is wide.\n\n"
                         "- **Boat rides**: a slow way to see the lake\n\n"
                         "## FAQ\n\n**Q: Is the crater, the caldera, open to walkers?**\n"
                         "A: Yes. The caldera floor is accessible on foot.")


def test_tags_stay_and_the_sentence_still_ends():
    text = (f"It holds 70 percent of the country's fresh water [R:a1] {EM} figures that give the lake its weight. "
            "[R:a1] The next sentence.")
    assert fix(text) == ("It holds 70 percent of the country's fresh water [R:a1]: figures that give the lake its "
                         "weight. [R:a1] The next sentence.")


# ---------------------------------------------------------------- never touched

def test_en_dash_ranges_and_hyphens_are_not_dashes():
    text = "A 3–5 day trek in 1637–1638 near Uran Togoo-Tulga Uul, a well-known site."
    assert h.fix_dashes(text, None) == (text, [])


def test_spaced_en_dash_is_a_dash_and_digit_em_dash_is_a_range():
    assert fix(f"The lake is huge {EN} it holds most of the water.") == "The lake is huge. It holds most of the water."
    assert fix(f"Built in 1637{EM}1638 and again in 20,000 {EM} 24,000 years.") == \
        f"Built in 1637{EN}1638 and again in 20,000{EN}24,000 years."


def test_trip_name_and_a_quote_from_the_source_are_kept():
    text = (f"The {TRIP} itinerary starts in Hanoi {EM} a city of lakes. Its name, \"Uran Togoo {EM} Tulga Uul\", "
            f"is official. A guide called it \"slow {EM} and right\".")
    new, log = h.fix_dashes(text, _ctx())
    assert new == (f"The {TRIP} itinerary starts in Hanoi: a city of lakes. Its name, \"Uran Togoo {EM} Tulga Uul\", "
                   "is official. A guide called it \"slow, and right\".")
    assert log == ["replaced 2 em-dash(es) with a comma, colon, full stop or brackets"]
    assert h.fix_dashes(text, None)[0].count(EM) == 0  # no context, nothing is kept


# ---------------------------------------------------------------- autofix

def test_autofix_runs_it_and_is_idempotent():
    text = f"Mörön is not the destination {EM} it is the frame.\n\nThe lake {EM} the largest in Mongolia {EM} is cold."
    once, log = h.autofix(text, _ctx())
    assert EM not in once and any("em-dash" in line for line in log)
    twice, log2 = h.autofix(once, _ctx())
    assert twice == once and not any("em-dash" in line for line in log2)


def test_contrast_frame_count_is_unchanged():
    # the contrast regexes accept ',;:' and a two-sentence 'not X. It is Y.' too (plan 09 #5 correction)
    text = (f"Northern Mongolia is not a single landscape {EM} it is two. That boundary isn't just a backdrop {EM} "
            f"it's a fact. The lake is cold {EM} not just cool, but cold.")
    assert len(h.contrast_frames(fix(text))) == len(h.contrast_frames(text)) == 3


# ---------------------------------------------------------------- F2 residue guard

def test_f2_flags_a_residue_em_dash_with_its_sentence():
    r = qg.gate_banned_patterns(f"The lake is wide. The water {EM} cold and clear {EM} is deep.", SEED)
    assert not r["passed"]
    assert r["violations"][0] == (f"uses an em-dash ('water {EM} cold'); use a comma, a colon or a full stop: "
                                  f"'The water {EM} cold and clear {EM} is deep.'")
    assert len(r["violations"]) == 2


def test_f2_exempts_a_phrase_copied_from_the_source():
    # the seed's own labels hold em-dashes: a bare dash would always be exempt, the phrase only when copied
    assert qg.gate_banned_patterns(f"We trek: Uran Togoo {EM} trek into the caldera.", SEED)["passed"]
    assert not qg.gate_banned_patterns(f"Uran Togoo {EM} a volcano.", SEED)["passed"]


def test_fixed_text_passes_f2():
    text = (f"Mörön {EM} the capital of Khövsgöl Province {EM} serves lunch. It is cold {EM} very cold. "
            f"The {TRIP} route is long.")
    assert qg.gate_banned_patterns(h.fix_dashes(text, _ctx())[0], f"{SEED}\n{TRIP}")["passed"]


def test_run_quality_gates_treats_cta_and_trip_name_as_source():
    rubric = qg.get_framework_rubric("promotion")
    f8 = {"items": [{"criterion": c, "score": "1", "evidence": "q"} for c in rubric]}
    f9 = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
          "failure_codes": [], "flagged_phrases": [], "notes": ""}
    cta = f"Ask us {EM} we plan it with you."

    def raw(**d):
        return {"text": json.dumps(d)}

    text = f"The {TRIP} is a fine trip. {cta}"
    with patch.object(qg, "invoke_judge", side_effect=[raw(**f8), raw(**f9)]):
        out = qg.run_quality_gates(content_text=text, atom_text=SEED, cta=cta, goal_key="promotion",
                                   brand_rubric_text="r", channel="linkedin", hygiene_ctx=_ctx(cta=cta))
    f2 = next(g for g in out["gate_ledger"] if g["gate"] == "F2_banned_patterns")
    assert f2["passed"], f2


def test_banned_seed_has_the_guard():
    assert h.EM_DASH_PATTERN in qg._BANNED_PATTERNS_SEED


# ---------------------------------------------------------------- admin sentence clean-up

def test_sanitize_replacement_converts_dashes():
    assert sr.sanitize_replacement(f"Mörön is not the destination {EM} it is the frame. [R:a1]", "blog") == \
        "Mörön is not the destination. It is the frame."
    assert EM not in sr.sanitize_replacement(f"A lake {EM} the largest {EM} in Mongolia.", None)


# ---------------------------------------------------------------- gates that read the autofix output

def test_testimonial_attribution_dash_is_kept_for_f19():
    # F19's attribution patterns need the dash; fix_dashes runs before the gates (review finding, S238)
    for text in (f"\"Quiet, green and endless, a place to slow down.\" {EM} Sarah, UK",
                 f"The valley is quiet.\n\n\"Quiet, green and endless, a place to slow down.\"\n{EM} Sarah, UK",
                 f"\"Quiet, green and endless.\" {EM} Tom B. (Sydney)"):
        assert authenticity.flagged(text)
        fixed, _ = h.autofix(text, _ctx())
        assert authenticity.flagged(fixed), fixed
        assert authenticity.flagged(h.fix_dashes(text, None)[0])
        cut, _ = authenticity.cut_flagged(fixed)
        assert EM not in cut and "Sarah" not in cut and "Tom B." not in cut


PUNAKHA_SEED = """Punakha Dzong — visit courtyards and assembly hall
Location: Punakha Dzong, Punakha, Punakha District, Bhutan
Place background (checked facts about the place itself — you may use these):
- It was built in 1637–1638 by Zhabdrung Ngawang Namgyal, who founded it to serve as the administrative centre.
- Punakha Dzong served as the capital and seat of government of Bhutan until 1955, when the capital moved to Thimphu."""


@pytest.mark.parametrize("sentence", [
    f"Built in 1637, the dzong served as the seat of government until 1955 {EM} today it remains Bhutan's "
    "administrative centre.",
    f"Zhabdrung founded it in 1637 {EM} it remains the seat of Bhutan's government today.",
    f"Founded in 1637 as a fortress {EM} it still serves as Bhutan's administrative centre.",
])
def test_date_authority_still_fires_after_the_dash_becomes_a_colon(sentence):
    assert tf.date_authority_claims(sentence, PUNAKHA_SEED)
    fixed = fix(sentence)
    assert EM not in fixed
    assert tf.date_authority_claims(fixed, PUNAKHA_SEED), fixed


def test_clause_split_leaves_clock_times_whole():
    assert tf._CLAUSE_SPLIT_RE.split("Doors open at 10:30 every day.") == ["Doors open at 10:30 every day."]
