"""AA-768 (S234 round 3) — repair integrity++, trip-fact grounding, rarity guard, anti-template,
date authority, CTA literal and the F1/F9 sentence-repair routing (round-2 panel §4 fixes 2, 3, 4,
5, 7, 8, 9). Sentences are taken from the S234 round-2 pieces the panel scored."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import service
from services.acp_content_writing import trip_facts as tf
from services.acp_content_writing import claim_rules as cr

TERRA_AUDIENCE = "Affluent families with children aged 7–16\nParents who want active holidays with safety"
AH_AUDIENCE = "High-net-worth couples, private families, family offices, senior executives"
TERRA_CTA = "Ask Terra Family Expeditions to plan this trip for your family"

ROUTE_SEED = """Uran Togoo — trek into the caldera of a dormant volcano
Location: Uran Togoo Volcano, Bulgan, Bulgan Province, Mongolia
Day 3 of the trip
Source itinerary: Journey to Uran Togoo, a dormant volcano.

Sod Camp — overnight in a standard ger camp
Location: Sod Camp, Mongolia
Day 3 of the trip
Source itinerary: Arrive at Sod Camp in the afternoon. The camp sits opposite a mountain range with trekking access and proximity to a mineral spring with reputed healing properties. Overnight in a standard ger camp.

Moron — stop for lunch
Location: Mörön, Mörön, Khövsgöl Province, Mongolia
Naming: The city is correctly spelled Mörön (also romanised as Murun or Muren); 'Moron' is a common approximation. It should be referred to as 'Mörön'.
Day 4 of the trip
Source itinerary: Stop for lunch near Moron, capital of Khövsgöl province.
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- Mörön has a small domestic airport with scheduled flights connecting it to Ulaanbaatar.

Lake Khövsgöl — visit the boundary between Central Asian steppes and Siberian Taiga forests
Location: Lake Khövsgöl, Khövsgöl Nuur National Park, Khövsgöl Province, Mongolia
Day 4 of the trip: Lake Khövsgöl — visit the boundary — Source itinerary: The lake sits at the boundary between Central Asian steppes and Siberian Taiga forests.
Day 5 of the trip: Khövsgöl Lake — take boat rides on the water — Source itinerary: Optional activities include boat rides on the water, horseback riding across surrounding terrain, visits to local nomadic families
Day 5 of the trip: Khövsgöl Lake — trek along shorelines and forest paths — Source itinerary: trekking along shorelines and forest paths
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- It holds an estimated 70 percent of Mongolia's fresh water.
- Visitors typically arrive via the nearby town of Khatgal at the lake's southern tip.

