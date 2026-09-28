"""AA-685 — every model call goes through the gateway (shared/llm_client/), so it has a stage
route, a catalog price and a caller-written shared.llm_call_log row. This test fails when a raw
Bedrock/OpenAI call appears anywhere else in the app code, which is how the calls AA-685 moved
had stayed invisible on External Spend.

Parsed with `ast`, so comments and docstrings that mention these methods do not count.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = ("services", "api", "shared")
GATEWAY = Path("shared/llm_client")
RAW_BEDROCK_METHODS = {"invoke_model", "invoke_model_with_response_stream", "converse", "converse_stream"}

# path -> reason. Keep this short; a new entry needs a reason a reviewer can check.
ALLOWED = {
    # _invoke_judge_legacy() and its three backends: only reached with an explicit `model=` or
    # without `stage=`, i.e. the AA-351 comparison scripts. All 5 production call sites pass
    # stage= and go through LLMClient (AA-659).
    "services/acp_produce/judge_client.py": "legacy judge path for comparison scripts",
}


def _raw_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        is_openai = (attr == "create" and isinstance(node.func.value, ast.Attribute)
                     and node.func.value.attr == "completions")
        if attr in RAW_BEDROCK_METHODS or is_openai:
            hits.append(f"{path.relative_to(REPO)}:{node.lineno} .{attr}()")
    return hits


def test_no_raw_model_calls_outside_the_gateway():
    found = []
    for root in SCANNED_ROOTS:
        for path in (REPO / root).rglob("*.py"):
            rel = path.relative_to(REPO)
            if rel.is_relative_to(GATEWAY) or str(rel) in ALLOWED:
                continue
            found.extend(_raw_calls(path))
    assert not found, (
        "Raw model calls outside shared/llm_client/ — route them through LLMClient.generate() or "
        "shared.llm_client.embed.embed() and write llm_call_log:\n" + "\n".join(found)
    )


def test_allowlist_entries_still_exist():
    for rel in ALLOWED:
        assert (REPO / rel).exists(), f"stale ALLOWED entry: {rel}"
