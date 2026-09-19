"""AA-450 — services/acp_content_writing/quality_gates.py. F1/F2/F4-adjusted are pure functions
(no mocking needed); F8/F9-adjusted patch invoke_judge, same convention gates.py's own test
suite and test_aa449_angle_gate_generate.py both already use."""
import json
from unittest.mock import patch

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing.framework_rubrics import DEFAULT_RUBRIC


class TestGateCtaPresent:
    def test_missing_cta_fails_non_repairable(self):
        result = qg.gate_cta_present(None)
        assert result["passed"] is False
        assert result["repairable"] is False

    def test_empty_string_cta_fails_non_repairable(self):
        result = qg.gate_cta_present("   ")
        assert result["passed"] is False
        assert result["repairable"] is False

    def test_real_cta_passes(self):
        result = qg.gate_cta_present("Book a consultation")
        assert result["passed"] is True


class TestGateGrounding:
    def test_no_fabricated_number_passes(self):
        atom = "The bridge is 22 meters long."
        content = "Cross the 22 meter bridge at dawn for a quiet start to the day."
        result = qg.gate_grounding(content, atom)
        assert result["passed"] is True

    def test_fabricated_number_fails(self):
        atom = "A quiet mountain trail near the village."
        content = "The trail stretches 45 kilometers through dense forest."
        result = qg.gate_grounding(content, atom)
        assert result["passed"] is False
        assert result["repairable"] is True


class TestGateBannedPatterns:
    def test_clean_text_passes(self):
        result = qg.gate_banned_patterns("A quiet morning by the river.", "")
        assert result["passed"] is True

    def test_banned_phrase_fails(self):
        result = qg.gate_banned_patterns("This breathtaking view awaits you.", "")
        assert result["passed"] is False

    def test_skill_v2_only_phrase_also_caught(self):
        # "game-changing" is in SKILL_v2.md's Avoid list but NOT in N7's own BANNED_PATTERNS_SEED
        # — confirms the union, not just a copy of N7's list (AA-450-02 gate map, F2 row).
        result = qg.gate_banned_patterns("This is a truly game-changing itinerary.", "")
        assert result["passed"] is False

    def test_verbatim_atom_text_exempt(self):
        atom = "Locals call it a hidden gem among the northern provinces."
        result = qg.gate_banned_patterns("Locals call it a hidden gem among the provinces.", atom)
        assert result["passed"] is True


class TestGateExtremeLength:
    def test_normal_length_passes(self):
        assert qg.gate_extreme_length("A" * 200)["passed"] is True

    def test_empty_fails(self):
        assert qg.gate_extreme_length("")["passed"] is False

    def test_runaway_length_fails(self):
        assert qg.gate_extreme_length("A" * 7000)["passed"] is False

    # AA-531 — channel=None (the default, every pre-existing call site above) keeps the
    # original 7-channel-shaped 20/6000 band untouched; `channel='blog'` gets its own wider band.
    def test_default_channel_none_unaffected_by_blog_band(self):
        # 7000 chars still fails the SHARED band when channel is omitted/None — confirms this
        # fix did not accidentally widen the default for non-blog callers.
        assert qg.gate_extreme_length("A" * 7000, None)["passed"] is False

    def test_blog_channel_7000_chars_now_passes(self):
        # Real-world shape: the 3 wanderlux-travel pieces this issue is about (6,679-7,266 chars)
        # were held by the old shared 6,000-char ceiling despite being under blog's real
        # 2,200-word (~13,000 char) ceiling.
        assert qg.gate_extreme_length("A" * 7000, "blog")["passed"] is True

    def test_blog_channel_below_blog_floor_fails(self):
        # Below the blog-specific 4,700-char floor (~800 words) even though it's well above the
        # generic 20-char floor — the issue's own "secondary note" (no real blog floor before).
        result = qg.gate_extreme_length("A" * 3000, "blog")
        assert result["passed"] is False
        assert "effectively empty" in result["violations"][0]

    def test_blog_channel_above_blog_ceiling_fails(self):
        assert qg.gate_extreme_length("A" * 13500, "blog")["passed"] is False

    def test_blog_channel_within_band_passes(self):
        # Midpoint of the 4,700-13,000 char blog band.
        assert qg.gate_extreme_length("A" * 9000, "blog")["passed"] is True

    def test_non_blog_channel_still_uses_shared_band(self):
        # A named non-blog channel (not just None) also keeps the shared 20/6000 band — confirms
        # the dispatch is "blog vs. everything else", not "blog vs. None".
        assert qg.gate_extreme_length("A" * 7000, "facebook")["passed"] is False
        assert qg.gate_extreme_length("A" * 200, "facebook")["passed"] is True


