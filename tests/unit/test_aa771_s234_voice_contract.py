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

    def test_open_with_becomes_lead_with_for_every_brand(self):
        brief = "Open with the geography as a thinking problem: why here? Move to the founder."
        for pack in (TRAIL, AH, TERRA, WILDKIND):
            clean, stripped = sanitize_best_final_style(brief, pack)
            assert clean == "Lead with the geography as a thinking problem: why here? Move to the founder."
            assert stripped == ["Open with"]

    def test_keeps_the_then_part_of_an_opening_sentence(self):
        clean, _ = sanitize_best_final_style("Open with the checkpoint, then explain why preparation matters.", TERRA)
        assert clean == "Lead with the checkpoint, then explain why preparation matters."

    def test_literal_first_line_is_cut(self):
        brief = 'Opening line: "The water is not supposed to be that colour." Then explain the limestone.'
        assert sanitize_best_final_style(brief, TRAIL)[0] == "Explain the limestone."
        clean, stripped = sanitize_best_final_style('Open with "Nobody warns you about the cold."', TRAIL)
        assert clean == "Lead with the most concrete details from the content seed." and stripped

    # S234 review — real stored S232/S233 briefs: the seed detail and what follows it survive.
    @pytest.mark.parametrize("pack,brief,clean", [
        (WILDKIND, "Open with the confluence itself: the Pho Chhu and Mo Chhu meeting at the peninsula where the "
                   "dzong stands. Build one short paragraph on what that siting meant.",
         "Lead with the confluence itself: the Pho Chhu and Mo Chhu meeting at the peninsula where the dzong "
         "stands. Build one short paragraph on what that siting meant."),
        (TRAIL, "Open with the question … why is the water that colour? Answer it directly using the limestone.",
         "Lead with the question … why is the water that colour? Answer it directly using the limestone."),
        (AH, "Open with the question of why the dzong stands here. Anchor the question in the rivers.",
         "Lead with the question of why the dzong stands here. Anchor the question in the rivers."),
        (TERRA, "Do not open with the bath as a reward or treat; frame both as intentional planning decisions.",
         "Do not lead with the bath as a reward or treat; frame both as intentional planning decisions."),
        (TRAIL, "Start with the trek to the pools, the swim and the rest, and finish at the café.",
         "Lead with the trek to the pools, the swim and the rest, and finish at the café."),
        (TRAIL, "Begin on Day 2 at the limestone pools; keep the swim central.",
         "Lead with Day 2 at the limestone pools; keep the swim central."),
        (TRAIL, "Start with the climb to the monastery on Day 3.", "Lead with the climb to the monastery on Day 3."),
    ])
    def test_opening_keeps_the_seed_detail(self, pack, brief, clean):
        assert sanitize_best_final_style(brief, pack)[0] == clean

    def test_lead_with_open_water_is_not_an_opening(self):
        assert sanitize_best_final_style("Lead with open water on Day 2.", TRAIL) == ("Lead with open water on Day 2.", [])

    @pytest.mark.parametrize("pack,brief", [
        (TRAIL, "Mention that local boat operators run the crossing at dawn, then the lake."),
        (TRAIL, "Reassure parents that guides and planners handle the altitude days."),
        (TERRA, "Speak to parents and family trip planners who worry about pacing for kids aged 7-16."),
    ])
    def test_trip_content_operators_and_planners_are_kept(self, pack, brief):
        assert sanitize_best_final_style(brief, pack) == (brief, [])

    @pytest.mark.parametrize("pack,brief", [
        (TERRA, "Tone: warm but analytically grounded — this should read like a thoughtful operator reflecting "
                "on trip design."),
        (TRAIL, "End with a light CTA asking LinkedIn readers how they think about sequencing when they plan or "
                "recommend active travel."),
        (TRAIL, "Close with a reflective question for the LinkedIn audience about what it means to really know a "
                "destination before you put travellers in it."),
    ])
    def test_trade_facing_briefs_are_stripped(self, pack, brief):
        clean, stripped = sanitize_best_final_style("Lead with the pools. " + brief, pack)
        assert clean == "Lead with the pools." and stripped == [brief]

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
        clean, stripped = sanitize_best_final_style("Open with \"1937.\"", TRAIL)
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

    # S234 review — the T8 prompt asks angles to keep thin moments out of the lead in exactly these words.
    @pytest.mark.parametrize("brief", [
        "Keep the ruins to one sentence at most; lead with the horse ride on Lake Khovsgol.",
        "Avoid anchoring on the ruins. Lead with the lake crossing.",
        "Lead with the horse ride on Lake Khovsgol, not the ruins.",
        "Do not open with the ruins; start on the lake shore instead.",
        "Mention the ruins only briefly. Lead with the lake shore.",
    ])
    def test_thin_moment_kept_out_of_the_lead_is_not_the_anchor(self, brief):
        ms = route_moments(SEGMENTS)
        assert angle_anchor(_angle("Four days on the water", brief), ms)["atom_id"] == "a_lake"
        assert thin_anchor(_angle("Four days on the water", brief), ms) is None

    def test_moment_after_the_lead_verb_wins(self):
        ms = route_moments(SEGMENTS)
        brief = "After the ruins on Day 14 the route ends, so lead with the Uran Togoo crater."
        assert angle_anchor(_angle("Four days", brief), ms)["atom_id"] == "a_uran"
        assert angle_anchor(_angle("Four days", "Lead with the ruins, then the lake."), ms)["atom_id"] == "a_ruins"

    def test_compliant_angles_do_not_trigger_the_all_thin_retry(self):
        ms = route_moments(SEGMENTS)
        ok = _angle("Four days on the water", "Keep the ruins to one sentence at most; lead with the lake shore.")
        assert angle_generate.angle_contract_violations([ok, ok, ok], {}, ms) == []

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
             patch.object(angle_service, "request_route_segment_ids",  # AA-914 guard: keeps every id here
                          new=AsyncMock(side_effect=lambda p, r, **k: list(r["route_segment_ids"]))), \
             patch.object(angle_service, "generate_angles", new=gen), \
             patch.object(angle_service, "fetch_request", new=AsyncMock(return_value={})):
            await angle_service.set_goal_and_generate(req["tenant_id"], req["request_id"], "promotion", _pool(conn))
        inserts = [c[0] for c in conn.execute.call_args_list if "angle_gate_option" in c[0][0]]
        return gen, inserts

    async def test_brief_is_sanitised_before_it_is_saved(self):
        angles = [_angle(f"A{i}", "Calm, considered LinkedIn voice — direct. Open with the pools. Lead with Day 2.")
                  for i in range(3)]
        _, inserts = await self._run(_req(), angles, TRAIL)
        assert [args[6] for args in inserts] == ["Lead with the pools. Lead with Day 2."] * 3

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
        assert "VOICE (from the brand; the channel and the angle never override it): Energetic, Fresh" in p
        assert "150–220 words (never more than 250)" in p
        assert "at most 140 characters" in p and "At most 3 hashtags" in p
        channel_lines = p.split("GOAL:")[0]
        assert "calm" not in channel_lines.lower()

    def test_linkedin_prompt_gets_the_sanitised_brief(self):
        p = _t9("linkedin")
        assert "ANGLE'S BEST FINAL STYLE: Lead with the pools. Lead with Day 2." in p

    def test_other_social_channel_keeps_its_style_but_gets_the_clean_brief(self):
        p = _t9("facebook")
        assert "CHANNEL STYLE: human, warm" in p and "LENGTH AND SHAPE" not in p
        assert "ANGLE'S BEST FINAL STYLE: Lead with the pools. Lead with Day 2." in p

    def test_sibling_openers_listed_for_social_not_blog(self):
        p = _t9("linkedin", sibling_openers=["There is a checkpoint before the temples."])
        assert "EARLIER PIECES FROM THIS BRIEF OPENED WITH" in p and "There is a checkpoint" in p

    def test_blog_prompts_are_byte_identical(self):
        """Guard: hashes computed on the PR2 base (feat/aa768-hygiene-scope-seed) before any AA-771
        S234 edit. Any change to a blog writer prompt must be deliberate and update these.
        Updated S234 round 2 review (AA-768): the FAQ is required with 3-4 pairs (F16_faq contract).
        Updated S234 round 4 (AA-768): the route blog prompt gets one line for a "Thin moment".
        Updated AA-773 (S235): SYSTEM_PROMPT gets the offered / included Status rule.
        Updated AA-775 (S235): render_brand_block fences the style guide as "never a source" (the
        fixture pack has a style_guide), so `single` and `route` change; SYSTEM_PROMPT does not.
        Updated AA-819 (S237): SYSTEM_PROMPT gets the usage-policy rule (no reviews / testimonials, no
        customer first person, no independence claim); `single` and `route` do not change.
        Updated AA-778 (S238): every em-dash is gone from the prompt text (blog format, SEO envelope,
        FACTS / GOAL / VOICE / CHANNEL AVOID lines, the brand block) and SYSTEM_PROMPT gets the plan
        §3.1 "Never use an em-dash" rule, so all three change.
        Updated AA-800 (S245): one WORDS line after the brand block (the word layers for this brand:
        platform truth, the superlative cap, undeclared positioning words, the service-level claim rule), so
        `single` and `route` change; SYSTEM_PROMPT does not.
        Updated AA-779 (S245): the blog FAQ bullet (both variants) prefers SEARCH QUESTIONS a seed fact answers,
        asks every answer to name its question's subject in the first sentence, 40-60 words, so `single` and
        `route` change; SYSTEM_PROMPT does not (no search questions are passed here).
        Updated AA-790 (S251 pilot, AI-tells 4.08): SYSTEM_PROMPT gets the AI-TELLS rules (ai_tells.PROMPT_RULES) and
        loses 'genuinely' from its summary line; both blog variants get the heading rule (never the angle's name as
        a heading, ai_tells.BLOG_HEADING_RULE), so all three change.
        Updated AA-790 round 2 (S251 re-panel, 25/10/2026): SYSTEM_PROMPT gets more AI-tell rules (do-denials, more
        verdict lines, anaphora, never naming the seed / angle / brief / atom, no comment on the text itself) and the
        VISIT DETAIL rule (ai_tells.VISIT_DETAIL_RULE); every channel gets the brand-energy line
        (ai_tells.brand_voice_rule; the fixture pack's voice is energetic, so a PACE line), so all three change.
        Updated AA-790 round 2 review (S252, 25/10/2026): SYSTEM_PROMPT bounds "what the reader does there" to the
        content seed and adds one priority line (SOURCE FIDELITY and VISIT DETAIL override the brand's style guide
        and its examples; the style guide sets tone and sentence shape, not content): writer_prompt_version
        751b0a34 → afc08095. The PACE line's "you" is bounded to the CONTENT SEED, so `single` and `route` change
        too.
        Updated AA-790 round 3 (S252 re-panel of round 2, 26/10/2026): SYSTEM_PROMPT's SOURCE FIDELITY block gets
        an AGES rule (no age, age band or minimum age the seed does not state; the AUDIENCE line's ages are the
        brand's customers), a CAUSE AND SCOPE rule (no 'established because …' the seed does not link, no widened
        fact) and 'feels like (cold water, a quiet hall)' in the scene-detail line; NUMBERS no longer lists 'age':
        writer_prompt_version afc08095 → 24363541. `single` and `route` do not change.
        Updated AA-790 round 4 (S252 4-arm panel of round 3, AI-tells 5.22, 26/10/2026): PROMPT_RULES adds 'not as A
        but as B' / 'A rather than B', more verdict lines, template closers, lists of three, CTA label lines and ten
        DON'T / WRITE INSTEAD pairs from the panel quotes; SYSTEM_PROMPT gets the silent self-check
        (ai_tells.SELF_CHECK_RULE) before the output line; after review, the last two WRITE halves keep the CTA worked
        into the piece and the self-check quotes the full 'more than' set-up (seed quantities stay):
        writer_prompt_version 24363541 → 2b510e08. The FAMILY
        BRAND line (ai_tells.family_presence_rule) is user-prompt only and the fixture pack is not a family brand,
        so `single` and `route` do not change.
        Updated AA-790 round 5 (S252 5-arm panel of round 4, Terra Family brand voice 7.0 → 5.5, 26/10/2026): the
        FAMILY BRAND line is rewritten (user prompt, family packs only) and VISIT DETAIL (SYSTEM_PROMPT) says that
        naming the children or the parents as the ones who do a seed moment is not a new fact, under a FAMILY BRAND
        brief only: writer_prompt_version 2b510e08 → 79cd046a. `single` and `route` do not change.
        Updated AA-790 round 6 (S252 6-arm panel of round 5, non-family brand voice below round 3, 26/10/2026): the
        user prompt of a non-family pack with an energetic, luxury / cultural or nature / respectful signal gets the
        BRAND TEXTURE block (ai_tells.brand_texture_rule), and SYSTEM_PROMPT's style-guide rule says that block sets
        sentence shape and where the reader stands, never content: writer_prompt_version 79cd046a → b5978839. The
        Trail Pulse fixture is energetic, so `single` and `route` gain the MOMENTUM block; without it they are the
        round-5 bytes (30305b79… / c212a3a2…)."""
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
        assert sha(SYSTEM_PROMPT) == "b5978839f7b24efec987aa89e4ffa77a50af2023d1c8ce14b734e9eb9b0ad17f"
        assert sha(single) == "58f9c85f586ebec43d0b77c853a38163dd852b0d29384652e681d507a8a13e76"
        assert sha(route) == "0aeab9e9bde2c7b6f7a286d01f673b817e483924cbeaf3e1fcad11028e84a29c"

    def test_prompts_carry_no_em_dash_but_the_rule(self):
        """AA-778 — our own prompt text primed the habit (S234: 78 in the writer prompts). The one em-dash
        left is the rule that names it; the seed's own (data, here 'Paro Taktsang \u2014 hike') is not ours."""
        em = "\u2014"
        assert SYSTEM_PROMPT.count(em) == 1 and f"Never use an em-dash ({em})" in SYSTEM_PROMPT
        for channel in ("blog", "linkedin", "facebook"):
            p = _t9(channel)
            assert p.count(em) == 1 and f"Kuang Si {em} swim" in p, channel  # the seed's, not ours
        route = build_user_prompt(
            content_seed="x", goal=GOAL, channel_style=get_channel_style("blog"), brand_audience=TRAIL,
            angle={"name": "A", "why_it_works": "B", "formula_fit": "C", "best_final_style": "D"}, cta="Ask us",
            route_segments=[("a1", "Lake"), ("a2", "Ruins")], revision_feedback=["F1: fix"],
            facts_text="[Fact id=f1] Visa needed.", keyword=None)
        assert em not in route
        angle_prompt = angle_prompts.build_user_prompt(
            content_seed="x", goal=GOAL, brand_audience={**TRAIL, "style_guide": "S."}, destination="Laos",
            trip_name="Laos Trip", channel_style=get_channel_style("linkedin"), revision_feedback=["fix"])
        assert em not in angle_prompts.SYSTEM_PROMPT and em not in angle_prompt


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
        # S234 round 2 — the first line has its own blocking result (F14_first_line)
        assert not any(s.startswith("first line") for s in v)
        fl = qg.gate_first_line(text, linkedin_spec(TRAIL))
        assert fl["violations"][0].startswith("first line is 150 characters, over 140")
        assert fl["blocking"] and not fl["repairable"]

    def test_hashtags_do_not_count_as_words(self):
        assert post_words("One two three. #Four #Five") == 3

    def test_no_spec_passes(self):
        r = qg.gate_social_shape(_words(900), None)
        assert r["passed"] and not r["blocking"]


