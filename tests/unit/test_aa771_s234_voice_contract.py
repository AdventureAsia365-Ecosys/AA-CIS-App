"""AA-771 (S234 PR3) — T8 angle contract (sanitiser + thin anchor) and the LinkedIn register:
channel spec by brand register, T9 VOICE / length lines, F14_social_shape, F15_voice, F9 voice,
and the guard that blog writer prompts stay byte-identical."""
import hashlib
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers.admin_content_quality import highlights
from services.acp_angle_gate import generate as angle_generate
from services.acp_angle_gate import prompts as angle_prompts
from services.acp_angle_gate import service as angle_service
from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import GOALS, get_goal
from services.acp_angle_gate.ranking import rank_angles
from services.acp_angle_gate.route_digest import angle_anchor, render_digest, route_moments, thin_anchor
from services.acp_content_writing import hygiene, quality_gates as qg, sentence_repair
from services.acp_content_writing import service as writing_service
from services.acp_content_writing.prompts import SYSTEM_PROMPT, build_user_prompt
from services.acp_shared.channel_spec import (CONSUMER_SOCIAL, EDITORIAL, OBSERVATIONAL, WARM_PRACTICAL,
                                              brand_register, channel_spec, linkedin_spec, post_words,
                                              sanitize_best_final_style)

# The 4 pilot tenants' T0 fields (S233 pieces_r1.json brand_pack), trimmed to what the rules read.
TERRA = {"brand_name": "Terra Family Expeditions", "brand_type": "Premium family adventure travel brand",
         "customer_segment": "Affluent families with children aged 7–16",
         "voice": ["Warm", "Reassuring", "Clear", "Practical", "Family-aware", "Encouraging", "Trustworthy"]}
TRAIL = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
         "customer_segment": "25–40 years old\nSolo travellers, couples, small groups of friends",
         "voice": ["Energetic", "Fresh", "Direct", "Friendly", "Modern", "Confident", "Lightly playful"]}
AH = {"brand_name": "Atlas & Hearth", "brand_type": "Luxury cultural travel brand",
      "customer_segment": "High-net-worth couples, senior executives, luxury travel advisors",
      "voice": ["Elegant", "Discreet", "Cultured", "Calm", "Precise", "Atmospheric", "Intelligent"]}
WILDKIND = {"brand_name": "WildKind Travel", "brand_type": "Responsible nature and conservation travel brand",
            "customer_segment": "Eco-conscious travellers, nature lovers",
            "voice": ["Thoughtful", "Grounded", "Respectful", "Nature-led", "Ethical", "Calm", "Informative"]}

GOAL = get_goal(GOALS[0]["key"])


# ---------------------------------------------------------------- channel spec / register

class TestLinkedinSpec:
    @pytest.mark.parametrize("pack, register", [
        (TRAIL, CONSUMER_SOCIAL), (AH, EDITORIAL), (TERRA, WARM_PRACTICAL), (WILDKIND, OBSERVATIONAL)])
    def test_pilot_registers(self, pack, register):
        assert brand_register(pack) == register

    def test_word_bands(self):
        assert linkedin_spec(TRAIL).words == (120, 250)
        assert linkedin_spec(AH).words == (120, 300)
        assert linkedin_spec(TRAIL).target_words == (150, 220)
        assert linkedin_spec(TRAIL).hashtags_max == 3 and linkedin_spec(TRAIL).first_line_max == 140
        assert linkedin_spec(TRAIL).block_words == 325 and linkedin_spec(AH).block_words == 390

    def test_only_linkedin_has_a_spec(self):
        assert channel_spec("linkedin", TRAIL) is not None
        for ch in ("blog", "facebook", "instagram", "email", None):
            assert channel_spec(ch, TRAIL) is None

    def test_non_editorial_registers_are_traveller_facing(self):
        for pack in (TRAIL, TERRA, WILDKIND):
            assert "never to travel professionals" in linkedin_spec(pack).use_when
        assert "travel professionals" not in linkedin_spec(AH).use_when

    def test_shared_linkedin_row_has_no_calm_or_b2b(self):
        row = get_channel_style("linkedin")
        assert "calm" not in row["style"].lower()
        assert "B2B" not in row["use_when"] and "partner" not in row["use_when"]

    def test_avoid_list_items_have_no_commas_inside(self):
        # ranking.split_avoid_phrases splits on commas — every stock phrase must survive as one item
        from services.acp_angle_gate.ranking import split_avoid_phrases
        phrases = split_avoid_phrases(linkedin_spec(TRAIL).avoid)
        assert "worth sitting with" in phrases and "Not as X. Not as Y." in phrases