def _judge_raw(**fields) -> dict:
    return {"text": json.dumps(fields)}


class TestGateFramework:
    def test_all_criteria_met_passes(self):
        rubric = qg.get_framework_rubric("promotion")
        data = {"items": [{"criterion": c, "score": "1", "evidence": "quote"} for c in rubric]}
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(**data)):
            result = qg.gate_framework("some piece", "promotion")
        assert result["passed"] is True

    def test_unmet_criterion_fails(self):
        rubric = qg.get_framework_rubric("promotion")
        items = [{"criterion": c, "score": "1", "evidence": "quote"} for c in rubric]
        items[0]["score"] = "0"
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(items=items)):
            result = qg.gate_framework("some piece", "promotion")
        assert result["passed"] is False

    def test_score_1_without_evidence_treated_as_fail(self):
        rubric = qg.get_framework_rubric("promotion")
        items = [{"criterion": c, "score": "1", "evidence": ""} for c in rubric]
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(items=items)):
            result = qg.gate_framework("some piece", "promotion")
        assert result["passed"] is False

    def test_judge_exception_treated_as_fail_not_crash(self):
        with patch.object(qg, "invoke_judge", side_effect=RuntimeError("bedrock down")):
            result = qg.gate_framework("some piece", "promotion")
        assert result["passed"] is False

    def test_covers_goal_with_no_n7_equivalent_framework(self):
        # product_service_explanation -> FAB, which does NOT exist in N7's own FRAMEWORK_RUBRICS
        # (AA-450-02 gate map, F8 row) — confirms the derived rubric, not a silent DEFAULT_RUBRIC.
        rubric = qg.get_framework_rubric("product_service_explanation")
        assert rubric != DEFAULT_RUBRIC
        assert any("Feature" in c for c in rubric)


