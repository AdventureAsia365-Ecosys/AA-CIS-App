"""AA-780 (S234 r4 plan #7) — LinkedIn: the 120-word floor blocks on attempt 1 only (one full rewrite,
never a hold), the CONSUMER_SOCIAL structure / avoid list (plan §3.7), new stock lines in F15_voice and
the broetry warn in F15_voice_warn. Fixtures are the real S234 r4 Trail Pulse posts (#7, #12) and
sentences quoted from the r1–r4 pieces."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers.admin_content_quality import highlights
from services.acp_angle_gate.ranking import split_avoid_phrases
from services.acp_content_writing import quality_gates as qg, sentence_repair
from services.acp_content_writing import service as writing_service
from services.acp_shared.channel_spec import (CONSUMER_SOCIAL, STOCK_PATTERNS, STOCK_PHRASES, linkedin_spec,
                                              post_words)

TRAIL = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
         "customer_segment": "25–40 years old\nSolo travellers, couples, small groups of friends",
         "voice": ["Energetic", "Fresh", "Direct", "Friendly", "Modern", "Confident", "Lightly playful"]}
AH = {"brand_name": "Atlas & Hearth", "brand_type": "Luxury cultural travel brand",
      "voice": ["Elegant", "Discreet", "Cultured", "Calm", "Precise", "Atmospheric", "Intelligent"]}
SPEC = linkedin_spec(TRAIL)

# S234 r4 #7 (Trail Pulse, 105 words, approved — a panel false pass)
R4_7 = ("Planning a trip well means building in rest before you need it — not scrambling to add it later.\n\n"
        "On Day 2, a short trek leads to the tiered limestone pools of Kuang Si Falls, where high calcium carbonate "
        "in the water turns each pool a vivid turquoise-blue. The itinerary keeps you there — time to swim, time to "
        "rest — before lunch at the View Café overlooking the cascade.\n\n"
        "That's not filler. That's structure. Recovery built into the day before fatigue makes the decision for "
        "you.\n\nIf you want to see how the rest of the pacing holds up, get the full Trail Pulse itinerary and "
        "dates.\n\n#LuangPrabang #KuangSiFalls #Laos")
# S234 r4 #12 (Trail Pulse, 113 words, approved — a panel false pass)
R4_12 = ("Swim time is already written into Day 2, with space to rest before lunch.\n\n"
         "The trek on Day 2 ends at the tiered pools of Kuang Si Falls near Luang Prabang. The pools descend through "
         "travertine limestone, and the water runs a vivid turquoise — the result of calcium carbonate leaching from "
         "the rock. That is not a backdrop. That is where the itinerary keeps you, with time set aside to swim and "
         "rest before lunch.\n\nThat kind of sequencing is deliberate.\n\n"
         "Trail Pulse builds trips around knowing when to move and when to stop.\n\n"
         "Get the full Trail Pulse itinerary and dates and see how Day 2 is structured from start to finish.\n\n"
         "#LuangPrabang #KuangSiFalls #Laos")


def _words(n):
    return "Hook line.\n\n" + " ".join(["walk"] * (n - 2)) + "."


# ---------------------------------------------------------------- F14 floor

class TestF14Floor:
    @pytest.mark.parametrize("text", [R4_7, R4_12])
    def test_r4_false_passes_block_on_attempt_1(self, text):
        assert post_words(qg.strip_citation_tags(text)) < 120
        r = qg.gate_social_shape(text, SPEC, floor_blocking=True)
        assert r["blocking"] and r["repairable"] and not r["passed"]
        assert "under the 120–250-word band" in r["violations"][0]
        assert "rewrite it to 150–220 words" in r["violations"][0]

    def test_floor_warns_only_without_the_flag(self):
        r = qg.gate_social_shape(R4_7, SPEC)
        assert not r["passed"] and not r["blocking"] and not r["repairable"]

    def test_in_band_and_far_over_unchanged(self):
        assert qg.gate_social_shape(_words(150), SPEC, floor_blocking=True)["passed"]
        assert qg.gate_social_shape(_words(330), SPEC, floor_blocking=False)["blocking"]
        assert qg.gate_social_shape(_words(900), None, floor_blocking=True)["passed"]

    def test_run_quality_gates_blocks_the_floor_on_attempt_1_only(self):
        f9 = [qg._result("F9_cta_fact", []), qg._result("F9_brand_style", [], blocking=False)]
        kw = dict(content_text=R4_12, atom_text="seed", cta="Get the full Trail Pulse itinerary and dates",
                  brand_rubric_text="r", channel="linkedin", brand_pack=TRAIL)
        with patch.object(qg, "gate_brand_voice", return_value=f9):
            first = qg.run_quality_gates(**kw)
            later = qg.run_quality_gates(**kw, stock_blocking=False)
        f14 = next(g for g in first["gate_ledger"] if g["gate"] == "F14_social_shape")
        assert f14["blocking"] and not first["passed"]
        assert any(g["gate"] == "F14_social_shape" and not g["blocking"] for g in later["flags"])
        assert not any(g["gate"] == "F14_social_shape" and g["blocking"] for g in later["gate_ledger"]
                       if not g["passed"])

    def test_other_channels_never_get_the_floor(self):
        f9 = [qg._result("F9_cta_fact", []), qg._result("F9_brand_style", [], blocking=False)]
        with patch.object(qg, "gate_brand_voice", return_value=f9):
            out = qg.run_quality_gates(content_text="A short post about the falls.", atom_text="seed", cta="Ask",
                                       brand_rubric_text="r", channel="instagram", brand_pack=TRAIL)
        f14 = next(g for g in out["gate_ledger"] if g["gate"] == "F14_social_shape")
        assert f14["passed"]

    def test_the_floor_goes_to_a_full_rewrite_not_a_sentence_repair(self):
        ledger = [qg.gate_social_shape(R4_7, SPEC, floor_blocking=True)]
        assert sentence_repair.plan(R4_7, ledger) is None


class TestF14FloorNeverHolds:
    def test_under_floor_never_holds_far_over_still_does(self):
        under = qg.gate_social_shape(R4_7, SPEC, floor_blocking=True)
        over = qg.gate_social_shape(_words(400), SPEC)
        assert qg.never_holds(under) and not qg.never_holds(over)
        assert qg.hold_failure([under]) is None
        assert qg.hold_failure([over])["gate"] == "F14_social_shape"
        assert qg.hold_failure([under, qg._result("F1_grounding", ["x"])])["gate"] == "F1_grounding"

    def test_reverted_attempt_1_ledger_ships_with_the_floor_waived(self):
        """AA-528 revert keeps attempt 1's ledger (F14 blocking): the piece ships, the entry is waived."""
        under = qg.gate_social_shape(R4_7, SPEC, floor_blocking=True)
        status, reason, flags = writing_service._hold_or_ship([under], [])
        assert (status, reason) == ("approved", None)
        assert flags[0]["gate"] == "F14_social_shape" and flags[0]["blocking"] is False
        waived = writing_service._waive_style_only([under], "approved")[0]
        assert waived["blocking"] is False and waived["waived"] is True

    def test_never_holds_reads_a_stored_ledger_snapshot(self):
        from services.acp_content_writing.gate_eval import ledger_snapshot
        snap = ledger_snapshot([qg.gate_social_shape(R4_7, SPEC, floor_blocking=True)])[0]
        assert qg.never_holds(snap)


