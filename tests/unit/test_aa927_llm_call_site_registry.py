"""AA-927 — every place that makes (or hands out) an LLM call is registered with the function that writes
its shared.llm_call_log row, or with a reason why it writes none.

Static (AST) guard: it catches a NEW call site. Whether each registered path really writes one row per
billed call is checked behaviourally in test_aa927_one_row_per_call.py.

A hit is, in services/, api/, shared/ and tracked scripts/ (the gateway internals client.py, embed.py and
shadow.py excluded — they are the gateway):
- any load of a `.generate` attribute in a module that imports LLMClient — called or passed
  (`asyncio.to_thread(llm.generate, request)`, `LLMClient().generate` as an argument);
- any load of the name `embed` imported from shared.llm_client.embed, and `<embed module>._call`;
- any load of `invoke_judge`, `score_brand_fit`, `score_candidate_fit`, `_judge` (the judge helpers whose
  callers write the row) and `submit_batch_job` (Bedrock Batch).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("services", "api", "shared", "scripts")
GATEWAY_INTERNALS = {"shared/llm_client/client.py", "shared/llm_client/embed.py", "shared/llm_client/shadow.py"}
HELPER_NAMES = {"invoke_judge", "score_brand_fit", "score_candidate_fit", "_judge", "submit_batch_job"}
LOGGERS = {"record_call", "record_call_sync", "record_call_with_pool", "record_calls", "record_calls_sync",
           "record_billed_failure", "record_billed_failure_sync", "record_unusable", "record_unusable_sync",
           "_log_t10_judge_call", "_record_t9_write_call"}

_JN = ("services/content_generation/judge_node.py", "judge_node")
_DEBATE = ("services/acp_shared/debate.py", "apply_debate")
_F9_LOG = ("services/acp_content_writing/quality_gates.py", "_log_t10_judge_call")
_F9_FAIL = ("services/acp_content_writing/quality_gates.py", "_log_t10_billed_unusable")


def _self(path: str, fn: str) -> tuple:
    return ((path, fn),)


# (path, enclosing function) -> the function(s) that write the row. The canonical pattern is the call
# site itself (row written right after generate() returns, billed_unusable row in the except branch).
LOGGED_CALL_SITES: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("services/acp_angle_gate/generate.py", "_generate_once"): _self(
        "services/acp_angle_gate/generate.py", "_generate_once"),
    ("services/acp_content_writing/generate.py", "_generate_full"): (
        ("services/acp_content_writing/generate.py", "_record_t9_write_call"),),
    ("services/acp_content_writing/generate.py", "repair_sentences_detailed"): _self(
        "services/acp_content_writing/generate.py", "repair_sentences_detailed"),
    ("services/acp_content_writing/generate.py", "fit_seo_fields"): _self(
        "services/acp_content_writing/generate.py", "fit_seo_fields"),
    ("services/acp_content_writing/generate.py", "write_faq"): _self(
        "services/acp_content_writing/generate.py", "write_faq"),
    ("services/acp_content_writing/quality_gates.py", "_gate_brand_voice"): (_F9_LOG, _F9_FAIL),
    ("services/acp_produce/judge_client.py", "invoke_judge"): (_F9_LOG, _F9_FAIL),
    ("services/acp_contract/a3_atomize.py", "_atomize_whole_tour_legacy"): _self(
        "services/acp_contract/a3_atomize.py", "_atomize_whole_tour_legacy"),
    ("services/acp_contract/a3_atomize.py", "_atomize_per_day"): _self(
        "services/acp_contract/a3_atomize.py", "_atomize_per_day"),
    ("services/acp_contract/segment_research.py", "_research_place"): _self(
        "services/acp_contract/segment_research.py", "_research_place"),
    ("services/acp_contract/segment_research_batch.py", "_propose"): _self(
        "services/acp_contract/segment_research_batch.py", "_propose"),
    ("services/acp_shared/content_embedding.py", "compute_embeddings"): _self(
        "services/acp_shared/content_embedding.py", "compute_embeddings"),
    ("services/acp_shared/debate.py", "apply_debate"): (_DEBATE,),
    ("services/acp_shared/place_cards.py", "draft_card"): (("services/acp_shared/place_cards.py", "_log"),),
    ("services/acp_shared/place_cards.py", "check_card"): (("services/acp_shared/place_cards.py", "_log"),),
    ("services/content_generation/brand_audit_node.py", "brand_audit_node"): _self(
        "services/content_generation/brand_audit_node.py", "brand_audit_node"),
    # brand_fit makes the judge calls; judge_node (S1/T2) and debate (T7) write their rows, one per call.
    ("services/content_generation/brand_fit.py", "_judge"): (_JN, _DEBATE),
    ("services/content_generation/brand_fit.py", "score_brand_fit"): (_JN,),
    ("services/content_generation/brand_fit.py", "_judge_parallel.one"): (_JN,),
    ("services/content_generation/brand_fit.py", "score_candidate_fit"): (_DEBATE,),
    ("services/content_generation/flag_fix_node.py", "_rerepair_meta"): _self(
        "services/content_generation/flag_fix_node.py", "_rerepair_meta"),
    ("services/content_generation/flag_fix_node.py", "flag_fix_node"): _self(
        "services/content_generation/flag_fix_node.py", "flag_fix_node"),
    ("services/content_generation/graph.py", "generate_node"): _self(
        "services/content_generation/graph.py", "generate_node"),
    ("services/content_generation/grounding.py", "repair"): _self(
        "services/content_generation/grounding.py", "repair"),
    # the nudge never raises after its call; both callers write the row right after it returns.
    ("services/content_generation/itinerary_utils.py", "nudge_itinerary_day"): (
        ("services/content_generation/graph.py", "_process_itineraries"),
        ("services/content_generation/flag_fix_node.py", "_repair_still_compressed_days")),
    ("services/content_generation/judge_node.py", "judge_node"): (_JN,),
    ("services/content_generation/source_facts.py", "extract_day_facts"): _self(
        "services/content_generation/source_facts.py", "extract_day_facts"),
    ("services/places/llm.py", "_generate_sync"): (("services/places/llm.py", "make_generate.generate"),),
    ("services/places/llm.py", "embed_texts"): _self("services/places/llm.py", "embed_texts"),
    ("shared/llm_client/column_mapper.py", "detect_column_mapping"): (
        ("shared/llm_client/column_mapper.py", "_log_call"),),
    ("scripts/aa856_global_profile_probe.py", "_invoke_global"): (
        ("scripts/aa856_global_profile_probe.py", "_log_probe"),),
}

# (path, enclosing function) -> why no llm_call_log row is written there.
EXCLUDED: dict[tuple[str, str], str] = {
    ("api/routers/admin_pipeline.py", "get_tour_version_detail"): "`_judge` is a local variable (stored judge JSON)",
    ("api/routers/admin_pipeline.py", "get_tour_detail"): "`_judge` is a local variable (stored judge JSON)",
    ("scripts/aa729/evalpatch.py", "install"): "the AA-729 harness wraps LLMClient.generate to count calls; the "
                                               "wrapped call's own caller writes the row",
    ("services/content_generation/s1_batch.py", "submit_s1_batch"): (
        "S1 Bedrock Batch writes no llm_call_log row: batch has never run (AA-624 blocks it). It MUST log one "
        "row per record once it is enabled — remove this entry then."),
}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", *SCAN_DIRS], cwd=ROOT, capture_output=True, text=True, check=True)
    return [f for f in out.stdout.split() if f.endswith(".py") and f not in GATEWAY_INTERNALS]


def _imports(tree: ast.AST) -> tuple[bool, set[str], set[str]]:
    """(imports LLMClient, names bound to shared.llm_client.embed.embed, names bound to the embed module)."""
    llm, embed_fn, embed_mod = False, set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                if a.name == "LLMClient":
                    llm = True
                if n.module.endswith("llm_client.embed") and a.name == "embed":
                    embed_fn.add(a.asname or "embed")
                if n.module.endswith("llm_client") and a.name == "embed":
                    embed_mod.add(a.asname or "embed")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.endswith("llm_client.embed") and a.asname:
                    embed_mod.add(a.asname)
    return llm, embed_fn, embed_mod


def _hits(path: str) -> list[tuple[str, str, int]]:
    tree = ast.parse((ROOT / path).read_text())
    llm, embed_fn, embed_mod = _imports(tree)
    found: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for ch in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{ch.name}" if scope else ch.name
            what = None
            if isinstance(ch, ast.Attribute) and isinstance(ch.ctx, ast.Load):
                if ch.attr == "generate" and llm:
                    what = ".generate"
                elif ch.attr == "_call" and isinstance(ch.value, ast.Name) and ch.value.id in embed_mod:
                    what = "embed._call"
                elif ch.attr in HELPER_NAMES:
                    what = ch.attr
            elif isinstance(ch, ast.Name) and isinstance(ch.ctx, ast.Load):
                if ch.id in embed_fn:
                    what = "embed"
                elif ch.id in HELPER_NAMES:
                    what = ch.id
            if what:
                found.append((scope or "<module>", what, ch.lineno))
            visit(ch, inner)

    visit(tree, "")
    return found


def _function(path: str, qualname: str):
    tree = ast.parse((ROOT / path).read_text())
    node: ast.AST = tree
    for part in qualname.split("."):
        node = next((c for c in ast.walk(node)
                     if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and c.name == part),
                    None)
        if node is None:
            return None
    return node


def _names_in(fn: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _all_hits() -> dict[tuple[str, str], list[tuple[str, int]]]:
    hits: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for path in _tracked_files():
        for scope, what, line in _hits(path):
            hits.setdefault((path, scope), []).append((what, line))
    return hits


def test_every_llm_call_site_is_registered():
    hits = _all_hits()
    unregistered = {k: v for k, v in hits.items() if k not in LOGGED_CALL_SITES and k not in EXCLUDED}
    assert not unregistered, (
        "New LLM call site(s) without a registered llm_call_log writer: "
        + "; ".join(f"{p}::{fn} ({', '.join(f'{w} line {ln}' for w, ln in v)})" for (p, fn), v in
                    sorted(unregistered.items()))
        + ". Route the call through LLMClient.generate() and write one llm_call_log row per call with "
          "record_call_* (canonical: right after generate() returns, record_unusable_sync in the except), "
          "then add it to LOGGED_CALL_SITES — or register a deliberate exclusion with a reason in EXCLUDED.")


def test_no_stale_registry_entries():
    hits = _all_hits()
    stale = sorted(k for k in (*LOGGED_CALL_SITES, *EXCLUDED) if k not in hits)
    assert not stale, f"Registry entries with no call site any more (remove them): {stale}"
    assert not set(LOGGED_CALL_SITES) & set(EXCLUDED)


def test_every_registered_writer_writes_llm_call_log():
    bad = []
    for site, writers in LOGGED_CALL_SITES.items():
        assert writers, site
        for path, fn in writers:
            node = _function(path, fn)
            if node is None:
                bad.append(f"{site} -> {path}::{fn} (function not found)")
            elif not (_names_in(node) & LOGGERS):
                bad.append(f"{site} -> {path}::{fn} (calls none of {sorted(LOGGERS)})")
    assert not bad, "; ".join(bad)


def test_exclusions_carry_a_reason():
    assert all(isinstance(r, str) and len(r) > 20 for r in EXCLUDED.values())


def test_detector_sees_indirect_references(tmp_path, monkeypatch):
    """The cases a `Call(.generate)`-only rule misses: a method passed to to_thread, a bound method as an
    argument, an imported embed passed to to_thread, the gateway-internal embed._call."""
    src = (
        "import asyncio\n"
        "from shared.llm_client.client import LLMClient\n"
        "from shared.llm_client.embed import embed\n"
        "from shared.llm_client import embed as embed_mod\n"
        "async def a(llm, r):\n    return await asyncio.to_thread(llm.generate, r)\n"
        "def b(r):\n    return run(LLMClient().generate, r)\n"
        "async def c(t):\n    return await asyncio.to_thread(embed, 's', t)\n"
        "def d(m):\n    return embed_mod._call(m, 'acc2', ['x'], 'q', None)\n"
    )
    (tmp_path / "probe.py").write_text(src)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    got = {(scope, what) for scope, what, _ in _hits("probe.py")}
    assert got == {("a", ".generate"), ("b", ".generate"), ("c", "embed"), ("d", "embed._call")}
