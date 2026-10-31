"""AA-642 (S11 prep, AA-1018 part 2) — a ratchet on the request-level model override.

``LLMRequest.model_tier`` skips the stage route (``shared.llm_role_config``): its fallbacks, its data scopes, the
writer/judge rule and the shadow. The design (gateway v2, step 4) moves every call to its stage route; the S11 flip that
removes the fixed tiers changes the model used, so it needs approval and a paid trial and is not done here. This test
freezes today's sites so no NEW one appears:

- a fixed tier anywhere in app or script code (``model_tier="haiku"`` as a keyword, a dict value or a state key);
- an ``LLMRequest(model_tier=<expression>)`` (the A/B and eval harnesses pass a model on purpose; app code passes the
  caller's optional override, None by default).

A new site fails: route the call through a stage row instead (add a role_config row by migration), or, for an eval /
adhoc script, add it here with the reason. A site that disappears fails too: remove it from ALLOWED (the ratchet only
shrinks). ``tests/`` and the gateway itself (``shared/llm_client/``) are out of scope.
"""
import ast
import subprocess
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXPR = "LLMRequest(model_tier=<expr>)"

# (file, site) -> count. "model_tier='x'" = a fixed tier; EXPR = an override passed through.
ALLOWED = {
    # App code: the six fixed tiers of the S262 audit (map "LLM call sites", AA-642 S11).
    ("api/routers/admin_pipeline.py", "model_tier='sonnet'"): 1,          # _sonnet_upgrade (AA-237 opt-in)
    ("api/routers/admin_pipeline.py", "model_tier='haiku'"): 1,           # revalidation graph state
    ("services/acp_contract/segment_research.py", "model_tier='haiku'"): 1,
    ("services/acp_contract/segment_research_batch.py", "model_tier='haiku'"): 1,
    ("services/eval/regression.py", "model_tier='haiku'"): 1,
    # App code: the caller's optional override, passed through (None = the stage route).
    ("services/acp_angle_gate/generate.py", EXPR): 1,                     # T8 A/B harness
    ("services/acp_content_writing/faq_answer_fit.py", EXPR): 1,          # adhoc_aa779 measurement
    ("services/acp_content_writing/faq_filter.py", EXPR): 1,              # adhoc_aa779 measurement
    ("services/acp_content_writing/seed_align/adjudicate.py", EXPR): 1,   # calibration script override
    ("services/content_generation/flag_fix_node.py", EXPR): 2,            # the S1 state's model_tier
    ("services/content_generation/graph.py", EXPR): 1,
    ("services/content_generation/grounding.py", EXPR): 1,
    ("services/content_generation/s1_writer_v2_live.py", EXPR): 1,
    ("services/content_generation/source_facts.py", EXPR): 1,
    # Eval / adhoc scripts (they compare models on purpose; logged under adhoc_* stages).
    ("scripts/aa645/s1_screen.py", EXPR): 1,
    ("scripts/aa645/shadow.py", "model_tier='sonnet'"): 1,
    ("scripts/aa729/score.py", "model_tier='haiku'"): 1,
    ("scripts/aa729/score.py", "model_tier='sonnet'"): 1,
    ("scripts/aa790/t9_writer_repanel.py", EXPR): 1,
}


def _sites(path: str, text: str) -> Counter:
    out: Counter = Counter()
    for node in ast.walk(ast.parse(text, filename=path)):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            for kw in node.keywords:
                if kw.arg != "model_tier":
                    continue
                if isinstance(kw.value, ast.Constant):
                    if kw.value.value is not None:
                        out[(path, f"model_tier={kw.value.value!r}")] += 1
                elif name == "LLMRequest":
                    out[(path, EXPR)] += 1
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "model_tier" and isinstance(v, ast.Constant)
                        and v.value is not None):
                    out[(path, f"model_tier={v.value!r}")] += 1
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value is not None:
            for t in node.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "model_tier":
                    out[(path, f"model_tier={node.value.value!r}")] += 1
    return out


def _scan() -> Counter:
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True,
                           check=True).stdout.split()
    found: Counter = Counter()
    for f in files:
        if f.startswith(("tests/", "shared/llm_client/")):
            continue
        found += _sites(f, (REPO / f).read_text(encoding="utf-8"))
    return found


def test_no_new_model_tier_site():
    found = _scan()
    new = {k: v for k, v in found.items() if v > ALLOWED.get(k, 0)}
    assert not new, ("new model_tier override(s) skip the stage route: " + "; ".join(
        f"{f} {s} x{n}" for (f, s), n in sorted(new.items()))
        + ". Route the call through its stage (llm_role_config row by migration), or allowlist an eval script.")


def test_the_allowlist_only_shrinks():
    found = _scan()
    gone = {k: v for k, v in ALLOWED.items() if found.get(k, 0) < v}
    assert not gone, "a model_tier site went away: lower or remove it in ALLOWED: " + "; ".join(
        f"{f} {s}" for (f, s) in sorted(gone))


def test_the_scanner_sees_each_shape():
    text = ("LLMRequest(system_prompt='s', user_prompt='u', model_tier=m)\n"
            "LLMRequest(system_prompt='s', user_prompt='u', model_tier=None)\n"
            "run(model_tier='haiku')\n"
            "state = {'model_tier': 'sonnet', 'other': 1}\n"
            "state['model_tier'] = 'haiku'\n")
    assert _sites("x.py", text) == Counter({("x.py", EXPR): 1, ("x.py", "model_tier='haiku'"): 2,
                                            ("x.py", "model_tier='sonnet'"): 1})