@pytest.mark.asyncio
async def test_t9_loop_rewrites_a_short_post_once_and_ships():
    """Attempt 1 is under the floor → one full rewrite with the explicit target; attempt 2 is still short →
    F14 is a warn flag, the piece ships (never a second rewrite, never a hold)."""
    ctx = {"atom_text": "seed", "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "l"},
           "channel_style": {"channel": "linkedin"}, "brand_audience": TRAIL, "chosen": {"name": "a"},
           "cta": "Ask", "destination": None, "trip_name": None, "brand_rubric_text": "r", "channel": "linkedin",
           "atom_id": "a1", "tenant_id": None}

    def fake_gates(*, content_text, stock_blocking=True, **_):
        f14 = qg.gate_social_shape(content_text, SPEC, floor_blocking=stock_blocking)
        ledger = [f14]
        first = f14 if (not f14["passed"] and f14["blocking"]) else None
        return {"passed": first is None, "gate_ledger": ledger, "first_failure": first,
                "flags": [g for g in ledger if not g["passed"] and not g["blocking"]]}

    gates = MagicMock(side_effect=fake_gates)
    with patch.object(writing_service, "write_content", return_value=(R4_7, 0.01, {}, None)), \
            patch.object(writing_service, "rewrite_with_feedback", return_value=(R4_12, 0.01, {}, None)) as rewrite, \
            patch.object(writing_service, "run_quality_gates", new=gates), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    assert rewrite.call_count == 1
    feedback = rewrite.call_args.kwargs["revision_feedback"]
    assert any(f.startswith("F14_social_shape: post is") and "rewrite it to 150–220 words" in f for f in feedback)
    assert [c.kwargs["stock_blocking"] for c in gates.call_args_list] == [True, False]
    kw = fin.await_args.kwargs
    assert kw["status"] == "approved" and kw["held_reason"] is None
    assert any(f["gate"] == "F14_social_shape" and f["blocking"] is False for f in kw["flags"])


