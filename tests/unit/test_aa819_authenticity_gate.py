"""AA-819 (AUP 2026-11) — T9 authenticity guard: no reviews / testimonials, no first-person customer or
traveller voice, no claim of independence.

Covers the deterministic detector (authenticity.py), the F19_authenticity gate in the T10 stack, the
sentence-repair wiring (plan + cheap stage), the fail-closed cut after the loop
(service._enforce_authenticity), the SEO fit refusal, the admin replacement check, the prompt rules,
and a loop-level fault fixture: a T9 piece prompted toward a testimonial is repaired (gate evidence in
the repair log, admin-only) or, when the repair keeps it, cut — never shipped."""
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import authenticity
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair, service
from services.acp_content_writing.generate import _REPAIR_SYSTEM
from services.acp_content_writing.prompts import SYSTEM_PROMPT

CTA = "Ask Trail Pulse about the March dates."

TESTIMONIALS = [
    "“The best week of our lives. We loved every day.” — Sarah, UK",
    "\"Best trip ever!\" said Tom from Leeds.",
    "\"The boat ride was magic,\" says one recent guest.",
    "One recent guest told us the monks were the highlight.",
    "Many of our travellers say the boat ride is the best part.",
    "As one guest put it, the silence was the point.",
    "Rated 4.9 on TripAdvisor by hundreds of guests.",
    "Our tours are five-star rated by travellers.",
    "Five-star reviews keep coming in.",
    "★★★★★ from the Smith family.",
    "Read our verified reviews before you book.",
    "Our guest reviews speak for themselves.",
    "A 5/5 experience from start to finish.",
]
FIRST_PERSON = [
    "I booked this trip last spring and loved it.",
    "My wife and I spent a week in Laos.",
    "I highly recommend the Mekong sunset cruise.",
    "We would definitely book again.",
    "Best trip of my life.",
    "Can't recommend Trail Pulse enough.",
    "We travelled with Trail Pulse in March.",
    "My trip with Trail Pulse started in Vientiane.",
]
INDEPENDENCE = [
    "This is an independent review of the Laos loop.",
    "Our honest review: the trail is steep.",
    "This post is not sponsored.",
    "We were not paid to write this.",
    "We have no affiliation with any operator.",
    "An unbiased look at the Bolaven Plateau.",
]
BRAND_VOICE = [  # the brand's own voice and ordinary travel copy — never flagged
    "We pick you up at the hotel at dawn.",
    "Our team plans every day around the river.",
    "Our guides grew up in Luang Prabang.",
    "We recommend light shoes for the temple steps.",
    "Travel with an independent local guide who knows the valley.",
    "The road climbs — Luang Prabang, Laos, lies below.",
    "- Luang Prabang, Laos: a short walk.",
    "Locals call it \"the singing stones\" — Pha That Luang, Vientiane is nearby.",
    "Our trip to Laos runs for ten days.",
    "Entry to the temple is not paid separately.",
    "Stay in a five-star hotel on the last night.",
    "The trek is rated moderate.",
    "Independent travellers can join at Vientiane.",
    "Book your trip with us today.",
    "The day ends with an honest meal of sticky rice.",
    "Families with children can join the boat ride.",
    "I.e. the boat leaves at the jetty.",
    "Day 3 covers 4/5 of the loop by road.",
]


# ---------------------------------------------------------------- detector

@pytest.mark.parametrize("text", TESTIMONIALS)
def test_testimonials_are_flagged(text):
    assert [g["kind"] for g in authenticity.flagged(text)] == ["testimonial"]


@pytest.mark.parametrize("text", FIRST_PERSON)
def test_first_person_customer_voice_is_flagged(text):
    assert [g["kind"] for g in authenticity.flagged(text)] == ["first_person"]


@pytest.mark.parametrize("text", INDEPENDENCE)
def test_independence_claims_are_flagged(text):
    assert [g["kind"] for g in authenticity.flagged(text)] == ["independence"]


@pytest.mark.parametrize("text", BRAND_VOICE)
def test_brand_voice_is_never_flagged(text):
    assert authenticity.flagged(text) == []


def test_quote_split_by_the_sentence_splitter_is_one_group_with_its_attribution():
    text = "Day 2 is the river.\n“The best week of our lives. We loved every day.” — Sarah, UK\nThe boat leaves at 8."
    groups = authenticity.flagged(text)
    assert len(groups) == 1
    assert groups[0]["sentence"] == "“The best week of our lives. We loved every day.” — Sarah, UK"
    assert text[groups[0]["start"]:groups[0]["end"]] == "“The best week of our lives. We loved every day.” — Sarah, UK"


