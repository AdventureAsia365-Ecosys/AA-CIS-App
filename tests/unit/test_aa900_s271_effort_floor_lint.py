"""S271 W0 (AA-900 / AA-741, lane l13-w0-gateway) — the CI lint: every call-site ceiling clears
token_floor(model, effort) of every model its stage's route names.

The stage list is the routes' own (every stage a route migration names), not a hand-picked set:

1. The routes, efforts and catalog floors are read from the migrations (``_routes`` / ``_efforts`` /
   ``_min_output_tokens``).
2. The call sites are found by a scan of the code: every ``LLMRequest(...)`` / ``judge_request`` / ``call_judge`` /
   ``invoke_judge`` call in api/, services/, shared/, worker/. The scan reads a site's stage and ceiling when they are
   constants (a literal, a module constant, an imported constant, a parameter default, a local assignment, both arms
   of an ``a if c else b``, ``stage_for(state, "s1_x")`` = the S1 stage and its T2 twin). A call without max_tokens
   sends ``LLMRequest``'s default.
3. A site the scan cannot read is declared in ``DECLARED_SITES`` (the stages it serves, and the lowest ceiling when
   the ceiling is not a constant), or exempt in ``EXEMPT_SITES`` with the reason. An unread site that is neither fails.
4. Every routed stage needs at least one site, or a reason in ``NO_CALL_SITE``.
5. Every (stage, site) whose lowest ceiling is under the effort floor of a model on the stage's route must be on
   ``UNDER_EFFORT_FLOOR`` with the reason. The list only shrinks: a site that clears the floor now fails as stale,
   and a new site under the floor fails as new.

The gateway lifts a reasoning model to ``tokens.REASONING_FLOOR`` (or the catalog's ``min_output_tokens``) at run time,
whatever the call site sends; the effort floor is the static target above that (8192 at the default effort). So a site
on ``UNDER_EFFORT_FLOOR`` is sent at least 4096 on a reasoning model; the list is the debt between the two.

No AWS, no vendor, no DB, no paid call. The scanner's own test parses an invented module.
"""
from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from shared.llm_client import token_floor
from shared.llm_client.models import LLMRequest

ROOT = Path(__file__).resolve().parents[2]

# ── 1. routes, efforts and catalog floors from the migrations ───────────────────────────────────────────────────────

# [stage, primary, [old fallbacks], [new fallbacks]] rows of the route migrations (366, 376, ...); the last one wins
_ROUTE_ROW = re.compile(r'\[\s*"([a-z0-9_]+)",\s*"([a-z0-9.\-]+)",\s*\[([^\]]*)\],\s*\[([^\]]*)\]\s*\]', re.S)
# ('stage', 'model', 'effort' rows of a shared.stage_model_params seed (migration 381)
_EFFORT_ROW = re.compile(r"\(\s*'([a-z0-9_]+)',\s*'([a-z0-9.\-]+)',\s*'(low|medium|high|xhigh|max)'")
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# min_output_tokens set by a catalog migration (382: gpt-6-luna, gpt-6-luna-openai)
_MIN_OUT = re.compile(r"jsonb_build_object\('min_output_tokens',\s*(\d+)\).*?model_key\s+IN\s+\(([^)]*)\)", re.S)


def _migrations():
    return sorted((ROOT / "api" / "migrations").glob("*.sql"))


def _routes() -> dict:
    out: dict = {}
    for p in _migrations():
        for stage, primary, _old, new in _ROUTE_ROW.findall(p.read_text(encoding="utf-8")):
            out[stage] = [primary, *re.findall(r'"([^"]+)"', new)]
    return out


def _efforts() -> dict:
    out: dict = {}
    for p in _migrations():
        text = p.read_text(encoding="utf-8")
        if "stage_model_params" not in text:
            continue
        for stage, model, effort in _EFFORT_ROW.findall(text):
            if stage not in _EFFORTS:
                out[(stage, model)] = effort
    return out


def _min_output_tokens() -> dict:
    out: dict = {}
    for p in _migrations():
        for n, keys in _MIN_OUT.findall(re.sub(r"--[^\n]*", "", p.read_text(encoding="utf-8"))):
            for key in re.findall(r"'([^']+)'", keys):
                out[key] = max(out.get(key, 0), int(n))
    return out