class TestF15Voice:
    spec = linkedin_spec(TRAIL)

    def test_stock_phrase_blocks_and_goes_to_sentence_repair(self):
        text = "Day 2 ends at the pools. That detail is worth sitting with. Swim, rest, then lunch."
        r = qg.gate_voice(text, spec=self.spec)
        assert r["blocking"] and r["repairable"]
        assert r["violations"][0].endswith("'That detail is worth sitting with.'")
        items = sentence_repair.plan(text, [r])
        assert items and items[0]["sentence"] == "That detail is worth sitting with."

    def test_not_as_pair_is_one_quoted_span(self):
        text = "Rest is built in. Not as filler. Not as an afterthought. It is the plan."
        r = qg.gate_voice(text, spec=self.spec)
        assert any("'Not as filler. Not as an afterthought.'" in v for v in r["violations"])
        items = sentence_repair.plan(text, [r])
        assert items and text[items[0]["start"]:items[0]["end"]] == "Not as filler. Not as an afterthought."

    def test_b2b_register_warns_for_trail_not_for_atlas_hearth(self):
        text = "Active travel brands have a pacing problem. The pools on Day 2 fix it."
        r = qg.gate_voice_warn(text, spec=self.spec, brand_pack=TRAIL)
        assert not r["blocking"] and any(v.startswith("writes to the travel trade") for v in r["violations"])
        assert qg.gate_voice_warn(text, spec=linkedin_spec(AH), brand_pack=AH)["passed"]
        assert qg.gate_voice(text, spec=self.spec)["passed"]

    def test_segment_leak_warns_unless_the_source_has_it(self):
        text = "We plan trips for clients in the 25–40 range. The pools are on Day 2."
        source = "Kuang Si pools\n\nAUDIENCE (who the brand writes for — not a fact about this trip): 25–40 years old"
        r = qg.gate_voice_warn(text, spec=self.spec, brand_pack=TRAIL, atom_text=source)
        assert any("audience definition ('25–40')" in v for v in r["violations"])
        r2 = qg.gate_voice_warn(text, spec=self.spec, brand_pack=TRAIL, atom_text="Group of 25-40 riders on Day 2.")
        assert not any("audience definition" in v for v in r2["violations"])

    def test_generic_and_repeated_openers_warn(self):
        terra_opener = "There is a checkpoint before you reach the inner temples of Paro Taktsang."
        r = qg.gate_voice_warn(terra_opener + " Cameras stay behind.", spec=linkedin_spec(TERRA), brand_pack=TERRA,
                               sibling_openers=[terra_opener])
        kinds = [v.split(":")[0] for v in r["violations"]]
        assert any(k.startswith("generic 'There is a") for k in kinds)
        assert any(k.startswith("opens like an earlier piece") for k in kinds)
        assert not r["blocking"]

    def test_clean_post_passes_and_other_channels_skip(self):
        text = "Day 2 ends at turquoise pools. Swim, rest, eat. Ask us for the dates."
        assert qg.gate_voice(text, spec=self.spec)["passed"]
        # AA-780 — "Swim, rest, eat." + "Ask us for the dates." would be a broetry run but for the CTA
        assert qg.gate_voice_warn(text, spec=self.spec, brand_pack=TRAIL, cta="Ask us for the dates.")["passed"]
        # S234 round 3 — the cross-round stock lines ('worth sitting with', …) run on every channel; the
        # rest of the LinkedIn stock list stays LinkedIn-only
        assert qg.gate_voice("That detail speaks volumes about the place.", spec=None)["passed"]
        assert not qg.gate_voice("That is worth sitting with today.", spec=None)["passed"]
        assert qg.gate_voice_warn("Travel brands skip it.", spec=None)["passed"]

    def test_run_quality_gates_wires_both_for_linkedin(self):
        text = "Active travel brands have a pacing problem. That detail is worth sitting with."
        with patch.object(qg, "gate_brand_voice", return_value=[qg._result("F9_cta_fact", []),
                                                                 qg._result("F9_brand_style", [], blocking=False)]):
            out = qg.run_quality_gates(content_text=text, atom_text="seed", cta="Ask us",
                                       brand_rubric_text="r", channel="linkedin", brand_pack=TRAIL)
        gates = {g["gate"]: g for g in qg.expand_ledger(out["gate_ledger"])}  # AA-924: the legacy view
        # 13 words, under the band: AA-780 — blocks on attempt 1 (one full rewrite), ahead of F15 in the ledger
        assert gates["F14_social_shape"]["blocking"] and out["first_failure"]["gate"] == "F14_social_shape"
        assert gates["F15_voice"]["blocking"]
        assert not gates["F15_voice_warn"]["passed"] and not gates["F15_voice_warn"]["blocking"]
        # AA-924 — stored: the stock phrase blocks inside F2_lexicon; F15_voice holds only the warn checks
        stored = {g["gate"]: g for g in out["gate_ledger"]}
        assert stored["F2_lexicon"]["blocking"] and not stored["F15_voice"]["blocking"]
        with patch.object(qg, "gate_brand_voice", return_value=[qg._result("F9_cta_fact", []),
                                                                 qg._result("F9_brand_style", [], blocking=False)]):
            later = qg.run_quality_gates(content_text=text, atom_text="seed", cta="Ask us",
                                         brand_rubric_text="r", channel="linkedin", brand_pack=TRAIL,
                                         stock_blocking=False)
        assert later["passed"] and {"F14_social_shape", "F15_voice"} <= {g["gate"] for g in later["flags"]}

    # S234 review — a stock repair touches the stock sentence only, never the warn-only checks.
    def test_stock_repair_plan_ignores_the_warn_only_checks(self):
        text = ("There is a lake you reach on Day 2. Active travel brands often skip it entirely. "
                "That detail is worth sitting with.")
        ledger = [qg.gate_voice(text, spec=self.spec),
                  qg.gate_voice_warn(text, spec=self.spec, brand_pack=TRAIL)]
        assert ledger[0]["blocking"] and len(ledger[1]["violations"]) == 2
        items = sentence_repair.plan(text, ledger)
        assert [i["sentence"] for i in items] == ["That detail is worth sitting with."]

    @pytest.mark.parametrize("text", [
        "Your guide tells you everything you need to know about the crossing before you set off.",
        "No accident has been reported on this trail in years.",
        "That tells you something.",
    ])
    def test_ordinary_sentences_are_not_stock(self, text):
        assert qg.gate_voice(text, spec=self.spec)["passed"]

    @pytest.mark.parametrize("text", ["It was no accident that the dzong stands at the confluence of two rivers.",
                                      "The confluence tells you something about how the dzong was sited."])
    def test_narrowed_stock_patterns_still_fire(self, text):
        assert not qg.gate_voice(text, spec=self.spec)["passed"]

    def test_a_stock_phrase_never_holds_a_piece(self):
        f15 = qg.gate_voice("Day 2. That detail is worth sitting with.", spec=self.spec)
        f1 = qg._result("F1_grounding", ["x"])
        assert qg.hold_failure([f15]) is None
        assert qg.hold_failure([f15, f1])["gate"] == "F1_grounding"
        status, reason, flags = writing_service._hold_or_ship([f15], [])
        assert (status, reason) == ("approved", None)
        assert flags[0]["gate"] == "F15_voice" and flags[0]["blocking"] is False
        assert writing_service._hold_or_ship([f15, f1], [])[0] == "held"


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
        f15 = qg.gate_voice(text, spec=linkedin_spec(TRAIL))
        f14 = qg.gate_first_line(text, linkedin_spec(TRAIL))
        hs = highlights(text, [f15, f14], [])
        gates = {h["gate"] for h in hs}
        assert {"F15_voice", "F14_first_line"} <= gates
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