# ---------------------------------------------------------------- sanitiser

class TestSanitizeBestFinalStyle:
    def test_strips_calm_voice_for_trail(self):
        brief = ("Calm, considered LinkedIn voice — direct sentences, no hype. Lead immediately with the "
                 "turquoise pools on Day 2.")
        clean, stripped = sanitize_best_final_style(brief, TRAIL)
        assert "Calm" not in clean and stripped
        assert clean.startswith("Lead immediately with the turquoise pools")

    def test_strips_quietly_authoritative_for_terra(self):
        brief = ("Tone: calm, considered, quietly authoritative — the voice of someone who has thought hard. "
                 "Move through the real shape of the day.")
        clean, stripped = sanitize_best_final_style(brief, TERRA)
        assert clean == "Move through the real shape of the day."
        assert stripped[0].startswith("Tone: calm, considered, quietly authoritative")

    def test_keeps_calm_tone_for_atlas_hearth(self):
        brief = "Tone: calm, intelligent, slightly contemplative — not mystical. Anchor the question in the rivers."
        assert sanitize_best_final_style(brief, AH) == (brief, [])

    def test_strips_open_with_sentences_for_every_brand(self):
        brief = "Open with the geography as a thinking problem: why here? Move to the founder."
        for pack in (TRAIL, AH, TERRA, WILDKIND):
            clean, stripped = sanitize_best_final_style(brief, pack)
            assert clean == "Move to the founder." and len(stripped) == 1

    def test_keeps_the_then_part_of_an_opening_sentence(self):
        clean, _ = sanitize_best_final_style("Open with the checkpoint, then explain why preparation matters.", TERRA)
        assert clean == "Explain why preparation matters."

    def test_strips_professional_audience_for_non_editorial_only(self):
        brief = ("Lead with the pools. Close with a light CTA inviting professionals planning active travel "
                 "to rethink pacing.")
        assert sanitize_best_final_style(brief, TRAIL)[0] == "Lead with the pools."
        assert sanitize_best_final_style(brief, AH) == (brief, [])

    def test_negated_tone_is_not_flagged(self):
        brief = "Direct sentences, no hype, never calm or sleepy. Lead with the pools."
        assert sanitize_best_final_style(brief, TRAIL) == (brief, [])

    def test_calm_as_scene_detail_is_kept(self):
        brief = "Lead with the calm water of the lake at dawn."
        assert sanitize_best_final_style(brief, TRAIL) == (brief, [])

    def test_never_empty(self):
        clean, stripped = sanitize_best_final_style("Open with the date 1937.", TRAIL)
        assert clean and stripped


# ---------------------------------------------------------------- T8 prompt

class TestT8Prompt:
    def test_definition_no_longer_asks_for_tone_or_opening(self):
        start = angle_prompts.SYSTEM_PROMPT.index("- best_final_style:")
        definition = angle_prompts.SYSTEM_PROMPT[start:angle_prompts.SYSTEM_PROMPT.index("- answers:")]
        assert "tone" not in definition.lower()
        assert "opening approach" not in definition
        assert "never sets a voice" in definition and 'no "Open with' in definition

    def test_thin_moment_rule_in_system_prompt(self):
        assert "ANCHOR CANDIDATES" in angle_prompts.SYSTEM_PROMPT and "THIN MOMENTS" in angle_prompts.SYSTEM_PROMPT

    def test_route_header_and_voice_line(self):
        p = angle_prompts.build_user_prompt(content_seed="ROUTE (…)", goal=GOAL, brand_audience=TRAIL,
                                            destination=None, trip_name=None, is_route=True)
        assert p.startswith("CONTENT SEED (a Route")
        assert "VOICE (from the BRAND block" in p and "Energetic, Fresh" in p
        single = angle_prompts.build_user_prompt(content_seed="atom", goal=GOAL, brand_audience={},
                                                 destination=None, trip_name=None)
        assert single.startswith("CONTENT SEED (from a curated atom") and "VOICE (" not in single