def test_the_lint_reads_the_routes_efforts_and_catalog_floors():
    routes, efforts, mins = _routes(), _efforts(), _min_output_tokens()
    assert routes["t9_write"][0] == "sonnet-5-5" and routes["t10_judge"][0] == "gpt-5.6-luna"
    assert efforts[("t9_write", "sonnet-5-5")] == "medium" and efforts[("t9_faq_filter", "haiku-5-5")] == "low"
    assert mins["gpt-6-luna"] == 8192 and mins["gpt-6-luna-openai"] == 8192
    assert len(routes) >= 30                       # every routed stage, not a hand-picked few


# ── 2. the call-site scan ───────────────────────────────────────────────────────────────────────────────────────────

SITE_CALLS = frozenset({"LLMRequest", "judge_request", "call_judge", "invoke_judge"})
SCAN_DIRS = ("api", "services", "shared", "worker")
# Files the scan skips: the judge client builds the request from its callers' stage / max_tokens (the callers are
# the sites), and the contract smoke sends fixture requests that reuse each stage's production constants.
SKIP_FILES = {
    "shared/llm_client/judge.py": "the judge client: its callers (brand_fit, quality_gates) are the sites",
    "services/jobs/llm_contract_smoke_job.py": "contract smoke: fixture requests on the production constants",
}
DEFAULT_MAX_TOKENS = LLMRequest.model_fields["max_tokens"].default
_UNREAD = object()


@dataclass(frozen=True)
class Site:
    key: str                          # "<path>:<enclosing function>"
    line: int
    stages: Optional[tuple]           # None = the scan could not read it
    ceiling: Optional[int]


def _const(v):
    return v if isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool)) else _UNREAD


def _from_module(module: str, attr: Optional[str] = None):
    try:
        m = importlib.import_module(module)
    except Exception:  # noqa: BLE001 — an unimportable module reads as unread
        return _UNREAD
    if attr is None:
        return m
    if hasattr(m, attr):
        return getattr(m, attr)
    try:
        return importlib.import_module(f"{module}.{attr}")
    except Exception:  # noqa: BLE001
        return _UNREAD


def _bindings(body: list, args: Optional[ast.arguments] = None) -> dict:
    """name -> list of what binds it in one scope: ("node", ast) for an assignment or parameter default,
    ("param",) for a parameter without default, ("from", module, attr) / ("module", module) for an import."""
    out: dict = {}
    if args is not None:
        pos = args.posonlyargs + args.args
        defaults = dict(zip([a.arg for a in pos[len(pos) - len(args.defaults):]], args.defaults))
        for a in pos:
            out.setdefault(a.arg, []).append(("node", defaults[a.arg]) if a.arg in defaults else ("param",))
        for a, d in zip(args.kwonlyargs, args.kw_defaults):
            out.setdefault(a.arg, []).append(("node", d) if d is not None else ("param",))
    for n in _scope_nodes(body):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(t.id, []).append(("node", n.value))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None:
            out.setdefault(n.target.id, []).append(("node", n.value))
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            for a in n.names:
                out.setdefault(a.asname or a.name, []).append(("from", n.module, a.name))
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    out.setdefault(a.asname, []).append(("module", a.name))
                else:
                    out.setdefault(a.name.split(".")[0], []).append(("module", a.name.split(".")[0]))
    return out


def _scope_nodes(body: list):
    """The statements of one scope, nested blocks included, nested functions / classes left out."""
    todo = list(body)
    while todo:
        n = todo.pop(0)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield n
        for field in ("body", "orelse", "finalbody", "handlers"):
            todo.extend(getattr(n, field, None) or [])