def test_attribution_line_of_its_own_is_flagged():
    text = "“We loved every single day on the river.”\n— Sarah, UK"
    assert authenticity.flagged(text)


def test_the_configured_cta_is_never_flagged():
    cta = "Read our guest reviews and ask Trail Pulse about the March dates."
    assert authenticity.flagged(f"The loop starts in Vientiane. {cta}", cta) == []
    assert authenticity.flagged(f"The loop starts in Vientiane. {cta}") != []


def test_violation_shape_is_what_sentence_repair_parses():
    v = authenticity.find_issues("The loop starts in Vientiane. I booked this trip last spring and loved it.")
    assert len(v) == 1
    problem, sentence = sentence_repair._BRAND_QUOTE_RE.search(v[0]).groups()
    assert sentence == "I booked this trip last spring and loved it."
    assert "first person" in problem and "brand's own voice" in problem


def test_title_and_meta_hits_are_prefixed_for_the_seo_fit():
    v = authenticity.find_issues("The loop starts in Vientiane.", seo_title="An Honest Review of the Laos Loop",
                                 meta_description="Rated 4.9 by travellers: ten days on the Mekong.")
    assert v[0].startswith("title ") and v[1].startswith("meta ")


def test_cut_flagged_removes_the_group_keeps_the_rest_and_never_the_cta():
    text = ("I booked this trip last spring and loved it. The dzong stands where two rivers meet.\n\n"
            "“The best week of our lives. We loved every day.” — Sarah, UK\n\n" + CTA)
    out, cut = authenticity.cut_flagged(text, CTA)
    assert len(cut) == 2
    assert "booked" not in out and "Sarah" not in out and "best week" not in out
    assert "The dzong stands where two rivers meet." in out and CTA in out
    assert authenticity.flagged(out, CTA) == []


def test_real_s232_s236_pieces_style_brand_copy_passes():
    """A typical brand-voice LinkedIn post (the S234 corpus shape: 163 real pieces scanned, 0 hits)."""
    post = ("Two rivers meet below Punakha Dzong, and the fortress sits right at the join.\n\n"
            "We walk the cantilever bridge in the morning, when the light is low. Our guides know the "
            "monks by name, and we keep the group small so the courtyard stays quiet.\n\n"
            "Built by the Zhabdrung, it is still the winter seat of the monastic body.\n\n" + CTA +
            "\n\n#Punakha #Bhutan")
    assert authenticity.flagged(post, CTA) == []


# ---------------------------------------------------------------- gate + T10 stack

def test_gate_is_blocking_and_sentence_repairable():
    r = qg.gate_authenticity("The loop starts in Vientiane. Rated 4.9 on TripAdvisor by hundreds of guests.", CTA)
    assert r["gate"] == "F19_authenticity" and not r["passed"] and r["blocking"] and r["repairable"]
    assert qg.gate_authenticity("We pick you up at dawn.", CTA)["passed"]


def test_f19_is_first_failure_ahead_of_style_gates_in_the_stack():
    f8 = {"items": [{"criterion": c, "score": "1", "evidence": "q"} for c in qg.get_framework_rubric("promotion")]}
    f9 = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
          "failure_codes": [], "flagged_phrases": [], "notes": ""}
    with patch.object(qg, "invoke_judge", side_effect=[{"text": json.dumps(f8)}, {"text": json.dumps(f9)}]), \
         patch.object(qg, "_log_t10_judge_call"):
        out = qg.run_quality_gates(
            content_text="The trail runs past the falls. I booked this trip last spring and loved it. Book now.",
            atom_text="the trail runs past the falls", cta="Book now", goal_key="promotion",
            brand_rubric_text="rubric", channel="tiktok")
    assert out["first_failure"]["gate"] == "F19_authenticity"
    assert qg.hold_failure(out["gate_ledger"])["gate"] == "F19_authenticity"


def test_plan_turns_f19_into_sentence_items_on_the_cheap_stage():
    text = "The loop starts in Vientiane. I booked this trip last spring and loved it. " + CTA
    ledger = [qg.gate_authenticity(text, CTA)]
    items = sentence_repair.plan(text, ledger)
    assert [i["sentence"] for i in items] == ["I booked this trip last spring and loved it."]
    assert "first person" in items[0]["problem"]
    assert service._style_only_repair(ledger) is True  # F19 repairs run on t9_repair


def test_title_only_f19_is_owned_by_the_seo_fit():
    text = "The loop starts in Vientiane. " + CTA
    ledger = [qg.gate_authenticity(text, CTA, seo_title="An Honest Review of the Laos Loop")]
    assert sentence_repair.plan(text, ledger) == []


