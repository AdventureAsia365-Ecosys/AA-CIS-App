"""Quality System v1 lint (AA-926, design §3.4): `python -m services.quality.lint [--report]`.

Exit 1 when any error is found. `--report` prints a Markdown report (the CI Lint job appends it to the step
summary). Needs only the standard library + PyYAML; it parses code with `ast`, it never imports app modules.

Errors
  (a) a gate-name literal outside services/quality/ (pattern `F<n>_<lower>…`, plus the exact registry names
      marked lint_exact) — use the registry constant; an unregistered name must be registered first.
  (b) a 7.0 pass-line literal (Python: a 7.0 float, a comparison of a score/quality operand with 7, a string
      with `>= 7` / `7.0` / `'<7'` / `'7-8'`; TS: a comparison of a score operand with 7, or `7.0` text) —
      use services/quality/thresholds.yaml (Python) or frontend/lib/quality-registry.generated.ts (TS).
  (c) a Jev question key (code constant `*_Q` / `*QUESTION*`, or a key seeded by
      `INSERT INTO shared.decision_question (question_key …)` in a migration) not in the registry.
  (d) an A1 validate / brand-audit / grounding code not mapped in `a1_codes`.
  (e) an admin chip not mapped (defect classes — set and order frozen —, duplicate reasons, review kinds).
  (f) a paths.yaml glob that matches no tracked file.
  (g) a hard/block class without a labelled positive and negative synthetic example.
  (h) a retired gate missing from the registry, or without its label.
  plus schema errors in the three YAML files and a stale generated TS file.
Warnings: coverage parity (§3.4 #4), hard/block classes with no enforcing detector, selector paths with no
Q0 keep/drop set yet (§3.4 #6; becomes an error once the Q-2 corpus exists), unused Jev keys read by code.

Exempt from (a)/(b): tests, docs, migrations, services/quality/, docstrings and comments, `__all__` lists,
the generated TS file, and a line carrying `# qlint: allow <reason>` (counted in the report).
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from services.quality import registry

ROOT = Path(__file__).resolve().parents[2]
PY_SCOPE = ("api/", "services/", "shared/", "worker/", "scripts/aa729/")
TS_SCOPE = ("frontend/", "portal/")
PY_EXEMPT = ("services/quality/", "api/migrations/", "tests/", "docs/")
GENERATED_TS = "frontend/lib/quality-registry.generated.ts"
RETIRED_REQUIRED = ("promises_an_option", "F8_framework", "F3_structural_variance", "F5_atom_density", "F7_faq_dedup")
ALLOW_RE = re.compile(r"#\s*qlint:\s*allow\b\s*(.*)")
TS_ALLOW_RE = re.compile(r"//\s*qlint:\s*allow\b\s*(.*)")
GATE_RE = re.compile(r"(?<![A-Za-z0-9_])F\d+_[a-z][a-z0-9_]*")
PASS_STR_RE = re.compile(r"(>=|<=|<|>)\s*7(\.0)?(?![\d.])|(?<![\d.])7\.0(?![\d.])|'<7'|'7-8'")
SCORE_CTX_RE = re.compile(r"score|quality", re.I)
TS_CMP_RE = re.compile(r"([A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*)\s*(>=|<=|<|>)\s*7(?:\.0)?(?![\d.\w])")
TS_SEVEN_RE = re.compile(r"(?<![\d.\w])7\.0(?![\d.\w])")
JEV_NAME_RE = re.compile(r"(^|_)Q$|QUESTION")
JEV_KEY_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9_]+$")
A1_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
SEED_RE = re.compile(r"INSERT\s+INTO\s+shared\.decision_question\s*\(\s*question_key\b(.*?)(?:ON\s+CONFLICT|\Z)",
                     re.S | re.I)
SEED_KEY_RE = re.compile(r"^\s*\(\s*'([a-z0-9_]+)'\s*,", re.M)
RUN_ID_RE = re.compile(r"^qcal-\d{8}-[a-z0-9-]+$")


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    allows: list[str] = field(default_factory=list)
    scanned: dict = field(default_factory=dict)


# -------------------------------------------------------------------------------------------- files

def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        files = [f for f in out.splitlines() if f]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".venv", "__pycache__", ".next")]
        for f in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, f), ROOT).replace(os.sep, "/"))
    return found


def _py_in_scope(path: str) -> bool:
    return path.endswith(".py") and path.startswith(PY_SCOPE) and not path.startswith(PY_EXEMPT)


def _ts_in_scope(path: str) -> bool:
    return (path.endswith((".ts", ".tsx")) and path.startswith(TS_SCOPE) and "/node_modules/" not in path
            and path != GENERATED_TS and not path.endswith(".d.ts")
            and not re.search(r"(\.test\.|\.spec\.|/e2e/|/__tests__/)", path))


# ------------------------------------------------------------------------------------ python (a)(b)

def _exempt_spans(tree: ast.AST) -> list[tuple[int, int]]:
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            spans.append((node.lineno, node.end_lineno or node.lineno))
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _in_spans(node: ast.AST, spans: list[tuple[int, int]]) -> bool:
    return any(a <= node.lineno and (node.end_lineno or node.lineno) <= b for a, b in spans)


def _allowed(node: ast.AST, lines: list[str]) -> str | None:
    for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
        if 0 < ln <= len(lines):
            m = ALLOW_RE.search(lines[ln - 1])
            if m:
                return m.group(1).strip() or "(no reason)"
    return None


def _is_num(node: ast.AST, value: float) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool) and node.value == value)


def scan_python(path: str, src: str, res: Result, exact_names: set[str], registered: set[str]) -> None:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:  # pragma: no cover — flake8 catches this first
        res.errors.append(f"{path}:{e.lineno}: cannot parse ({e.msg})")
        return
    lines = src.splitlines()
    spans = _exempt_spans(tree)
    alt = "|".join(map(re.escape, sorted(exact_names)))
    exact_re = re.compile(r"(?<![A-Za-z0-9_])(" + alt + r")(?![A-Za-z0-9_])") if exact_names else None
    seen: set[tuple[int, str]] = set()

    def report(node, kind, msg):
        key = (node.lineno, msg)
        if key in seen:
            return
        seen.add(key)
        why = _allowed(node, lines)
        if why is not None:
            res.allows.append(f"{path}:{node.lineno}: {kind} — {why}")
        else:
            res.errors.append(f"{path}:{node.lineno}: {msg}")

    for node in ast.walk(tree):
        if _in_spans(node, spans) if hasattr(node, "lineno") else False:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = set(GATE_RE.findall(node.value))
            if exact_re:
                names |= set(exact_re.findall(node.value))
            for n in sorted(names):
                hint = "use the services.quality.registry constant" if n in registered else \
                    "unregistered gate name: add it to services/quality/taxonomy.yaml, then use a registry constant"
                report(node, "gate literal", f"(a) gate-name literal '{n}' — {hint}")
            if PASS_STR_RE.search(node.value):
                report(node, "pass-line literal",
                       f"(b) pass-line literal in a string {node.value[:60]!r} — read it from thresholds.yaml")
        elif _is_num(node, 7.0) and isinstance(node.value, float):
            report(node, "pass-line literal", "(b) 7.0 literal — use services.quality.registry.threshold(...)")
        elif isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            if any(_is_num(o, 7) for o in operands):
                others = " ".join(ast.get_source_segment(src, o) or "" for o in operands if not _is_num(o, 7))
                if SCORE_CTX_RE.search(others):
                    report(node, "pass-line literal",
                           f"(b) score compared with 7 ({others.strip()[:50]}) — use a thresholds.yaml key")


# ---------------------------------------------------------------------------------------- TS (b)

def _strip_ts_comments(src: str) -> str:
    out, i, n = [], 0, len(src)
    quote = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
            out.append(c)
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            seg = src[i:(n if j < 0 else j + 2)]
            out.append("\n" * seg.count("\n"))
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def scan_ts(path: str, src: str, res: Result) -> None:
    raw_lines = src.splitlines()
    code = _strip_ts_comments(src)
    for ln, line in enumerate(code.splitlines(), 1):
        hits = []
        for m in TS_CMP_RE.finditer(line):
            operand = m.group(1)
            if re.search(r"score", operand, re.I) or operand == "s":
                hits.append(f"score compared with 7 ({m.group(0).strip()})")
        if TS_SEVEN_RE.search(line):
            hits.append("'7.0' text")
        for h in hits:
            raw = raw_lines[ln - 1] if ln <= len(raw_lines) else ""
            m = TS_ALLOW_RE.search(raw)
            if m:
                res.allows.append(f"{path}:{ln}: pass-line literal — {m.group(1).strip() or '(no reason)'}")
            else:
                res.errors.append(f"{path}:{ln}: (b) {h} — use PASS_LINE / SCORE_BANDS from {GENERATED_TS}")


# -------------------------------------------------------------------------------------- (c) Jev keys

def jev_code_keys(files: dict[str, str]) -> dict[str, str]:
    """key → 'path:line' for every Jev question constant (NAME_Q / *QUESTION* = 'a1_x')."""
    out = {}
    for path, src in files.items():
        if "Q" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                v = node.value
                if JEV_NAME_RE.search(name) and isinstance(v, ast.Constant) and isinstance(v.value, str) \
                        and JEV_KEY_RE.match(v.value):
                    out.setdefault(v.value, f"{path}:{node.lineno}")
    return out


def jev_seed_keys(root: Path, tracked: list[str]) -> dict[str, str]:
    out = {}
    for path in sorted(p for p in tracked if p.startswith("api/migrations/") and p.endswith(".sql")):
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        for m in SEED_RE.finditer(text):
            for k in SEED_KEY_RE.findall(m.group(1)):
                out.setdefault(k, path)
    return out


# ------------------------------------------------------------------------------------- (d) A1 codes

def a1_code_sites(files: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}

    def add(code, path, line):
        if isinstance(code, str) and A1_CODE_RE.match(code):
            out.setdefault(code, f"{path}:{line}")

    for path, src in files.items():
        if not (path.startswith("services/content_generation/") or path == "api/routers/admin_pipeline.py"):
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {t.id for t in targets if isinstance(t, ast.Name)}
                code_maps = {"_FAILURE_MAP", "HARD_CODE_REPAIR", "STAGE2_FIX_MAPPING", "_CODE_FIELD_MAP"}
                if names & code_maps and isinstance(node.value, ast.Dict):
                    for k in node.value.keys:
                        if isinstance(k, ast.Constant):
                            add(k.value, path, k.lineno)
                if any(n.endswith("_CODES") for n in names) and node.value is not None:
                    for c in ast.walk(node.value):
                        if isinstance(c, ast.Constant):
                            add(c.value, path, c.lineno)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "append" and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id in ("codes", "fired") and path.startswith("services/content_generation/"):
                for a in node.args:
                    if isinstance(a, ast.Constant):
                        add(a.value, path, a.lineno)
            elif isinstance(node, ast.Dict) and path == "services/content_generation/grounding.py":
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "code" and isinstance(v, ast.Constant):
                        add(v.value, path, v.lineno)
    return out


# ---------------------------------------------------------------------------------------- (e) chips

def _ts_object_keys(src: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}[^=]*=\s*\{{(.*?)\n?\}};", src, re.S)
    if not m:
        return []
    return re.findall(r"(?:^|[,{\s])([A-Za-z_][A-Za-z0-9_]*)\s*:", _strip_ts_comments(m.group(1)))


def check_chips(files: dict[str, str], root: Path, res: Result) -> None:
    chips = registry.chips()
    src = files.get("api/routers/admin_content_quality.py")
    if src:
        tree = ast.parse(src)
        found = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEFECT_CLASSES"
                                                    for t in node.targets):
                if isinstance(node.value, ast.Tuple):
                    found = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
                elif isinstance(node.value, ast.Call):  # tuple(registry...) — read from the registry
                    found = list(chips.get("defect_classes") or {})
        if found is None:
            res.errors.append("(e) admin_content_quality.DEFECT_CLASSES not found")
        elif found != list(chips.get("defect_classes") or {}):
            res.errors.append(f"(e) DEFECT_CLASSES {found} != registry chips.defect_classes "
                              f"{list(chips.get('defect_classes') or {})} (set and order are frozen)")
    shared = root / "frontend/app/admin/content-quality/shared.ts"
    if shared.exists():
        ts = shared.read_text(encoding="utf-8")
        for const, group in (("DEFECT_NAMES", "defect_classes"), ("DUPLICATE_REASON_LABELS", "duplicate_reasons")):
            for k in _ts_object_keys(ts, const):
                if k not in (chips.get(group) or {}):
                    res.errors.append(f"(e) frontend {const}.{k} is not mapped in chips.{group}")
    ap = files.get("api/routers/admin_pipeline.py")
    if ap:
        for node in ast.walk(ast.parse(ap)):
            if isinstance(node, ast.FunctionDef) and node.name == "_review_block":
                for d in ast.walk(node):
                    if isinstance(d, ast.Dict):
                        for k, v in zip(d.keys, d.values):
                            if isinstance(k, ast.Constant) and k.value == "kind" and isinstance(v, ast.Constant) \
                                    and v.value not in (chips.get("review_block_kinds") or {}):
                                res.errors.append(f"(e) review kind {v.value!r} is not mapped in "
                                                  f"chips.review_block_kinds")


# -------------------------------------------------------------------------------------------- schema

def _mapped(entry: dict, where: str, class_ids: set[str], res: Result) -> None:
    if entry.get("class") == "none":
        if not entry.get("reason"):
            res.errors.append(f"{where}: class: none needs a reason")
        return
    cls = entry.get("classes") or []
    if not cls:
        res.errors.append(f"{where}: maps to no class (give classes: [...] or class: none + reason)")
    for c in cls:
        if c not in class_ids:
            res.errors.append(f"{where}: unknown class {c!r}")


def check_schema(res: Result, tracked: set[str]) -> None:
    tax = registry.taxonomy()
    if tax.get("version") != registry.TAXONOMY_VERSION:
        res.errors.append(f"taxonomy.yaml version {tax.get('version')!r} != registry.TAXONOMY_VERSION")
    sev, writers = set(tax.get("severities") or []), set(tax.get("writers") or [])
    kinds, modes = set(tax.get("detector_kinds") or []), set(tax.get("detector_modes") or [])
    ids = [c.get("id") for c in tax.get("classes") or []]
    class_ids = set(ids)
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        res.errors.append(f"taxonomy: duplicate class id {dup!r}")
    gates, jev, a1 = registry.gates(), registry.jev_questions(), registry.a1_codes()
    detector_refs = set(gates) | set(jev) | set(a1)
    for c in tax.get("classes") or []:
        cid = c.get("id")
        if not re.match(r"^[a-z]+\.[a-z0-9_]+$", cid or ""):
            res.errors.append(f"taxonomy: bad class id {cid!r}")
        for f in ("since", "severity", "checkability", "artefact", "definition"):
            if not c.get(f):
                res.errors.append(f"taxonomy {cid}: missing {f}")
        if c.get("severity") not in sev:
            res.errors.append(f"taxonomy {cid}: severity {c.get('severity')!r} not in {sorted(sev)}")
        if c.get("artefact") not in ("output", "selector"):
            res.errors.append(f"taxonomy {cid}: artefact must be output or selector")
        for w in c.get("writers") or []:
            if w not in writers:
                res.errors.append(f"taxonomy {cid}: unknown writer {w!r}")
        for d in c.get("detectors") or []:
            if d.get("ref") not in detector_refs:
                res.errors.append(f"taxonomy {cid}: detector {d.get('ref')!r} is not a registered gate, Jev key "
                                  f"or A1 code")
            if d.get("kind") not in kinds or d.get("mode") not in modes:
                res.errors.append(f"taxonomy {cid}: detector {d.get('ref')!r} kind/mode invalid")
            for w in d.get("writers") or []:
                if w not in writers:
                    res.errors.append(f"taxonomy {cid}: detector {d.get('ref')!r} unknown writer {w!r}")
        # (g) labelled examples for hard/block classes
        if c.get("severity") in ("hard", "block") and c.get("kind") != "meta":
            ex = c.get("synthetic_examples") or []
            truths = {e.get("truth") for e in ex if e.get("text")}
            if len(ex) < 2 or not {"positive", "negative"} <= truths:
                res.errors.append(f"(g) taxonomy {cid}: a {c['severity']} class needs >= 2 labelled synthetic "
                                  f"examples (one positive, one negative)")
    constants = set(registry.GATE_CONSTANTS.values())
    for n in sorted(set(gates) - constants):
        res.errors.append(f"registry: gate {n!r} is in taxonomy.yaml but has no constant in registry.py")
    for n in sorted(constants - set(gates)):
        res.errors.append(f"registry: constant {n!r} is not in taxonomy.yaml gates")
    for n, g in gates.items():
        if g.get("kind") not in ("gate", "flag", "telemetry"):
            res.errors.append(f"gate {n}: kind must be gate, flag or telemetry")
        if g.get("kind") == "gate" and not g.get("label"):
            res.errors.append(f"gate {n}: a gate needs a label")
        _mapped(g, f"gate {n}", class_ids, res)
    for n in RETIRED_REQUIRED:  # (h)
        g = gates.get(n)
        if not g or not g.get("retired") or not g.get("label"):
            res.errors.append(f"(h) retired gate {n!r} must stay in the registry with retired: true and its label")
    for k, q in jev.items():
        if q.get("status") not in ("used", "unused", "retired"):
            res.errors.append(f"jev {k}: status must be used, unused or retired")
        _mapped(q, f"jev {k}", class_ids, res)
    for k, a in a1.items():
        _mapped(a, f"a1 code {k}", class_ids, res)
    for group, entries in registry.chips().items():
        for k, e in (entries or {}).items():
            _mapped(e, f"chip {group}.{k}", class_ids, res)
    # thresholds
    th = registry.thresholds_doc().get("thresholds") or {}
    for k, e in th.items():
        if ("value" in e) == ("ref" in e):
            res.errors.append(f"threshold {k}: give exactly one of value / ref")
        try:
            registry.threshold(k)
        except (KeyError, ValueError) as err:
            res.errors.append(f"threshold {k}: {err}")
        for f in e.get("used_by") or []:
            if f not in tracked:
                res.errors.append(f"threshold {k}: used_by {f!r} is not a tracked file")
        cal = e.get("calibration")
        if not isinstance(cal, dict) or set(cal) != {"run_id", "precision", "recall", "n", "calibrated"}:
            res.errors.append(f"threshold {k}: calibration must be {{run_id, precision, recall, n, calibrated}}")
        elif cal["calibrated"] is not False and not (isinstance(cal["run_id"], str) and RUN_ID_RE.match(cal["run_id"])):
            res.errors.append(f"threshold {k}: a calibrated threshold needs run_id like qcal-YYYYMMDD-<slug>")
        elif cal["calibrated"] is False and cal["run_id"] is not None and not RUN_ID_RE.match(str(cal["run_id"])):
            res.errors.append(f"threshold {k}: run_id format must be qcal-YYYYMMDD-<slug>")


def check_paths(res: Result, tracked: list[str]) -> None:
    for i, entry in enumerate(registry.paths_doc().get("paths") or []):
        for g in entry.get("globs") or []:
            if not any(fnmatch.fnmatchcase(f, g) for f in tracked):
                res.errors.append(f"(f) paths.yaml entry {i}: glob {g!r} matches no tracked file")
        if entry.get("artefact") == "selector" and not entry.get("q0_set"):
            res.warnings.append(f"(i) paths.yaml selector {entry.get('globs')}: no Q0 keep/drop set yet "
                                f"(error once the Q-2 corpus exists)")


# ------------------------------------------------------------------------------------------------ run

def run(root: Path = ROOT) -> Result:
    res = Result()
    tracked = tracked_files()
    tracked_set = set(tracked)
    gates = registry.gates()
    exact = {n for n, g in gates.items() if g.get("lint_exact")}
    registered = set(gates)
    py_files = {}
    for path in tracked:
        if _py_in_scope(path) and (root / path).exists():
            py_files[path] = (root / path).read_text(encoding="utf-8")
    for path, src in py_files.items():
        scan_python(path, src, res, exact, registered)
    ts_n = 0
    for path in tracked:
        if _ts_in_scope(path) and (root / path).exists():
            ts_n += 1
            scan_ts(path, (root / path).read_text(encoding="utf-8"), res)
    res.scanned = {"python": len(py_files), "ts": ts_n}
    # (c)
    jev = registry.jev_questions()
    code_keys = jev_code_keys(py_files)
    for k, where in sorted(code_keys.items()):
        if k not in jev:
            res.errors.append(f"{where}: (c) Jev question key {k!r} is not in taxonomy.yaml jev_questions")
        elif jev[k].get("status") != "used":
            res.warnings.append(f"{where}: Jev key {k!r} is read by code but registered as {jev[k].get('status')}")
    for k, where in sorted(jev_seed_keys(root, tracked).items()):
        if k not in jev:
            res.errors.append(f"{where}: (c) seeded Jev question key {k!r} is not in taxonomy.yaml jev_questions")
    for k, q in sorted(jev.items()):
        if q.get("status") == "used" and k not in code_keys:
            res.warnings.append(f"Jev key {k!r} is registered as used but no code constant names it")
    # (d)
    a1 = registry.a1_codes()
    for code, where in sorted(a1_code_sites(py_files).items()):
        if code not in a1:
            res.errors.append(f"{where}: (d) A1 code {code!r} is not mapped in taxonomy.yaml a1_codes")
    # (e)(f) + schema
    check_chips(py_files, root, res)
    check_paths(res, tracked)
    check_schema(res, tracked_set)
    # generated TS freshness
    from services.quality import gen_frontend
    gen = root / GENERATED_TS
    if not gen.exists() or gen.read_text(encoding="utf-8") != gen_frontend.render():
        res.errors.append(f"{GENERATED_TS} is stale: run python -m services.quality.gen_frontend")
    # warnings
    for row in registry.parity_report():
        res.warnings.append(f"parity: {row['severity']} class {row['class']} has detectors for {row['covered']} "
                            f"but none for {row['missing']}")
    for cid in registry.enforce_gaps():
        res.warnings.append(f"{cid}: hard/block class with no enforcing detector")
    return res


def render_report(res: Result) -> str:
    lines = ["### Quality registry lint (qtax-v1)", "",
             f"Scanned {res.scanned.get('python', 0)} Python and {res.scanned.get('ts', 0)} TS files. "
             f"**{len(res.errors)} error(s)**, {len(res.warnings)} warning(s), "
             f"{len(res.allows)} `qlint: allow`.", ""]
    for title, items in (("Errors", res.errors), ("Warnings", res.warnings), ("qlint: allow", res.allows)):
        if items:
            lines += [f"<details><summary>{title} ({len(items)})</summary>", "", *[f"- {i}" for i in items], "",
                      "</details>", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Quality System v1 lint (AA-926)")
    ap.add_argument("--report", action="store_true", help="print the Markdown report")
    args = ap.parse_args(argv)
    res = run()
    if args.report:
        print(render_report(res))
    else:
        for e in res.errors:
            print(f"ERROR {e}")
        for w in res.warnings:
            print(f"warn  {w}")
        print(f"{len(res.errors)} error(s), {len(res.warnings)} warning(s), {len(res.allows)} qlint: allow")
    return 1 if res.errors else 0


if __name__ == "__main__":
    sys.exit(main())
