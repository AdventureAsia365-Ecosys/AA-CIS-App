"""S279 thresholds lane L2 (AA-1096, CIS 409) — where each quality value lives in the settings registry.

Every key the quality registry, the T10 / seed-align / T7 / T2 modes, the claim verdicts, the detector modes, the SEO
bands and the T7 Channel bars read through ``shared.settings`` (Platform › Settings, evidence-gated, maker-checker).
Readers name a key through these maps, never a copied value: ``settings.get(settings_keys.QUALITY['pass_line'])``.
Standard library only (the CI Lint job imports the quality registry with PyYAML alone). The keys equal the CIS 409
seed: tests/unit/quality/test_s279_quality_settings_keys.py checks both ways.
"""
from __future__ import annotations

# services/quality/thresholds.yaml key -> setting (an entry with `ref:` reads its target's setting)
QUALITY: dict[str, str] = {
    "pass_line": "cis.quality.pass_line",
    "a1.missing_field_cap": "cis.quality.a1_missing_field_cap",
    "judge.tie_band": "cis.quality.judge_tie_band",
    "judge.mission_absent_cap": "cis.quality.judge_mission_absent_cap",
    "brand_fit.repeat_band_low": "cis.quality.brand_fit_repeat_band_low",
    "brand_fit.repeat_band_high": "cis.quality.brand_fit_repeat_band_high",
    "t2.cross_brand_overlap_warn": "cis.quality.t2_cross_brand_overlap_warn",
    "t9.blog_length_target_margin": "cis.quality.t9_blog_length_target_margin",
    "t10.reuse_similarity": "cis.quality.t10_reuse_similarity",
    "review.score_band_low": "cis.quality.review_score_band_low",
    "ui.score_band_top": "cis.quality.ui_score_band_top",
}
AUTO_UPGRADE_THRESHOLD = "cis.quality.auto_upgrade_threshold"

# writing_policy.yaml bands / writer_aims (D5) -> setting
SEO: dict[str, str] = {
    "meta_min": "cis.seo.meta_min",
    "meta_max": "cis.seo.meta_max",
    "title_min": "cis.seo.title_min",
    "title_max": "cis.seo.title_max",
    "slug_max": "cis.seo.slug_max",
}
WRITER_AIMS: dict[str, tuple[str, str]] = {
    "t9": ("cis.seo.aim_t9_meta_min", "cis.seo.aim_t9_meta_max"),
    "s1": ("cis.seo.aim_s1_meta_min", "cis.seo.aim_s1_meta_max"),
    "t2": ("cis.seo.aim_t2_meta_min", "cis.seo.aim_t2_meta_max"),
}
T9_BLOG_CHARS = ("cis.t9.blog_chars_min", "cis.t9.blog_chars_max")

# gate modes (worker env vars before L2; the env var stays the definition's emergency override)
MODES: dict[str, str] = {
    "t10_f12_scope": "cis.quality_modes.t10_f12_scope",
    "t10_f9_brand_style": "cis.quality_modes.t10_f9_brand_style",
    "t10_f14_floor": "cis.quality_modes.t10_f14_floor",
    "t10_f9_policy": "cis.quality_modes.t10_f9_policy",
    "t10_seed_align": "cis.quality_modes.t10_seed_align",
    "t10_adjudicate": "cis.quality_modes.t10_adjudicate",
    "t9_brief_grounding": "cis.quality_modes.t9_brief_grounding",
    "t7_debate": "cis.quality_modes.t7_debate",
    "t2_judge_gate": "cis.quality_modes.t2_judge_gate",
    "t3_background_facts": "cis.quality_modes.t3_background_facts",
}
SEED_ALIGN_CLASS_MODES: dict[str, str] = {
    "optional_as_planned": "cis.quality_modes.t10_seed_align_optional_as_planned",
    "planned_as_optional": "cis.quality_modes.t10_seed_align_planned_as_optional",
    "near_as_at": "cis.quality_modes.t10_seed_align_near_as_at",
    "difficulty_promise": "cis.quality_modes.t10_seed_align_difficulty_promise",
    "world_share_unsupported": "cis.quality_modes.t10_seed_align_world_share_unsupported",
    "scopeless_superlative": "cis.quality_modes.t10_seed_align_scopeless_superlative",
    "brief_recipe_leak": "cis.quality_modes.t10_seed_align_brief_recipe_leak",
    "naming_note_leak": "cis.quality_modes.t10_seed_align_naming_note_leak",
    "paraphrased_repeat": "cis.quality_modes.t10_seed_align_paraphrased_repeat",
    "orphan_opener": "cis.quality_modes.t10_seed_align_orphan_opener",
}
ADJUDICATE_CLASS_MODES: dict[str, str] = {
    "naming_note_leak": "cis.quality_modes.t10_adjudicate_naming_note_leak",
    "paraphrased_repeat": "cis.quality_modes.t10_adjudicate_paraphrased_repeat",
    "faq_non_answer": "cis.quality_modes.t10_adjudicate_faq_non_answer",
    "scopeless_superlative": "cis.quality_modes.t10_adjudicate_scopeless_superlative",
}
INHERIT = "inherit"  # a per-class mode that follows the mode of every class