# ---------------------------------------------------------------- route digest / thin anchor

RUINS = ("ruins of an ancient monastery — visit\nLocation: ruins of an ancient monastery, Mongolia\n"
         "Day 14 of the trip\n"
         "Source itinerary: visit the ruins of an ancient monastery destroyed during the political purges of 1937.\n"
         "Thin moment: only 1 source fact — mention it in one sentence at most; never make it the hook, the title "
         "or the climax.")
URAN = ("Uran Togoo Volcano\nLocation: Uran Togoo Volcano, Bulgan, Mongolia\nDay 3 of the trip\n"
        "Source itinerary: hike to the crater of the extinct Uran Togoo volcano.\n"
        "Place background (checked facts):\n- It is the centerpiece of a natural monument.\n- It last erupted long ago."
        "\n- Its crater is about 500 m wide.")
LAKE = ("Lake Khövsgöl — horse riding\nLocation: Lake Khövsgöl, Khövsgöl, Mongolia\nDay 5 of the trip\n"
        "Source itinerary: ride horses along the shore.\nPlace background (checked facts):\n- A large freshwater lake."
        "\n- Near the Russian border.\n- One of the deepest lakes in Asia.")
SEGMENTS = [("a_uran", URAN), ("a_lake", LAKE), ("a_ruins", RUINS)]


def _angle(name, style="Lead with the lake shore."):
    return {"name": name, "why_it_works": "w", "formula_fit": "f", "best_final_style": style}