# ---------------------------------------------------------------- T9 loop: F15 never holds (S234 review)

@pytest.mark.asyncio
async def test_stock_phrase_left_after_repairs_ships_instead_of_held():
    text = "Day 2 ends at the pools. That detail is worth sitting with. Ask us for dates."
    f15 = qg.gate_voice(text, spec=linkedin_spec(TRAIL))
    outcome = {"passed": False, "gate_ledger": [f15], "first_failure": f15, "flags": []}
    ctx = {"atom_text": "seed", "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "l"},
           "channel_style": {"channel": "linkedin"}, "brand_audience": TRAIL, "chosen": {"name": "a"},
           "cta": "Ask", "destination": None, "trip_name": None, "brand_rubric_text": "r", "channel": "linkedin",
           "atom_id": "a1", "tenant_id": None}
    gates = MagicMock(return_value=outcome)  # the mock ignores stock_blocking: worst case
    with patch.object(writing_service, "write_content", return_value=(text, 0.01, {}, None)), \
            patch.object(writing_service, "rewrite_with_feedback") as rewrite, \
            patch.object(writing_service, "repair_sentences",
                         return_value=(["That detail matters on a hot day."], 0.01)), \
            patch.object(writing_service, "run_quality_gates", new=gates), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    rewrite.assert_not_called()
    assert [c.kwargs["stock_blocking"] for c in gates.call_args_list] == [True, False]  # one repair round
    kw = fin.await_args.kwargs
    assert kw["status"] == "approved" and kw["held_reason"] is None
    assert any(f["gate"] == "F15_voice" and f["blocking"] is False for f in kw["flags"])