class TestGateBrandVoice:
    """AA-613 — gate_brand_voice now returns a LIST of two results: F9_cta_fact (blocking,
    product-truth) + F9_brand_style (non-blocking warn)."""

    def _by_gate(self, results):
        return {r["gate"]: r for r in results}

    def test_pass_status_both_results_pass(self):
        data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                 "failure_codes": [], "flagged_phrases": [], "notes": ""}
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(**data)):
            results = qg.gate_brand_voice("piece", "Book now", "brand rubric text")
        by_gate = self._by_gate(results)
        assert by_gate["F9_cta_fact"]["passed"] is True
        assert by_gate["F9_brand_style"]["passed"] is True

    def test_brand_style_failure_is_non_blocking_warn(self):
        data = {"status": "flagged", "brand_fit": "0", "cta_clear": "1", "human_read": "1",
                 "failure_codes": ["SUMMARY_OFF_BRAND"], "flagged_phrases": ["generic phrase"],
                 "notes": "reads generic"}
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(**data)):
            results = qg.gate_brand_voice("piece", "Book now", "brand rubric text")
        by_gate = self._by_gate(results)
        # brand-style code -> the non-blocking warn result fails; cta_fact (blocking) stays clean
        assert by_gate["F9_brand_style"]["passed"] is False
        assert by_gate["F9_brand_style"]["blocking"] is False
        assert "generic phrase" in by_gate["F9_brand_style"]["violations"][0]
        assert by_gate["F9_cta_fact"]["passed"] is True

    def test_product_truth_failure_is_blocking(self):
        data = {"status": "manual_check", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                 "failure_codes": ["FACT_CHECK_MANUAL_CHECK"], "flagged_phrases": [], "notes": ""}
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(**data)):
            results = qg.gate_brand_voice("piece", "Book now", "brand rubric text")
        by_gate = self._by_gate(results)
        assert by_gate["F9_cta_fact"]["passed"] is False
        assert by_gate["F9_cta_fact"]["blocking"] is True
        assert by_gate["F9_brand_style"]["passed"] is True

    def test_cta_not_reflected_is_blocking(self):
        data = {"status": "flagged", "brand_fit": "1", "cta_clear": "0", "human_read": "1",
                 "failure_codes": ["CTA_NOT_REFLECTED"], "flagged_phrases": [], "notes": ""}
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(**data)):
            results = qg.gate_brand_voice("piece", "Book now", "brand rubric text")
        by_gate = self._by_gate(results)
        assert by_gate["F9_cta_fact"]["passed"] is False
        assert by_gate["F9_brand_style"]["passed"] is True

    def test_non_pass_with_no_recognized_code_fails_closed_blocking(self):
        data = {"status": "manual_check", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                 "failure_codes": [], "flagged_phrases": [], "notes": "unsure about something"}
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(**data)):
            results = qg.gate_brand_voice("piece", "Book now", "brand rubric text")
        by_gate = self._by_gate(results)
        assert by_gate["F9_cta_fact"]["passed"] is False  # fail-closed on the blocking side

    def test_judge_unavailable_fails_closed_blocking(self):
        with patch.object(qg, "invoke_judge", side_effect=RuntimeError("judge down")):
            results = qg.gate_brand_voice("piece", "Book now", "brand rubric text")
        by_gate = self._by_gate(results)
        assert by_gate["F9_cta_fact"]["passed"] is False
        assert by_gate["F9_brand_style"]["passed"] is True

    def test_required_cta_reaches_the_prompt(self):
        data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                 "failure_codes": [], "flagged_phrases": [], "notes": ""}
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(**data)) as mock_judge:
            qg.gate_brand_voice("piece", "Book a consultation today", "brand rubric text")
        user_prompt = mock_judge.call_args[0][1]
        assert "Book a consultation today" in user_prompt


