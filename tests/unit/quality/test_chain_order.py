"""AA-925 (Q-2) — chain order guards (design §4.4 point 5): no fixer runs after the last integrity pass.

* T9 (`service.run_write_background`): no assignment to ``content_text`` after the ``_final_integrity(...)``
  call — the text it checks is the text that is stored. (``strip_citation_tags(discarded_attempt[...])`` after
  it rewrites the DISCARDED attempt, not the stored text, so the check is on assignments to the name.)
  ``_cut_truth_items`` (inside the loop) and ``_enforce_authenticity`` run after the in-loop
  ``text_integrity.final_pass``; that is why ``_final_integrity`` is the real last pass.
* A1 (`graph.revalidate_node`): the deterministic strip and the seo_meta fit run before ``validate_node``
  re-scores the repaired content.
* The three content graphs keep grounding → flag_fix → revalidate.
* Known post-validate repair: ``_apply_grounding_recheck`` calls ``repair_and_recheck`` after
  ``validate_node``; the repair contract (services/quality/repair_contract.py) covers that call.
"""
import ast
import inspect
import textwrap

import pytest

from services.acp_content_writing import service
from services.content_generation import graph


def _tree(fn) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _call_lines(tree: ast.AST, name: str) -> list[int]:
    """Line numbers of calls to ``name`` (a bare name or the last attribute: ``x.final_pass``)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            called = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if called == name:
                out.append(node.lineno)
    return sorted(out)


def _assigned_names(node: ast.AST) -> set[str]:
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    names = set()
    for t in targets:
        for sub in ast.walk(t):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
    return names


def _assignment_lines(tree: ast.AST, name: str) -> list[int]:
    return sorted(n.lineno for n in ast.walk(tree) if name in _assigned_names(n))


def _norm_unpack(node: ast.AST) -> bool:
    """``x = norm["..."]`` / ``a, b = norm["a"], norm["b"]``: the unpack of the normalizer's result (AA-1047)."""
    if not isinstance(node, ast.Assign):
        return False
    vals = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
    return all(isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name) and v.value.id == "norm" for v in vals)


def test_t9_nothing_rewrites_content_text_after_final_integrity():
    """AA-1047 W1: the post-loop edits live in ONE normalizer (_normalize_final_text) whose last step is
    _final_integrity; run_write_background calls it once and assigns content_text after it only from its result."""
    norm_tree = _tree(service._normalize_final_text)
    final = _call_lines(norm_tree, "_final_integrity")
    assert len(final) == 1, "_normalize_final_text calls _final_integrity exactly once"
    late = [ln for ln in _assignment_lines(norm_tree, "content_text") if ln > final[0]]
    assert not late, f"content_text is assigned after _final_integrity in the normalizer (line(s) {late})"

    tree = _tree(service.run_write_background)
    assert not _call_lines(tree, "_final_integrity"), "the final integrity pass belongs to _normalize_final_text"
    norm = _call_lines(tree, "_normalize_final_text")
    assert len(norm) == 1, "run_write_background calls _normalize_final_text exactly once"
    late = [n.lineno for n in ast.walk(tree) if "content_text" in _assigned_names(n) and n.lineno > norm[0]
            and not _norm_unpack(n)]
    assert not late, (f"content_text is assigned at line(s) {late} of run_write_background, after the normalizer "
                      f"(line {norm[0]}): a fixer after the last integrity pass ships text no gate checked")


def test_t9_post_loop_fixers_sit_between_final_pass_and_final_integrity():
    tree = _tree(service.run_write_background)
    final_pass = _call_lines(tree, "final_pass")
    norm = _call_lines(tree, "_normalize_final_text")[0]
    assert final_pass and max(final_pass) < norm
    lines = _call_lines(tree, "_cut_truth_items")
    assert lines and min(final_pass) < max(lines) < norm, "_cut_truth_items"
    norm_tree = _tree(service._normalize_final_text)
    final = _call_lines(norm_tree, "_final_integrity")[0]
    for fixer in ("_cut_stock_lines", "_enforce_authenticity", "strip_citation_tags", "_apply_faq_answer_fit"):
        lines = _call_lines(norm_tree, fixer)
        assert lines, f"{fixer} is no longer called — update this test and design §4.4"
        assert max(lines) < final, fixer


