"""AA-234 Phần A — re-validation graph + human-edit gate.

Phần A adds a re-validation path for content a reviewer edited IN PLACE: a dedicated
graph (validate → judge → brand_audit → human_edit_gate, NO flag_fix) re-scores the
edited version and sets revalidate_passed. Approve is then gated on that flag.

These tests exercise:
  * human_edit_gate_node pass/fail logic (the gate that decides revalidate_passed).
  * build_revalidation_graph compiles and is wired entry=validate (NOT generate),
    with no flag_fix node and no retry loop.
They do NOT hit the DB or any LLM — the gate node is pure, and graph structure is
introspected from the compiled graph.
"""
import pytest
from services.content_generation.graph import (
    build_revalidation_graph,
    human_edit_gate_node,
    min_quality,
)


def _state(score, audit_status="", failure_codes=None):
    return {
        "quality_score": score,
        "brand_audit_status": audit_status,
        "failure_codes": failure_codes or [],
    }


def test_gate_pass_when_score_clears_and_audit_clean():
    out = human_edit_gate_node(_state(min_quality(), ""))
    assert out["revalidate_passed"] is True
    assert out["revalidate_ran"] is True


def test_gate_pass_above_threshold():
    out = human_edit_gate_node(_state(9.2, "pass"))
    assert out["revalidate_passed"] is True


def test_gate_fail_when_score_below_min():
    out = human_edit_gate_node(_state(min_quality() - 0.1, "pass"))
    assert out["revalidate_passed"] is False
    assert out["revalidate_ran"] is True


def test_gate_fail_when_brand_audit_manual_check():
    out = human_edit_gate_node(_state(9.5, "manual_check"))
    assert out["revalidate_passed"] is False


def test_gate_flagged_audit_still_passes_if_score_ok():
    out = human_edit_gate_node(_state(8.0, "flagged"))
    assert out["revalidate_passed"] is True


def test_gate_preserves_state():
    s = _state(8.0, "pass")
    s["seo_meta_extra"] = "keep-me"
    out = human_edit_gate_node(s)
    assert out["seo_meta_extra"] == "keep-me"


def test_revalidation_graph_compiles():
    g = build_revalidation_graph()
    assert g is not None
    assert type(g).__name__ == "CompiledStateGraph"


def test_revalidation_graph_has_no_flag_fix_or_generate():
    g = build_revalidation_graph()
    nodes = set(g.get_graph().nodes.keys())
    assert "flag_fix" not in nodes
    assert "generate" not in nodes
    assert "increment_retry" not in nodes
    assert "validate" in nodes
    assert "llm_judge" in nodes
    assert "brand_audit" in nodes
    assert "human_edit_gate" in nodes


def test_gate_hard_block_code_fails_despite_high_score():
    # AA-234: a hard-block rule violation (meta under floor) must fail the gate even when
    # the overall score clears min_quality() — the exact bug #1 the gate closes.
    out = human_edit_gate_node(_state(9.0, "pass", ["META_TOO_SHORT"]))
    assert out["revalidate_passed"] is False


def test_gate_forbidden_word_hard_blocks():
    out = human_edit_gate_node(_state(8.5, "flagged", ["FORBIDDEN_WORD"]))
    assert out["revalidate_passed"] is False


def test_gate_soft_code_does_not_block():
    # HIGHLIGHTS_NOT_LIST is a soft/format code — surfaced but must NOT block approve.
    out = human_edit_gate_node(_state(8.0, "pass", ["HIGHLIGHTS_NOT_LIST"]))
    assert out["revalidate_passed"] is True


def test_gate_mixed_codes_hard_wins():
    out = human_edit_gate_node(_state(9.0, "pass", ["HIGHLIGHTS_NOT_LIST", "META_TOO_SHORT"]))
    assert out["revalidate_passed"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
