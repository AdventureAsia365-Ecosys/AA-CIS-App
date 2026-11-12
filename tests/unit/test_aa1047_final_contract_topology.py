"""AA-1047 W1 (root ADR 0020 invariant 4) — every exit of S1, T2 and T9 passes through the final contract.

Two halves:

* **Graph topology.** Every A1 content graph (live, T2 drafts, resumed, Bedrock batch seed, re-validation) has ONE
  edge to END, from the ``final_contract`` node; every other node has an outgoing edge (a node without one ends the
  run silently: the batch graph's retry writer did, found by this test) and reaches ``final_contract``.
* **Write sites.** Every function in ``api/`` and ``services/`` that writes stored AI text (a generated_content text or
  approved-status write, a tenant_tour_versions rewritten_content write, a content_piece content_text write) or
  publishes a Master (``process_export``) is an exit: it is listed in ``final_contract.EXITS`` and calls the
  contract, or it is listed below with why it is not an exit. A new write site fails here until it is wired.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from services.content_generation import graph
from services.quality import final_contract as fc

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("api", "services")

# Write sites that store no AI text a reader gets, with the reason. Keys like final_contract.EXITS.
NOT_EXITS = {
    "services/tenant_account/e2e_fixtures.py::subject_gate_flag":
        "Dev-only E2E fixture (AA-1087 S278): stores one invented attempt text on a synthetic tenant so the real "
        "subject gate opens a flag for the release spec; no AI text, refuses outside Dev",
    "services/acp_content_writing/service.py::_insert_placeholder_piece":
        "the empty 'processing' row a write starts with (no text yet)",
    "services/acp_content_writing/service.py::_finalize_piece":
        "the one T9 write; the exit is its caller run_write_background, which passes the contract in",
    "services/content_generation/t2_orchestrator.py::save_version":
        "the one linear T2 write (AA-1049 W3, opt-in); the exit is its caller run_and_save, which passes the "
        "contract in",
    "api/routers/v1_tours.py::trigger_rewrite":
        "the empty 'pending' T2 version a rewrite starts with (no text yet)",
    "services/export/handler.py::_refuse_not_live":
        "a publish refusal: moves an approved version back to 'hitl' (no text, no publish)",
    "services/export/handler.py::_refuse_policy_review":
        "the elephant-riding policy refusal (AA-1049): moves an approved version back to 'hitl' (no text, no publish)",
    "services/validation/handler.py::process_validation":
        "the legacy Step Functions validation handler (no live caller; ADR 0020 W0 lists legacy paths for removal)",
}

_WRITE_RE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE)\s+[\w{}.]*?\b(generated_content|tenant_tour_versions|"
                       r"content_piece)\b", re.I)
_TEXT_COLUMNS = {
    "generated_content": re.compile(r"\baa_\w+|\bseo_title\b|\bseo_meta\b|SET\s*\{|status\s*=\s*'approved'", re.I),
    "tenant_tour_versions": re.compile(r"\brewritten_content\b", re.I),
    "content_piece": re.compile(r"\bcontent_text\b", re.I),
}
_CALLS_CONTRACT = re.compile(r"final_contract|\b_fc(?:_store)?\.|_contract\(|contract_record|contract_exit=")


def _graphs():
    return {
        "build_graph": graph.build_graph(),
        "build_graph_t2_drafts": graph.build_graph(t2_drafts=True),
        "build_graph_resumed": graph.build_graph_resumed(),
        "build_graph_from_generated": graph.build_graph_from_generated(),
        "build_revalidation_graph": graph.build_revalidation_graph(),
    }


@pytest.mark.parametrize("name", list(_graphs()))
def test_every_graph_leaves_through_the_final_contract(name):
    g = _graphs()[name].get_graph()
    edges = [(e.source, e.target) for e in g.edges]
    to_end = sorted({s for s, t in edges if t == "__end__"})
    assert to_end == [graph.FINAL_CONTRACT_NODE], f"{name}: END is reached from {to_end}"
    nodes = set(g.nodes) - {"__start__", "__end__"}
    out = {n: {t for s, t in edges if s == n} for n in nodes}
    dead = sorted(n for n in nodes if not out[n])
    assert not dead, f"{name}: node(s) {dead} have no outgoing edge (LangGraph would end the run there)"
    # every node reaches the final contract
    reach = {graph.FINAL_CONTRACT_NODE}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n not in reach and out[n] & reach:
                reach.add(n)
                changed = True
    assert reach >= nodes, f"{name}: {sorted(nodes - reach)} never reach the final contract"


def _py_files():
    for d in SCAN_DIRS:
        yield from sorted((ROOT / d).rglob("*.py"))


def _docstrings(node: ast.AST) -> set[int]:
    """ids of the docstring constants under ``node`` (prose, never SQL)."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def _strings(node: ast.AST) -> list[str]:
    out = []
    docs = _docstrings(node)
    for n in ast.walk(node):
        if id(n) in docs:
            continue
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            out.append("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values))
    return out


def _is_text_write(sql: str) -> bool:
    for m in _WRITE_RE.finditer(sql):
        if _TEXT_COLUMNS[m.group(1).lower()].search(sql[m.start():]):
            return True
    return False


def _write_sites() -> dict[str, str]:
    """{"path::function": source} of every function that writes stored text or calls process_export."""
    found: dict[str, str] = {}
    for path in _py_files():
        rel = path.relative_to(ROOT).as_posix()
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        # module-level SQL constants that write text, by name
        consts = {t.id for node in tree.body if isinstance(node, ast.Assign)
                  for t in node.targets if isinstance(t, ast.Name)
                  and any(_is_text_write(s) for s in _strings(node.value))}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body_src = ast.get_source_segment(src, node) or ""
            writes = any(_is_text_write(s) for s in _strings(node))
            writes = writes or any(isinstance(n, ast.Name) and n.id in consts for n in ast.walk(node))
            calls_export = any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None))
                               == "process_export" for n in ast.walk(node))
            if writes or (calls_export and node.name != "process_export"):
                found[f"{rel}::{node.name}"] = body_src
    return found


