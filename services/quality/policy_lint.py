"""AA-1047 (root ADR 0020, layer L1) — the writing-policy lint: no hand restatement of a policy value.

Run inside `python -m services.quality.lint` (the CI Lint job); `run_policy(root, tracked, res)` adds its findings to
the same Result. Standard library + PyYAML only (it parses code with `ast`, it never imports an app module).

Errors (each names the file and line; the only allowlist is the generated / source files of the policy itself:
services/quality/writing_policy.{yaml,py,compiled.json}, this file, and frontend/lib/quality-registry.generated.ts):

  (p1) a band number in a string: a range like "140-155" / "130 to 145" whose two numbers are policy band or aim
       bounds, or a single band bound (meta, title, slug) next to "char", "meta", "title" or "slug" — render it with
       `writing_policy.fill("<<meta.min>>-<<meta.max>>")` or read `writing_policy.band(...)`.
  (p2) a comparison of a meta / title / slug / description length with a band-bound literal (`120 <= ml <= 155`),
       a slice of one cut at a band bound (`meta_description[:155]`), or a call that passes a band bound as an
       argument with a meta / title / slug operand or function name (`shorten(meta, 155)`, `trim(title, width=60)`).
  (p3) a constant named like META/TITLE/SLUG/DESC + MIN/MAX/TARGET/BAND/AIM/CHARS/LIMIT (any case) assigned a
       band-bound literal; a dict keyed 'meta' / 'title' / 'slug' holding one; a band default on a limit parameter of
       a meta / title / slug function; in YAML a min / max key with a band value under a meta / title / slug key.
  (p4) a copy of a compiled policy text (a fragment, a label-rubric line): a string sharing an 8-word run with it.
  (p5) a retired wording (`retired_wordings` in the YAML) back in a string.
  (p6) the compiled file is stale; a class severity in taxonomy.yaml differs from the policy's; a route row names a
       gate the registry does not know.
  (p7) TS (frontend/, portal/): a meta / title / slug length compared with a band-bound literal, a band-named constant
       with a band literal, a maxLength / minLength attribute with one on a meta / title / slug line, or a field-keyed
       object literal with one — read it at runtime (S279 L2: useQualityThresholds, lib/qualityThresholds.ts).

Not covered (S272 review 2, recorded in the AA-1047 notes): a band bound built into a string by concatenation
(`"..." + str(155)`), and a bare band literal with no field word in its statement.

Docstrings and comments are not code and are not scanned. Tests, docs and migrations are out of scope.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path


from services.quality import writing_policy as wp

PY_SCOPE = ("api/", "services/", "shared/", "worker/", "scripts/")
PY_EXEMPT = ("api/migrations/", "tests/", "docs/")
TS_SCOPE = ("frontend/", "portal/")
ALLOWLIST = frozenset({
    "services/quality/writing_policy.yaml", "services/quality/writing_policy.py",
    "services/quality/writing_policy.compiled.json", "services/quality/policy_lint.py",
    "frontend/lib/quality-registry.generated.ts",
})
YAML_SCOPE = ("services/",)
RANGE_RE = re.compile(r"(?<![\d.])(\d{2,3})\s*(?:-|–|—|to)\s*(\d{2,3})(?![\d.])")
NUM_RE = re.compile(r"(?<![\d.:/])(\d{2,3})(?![\d.:%])")
CONTEXT_RE = re.compile(r"char|meta|title|slug", re.I)
FIELD_RE = re.compile(r"meta|title|slug|seo", re.I)          # a single bound needs a field word nearby
LEN_OPERAND_RE = re.compile(r"meta|title|slug|seo|desc|\bml\b|\btl\b", re.I)
# S272 review 2: any case ('meta_max = 155' as well as 'SEO_META_MAX = 155')
CONST_NAME_RE = re.compile(r"(?:META|TITLE|SLUG|DESC)\w*(?:MIN|MAX|TARGET|BAND|AIM|CHARS|LIMIT|LEN)"
                           r"|(?:MIN|MAX|LIMIT)\w*(?:META|TITLE|SLUG|DESC)", re.I)
# a call operand / function name about a field: the word starts an identifier part ('clip_meta', "page['seo_title']",
# 'metaDescription'), never inside another word ('DATAFORSEO_BASE')
CALL_FIELD_RE = re.compile(r"(?<![a-z])(?:meta|title|slug|seo|desc)|\bml\b|\btl\b", re.I)
LIMIT_PARAM_RE = re.compile(r"limit|max|min|cap|len|chars|size|band|width", re.I)
SHINGLE = 8
TS_CMP_RE = re.compile(r"\b[\w.$?]*(?:meta|title|slug)[\w.$?]*(?:\.length)?\s*(?:>=|<=|<|>)\s*(\d{2,3})\b"
                       r"|\b(\d{2,3})\s*(?:>=|<=|<|>)\s*[\w.$?]*(?:meta|title|slug)", re.I)
TS_CONST_RE = re.compile(r"\b(?:export\s+)?const\s+SEO_(?:META|TITLE)_(?:MIN|MAX)\s*=\s*\d")
# S272 review 2: any band-named constant ('META_MAX = 155'), a field's maxLength / minLength attribute and a field-keyed
# object literal ('{ meta: [140, 155] }') with a band bound
TS_NAMED_CONST_RE = re.compile(r"\bconst\s+\w*(?:meta|title|slug|desc)\w*(?:min|max|limit|len)\w*\s*=\s*(\d{2,3})\b"
                               r"|\bconst\s+\w*(?:min|max|limit)\w*(?:meta|title|slug|desc)\w*\s*=\s*(\d{2,3})\b", re.I)
TS_ATTR_RE = re.compile(r"\b(?:maxLength|minLength|max|min)\s*=\s*\{?\s*['\"]?(\d{2,3})\b")
TS_KEYED_RE = re.compile(r"\b(?:meta|title|slug)\w*['\"]?\s*:\s*[\[(]?\s*(\d{2,3})(?:\s*,\s*(\d{2,3}))?", re.I)


def bounds(c: dict) -> tuple[frozenset, frozenset]:
    """(every band and aim bound, the band bounds only) of a compiled policy."""
    bands = c["bands"]
    band_nums = {bands["meta"]["min"], bands["meta"]["max"], bands["title"]["min"], bands["title"]["max"],
                 bands["slug"]["max"]}
    aim_nums = set()
    for by_writer in (v for k, v in c["writer_aims"].items() if k != "policy_sha"):
        for aim in by_writer.values():
            aim_nums |= {aim["min"], aim["max"]}
    return frozenset(band_nums | aim_nums), frozenset(band_nums)


def _norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower().replace("’", "'"))


def shingles(text: str, n: int = SHINGLE) -> set[tuple[str, ...]]:
    w = _norm_words(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def policy_texts(c: dict) -> dict[str, str]:
    """{name: text} of the compiled texts a consumer must never copy by hand."""
    out = {f"fragment {k}": v for k, v in c["fragments"].items() if k != "policy_sha"}
    r = c["label_rubric"]
    out["label_rubric pass"] = r["pass"]
    out["label_rubric fail"] = r["fail"]
    out.update({f"label_rubric rule {i + 1}": t for i, t in enumerate(r["rules"])})
    return out


def string_findings(value: str, all_bounds: frozenset, band_bounds: frozenset,
                    texts: dict[str, set], retired: list[str]) -> list[str]:
    """(p1)(p4)(p5) findings of one string literal (pure; the tests call it directly)."""
    out: list[str] = []
    low = value.lower()
    for m in RANGE_RE.finditer(value):
        a, b = int(m.group(1)), int(m.group(2))
        if a in all_bounds and b in all_bounds and CONTEXT_RE.search(value):
            out.append(f"(p1) band range {m.group(0)!r} restated — render it from writing_policy.yaml")
    if not out:
        for m in NUM_RE.finditer(value):
            if int(m.group(1)) in band_bounds:
                window = value[max(0, m.start() - 40):m.end() + 40]
                if FIELD_RE.search(window) and not RANGE_RE.search(window):
                    out.append(f"(p1) band bound {m.group(1)} restated near {window.strip()[:60]!r}")
                    break
    sh = shingles(value)
    if sh:
        for name, ts in texts.items():
            if sh & ts:
                out.append(f"(p4) copies the policy's {name} — use writing_policy (fragment / label_rubric)")
    for pat in retired:
        if pat.lower() in low:
            out.append(f"(p5) retired wording {pat!r} — the policy changed it (writing_policy.yaml retired_wordings)")
    return out


def _doc_spans(tree: ast.AST) -> list[tuple[int, int]]:
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _int_in(node: ast.AST, nums: frozenset) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)
            and node.value in nums)


def _band_call_arg(node: ast.Call, src: str, band_b: frozenset) -> tuple[int, str] | None:
    """(the literal, the call) when a call passes a band bound as an argument (positional or keyword) and the call
    is about a meta / title / slug: its function name, a keyword name or another argument (not a literal or an
    f-string) names one (CALL_FIELD_RE).
    None otherwise (`rows[:60]`-like numbers in unrelated calls, `fetch(url, 60)`, stay quiet)."""
    kw_vals = [k.value for k in node.keywords]
    lits = [a for a in list(node.args) + kw_vals if _int_in(a, band_b)]
    if not lits:
        return None
    func = ast.get_source_segment(src, node.func) or ""
    others = [ast.get_source_segment(src, a) or "" for a in list(node.args) + kw_vals
              if a not in lits and not isinstance(a, (ast.Constant, ast.JoinedStr))]
    names = [k.arg or "" for k in node.keywords if k.value in lits]
    if CALL_FIELD_RE.search(func) or any(CALL_FIELD_RE.search(o) for o in others) \
            or any(CALL_FIELD_RE.search(n) for n in names):
        return lits[0].value, ast.get_source_segment(src, node) or func
    return None


def scan_python(path: str, src: str, c: dict) -> list[str]:
    """Every (p1)–(p5) finding of one Python file as 'path:line: message'."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    all_b, band_b = bounds(c)
    texts = {k: shingles(v) for k, v in policy_texts(c).items()}
    retired = list(c.get("retired_wordings") or [])
    spans = _doc_spans(tree)
    out: list[str] = []
    seen: set[tuple[int, str]] = set()

    def add(node, msg):
        key = (node.lineno, msg)
        if key not in seen:
            seen.add(key)
            out.append(f"{path}:{node.lineno}: {msg}")

    in_fstring = {id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for v in node.values}
    for node in ast.walk(tree):
        if not hasattr(node, "lineno") or any(a <= node.lineno and (node.end_lineno or node.lineno) <= b
                                              for a, b in spans):
            continue
        if isinstance(node, ast.JoinedStr):
            # an f-string is read whole: its literal parts joined, each {expression} as one placeholder word
            text = "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " {x} "
                           for v in node.values)
            for msg in string_findings(text, all_b, band_b, texts, retired):
                add(node, msg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in in_fstring:
            for msg in string_findings(node.value, all_b, band_b, texts, retired):
                add(node, msg)
        elif isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            lits = [o for o in operands if _int_in(o, all_b)]
            if not lits:
                continue  # AA-1084: get_source_segment splits the whole file per call; only a band literal needs it
            others = " ".join(ast.get_source_segment(src, o) or "" for o in operands if o not in lits)
            if LEN_OPERAND_RE.search(others):
                add(node, f"(p2) {others.strip()[:50]} compared with {lits[0].value} — use writing_policy.band(...)")
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice) \
                and _int_in(node.slice.upper, band_b) \
                and LEN_OPERAND_RE.search(ast.get_source_segment(src, node.value) or ""):
            # S272 review 2: `meta_description[:155]` is how code trims to a band
            add(node, f"(p2) {ast.get_source_segment(src, node.value)[:50]} cut at {node.slice.upper.value} — use "
                      "writing_policy.band(...)")
        elif isinstance(node, ast.Dict):
            # S272 review 2: `{"meta": (140, 155), "title": (45, 60)}`
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and FIELD_RE.search(k.value) and (
                        _int_in(v, band_b) or (isinstance(v, (ast.Tuple, ast.List))
                                               and any(_int_in(e, band_b) for e in v.elts))):
                    add(node, f"(p3) {k.value!r} keyed to a band literal — read it from writing_policy")
                    break
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # S272 review 2: `def fit(meta, limit=155)` — a band default on a limit parameter of a meta / title / slug
            # function
            args = node.args
            params = args.posonlyargs + args.args
            pairs = list(zip(params[len(params) - len(args.defaults):], args.defaults))
            pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
            field = LEN_OPERAND_RE.search(node.name) or any(LEN_OPERAND_RE.search(a.arg) for a in params)
            for a, d in pairs:
                if _int_in(d, band_b) and (LEN_OPERAND_RE.search(a.arg) or (field and LIMIT_PARAM_RE.search(a.arg))):
                    add(d, f"(p3) {node.name}({a.arg}={d.value}) — a band default; read it from writing_policy")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" \
                and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str) \
                and FIELD_RE.search(node.func.value.value) and any(_int_in(a, band_b) for a in node.args):
            add(node, "(p1) a band bound formatted into a meta / title / slug string — render it from "
                      "writing_policy.yaml")
        elif isinstance(node, ast.Call) and _band_call_arg(node, src, band_b) is not None:
            # S272 review 5: `shorten(meta, 155)` / `trim(title, width=60)` passes the band as an argument
            lit, what = _band_call_arg(node, src, band_b)
            add(node, f"(p2) {what[:50]} called with the band literal {lit} — read it from writing_policy.band(...)")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            names += [e.id for t in targets if isinstance(t, ast.Tuple) for e in t.elts if isinstance(e, ast.Name)]
            value = node.value
            vals = value.elts if isinstance(value, ast.Tuple) else [value] if value is not None else []
            if any(CONST_NAME_RE.search(n) for n in names) and any(_int_in(v, all_b) for v in vals):
                add(node, f"(p3) {', '.join(names)} = a band literal — read it from writing_policy")
    return out