Tsaatan reindeer herders' settlements — excursions to Tsaatan reindeer herders' settlements
Location: Tsaatan Reindeer Herder Settlements, Tsagaannuur sum, Khövsgöl Province, Mongolia
Day 5 of the trip
Source itinerary: excursions to Tsaatan reindeer herders' settlements
Why it matters: encounter one of Mongolia's most distinct ethnic groups
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- The Tsaatan (Dukha) are a small community, numbering only a few hundred people.
- Their reindeer-herding culture is one of the last of its kind in the world.
- Their territory lies deep in the taiga, and access requires a multi-hour horse or reindeer ride, often crossing rivers and boggy forest terrain.
- Reindeer are used for riding and milk, making this practice distinct from many other reindeer-herding traditions."""

PUNAKHA_SEED = """Punakha Dzong — visit courtyards and assembly hall
Location: Punakha Dzong, Punakha, Punakha District, Bhutan
Day 4 of the trip
Source itinerary: Visit Punakha Dzong's courtyards and assembly hall.
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- It was built in 1637–1638 by Zhabdrung Ngawang Namgyal, the unifier of Bhutan.
- Punakha Dzong served as the capital and seat of government of Bhutan until 1955, when the capital was moved to Thimphu.
- It remains the winter residence of the Je Khenpo and the Central Monk Body, who relocate here from Thimphu each year."""


def _tctx(seed=ROUTE_SEED, audience=TERRA_AUDIENCE, cta=TERRA_CTA):
    return tf.TripFactContext(seed=seed, audience=audience, cta=cta)


def _hctx(seed=ROUTE_SEED, **kw):
    base = dict(seed=seed, brand_name="Terra Family Expeditions", cta=TERRA_CTA)
    base.update(kw)
    return h.HygieneContext(**base)


# ---------------------------------------------------------------- 1. repair integrity++

class TestNearDuplicates:
    def test_a_figure_told_twice_is_found_and_a_short_restatement_deleted(self):
        """r2 #16: 'holding an estimated seventy percent…' then 'The lake holds an estimated seventy
        percent of Mongolia's fresh water.'"""
        text = ("Lake Khövsgöl sits precisely at that boundary, holding an estimated seventy percent of Mongolia's "
                "fresh water and roughly 0.4 percent of the world's fresh surface water.\n\n"
                "The surrounding area is a national park. The lake holds an estimated seventy percent of Mongolia's "
                "fresh water. Optional activities include boat rides.")
        dups = h.near_duplicates(text)
        assert [d["kind"] for d in dups] == ["copy"]
        fixed, log = h.dedupe(text)
        assert fixed.count("seventy percent") == 1 and "deleted a sentence" in log[0]

    def test_a_number_word_repeat_is_a_fact_duplicate(self):
        """r2 #17: 'only a few hundred people' twice; the later one is left for F13_repeat when its
        deletion would orphan the next pronoun."""
        text = ("For a community numbering only a few hundred people, the Tsaatan keep reindeer. "
                "Their population numbers only a few hundred people. They live in the taiga.")
        dups = h.near_duplicates(text)
        assert dups and dups[0]["kind"] == "fact" and "few 100 people" in dups[0]["shared"]
        fixed, _ = h.dedupe(text)
        assert fixed == text  # 'They live…' would lose its referent
        assert h.find_issues(fixed, _hctx())["repeat"]

    def test_an_echo_across_a_sentence_break_is_trimmed(self):
        """r2 #16: 'Uran Togoo arrives on day three. On day three, the route passes…'"""
        text = "Uran Togoo arrives on day three. On day three, the route passes through Bulgan Province."
        fixed, log = h.dedupe(text)
        assert fixed == "Uran Togoo arrives on day three. The route passes through Bulgan Province."
        assert "echoed" in log[0]

    def test_an_orphan_line_left_by_a_repair_is_deleted(self):
        """r2 #3: 'The itinerary builds it in.' between two sentences that already say it."""
        text = ("The lower pools are open for swimming. The itinerary keeps you there — time set aside to rest.\n\n"
                "The itinerary builds it in.\n\n"
                "Trail Pulse builds trips around movement.")
        assert h.orphan_lines(text) == ["The itinerary builds it in."]
        fixed, _ = h.dedupe(text)
        assert "builds it in" not in fixed

    def test_day_indexes_and_different_facts_are_not_duplicates(self):
        text = ("On day 5, families ride boats on the lake. On day 5 the Tsaatan camps are visited too.\n\n"
                "The crater is 600 metres wide. The lake sits at 1,645 metres.")
        assert h.near_duplicates(text) == []

    def test_integrity_problems_catch_a_new_fact_duplicate(self):
        before = "The lake holds 70 percent of Mongolia's fresh water. The camp faces a range."
        after = ("The lake holds 70 percent of Mongolia's fresh water. With 70 percent of Mongolia's fresh water, "
                 "the camp faces a range.")
        assert any("repeats a figure" in p for p in h.integrity_problems(before, after))


