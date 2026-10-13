"""AA-768 (S234 round 2 review) — regressions found in review of the round-2 hygiene rules: autofix
must keep running the older checks after a round-2 trim, audience ranges only in an age context,
no half-cut trip names, 'the guide' is a tour guide, FAQ answers are not paragraph openers, a
piece-opening 'It…' is a warning, sibling templates skip the keyword and seed/Facts paraphrases,
and the blog writer is told the FAQ is required (3-4 pairs)."""
import uuid
from unittest.mock import patch

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service as writing_service
from services.acp_content_writing.prompts import build_user_prompt

from tests.unit.test_aa768_s234_round2 import BLOG, SEED, SCOPE, TERRA_AUDIENCE, TRAIL, TRIP, _ctx
from tests.unit.test_aa771_s234_voice_contract import GOAL


def _trail(**kw):
    base = dict(audience=TRAIL["customer_segment"], seed="Kuang Si Falls — swim\nDay 2 of the trip",
                scope="SCOPE: this piece is about one moment of the trip (day 2): Kuang Si Falls.", trip_name=None,
                brand_name="Trail Pulse", cta="Get the full Trail Pulse itinerary and dates")
    base.update(kw)
    return _ctx(**base)


# ---------------------------------------------------------------- autofix falls through after a trim

class TestAutofixFallThrough:
    def test_trip_length_trim_still_fixes_source_notes_wording(self):
        """S233 #13: both the '15-day' trim and the source-notes fix apply to one sentence."""
        text = ("Sod Camp faces a mountain range.\n\nThe content seed describes this as a 15-day journey with a mix "
                "of physical activity, including crater trekking and horseback riding.")
        fixed, log = h.autofix(text, _ctx())
        assert "content seed" not in fixed and "15-day" not in fixed
        assert "This is a journey with a mix of physical activity" in fixed
        assert any("removed source-notes wording" in line for line in log)
        assert not h.find_issues(fixed, _ctx())["leak"]

    def test_trimmed_sentence_can_still_be_social_proof(self):
        text = ("Sod Camp faces a mountain range.\n\nFamilies always say the fifteen-day route was the trip of a "
                "lifetime and they never forget it.")
        fixed, log = h.autofix(text, _ctx())
        assert "always say" not in fixed and any("social proof" in line for line in log)


# ---------------------------------------------------------------- audience ranges only as an age

class TestAudienceAgeContext:
    def test_measurements_are_never_the_audience(self):
        terra, trail = _ctx(), _trail()
        for sentence, ctx in (
            ("The drive takes 25 to 40 minutes each way.", trail),
            ("Most visitors stay 25 to 40 minutes at the upper pool.", trail),
            ("The hike climbs from 25 to 40 metres above the river.", trail),
            ("The drive from Mörön takes seven to sixteen hours depending on the road.", terra),
        ):
            assert h.audience_leaks(sentence, ctx.audience, ctx.source_text) == [], sentence
            fixed, _ = h.autofix(f"Sod Camp faces a mountain range. {sentence}", ctx)
            assert sentence in fixed

    def test_between_range_is_a_leak_but_never_half_cut(self):
        """S233 #16: 'children between seven and sixteen is the density…' must not become
        'children between is the density…'."""
        s = ("What makes it compelling for families with children between seven and sixteen is the density of "
             "the landscape.")
        assert h.audience_leaks(s, TERRA_AUDIENCE, _ctx().source_text) == ["seven and sixteen"]
        fixed, _ = h.autofix(f"Sod Camp faces a mountain range. {s} Lake Khövsgöl is deep.", _ctx())
        assert "between is" not in fixed and "children between seven" not in fixed  # deleted whole
        unsafe, _ = h.autofix(f"Sod Camp faces a mountain range. {s} It rewards curiosity.", _ctx())
        assert s in unsafe  # deleting would orphan 'It…' → left for F13_leak
        assert h.find_issues(unsafe, _ctx())["leak"]

    def test_age_shapes_are_trimmed(self):
        fixed, _ = h.autofix("Day 2 is easy. If you're a 25–40 traveller who wants active days, this works.",
                             _trail())
        assert "If you're a traveller who wants active days" in fixed
        fixed, _ = h.autofix("Sod Camp faces a range. The trip is designed for families with children aged 7 to 16.",
                             _ctx())
        assert "families with children." in fixed and "7 to 16" not in fixed
        assert h._trim_audience("Meet an 18-35-year-old explorer here.", "18-35") == "Meet an explorer here."
        assert h._trim_audience("Meet a 25–40 adult crowd.", "25–40") == "Meet an adult crowd."

    def test_range_in_the_facts_block_is_a_source_fact(self):
        s = "The trek suits travellers aged 25 to 40."
        assert h.audience_leaks(s, TRAIL["customer_segment"], _trail().source_text)
        ctx = _trail(facts="[Fact id=f1] The trek suits travellers aged 25 to 40.")
        assert h.audience_leaks(s, ctx.audience, ctx.source_text) == []
        fixed, _ = h.autofix(f"Day 2 is easy. {s}", ctx)
        assert s in fixed