def scan_ts(path: str, src: str, c: dict) -> list[str]:
    all_b, band_b = bounds(c)
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        code = line.split("//", 1)[0]
        if TS_CONST_RE.search(code):
            out.append(f"{path}:{i}: (p7) SEO band constant with a literal — read it at runtime "
                       "(useQualityThresholds, lib/qualityThresholds.ts)")
            continue
        named = TS_NAMED_CONST_RE.search(code)
        attr = TS_ATTR_RE.search(code) if re.search(r"meta|title|slug|desc", code, re.I) else None
        keyed = [int(x) for m in TS_KEYED_RE.finditer(code) for x in m.groups() if x]
        if (named and int(named.group(1) or named.group(2)) in band_b) or (attr and int(attr.group(1)) in band_b) \
                or any(n in band_b for n in keyed):
            out.append(f"{path}:{i}: (p7) a meta / title / slug band literal — read it at runtime "
                       "(useQualityThresholds, lib/qualityThresholds.ts)")
            continue
        for m in TS_CMP_RE.finditer(code):
            n = int(m.group(1) or m.group(2))
            if n in all_b:
                out.append(f"{path}:{i}: (p7) a meta / title / slug length compared with {n} — read the band at "
                           "runtime (useQualityThresholds, lib/qualityThresholds.ts)")
                break
    return out