def _resolve(node, scopes: list) -> Optional[list]:
    """The constant values ``node`` can take, or None when the scan cannot read it."""
    if isinstance(node, ast.Constant):
        v = _const(node.value)
        return None if v is _UNREAD else [v]
    if isinstance(node, ast.IfExp):
        a, b = _resolve(node.body, scopes), _resolve(node.orelse, scopes)
        return a + b if a is not None and b is not None else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "stage_for"
            and len(node.args) == 2):
        base = _resolve(node.args[1], scopes)
        if base is None:
            return None
        twins = getattr(_from_module("services.content_generation.stages"), "T2_TWINS", {})
        return list(dict.fromkeys([*base, *(twins.get(b, b) for b in base)]))
    if isinstance(node, ast.Name):
        for i in range(len(scopes) - 1, -1, -1):
            if node.id in scopes[i]:
                return _resolve_bindings(scopes[i][node.id], scopes[: i + 1])
        return None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        for i in range(len(scopes) - 1, -1, -1):
            if node.value.id in scopes[i]:
                values = []
                for b in scopes[i][node.value.id]:
                    base = (_from_module(b[1]) if b[0] == "module" else
                            _from_module(b[1], b[2]) if b[0] == "from" else _UNREAD)
                    v = _const(getattr(base, node.attr, _UNREAD)) if base is not _UNREAD else _UNREAD
                    if v is _UNREAD:
                        return None
                    values.append(v)
                return values or None
        return None
    return None


def _resolve_bindings(bindings: list, scopes: list) -> Optional[list]:
    values: list = []
    for b in bindings:
        if b[0] == "node":
            got = _resolve(b[1], scopes)
            if got is None:
                if len(bindings) > 1:
                    continue          # max_tokens = max(max_tokens, X) after max_tokens = X: the first is the lowest
                return None
            values.extend(got)
        elif b[0] == "from":
            v = _const(_from_module(b[1], b[2]))
            if v is _UNREAD:
                return None
            values.append(v)
        else:
            return None
    return values or None


def _scan_tree(rel: str, tree: ast.Module) -> list:
    sites: list = []

    def visit(node, scopes, fname):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(ch, scopes + [_bindings(ch.body, ch.args)], ch.name)
                continue
            if isinstance(ch, ast.Call):
                f = ch.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name in SITE_CALLS:
                    kw = {k.arg: k.value for k in ch.keywords if k.arg}
                    stages = _resolve(kw["stage"], scopes) if "stage" in kw else None
                    ceilings = _resolve(kw["max_tokens"], scopes) if "max_tokens" in kw else [DEFAULT_MAX_TOKENS]
                    ints = [c for c in ceilings or [] if isinstance(c, int)]
                    strs = [s for s in stages or [] if isinstance(s, str)]
                    sites.append(Site(f"{rel}:{fname}", ch.lineno, tuple(dict.fromkeys(strs)) if strs else None,
                                      min(ints) if ceilings and len(ints) == len(ceilings) else None))
            visit(ch, scopes, fname)

    visit(tree, [_bindings(tree.body)], "<module>")
    return sites


def _scan() -> list:
    out: list = []
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            if "tests" in p.parts or "node_modules" in p.parts or rel in SKIP_FILES:
                continue
            out.extend(_scan_tree(rel, ast.parse(p.read_text(encoding="utf-8"))))
    return out


def test_the_scanner_reads_constants_defaults_imports_and_both_arms():
    src = '''
from shared.llm_client.models import LLMRequest
from services.content_generation.stages import stage_for
WIDE = 9000
NARROW = 300
STAGE = "lantern_write"

def build(*, short=False, stage: str = STAGE):
    return LLMRequest(system_prompt="s", user_prompt="u", stage=stage, max_tokens=NARROW if short else WIDE)

def node(state):
    return LLMRequest(system_prompt="s", user_prompt="u", stage=stage_for(state, "s1_flag_fix"))

def passed(stage, max_tokens):
    from services.places.dedupe import MAX_TOKENS
    first = LLMRequest(system_prompt="s", user_prompt="u", stage="lantern_check", max_tokens=MAX_TOKENS)
    return first, LLMRequest(system_prompt="s", user_prompt="u", stage=stage, max_tokens=max_tokens)
'''
    from services.places import dedupe
    got = {(s.key, s.stages, s.ceiling) for s in _scan_tree("invented.py", ast.parse(src))}
    assert got == {("invented.py:build", ("lantern_write",), 300),
                   ("invented.py:node", ("s1_flag_fix", "t2_flag_fix"), DEFAULT_MAX_TOKENS),
                   ("invented.py:passed", ("lantern_check",), dedupe.MAX_TOKENS),
                   ("invented.py:passed", None, None)}


# ── 3. what the scan cannot read: declared, exempt, or no call site in this repo ────────────────────────────────────

def _const_of(module: str, attr: str) -> Callable[[], int]:
    return lambda: getattr(importlib.import_module(module), attr)


