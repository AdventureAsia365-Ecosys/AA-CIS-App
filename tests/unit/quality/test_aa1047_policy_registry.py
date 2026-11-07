"""AA-1047 (root ADR 0020, W1) — the writing policy registry: one source, one policy_sha, consumers switched.

Done-when pins: one policy_sha across every compiled artefact and its consumers (rubric, LABEL_RUBRIC, RulesConfig,
route rows, the judge prompt, the release fingerprint, the generated TS), one meta band in code, the AA-788 / AA-920
registry entries equal to today's code (no behaviour change), and the compiled file fresh.
"""
from __future__ import annotations

import json

import pytest

from services.quality import writing_policy as wp


def test_compiled_file_is_fresh_and_the_schema_is_clean():
    src = wp.load_source()
    assert wp.schema_errors(src) == []
    assert not wp.stale(), "run python -m services.quality.writing_policy --write"


def test_one_policy_sha_across_every_artefact():
    shas = wp.artefact_shas()
    assert {"bands", "label_rubric", "rules_config", "route_rows", "fragments", "eval", "severities"} <= set(shas)
    assert set(shas.values()) == {wp.policy_sha()}
    assert wp.policy_sha() == wp.sha_of(wp.load_source())


def test_the_sixteen_decisions_are_frozen_in_v1():
    c = wp.compiled()
    assert c["policy_version"] == "writing-policy-v1"
    assert list(c["decisions"]) == [f"D{i}" for i in range(1, 17)]
    assert "140-155" in c["decisions"]["D5"]


def test_one_meta_band_everywhere_in_code():
    from services.acp_content_writing import quality_gates as qg
    from services.content_generation import seo_meta_utils as smu
    lo, hi = wp.band("meta")
    assert (lo, hi) == (140, 155)
    assert (smu.SEO_META_MIN, smu.SEO_META_MAX) == (lo, hi)
    assert (qg._META_DESCRIPTION_MIN_CHARS, qg._META_DESCRIPTION_MAX_CHARS) == (lo, hi)
    assert (qg._SEO_TITLE_MIN_CHARS, qg._SEO_TITLE_MAX_CHARS) == wp.band("title") and smu.SEO_TITLE_MAX == 60
    assert qg._SLUG_MAX_CHARS == wp.slug_max()


def test_writer_aims_stay_below_the_band():
    lo, hi = wp.band("meta")
    for writer in ("t9", "s1", "t2"):
        a_lo, a_hi = wp.writer_aim(writer)
        assert a_lo < a_hi <= hi
    from services.content_generation.s1_writer_v2 import META_TARGET
    assert tuple(META_TARGET) == (130, 148)        # byte-equal S1 v2 meta prompt (W5 decides a change)


def test_label_rubric_is_one_object_for_the_panel_and_the_judge():
    from scripts.aa729.labels import RUBRIC
    from services.writing_memory import eval_judge as ej
    pol = wp.label_rubric()
    assert ej.LABEL_RUBRIC == pol
    assert {k: RUBRIC[k] for k in ("pass", "fail", "rules", "policy_sha")} == {k: pol[k] for k in
                                                                               ("pass", "fail", "rules", "policy_sha")}
    rules = " ".join(pol["rules"])
    assert "brief and brand pack are part of the source" not in rules          # D2
    assert "instructions, never a source" in rules
    assert "Background about a place the source names" in rules               # D1
    assert "Rarity, uniqueness or age attached to the trip" in rules          # D13


def test_judge_prompt_names_the_policy_and_its_version_moves_with_it(monkeypatch):
    from services.writing_memory import eval_judge as ej
    assert wp.policy_sha() in ej.system_prompt()
    before = ej.judge_version()
    patched = {**wp.compiled(), "policy_sha": "0" * 16}
    monkeypatch.setattr(wp, "compiled", lambda: patched)
    ej.system_prompt.cache_clear()
    try:
        assert ej.judge_version() != before
    finally:
        monkeypatch.undo()
        ej.system_prompt.cache_clear()
    assert ej.judge_version() == before