class TestRouteDigest:
    def test_moments_fact_counts_and_thin(self):
        ms = {m["atom_id"]: m for m in route_moments(SEGMENTS)}
        assert ms["a_ruins"]["fact_count"] == 1 and ms["a_ruins"]["thin"]
        assert ms["a_uran"]["fact_count"] == 4 and not ms["a_uran"]["thin"]
        assert ms["a_ruins"]["day"] == 14

    def test_digest_lists_anchor_candidates_and_thin_moments(self):
        digest = render_digest(route_moments(SEGMENTS))
        assert "Day 14 · ruins of an ancient monastery — visit · 1 fact" in digest
        assert "ANCHOR CANDIDATES" in digest and "Uran Togoo Volcano" in digest.split("ANCHOR CANDIDATES")[1]
        assert "THIN MOMENTS" in digest and "ruins" in digest.split("THIN MOMENTS")[1]

    def test_anchor_by_name_and_by_lead_sentence(self):
        ms = route_moments(SEGMENTS)
        assert angle_anchor(_angle("What One Ruined Monastery Tells Families"), ms)["atom_id"] == "a_ruins"
        assert angle_anchor(_angle("What Silence Leaves Behind",
                                   "Lead with the ruins themselves as a physical fact on Day 14."),
                            ms)["atom_id"] == "a_ruins"
        assert angle_anchor(_angle("Uran Togoo Before Breakfast"), ms)["atom_id"] == "a_uran"
        assert thin_anchor(_angle("Uran Togoo Before Breakfast"), ms) is None
        assert angle_anchor(_angle("A Morning Climb", "Lead with the lake shore."), ms)["atom_id"] == "a_lake"
        assert thin_anchor(_angle("What One Ruined Monastery Tells Families"), ms) == \
            "ruins of an ancient monastery — visit (1 fact)"

    def test_contract_violation_only_when_all_three_are_thin(self):
        ms = route_moments(SEGMENTS)
        thin = _angle("What One Ruined Monastery Tells Families")
        good = _angle("Uran Togoo Before Breakfast")
        assert angle_generate.angle_contract_violations([thin, thin, thin], {}, ms)
        assert angle_generate.angle_contract_violations([thin, good, thin], {}, ms) == []
        assert angle_generate.angle_contract_violations([thin, thin, thin], {}) == []  # no moments: no check

    def test_ranking_puts_a_thin_anchor_last(self):
        angles = [_angle("Ruins"), _angle("Crater"), _angle("Lake")]
        evidence, best = rank_angles(angles, claimed_answers=[["Q1", "Q2"], [], []], asked_questions=["Q1", "Q2"],
                                     avoid_text="", thin_anchors=["ruins (1 fact)", None, None])
        assert best == 1  # the thin angle answers 2 questions but still loses
        assert evidence[0].thin_anchor and evidence[0].violations == []  # never in the tenant-visible list

    @pytest.mark.asyncio
    async def test_generate_retries_once_when_all_three_are_thin(self):
        ms = route_moments(SEGMENTS)
        thin = [_angle("What One Ruined Monastery Tells Families")] * 3
        good = [_angle("Uran Togoo Before Breakfast"), _angle("Lake Khovsgol by Horse"),
                _angle("A Volcano Morning", "Lead with the crater on Day 3.")]
        once = AsyncMock(side_effect=[([dict(a) for a in thin], 0, "r", 0.02), ([dict(a) for a in good], 1, "r", 0.02)])
        with patch.object(angle_generate, "_generate_once", new=once):
            angles, idx, _, cost = await angle_generate.generate_angles(content_seed="d", goal=GOAL,
                                                                        brand_audience={}, moments=ms)
        assert once.await_count == 2
        assert "thin moment" in once.await_args_list[1].kwargs["revision_feedback"][0]
        assert cost == pytest.approx(0.04)
        assert [a["thin_anchor"] for a in angles] == [None, None, None]

    @pytest.mark.asyncio
    async def test_no_retry_when_one_angle_is_fine(self):
        ms = route_moments(SEGMENTS)
        mixed = [_angle("What One Ruined Monastery Tells Families"), _angle("Uran Togoo Before Breakfast"),
                 _angle("Lake Khovsgol by Horse")]
        once = AsyncMock(return_value=(mixed, 0, "r", 0.02))
        with patch.object(angle_generate, "_generate_once", new=once):
            angles, *_ = await angle_generate.generate_angles(content_seed="d", goal=GOAL, brand_audience={},
                                                              moments=ms)
        assert once.await_count == 1
        assert angles[0]["thin_anchor"] and angles[1]["thin_anchor"] is None


# ---------------------------------------------------------------- T8 service