def _t9_write_lowest() -> int:
    from services.acp_content_writing import generate
    return min([*generate._MAX_TOKENS_BY_CHANNEL.values(), generate._MAX_TOKENS_DEFAULT])


def _s1_v2_lowest() -> int:
    from services.content_generation import s1_writer_v2 as v2
    return min(v2.WRITER_MAX_TOKENS, v2.META_MAX_TOKENS)


# site -> {stage: lowest ceiling (a callable reading the code's constant) | None (the scanned ceiling)}. The stages
# replace what the scan read for that site (a parameter default names one stage; the callers pass the others).
DECLARED_SITES: dict = {
    # write_content / rewrite_with_feedback: max_tokens_for(channel), one ceiling per channel
    "services/acp_content_writing/generate.py:write_content": {"t9_write": _t9_write_lowest},
    "services/acp_content_writing/generate.py:rewrite_with_feedback": {"t9_write": _t9_write_lowest},
    # stage=gen_stage: state["generate_stage"] (t2_generate on a tenant rewrite) or s1_generate
    "services/content_generation/graph.py:generate_node": {"s1_generate": None, "t2_generate": None},
    # the S1 writer v2 (state["generate_stage"] or A1_WRITE_STAGE): the draft and the meta calls
    "services/content_generation/s1_writer_v2_live.py:_call": {"s1_generate": _s1_v2_lowest,
                                                               "t2_generate": _s1_v2_lowest},
    # brand_fit's one judge call: s1_judge by default; T2 passes t2_judge, T7 Debate t7_debate_judge
    "services/content_generation/brand_fit.py:_judge": {"s1_judge": None, "t2_judge": None,
                                                        "t7_debate_judge": None},
    # brand_audit_node / flag_fix_node read the stage from stage_for(state, ...) through a local name
    "services/content_generation/brand_audit_node.py:brand_audit_node": {"s1_brand_audit": None,
                                                                         "t2_brand_audit": None},
    "services/content_generation/flag_fix_node.py:flag_fix_node": {"s1_flag_fix": None, "t2_flag_fix": None},
    "services/content_generation/flag_fix_node.py:_rerepair_meta": {"s1_flag_fix": None, "t2_flag_fix": None},
    "services/content_generation/grounding.py:repair": {"s1_flag_fix": None, "t2_flag_fix": None},
    "services/content_generation/itinerary_utils.py:nudge_itinerary_day": {"s1_itinerary_nudge": None,
                                                                            "t2_itinerary_nudge": None},
    # place dedupe: the primary and its check, both on dedupe.MAX_TOKENS
    "services/jobs/place_dedupe_job.py:_generate_sync": {"a3_place_dedupe": None, "a3_place_dedupe_check": None},
    # places.llm.make_generate(pool, stage, max_tokens): the extract job (days) and the geocode job (locate)
    "services/places/llm.py:_generate_sync": {"a3_place_days": _const_of("services.places.days", "MAX_TOKENS"),
                                              "a3_place_locate": _const_of("services.places.locate", "MAX_TOKENS")},
    # a3_atomize: the gateway retries a cut read (AA-1053); a3 stays out of the floor (token_floor.NO_FLOOR_STAGES)
    "services/acp_contract/a3_atomize.py:_ask_atoms": {"a3_atomize": _const_of("services.acp_contract.a3_atomize",
                                                                                "_MAX_TOKENS")},
}

EXEMPT_SITES = {
    "api/services/llm_connections.py:test_connection": "connection probe (adhoc_conn_test), not a routed stage",
    "api/services/llm_route_trace.py:_test_one": "admin route test: 'reply ok' on any stage's route",
    "api/services/llm_route_chains.py:resolve_stage": "route listing: the request is planned, never sent",
    "services/acp_contract/segment_research.py:_research_place": "model_tier haiku (Claude 4.5, no stage, no "
                                                                 "reasoning)",
    "services/acp_contract/segment_research_batch.py:_propose": "model_tier haiku (Claude 4.5, no stage, no "
                                                                "reasoning)",
}

# Routed stages with no call site in this repo.
NO_CALL_SITE = {
    "tp_compose": "TripPlanner's stage: the call site is apps/AA-TripPlanner-Web",
    "segment_research": "route row kept for the admin page; the CIS calls send model_tier haiku with no stage",
    "segment_research_batch": "route row kept for the admin page; the CIS calls send model_tier haiku with no stage",
    "t10_claims": "F9 v2 claim extraction, not wired into the gates until its replay passes (role_config.py); only "
                  "the contract smoke sends it",
}