# ---------------------------------------------------------------- channel spec §3.7

class TestConsumerSocialSpec:
    def test_structure_has_no_seed_example_and_no_contractions_line(self):
        s = SPEC.structure
        assert SPEC.register == CONSUMER_SOCIAL
        assert "states the observation, not a question" in s and "3-4 short paragraphs" in s
        assert "second person with active verbs" in s and "no maxims" in s
        assert "Kuang Si" not in s and "ontraction" not in s

    def test_avoid_list_items_have_no_inner_comma(self):
        items = split_avoid_phrases(SPEC.avoid)
        assert "the one-line-per-paragraph broetry cadence" in items
        assert 'two short maxims in a row ("That\'s not X. That\'s Y.")' in items
        assert "humblebrag framing" in items
        for phrase in ("not filler", "worth pausing on", "is not a backdrop", "does not perform",
                       "tolerates them", "isn't an inconvenience"):
            assert phrase in items and phrase in STOCK_PHRASES

    def test_every_new_stock_phrase_is_caught_by_a_pattern(self):
        for phrase in STOCK_PHRASES[STOCK_PHRASES.index("not filler"):]:
            assert any(p.search(f"That {phrase}.") for p in STOCK_PATTERNS), phrase

    def test_other_registers_keep_their_structure(self):
        assert "3-4 short paragraphs" not in linkedin_spec(AH).structure


# ---------------------------------------------------------------- F15 new stock lines

class TestNewStockLines:
    @pytest.mark.parametrize("sentence", [
        "That's not filler.",                                              # S234 r4 #7
        "That's not downtime filler.",                                     # S234 r1 #15
        "That stop isn't filler.",                                         # S234 r2 #12
        "The swim isn’t filler between activities.",
        "Those numbers are worth pausing on before you plan anything.",    # S234 r4 #6 (blog)
        "That is not a backdrop.",                                         # S234 r4 #12
        "The confluence was not simply a dramatic backdrop.",              # S234 r4 #1
        "The landscape here does not perform for visitors.",               # S234 r4 #16 (blog)
        "The valley doesn't perform.",
        "It is not a landscape that accommodates visitors; it tolerates them.",  # S234 r4 #18 (blog)
        "Leaving your camera at the checkpoint isn't an inconvenience.",   # S234 r4 #8
        "These aren't inconveniences, they are the shape of the place.",   # S234 r4 #11
        "That requirement is not a bureaucratic inconvenience.",           # S234 r2 #17 (blog)
    ])
    def test_fires_on_linkedin(self, sentence):
        r = qg.gate_voice(f"Day 2 ends at the pools. {sentence} Ask us for dates.", spec=SPEC)
        assert not r["passed"] and r["blocking"]
        assert any(v.startswith("stock phrase") for v in r["violations"])

    @pytest.mark.parametrize("sentence", [
        "The Khoridol Saridag range forms a dramatic backdrop on the opposite shore.",  # S234 r1 #17
        "The monks do not perform the cham dance outside the festival days.",
        "Guides did not perform the ritual for the group.",
        "Pack light and leave the filler snacks at home.",
        "The checkpoint search is a small inconvenience for most guests.",
    ])
    def test_trip_facts_do_not_fire(self, sentence):
        assert qg.gate_voice(sentence, spec=SPEC)["passed"]

    def test_r4_7_stock_line_goes_to_sentence_repair(self):
        r = qg.gate_voice(R4_7, spec=SPEC)
        items = sentence_repair.plan(R4_7, [r])
        assert items and any("That's not filler." in i["sentence"] for i in items)

    def test_blog_channel_is_unaffected(self):
        assert qg.gate_voice("That is not a backdrop. It is the reason for the trip.", spec=None)["passed"]