class _TxnCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _pool(conn):
    conn.transaction = MagicMock(return_value=_TxnCM())
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _req(**over):
    base = {"request_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "atom_id": "a_ruins", "trip_id": uuid.uuid4(),
            "channel": "linkedin", "goal": None, "cta": None, "status": "pending_goal", "dfs_paa_snapshot": None,
            "route_segment_ids": None, "subject_id": None, "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc)}
    base.update(over)
    return base


@pytest.mark.asyncio
class TestT8Service:
    async def _run(self, req, angles, pack, route=None):
        conn = AsyncMock()
        gen = AsyncMock(return_value=(angles, 0, "r", 0.02))
        with patch.object(angle_service, "_fetch_request_row", new=AsyncMock(return_value=req)), \
             patch.object(angle_service, "ensure_place_cards", new=AsyncMock()), \
             patch.object(angle_service, "_fetch_atom_for_tenant",
                          new=AsyncMock(return_value={"atom_id": "a_ruins", "trip_id": None, "text": "atom seed"})), \
             patch.object(angle_service, "fetch_brand_audience", new=AsyncMock(return_value=pack)), \
             patch.object(angle_service, "fetch_tenant_trips", new=AsyncMock(return_value=[])), \
             patch.object(angle_service, "fetch_search_demand_signal", new=AsyncMock(return_value=None)), \
             patch.object(angle_service, "fetch_piece_history", new=AsyncMock(return_value=[])), \
             patch.object(angle_service, "verify_answers",
                          new=AsyncMock(side_effect=lambda a, ev, t: (ev, 0, 0))), \
             patch.object(angle_service, "_route_moments", new=AsyncMock(return_value=route)), \
             patch.object(angle_service, "generate_angles", new=gen), \
             patch.object(angle_service, "fetch_request", new=AsyncMock(return_value={})):
            await angle_service.set_goal_and_generate(req["tenant_id"], req["request_id"], "promotion", _pool(conn))
        inserts = [c[0] for c in conn.execute.call_args_list if "angle_gate_option" in c[0][0]]
        return gen, inserts

    async def test_brief_is_sanitised_before_it_is_saved(self):
        angles = [_angle(f"A{i}", "Calm, considered LinkedIn voice — direct. Open with the pools. Lead with Day 2.")
                  for i in range(3)]
        _, inserts = await self._run(_req(), angles, TRAIL)
        assert [args[6] for args in inserts] == ["Lead with Day 2."] * 3

    async def test_linkedin_channel_block_follows_the_register(self):
        gen, _ = await self._run(_req(), [_angle(f"A{i}") for i in range(3)], TRAIL)
        style = gen.await_args.kwargs["channel_style"]
        assert "never to travel professionals" in style["use_when"]
        assert "worth sitting with" in style["avoid"]
        assert "moments" not in gen.await_args.kwargs  # not a route request

    async def test_route_request_gets_the_digest_and_moments(self):
        ms = route_moments(SEGMENTS)
        gen, _ = await self._run(_req(channel="blog", route_segment_ids=["s1", "s2", "s3"]),
                                 [_angle(f"A{i}") for i in range(3)], AH, route=ms)
        kw = gen.await_args.kwargs
        assert kw["is_route"] is True and kw["moments"] is ms
        assert kw["content_seed"].startswith("ROUTE (")


# ---------------------------------------------------------------- T9 prompt

ANGLE = {"name": "Pools", "why_it_works": "w", "formula_fit": "f",
         "best_final_style": "Calm, considered LinkedIn voice. Open with the pools. Lead with Day 2."}


def _t9(channel, pack=TRAIL, **kw):
    return build_user_prompt(content_seed="Kuang Si — swim\nSource itinerary: Swim in the pools.", goal=GOAL,
                             channel_style=get_channel_style(channel), brand_audience=pack, angle=ANGLE,
                             cta="Ask us", **kw)


class TestT9Prompt:
    def test_linkedin_prompt_has_voice_and_numbers(self):
        p = _t9("linkedin")
        assert "CHANNEL STYLE" not in p
        assert "VOICE (from the brand — the channel and the angle never override it): Energetic, Fresh" in p
        assert "150–220 words (never more than 250)" in p
        assert "at most 140 characters" in p and "At most 3 hashtags" in p
        channel_lines = p.split("GOAL:")[0]
        assert "calm" not in channel_lines.lower()

    def test_linkedin_prompt_gets_the_sanitised_brief(self):
        p = _t9("linkedin")
        assert "ANGLE'S BEST FINAL STYLE: Lead with Day 2." in p

    def test_other_social_channel_keeps_its_style_but_gets_the_clean_brief(self):
        p = _t9("facebook")
        assert "CHANNEL STYLE: human, warm" in p and "LENGTH AND SHAPE" not in p
        assert "ANGLE'S BEST FINAL STYLE: Lead with Day 2." in p

    def test_sibling_openers_listed_for_social_not_blog(self):
        p = _t9("linkedin", sibling_openers=["There is a checkpoint before the temples."])
        assert "EARLIER PIECES FROM THIS BRIEF OPENED WITH" in p and "There is a checkpoint" in p

    def test_blog_prompts_are_byte_identical(self):
        """Guard: hashes computed on the PR2 base (feat/aa768-hygiene-scope-seed) before any AA-771
        S234 edit. Any change to a blog writer prompt must be deliberate and update these."""
        pack = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
                "core_idea": "Active trips for people who like to move.", "customer_segment": "25-40 years old",
                "voice": ["Energetic", "Fresh", "Direct", "Friendly", "Modern", "Confident", "Lightly playful"],
                "style_guide": "Focus on the group.", "forbidden_words": ["luxury"]}
        angle = {"name": "What the Ruins Hold", "why_it_works": "Grounds the route.", "formula_fit": "PAS",
                 "best_final_style": "Calm, considered voice. Open with the ruins on Day 14. Lead with the lake."}
        single = build_user_prompt(
            content_seed="Paro Taktsang — hike\nSource itinerary: Hike up.", goal=GOAL,
            channel_style=get_channel_style("blog"), brand_audience=pack, angle=angle, cta="Ask us",
            destination="Bhutan", trip_name="Best of Bhutan", atom_id="a1", keyword="tigers nest hike",
            facts_text="[Fact id=f1] Visa needed.", scope="SCOPE: one moment.",
            sibling_openers=["There is a lake."])
        route = build_user_prompt(
            content_seed="x", goal=GOAL, channel_style=get_channel_style("blog"), brand_audience=pack, angle=angle,
            cta="Ask us", route_segments=[("a1", "Lake"), ("a2", "Ruins")], revision_feedback=["F1: fix"],
            keyword=None)

        def sha(s):
            return hashlib.sha256(s.encode()).hexdigest()
        assert GOAL["key"] == GOALS[0]["key"]
        assert sha(SYSTEM_PROMPT) == "be2787f580be157dccf3ed5ae5f7183d6e7bbd2f1a78a747089b1031806b8cba"
        assert sha(single) == "6dc220ac252478f0947e4644db81aae5c26d3aa6f5a25b5190540f097979aed3"
        assert sha(route) == "d2619b3f8f456ecb0ae02abc762d22dcefcfd4af42a1bdf7ef81dacb3b7acdca"