class TestPlacement:
    BLOG = ("## Uran Togoo: Ancient Fire\n\n"
            "From Uran Togoo, the journey continues to Sod Camp, with trekking access from camp and a mineral spring "
            "in the vicinity with reputed healing properties.\n\n"
            "## Lake Khövsgöl: Scale\n\n"
            "The lake holds most of the water.\n\n"
            "Sod Camp, a standard ger camp, offers trekking access and sits in proximity to a mineral spring with "
            "reputed healing properties.\n\n"
            "The Tsaatan live to the north.")

    def test_a_moment_repeated_under_another_moments_heading_is_found_and_deleted(self):
        """r2 #4: the Sod Camp line inside the Lake Khövsgöl section."""
        moments = tf.parse_moments(ROUTE_SEED)
        assert tf.misplaced_sentences(self.BLOG, moments) == [
            "Sod Camp, a standard ger camp, offers trekking access and sits in proximity to a mineral spring with "
            "reputed healing properties."]
        fixed, log = tf.fix_misplaced(self.BLOG, moments)
        assert fixed.count("Sod Camp") == 1 and "Sod Camp sentence repeated" in log[0]

    def test_a_transition_naming_the_next_moment_is_not_misplaced(self):
        text = ("## Mörön: Gateway\n\nMörön is a provincial capital. Arriving at Lake Khövsgöl on day four, the shift "
                "becomes physical.\n\n## Lake Khövsgöl\n\nThe lake is deep.")
        assert tf.misplaced_sentences(text, tf.parse_moments(ROUTE_SEED)) == []

    def test_splice_checked_deletes_the_new_later_duplicate_instead_of_reverting(self):
        before = ("The lake holds 70 percent of Mongolia's fresh water, a measure of its scale.\n\n"
                  "Boats leave from the shore. The ger camp faces a mountain range.")
        item = {"start": before.index("Boats"), "end": before.index("shore.") + 6, "sentence": "Boats…",
                "problem": "p"}
        out = sr.splice_checked(before, [item], ["It holds 70 percent of Mongolia's fresh water."])
        assert out.count("70 percent") == 1 and item.get("guard") == "dedupe"

    def test_a_repair_that_misplaces_a_moment_is_cut_when_safe_else_reverted(self):
        moments = tf.parse_moments(ROUTE_SEED)
        before = ("## Uran Togoo\n\nUran Togoo is a dormant volcano with a walkable caldera floor.\n\n"
                  "## Lake Khövsgöl\n\nThe lake is large. It feels open. Boats are optional here.")
        bad = "Uran Togoo rises above green steppe to the south."
        item = {"start": before.index("It feels"), "end": before.index("open.") + 5, "sentence": "It feels open.",
                "problem": "p"}
        out = sr.splice_checked(before, [item], [bad], moments=moments)
        assert "Uran Togoo" not in out.split("## Lake")[1] and item["guard"] == "dedupe"
        unsafe = before.replace("Boats are optional here.", "It has boats.")
        item = {"start": unsafe.index("It feels"), "end": unsafe.index("open.") + 5, "sentence": "It feels open.",
                "problem": "p"}
        out = sr.splice_checked(unsafe, [item], [bad], moments=moments)
        assert out == unsafe and item["guard"] == "integrity"


# ---------------------------------------------------------------- 2. trip-fact grounding

class TestOptionalActivities:
    def test_unhedged_optional_activity_fires_and_a_hedged_one_does_not(self):
        moments = tf.parse_moments(ROUTE_SEED)
        assert [a.phrase for a in tf.optional_activities(moments)] == [
            "boat rides on the water", "horseback riding across surrounding terrain",
            "visits to local nomadic families", "trekking along shorelines and forest paths"]
        bad = "Horseback riding crosses the surrounding terrain."
        ok = "Optional activities include boat rides on the water and horseback riding."
        assert tf.optional_as_fixed(bad, moments)
        assert tf.optional_as_fixed(ok, moments) == []

    def test_the_clause_after_a_semicolon_is_checked_on_its_own(self):
        """r2 #16: '…; the journey also follows shorelines and forest paths on foot.'"""
        text = ("The activities possible here reflect the landscape. Optional activities include boat rides on the "
                "water; the journey also follows shorelines and forest paths on foot.")
        out = tf.optional_as_fixed(text, tf.parse_moments(ROUTE_SEED))
        assert len(out) == 1 and "trekking along shorelines" in out[0]

    def test_allows_for_is_not_a_hedge_and_generic_words_do_not_count(self):
        moments = tf.parse_moments(ROUTE_SEED)
        assert tf.optional_as_fixed("Time at the lake allows for days in the company of local nomadic families.",
                                    moments)
        assert tf.optional_as_fixed("On Day 14, families visit the ruins of an ancient monastery.", moments) == []
        assert tf.optional_as_fixed("Reindeer are used for riding and for milk.", moments) == []