def _sites() -> dict:
    """site key -> {stage: lowest ceiling}, the declarations applied, the exempt sites left out."""
    out: dict = {}
    for s in _scan():
        if s.key in EXEMPT_SITES:
            continue
        declared = DECLARED_SITES.get(s.key)
        if declared is not None:
            for stage, read in declared.items():
                ceiling = read() if read is not None else s.ceiling
                if ceiling is None:
                    out.setdefault(s.key, {})[stage] = None
                    continue
                prev = out.setdefault(s.key, {}).get(stage)
                out[s.key][stage] = ceiling if prev is None else min(prev, ceiling)
            continue
        for stage in s.stages or (None,):
            prev = out.setdefault(s.key, {}).get(stage)
            out[s.key][stage] = s.ceiling if prev is None or s.ceiling is None else min(prev, s.ceiling)
    return out


def test_every_call_site_is_read_declared_or_exempt():
    sites = _sites()
    unread = sorted(f"{k} (stage)" for k, v in sites.items() if None in v) + \
        sorted(f"{k} ({st})" for k, v in sites.items() for st, c in v.items() if st is not None and c is None)
    assert not unread, ("a call site whose stage or ceiling the scan cannot read: declare it in DECLARED_SITES (its "
                        f"stages, its lowest ceiling) or exempt it in EXEMPT_SITES with the reason: {unread}")
    keys = {s.key for s in _scan()}
    stale = sorted((set(DECLARED_SITES) | set(EXEMPT_SITES)) - keys)
    assert not stale, f"no such call site any more: remove from DECLARED_SITES / EXEMPT_SITES: {stale}"


def test_every_routed_stage_has_a_call_site():
    covered = {st for v in _sites().values() for st in v if st is not None}
    routes = _routes()
    missing = sorted(set(routes) - covered - set(NO_CALL_SITE))
    assert not missing, ("a routed stage with no call site the lint reads: declare the site that sends it, or add "
                         f"the stage to NO_CALL_SITE with the reason: {missing}")
    stale = sorted(st for st in NO_CALL_SITE if st in covered or st not in routes)
    assert not stale, f"remove from NO_CALL_SITE (covered now, or no longer routed): {stale}"


# ── 4. the floor ────────────────────────────────────────────────────────────────────────────────────────────────────

