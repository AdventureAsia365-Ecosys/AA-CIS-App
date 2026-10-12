"""AA-768 — T9 source fidelity (S231 pilot: 5/5 pieces held on F1_grounding / F9 manual_check).

Covers the four changes: the writer prompt's A/B + numbers rule, the gates' source text now
including the audience/trip lines, the F9 judge seeing that source and quoting what it flags,
and the rewrite receiving every blocking failure instead of only the first.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service
from services.acp_content_writing.prompts import SYSTEM_PROMPT, build_user_prompt


def _judge(**fields) -> dict:
    return {"text": json.dumps(fields)}


class TestWriterPrompt:
    def test_system_prompt_has_numbers_rule(self):
        assert "SOURCE FIDELITY" in SYSTEM_PROMPT
        assert "ONLY if that exact number appears" in SYSTEM_PROMPT

    def test_revision_prompt_says_remove_not_replace(self):
        prompt = build_user_prompt(
            content_seed="seed", goal={"name": "g", "description": "d", "logic": "l", "key": "k"},
            channel_style={"channel": "linkedin", "display_name": "LinkedIn", "use_when": "u",
                           "structure": "s", "style": "st", "avoid": "a"},
            brand_audience={}, angle={"name": "a", "why_it_works": "w", "best_final_style": "b"},
            cta="Talk to us", revision_feedback=["F1_grounding: sentence states ['900'] ..."],
        )
        assert "Never replace it with a different number" in prompt


class TestGroundingText:
    def test_includes_audience_trip_destination_and_facts(self):
        text = service._grounding_text({
            "atom_text": "Ride to the homestay.", "facts_text": "[Fact id=f1] e-visa",
            "brand_audience": {"customer_segment": "Families with children aged 7–16"},
            "trip_name": "Laos to Vietnam by bicycle 12 days", "destination": "Laos",
        })
        for part in ("Ride to the homestay.", "[Fact id=f1]", "aged 7–16", "12 days", "Laos"):
            assert part in text

    def test_audience_number_no_longer_fails_f1(self):
        ctx = {"atom_text": "Ride to the homestay.",
               "brand_audience": {"customer_segment": "Families with children aged 7–16"}}
        piece = "The ride suits children aged 7 to 16."
        assert qg.gate_grounding(piece, ctx["atom_text"])["passed"] is False
        assert qg.gate_grounding(piece, service._grounding_text(ctx))["passed"] is True

    def test_minimal_context_is_just_the_atom(self):
        assert service._grounding_text({"atom_text": "seed"}) == "seed"


class TestF9SeesSource:
    _pass = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
             "failure_codes": [], "flagged_phrases": [], "notes": ""}

    def test_source_reaches_judge_prompt(self):
        with patch.object(qg, "invoke_judge", return_value=_judge(**self._pass)) as mock_judge:
            qg.gate_brand_voice("piece", "Book", "rubric", source_text="Paro Taktsang hike")
        user_prompt = mock_judge.call_args[0][1]
        assert "SOURCE (everything the writer was given as fact):\nParo Taktsang hike" in user_prompt
        assert "When uncertain about a factual claim" not in user_prompt

    def test_without_source_keeps_old_rule(self):
        with patch.object(qg, "invoke_judge", return_value=_judge(**self._pass)) as mock_judge:
            qg.gate_brand_voice("piece", "Book", "rubric")
        assert "When uncertain about a factual claim" in mock_judge.call_args[0][1]

    def test_fact_check_violation_quotes_the_claim(self):
        data = {**self._pass, "status": "manual_check", "failure_codes": ["FACT_CHECK_MANUAL_CHECK"],
                "flagged_phrases": ["Every guest gets a private chef."],
                "unsupported_claims": [{"sentence": "Every guest gets a private chef.",
                                        "claim": "a private chef is included",
                                        "why_not_in_source": "the source lists no meals"}]}
        with patch.object(qg, "invoke_judge", return_value=_judge(**data)):
            results = qg.gate_brand_voice("piece", "Book", "rubric", source_text="seed")
        cta_fact = next(r for r in results if r["gate"] == "F9_cta_fact")
        assert cta_fact["passed"] is False
        assert "Every guest gets a private chef." in cta_fact["violations"][0]
        assert "the source lists no meals" in cta_fact["violations"][0]

    def test_unjustified_fact_flag_is_dropped(self):
        """Round 3: the judge flagged a sentence the SOURCE states verbatim, with no claim/reason."""
        data = {**self._pass, "status": "manual_check", "failure_codes": ["FACT_CHECK_MANUAL_CHECK"],
                "flagged_phrases": ["Pawangka was built by Songtsen Gampo."], "unsupported_claims": []}
        with patch.object(qg, "invoke_judge", return_value=_judge(**data)):
            results = qg.gate_brand_voice("piece", "Book", "rubric", source_text="built by Songtsen Gampo")
        assert next(r for r in results if r["gate"] == "F9_cta_fact")["passed"] is True

    def test_dropped_fact_flag_keeps_other_codes(self):
        data = {**self._pass, "status": "flagged", "failure_codes": ["FACT_CHECK_MANUAL_CHECK", "CTA_NOT_REFLECTED"],
                "unsupported_claims": [{"claim": "x"}]}  # no reason -> not justified
        with patch.object(qg, "invoke_judge", return_value=_judge(**data)):
            results = qg.gate_brand_voice("piece", "Book", "rubric", source_text="seed")
        assert next(r for r in results if r["gate"] == "F9_cta_fact")["violations"] == [
            "audit flagged: CTA_NOT_REFLECTED"]

    def test_without_source_fact_flag_is_not_second_guessed(self):
        data = {**self._pass, "status": "manual_check", "failure_codes": ["FACT_CHECK_MANUAL_CHECK"]}
        with patch.object(qg, "invoke_judge", return_value=_judge(**data)):
            results = qg.gate_brand_voice("piece", "Book", "rubric")
        assert next(r for r in results if r["gate"] == "F9_cta_fact")["passed"] is False

    def test_cta_only_failure_does_not_get_phrases(self):
        data = {**self._pass, "status": "flagged", "failure_codes": ["CTA_NOT_REFLECTED"],
                "flagged_phrases": ["unrelated"]}
        with patch.object(qg, "invoke_judge", return_value=_judge(**data)):
            results = qg.gate_brand_voice("piece", "Book", "rubric", source_text="seed")
        cta_fact = next(r for r in results if r["gate"] == "F9_cta_fact")
        assert cta_fact["violations"] == ["audit flagged: CTA_NOT_REFLECTED"]

    def test_run_quality_gates_passes_source_to_f9(self):
        with patch.object(qg, "gate_brand_voice", return_value=[
            qg._result("F9_cta_fact", []), qg._result("F9_brand_style", [], blocking=False)]) as mock_bv, \
             patch.object(qg, "gate_framework", return_value=qg._result("F8_framework", [])):
            qg.run_quality_gates(content_text="A short piece.", atom_text="SEED TEXT", cta="Book",
                                 goal_key="promotion", brand_rubric_text="r", channel="linkedin")
        assert mock_bv.call_args.kwargs["source_text"] == "SEED TEXT"


class TestRepairFeedback:
    def test_every_blocking_failure_is_sent(self):
        ledger = [
            {"gate": "F1_grounding", "passed": False, "blocking": True, "violations": ["a", "b"]},
            {"gate": "F4_seo_surface", "passed": False, "blocking": True, "violations": ["c"]},
            {"gate": "F8_framework", "passed": False, "blocking": False, "violations": ["warn"]},
            {"gate": "F2_banned_patterns", "passed": True, "blocking": True, "violations": []},
        ]
        assert service._repair_feedback(ledger) == [
            "F1_grounding: a", "F1_grounding: b", "F4_seo_surface: c"]

    @pytest.mark.asyncio
    async def test_rewrite_receives_all_blocking_failures(self):
        f1 = {"gate": "F1_grounding", "passed": False, "blocking": True, "repairable": True,
              "violations": ["number 900"]}
        f9 = {"gate": "F9_cta_fact", "passed": False, "blocking": True, "repairable": True,
              "violations": ["audit manual_check: FACT_CHECK_MANUAL_CHECK"]}
        failing = {"passed": False, "gate_ledger": [f1, f9], "first_failure": f1, "flags": []}
        passing = {"passed": True, "gate_ledger": [], "first_failure": None, "flags": []}
        context = {
            "atom_text": "seed", "goal": {"key": "promotion"}, "channel_style": {"channel": "linkedin"},
            "brand_audience": {}, "chosen": {}, "cta": "Book", "destination": None, "trip_name": None,
            "brand_rubric_text": "", "channel": "linkedin", "atom_id": "a1",
        }
        row = {"piece_id": str(uuid4()), "attempt_number": 2, "status": "approved"}
        with patch.object(service, "write_content", return_value=("d1", 0.01, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("d2", 0.01, {}, None)) as mock_rw, \
             patch.object(service, "run_quality_gates", side_effect=[failing, passing]), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=row)):
            await service.run_write_background(uuid4(), uuid4(), context, pool=MagicMock())
        assert mock_rw.call_args.kwargs["revision_feedback"] == [
            "F1_grounding: number 900", "F9_cta_fact: audit manual_check: FACT_CHECK_MANUAL_CHECK"]


def test_f9_rule_excludes_opinions_brand_approach_and_cta():
    """AA-768 round 2 — live re-run: the judge flagged rhetoric and the brand's own approach."""
    data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
            "failure_codes": [], "flagged_phrases": [], "notes": ""}
    with patch.object(qg, "invoke_judge", return_value=_judge(**data)) as mock_judge:
        qg.gate_brand_voice("piece", "Book", "rubric", source_text="seed")
    prompt = mock_judge.call_args[0][1]
    assert "checkable statement of fact" in prompt
    for excluded in ("opinions", "rhetorical", "describing its own approach", "required call to action"):
        assert excluded in prompt


