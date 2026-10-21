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


def test_t9_nothing_rewrites_content_text_after_final_integrity():
    tree = _tree(service.run_write_background)
    final = _call_lines(tree, "_final_integrity")
    assert len(final) == 1, "run_write_background calls _final_integrity exactly once"
    late = [ln for ln in _assignment_lines(tree, "content_text") if ln > final[0]]
    assert not late, (f"content_text is assigned at line(s) {late} of run_write_background, after "
                      f"_final_integrity (line {final[0]}): a fixer after the last integrity pass ships text "
                      "no gate checked")


def test_t9_post_loop_fixers_sit_between_final_pass_and_final_integrity():
    tree = _tree(service.run_write_background)
    final_pass = _call_lines(tree, "final_pass")
    final = _call_lines(tree, "_final_integrity")[0]
    assert final_pass and max(final_pass) < final
    for fixer in ("_enforce_authenticity", "_cut_truth_items"):
        lines = _call_lines(tree, fixer)
        assert lines, f"{fixer} is no longer called — update this test and design §4.4"
        assert max(lines) > min(final_pass) and max(lines) < final, fixer


def test_a1_revalidate_strips_and_fits_before_it_validates():
    tree = _tree(graph.revalidate_node)
    validate = _call_lines(tree, "validate_node")
    assert validate, "revalidate_node no longer re-validates"
    for step in ("_apply_forbidden_strip", "_apply_seo_meta_fit"):
        lines = _call_lines(tree, step)
        assert lines and max(lines) < min(validate), f"{step} must run before validate_node in revalidate_node"


# The repairs that run AFTER validate_node scored the content. Each must be wrapped by the repair contract
# (AA-925 PR3) and re-checked; a new one added here needs its own Q0 case.
KNOWN_POST_VALIDATE_REPAIRS = {"_apply_grounding_recheck": "repair_and_recheck"}


def test_known_post_validate_repairs_are_listed():
    for fn_name, repair in KNOWN_POST_VALIDATE_REPAIRS.items():
        assert _call_lines(_tree(getattr(graph, fn_name)), repair), f"{fn_name} no longer calls {repair}"


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
    assert not any(src == "revalidate" and dst != "__end__" for src, dst in edges), "revalidate is the last node"
