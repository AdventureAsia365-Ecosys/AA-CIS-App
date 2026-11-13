"""S279 thresholds lane L2 (AA-1096, CIS 412 / 413) — the quality registry on the platform settings registry.

1. **Parity.** With no stored value every reader returns exactly the value it had before the move (the
   thresholds.yaml / writing_policy.yaml / taxonomy.yaml value, the worker's Terraform env value or the code literal),
   the T9 blog length block renders byte for byte as t9_write v7 did, and the AA-926 gate-outcome snapshot
   (tests/unit/test_aa926_equivalence.py, the fixed corpus of gate verdicts recorded before the move) is unchanged.
2. **Follow.** A value the registry serves (an approved row) reaches each reader at call time: the pass line, a
   detector's mode, a claim verdict, the T10 / seed-align / adjudicator modes, the T7 debate cut and Channel bars, the
   blog length band and the A1 auto-upgrade score; a prompt-rendered value puts the settings fingerprint on the
   ledger's prompt version and the release fingerprint.
3. **Evidence, admin only, bounds.** Every quality key is evidence_required + maker_checker; a proposal without an
   evidence link is refused (422) before any DB call; only the administrator may propose or approve; values outside
   a definition's bounds or breaking a rule between keys are refused; the SEO bands are listed but not editable yet.

Invented values only. No DB, no LLM, no network.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from services.quality import quality_settings, registry, settings_keys as K, writing_policy
from shared import settings
from tests.unit.settings_serve import bar_with, serve


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for key in K.ALL_KEYS:
        env = settings.definition(key).env_override
        if env:
            monkeypatch.delenv(env, raising=False)


# ── 1. parity ────────────────────────────────────────────────────────────────────────────────────────────────────

BEFORE = {   # the value each key had before the move, and where it lived
    "cis.quality.pass_line": 7.0,                       # thresholds.yaml pass_line
    "cis.quality.a1_missing_field_cap": 4.0,
    "cis.quality.judge_tie_band": 0.5,
    "cis.quality.judge_mission_absent_cap": 6.0,
    "cis.quality.brand_fit_repeat_band_low": 6.0,
    "cis.quality.brand_fit_repeat_band_high": 8.0,
    "cis.quality.t2_cross_brand_overlap_warn": 0.35,
    "cis.quality.t9_blog_length_target_margin": 0.17,
    "cis.quality.t10_reuse_similarity": 0.92,
    "cis.quality.review_score_band_low": 5,
    "cis.quality.ui_score_band_top": 9,
    "cis.quality.auto_upgrade_threshold": 8.5,         # env AUTO_UPGRADE_THRESHOLD default (set nowhere)
    "cis.seo.meta_min": 140, "cis.seo.meta_max": 155,  # writing_policy.yaml bands (D5)
    "cis.seo.title_min": 45, "cis.seo.title_max": 60, "cis.seo.slug_max": 60,
    "cis.seo.aim_t9_meta_min": 130, "cis.seo.aim_t9_meta_max": 145,
    "cis.seo.aim_s1_meta_min": 130, "cis.seo.aim_s1_meta_max": 148,
    "cis.seo.aim_t2_meta_min": 145, "cis.seo.aim_t2_meta_max": 152,
    "cis.t9.blog_chars_min": 4700, "cis.t9.blog_chars_max": 13000,    # length_contract literals
    "cis.quality_modes.t10_f12_scope": "warn",          # quality_gates F12_SCOPE_BLOCKING off
    "cis.quality_modes.t10_f9_brand_style": "warn",
    "cis.quality_modes.t10_f14_floor": "repair",        # F14_FLOOR_BLOCKING on
    "cis.quality_modes.t10_f9_policy": "interim",
    "cis.quality_modes.t10_seed_align": "off",
    "cis.quality_modes.t10_adjudicate": "off",
    "cis.quality_modes.t9_brief_grounding": "off",
    "cis.quality_modes.t7_debate": "rank",
    "cis.quality_modes.t2_judge_gate": "brand_fit",     # the worker's Terraform T2_JUDGE_GATE
    "cis.quality_modes.t3_background_facts": True,      # the worker's Terraform T3_BACKGROUND_FACTS
    "cis.quality_claims.rules_hypothetical": "warn",
    "cis.quality_claims.rules_uncited_background": "warn",
    "cis.quality_claims.f9_trip_promise": "hold",
    "cis.quality_claims.f9_other": "warn",
    "cis.t7.debate_contested_cut": 0.5,                 # atom_ranking.CONTESTED_CUT_THRESHOLD
    "cis.t7.debate_max_cut_fraction": 0.5,              # debate.MAX_CUT_FRACTION
    "cis.t7.channel_bar_blog": {"needs_demand": 1000, "needs_questions": 3, "needs_said": 0, "most_per_hub": 99},
    "cis.t7.channel_bar_linkedin": {"needs_demand": 0, "needs_questions": 0, "needs_said": 150, "most_per_hub": 99},
    "cis.t7.channel_bar_email": {"needs_demand": 0, "needs_questions": 0, "needs_said": 0},
}


@pytest.mark.parametrize("key", sorted(BEFORE))
def test_every_default_is_the_value_before_the_move(key):
    assert settings.get(key) == BEFORE[key]


def test_every_quality_key_is_seeded_evidence_gated_and_maker_checker():
    assert set(K.ALL_KEYS) <= set(settings.READER.keys())
    for key in K.ALL_KEYS:
        d = settings.definition(key)
        assert d.evidence_required is True and d.approval == "maker_checker", key


def test_detector_modes_at_their_seed_match_the_taxonomy_counts():
    modes = [settings.get(k) for k in K.DETECTOR_MODES.values()]
    assert len(modes) == 81
    assert (modes.count("enforce"), modes.count("shadow"), modes.count("warn")) == (50, 19, 12)


# t9_write v7's _BLOG_LENGTH_INSTRUCTIONS, sha256[:16] of the exact text (recorded on CIS main before the move)
V7_BLOG_BLOCK_SHA16 = "025fdcaf411a1034"


def test_the_blog_length_block_renders_as_v7_at_the_seeded_values():
    from services.acp_content_writing import length_contract as lc, prompts
    block = prompts._blog_length_instructions()
    assert hashlib.sha256(block.encode("utf-8")).hexdigest()[:16] == V7_BLOG_BLOCK_SHA16
    assert prompts._blog_words() == (950, 1300) and lc.target_chars() == 5499
    assert (lc.floor_chars(), lc.ceiling_chars(), lc.target_margin()) == (4700, 13000, 0.17)


def test_the_fingerprints_are_empty_and_the_policy_sha_unchanged_at_the_seeded_values():
    assert quality_settings.settings_fp() == "" and quality_settings.prompt_fp() == ""
    assert writing_policy.policy_sha_in_force() == writing_policy.policy_sha()
    assert writing_policy.sha_of(writing_policy.load_source()) == writing_policy.policy_sha()   # the compiled file


def test_the_effective_policy_is_the_pre_move_yaml_shape():
    src = writing_policy.load_source()
    assert src["bands"]["meta"] == {"min": 140, "max": 155} and src["bands"]["slug"]["max"] == 60
    assert src["writer_aims"]["meta"]["t9"]["min"] == 130 and "short_fix" in src["writer_aims"]["meta"]["t9"]
    assert src["rules_config"] == {"hypothetical": "warn", "uncited_background": "warn"}
    assert {r["class"]: r["action"] for r in src["f9_class_rows"]}["trip_promise"] == "hold"


def test_ui_values_shape():
    assert quality_settings.ui_values() == {
        "pass_line": 7.0, "score_bands": {"top": 9.0, "pass": 7.0, "low": 5.0},
        "seo_bands": {"meta": {"min": 140, "max": 155}, "title": {"min": 45, "max": 60}, "slug": {"max": 60}},
        "writing_policy_sha": writing_policy.policy_sha(), "settings_fp": ""}


# ── 2. follow ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_pass_line_reaches_every_gate_that_refs_it():
    from api.routers import admin_pipeline
    from services.quality import approve_gate
    from services.validation import handler
    with serve({"cis.quality.pass_line": 8.0}):
        assert registry.pass_line() == 8.0
        assert admin_pipeline._is_publishable({"quality_score": 7.5, "failure_codes": []}) is False
        assert handler._validation_route(7.5, 0) == ("pending", "regenerate")
        assert approve_gate._publish_floor() == 8.0
        assert quality_settings.ui_values()["pass_line"] == 8.0
    assert admin_pipeline._is_publishable({"quality_score": 7.5, "failure_codes": []}) is True


def test_a_detector_mode_reaches_the_owner_gates():
    cls, ref = "fact.embellish", "F18_rarity"
    key = K.DETECTOR_MODES[(cls, ref)]
    flip = "warn" if settings.get(key) == "enforce" else "enforce"
    with serve({key: flip}):
        assert registry.detector_mode(cls, ref) == flip
        assert (ref in registry.classes()[cls]["owner_gates"]) is (flip == "enforce")


def test_a_claim_verdict_reaches_the_f9_policy_and_the_rules():
    from services.acp_content_writing import claim_rules, f9_policy
    with serve({"cis.quality_claims.f9_other": "hold", "cis.quality_claims.rules_hypothetical": "fail"}):
        assert f9_policy.hold_classes() == frozenset({f9_policy.TRIP_PROMISE, f9_policy.OTHER})
        cfg = claim_rules.RulesConfig()
        assert cfg.hypothetical == "fail" and cfg.policy_sha != writing_policy.policy_sha()
    assert claim_rules.RulesConfig().hypothetical == "warn"


def test_the_t10_modes_follow_and_a_class_inherits_unless_set():
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing.seed_align import adjudicate, modes
    cls = next(iter(K.SEED_ALIGN_CLASS_MODES))
    with serve({"cis.quality_modes.t10_f12_scope": "block", "cis.quality_modes.t10_seed_align": "shadow",
                "cis.quality_modes.t10_adjudicate": "shadow"}):
        assert qg.f12_scope_mode() == "block"
        assert modes.class_mode(cls) in ("shadow", "off")            # off only for a class not built yet
        with serve({K.SEED_ALIGN_CLASS_MODES[cls]: "off"}):
            assert modes.class_mode(cls) == "off"
        a_cls = next(iter(K.ADJUDICATE_CLASS_MODES))
        assert adjudicate.class_mode(a_cls) == "shadow"


def test_the_t7_debate_cut_and_a_channel_bar_follow():
    from services.acp_contract import atom_ranking
    from services.acp_shared import debate, slate
    with serve({"cis.t7.debate_contested_cut": 0.8, "cis.t7.debate_max_cut_fraction": 0.25,
                **bar_with("linkedin", needs_said=40)}):
        assert atom_ranking.contested_cut_threshold() == 0.8 and debate.max_cut_fraction() == 0.25
        sig = [debate.DebateSignal(flags=("brand_fit",), severity=1.0)] * 8
        assert debate.would_cut(sig) == 2
        cand = slate.Candidate(segment_id="s1", route_id=None, score=1.0, demand=0, questions=0, said=60)
        assert slate.bar_status("linkedin", cand) == slate.BAR_CLEARED
    assert slate.bar_status("linkedin", cand) == slate.BAR_FEW_SAID


def test_the_blog_length_band_reaches_the_gate_and_the_prompt():
    from services.acp_content_writing import length_contract as lc, prompts, quality_gates as qg
    with serve({"cis.t9.blog_chars_min": 5200, "cis.quality.t9_blog_length_target_margin": 0.1}):
        assert qg._length_bounds_for_channel("blog") == (5200, 13000)
        assert lc.target_chars() == 5720 and prompts._blog_words()[0] == lc.target_words() == 990
        assert "never under 990" in prompts._blog_length_instructions()
        assert quality_settings.prompt_fp() != ""


def test_a_prompt_rendered_value_marks_the_prompt_version_and_the_release_fingerprint(monkeypatch):
    from services.writing_memory import config_fp
    from shared.llm_client import prompt_registry as pr
    monkeypatch.setattr(pr, "_deployed", lambda stage: ("v8", "sha"))
    monkeypatch.setattr(pr, "_registered", lambda: {("t9_write", "v8", "sha"), ("a3_place_card", "v8", "sha")})
    assert pr.version_for("t9_write") == "v8"
    base = config_fp._policy_sha()
    with serve({"cis.quality.t9_blog_length_target_margin": 0.2}):
        fp = quality_settings.prompt_fp()
        assert len(fp) == 8 and pr.version_for("t9_write") == f"v8.s{fp}"
        assert pr.version_for("a3_place_card") == "v8"                 # not a writer stage
        assert config_fp._policy_sha() == f"{base}+{quality_settings.settings_fp()}"
    assert pr.version_for("t9_write") == "v8" and config_fp._policy_sha() == base


def test_the_auto_upgrade_score_is_a_threshold():
    with serve({"cis.quality.auto_upgrade_threshold": 9.0}):
        assert registry.threshold("a1.auto_upgrade") == 9.0
    assert registry.threshold("a1.auto_upgrade") == 8.5


def test_the_env_override_still_wins_for_one_release(monkeypatch):
    from services.acp_shared import debate
    monkeypatch.setenv("T7_DEBATE_MODE", "cut")
    assert debate.debate_mode() == "cut"
    assert settings.resolve("cis.quality_modes.t7_debate").source == "env"


# ── 3. evidence, admin only, bounds ──────────────────────────────────────────────────────────────────────────────

class _NoPool:
    def acquire(self):  # the checks run before any DB call
        raise AssertionError("no DB call expected")


def _propose(key, value, evidence=None):
    from api.services import settings_registry as reg
    return asyncio.run(reg.propose(_NoPool(), key, value=value, reason="calibration run on the invented set",
                                   actor="admin:test", base_version=0, evidence_ref=evidence))


@pytest.mark.parametrize("key,value", [("cis.quality.pass_line", 7.5),
                                       (K.DETECTOR_MODES[("fact.embellish", "F18_rarity")], "warn"),
                                       ("cis.quality_claims.f9_other", "hold")])
def test_a_proposal_without_evidence_is_refused(key, value):
    from api.services import settings_registry as reg
    with pytest.raises(reg.SettingsError) as e:
        _propose(key, value)
    assert e.value.status == 422 and e.value.code == "evidence_required"


def test_only_the_administrator_may_propose_or_approve():
    from api.core import staff_authz as az
    assert az.POLICY[("POST", "/admin/settings/registry/{key}/proposals")] == az.SETTINGS_WRITE
    assert az.POLICY[("POST", "/admin/settings/registry/proposals/{value_id}/approve")] == az.SETTINGS_APPROVE
    for role in az.ROLE_PERMS:
        assert az.has_perm(role, az.SETTINGS_WRITE) is (role == "admin"), role
        assert az.has_perm(role, az.SETTINGS_APPROVE) is (role == "admin"), role


def test_the_thresholds_read_is_open_to_the_score_views_only():
    from api.core import staff_authz as az
    entry = az.POLICY[("GET", "/admin/quality/thresholds")]
    allowed = {r for r in az.ROLE_PERMS if any(az.has_perm(r, c) for c in az.policy_capabilities(entry))}
    assert {"admin", "content_lead", "content", "tenant_qa", "ai_ops", "customer_success", "support",
            "viewer"} <= allowed
    assert "finance" not in allowed


@pytest.mark.parametrize("key,value", [("cis.quality.pass_line", 4.9), ("cis.quality.pass_line", 9.6),
                                       ("cis.quality.t10_reuse_similarity", 1.2),
                                       ("cis.quality_modes.t7_debate", "drop"),
                                       ("cis.quality_claims.f9_other", "fail")])
def test_a_value_outside_its_definition_is_refused(key, value):
    from api.services import settings_registry as reg
    with pytest.raises(reg.SettingsError) as e:
        _propose(key, value, evidence="eval:00000000-0000-4000-8000-000000000001")
    assert e.value.status == 422 and e.value.code == "out_of_bounds"


@pytest.mark.parametrize("key,value,why", [
    ("cis.quality.a1_missing_field_cap", 7.0, "must stay under the pass line"),
    ("cis.quality.pass_line", 9.0, "must stay under the top colour band"),
    ("cis.quality.review_score_band_low", 7.5, "must stay under the pass line"),
    ("cis.quality.brand_fit_repeat_band_low", 8.5, "must not be above"),
    ("cis.t9.blog_chars_min", 13000, "floor"),
    ("cis.t7.channel_bar_blog", {"needs_demand": 1000, "needs_questions": 3, "needs_said": 0}, "most_per_hub"),
    ("cis.t7.channel_bar_email", {"needs_demand": 0, "needs_questions": 0, "needs_said": 0, "most_per_hub": 2},
     "on-demand"),
    ("cis.t7.channel_bar_blog", {"needs_demand": 1, "needs_questions": 3, "needs_said": 0, "most_per_hub": 99,
                                 "colour": 1}, "unknown"),
])
def test_a_value_that_breaks_a_rule_between_keys_is_refused(key, value, why):
    problem = quality_settings.cross_problem(key, value)
    assert problem and why in problem
    from api.services import settings_registry as reg
    with pytest.raises(reg.SettingsError) as e:
        _propose(key, value, evidence="eval:00000000-0000-4000-8000-000000000001")
    assert e.value.code in ("cross_check", "out_of_bounds")


@pytest.mark.parametrize("key", sorted(K.SEO.values()))
def test_the_seo_bands_are_listed_but_not_editable_yet(key):
    seed = settings.definition(key).default_value
    assert quality_settings.cross_problem(key, seed + 1) == quality_settings.START_TIME_REASON
    assert quality_settings.cross_problem(key, seed) is None          # re-stating today's value is fine


def test_a_good_value_passes_every_rule():
    for key, value in (("cis.quality.pass_line", 7.5), ("cis.t7.debate_contested_cut", 0.7),
                       ("cis.t7.channel_bar_blog", {"needs_demand": 800, "needs_questions": 3, "needs_said": 0,
                                                    "most_per_hub": 99}),
                       ("cis.t9.blog_chars_min", 5000)):
        assert settings.validate(settings.definition(key), value) is None, key
        assert quality_settings.cross_problem(key, value) is None, key


# ── the $0 replay script (live step after deploy) ─────────────────────────────────────────────────────────────────

def test_the_replay_before_values_are_the_seeds_and_agree_with_the_values_in_force():
    from scripts.quality import replay_s279_l2_gate_parity as rp
    for key, value in rp.BEFORE.items():
        assert settings.definition(key).default_value == value, key
    from services.jobs import t2_rewrite_job as t2
    assert all(len(set(rp._both(lambda s=s: t2._t2_status(s)))) == 1 for s in rp._SCORES)
    with serve({"cis.quality.pass_line": 8.0}):           # an approved change shows as a verdict difference
        assert rp._both(lambda: t2._t2_status(7.5)) == ("needs_review", "ai_generated")
