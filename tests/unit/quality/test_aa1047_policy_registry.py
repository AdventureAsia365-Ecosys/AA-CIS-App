"""AA-1047 (root ADR 0020, W1) — the writing policy registry: one source, one policy_sha, consumers switched.

Done-when pins: one policy_sha across every compiled artefact and its consumers (rubric, LABEL_RUBRIC, RulesConfig,
route rows, the judge prompt, the release fingerprint, the generated TS), one meta band in code, the AA-788 / AA-920
registry entries equal to today's code (no behaviour change), and the compiled file fresh.
"""
from __future__ import annotations

import json
import re

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
    # S272: main's values until the W2 claim instrument (the D1 intent is claims.background.w2_intent)
    assert (rc.hypothetical, rc.uncited_background) == ("warn", "warn")
    assert not hasattr(rc, "background_numbers")
    assert rc.policy_sha == wp.policy_sha()


def test_route_rows_rebuild_the_s271_gate_sets_unchanged():
    from services.acp_content_writing import f9_policy, quality_gates as qg
    assert qg.CHEAP_REPAIR_GATES == frozenset({"F15_voice", "F13_repeat", "F17_trip_fact", "F18_rarity",
                                               "F19_authenticity", "F2_platform_truth", "F2_superlatives",
                                               "F9_brand_style"})
    assert qg.STYLE_ONLY_GATES == frozenset({"F15_voice", "F2_superlatives", "F9_brand_style"})
    assert qg.OWNED_BY_FIX_STEP == frozenset({"F16_faq", "F14_first_line"})
    assert f9_policy.hold_classes() == frozenset({f9_policy.TRIP_PROMISE})   # S279 L2: the seeded verdicts
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



def test_a_policy_constant_imported_from_another_module_reads_the_policy(tmp_path):
    """S272 review: `_wp.` in the hashed symbols missed prompts rendered through constants (`_BACKGROUND`, the meta
    band constants). reads_policy is file level and follows imported constants, never helper functions."""
    from shared.llm_client import prompt_registry as pr
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "bands.py").write_text("from services.quality import writing_policy as _wp\n"
                                  "LO, HI = _wp.band('meta')\nDERIVED = LO + 1\n"
                                  "def helper():\n    return _wp.band('meta')\nPLAIN = 3\n", encoding="utf-8")
    (pkg / "uses_const.py").write_text("from .bands import DERIVED\nP = f'aim {DERIVED}'\n", encoding="utf-8")
    (pkg / "uses_helper.py").write_text("from pkg.bands import helper, PLAIN\nP = 'x'\n", encoding="utf-8")
    (pkg / "direct.py").write_text("import services.quality.writing_policy as policy\nP = 'x'\n", encoding="utf-8")
    assert pr.reads_policy("pkg/bands.py", tmp_path)
    assert pr.reads_policy("pkg/uses_const.py", tmp_path)        # a relative import of a derived constant
    assert pr.reads_policy("pkg/direct.py", tmp_path)
    assert not pr.reads_policy("pkg/uses_helper.py", tmp_path)   # a helper's output is behaviour, not prompt text
    (tmp_path / pr.POLICY_COMPILED).parent.mkdir(parents=True)
    compiled = tmp_path / pr.POLICY_COMPILED
    compiled.write_text(json.dumps({"policy_sha": "a" * 16}), encoding="utf-8")
    before = pr.prompt_hash_of("pkg/uses_const.py", ["P"], root=tmp_path)
    compiled.write_text(json.dumps({"policy_sha": "b" * 16}), encoding="utf-8")
    assert pr.prompt_hash_of("pkg/uses_const.py", ["P"], root=tmp_path) != before


_POLICY_IMPORT = re.compile(r"^\s*(?:from services\.quality import [^\n]*\bwriting_policy\b"
                            r"|from services\.quality\.writing_policy import|import services\.quality\.writing_policy)",
                            re.M)
_POLICY_CONSTANTS = re.compile(r"\b(?:SEO_META_MIN|SEO_META_MAX|SEO_TITLE_MAX|SEO_TITLE_MIN|FAQ_MIN_PAIRS)\b")