# ---------------------------------------------------------------- scoped trip name never half-cut

class TestScopedTripNameFallback:
    @pytest.mark.parametrize("trip,seed,scope", [
        ("Laos to Vietnam by bicycle 12 days", "Kuang Si Falls — swim\nLocation: Kuang Si Falls, Luang Prabang, Laos",
         "SCOPE: this piece is about one moment of the trip (day 2): Kuang Si Falls."),
        ("Vietnam: Hanoi, Halong Bay and Hoi An — 10 Days", "Halong Bay — cruise\nLocation: Halong Bay, Quang Ninh, "
         "Vietnam", "SCOPE: this piece is about one moment of the trip (day 3): Halong Bay."),
        ("Cambodia & Vietnam: Angkor to the Mekong Delta — 12 Days", "Angkor Wat — sunrise\nLocation: Angkor Wat, "
         "Siem Reap, Cambodia", "SCOPE: this piece is about one moment of the trip (day 2): Angkor Wat."),
    ])
    def test_broken_cut_gives_no_trip_line(self, trip, seed, scope):
        lex = h.build_scope_lexicon(seed, scope, trip)
        assert lex.cap_only  # the name does name an out-of-scope place
        assert h.scoped_trip_name(trip, scope, lex) is None

    def test_clean_cut_is_kept(self):
        lex = h.build_scope_lexicon(SEED, SCOPE, TRIP)
        assert h.scoped_trip_name(TRIP, SCOPE, lex) == "Mongolia: Khövsgöl Lake"

    def test_broken_name_shapes(self):
        assert h._broken_trip_name("Laos to by bicycle")
        assert h._broken_trip_name("Vietnam:, Halong Bay An")
        assert h._broken_trip_name("Cambodia: Angkor to the")
        assert not h._broken_trip_name("Mongolia: Khövsgöl Lake")


# ---------------------------------------------------------------- 'the guide' is a tour guide

class TestTourGuideIsNotALeak:
    @pytest.mark.parametrize("sentence", [
        "The guide will meet you at the trailhead and walks you through the safest route to the upper pool.",
        "The guide will point out the nesting cranes along the shore.",
        "The guide describes how the dzong survived the 1897 earthquake.",
        "In the afternoon the guide covers the history of the valley.",
        "Inside, the guide walks you through the assembly hall.",
    ])
    def test_not_a_leak_and_not_deleted(self, sentence):
        assert h.instruction_leaks(sentence, SCOPE) == []
        text = f"Punakha Dzong sits at the confluence.\n\n{sentence}\n\nLake Khövsgöl is deep."
        fixed, log = h.autofix(text, _ctx())
        assert sentence in fixed and not any("writer-instruction" in line for line in log)
        assert not h.find_issues(fixed, _ctx())["leak"]

    def test_self_reference_still_caught(self):
        for s in ("This guide covers the Khövsgöl region in depth.", "This piece traces one route.",
                  "The article will walk you through the route.", "This post explores the lake."):
            assert h.instruction_leaks(s, SCOPE), s


# ---------------------------------------------------------------- FAQ answers are not openers