# ---------------------------------------------------------------- hygiene with the spec (finalize_social)

class TestHygieneWithSpec:
    def _ctx(self, seed="Paro Taktsang — hike\nLocation: Paro Taktsang, Paro, Bhutan"):
        spec = linkedin_spec(TERRA)
        return hygiene.HygieneContext(seed=seed, brand_name="Terra Family Expeditions",
                                      hashtags_max=spec.hashtags_max, first_line_max=spec.first_line_max)

    def test_eight_hashtags_become_three_on_the_last_line(self):
        text = ("Short hook.\n\nBody text here.\n\n#Bhutan #ParoTaktsang #FamilyTravel #Hiking #Monastery "
                "#Asia #Culture #Travel")
        out, log = hygiene.autofix(text, self._ctx())
        assert out.rstrip().split("\n")[-1] == "#Bhutan #ParoTaktsang #FamilyTravel"
        assert any("kept 3 of 8" in line for line in log)

    def test_misspelt_place_hashtag_is_fixed(self):
        out, _ = hygiene.autofix("Hook.\n\nBody.\n\n#PakoTaktsang #Bhutan", self._ctx())
        assert "#ParoTaktsang" in out and "#PakoTaktsang" not in out

    def test_long_first_line_is_split_after_its_first_sentence(self):
        first = "There is a checkpoint before you reach the inner temples of Paro Taktsang."
        text = (first + " Cameras stay behind at the gate. Modest dress is required for every visitor. "
                "The path climbs through pine forest to the cliff.\n\nMore.")
        out, log = hygiene.autofix(text, self._ctx())
        assert out.split("\n")[0] == first and out.split("\n")[1] == ""
        assert "split the first line" in " ".join(log)

    def test_repaired_text_with_six_tags_ends_with_three(self):
        # autofix runs after the last LLM step (repair included) — the cap holds whatever the repair wrote
        repaired = "Hook.\n\nBody.\n\n#A1 #B2 #C3 #D4 #E5 #F6"
        out, _ = hygiene.autofix(repaired, self._ctx(seed="x"))
        assert out.rstrip().endswith("#A1 #B2 #C3")

    def test_no_spec_leaves_layout_alone(self):
        text = "Hook.\n\n#A1 #B2 #C3 #D4\n\nBody after tags."
        out, _ = hygiene.autofix(text, hygiene.HygieneContext(seed="x"))
        assert out == text


# ---------------------------------------------------------------- F14 / F15

def _words(n):
    """A post of n words with a short first line."""
    return "Hook line.\n\n" + " ".join(["walk"] * (n - 2)) + "."


