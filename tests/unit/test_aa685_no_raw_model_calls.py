"""AA-685 / AA-642 — every model call goes through the gateway (shared/llm_client/), so it has a stage
route, a catalog price and a caller-written shared.llm_call_log row. This test fails when a raw
Bedrock / Anthropic / OpenAI call appears anywhere else in the app code or in scripts/, which is how the
calls AA-685 moved had stayed invisible on External Spend.

What counts as a bypass (AA-642 L6, S263):
- a Bedrock runtime or batch method: invoke_model(_with_response_stream), converse(_stream), apply_guardrail,
  create_model_invocation_job;
- a vendor SDK call: <x>.completions / .messages / .responses / .embeddings .create/.stream/.parse
  (OpenAI chat, Anthropic Messages, OpenAI Responses, OpenAI embeddings);
- importing the vendor SDK (`openai`, `anthropic`) or a gateway internal (invoke_claude, get_satellite_client);
- a vendor endpoint in a string: api.anthropic.com, api.openai.com, bedrock-runtime (an httpx / requests URL, or a
  boto3 "bedrock-runtime" client built outside the gateway).

Parsed with `ast`, so comments and docstrings that mention these names do not count.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = ("services", "api", "shared", "worker", "scripts")
GATEWAY = Path("shared/llm_client")
RAW_BEDROCK_METHODS = {
    "invoke_model", "invoke_model_with_response_stream", "converse", "converse_stream",
    "apply_guardrail", "create_model_invocation_job",
}
# <client>.<resource>.<verb>(): OpenAI chat / Responses / embeddings, Anthropic Messages (also client.beta.messages).
SDK_RESOURCES = {"completions", "messages", "responses", "embeddings"}
SDK_VERBS = {"create", "stream", "parse"}
VENDOR_MODULES = {"openai", "anthropic"}
VENDOR_HOSTS = ("api.anthropic.com", "api.openai.com", "bedrock-runtime")
# AA-757 (S224): the gateway's own low-level helpers. Calling them from outside skips the route,
# fallback, shadow and JSON check even though the call is logged — a3_atomize (then stage t5_atomize) did exactly that.
GATEWAY_INTERNALS = {"invoke_claude", "get_satellite_client"}

# (path, what) -> reason. `what` is the hit without its line number, as the failure message prints it.
# A new entry needs a reason a reviewer can check; a real bypass found later goes in with "TODO: ..." and an issue.
ALLOWED: dict[tuple[str, str], str] = {
    ("services/llm_cost/reconcile.py", "host api.anthropic.com"):
        "AA-1002 S10 nightly reconcile reads the Anthropic organization cost report (billing API, no model call)",
    ("services/llm_cost/reconcile.py", "host api.openai.com"):
        "AA-1002 S10 nightly reconcile reads the OpenAI organization costs endpoint (billing API, no model call)",
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _hits(source: str) -> list[tuple[int, str]]:
    """(line, what) for every gateway bypass in one module's source."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in VENDOR_MODULES:
                    hits.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in VENDOR_MODULES and node.level == 0:
                hits.append((node.lineno, f"import {node.module}"))
            names = [a.name for a in node.names if a.name in GATEWAY_INTERNALS]
            if names:
                hits.append((node.lineno, "import " + ", ".join(names)))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in GATEWAY_INTERNALS:
                hits.append((node.lineno, f"{name}()"))
            elif isinstance(func, ast.Attribute):
                if func.attr in RAW_BEDROCK_METHODS:
                    hits.append((node.lineno, f".{func.attr}()"))
                elif (func.attr in SDK_VERBS and isinstance(func.value, ast.Attribute)
                      and func.value.attr in SDK_RESOURCES):
                    hits.append((node.lineno, f".{func.value.attr}.{func.attr}()"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            for host in VENDOR_HOSTS:
                if host in node.value:
                    hits.append((node.lineno, f"host {host}"))
    return hits


def _scan() -> list[tuple[str, int, str]]:
    found = []
    for root in SCANNED_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO)
            if rel.is_relative_to(GATEWAY):
                continue
            found.extend((str(rel), line, what) for line, what in _hits(path.read_text(encoding="utf-8")))
    return found


def test_no_raw_model_calls_outside_the_gateway():
    bad = [f"{rel}:{line} {what}" for rel, line, what in _scan() if (rel, what) not in ALLOWED]
    assert not bad, (
        "Raw model calls outside shared/llm_client/ — route them through LLMClient.generate() or "
        "shared.llm_client.embed.embed() and write llm_call_log (or add a reviewed ALLOWED entry):\n"
        + "\n".join(bad)
    )


def test_allowlist_entries_still_match():
    """Ratchet: an ALLOWED entry whose call site went away must be removed, so the list never hides a new one."""
    seen = {(rel, what) for rel, _, what in _scan()}
    stale = [f"{rel} {what}" for rel, what in ALLOWED if (rel, what) not in seen]
    assert not stale, "stale ALLOWED entries (remove them):\n" + "\n".join(stale)


def test_scanned_roots_exist():
    for root in SCANNED_ROOTS:
        assert (REPO / root).is_dir(), f"scanned root missing: {root}"


# The detector itself, on invented snippets (so a refactor of _hits cannot silently stop catching a pattern).
_BYPASSES = {
    "rt.converse(modelId=m, messages=[])": ".converse()",
    "rt.converse_stream(modelId=m)": ".converse_stream()",
    "rt.invoke_model(body=b, modelId=m)": ".invoke_model()",
    "rt.apply_guardrail(guardrailIdentifier=g, source='INPUT', content=[])": ".apply_guardrail()",
    "bedrock.create_model_invocation_job(jobName=j)": ".create_model_invocation_job()",
    "client.chat.completions.create(model=m, messages=[])": ".completions.create()",
    "client.messages.create(model=m, max_tokens=5, messages=[])": ".messages.create()",
    "client.beta.messages.stream(model=m, messages=[])": ".messages.stream()",
    "client.responses.create(model=m, input='hi')": ".responses.create()",
    "client.embeddings.create(model=m, input=['hi'])": ".embeddings.create()",
    "import openai": "import openai",
    "from anthropic import Anthropic": "import anthropic",
    "from shared.llm_client.bedrock_satellite import get_satellite_client": "import get_satellite_client",
    "invoke_claude(prompt)": "invoke_claude()",
    "httpx.post('https://api.anthropic.com/v1/messages', json={})": "host api.anthropic.com",
    "requests.post(f'https://api.openai.com/v1/{path}', json={})": "host api.openai.com",
    "boto3.client('bedrock-runtime', region_name='us-west-1')": "host bedrock-runtime",
}


def test_detector_catches_each_bypass_pattern():
    for snippet, expected in _BYPASSES.items():
        assert [what for _, what in _hits(snippet)] == [expected], snippet


def test_detector_ignores_docstrings_comments_and_unrelated_calls():
    source = '''
"""Calls api.openai.com through the gateway; never rt.converse() here."""
def f(db, inbox):
    """bedrock-runtime is only named in this docstring."""
    # client.messages.create(...) in a comment
    inbox.messages.send("x")
    db.create(table="t")
    return "converse"
'''
    assert _hits(source) == []