def test_judge_classes_come_from_the_policy():
    from services.writing_memory import eval_judge as ej
    assert ej.JUDGE_CLASSES == wp.judge_classes()
    assert not any(c.startswith(("other.", "safety.")) for c in ej.JUDGE_CLASSES)   # measured by code (declared)


def test_rules_config_defaults_are_the_policy():
    from services.acp_content_writing.claim_rules import RulesConfig
    rc = RulesConfig()
    assert (rc.hypothetical, rc.uncited_background, rc.background_numbers) == ("warn", "pass", "warn")
    assert rc.policy_sha == wp.policy_sha()


def test_route_rows_rebuild_the_s271_gate_sets_unchanged():
    from services.acp_content_writing import f9_policy, quality_gates as qg
    assert qg.CHEAP_REPAIR_GATES == frozenset({"F15_voice", "F13_repeat", "F17_trip_fact", "F18_rarity",
                                               "F19_authenticity", "F2_platform_truth", "F2_superlatives",
                                               "F9_brand_style"})
    assert qg.STYLE_ONLY_GATES == frozenset({"F15_voice", "F2_superlatives", "F9_brand_style"})
    assert qg.OWNED_BY_FIX_STEP == frozenset({"F16_faq", "F14_first_line"})
    assert f9_policy.HOLD_CLASSES == frozenset({f9_policy.TRIP_PROMISE})
    gates = [r["gate"] for r in wp.compiled()["route_rows"]["gates"]]
    assert len(gates) == len(set(gates))


def test_aa788_promotions_are_registry_entries_equal_to_the_code(monkeypatch):
    from services.acp_content_writing import quality_gates as qg
    for name in (qg.F12_SCOPE_MODE_ENV, qg.F9_BRAND_STYLE_MODE_ENV, qg.F14_FLOOR_MODE_ENV):
        monkeypatch.delenv(name, raising=False)
    code = {qg.F12_SCOPE_MODE_ENV: qg.f12_scope_mode(), qg.F9_BRAND_STYLE_MODE_ENV: qg.f9_brand_style_mode(),
            qg.F14_FLOOR_MODE_ENV: qg.f14_floor_mode()}
    rows = wp.compiled()["gate_promotions"]["rows"]
    assert {r["env"]: r["default"] for r in rows} == code


def test_aa920_rule_injection_is_registered_not_built():
    ri = wp.compiled()["rule_injection"]
    assert ri["status"] == "planned" and set(ri["writers"]) == {"a1", "t2", "t9_blog", "t9_social"}


def test_fill_renders_tokens_and_refuses_an_unknown_one():
    assert wp.fill("<<meta.min>>-<<meta.max>>") == "140-155"
    assert wp.fill("<<t9_repair.background>>") == wp.fragment("t9_repair.background")
    with pytest.raises(wp.PolicyError):
        wp.fill("<<meta.typo>>")


@pytest.mark.parametrize("slug,cap,want", [
    ("corran-monastery", 60, "corran-monastery"),
    ("a" * 70, 60, "a" * 60),
    ("corran-monastery-library-hall-above-orvine-and-the-old-pilgrim-path", 60,
     "corran-monastery-library-hall-above-orvine-and-the-old"),
    ("corran-monastery-library", 20, "corran-monastery"),
    ("corran-monastery-libr", 21, "corran-monastery-libr"),
])
def test_slug_at_boundary(slug, cap, want):
    assert wp.slug_at_boundary(slug, cap) == want


def test_slug_folds_cut_at_a_word_boundary():
    from services.acp_content_writing.service import _ascii_slug
    from services.acp_publish.idempotency import blog_slug
    long = "corran-monastery-library-hall-above-orvine-and-the-old-pilgrim-path"
    assert _ascii_slug(long) == "corran-monastery-library-hall-above-orvine-and-the-old"
    want = "corran-monastery-library-hall-above-orvine-and-the-old-01234567"
    assert blog_slug(long, "p:blog:0123456789abcdef") == want