# "<stage>@<site>" -> why the site's lowest ceiling stays under token_floor(model, effort) of a routed model. The
# gateway still sends a reasoning model at least tokens.REASONING_FLOOR (4096) or the catalog floor. Shrink only:
# raise the constant (a ceiling is head-room; a complete reply costs the same) and delete the line.
UNDER_EFFORT_FLOOR = {
    "a0_column_map@shared/llm_client/column_mapper.py:detect_column_mapping":
        "column mapping (one small JSON object); 500 bounds the non-reasoning fallback",
    "a3_atomize@services/acp_contract/a3_atomize.py:_ask_atoms":
        "first ceiling kept (token_floor.NO_FLOOR_STAGES); the gateway retries a cut read at 8192 (AA-1053)",
    "a3_place_card@services/acp_shared/place_cards.py:draft_card": "place card draft (a few short fields)",
    "a3_place_card_check@services/acp_shared/place_cards.py:check_card": "place card check verdict",
    "a3_place_days@services/places/llm.py:_generate_sync": "place day split (extraction JSON)",
    "a3_place_dedupe@services/jobs/place_dedupe_job.py:_generate_sync": "place dedupe yes/no verdict",
    "a3_place_dedupe_check@services/jobs/place_dedupe_job.py:_generate_sync": "place dedupe yes/no verdict",
    "a3_place_locate@services/places/llm.py:_generate_sync": "place locate (extraction JSON)",
    "s1_brand_audit@services/content_generation/brand_audit_node.py:brand_audit_node":
        "no max_tokens (LLMRequest default); a stage_model_params row may set it",
    "t2_brand_audit@services/content_generation/brand_audit_node.py:brand_audit_node":
        "no max_tokens (LLMRequest default); a stage_model_params row may set it",
    "s1_flag_fix@services/content_generation/flag_fix_node.py:flag_fix_node": "no max_tokens (LLMRequest default)",
    "t2_flag_fix@services/content_generation/flag_fix_node.py:flag_fix_node": "no max_tokens (LLMRequest default)",
    "s1_flag_fix@services/content_generation/flag_fix_node.py:_rerepair_meta": "no max_tokens (one SEO meta line)",
    "t2_flag_fix@services/content_generation/flag_fix_node.py:_rerepair_meta": "no max_tokens (one SEO meta line)",
    "s1_flag_fix@services/content_generation/grounding.py:repair": "grounding repair, 4096",
    "t2_flag_fix@services/content_generation/grounding.py:repair": "grounding repair, 4096",
    "s1_generate@services/content_generation/s1_writer_v2_live.py:_call": "the meta call (META_MAX_TOKENS); the draft "
                                                                          "sends WRITER_MAX_TOKENS 16000",
    "t2_generate@services/content_generation/s1_writer_v2_live.py:_call": "the meta call (META_MAX_TOKENS); the draft "
                                                                          "sends WRITER_MAX_TOKENS 16000",
    "s1_itinerary_nudge@services/content_generation/itinerary_utils.py:nudge_itinerary_day":
        "no max_tokens (one itinerary day)",
    "t2_itinerary_nudge@services/content_generation/itinerary_utils.py:nudge_itinerary_day":
        "no max_tokens (one itinerary day)",
    "s1_judge@services/content_generation/brand_fit.py:_judge": "no max_tokens (the judge client leaves it unset)",
    "t2_judge@services/content_generation/brand_fit.py:_judge": "no max_tokens (the judge client leaves it unset)",
    "t7_debate_judge@services/content_generation/brand_fit.py:_judge":
        "no max_tokens (the judge client leaves it unset)",
    "s1_source_facts@services/content_generation/source_facts.py:extract_day_facts": "day facts extraction, 4096",
    "supply_content_writer@services/supply_content/writer.py:draft_content": "supplier content writer, 2048",
    "writing_memory_mine@services/writing_memory/drafter.py:_generate_sync": "rule drafter, 4096",
    "writing_memory_mine@services/writing_memory/preference.py:_generate_sync": "preference miner, 4096",
}


def _under_floor() -> dict:
    routes, efforts, mins = _routes(), _efforts(), _min_output_tokens()
    low: dict = {}
    for site, stages in _sites().items():
        for stage, ceiling in stages.items():
            if stage not in routes or ceiling is None:
                continue                      # not routed (adhoc / model_tier) or reported by the test above
            for model in routes[stage]:
                caps = {"min_output_tokens": mins[model]} if model in mins else None
                floor = token_floor.effort_floor(model, efforts.get((stage, model)), caps)
                if floor is not None and ceiling < floor:
                    low.setdefault(f"{stage}@{site}", []).append(
                        f"{ceiling} < {floor} ({model} at {efforts.get((stage, model)) or 'default'})")
    return low


def test_every_call_site_ceiling_clears_the_effort_floor_of_its_routed_models():
    low = _under_floor()
    new = sorted(f"{k}: {'; '.join(v)}" for k, v in low.items() if k not in UNDER_EFFORT_FLOOR)
    assert not new, ("call-site ceiling under token_floor(model, effort): raise it, or add it to UNDER_EFFORT_FLOOR "
                     f"with the reason: {new}")
    stale = sorted(set(UNDER_EFFORT_FLOOR) - set(low))
    assert not stale, f"clears the floor now: remove from UNDER_EFFORT_FLOOR: {stale}"


def test_the_two_stages_the_review_named_clear_the_floor():
    """t9_faq_answer_fit (Sonnet 5.5 at effort low: 4096) and t10_adjudicate (GPT-5.6 Luna has no catalog floor:
    8192 at its default effort) were raised in S271 W0, not allowlisted."""
    from services.acp_content_writing import faq_answer_fit
    from services.acp_content_writing.seed_align import adjudicate
    low = _under_floor()
    assert not [k for k in low if k.startswith(("t9_faq_answer_fit@", "t10_adjudicate@"))]
    assert faq_answer_fit.MAX_TOKENS >= 4096 and faq_answer_fit.ISOLATED_MAX_TOKENS >= 4096
    assert adjudicate.MAX_TOKENS >= 8192
