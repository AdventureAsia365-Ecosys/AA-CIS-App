"""AA-926 — the quality registry (services/quality/taxonomy.yaml + registry.py): schema, gate coverage,
per-channel counts, the frontend label superset and total lookups."""
import json
import re
from pathlib import Path

from services.quality import gen_frontend, lint, registry

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests" / "unit" / "fixtures" / "aa926_gate_snapshot.json"

# frontend/app/admin/content-quality/shared.ts GATE_LABELS before AA-926 (frozen copy, in order).
OLD_GATE_LABELS = [
    ("F0_complete", "Text cut off"),
    ("F1_grounding", "Number not in source"),
    ("F2_banned_patterns", "Cliché / banned phrase"),
    ("F2_brand_forbidden", "Brand-forbidden word"),
    ("F11_brand_name", "Wrong brand name"),
    ("F10_cannibalization_cross_tenant", "Too close to another tenant"),
    ("promises_an_option", "Optional item stated as included"),
    ("F4_extreme_length", "Length out of range"),
    ("F4_seo_surface", "SEO title / meta"),
    ("F5_atom_density", "Few source citations"),
    ("F3_structural_variance", "Flat structure"),
    ("F7_faq_dedup", "FAQ restates body"),
    ("F7_faq_autofix", "FAQ pairs removed"),
    ("F6_cta_present", "No CTA"),
    ("F8_framework", "Goal framework"),
    ("F9_cta_fact", "Unsupported claim / CTA"),
    ("F9_brand_style", "Off-brand / AI wording"),
    ("F12_scope", "Place outside the piece's scope"),
    ("F13_hygiene", "Leak / social proof / repeat / hashtag"),
    ("F13_leak", "Instruction / audience leak, broken opener"),
    ("F13_autofix", "Auto-fixed sentences"),
    ("F13_final_pass", "Final pass edits (duplicate, CTA, whitespace)"),
    ("F13_repeat", "Repeat / second CTA / broken sentence"),
    ("F17_trip_fact", "Trip fact not in source (optional, time, caveat, date, thin moment, hazard, base)"),
    ("F18_rarity", "Rarity / quantifier overclaim"),
    ("F14_social_shape", "LinkedIn length / hashtags"),
    ("F14_first_line", "LinkedIn first line too long"),
    ("F16_faq", "Blog FAQ under 3 pairs"),
    ("F15_voice", "Stock phrase"),
    ("F15_voice_warn", "Trade register / audience leak / opener"),
    ("within_tenant_reuse", "Repeats this tenant"),
    ("T3_grounding", "Number not in the source tour"),
    ("FORBIDDEN_WORD", "Forbidden word"),
]
OLD_RETIRED = {"promises_an_option", "F8_framework", "F3_structural_variance", "F5_atom_density", "F7_faq_dedup"}
NEW_LABELLED = ["F2_platform_truth", "F2_superlatives", "F2_style_warn", "F19_authenticity", "F15_stock_cut"]
T10_CHANNELS = ("blog", "linkedin", "facebook", "instagram", "tiktok", "email", "landing_page", "ads")


def _schema_errors() -> list[str]:
    res = lint.Result()
    lint.check_schema(res, set(lint.tracked_files()))
    return res.errors


def test_schema_is_valid():
    assert _schema_errors() == []


def test_taxonomy_version_and_class_count():
    assert registry.taxonomy()["version"] == registry.TAXONOMY_VERSION == "qtax-v1"
    core = [c for c in registry.classes() if not c.startswith(("other.", "judge."))]
    assert len(core) == 17  # design §3.3


def test_yaml_gates_equal_registry_constants():
    assert set(registry.gates()) == set(registry.GATE_CONSTANTS.values())


def test_every_ledger_gate_in_the_fault_set_is_registered_and_active():
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    seen = {e["gate"] for c in snap["t10"]["cases"].values() for e in c["ledger"]}
    seen |= {g for order in snap["t10"]["channel_gate_order"].values() for g in order}
    for g in seen:
        entry = registry.gate(g)
        assert entry is not None, g
        assert entry["kind"] == "gate" and not entry.get("retired"), g


def test_registry_channels_match_the_real_ledger_per_channel():
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for ch in T10_CHANNELS:
        expected = {n for n, g in registry.gates().items() if g.get("kind") == "gate" and not g.get("retired")
                    and (g.get("channels") == "all" or ch in (g.get("channels") or []))}
        assert expected == set(snap["t10"]["channel_gate_order"][ch]), ch
    counts = {ch: len(snap["t10"]["channel_gate_order"][ch]) for ch in T10_CHANNELS}
    assert counts["blog"] == 25 and counts["linkedin"] == 24  # CONTEXT.md "Gate" (AA-787)
    assert all(counts[c] == 23 for c in T10_CHANNELS if c not in ("blog", "linkedin"))


def test_every_gate_kind_has_a_label_and_a_class():
    for n, g in registry.gates().items():
        if g["kind"] == "gate":
            assert g.get("label"), n
            assert g.get("classes"), n


def test_persisted_and_retired_names_stay_registered():
    for n in OLD_RETIRED:
        assert registry.gate(n)["retired"] is True
    assert set(registry.retired_gates()) == OLD_RETIRED
    assert registry.gate("F9_brand_voice")["kind"] == "telemetry"  # llm_call_log.quality_signal.gate
    for n in ("F7_faq_autofix", "F13_autofix", "F13_final_pass", "within_tenant_reuse", "F15_stock_cut"):
        assert registry.gate(n)["kind"] == "flag"


def test_lookups_are_total():
    assert registry.gate("F99_unknown_legacy") is None
    assert registry.gate(None) is None
    assert registry.gate(42) is None
    assert registry.gate_label("F99_unknown_legacy") is None
    assert registry.gate_classes("F99_unknown_legacy") == []
    assert registry.class_info("nope.nothing") is None