def test_brand_name_reaches_prompt_and_gate_source():
    """S232 re-run: Trail Pulse pieces called the brand 'Adventure Airlines' — the name was never given."""
    kwargs = dict(content_seed="seed", goal={"name": "g", "description": "d", "logic": "l", "key": "k"},
                  channel_style={"channel": "linkedin", "display_name": "LinkedIn", "use_when": "u",
                                 "structure": "s", "style": "st", "avoid": "a"},
                  angle={"name": "a", "why_it_works": "w", "best_final_style": "b"}, cta="Book")
    prompt = build_user_prompt(brand_audience={"brand_name": "Trail Pulse"}, **kwargs)
    assert "BRAND NAME (the only name to use for the brand — never invent another): Trail Pulse" in prompt
    assert "BRAND NAME" not in build_user_prompt(brand_audience={}, **kwargs)
    assert "BRAND: Trail Pulse" in service._grounding_text(
        {"atom_text": "seed", "brand_audience": {"brand_name": "Trail Pulse"}})


class TestSeoFit:
    def test_long_meta_and_title_are_fitted_into_band(self):
        meta = ("Discover Bhutan's craft bazaar and textile traditions in Thimphu on a private journey; "
                "meet weavers, see the Textile Museum, and taste the farmers market at your own pace.")
        assert len(meta) > 158
        out = service._fit_seo_meta({"seo_title": "Bhutan Craft Bazaar Guide — Textiles, Weavers and Markets | Atlas",
                                     "meta_description": meta, "slug": "s"}, keyword="bhutan")
        assert len(out["seo_title"]) <= 60 and "bhutan" in out["seo_title"].lower()
        assert 120 <= len(out["meta_description"]) <= 158 or out["meta_description"] == meta
        assert out["slug"] == "s"

    def test_in_band_values_untouched(self):
        meta = "A" * 130 + "."
        assert service._fit_seo_meta({"seo_title": "Short", "meta_description": meta}, None) == {
            "seo_title": "Short", "meta_description": meta}

    def test_fit_that_drops_keyword_is_rejected(self):
        title = "Kyichu Lhakhang and the Paro Valley: a quiet guide for Bhutan travellers"
        out = service._fit_seo_meta({"seo_title": title}, keyword="bhutan travellers")
        assert out["seo_title"] == title or "bhutan travellers" in out["seo_title"].lower()