class TestTimesAndPositions:
    def test_first_night_and_mid_morning_fire(self):
        moments = tf.parse_moments(ROUTE_SEED)
        assert tf.timing_claims("It is a considered place to spend the first night before pressing north.", moments)
        kuang = tf.parse_moments("Kuang Si Falls — swim\nDay 2 of the trip\nSource itinerary: Time is left for "
                                 "swimming and resting before lunch.")
        assert tf.timing_claims("Trail Pulse knows when a swim mid-morning is the right call.", kuang)

    def test_source_times_and_day_numbers_are_supported(self):
        moments = tf.parse_moments(ROUTE_SEED)
        assert tf.timing_claims("That afternoon, the journey continues to Sod Camp.", moments) == []
        assert tf.timing_claims("The evening is in a traditional ger at Sod Camp.", moments) == []
        assert tf.timing_claims("On the fourth day, the journey pauses at Mörön.", moments) == []

    def test_a_background_position_supports_only_a_sentence_about_it(self):
        moments = tf.parse_moments(ROUTE_SEED)
        assert tf.timing_claims("You stand at the southern tip of the lake, looking north.", moments)
        assert tf.timing_claims("Visitors arrive via the town of Khatgal at the lake's southern tip.", moments) == []


class TestFamilyCaveat:
    PITCH = "## The Tsaatan\n\nThe Tsaatan herd reindeer in the taiga, and families meet them in their camps."

    def test_a_family_brand_pitch_without_the_caveat_fires(self):
        out = tf.missing_family_caveat(self.PITCH, tf.parse_moments(ROUTE_SEED), TERRA_AUDIENCE)
        assert len(out) == 1 and "multi-hour horse or reindeer ride" in out[0]["caveat"]
        found = tf.find_trip_fact_issues(self.PITCH, _tctx())["trip_fact"]
        assert any("add ONE short sentence" in v for v in found)

    def test_the_caveat_in_the_moments_section_or_a_non_child_audience_passes(self):
        moments = tf.parse_moments(ROUTE_SEED)
        said = self.PITCH + " Reaching the camps takes a multi-hour horse ride."
        assert tf.missing_family_caveat(said, moments, TERRA_AUDIENCE) == []
        assert tf.missing_family_caveat(self.PITCH, moments, AH_AUDIENCE) == []

    def test_horseback_riding_as_a_lake_activity_is_not_the_caveat(self):
        text = ("## Day Five\n\nOptional activities include horseback riding across the surrounding terrain. "
                "The Tsaatan herd reindeer in the taiga.")
        assert tf.missing_family_caveat(text, tf.parse_moments(ROUTE_SEED), TERRA_AUDIENCE)


# ---------------------------------------------------------------- 3. rarity / quantifier