def test_every_manifest_stage_whose_file_reads_the_policy_hashes_the_policy_sha(monkeypatch):
    """S272 review (done-when "one policy_sha across all artefacts"): walk the prompt manifest; a stage whose source
    file imports the writing policy, or one of its band / pair-count constants, must change hash when the policy_sha
    does, else a policy edit changes what it sends while the ledger keeps a stale prompt_version."""
    from shared.llm_client import prompt_registry as pr
    manifest = pr.load_manifest()
    reading, plain = [], []
    for stage, entries in sorted(manifest.items()):
        entry = entries[-1]
        text = (pr.REPO_ROOT / entry["source_path"]).read_text(encoding="utf-8")
        monkeypatch.setattr(pr, "_policy_sha", lambda root: "a" * 16)
        a = pr.prompt_hash_of(entry["source_path"], entry["symbols"])
        monkeypatch.setattr(pr, "_policy_sha", lambda root: "b" * 16)
        moved = pr.prompt_hash_of(entry["source_path"], entry["symbols"]) != a
        if _POLICY_IMPORT.search(text) or _POLICY_CONSTANTS.search(text):
            assert moved, f"{stage}: {entry['source_path']} reads the writing policy but its hash ignores policy_sha"
            reading.append(stage)
        else:
            plain.append(stage)
    # the four the review named (rendered through constants / helpers, not `_wp.` in the hashed symbols)
    assert {"s1_generate", "t2_generate", "s1_flag_fix", "t10_judge"} <= set(reading)
    assert "a3_atomize" in plain and "t8_angle_gen" in plain


def test_t10_judge_hashes_its_user_prompt():
    """The F9 user prompt renders the policy's background line; t10_judge v2 hashes it with the system prompt."""
    from shared.llm_client import prompt_registry as pr
    assert "f9_user_prompt" in pr.newest(pr.load_manifest(), "t10_judge")["symbols"]

def test_generated_ts_carries_no_band_copy():
    """S279 L2: the bands are settings; the pages read them at runtime (GET /admin/quality/thresholds), so the
    generated file carries no copy of them (audit 5.4 check 3: no mirrors)."""
    from services.quality import gen_frontend
    ts = gen_frontend.render()
    assert "SEO_BANDS" not in ts and "WRITING_POLICY_SHA" not in ts and "140" not in ts


def test_an_aim_under_the_band_names_a_short_fix_that_exists():
    """S272 review: the T9 aim (130-145) sat under the band (140-155) with nothing to land a 130-139 meta in it."""
    import importlib
    import inspect

    from services.acp_content_writing import service as svc
    src = wp.load_source()
    lo, _hi = wp.band("meta")
    for writer, aim in src["writer_aims"]["meta"].items():
        if aim["min"] >= lo:
            continue
        module, func = aim["short_fix"].split(":")
        assert callable(getattr(importlib.import_module(module), func)), (writer, aim["short_fix"])
    # S272: the T9 short fix is the SEO fit step itself (F4 holds what it leaves, as on main); no country tail
    assert src["writer_aims"]["meta"]["t9"]["short_fix"] == "services.acp_content_writing.service:_fit_seo_meta"
    assert "extend_short_meta" not in inspect.getsource(svc._fit_seo_meta)
    bad = {**src, "writer_aims": {"meta": {"t9": {"min": 130, "max": 145}}}}
    assert any("short_fix" in p for p in wp.schema_errors(bad))


def test_the_fit_note_tells_a_short_meta_the_band():
    from services.acp_content_writing import generate as gen
    assert gen._META_SHORT == " — too short: must be 140-155"
    assert "130-145" in gen._META_MUST


def test_t9_short_meta_goes_to_the_fit_call_and_is_never_composed():
    """S272: a 130-139 T9 meta is on the writer's aim but under the band. Three review rounds found the deterministic
    DESTINATION tail naming the wrong country (cross-border trips, route pieces, card-less moments), so it was
    withdrawn:
    the meta goes to the fit call and, when the fit leaves it, stays as written for F4 to hold, as on main."""
    import asyncio
    from unittest.mock import patch
    from uuid import uuid4

    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import service as svc
    meta = ("Walk up to Corran Monastery above Orvine, see the library hall the monks still keep open and follow "
            "the steep path down to the lake.")
    title = "Corran Monastery: The Library Hall Above Orvine"
    lo, _hi = wp.band("meta")
    seed = ("Corran Monastery — visit\nLocation: Corran Monastery, Velmora (do not place it in any other town, region "
            "or country)\nSource itinerary: Walk up to Corran Monastery.")
    assert wp.writer_aim("t9")[0] <= len(meta) < lo
    with patch.object(svc, "fit_seo_fields", return_value=({}, 0.01)) as fit:
        out, _cost = asyncio.run(svc._fit_seo_meta({"seo_title": title, "meta_description": meta}, None, body="b",
                                                   context={"destination": "Velmora", "atom_text": seed},
                                                   request_id=uuid4()))
    assert fit.call_count >= 1
    assert out["meta_description"] == meta
    g = qg.gate_seo_surface(title, out["meta_description"], "corran-monastery-orvine", None)
    assert not g["passed"] and any(v.startswith("meta description") for v in g["violations"])