class TestF14SocialShape:
    def test_band_warn_and_block(self):
        spec = linkedin_spec(TRAIL)
        assert qg.gate_social_shape(_words(200), spec)["passed"]
        warn = qg.gate_social_shape(_words(260), spec)
        assert not warn["passed"] and not warn["blocking"]
        block = qg.gate_social_shape(_words(330), spec)
        assert block["blocking"] and block["repairable"] and "rewrite it to 150–220 words" in block["violations"][0]

    def test_editorial_band_is_wider(self):
        assert qg.gate_social_shape(_words(280), linkedin_spec(AH))["passed"]

    def test_hashtags_and_first_line_residue_are_quoted(self):
        text = "x" * 150 + "\n\n" + _words(150) + "\n\n#A #B #C #D"
        v = qg.gate_social_shape(text, linkedin_spec(TRAIL))["violations"]
        assert any(s.startswith("4 hashtags, more than 3: '#A #B #C #D'") for s in v)
        assert any(s.startswith("first line is 150 characters, over 140") for s in v)

    def test_hashtags_do_not_count_as_words(self):
        assert post_words("One two three. #Four #Five") == 3

    def test_no_spec_passes(self):
        r = qg.gate_social_shape(_words(900), None)
        assert r["passed"] and not r["blocking"]


class TestF15Voice:
    spec = linkedin_spec(TRAIL)

    def test_stock_phrase_blocks_and_goes_to_sentence_repair(self):
        text = "Day 2 ends at the pools. That detail is worth sitting with. Swim, rest, then lunch."
        r = qg.gate_voice(text, spec=self.spec, brand_pack=TRAIL)
        assert r["blocking"] and r["repairable"]
        assert r["violations"][0].endswith("'That detail is worth sitting with.'")
        items = sentence_repair.plan(text, [r])
        assert items and items[0]["sentence"] == "That detail is worth sitting with."

    def test_not_as_pair_is_one_quoted_span(self):
        text = "Rest is built in. Not as filler. Not as an afterthought. It is the plan."
        r = qg.gate_voice(text, spec=self.spec, brand_pack=TRAIL)
        assert any("'Not as filler. Not as an afterthought.'" in v for v in r["violations"])
        items = sentence_repair.plan(text, [r])
        assert items and text[items[0]["start"]:items[0]["end"]] == "Not as filler. Not as an afterthought."

    def test_b2b_register_warns_for_trail_not_for_atlas_hearth(self):
        text = "Active travel brands have a pacing problem. The pools on Day 2 fix it."
        r = qg.gate_voice(text, spec=self.spec, brand_pack=TRAIL)
        assert not r["blocking"] and any(v.startswith("writes to the travel trade") for v in r["violations"])
        assert qg.gate_voice(text, spec=linkedin_spec(AH), brand_pack=AH)["passed"]

    def test_segment_leak_warns_unless_the_source_has_it(self):
        text = "We plan trips for clients in the 25–40 range. The pools are on Day 2."
        source = "Kuang Si pools\n\nAUDIENCE (who the brand writes for — not a fact about this trip): 25–40 years old"
        r = qg.gate_voice(text, spec=self.spec, brand_pack=TRAIL, atom_text=source)
        assert any("audience definition ('25–40')" in v for v in r["violations"])
        r2 = qg.gate_voice(text, spec=self.spec, brand_pack=TRAIL, atom_text="Group of 25-40 riders on Day 2.")
        assert not any("audience definition" in v for v in r2["violations"])

    def test_generic_and_repeated_openers_warn(self):
        terra_opener = "There is a checkpoint before you reach the inner temples of Paro Taktsang."
        r = qg.gate_voice(terra_opener + " Cameras stay behind.", spec=linkedin_spec(TERRA), brand_pack=TERRA,
                          sibling_openers=[terra_opener])
        kinds = [v.split(":")[0] for v in r["violations"]]
        assert any(k.startswith("generic 'There is a") for k in kinds)
        assert any(k.startswith("opens like an earlier piece") for k in kinds)
        assert not r["blocking"]

    def test_clean_post_passes_and_other_channels_skip(self):
        text = "Day 2 ends at turquoise pools. Swim, rest, eat. Ask us for the dates."
        assert qg.gate_voice(text, spec=self.spec, brand_pack=TRAIL)["passed"]
        assert qg.gate_voice("That is worth sitting with today.", spec=None)["passed"]

    def test_run_quality_gates_wires_both_for_linkedin(self):
        text = "Active travel brands have a pacing problem. That detail is worth sitting with."
        with patch.object(qg, "gate_framework", return_value=qg._result("F8_framework", [])), \
             patch.object(qg, "gate_brand_voice", return_value=[qg._result("F9_cta_fact", []),
                                                                 qg._result("F9_brand_style", [], blocking=False)]):
            out = qg.run_quality_gates(content_text=text, atom_text="seed", cta="Ask us", goal_key="promotion",
                                       brand_rubric_text="r", channel="linkedin", brand_pack=TRAIL)
        gates = {g["gate"]: g for g in out["gate_ledger"]}
        assert not gates["F14_social_shape"]["passed"]  # 13 words, under the band
        assert gates["F15_voice"]["blocking"] and out["first_failure"]["gate"] == "F15_voice"