def scan_yaml(path: str, src: str, c: dict) -> list[str]:
    all_b, band_b = bounds(c)
    out = []
    # S272 review 2: a min / max key with a band value under a meta / title / slug parent ('meta:\n  max: 155')
    parents: list[tuple[int, str]] = []
    for i, line in enumerate(src.splitlines(), 1):
        body = line.split("#", 1)[0]
        for m in RANGE_RE.finditer(body):
            if int(m.group(1)) in all_b and int(m.group(2)) in all_b and CONTEXT_RE.search(body):
                out.append(f"{path}:{i}: (p1) band range {m.group(0)!r} restated in config")
        km = re.match(r"^(\s*)-?\s*['\"]?([\w-]+)['\"]?\s*:\s*(.*)$", body)
        if not km:
            continue
        indent, key, rest = len(km.group(1)), km.group(2), km.group(3).strip()
        while parents and parents[-1][0] >= indent:
            parents.pop()
        under_field = any(FIELD_RE.search(k) for _, k in parents)
        if re.fullmatch(r"(?:min|max|limit|target)\w*", key, re.I) and under_field \
                and re.fullmatch(r"\d{2,3}", rest) and int(rest) in band_b:
            out.append(f"{path}:{i}: (p3) {key}: {rest} under a meta / title / slug key — read it from the policy")
        flow = re.findall(r"\b(?:min|max)\s*:\s*(\d{2,3})\b", rest)
        if flow and (FIELD_RE.search(key) or under_field) and any(int(x) in band_b for x in flow):
            out.append(f"{path}:{i}: (p3) {key}: {rest[:40]} restates a band — read it from the policy")
        if not rest:
            parents.append((indent, key))
    return out