# ---------------------------------------------------------------- fail-closed cut

def _f19(text, **kw):
    return qg.gate_authenticity(text, CTA, **kw)


def test_enforce_cuts_what_the_repair_left_and_waives_f19():
    text = "The loop starts in Vientiane. I booked this trip last spring and loved it. " + CTA
    ledger = [qg.gate_complete(text), _f19(text)]
    out, new, flags, status, held, entry = service._enforce_authenticity(
        text, ledger, [], "held", "F19_authenticity: …", cta=CTA, channel="facebook")
    assert "booked" not in out and CTA in out
    f19 = next(g for g in new if g["gate"] == "F19_authenticity")
    assert f19["blocking"] is False and f19["waived"] is True and f19["sentences_cut"] == 1
    assert f19["cut_sentences"] == ["I booked this trip last spring and loved it."]
    assert status == "approved" and held is None and f19 in flags
    assert entry["repair_mode"] == "aup_cut" and entry["gate_targeted"] == "F19_authenticity"


def test_enforce_cuts_the_hook_too():
    text = "I booked this trip last spring and loved it. The loop starts in Vientiane. " + CTA
    out, *_ = service._enforce_authenticity(text, [_f19(text)], [], "held", "x", cta=CTA, channel="facebook")
    assert out.startswith("The loop starts in Vientiane.")


def test_enforce_keeps_f19_blocking_when_a_title_hit_is_left():
    text = "The loop starts in Vientiane. I booked this trip last spring and loved it. " + CTA
    ledger = [_f19(text, seo_title="An Honest Review of the Laos Loop")]
    out, new, _, status, held, entry = service._enforce_authenticity(
        text, ledger, [], "held", "x", cta=CTA, channel="blog")
    assert "booked" not in out and entry["sentences_cut"] == 1
    assert new[0]["blocking"] is True and new[0]["violations"][0].startswith("title ")
    assert status == "held" and held.startswith("F19_authenticity")


def test_enforce_is_a_no_op_without_an_f19_failure():
    text = "The loop starts in Vientiane. " + CTA
    ledger = [_f19(text)]
    assert service._enforce_authenticity(text, ledger, [], "approved", None, cta=CTA, channel="facebook") == \
        (text, ledger, [], "approved", None, None)


def test_enforce_keeps_another_hold():
    text = "The loop starts in Vientiane. I booked this trip last spring and loved it. " + CTA
    f1 = {"gate": "F1_grounding", "passed": False, "blocking": True, "repairable": True,
          "violations": ["sentence states ['12'] not present in the content seed: 'x'"]}
    _, _, _, status, held, _ = service._enforce_authenticity(
        text, [f1, _f19(text)], [], "held", "x", cta=CTA, channel="facebook")
    assert status == "held" and held.startswith("F1_grounding")


# ---------------------------------------------------------------- prompts, SEO fit, admin edits

def test_writer_and_repair_prompts_carry_the_rule():
    assert "FORBIDDEN (usage policy): reviews, ratings and testimonials" in SYSTEM_PROMPT
    assert "first person" in SYSTEM_PROMPT and "independent, unbiased or unsponsored" in SYSTEM_PROMPT
    assert "Never write a review, rating, testimonial or customer quote" in _REPAIR_SYSTEM


@pytest.mark.asyncio
async def test_seo_fit_refuses_a_review_title_and_tells_the_fit_why():
    seo = {"seo_title": "An Honest Review of the Laos Mekong Loop by Boat", "slug": "laos-loop",
           "meta_description": "Ten days on the Mekong from Vientiane to Luang Prabang, with temples, "
                               "river villages and the Kuang Si falls along the way."}
    fitted = {"seo_title": "Ten Days on the Laos Mekong Loop by Slow Boat", "meta_description": None}
    with patch.object(service, "fit_seo_fields", return_value=(fitted, 0.001)) as fit:
        out, _ = await service._fit_seo_meta(seo, None, body="b", context={"atom_text": "seed"}, request_id="r")
    assert out["seo_title"] == fitted["seo_title"]
    assert "honest review" in [w.lower() for w in fit.call_args.kwargs["forbidden_words"]]


def test_admin_replacement_with_a_testimonial_is_refused():
    out = sentence_repair.check_replacement("One recent guest told us the monks were the highlight.",
                                            "the monks chant at dawn", {})
    assert any(r["gate"] == "F19_authenticity" and r["blocking"] for r in out)
    assert sentence_repair.check_replacement("The monks chant at dawn.", "the monks chant at dawn", {}) == []