def test_taxonomy_severities_agree_with_the_policy():
    from services.quality import policy_lint, registry
    assert policy_lint.structural_errors(registry.ROOT if hasattr(registry, "ROOT") else None) == []
    classes = registry.classes()
    for cid, sev in wp.severities().items():
        assert classes[cid]["severity"] == sev


def test_release_fingerprint_has_the_policy_component():
    from services.writing_memory import config_fp
    rec = config_fp.build("t9_blog", routes=[], policies=[], params=[], catalog=[], deployments=[], env={})
    assert rec["parts"]["policy"] == wp.policy_sha()
    old = {"config_fp": {**rec, "parts": {**rec["parts"], "policy": "0" * 16}}}
    assert config_fp.reverify_components(old, {"config_fp": rec}) == ["policy"]
    assert config_fp.reasons_of(["policy"]) == ["prompt_version_mismatch"]
    legacy = {"config_fp": {**rec, "parts": {k: v for k, v in rec["parts"].items() if k != "policy"}}}
    assert config_fp.reverify_components(legacy, {"config_fp": rec}) == []   # a pre-W1 record is not a change


def test_prompt_hash_moves_with_the_policy(tmp_path):
    from shared.llm_client import prompt_registry as pr
    (tmp_path / "services" / "quality").mkdir(parents=True)
    (tmp_path / "m.py").write_text('P = _wp.fill("<<meta.min>>")\nQ = "plain"\n', encoding="utf-8")
    compiled = tmp_path / pr.POLICY_COMPILED
    compiled.write_text(json.dumps({"policy_sha": "a" * 16}), encoding="utf-8")
    a = pr.prompt_hash_of("m.py", ["P"], root=tmp_path)
    plain = pr.prompt_hash_of("m.py", ["Q"], root=tmp_path)
    compiled.write_text(json.dumps({"policy_sha": "b" * 16}), encoding="utf-8")
    assert pr.prompt_hash_of("m.py", ["P"], root=tmp_path) != a
    assert pr.prompt_hash_of("m.py", ["Q"], root=tmp_path) == plain


def test_generated_ts_carries_the_bands_and_the_sha():
    from services.quality import gen_frontend
    ts = gen_frontend.render()
    assert f'WRITING_POLICY_SHA = "{wp.policy_sha()}"' in ts
    assert "meta: { min: 140, max: 155 }" in ts


def test_a_day_index_line_never_supports_a_count():
    """S272 review: 'Day 2 of the trip' let F1 pass 'two nights' / '2 nights' (num-11); the day index is no figure."""
    from services.acp_content_writing import claim_rules as cr
    from services.acp_content_writing import quality_gates as qg
    seed = "Brecca harbour — visit\n\nDay 2 of the trip\n\nSource itinerary: Walk to Brecca harbour."
    assert cr.number_source_text(seed).count("2") == 0 and "Day 3: Walk" in cr.number_source_text("Day 3: Walk")
    for text in ("The tour includes two nights in Brecca.", "The tour includes 2 nights in Brecca."):
        g = qg.gate_grounding(text, seed)
        assert not g["passed"] and g["blocking"], text
    assert qg.gate_grounding("On day 2 you walk to Brecca harbour.", seed)["passed"]


def test_background_position_and_trip_number_follow_f17_and_f1():
    """S272 review: place_background passed an invented position (F17 holds it) and warned on a number about the trip
    (F1 holds it)."""
    from services.acp_content_writing import claim_rules as cr
    seed = "Brecca harbour — visit\n\nSource itinerary: Walk to Brecca harbour on Lake Tamsel."
    lines = cr.number_source_lines(seed)

    def verdict(text):
        sents = cr.split_piece(text)
        rows = {s.sid: cr.ClaimRow(s.sid, type="place_background", places=("Brecca",)) for s in sents}
        (v,) = cr.apply_rules(rows, lines, sents)
        return "block" if v.blocking else ("warn" if v.problem else "pass")

    assert verdict("Brecca sits on the far shore of Lake Tamsel.") == "block"
    assert verdict("Brecca harbour holds 300 fishing boats.") == "warn"
    assert verdict("On this walk you count 300 fishing boats in Brecca harbour.") == "block"