def structural_errors(root: Path) -> list[str]:
    """(p6): compiled file fresh, taxonomy severities agree, route rows name registered gates."""
    from services.quality import registry
    out: list[str] = []
    try:
        if wp.stale():
            out.append(f"{wp.COMPILED_REL} is stale: run python -m services.quality.writing_policy --write")
    except wp.PolicyError as e:
        out.append(f"{wp.POLICY_REL}: {e}")
        return out
    c = json.loads(wp.render_compiled())
    classes = registry.classes()
    for cid, sev in c["severities"].items():
        if cid == "policy_sha":
            continue
        have = (classes.get(cid) or {}).get("severity")
        if have is None:
            out.append(f"(p6) writing_policy severities.{cid}: no such taxonomy class")
        elif have != sev:
            out.append(f"(p6) taxonomy.yaml {cid} severity {have!r} != writing_policy {sev!r}")
    known = set(registry.GATE_CONSTANTS.values())
    for r in c["route_rows"]["gates"]:
        if r["gate"] not in known:
            out.append(f"(p6) writing_policy route_rows: gate {r['gate']!r} is not a registry gate")
    for cid in c["eval"]["judge_classes"]:
        if cid not in classes:
            out.append(f"(p6) writing_policy eval.judge_classes: {cid!r} is not a taxonomy class")
    return out