# claim verdicts: writing_policy.yaml rules_config (claim_rules.RulesConfig) and the F9 class actions (f9_policy)
RULES_CONFIG: dict[str, str] = {
    "hypothetical": "cis.quality_claims.rules_hypothetical",
    "uncited_background": "cis.quality_claims.rules_uncited_background",
}
F9_CLASS_ACTIONS: dict[str, str] = {
    "trip_promise": "cis.quality_claims.f9_trip_promise",
    "unsupported_other": "cis.quality_claims.f9_unsupported_other",
    "fact_check_unquoted": "cis.quality_claims.f9_fact_check_unquoted",
    "seo_field_claim": "cis.quality_claims.f9_seo_field_claim",
    "cta_not_reflected": "cis.quality_claims.f9_cta_not_reflected",
    "text_integrity": "cis.quality_claims.f9_text_integrity",
    "judge_unavailable": "cis.quality_claims.f9_judge_unavailable",
    "reasonless": "cis.quality_claims.f9_reasonless",
    "other": "cis.quality_claims.f9_other",
}

# taxonomy.yaml detector (class id, ref) -> its mode setting
DETECTOR_MODES: dict[tuple[str, str], str] = {
    ("fact.inclusion", "ITINERARY_MEAL_TIME_INVENTED"): "cis.quality_detectors.fact_inclusion.itinerary_meal_time_invented",  # noqa: E501
    ("fact.inclusion", "UNSUPPORTED_CLAIM"): "cis.quality_detectors.fact_inclusion.unsupported_claim",
    ("fact.inclusion", "a1_promise_unsupported"): "cis.quality_detectors.fact_inclusion.a1_promise_unsupported",
    ("fact.inclusion", "F9_cta_fact"): "cis.quality_detectors.fact_inclusion.f9_cta_fact",
    ("fact.inclusion", "F2_platform_truth"): "cis.quality_detectors.fact_inclusion.f2_platform_truth",
    ("fact.inclusion", "INCLUSION_CONTRADICTED"): "cis.quality_detectors.fact_inclusion.inclusion_contradicted",
    ("fact.number", "UNSUPPORTED_NUMBER"): "cis.quality_detectors.fact_number.unsupported_number",
    ("fact.number", "NUMBER_NOT_IN_SOURCE"): "cis.quality_detectors.fact_number.number_not_in_source",
    ("fact.number", "T3_grounding"): "cis.quality_detectors.fact_number.t3_grounding",
    ("fact.number", "F1_grounding"): "cis.quality_detectors.fact_number.f1_grounding",
    ("fact.number", "F9_cta_fact"): "cis.quality_detectors.fact_number.f9_cta_fact",
    ("fact.misbinding", "a1_claim_supported"): "cis.quality_detectors.fact_misbinding.a1_claim_supported",
    ("fact.misbinding", "F17_trip_fact"): "cis.quality_detectors.fact_misbinding.f17_trip_fact",
    ("fact.misbinding", "F9_cta_fact"): "cis.quality_detectors.fact_misbinding.f9_cta_fact",
    ("fact.misbinding", "NUMBER_NOUN_MISMATCH"): "cis.quality_detectors.fact_misbinding.number_noun_mismatch",
    ("fact.name", "a1_claim_supported"): "cis.quality_detectors.fact_name.a1_claim_supported",
    ("fact.name", "F9_cta_fact"): "cis.quality_detectors.fact_name.f9_cta_fact",
    ("fact.name", "NAME_NOT_IN_SOURCE"): "cis.quality_detectors.fact_name.name_not_in_source",
    ("fact.direction", "a1_claim_supported"): "cis.quality_detectors.fact_direction.a1_claim_supported",
    ("fact.embellish", "NAME_SUPERLATIVE"): "cis.quality_detectors.fact_embellish.name_superlative",
    ("fact.embellish", "STOCK_SUPERLATIVE"): "cis.quality_detectors.fact_embellish.stock_superlative",
    ("fact.embellish", "F2_superlatives"): "cis.quality_detectors.fact_embellish.f2_superlatives",
    ("fact.embellish", "F18_rarity"): "cis.quality_detectors.fact_embellish.f18_rarity",
    ("fact.embellish", "F19_authenticity"): "cis.quality_detectors.fact_embellish.f19_authenticity",
    ("struct.day_structure", "ITINERARY_DAY_COUNT_MISMATCH"): "cis.quality_detectors.struct_day_structure.itinerary_day_count_mismatch",  # noqa: E501
    ("struct.day_structure", "ITINERARY_STILL_COMPRESSED"): "cis.quality_detectors.struct_day_structure.itinerary_still_compressed",  # noqa: E501
    ("struct.day_structure", "ITINERARY_STRUCTURE_WEAK"): "cis.quality_detectors.struct_day_structure.itinerary_structure_weak",  # noqa: E501
    ("struct.day_structure", "DAY_SUBDAY_SPLIT"): "cis.quality_detectors.struct_day_structure.day_subday_split",
    ("struct.day_structure", "DAY_OUTPUT_DUPLICATE"): "cis.quality_detectors.struct_day_structure.day_output_duplicate",
    ("struct.day_structure", "DAY_OUTPUT_ORDER"): "cis.quality_detectors.struct_day_structure.day_output_order",
    ("struct.day_structure", "DAY_OUTPUT_GAP"): "cis.quality_detectors.struct_day_structure.day_output_gap",
    ("struct.day_structure", "DAY_OUTPUT_EXTRA"): "cis.quality_detectors.struct_day_structure.day_output_extra",
    ("struct.day_structure", "DAY_DEPARTURE_MIDTRIP"): "cis.quality_detectors.struct_day_structure.day_departure_midtrip",  # noqa: E501
    ("struct.day_structure", "DAY_COUNT_VS_DURATION"): "cis.quality_detectors.struct_day_structure.day_count_vs_duration",  # noqa: E501
    ("struct.day_structure", "DAY_PLACE_MISMATCH"): "cis.quality_detectors.struct_day_structure.day_place_mismatch",
    ("struct.day_structure", "repair_contract"): "cis.quality_detectors.struct_day_structure.repair_contract",
    ("struct.truncated", "META_INCOMPLETE_SENTENCE"): "cis.quality_detectors.struct_truncated.meta_incomplete_sentence",
    ("struct.truncated", "MISSING_FIELD"): "cis.quality_detectors.struct_truncated.missing_field",
    ("struct.truncated", "F0_complete"): "cis.quality_detectors.struct_truncated.f0_complete",
    ("struct.truncated", "META_CUT_SIGNATURE"): "cis.quality_detectors.struct_truncated.meta_cut_signature",
    ("struct.truncated", "META_CUT"): "cis.quality_detectors.struct_truncated.meta_cut",
    ("struct.truncated", "TEXT_TRUNCATED"): "cis.quality_detectors.struct_truncated.text_truncated",
    ("struct.truncated", "repair_contract"): "cis.quality_detectors.struct_truncated.repair_contract",
    ("struct.leaked_instruction", "F13_leak"): "cis.quality_detectors.struct_leaked_instruction.f13_leak",
    ("struct.leaked_instruction", "F13_hygiene"): "cis.quality_detectors.struct_leaked_instruction.f13_hygiene",
    ("scope.framing", "F12_scope"): "cis.quality_detectors.scope_framing.f12_scope",
    ("scope.framing", "F4_seo_surface"): "cis.quality_detectors.scope_framing.f4_seo_surface",
    ("scope.persona_promise", "F13_leak"): "cis.quality_detectors.scope_persona_promise.f13_leak",
    ("scope.persona_promise", "F15_voice"): "cis.quality_detectors.scope_persona_promise.f15_voice",
    ("scope.faq_question_offtopic", "a3_question_about_here"): "cis.quality_detectors.scope_faq_question_offtopic.a3_question_about_here",  # noqa: E501
    ("scope.faq_question_offtopic", "a3_question_foreign"): "cis.quality_detectors.scope_faq_question_offtopic.a3_question_foreign",  # noqa: E501
    ("safety.tenant_isolation", "F11_brand_name"): "cis.quality_detectors.safety_tenant_isolation.f11_brand_name",
    ("safety.tenant_isolation", "F10_cannibalization_cross_tenant"): "cis.quality_detectors.safety_tenant_isolation.f10_cannibalization_cross_tenant",  # noqa: E501
    ("source.contradiction", "SOURCE_DAY_CONTRADICTION"): "cis.quality_detectors.source_contradiction.source_day_contradiction",  # noqa: E501
    ("other.f6", "F17_trip_fact"): "cis.quality_detectors.other_f6.f17_trip_fact",
    ("other.f6", "HIGHLIGHTS_OPTIONAL_LANGUAGE"): "cis.quality_detectors.other_f6.highlights_optional_language",
    ("other.f7", "F17_trip_fact"): "cis.quality_detectors.other_f7.f17_trip_fact",
    ("other.f7", "ITINERARY_MEAL_TIME_INVENTED"): "cis.quality_detectors.other_f7.itinerary_meal_time_invented",
    ("other.s3", "F13_repeat"): "cis.quality_detectors.other_s3.f13_repeat",
    ("other.s4", "F13_leak"): "cis.quality_detectors.other_s4.f13_leak",
    ("other.s5", "F16_faq"): "cis.quality_detectors.other_s5.f16_faq",
    ("other.s7", "F13_hygiene"): "cis.quality_detectors.other_s7.f13_hygiene",
    ("other.s7", "NAME_ALL_CAPS"): "cis.quality_detectors.other_s7.name_all_caps",
    ("other.c1", "a1_keyword_about_tour"): "cis.quality_detectors.other_c1.a1_keyword_about_tour",
    ("other.c3", "F17_trip_fact"): "cis.quality_detectors.other_c3.f17_trip_fact",
    ("other.c5", "F6_cta_present"): "cis.quality_detectors.other_c5.f6_cta_present",
    ("other.c5", "F16_faq"): "cis.quality_detectors.other_c5.f16_faq",
    ("other.v1", "F9_brand_style"): "cis.quality_detectors.other_v1.f9_brand_style",
    ("other.v1", "F15_voice"): "cis.quality_detectors.other_v1.f15_voice",
    ("other.v1", "F2_lexicon"): "cis.quality_detectors.other_v1.f2_lexicon",
    ("other.v1", "a1_brand_fit"): "cis.quality_detectors.other_v1.a1_brand_fit",
    ("other.lexicon", "FORBIDDEN_WORD"): "cis.quality_detectors.other_lexicon.forbidden_word",
    ("other.lexicon", "F2_lexicon"): "cis.quality_detectors.other_lexicon.f2_lexicon",
    ("other.lexicon", "F2_style_warn"): "cis.quality_detectors.other_lexicon.f2_style_warn",
    ("other.length_shape", "SEO_META_TOO_LONG"): "cis.quality_detectors.other_length_shape.seo_meta_too_long",
    ("other.length_shape", "F4_extreme_length"): "cis.quality_detectors.other_length_shape.f4_extreme_length",
    ("other.length_shape", "F14_social_shape"): "cis.quality_detectors.other_length_shape.f14_social_shape",
    ("other.length_shape", "F14_first_line"): "cis.quality_detectors.other_length_shape.f14_first_line",
    ("other.length_shape", "META_LENGTH"): "cis.quality_detectors.other_length_shape.meta_length",
    ("other.degraded_detector", "DEGRADED_DETECTOR"): "cis.quality_detectors.other_degraded_detector.degraded_detector",
    ("other.degraded_detector", "degraded"): "cis.quality_detectors.other_degraded_detector.degraded",
}