# ---------------------------------------------------------------- loop-level fault fixture

SEED = "The dzong stands where two rivers meet in Punakha. It was built by the Zhabdrung."
TESTIMONIAL_PIECE = ("The dzong stands where two rivers meet in Punakha. "
                     "One recent guest told us the courtyard at dusk was the highlight of the whole trip. "
                     "It was built by the Zhabdrung. " + CTA)


def _context():
    return {"atom_text": SEED, "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "AIDA"},
            "channel_style": {"key": "facebook"}, "brand_audience": {"brand_name": "Trail Pulse"},
            "chosen": {"name": "A"}, "cta": CTA, "destination": None, "trip_name": None,
            "brand_rubric_text": "rubric", "channel": "facebook", "atom_id": "atom_1"}


def _real_gates_with_passing_judges(**kw):
    """The real T10 stack, with the two LLM judges (F8 framework, F9 brand voice) passing."""
    passing = {"passed": True, "violations": [], "repairable": False, "blocking": False}
    with patch.object(qg, "gate_framework", return_value={"gate": "F8_framework", **passing}), \
         patch.object(qg, "gate_brand_voice", return_value=[{"gate": "F9_cta_fact", **passing, "blocking": True},
                                                            {"gate": "F9_brand_style", **passing}]):
        return qg.run_quality_gates(**kw)


@pytest.mark.asyncio
async def test_fault_fixture_testimonial_is_repaired_with_gate_evidence():
    """Attempt 1 writes a guest testimonial → F19 blocks → the cheap sentence repair rewrites it in the
    brand's voice → attempt 2 passes. The evidence (F19 violation) is in the repair log; the stored text
    has no testimonial."""
    def repair(*, sentences, **kw):
        assert kw.get("stage") == service.SEO_FIT_STAGE  # the cheap t9_repair stage
        assert "testimonial" in sentences[0]["problem"]
        return ["We keep the group small, so the courtyard at dusk stays quiet."], 0.0004
    with patch.object(service, "write_content", return_value=(TESTIMONIAL_PIECE, 0.01, {}, None)), \
         patch.object(service, "repair_sentences", side_effect=repair) as rep, \
         patch.object(service, "rewrite_with_feedback") as rw, \
         patch.object(service, "run_quality_gates", side_effect=_real_gates_with_passing_judges), \
         patch.object(service, "observe_t10", new=AsyncMock()), \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(uuid4(), uuid4(), _context(), pool=MagicMock())
    kw = fin.call_args.kwargs
    assert rep.call_count == 1 and rw.call_count == 0
    assert kw["status"] == "approved", kw["held_reason"]
    assert "One recent guest" not in kw["content_text"] and "courtyard at dusk stays quiet" in kw["content_text"]
    first = kw["repair_log"][0]
    assert first["gate_targeted"] == "F19_authenticity" and first["repair_mode"] == "sentences"
    assert "One recent guest told us" in first["violations"][0]
    assert next(g for g in kw["gate_ledger"] if g["gate"] == "F19_authenticity")["passed"] is True


@pytest.mark.asyncio
async def test_fault_fixture_repair_that_keeps_the_testimonial_is_cut_not_shipped():
    """Every repair returns the testimonial unchanged → after the last attempt it is cut in code and F19
    is stored waived with the cut sentence as evidence; the piece ships without it."""
    def same(*, sentences, **_):
        return [s["sentence"] for s in sentences], 0.0
    with patch.object(service, "write_content", return_value=(TESTIMONIAL_PIECE, 0.01, {}, None)), \
         patch.object(service, "repair_sentences", side_effect=same), \
         patch.object(service, "rewrite_with_feedback", return_value=(TESTIMONIAL_PIECE, 0.01, {}, None)), \
         patch.object(service, "run_quality_gates", side_effect=_real_gates_with_passing_judges), \
         patch.object(service, "observe_t10", new=AsyncMock()), \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(uuid4(), uuid4(), _context(), pool=MagicMock())
    kw = fin.call_args.kwargs
    assert "One recent guest" not in kw["content_text"]
    assert "The dzong stands where two rivers meet in Punakha." in kw["content_text"] and CTA in kw["content_text"]
    f19 = next(g for g in kw["gate_ledger"] if g["gate"] == "F19_authenticity")
    assert f19["waived"] is True and f19["sentences_cut"] == 1
    assert "One recent guest told us" in f19["cut_sentences"][0]
    assert any(r.get("repair_mode") == "aup_cut" for r in kw["repair_log"])
    assert kw["status"] == "approved" and kw["content_summary"] is None