def run_policy(root: Path, tracked: list[str]) -> tuple[list[str], dict]:
    """(errors, scanned counts) over the tracked files under `root`."""
    errors = structural_errors(root)
    try:
        c = json.loads(wp.render_compiled())
    except wp.PolicyError:
        return errors, {}
    n = {"python": 0, "ts": 0, "yaml": 0}
    for path in tracked:
        if path in ALLOWLIST or path.startswith(PY_EXEMPT):
            continue
        full = root / path
        if path.endswith(".py") and path.startswith(PY_SCOPE):
            n["python"] += 1
            try:
                errors += scan_python(path, full.read_text(encoding="utf-8"), c)
            except (OSError, UnicodeDecodeError):
                continue
        elif path.endswith((".ts", ".tsx")) and path.startswith(TS_SCOPE) and "/node_modules/" not in path \
                and not path.endswith(".d.ts") and not re.search(r"(\.test\.|\.spec\.|/e2e/|/__tests__/)", path):
            n["ts"] += 1
            try:
                errors += scan_ts(path, full.read_text(encoding="utf-8"), c)
            except (OSError, UnicodeDecodeError):
                continue
        elif path.endswith((".yaml", ".yml")) and path.startswith(YAML_SCOPE):
            n["yaml"] += 1
            try:
                errors += scan_yaml(path, full.read_text(encoding="utf-8"), c)
            except (OSError, UnicodeDecodeError):
                continue
    return errors, n


__all__ = ["ALLOWLIST", "bounds", "policy_texts", "run_policy", "scan_python", "scan_ts", "scan_yaml", "shingles",
           "string_findings", "structural_errors"]