class TestRarity:
    SRC = h.split_seed(ROUTE_SEED)[0]

    @pytest.mark.parametrize("sentence", [
        "This convergence distinguishes the region from almost anywhere else on earth.",
        "The Tsaatan represent a way of life that exists almost nowhere else on earth.",
        "It is the reason the north feels categorically different from every other part of the country.",
        "Reindeer are used in ways that have no equivalent elsewhere.",
        "The taiga leads to one of the world's most distinctive living cultures.",
    ])
    def test_unsourced_rarity_fires(self, sentence):
        assert tf.rarity_claims(sentence, self.SRC)

    def test_many_becoming_most_fires_and_the_seeds_own_words_pass(self):
        out = tf.rarity_claims("This sets their practice apart from most other reindeer-herding traditions.", self.SRC)
        assert out and "the source says 'many'" in out[0]
        assert tf.rarity_claims("Their culture is one of the last of its kind in the world.", self.SRC) == []
        assert tf.rarity_claims("The Tsaatan are one of Mongolia's most distinct ethnic groups.", self.SRC) == []
        assert tf.rarity_claims("The careful observer is the most honest traveller.", self.SRC) == []


# ---------------------------------------------------------------- 5. date authority

class TestDateAuthority:
    def test_present_role_claims_fire_in_body_and_meta(self):
        meta = "Discover why Punakha Dzong was built where two rivers meet, and how it remains Bhutan's active " \
               "religious and administrative center today."
        out = tf.date_authority_claims("", PUNAKHA_SEED, extra_texts=(("meta", meta),))
        assert out and out[0].startswith("meta ") and "until 1955" in out[0]
        assert tf.date_authority_claims("The founding purpose, religious and governmental in one, remains present.",
                                        PUNAKHA_SEED)

    def test_a_meta_only_finding_is_a_warning_not_a_hold(self):
        meta = "Punakha Dzong remains Bhutan's administrative center today, built where two rivers meet in 1637."
        f17, f18 = qg.gate_trip_facts("Punakha Dzong sits where two rivers meet.", _tctx(seed=PUNAKHA_SEED),
                                      meta_description=meta)
        assert not f17["passed"] and not f17["blocking"] and f18["passed"]

    def test_past_tense_and_district_life_pass(self):
        ok = ("It became the administrative and religious centre of a unified Bhutan, the seat of government until "
              "the mid-twentieth century. He placed it there to function as the administrative centre of Bhutan. "
              "Administrative and spiritual life continues to honour the site. Punakha Dzong remains the winter "
              "residence of the Je Khenpo.")
        assert tf.date_authority_claims(ok, PUNAKHA_SEED) == []

    def test_spans_are_checked_against_the_seed_dates(self):
        assert tf.date_authority_claims("For nearly three centuries it served as Bhutan's capital.", PUNAKHA_SEED)
        assert tf.date_authority_claims("For over three centuries it served as Bhutan's capital.", PUNAKHA_SEED) == []
        assert tf.date_authority_claims("The choice was made almost four centuries ago by the capital's founder.",
                                        PUNAKHA_SEED) == []

    def test_the_writer_gets_the_span_as_a_stated_fact_once(self):
        seed = tf.with_date_spans(PUNAKHA_SEED)
        line = ("- It served as the capital and seat of government of Bhutan for 318 years (1637–1955), just over "
                "three centuries; it has not held that role since 1955.")
        assert line in seed and tf.with_date_spans(seed) == seed
        assert seed.index(line) > seed.index("until 1955")


# ---------------------------------------------------------------- 2c. out-of-scope trip places