# T7: debate and the Channel bars (slate.CHANNEL_BARS keeps the structure; the numbers are these settings)
T7_DEBATE_CONTESTED_CUT = "cis.t7.debate_contested_cut"
T7_DEBATE_MAX_CUT_FRACTION = "cis.t7.debate_max_cut_fraction"
CHANNEL_BARS: dict[str, str] = {
    "blog": "cis.t7.channel_bar_blog",
    "linkedin": "cis.t7.channel_bar_linkedin",
    "facebook": "cis.t7.channel_bar_facebook",
    "instagram": "cis.t7.channel_bar_instagram",
    "tiktok": "cis.t7.channel_bar_tiktok",
    "email": "cis.t7.channel_bar_email",
    "landing_page": "cis.t7.channel_bar_landing_page",
    "ads": "cis.t7.channel_bar_ads",
}
CHANNEL_BAR_FIELDS = ("needs_demand", "needs_questions", "needs_said", "most_per_hub")

ALL_KEYS: tuple[str, ...] = tuple(sorted(
    list(QUALITY.values()) + [AUTO_UPGRADE_THRESHOLD] + list(SEO.values())
    + [key for pair in WRITER_AIMS.values() for key in pair] + list(T9_BLOG_CHARS) + list(MODES.values())
    + list(SEED_ALIGN_CLASS_MODES.values()) + list(ADJUDICATE_CLASS_MODES.values()) + list(RULES_CONFIG.values())
    + list(F9_CLASS_ACTIONS.values()) + list(DETECTOR_MODES.values()) + [T7_DEBATE_CONTESTED_CUT,
                                                                   T7_DEBATE_MAX_CUT_FRACTION]
    + list(CHANNEL_BARS.values())))
# The keys a writer prompt renders at call time (the prompt fingerprint covers them: prompt_settings_fp)
PROMPT_KEYS: tuple[str, ...] = tuple(sorted(list(SEO.values()) + [key for pair in WRITER_AIMS.values() for key in pair]
                                            + list(T9_BLOG_CHARS) + [QUALITY["t9.blog_length_target_margin"],
                                                                     QUALITY["pass_line"]]))