def _exits() -> dict[str, tuple[str, ...]]:
    return {k: v for wf in fc.EXITS.values() for k, v in wf.items()}


def test_every_text_write_is_a_listed_exit():
    sites = _write_sites()
    exits = _exits()
    unlisted = sorted(k for k in sites if k not in exits and k not in NOT_EXITS)
    assert not unlisted, ("these functions store AI text or publish a Master but are not final-contract exits "
                          f"(wire the contract and list them in final_contract.EXITS, or in NOT_EXITS): {unlisted}")


def test_every_listed_exit_exists_and_calls_the_contract():
    sites = _write_sites()
    for key in _exits():
        path, func = key.split("::")
        src = (ROOT / path).read_text(encoding="utf-8")
        nodes = [n for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == func]
        assert nodes, f"{key} does not exist"
        body = sites.get(key) or ast.get_source_segment(src, nodes[0]) or ""
        assert _CALLS_CONTRACT.search(body), f"{key} does not run the final contract"
    for key in NOT_EXITS:
        path, func = key.split("::")
        assert re.search(rf"def {func}\(", (ROOT / path).read_text(encoding="utf-8")), f"{key} does not exist"


def test_every_publish_names_its_exit():
    for path in _py_files():
        src = path.read_text(encoding="utf-8")
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "process_export":
                kws = {k.arg for k in n.keywords}
                assert "contract_exit" in kws, f"{path.relative_to(ROOT)}:{n.lineno} process_export without contract_exit"


def test_the_exit_table_names_every_s1_exit_of_the_design():
    """ADR 0020 roadmap W1: done, hitl, revalidation, resumed, promote, server approve (+ T2 and T9)."""
    s1 = {e for names in fc.EXITS[fc.S1].values() for e in names}
    assert {"done", "hitl", "revalidation", "resumed", "promote", "approve"} <= s1
    assert any("done" in v for v in fc.EXITS[fc.T2].values()) and any("done" in v for v in fc.EXITS[fc.T9].values())