class TestFaqAnswerPronouns:
    def test_pronoun_answers_are_not_integrity_problems(self):
        after = qg.replace_faq_section(BLOG, [
            ("How deep is Lake Khövsgöl?", "It reaches 262 metres at its deepest point."),
            ("What are reindeer used for?", "They are ridden and used as pack animals."),
            ("Where is Sod Camp?", "It sits opposite a mountain range."),
        ])
        assert qg.faq_pair_count(after) == 3
        assert h.integrity_problems(BLOG, after) == []

    def test_prose_pronoun_paragraph_still_reported(self):
        before = "Families arrive at the lake. They find a boundary.\n\nThe camp is quiet."
        after = "They find a boundary.\n\nThe camp is quiet."
        assert h.integrity_problems(before, after)

    @pytest.mark.asyncio
    async def test_regenerated_faq_with_pronoun_answers_is_kept(self):
        pairs = [("What is Sod Camp?", "It is a standard ger camp opposite a mountain range."),
                 ("Who lives near the lake?", "They are reindeer-herding Tsaatan (Dukha) communities."),
                 ("What share of fresh water does the lake hold?",
                  "It holds about 0.4 percent of the world's fresh surface water.")]
        ctx = {"atom_text": SEED, "facts_text": "", "brand_audience": {"brand_name": "Terra"}, "tenant_id": None,
               "cta": "Ask Terra Family Expeditions to plan this trip for your family"}
        with patch.object(writing_service, "write_faq", return_value=(pairs, 0.006)):
            out, _cost, log = await writing_service._enforce_faq_contract(
                BLOG, context=ctx, hyg=_ctx(), request_id=uuid.uuid4(), brand_ignore=frozenset())
        assert qg.faq_pair_count(out) == 3 and "1 → 3 pairs" in log[0]


# ---------------------------------------------------------------- piece-opening pronoun

class TestPieceOpener:
    @pytest.mark.parametrize("opener", [
        "It takes four hours to drive from Mörön to Khatgal.",
        "It is 5 a.m. in Paro and the dzong is already lit.",
        "This is Punakha Dzong, built where two rivers meet.",
        "They call it the Palace of Great Happiness.",
    ])
    def test_impersonal_hooks_only_warn(self, opener):
        found = h.find_issues(f"{opener} The courtyards are open to visitors.", _ctx())
        assert found["leak"] == []
        assert any("may open on a pronoun" in v for v in found["hygiene"])
        assert qg.gate_leak(f"{opener} The courtyards are open.", _ctx())["passed"]

    def test_what_they_find_still_blocks(self):
        found = h.find_issues("What they find at Lake Khövsgöl is a boundary. The lake is deep.", _ctx())
        assert any("opens on a pronoun" in v for v in found["leak"])


# ---------------------------------------------------------------- sibling templates: keyword, seed facts

class TestTemplatesSkipSourceAndKeyword:
    def test_keyword_sentence_is_not_a_template(self):
        kw = "Khovsgol and Gobi tour Mongolia price"
        sib = "If you are weighing a Khovsgol and Gobi tour Mongolia price against what the journey actually gives."
        mine = "For parents thinking about the Khovsgol and Gobi tour Mongolia price in terms of what a family gets."
        assert h.sibling_templates(mine, [sib], exclude=[SEED]) != []  # the shared keyword run alone
        assert h.sibling_templates(mine, [sib], exclude=[SEED], keyword=kw) == []
        g = qg.gate_voice(mine, spec=None, sibling_texts=[sib], exclude_texts=[SEED], keyword=kw)
        assert g["passed"]

    def test_seed_fact_paraphrase_is_not_a_template(self):
        seed = ("Sod Camp — camp\nSource itinerary: The camp sits near a mineral spring with reputed healing "
                "properties.\n- A mineral spring in the vicinity is traditionally attributed with healing properties "
                "and is visited by both locals and tourists.")
        sib = ("A mineral spring in the vicinity carries a long local tradition of healing properties, visited by both "
               "residents of the region and those passing through.")
        mine = ("A mineral spring in the vicinity carries a long local reputation for healing properties, visited by "
                "both local communities and travellers.")
        assert h.sibling_templates(mine, [sib], exclude=[h.build_scope_lexicon(seed, None, None).own_text]) == []

    def test_real_template_still_found(self):
        sib = "These are the shape of the day, and a well-prepared family arrives knowing exactly what to expect."
        mine = "A well-prepared family arrives already knowing what to expect."
        seed = "Visitors must leave cameras and bags at a checkpoint; modest, respectful dress is required."
        assert [s for s, _ in h.sibling_templates(mine, [sib], exclude=[seed])] == [mine]


# ---------------------------------------------------------------- blog writer contract = F16_faq

def test_blog_prompt_requires_three_to_four_faq_pairs():
    p = build_user_prompt(content_seed="Paro Taktsang — hike\nSource itinerary: Hike up.",
                          goal=GOAL, channel_style=get_channel_style("blog"), brand_audience=TRAIL,
                          angle={"name": "A", "why_it_works": "B", "formula_fit": "PAS", "best_final_style": "C"},
                          cta="Ask us", atom_id="a1")
    assert "3-4 Q/A pairs" in p and "FAQ: 3-4 questions" in p and "If, and only if" not in p
    assert "FAQ (if top-of-funnel)" not in p