def test_frontend_labels_are_a_superset_in_the_same_order():
    labels = {n: g["label"] for n, g in registry.gates().items() if g.get("label")}
    items = list(labels.items())
    assert items[:33] == OLD_GATE_LABELS
    assert [n for n, _ in items[33:]] == NEW_LABELLED
    ts = (ROOT / gen_frontend.OUT.relative_to(ROOT)).read_text(encoding="utf-8")
    body = re.search(r"GATE_LABELS: Record<string, string> = \{(.*?)\n\};", ts, re.S).group(1)
    assert re.findall(r"^\s+([A-Za-z0-9_]+):", body, re.M) == [n for n, _ in OLD_GATE_LABELS] + NEW_LABELLED
    retired = re.search(r"RETIRED_GATES[^\[]*\[(.*?)\]", ts, re.S).group(1)
    assert set(re.findall(r'"([^"]+)"', retired)) == OLD_RETIRED


def test_generated_frontend_file_is_fresh():
    assert gen_frontend.OUT.read_text(encoding="utf-8") == gen_frontend.render()
    assert "export const PASS_LINE = 7;" in gen_frontend.render()
    assert "  top: 9,\n  pass: 7,\n  low: 5," in gen_frontend.render()


def test_defect_classes_set_and_order_frozen():
    from api.routers import admin_content_quality as acq
    assert acq.DEFECT_CLASSES == ("fact", "voice", "format", "ai_phrasing")
    assert tuple(registry.chips()["defect_classes"]) == acq.DEFECT_CLASSES


def test_writing_memory_hard_stop_1_is_every_hard_fact_class():
    assert registry.hard_fact_classes() == ["fact.inclusion", "fact.number", "fact.misbinding", "fact.name",
                                            "fact.direction"]


def test_derived_owner_metric_tolerance():
    c = registry.class_info("fact.number")
    assert "F1_grounding" in c["owner_gates"] and c["metric"] == "new_verified_errors"
    assert registry.class_info("struct.truncated")["metric"] == "new_minus_fixed"
    assert registry.class_info("other.v1")["tolerance"] == "report only"


def test_every_seeded_and_coded_jev_key_is_registered():
    tracked = lint.tracked_files()
    seeded = lint.jev_seed_keys(ROOT, tracked)
    assert {"a1_promise_unsupported", "t10_rubric_met", "a3_same_place"} <= set(seeded)  # incl. migration 211
    assert set(seeded) <= set(registry.jev_questions())
    py = {p: (ROOT / p).read_text(encoding="utf-8") for p in tracked if lint._py_in_scope(p)}
    assert set(lint.jev_code_keys(py)) <= set(registry.jev_questions())


def test_parity_report_flags_t2_fact_gaps():
    rows = {r["class"]: r for r in registry.parity_report()}
    assert "T2" in rows["fact.inclusion"]["missing"]  # design §3.3: T2 only has a brand-fit score


# ------------------------------------------------------------------ S250: codes since the first base, Q0 corpus

def test_f15_stock_cut_is_a_registered_labelled_flag():
    from services.acp_content_writing import service

    g = registry.gate("F15_stock_cut")
    assert g["kind"] == "flag" and g["label"] and registry.F15_STOCK_CUT == "F15_stock_cut"
    assert service.F15_STOCK_CUT_FLAG == registry.F15_STOCK_CUT
    assert "F15_stock_cut:" in gen_frontend.render()


def test_day_oracle_and_s1_check_codes_are_mapped():
    from services.quality import day_oracle, degraded, s1_checks

    a1 = registry.a1_codes()
    assert set(day_oracle.DAY_ORACLE_MODES) <= set(a1)
    for name in ("META_CUT", "META_LENGTH", "TEXT_TRUNCATED", "NUMBER_NOT_IN_SOURCE", "NUMBER_NOUN_MISMATCH",
                 "NAME_NOT_IN_SOURCE", "DAY_PLACE_MISMATCH", "INCLUSION_CONTRADICTED"):
        assert getattr(s1_checks, name) in a1, name
    for code in (degraded.HOLD_CODE, "META_CUT_SIGNATURE", "BRAND_AUDIT_FLAGGED", "BRAND_AUDIT_UNAVAILABLE",
                 "ITINERARY_MEAL_TIME_INVENTED"):
        assert code in a1, code
    assert registry.a1_codes()["SOURCE_DAY_CONTRADICTION"]["classes"] == [day_oracle.CLASS_SOURCE]
    assert registry.a1_codes()["DAY_SUBDAY_SPLIT"]["classes"] == [day_oracle.CLASS_DAY]


def test_every_q0_case_resolves_through_the_registry():
    from tests.unit.quality import chain

    cases = chain.load_cases()
    assert len(cases) >= 70
    refs = registry.detector_refs()
    for c in cases:
        assert registry.resolve_class(c["class_id"]), (c["id"], c["class_id"])
        assert c["detector"] in refs, (c["id"], c["detector"])


def test_aliases_point_at_classes_and_never_shadow_one():
    for alias, targets in registry.aliases().items():
        assert alias not in registry.classes(), alias
        assert targets and all(t in registry.classes() for t in targets), alias
    assert registry.resolve_class("other.length") == ["other.length_shape"]
    assert registry.resolve_class("source.unsupported_specific") == ["fact.number", "fact.inclusion"]
    assert registry.resolve_class("fact.number") == ["fact.number"]
    assert registry.resolve_class("other.nope") == [] and registry.resolve_class(None) == []


def test_code_checks_name_real_modules():
    for name, cc in registry.code_checks().items():
        assert (ROOT / cc["path"]).is_file(), name
        assert cc["path"].endswith(f"/{name}.py"), name