class TestTripPlaces:
    def test_a_background_place_is_out_of_scope_only_as_a_leg_of_travel(self):
        lex = h.build_scope_lexicon(ROUTE_SEED, trip_places=("Ulaanbaatar", "Gobi Desert", "Khövsgöl Lake"))
        assert "ulaanbaatar" in lex.leg_only and "gobi" in lex.trip_keys and "khovsgol" not in lex.gazetteer
        assert h.places_named(lex, "Mörön has a domestic airport connecting the region to Ulaanbaatar.") == [
            "Ulaanbaatar"]
        assert h.places_named(lex, "Its people were long governed from Ulaanbaatar.") == []
        assert h.places_named(lex, "The Gobi feels far away.") == ["Gobi"]

    def test_the_je_khenpo_relocation_from_thimphu_is_not_a_leg(self):
        lex = h.build_scope_lexicon(PUNAKHA_SEED, trip_places=("Thimphu", "Paro"))
        assert h.places_named(lex, "The Je Khenpo still returns here each winter, relocating from Thimphu.") == []
        assert h.places_named(lex, "Paro is where the journey starts.") == ["Paro"]

    @pytest.mark.asyncio
    async def test_trip_places_query_is_one_read_and_fails_open(self):
        conn = AsyncMock()
        conn.fetch.return_value = [{"place": "Ulaanbaatar"}, {"place": None}]
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        assert await service._trip_places_outside(uuid.uuid4(), "trip", ["a1"], pool) == ["Ulaanbaatar"]
        assert conn.fetch.await_args.args[3] == ["a1"]
        conn.fetch.side_effect = RuntimeError("db down")
        assert await service._trip_places_outside(uuid.uuid4(), "trip", ["a1"], pool) == []
        assert await service._trip_places_outside(uuid.uuid4(), None, ["a1"], pool) == []


# ---------------------------------------------------------------- 4. anti-template

class TestAntiTemplate:
    def test_stock_lines_fire_on_every_channel(self):
        text = ("That proportion is worth sitting with. The mountain remembers what it was. That stop isn't filler. "
                "The caldera is not a metaphor for the trip.")
        lines = [line for line, _ in h.template_lines(text)]
        assert lines == ["worth sitting with", "The mountain remembers", "isn't filler", "not a metaphor"]
        g = qg.gate_voice(text, spec=None)
        assert not g["passed"] and g["repairable"] and len(g["violations"]) == 4

    def test_knowing_that_in_advance_with_an_antecedent_passes(self):
        assert h.template_lines("Cameras stay behind — and knowing that in advance changes how you plan.") == []
        assert h.template_lines("Knowing them in advance makes the difference.")

    def test_a_brand_pack_phrase_in_the_sign_off_fires_and_an_angle_phrase_elsewhere_does_not(self):
        briefs = h.brief_texts({"core_idea": "Movement, energy, freedom and social discovery."},
                               {"best_final_style": "One brand sentence grounded in Trail Pulse's core idea of "
                                                    "movement and energy delivered well. Note the choice was "
                                                    "inseparable from the institution's purpose."})
        text = ("That choice was inseparable from the institution's purpose.\n\n"
                "Trail Pulse plans trips around movement and energy delivered well, with time to swim.")
        out = h.brief_phrases(text, briefs, brand_name="Trail Pulse")
        assert [s for s, _ in out] == ["Trail Pulse plans trips around movement and energy delivered well, with "
                                       "time to swim."]


# ---------------------------------------------------------------- 6. CTA literal

class TestCtaLiteral:
    def test_a_reworded_closing_line_is_replaced_and_the_brand_line_kept(self):
        text = ("Body about the lake.\n\nTerra Family Expeditions plans journeys for families who want active travel. "
                "If this route sounds right, ask Terra Family Expeditions to plan this journey for your family.\n\n"
                "## FAQ\n\n**Q: Is it cold?**\nA: Yes, at night.")
        out, log = h.ensure_cta(text, TERRA_CTA)
        assert "Terra Family Expeditions plans journeys for families" in out
        assert "Ask Terra Family Expeditions to plan this trip for your family.\n\n## FAQ" in out
        assert "restored" in log[0] and h.has_cta_literal(out, TERRA_CTA)

    def test_missing_cta_goes_before_the_faq_or_before_the_hashtags(self):
        blog, _ = h.ensure_cta("Body.\n\nSpeak to Terra about how this works.\n\n## FAQ\n\n**Q: Why?**\nA: Because.",
                               TERRA_CTA)
        assert blog.index(TERRA_CTA) < blog.index("## FAQ") and "Speak to Terra" in blog
        post, log = h.ensure_cta("Hook.\n\nBody.\n\n#Bhutan #Punakha", "Ask WildKind Travel for the full itinerary")
        assert post.endswith("Ask WildKind Travel for the full itinerary.\n\n#Bhutan #Punakha") and log

    def test_the_literal_line_is_left_alone_whatever_its_case_and_punctuation(self):
        text = "Body.\n\nTalk to an Atlas and Hearth travel designer about this journey!"
        assert h.ensure_cta(text, "Talk to an Atlas & Hearth travel designer about this journey") == (text, [])
        assert cr.check_cta(text, "Talk to an Atlas & Hearth travel designer about this journey", "linkedin").ok