class TestRunQualityGates:
    def _passing_data(self, fields):
        return _judge_raw(**fields)

    def test_missing_cta_short_circuits_before_any_judge_call(self):
        with patch.object(qg, "invoke_judge") as mock_judge:
            outcome = qg.run_quality_gates(
                content_text="piece", atom_text="atom", cta=None, goal_key="promotion",
                brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is False
        assert outcome["first_failure"]["gate"] == "F6_cta_present"
        assert outcome["first_failure"]["repairable"] is False
        mock_judge.assert_not_called()

    def test_all_gates_pass(self):
        rubric = qg.get_framework_rubric("promotion")
        f8_data = {"items": [{"criterion": c, "score": "1", "evidence": "q"} for c in rubric]}
        f9_data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                    "failure_codes": [], "flagged_phrases": [], "notes": ""}
        with patch.object(qg, "invoke_judge", side_effect=[_judge_raw(**f8_data), _judge_raw(**f9_data)]):
            outcome = qg.run_quality_gates(
                content_text="A clean, specific piece about the trail.", atom_text="the trail",
                cta="Book now", goal_key="promotion", brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is True
        # AA-514: + promises_an_option (runs for every channel now, not just blog)
        # AA-484: + F10_cannibalization_cross_tenant (runs for every channel, no match here since
        # this call passes no cannibalization_match — always passes with 0 violations).
        # AA-613: F9 now yields TWO results (F9_cta_fact + F9_brand_style), so 9 not 8.
        assert len(outcome["gate_ledger"]) == 9  # cta+grounding+banned+cannib+promises+length+f8+f9_cta_fact+f9_brand_style

    def test_first_det_failure_used_for_repair_targeting(self):
        rubric = qg.get_framework_rubric("promotion")
        f8_data = {"items": [{"criterion": c, "score": "1", "evidence": "q"} for c in rubric]}
        f9_data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                    "failure_codes": [], "flagged_phrases": [], "notes": ""}
        with patch.object(qg, "invoke_judge", side_effect=[_judge_raw(**f8_data), _judge_raw(**f9_data)]):
            outcome = qg.run_quality_gates(
                content_text="This breathtaking view awaits you.", atom_text="a view",
                cta="Book now", goal_key="promotion", brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is False
        assert outcome["first_failure"]["gate"] == "F2_banned_patterns"
        assert outcome["first_failure"]["repairable"] is True


class TestRunQualityGatesFlagNotBlock:
    """AA-519 Việc 5 — ADR 0023/0026 (Ms. Thư repo): a Piece whose ONLY violation is
    promises_an_option ships (`passed=True`, gate never becomes `first_failure`, held out
    separately in `flags`) rather than being held. The other 8 gates are completely unaffected —
    a real DET failure among them still holds/repairs exactly as before this change."""

    def _judge_pass(self):
        rubric = qg.get_framework_rubric("promotion")
        f8_data = {"items": [{"criterion": c, "score": "1", "evidence": "q"} for c in rubric]}
        f9_data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                   "failure_codes": [], "flagged_phrases": [], "notes": ""}
        return [_judge_raw(**f8_data), _judge_raw(**f9_data)]

    def test_only_promises_an_option_violation_still_passes_and_is_flagged(self):
        atom_text = "The temple visit is optional, at your own expense, weather permitting."
        content = "You will visit the temple at dawn."  # states the offered moment as definite
        with patch.object(qg, "invoke_judge", side_effect=self._judge_pass()):
            outcome = qg.run_quality_gates(
                content_text=content, atom_text=atom_text, cta="Book now", goal_key="promotion",
                brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is True  # ships -- ADR 0023, "every Piece ships"
        assert outcome["first_failure"] is None  # never selected -- blocking=False
        assert len(outcome["flags"]) == 1
        assert outcome["flags"][0]["gate"] == "promises_an_option"
        assert outcome["flags"][0]["passed"] is False
        # the gate's own violation is still in gate_ledger too (nothing silently dropped)
        ledger_entry = next(g for g in outcome["gate_ledger"] if g["gate"] == "promises_an_option")
        assert ledger_entry["passed"] is False
        assert ledger_entry["blocking"] is False

    def test_other_8_gates_keep_blocking_unaffected_by_this_change(self):
        """Regression guard — a real DET failure (F2, blocking=True by _result()'s own default)
        still becomes first_failure and holds, exactly as before AA-519."""
        with patch.object(qg, "invoke_judge", side_effect=self._judge_pass()):
            outcome = qg.run_quality_gates(
                content_text="This breathtaking view awaits you.", atom_text="a view",
                cta="Book now", goal_key="promotion", brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is False
        assert outcome["first_failure"]["gate"] == "F2_banned_patterns"
        assert outcome["first_failure"]["blocking"] is True
        assert outcome["flags"] == []

    def test_blocking_flags_match_the_3_tier_severity_model(self):
        # AA-613 — the non-blocking (warn/note) gates are: promises_an_option (note),
        # F8_framework (warn), F9_brand_style (warn). Everything else (product-truth /
        # structural / SEO) stays blocking.
        _NON_BLOCKING = {"promises_an_option", "F8_framework", "F9_brand_style"}
        with patch.object(qg, "invoke_judge", side_effect=self._judge_pass()):
            outcome = qg.run_quality_gates(
                content_text="A clean, specific piece about the trail.", atom_text="the trail",
                cta="Book now", goal_key="promotion", brand_rubric_text="rubric", channel="facebook",
            )
        for g in outcome["gate_ledger"]:
            expected = g["gate"] not in _NON_BLOCKING
            assert g["blocking"] is expected, f"{g['gate']} blocking={g['blocking']}, expected {expected}"