def test_t9_final_contract_checks_the_stored_text():
    """AA-1047 W1: the contract runs after the normalizer, on the variable _finalize_piece stores."""
    tree = _tree(service.run_write_background)
    norm = _call_lines(tree, "_normalize_final_text")[0]
    contract = _call_lines(tree, "_t9_final_contract")
    finalize = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_finalize_piece" and any(k.arg == "final_contract" for k in n.keywords)]
    assert len(contract) == 1 and norm < contract[0], "the contract runs once, after the normalizer"
    assert len(finalize) == 1 and finalize[0].lineno > contract[0]
    kw = {k.arg: k.value for k in finalize[0].keywords}
    assert isinstance(kw["content_text"], ast.Name) and kw["content_text"].id == "content_text"


def test_a1_revalidate_strips_and_fits_before_it_validates():
    tree = _tree(graph.revalidate_node)
    validate = _call_lines(tree, "validate_node")
    assert validate, "revalidate_node no longer re-validates"
    for step in ("_apply_forbidden_strip", "_apply_seo_meta_fit"):
        lines = _call_lines(tree, step)
        assert lines and max(lines) < min(validate), f"{step} must run before validate_node in revalidate_node"


# The repairs that run AFTER validate_node scored the content. Each must be wrapped by the repair contract
# (AA-925 PR3) and re-checked; a new one added here needs its own Q0 case.
KNOWN_POST_VALIDATE_REPAIRS = {"_grounding_recheck": "repair_and_recheck"}


def test_known_post_validate_repairs_are_listed():
    for fn_name, repair in KNOWN_POST_VALIDATE_REPAIRS.items():
        assert _call_lines(_tree(getattr(graph, fn_name)), repair), f"{fn_name} no longer calls {repair}"


def test_post_validate_grounding_repair_is_under_the_contract():
    from services.content_generation import grounding
    assert _call_lines(_tree(grounding.repair_and_recheck), "enforce_fields"), \
        "repair_and_recheck no longer runs the repair contract"


def test_a1_degraded_hold_is_the_last_step():
    """AA-925: the one A1 degraded hold wraps the last step of the graph (revalidate -> grounding re-check)."""
    tree = _tree(graph._apply_grounding_recheck)
    assert _call_lines(tree, "apply_a1_hold") and _call_lines(tree, "_grounding_recheck")


def test_t9_degraded_hold_follows_final_integrity():
    """AA-925: the T9 degraded hold is computed after the last integrity pass (the normalizer, AA-1047), from the
    final attempt."""
    tree = _tree(service.run_write_background)
    final = _call_lines(tree, "_normalize_final_text")[0]
    hold = _call_lines(tree, "_degraded_final")
    assert hold and min(hold) > final


def _edges(compiled) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in compiled.get_graph().edges}


@pytest.mark.parametrize("builder", [
    pytest.param(lambda: graph.build_graph(), id="build_graph"),
    pytest.param(lambda: graph.build_graph(t2_drafts=True), id="build_graph_t2_drafts"),
    pytest.param(graph.build_graph_from_generated, id="build_graph_from_generated"),
])
def test_graphs_ground_then_fix_then_revalidate(builder):
    edges = _edges(builder())
    for edge in (("brand_audit", "grounding"), ("grounding", "flag_fix"), ("flag_fix", "revalidate")):
        assert edge in edges, f"missing edge {edge}"
    # AA-1047 W1: revalidate leads only to the final contract, the one edge to END
    assert not any(src == "revalidate" and dst != "final_contract" for src, dst in edges), \
        "revalidate is the last check node"
    assert ("final_contract", "__end__") in edges
