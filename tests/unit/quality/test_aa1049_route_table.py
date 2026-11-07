"""AA-1049 W3 (root ADR 0020 S5) — the one T9 route table: coverage lint, schema, and the S271 sets as views.

The zero-diff proof of the refactor is tests/unit/test_aa926_equivalence.py, which replays every routing decision
(sentence_repair.plan, hold_failure, never_holds, the routing sets, the truth-cut sets) against its committed fixture
UNCHANGED. This file pins the table itself: exactly one row per T10 gate id, a row agrees with its policy half, and
every set the loop used to keep by hand is the matching view of the table.
"""
from __future__ import annotations

import copy

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import route_table as rt
from services.acp_content_writing import sentence_repair, service
from services.quality import registry as qr

# The S271 hand-written sets, as they were before the route table (frozen literals: never edit to make a test pass).
S271_ELIGIBLE = frozenset({"F1_grounding", "F9_cta_fact", "F2_brand_forbidden", "F11_brand_name", "F12_scope",
                           "F13_hygiene", "F13_leak", "F15_voice", "F13_repeat", "F17_trip_fact", "F18_rarity",
                           "F19_authenticity", "F2_platform_truth", "F2_superlatives", "F9_brand_style"})
S271_QUOTED = frozenset({"F2_brand_forbidden", "F11_brand_name", "F12_scope", "F13_hygiene", "F13_leak", "F15_voice",
                         "F13_repeat", "F17_trip_fact", "F18_rarity", "F19_authenticity", "F2_platform_truth",
                         "F2_superlatives"})


def test_lint_exactly_one_row_per_t10_gate_id():
    assert rt.coverage_errors() == []
    assert rt.schema_errors() == []


def test_lint_catches_a_missing_and_a_doubled_row(monkeypatch):
    doc = copy.deepcopy(rt.mechanics())
    doc["rows"] = [r for r in doc["rows"] if r["gate"] != "F13_leak"] + [dict(doc["rows"][0])]
    errors = rt.schema_errors(doc)
    assert any("exactly one row per gate" in e for e in errors)
    assert any("route the same gates" in e and "F13_leak" in e for e in errors)
    rt.mechanics.cache_clear()
    monkeypatch.setattr(rt, "mechanics", lambda: doc)
    assert any("F13_leak" in e and "no route row" in e for e in rt.coverage_errors())


@pytest.mark.parametrize("row, needle", [
    ({"gate": "F1_grounding", "action": "repair_sentence"}, "parse is required"),
    ({"gate": "F1_grounding", "action": "cut"}, "action must be one of"),
    ({"gate": "F16_faq", "action": "regenerate_section"}, "section is required"),
    ({"gate": "F6_cta_present", "action": "rewrite", "cut": "truth"}, "cut must be one of"),
    ({"gate": "F6_cta_present", "action": "rewrite", "style_only": True}, "belong to the policy"),
    ({"gate": "F13_repeat", "action": "rewrite"}, "cheap row must be a repair_sentence"),
    ({"gate": "F16_faq", "action": "regenerate_section", "section": "seo_fields"}, "fix_step is exactly"),
    ({"gate": "F4_seo_surface", "action": "regenerate_section", "section": "seo_fields", "slug": "cut"},
     "slug must be one of"),
])
def test_row_schema_refuses_a_bad_row(row, needle):
    policy = {r["gate"]: r for r in rt._policy_rows()}
    assert any(needle in e for e in rt.row_errors(row, policy[row["gate"]]))


def test_every_live_t10_gate_has_a_taxonomy_class_and_an_action():
    for gate, r in rt.TABLE.items():
        assert r.action in rt.ROW_ACTIONS and r.classes, gate
    live = {g for g, i in qr.gates().items() if i.get("kind") == "gate" and not i.get("retired")
            and i.get("channels") != ["t2_version"]}
    assert live <= set(rt.TABLE)


def test_the_s271_sets_are_views_of_the_table_unchanged():
    assert sentence_repair._ELIGIBLE_GATES == S271_ELIGIBLE == rt.SENTENCE_REPAIR_GATES
    assert sentence_repair._QUOTED_GATES == S271_QUOTED == rt.QUOTED_GATES
    assert sentence_repair.TRUTH_GATES == (qr.F1_GROUNDING, qr.F9_CTA_FACT)
    assert sentence_repair.OWNED_ELSEWHERE == qg.OWNED_BY_FIX_STEP == frozenset({"F16_faq", "F14_first_line"})
    assert qg.STYLE_ONLY_GATES == frozenset({"F15_voice", "F2_superlatives", "F9_brand_style"})
    assert qg.CHEAP_REPAIR_GATES == frozenset({"F15_voice", "F13_repeat", "F17_trip_fact", "F18_rarity",
                                               "F19_authenticity", "F2_platform_truth", "F2_superlatives",
                                               "F9_brand_style"})
    assert service.TRUTH_CUT_GATES == frozenset({qr.F1_GROUNDING, qr.F9_CTA_FACT})
    assert service.F17_CUT_GATE == qr.F17_TRIP_FACT
    assert service.F9_CUT_NOT_HOLD_CHANNELS == frozenset({"linkedin"})
    # The two section paths of the S271 plan(): F4_seo_surface + the fix steps never rewrite; a slug does.
    assert rt.SECTION_GATES == frozenset({qr.F4_SEO_SURFACE}) | qg.OWNED_BY_FIX_STEP
    assert rt.SLUG_REWRITE_GATES == frozenset({qr.F4_SEO_SURFACE})


def test_parse_dispatch_names_one_gate_per_judge_format():
    assert {g for g, r in rt.TABLE.items() if r.parse == rt.PARSE_F9} == {qr.F9_CTA_FACT}
    assert {g for g, r in rt.TABLE.items() if r.parse == rt.PARSE_F1} == {qr.F1_GROUNDING}
    assert {g for g, r in rt.TABLE.items() if r.parse == rt.PARSE_BRAND_STYLE} == {qr.F9_BRAND_STYLE}


def test_holds_and_f9_classes():
    assert not rt.route(qr.F15_VOICE).holds and rt.route(qr.F9_CTA_FACT).holds
    assert rt.route("not_a_gate") is None and rt.action_of(None) is None
    assert rt.F9_CLASS_ACTIONS["trip_promise"] == "hold"
    assert {a for c, a in rt.F9_CLASS_ACTIONS.items() if c != "trip_promise"} == {"warn"}


def test_seo_field_violation_reads_the_parse():
    assert rt.is_seo_field_violation(qr.F12_SCOPE, "title names a place out of scope")
    assert rt.is_seo_field_violation(qr.F9_CTA_FACT, "meta claims a sunrise boat")
    assert not rt.is_seo_field_violation(qr.F1_GROUNDING, "title states 12")
    assert not rt.is_seo_field_violation(qr.F12_SCOPE, "scope: 'A sentence about Velmora harbour.'")
