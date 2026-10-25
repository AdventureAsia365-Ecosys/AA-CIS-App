"""AA-450 — services/acp_content_writing/quality_gates.py. F1/F2/F4-adjusted are pure functions
(no mocking needed); F9-adjusted patches invoke_judge, same convention gates.py's own test
suite and test_aa449_angle_gate_generate.py both already use. (F8_framework was dropped by AA-787.)"""
import json
from unittest.mock import patch

from services.acp_content_writing import quality_gates as qg


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

    def test_non_pass_with_no_recognized_code_is_a_warn(self):
        """AA-786 (Gate-1 policy) — a verdict with no checkable reason is flagged, never held."""
        data = {"status": "manual_check", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                 "failure_codes": [], "flagged_phrases": [], "notes": "unsure about something"}
        with patch.object(qg, "invoke_judge", return_value=_judge_raw(**data)):
            results = qg.gate_brand_voice("piece", "Book now", "brand rubric text")
        by_gate = self._by_gate(results)
        assert by_gate["F9_cta_fact"]["passed"] is False
        assert by_gate["F9_cta_fact"]["blocking"] is False

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
                content_text="piece", atom_text="atom", cta=None,
                brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is False
        assert outcome["first_failure"]["gate"] == "F6_cta_present"
        assert outcome["first_failure"]["repairable"] is False
        mock_judge.assert_not_called()

    def test_all_gates_pass(self):
        f9_data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                    "failure_codes": [], "flagged_phrases": [], "notes": ""}
        with patch.object(qg, "invoke_judge", side_effect=[_judge_raw(**f9_data)]):
            outcome = qg.run_quality_gates(
                content_text="A clean, specific piece about the trail.", atom_text="the trail",
                cta="Book now", brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is True
        # AA-514: + promises_an_option (runs for every channel now, not just blog) — AA-787: dropped
        # AA-484: + F10_cannibalization_cross_tenant (runs for every channel, no match here since
        # this call passes no cannibalization_match — always passes with 0 violations).
        # AA-613: F9 now yields TWO results (F9_cta_fact + F9_brand_style), so 9 not 8.
        # AA-768 (S233): + F0_complete; AA-771: + F2_brand_forbidden, F11_brand_name.
        # AA-768 (S234): + F12_scope, F13_hygiene (warn; pass without a hygiene context).
        # AA-771 (S234): + F14_social_shape, F15_voice, F15_voice_warn (pass for a channel without a spec).
        # ...+f12+f13+f13_leak+f13_repeat+f17+f18 (S234 round 3)+f14+f15+f15w+f8+f9x2
        # AA-819: + F19_authenticity (after F11, every channel).
        # AA-800: + F2_platform_truth, F2_superlatives, F2_style_warn (the word layers, every channel).
        # AA-787: - promises_an_option, - F8_framework (25 → 23).
        # AA-924: F2_banned_patterns + F2_brand_forbidden → F2_lexicon, F15_voice_warn → F15_voice (23 → 21).
        assert len(outcome["gate_ledger"]) == 21
        assert not {"promises_an_option", "F8_framework"} & {g["gate"] for g in outcome["gate_ledger"]}

    def test_first_det_failure_used_for_repair_targeting(self):
        f9_data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                    "failure_codes": [], "flagged_phrases": [], "notes": ""}
        with patch.object(qg, "invoke_judge", side_effect=[_judge_raw(**f9_data)]):
            outcome = qg.run_quality_gates(
                content_text="This breathtaking view awaits you.", atom_text="a view",
                cta="Book now", brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is False
        assert outcome["first_failure"]["gate"] == "F2_banned_patterns"
        assert outcome["first_failure"]["repairable"] is True


class TestRunQualityGatesFlagNotBlock:
    """AA-519 Việc 5 — a failed non-blocking (warn) gate ships the piece (`passed=True`, never
    `first_failure`, collected in `flags`); a real DET failure still holds/repairs as before.
    AA-787: promises_an_option, the first warn gate, is gone — an offered moment stated as planned is
    F17_trip_fact's (blocking, sentence-repairable) when the trip-fact context is given."""

    def _judge_pass(self):
        f9_data = {"status": "pass", "brand_fit": "1", "cta_clear": "1", "human_read": "1",
                   "failure_codes": [], "flagged_phrases": [], "notes": ""}
        return [_judge_raw(**f9_data)]

    def test_offered_moment_stated_as_definite_is_no_longer_a_flag(self):
        atom_text = "The temple visit is optional, at your own expense, weather permitting."
        content = "You will visit the temple at dawn."  # states the offered moment as definite
        with patch.object(qg, "invoke_judge", side_effect=self._judge_pass()):
            outcome = qg.run_quality_gates(
                content_text=content, atom_text=atom_text, cta="Book now",
                brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is True
        assert "promises_an_option" not in {g["gate"] for g in outcome["gate_ledger"] + outcome["flags"]}

    def test_other_8_gates_keep_blocking_unaffected_by_this_change(self):
        """Regression guard — a real DET failure (F2, blocking=True by _result()'s own default)
        still becomes first_failure and holds, exactly as before AA-519."""
        with patch.object(qg, "invoke_judge", side_effect=self._judge_pass()):
            outcome = qg.run_quality_gates(
                content_text="This breathtaking view awaits you.", atom_text="a view",
                cta="Book now", brand_rubric_text="rubric", channel="facebook",
            )
        assert outcome["passed"] is False
        assert outcome["first_failure"]["gate"] == "F2_banned_patterns"
        assert outcome["first_failure"]["blocking"] is True
        # AA-800: the one stock superlative ('breathtaking') is a warning now, not a banned pattern
        assert [g["gate"] for g in outcome["flags"]] == ["F2_style_warn"]

    def test_blocking_flags_match_the_3_tier_severity_model(self):
        # AA-613 — the non-blocking (warn/note) gates are: F9_brand_style (warn); promises_an_option (note)
        # and F8_framework (warn) were dropped by AA-787. Everything else (product-truth /
        # structural / SEO) stays blocking.
        # AA-768 (S234): + F12_scope (warn for one round) and F13_hygiene (warn).
        # AA-771 (S234): F14/F15 block only on their own triggers; a passing result is non-blocking.
        # AA-800: F2_superlatives blocks only on its own trigger (a 2nd superlative); F2_style_warn is warn-only.
        _NON_BLOCKING = {"F9_brand_style", "F12_scope", "F13_hygiene",
                         "F14_social_shape", "F15_voice", "F15_voice_warn", "F2_superlatives", "F2_style_warn"}
        with patch.object(qg, "invoke_judge", side_effect=self._judge_pass()):
            outcome = qg.run_quality_gates(
                content_text="A clean, specific piece about the trail.", atom_text="the trail",
                cta="Book now", brand_rubric_text="rubric", channel="facebook",
            )
        for g in outcome["gate_ledger"]:
            expected = g["gate"] not in _NON_BLOCKING
            assert g["blocking"] is expected, f"{g['gate']} blocking={g['blocking']}, expected {expected}"
