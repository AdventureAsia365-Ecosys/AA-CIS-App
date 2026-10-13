"""AA-768 (S234 round 2) — blog FAQ contract (F16_faq), writer-instruction / audience leaks
(F13_leak), trip-name hygiene for part-of-trip pieces, repair integrity, contrast-frame density and
sibling templates (F15_voice), LinkedIn first line (F14_first_line), and the #7 ledger waiver.
Fixtures are trimmed from the 20 S234 checkpoint pieces (.tmp-session/s234/pieces.json)."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import service as writing_service
from services.acp_shared.channel_spec import linkedin_spec

TRIP = "Mongolia: Khövsgöl Lake and Gobi Desert — 15 Days"
SCOPE = ("SCOPE: this piece covers days 3–14 of the trip, in this order: Sod Camp → Uran Togoo → Moron → "
         "Lake Khövsgöl → ruins of an ancient monastery.")
SEED = ("Sod Camp — overnight in a standard ger camp\n"
        "Location: Sod Camp, Khövsgöl Lake area, Khövsgöl Province, Mongolia\n"
        "Day 3 of the trip\n"
        "Source itinerary: Arrive at Sod Camp in the afternoon. The camp sits opposite a mountain range.\n"
        "Place background (checked facts about the place itself):\n"
        "- Lake Khövsgöl holds approximately 0.4 percent of the world's fresh surface water.\n"
        "- The surrounding area is home to reindeer-herding Tsaatan (Dukha) communities.")
TERRA_AUDIENCE = "Affluent families with children aged 7–16\nParents who want active holidays"
TRAIL = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
         "customer_segment": "25–40 years old\nSolo travellers, couples",
         "voice": ["Energetic", "Fresh", "Direct", "Friendly", "Modern", "Confident", "Lightly playful"]}


def _ctx(**kw):
    base = dict(seed=SEED, scope=SCOPE, trip_name=TRIP, brand_name="Terra Family Expeditions",
                cta="Ask Terra Family Expeditions to plan this trip for your family", audience=TERRA_AUDIENCE)
    base.update(kw)
    return h.HygieneContext(**base)


# ---------------------------------------------------------------- numbers

def test_small_numbers_and_ranges_read_words_and_digits():
    assert h.small_number("fifteen") == 15 and h.small_number("twenty-one") == 21 and h.small_number("15") == 15
    assert h.number_ranges("the seven-to-sixteen range, ages 7–16, 25 to 40") == {(7, 16), (25, 40)}


# ---------------------------------------------------------------- leaks (fix 2)

class TestInstructionLeaks:
    def test_heading_keeps_its_topic(self):
        text = "Intro line here.\n\n## A Single Sentence on What Time Has Taken\n\nThe ruins remain on the hill."
        fixed, log = h.autofix(text, _ctx())
        assert "## What Time Has Taken" in fixed and "Sentence on" not in fixed
        fixed, _ = h.autofix("A.\n\n## The Monastery Ruins: A Sentence on Loss\n\nThe ruins remain.", _ctx())
        assert "## The Monastery Ruins: Loss" in fixed

    def test_self_reference_is_deleted_when_safe(self):
        text = ("Lake Khövsgöl sits in the north. This piece traces one route through that threshold, from the "
                "caldera to the lake. Sod Camp faces a mountain range.")
        fixed, log = h.autofix(text, _ctx())
        assert "This piece traces" not in fixed and any("writer-instruction" in line for line in log)
        assert not h.find_issues(fixed, _ctx())["leak"]

    def test_unsafe_delete_is_left_for_f13_leak(self):
        text = ("Lake Khövsgöl sits in the north.\n\nThis guide covers the Khövsgöl region in depth, from the "
                "caldera to the camp. It is written for families who want to understand a place first.")
        fixed, _ = h.autofix(text, _ctx())
        assert "This guide covers" in fixed  # deleting it would orphan "It is written…"
        g = qg.gate_leak(fixed, _ctx())
        assert g["blocking"] and g["repairable"] and "This guide covers" in g["violations"][0]
        assert sr._BRAND_QUOTE_RE.search(g["violations"][0]).group(2).startswith("This guide covers")

    def test_scope_day_span_is_a_leak_only_when_it_is_the_scope(self):
        assert h.instruction_leaks("The pacing across days three to fourteen is designed well.", SCOPE)
        assert not h.instruction_leaks("Days five to six are spent on horseback.", SCOPE)

    def test_audience_range_trimmed_or_deleted(self):
        trail = _ctx(audience=TRAIL["customer_segment"], seed="Kuang Si Falls — swim\nDay 2 of the trip",
                     scope="SCOPE: this piece is about one moment of the trip (day 2): Kuang Si Falls.", trip_name=None)
        fixed, log = h.autofix("Day 2 is easy. If you're a 25–40 traveller who wants active days, this works.", trail)
        assert "If you're a traveller who wants active days" in fixed
        fixed, _ = h.autofix("The camp is calm. Children at both ends of the seven-to-sixteen range find room. "
                             "The lake is wide.", _ctx())
        assert "seven-to-sixteen" not in fixed and "The lake is wide." in fixed

    def test_audience_range_in_the_seed_is_not_a_leak(self):
        ctx = _ctx(seed=SEED + "\n- Suitable for children aged 7–16.")
        assert not h.audience_leaks("Children aged 7–16 can ride.", ctx.audience, ctx.lexicon.own_text)

    def test_clean_linkedin_text_has_no_leak(self):
        text = ("Punakha Dzong sits where two rivers meet.\n\nThe Je Khenpo still moves here each winter.\n\n"
                "Ask WildKind Travel for the full itinerary.")
        ctx = h.HygieneContext(seed="Punakha Dzong — visit\nLocation: Punakha Dzong, Punakha, Bhutan",
                               audience="Eco-conscious travellers, nature lovers")
        assert h.find_issues(text, ctx)["leak"] == []


# ---------------------------------------------------------------- trip name (fix 3)

class TestTripName:
    def test_scoped_trip_name_drops_duration_and_out_of_scope_places(self):
        assert h.scoped_trip_name(TRIP, SCOPE, _ctx().lexicon) == "Mongolia: Khövsgöl Lake"

    def test_full_trip_scope_keeps_the_name(self):
        full = "SCOPE: this piece covers days 1–15 of the trip, in this order: A → B."
        assert h.scoped_trip_name(TRIP, full, _ctx().lexicon) == TRIP
        assert h.scoped_trip_name(TRIP, None, None) == TRIP

    def test_trip_length_is_an_f12_hit_and_its_adjective_is_dropped(self):
        ctx = _ctx()
        found = h.find_issues("The trip runs fifteen days in all, with many stops on the way north.", ctx)
        assert any(v.startswith("states the whole trip's length ('fifteen days')") for v in found["scope"])
        fixed, log = h.autofix("Ask Terra Family Expeditions to plan this trip for your family, and see how the "
                               "full fifteen-day Mongolia itinerary fits together.", ctx)
        assert "fifteen-day" not in fixed and "full Mongolia itinerary" in fixed  # CTA kept, trimmed only

    def test_journey_end_flagged_unless_scope_reaches_the_last_day(self):
        s = "The journey concludes with a visit to the ruins of an ancient monastery."
        assert h.journey_end_claims(s, _ctx())
        assert not h.journey_end_claims(s, _ctx(scope="SCOPE: this piece covers days 3–15 of the trip."))
        assert not h.journey_end_claims("The route does not end at the lake.", _ctx())


# ---------------------------------------------------------------- repair integrity (fix 4)

class TestIntegrity:
    def test_dangling_openers(self):
        assert h.dangling_opener("What they find at Lake Khövsgöl is something more specific.")
        assert h.dangling_opener("This is the core reason the lake works.")
        assert not h.dangling_opener("This dormant volcano last erupted long ago.")
        assert not h.dangling_opener("Lake Khövsgöl is deep.")

    def test_integrity_only_reports_what_the_repair_made(self):
        before = "Families arrive at the lake. What they find there is a boundary.\n\nThis is the core reason."
        after = "What they find there is a boundary.\n\nThis is the core reason."
        probs = h.integrity_problems(before, after)
        assert len(probs) == 1 and "What they find" in probs[0]

    def test_splice_checked_reverts_a_cut_that_orphans_a_pronoun(self):
        text = "The lake holds 70 percent of the water. It is deep and cold. Sod Camp faces the range."
        items = sr.plan(text, [{"gate": "F11_brand_name", "passed": False, "blocking": True,
                                "violations": ["names x: 'The lake holds 70 percent of the water.'"]}])
        out = sr.splice_checked(text, items, [""])
        assert out == text and items[0]["guard"] == "integrity"

    def test_splice_checked_keeps_a_good_repair(self):
        text = "The lake holds 70 percent of the water. It is deep and cold. Sod Camp faces the range."
        items = sr.plan(text, [{"gate": "F11_brand_name", "passed": False, "blocking": True,
                                "violations": ["names x: 'Sod Camp faces the range.'"]}])
        out = sr.splice_checked(text, items, ["The camp faces a mountain range."])
        assert out.endswith("The camp faces a mountain range.") and "guard" not in items[0]


# ---------------------------------------------------------------- contrast + templates (fix 5)

class TestVoiceRound2:
    def test_contrast_density(self):
        text = ("Day 2 ends at the pools. That's not downtime filler. It's a deliberate call. The pools descend "
                "through limestone. The stop isn't a gap in the programme. It's the programme working.")
        n, allowed, _ = h.contrast_excess(text)
        assert (n, allowed) == (2, 1)
        g = qg.gate_voice(text, spec=None, stock_blocking=True)  # every channel, blog included
        assert g["blocking"] and g["repairable"] and len(g["violations"]) == 1
        assert "contrast frame" in g["violations"][0]
        assert qg.gate_voice(text, spec=None, stock_blocking=False)["blocking"] is False

    def test_one_frame_per_150_words_passes(self):
        text = "That's not filler. It's a structural choice. " + " ".join(["walk"] * 150) + "."
        assert qg.gate_voice(text, spec=None)["passed"]

    def test_sibling_template_found_seed_fact_not(self):
        sib = ("Bags and cameras stay at a checkpoint. Modest dress is required to enter. These are the shape of "
               "the day, and a well-prepared family arrives knowing exactly what to expect.")
        mine = "Modest, respectful dress is required. A well-prepared family arrives already knowing what to expect."
        seed = "Visitors must leave cameras and bags at a checkpoint; modest, respectful dress is required."
        hits = h.sibling_templates(mine, [sib], exclude=[seed])
        assert [s for s, _ in hits] == ["A well-prepared family arrives already knowing what to expect."]
        g = qg.gate_voice(mine, spec=None, sibling_texts=[sib], exclude_texts=[seed])
        assert "repeats a line from an earlier piece" in g["violations"][0]
        assert h.sibling_templates(mine, [], exclude=[seed]) == []


# ---------------------------------------------------------------- FAQ contract (fix 1)

BLOG = ("## Lake\n\nThe lake is deep. Ask Terra Family Expeditions to plan this trip for your family.\n\n"
        "## FAQ\n\n**Q: How deep is the lake?**\nA: It is one of the deepest lakes in Asia.\n")


class TestFaqContract:
    def test_count_and_gate(self):
        assert qg.faq_pair_count(BLOG) == 1 and qg.faq_pair_count("No FAQ here.") == 0
        g = qg.gate_faq_contract(BLOG)
        assert g["gate"] == "F16_faq" and g["blocking"] and not g["repairable"]
        assert qg.gate_faq_contract(qg.replace_faq_section(BLOG, [("a?", "x."), ("b?", "y."), ("c?", "z.")]))["passed"]

    def test_replace_moves_prose_after_the_faq_above_it(self):
        text = BLOG + "\nTalk to us to plan this trip.\n"
        out = qg.replace_faq_section(text, [("Q one?", "A one."), ("Q two?", "A two.")])
        assert out.index("Talk to us") < out.index("## FAQ") and out.endswith("A: A two.\n")
        assert qg.faq_pair_count(out) == 2

    def test_plan_skips_contract_gates(self):
        f16 = qg.gate_faq_contract(BLOG)
        fl = {"gate": "F14_first_line", "passed": False, "blocking": True, "repairable": False,
              "violations": ["first line is 160 characters, over 140: 'x'"]}
        f11 = {"gate": "F11_brand_name", "passed": False, "blocking": True,
               "violations": ["names x: 'The lake is deep and very cold indeed.'"]}
        items = sr.plan("The lake is deep and very cold indeed. More.", [f16, fl, f11])
        assert items[0]["sentence"].startswith("The lake")
        assert sr.plan("The lake is deep.", [f16]) is None  # alone it holds at once (not repairable)


@pytest.mark.asyncio
class TestServiceSteps:
    async def test_faq_regenerated_once_and_kept_when_longer(self):
        pairs = [("What is Sod Camp?", "A tourist ger camp."),
                 ("Who lives near the lake?", "Reindeer-herding Tsaatan communities."),
                 ("What share of fresh water does the lake hold?",
                  "About 0.4 percent of the world's fresh surface water."),
                 ("Is it suitable for kids aged 5?", "Yes.")]
        ctx = {"atom_text": SEED, "facts_text": "", "brand_audience": {"brand_name": "Terra"}, "tenant_id": None,
               "cta": "Ask Terra Family Expeditions to plan this trip for your family"}
        with patch.object(writing_service, "write_faq", return_value=(pairs, 0.006)) as wf:
            out, cost, log = await writing_service._enforce_faq_contract(
                BLOG, context=ctx, hyg=_ctx(), request_id=uuid.uuid4(), brand_ignore=frozenset())
        wf.assert_called_once()
        # the age question was dropped; the writer's surviving pair stays first (S234 r2 review: merged)
        assert qg.faq_pair_count(out) == 4 and cost == 0.006
        assert qg.faq_pairs(out)[0][0] == "How deep is the lake?"
        assert "1 → 4 pairs" in log[0] and out.index("Ask Terra") < out.index("## FAQ")

    async def test_faq_not_called_when_long_enough(self):
        full = qg.replace_faq_section(BLOG, [("a?", "x."), ("b?", "y."), ("c?", "z.")])
        with patch.object(writing_service, "write_faq") as wf:
            out, cost, log = await writing_service._enforce_faq_contract(
                full, context={"atom_text": SEED}, hyg=_ctx(), request_id=uuid.uuid4(), brand_ignore=frozenset())
        wf.assert_not_called()
        assert out == full and cost == 0.0 and log == []

    async def test_first_line_rewritten_on_t9_repair(self):
        spec = linkedin_spec(TRAIL)
        long_first = "On Day 2 " + "a long winding sentence about the limestone pools and the swim " * 3 + "ends here."
        text = long_first + "\n\nThe pools are turquoise.\n\nGet the full Trail Pulse itinerary and dates."
        ctx = {"atom_text": "Kuang Si Falls — swim", "channel": "linkedin", "tenant_id": None,
               "brand_audience": TRAIL, "cta": "Get the full Trail Pulse itinerary and dates"}
        hyg = h.HygieneContext(seed="Kuang Si Falls — swim", first_line_max=spec.first_line_max)
        with patch.object(writing_service, "repair_sentences",
                          return_value=(["On Day 2 the trek ends at the Kuang Si pools."], 0.001)) as rs:
            out, cost, log = await writing_service._fix_first_line(
                text, spec=spec, context=ctx, hyg=hyg, request_id=uuid.uuid4(), brand_ignore=frozenset())
        assert rs.call_args.kwargs["stage"] == "t9_repair" and rs.call_args.kwargs["mode"] == "first_line"
        assert out.startswith("On Day 2 the trek ends at the Kuang Si pools.\n\n") and "rewrote" in log[0]
        assert qg.gate_first_line(out, spec)["passed"]

    async def test_first_line_still_long_is_not_kept(self):
        spec = linkedin_spec(TRAIL)
        text = "x " * 100 + "end.\n\nBody."
        with patch.object(writing_service, "repair_sentences", return_value=(["y " * 90 + "end."], 0.001)):
            out, _, log = await writing_service._fix_first_line(
                text, spec=spec, context={"channel": "linkedin", "atom_text": ""},
                hyg=h.HygieneContext(seed=""), request_id=uuid.uuid4(), brand_ignore=frozenset())
        assert out == text and "not kept" in log[0]
        assert not qg.gate_first_line(out, spec)["passed"]

    async def test_sibling_texts_one_select_and_soft_fail(self):
        conn = AsyncMock()
        conn.fetch.return_value = [{"content_text": "Earlier piece."}, {"content_text": None}]
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        out = await writing_service._sibling_texts(str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), pool)
        assert out == ["Earlier piece."] and conn.fetch.await_count == 1
        assert "angle_gate_request_id = $2::uuid" in conn.fetch.await_args.args[0]
        assert await writing_service._sibling_texts(None, uuid.uuid4(), uuid.uuid4(), pool) == []
        assert await writing_service._sibling_texts("t", uuid.uuid4(), uuid.uuid4(), MagicMock()) == []


# ---------------------------------------------------------------- #7 ledger waiver (fix 7)

def test_style_only_failure_is_waived_in_the_ledger_when_approved():
    f15 = {"gate": "F15_voice", "passed": False, "blocking": True, "repairable": True, "violations": ["x: 'y'"]}
    f1 = {"gate": "F1_grounding", "passed": True, "blocking": True, "repairable": True, "violations": []}
    out = writing_service._waive_style_only([f15, f1], "approved")
    assert out[0]["blocking"] is False and out[0]["waived"] is True and out[1] is f1
    assert writing_service._waive_style_only([f15], "held")[0]["blocking"] is True


def test_style_only_repair_uses_the_cheap_stage():
    f15 = {"gate": "F15_voice", "passed": False, "blocking": True, "violations": []}
    f16 = {"gate": "F16_faq", "passed": False, "blocking": True, "violations": []}
    f1 = {"gate": "F1_grounding", "passed": False, "blocking": True, "violations": []}
    assert writing_service._style_only_repair([f15, f16])
    assert not writing_service._style_only_repair([f15, f1])


def test_grounding_uses_the_writer_trip_name():
    ctx = {"atom_text": "seed", "trip_name": TRIP, "writer_trip_name": "Mongolia: Khövsgöl Lake"}
    g = writing_service._grounding_text(ctx)
    assert "TRIP: Mongolia: Khövsgöl Lake" in g and "15 Days" not in g
    wi = writing_service.writer_input_snapshot(ctx)
    assert wi["trip"] == TRIP and wi["writer_trip"] == "Mongolia: Khövsgöl Lake"
    assert "15 Days" not in writing_service.grounding_from_writer_input(wi)