# ---------------------------------------------------------------- F15_voice_warn broetry

class TestBroetry:
    def test_r4_7_maxim_pair_warns(self):
        r = qg.gate_voice_warn(R4_7, spec=SPEC, brand_pack=TRAIL)
        v = [x for x in r["violations"] if x.startswith("broetry")]
        assert len(v) == 1 and "That's not filler. That's structure." in v[0]
        assert not r["blocking"] and not r["repairable"]

    def test_short_sentences_across_paragraphs_count(self):
        text = ("Day 2 ends at the falls after a two-hour trek.\n\nNot squeezed in.\n\nPlanned in.\n\n"
                "You swim, then lunch at the café above the cascade.")
        r = qg.gate_voice_warn(text, spec=SPEC, brand_pack=TRAIL)
        assert any(x.startswith("broetry: 2 very short") for x in r["violations"])

    def test_one_short_sentence_or_hashtags_do_not_warn(self):
        r = qg.gate_voice_warn(R4_12, spec=SPEC, brand_pack=TRAIL)
        assert not any(x.startswith("broetry") for x in r["violations"])
        text = "You swim at the falls before lunch on Day 2.\n\nAsk us.\n\n#Laos #KuangSi #Trek"
        r = qg.gate_voice_warn(text, spec=SPEC, brand_pack=TRAIL)
        assert not any(x.startswith("broetry") for x in r["violations"])

    def test_a_run_of_three_is_one_warning(self):
        text = "Day 2 is the falls day. Swim. Rest. Eat. Then the trek back to town before dark."
        v = [x for x in qg.gate_voice_warn(text, spec=SPEC, brand_pack=TRAIL)["violations"] if x.startswith("broetry")]
        assert len(v) == 1 and v[0].startswith("broetry: 3 very short")

    def test_the_configured_cta_never_counts(self):
        text = "Day 2 ends at the falls after a two-hour trek. Swim and rest. Ask us for dates."
        assert any(x.startswith("broetry") for x in qg.gate_voice_warn(text, spec=SPEC)["violations"])
        assert qg.gate_voice_warn(text, spec=SPEC, cta="Ask us for dates")["passed"]

    def test_run_quality_gates_passes_the_cta(self):
        f9 = [qg._result("F9_cta_fact", []), qg._result("F9_brand_style", [], blocking=False)]
        with patch.object(qg, "gate_brand_voice", return_value=f9):
            out = qg.run_quality_gates(content_text=R4_7, atom_text="seed", cta="Get the full itinerary.",
                                       brand_rubric_text="r", channel="linkedin", brand_pack=TRAIL)
        warn = next(g for g in out["gate_ledger"] if g["gate"] == "F15_voice_warn")
        assert any(v.startswith("broetry") for v in warn["violations"]) and not warn["blocking"]

    def test_no_spec_skips(self):
        assert qg.gate_voice_warn("Swim. Rest.", spec=None)["passed"]

    def test_admin_highlights_the_run(self):
        r = qg.gate_voice_warn(R4_7, spec=SPEC, brand_pack=TRAIL)
        hs = [h for h in highlights(R4_7, [r], []) if h["gate"] == "F15_voice_warn"]
        assert any(R4_7[h["start"]:h["end"]].startswith("That's not filler. That's structure.") for h in hs)