# ---------------------------------------------------------------- F9 voice

class TestF9Voice:
    def _prompt(self, **kw):
        captured = {}

        def fake_judge(system, user, **_):
            captured["user"] = user
            return {"text": '{"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1", '
                            '"failure_codes": [], "flagged_phrases": [], "notes": ""}'}
        with patch.object(qg, "invoke_judge", side_effect=fake_judge), patch.object(qg, "_log_t10_judge_call"):
            qg.gate_brand_voice("piece", "Ask us", "rubric", source_text="seed", **kw)
        return captured["user"]

    def test_no_calm_house_style(self):
        assert "Do not flag calm" not in self._prompt()
        assert "calm and precise" not in self._prompt()

    def test_voice_adjectives_reach_the_judge(self):
        p = self._prompt(voice=TRAIL["voice"])
        assert "BRAND VOICE (judge the tone against these words" in p and "Energetic, Fresh" in p
        assert "BRAND VOICE" not in self._prompt()


# ---------------------------------------------------------------- admin highlights

class TestHighlights:
    def test_f15_and_f14_quotes_are_highlighted(self):
        text = "x" * 150 + "\n\nDay 2 ends at the pools. That detail is worth sitting with."
        f15 = qg.gate_voice(text, spec=linkedin_spec(TRAIL), brand_pack=TRAIL)
        f14 = qg.gate_social_shape(text, linkedin_spec(TRAIL))
        hs = highlights(text, [f15, f14], [])
        gates = {h["gate"] for h in hs}
        assert {"F15_voice", "F14_social_shape"} <= gates
        h = next(h for h in hs if h["gate"] == "F15_voice")
        assert text[h["start"]:h["end"]] == "That detail is worth sitting with."


# ---------------------------------------------------------------- T9 service

@pytest.mark.asyncio
class TestSiblingOpeners:
    async def test_first_sentences_deduplicated(self):
        conn = AsyncMock()
        conn.fetch.return_value = [{"head": "There is a checkpoint. Cameras stay."},
                                   {"head": "There is a checkpoint. Bags stay."}, {"head": None}]
        out = await writing_service._sibling_openers(uuid.uuid4(), uuid.uuid4(), "a1", "linkedin", _pool(conn))
        assert out == ["There is a checkpoint."]
        sql, *args = conn.fetch.await_args.args
        assert "angle_gate_request_id = $2::uuid" in sql and args[3] == "linkedin"

    async def test_blog_and_errors_give_nothing(self):
        conn = AsyncMock()
        assert await writing_service._sibling_openers(uuid.uuid4(), uuid.uuid4(), "a1", "blog", _pool(conn)) == []
        conn.fetch.assert_not_awaited()
        conn.fetch.side_effect = RuntimeError("db down")
        assert await writing_service._sibling_openers(uuid.uuid4(), uuid.uuid4(), "a1", "linkedin",
                                                      _pool(conn)) == []