# ---------------------------------------------------------------- gates + sentence repair routing (fix 7)

F9_FRAGMENT = {"gate": "F9_cta_fact", "passed": False, "blocking": True, "repairable": True, "violations": [
    'audit flagged: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "understood exactly what that meant" '
    '(claims: the founders\' intent; source: silent)']}
POST = ("Two rivers shape the peninsula where Punakha Dzong stands — and the people who placed it there understood "
        "exactly what that meant.\n\nZhabdrung Ngawang Namgyal built the dzong in the late 1630s at the point where "
        "the Pho Chhu and Mo Chhu converge. The founding purpose of the site has never been set aside. The Je Khenpo "
        "still relocates here each winter.\n\nAsk WildKind Travel for the full itinerary.\n\n#Bhutan")


class TestRouting:
    def test_a_mid_sentence_judge_quote_is_widened_to_its_sentence(self):
        """r2 #13: the F9 quote is the end of the hook; the repair must get the whole sentence."""
        items = sr.plan(POST, [F9_FRAGMENT])
        assert items and items[0]["start"] == 0 and items[0]["sentence"].startswith("Two rivers shape")

    def test_a_paraphrased_quote_finds_its_sentence_and_an_unfound_f9_claim_is_skipped(self):
        f9 = dict(F9_FRAGMENT, violations=[
            'audit flagged: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "The founding purpose of this site has '
            'never been set aside at all." (claims: still the seat; source: 1955); "A sentence not in the post at '
            'all, anywhere." (claims: x; source: y)'])
        items = sr.plan(POST, [f9])
        assert [i["sentence"] for i in items] == ["The founding purpose of the site has never been set aside."]

    def test_new_gates_are_sentence_repairable(self):
        moments_gate = {"gate": "F17_trip_fact", "passed": False, "blocking": True, "repairable": True,
                        "violations": ["gives a time the source does not ('the first night') — cut it: 'The Je "
                                       "Khenpo still relocates here each winter.'"]}
        rep = dict(moments_gate, gate="F18_rarity")
        assert sr.plan(POST, [moments_gate]) and sr.plan(POST, [rep])
        assert service._style_only_repair([moments_gate, rep])
        assert not service._style_only_repair([moments_gate, F9_FRAGMENT])

    def test_delete_flagged_never_cuts_the_hook_the_cta_or_an_add_item(self):
        items = sr.plan(POST, [F9_FRAGMENT])
        assert sr.delete_flagged(POST, items) is None  # the hook
        f1 = qg.gate_grounding(POST, "Built in 1637–1638.")
        cut = sr.delete_flagged(POST, sr.plan(POST, [f1]))
        assert cut is not None and "1630s" not in cut and "Two rivers" in cut
        add = [dict(sr.plan(POST, [f1])[0], problem="keep this sentence and add ONE short sentence")]
        assert sr.delete_flagged(POST, add) is None

    def test_run_quality_gates_reports_the_round3_gates(self):
        f8 = {"items": [{"criterion": c, "score": "1", "evidence": "q"} for c in qg.get_framework_rubric("promotion")]}
        f9 = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1", "failure_codes": [],
              "flagged_phrases": [], "notes": ""}
        raw = [{"text": __import__("json").dumps(d)} for d in (f8, f9)]
        text = "The Tsaatan herd reindeer. It is a place to spend the first night. " + TERRA_CTA + "."
        with patch.object(qg, "invoke_judge", side_effect=raw), patch.object(qg, "_log_t10_judge_call"):
            out = qg.run_quality_gates(content_text=text, atom_text=ROUTE_SEED, cta=TERRA_CTA, goal_key="promotion",
                                       brand_rubric_text="r", channel="facebook", hygiene_ctx=_hctx(),
                                       trip_ctx=_tctx())
        by = {g["gate"]: g for g in out["gate_ledger"]}
        assert not by["F17_trip_fact"]["passed"] and by["F17_trip_fact"]["blocking"]
        assert by["F18_rarity"]["passed"] and by["F13_repeat"]["passed"]


def _ctx():
    return {"atom_text": PUNAKHA_SEED, "goal": {"key": "promotion"}, "channel_style": {"channel": "linkedin"},
            "brand_audience": {"brand_name": "WildKind Travel"}, "chosen": {}, "cta":
            "Ask WildKind Travel for the full itinerary", "destination": None, "trip_name": None,
            "brand_rubric_text": "", "channel": "linkedin", "atom_id": "a1"}


def _outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


@pytest.mark.asyncio
async def test_attempt_three_cuts_the_flagged_sentence_when_the_repair_call_fails():
    """r2 #13 was held at attempt 2 on one F1 sentence. Attempt 2 repairs it; when the attempt-3 repair
    call fails, the flagged sentence is cut in code and the piece is checked again (not held)."""
    f1 = qg.gate_grounding(POST, "Built in 1637–1638.")
    row = {"piece_id": str(uuid.uuid4()), "attempt_number": 3, "status": "approved"}
    with patch.object(service, "write_content", return_value=(POST, 0.01, {}, None)), \
         patch.object(service, "repair_sentences", side_effect=[
             # the attempt-2 repair keeps the number (the gate flags the same sentence again) …
             (["Zhabdrung Ngawang Namgyal built the dzong in the late 1630s at the point where the Pho Chhu and Mo "
               "Chhu converge."], 0.002),
             service.SentenceRepairError("bad reply")]) as rep, \
         patch.object(service, "rewrite_with_feedback") as rw, \
         patch.object(service, "run_quality_gates",
                      side_effect=[_outcome([f1]), _outcome([f1]), _outcome([])]) as gates, \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value=row)) as fin:
        await service.run_write_background(uuid.uuid4(), uuid.uuid4(), _ctx(), pool=MagicMock())
    rw.assert_not_called()
    assert rep.call_count == 2
    final = gates.call_args_list[2].kwargs["content_text"]
    assert "1630s" not in final and final.startswith("Two rivers shape")
    kw = fin.call_args.kwargs
    assert kw["status"] == "approved" and kw["attempt_number"] == 3
    assert kw["repair_log"][-1]["repair_mode"] == "sentences_cut"


@pytest.mark.asyncio
async def test_the_job_restores_the_cta_and_gives_the_writer_the_date_span():
    text = "Punakha Dzong sits where two rivers meet.\n\nIf you like, ask WildKind Travel for more.\n\n#Bhutan"
    row = {"piece_id": str(uuid.uuid4()), "attempt_number": 1, "status": "approved"}
    ctx = _ctx()
    with patch.object(service, "write_content", return_value=(text, 0.01, {}, None)) as wc, \
         patch.object(service, "run_quality_gates", return_value=_outcome([])) as gates, \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value=row)):
        await service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    checked = gates.call_args.kwargs["content_text"]
    assert "Ask WildKind Travel for the full itinerary.\n\n#Bhutan" in checked
    assert "for 318 years (1637–1955)" in wc.call_args.kwargs["content_seed"]
    assert gates.call_args.kwargs["trip_ctx"] is not None and "pack" in gates.call_args.kwargs["brief_texts"]
