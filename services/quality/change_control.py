"""AA-1052 W6 (root ADR 0020 "Measurement": a release fingerprint that moves on any route, effort, parameter, prompt,
policy or memory change; amendment d12: a route change on a gated stage needs an eval run id or a break-glass reason)
— permanent change control of the writing path, enforced in CI ($0, no DB, no model call).

Two checks, both run by the required "Unit Tests" job (``tests/unit/test_aa1052_change_control.py``) and by
``python -m services.quality.change_control``:

1. **The lock.** ``change_control.lock.json`` records, per writing-path component, the sha in force and the
   evidence that let it change. A PR that moves a component (a meta band widened in the policy, a prompt version
   bump, a route-table row, a gate threshold, the final contract's version or mode, a gated stage's code route
   default) fails until the lock carries the new sha with its evidence:

     python -m services.quality.change_control --accept writing_policy --evidence "eval:<run uuid>"
     python -m services.quality.change_control --accept route_table --evidence "parity:tests/unit/<replay test>.py"
     python -m services.quality.change_control --accept gates --evidence "break-glass:<why, at least 12 chars>"

   Evidence kinds: ``eval:<uuid>`` (a Writing Lab run, ``shared.prompt_eval_runs.id``, that measured the change),
   ``parity:<tests/...py>`` (a tracked replay or parity test that proves the output is unchanged: a mechanics-only
   change) or ``break-glass:<reason>`` (an incident or a decided policy change ahead of its measurement; the reason
   is reviewed in the PR). The lock's git history is the change log of the writing path.

2. **Route migrations.** A CIS migration numbered above ``MIGRATION_BASELINE`` that writes the route config
   (``ROUTE_TABLES``) of a gated stage (``GATED_STAGE_PREFIXES``; a global table such as the deployment priorities
   or the route settings always counts) or a writing-memory release, must carry ``-- eval: <run uuid>`` or
   ``-- break-glass: <reason>`` in its leading comment block (CIS 383 is the first file that did). Prompt
   registrations (``shared.prompt_version``) are the version bump itself and need no header. The lint fails
   closed: a write to a stage-scoped table is exempt only when every stage it selects is a literal outside the
   gated prefixes (``stage = '<lit>'``, ``stage IN ('<lit>', ...)``, an INSERT ... VALUES of literals, or a LIKE
   pattern that cannot match a gated prefix); a write by role, by a subquery, a regex, ``<>`` / ``NOT``, ``OR``
   or with no stage selector at all counts as gated.

The components read files only (``root`` defaults to the repo), except ``route_defaults``, which imports
``shared.llm_client.role_config.SAFE_DEFAULTS`` (the code route a gated stage falls back to without its DB row).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = Path(__file__).resolve().parent / "change_control.lock.json"
LOCK_VERSION = "cc-v1"

# The writer paths' stages (services/writing_memory/config_fp.STAGE_PREFIXES + EVAL_STAGES), restated as literals so
# this module stays file-only; the test checks the two agree.
GATED_STAGE_PREFIXES = ("s1_", "t2_", "t3_", "t8_", "t9_", "t10_", "writing_memory_eval")

POLICY_FILE = "services/quality/writing_policy.yaml"
GATE_FILES = ("services/quality/taxonomy.yaml", "services/quality/thresholds.yaml")
PROMPT_MANIFEST = "shared/llm_client/prompt_versions.json"
ROUTE_TABLE_FILE = "services/acp_content_writing/route_table.yaml"
FINAL_CONTRACT_FILE = "services/quality/final_contract.py"
FINAL_CONTRACT_NAMES = ("CONTRACT_VERSION", "MODE")
MIGRATIONS_DIR = "api/migrations"

# Every migration up to here is grandfathered (written before the lint): only newer files must carry the header.
MIGRATION_BASELINE = 390
# Route config tables: rows of one stage (the stage literal decides whether it is gated) ...
STAGE_ROUTE_TABLES = ("llm_role_config", "stage_model_params")
# ... and tables that change every stage's route at once (always gated), plus a writing-memory release.
GLOBAL_ROUTE_TABLES = ("llm_model_deployment", "llm_model_catalog", "llm_route_settings", "llm_route_policy",
                       "writing_memory_release")
ROUTE_TABLES = STAGE_ROUTE_TABLES + GLOBAL_ROUTE_TABLES
_WRITE_RE = re.compile(r"\b(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|ALTER\s+TABLE)\s+"
                       r"(?:ONLY\s+)?shared\.(" + "|".join(ROUTE_TABLES) + r")\b", re.IGNORECASE)
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_TABLE_REF = r"(?:ONLY\s+)?shared\.(?:" + "|".join(STAGE_ROUTE_TABLES) + r")\b"
_STAGE_WRITE_RE = re.compile(r"\b(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|ALTER\s+TABLE)\s+"
                             + _TABLE_REF, re.IGNORECASE)
_COL = r"(?:\b\w+\.)?\bstage\b"   # the stage column, optionally qualified by an alias
# the stage selectors the lint can read (literals masked as '#<n>')
_STAGE_EQ_RE = re.compile(_COL + r"\s*=\s*'#(\d+)'", re.IGNORECASE)
_STAGE_IN_RE = re.compile(_COL + r"\s+IN\s*\(\s*('#\d+'(?:\s*,\s*'#\d+')*)\s*\)", re.IGNORECASE)
_STAGE_LIKE_RE = re.compile(_COL + r"\s+I?LIKE\s+'#(\d+)'(?:\s+ESCAPE\s+'#(\d+)')?", re.IGNORECASE)
_STAGE_ANY_RE = re.compile(_COL, re.IGNORECASE)
_INSERT_COLS_RE = re.compile(r"\bINSERT\s+INTO\s+" + _TABLE_REF + r"\s*(?:AS\s+\w+\s*)?\(([^)]*)\)", re.IGNORECASE)
_EVAL_HEADER_RE = re.compile(r"^--\s*eval:\s*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s*$")
_BREAK_GLASS_HEADER_RE = re.compile(r"^--\s*break-glass:\s*(\S.{11,})$")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
BREAK_GLASS_MIN = 12


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()[:16]


def _yaml(root: Path, rel: str):
    with open(root / rel, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── components ────────────────────────────────────────────────────────────────────────────────────────────────────

def _writing_policy(root: Path) -> str:
    """policy_sha of the policy SOURCE (the compiled file can lag; the quality lint checks it is fresh)."""
    from services.quality import writing_policy
    return writing_policy.sha_of(writing_policy.load_source(root / POLICY_FILE))


def _gates(root: Path) -> str:
    return _sha({rel: _yaml(root, rel) for rel in GATE_FILES})


def _prompt_registry(root: Path) -> str:
    """The registered version of every prompt stage (a prompt edit without a bump already fails the prompt
    registry gate; a bump moves this)."""
    with open(root / PROMPT_MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)
    pairs = sorted({(p["stage"], p["version"]) for p in manifest.get("prompts") or []})
    return _sha(pairs)


def _route_table(root: Path) -> str:
    return _sha(_yaml(root, ROUTE_TABLE_FILE))


def _module_constants(path: Path, names: Iterable[str]) -> dict:
    """Module-level string constants of a file, a name bound to another constant resolved (``MODE = MODE_REPORT``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    env: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            v = node.value
            if isinstance(v, ast.Constant):
                env[node.targets[0].id] = v.value
            elif isinstance(v, ast.Name) and v.id in env:
                env[node.targets[0].id] = env[v.id]
    return {n: env.get(n) for n in names}


def _final_contract(root: Path) -> str:
    return _sha(_module_constants(root / FINAL_CONTRACT_FILE, FINAL_CONTRACT_NAMES))


def is_gated_stage(stage: str) -> bool:
    return any(stage.startswith(p) for p in GATED_STAGE_PREFIXES)


def _route_defaults(root: Path) -> str:  # noqa: ARG001 — reads the imported module, not a file
    from dataclasses import asdict
    from shared.llm_client.role_config import SAFE_DEFAULTS
    return _sha({s: asdict(c) for s, c in sorted(SAFE_DEFAULTS.items()) if is_gated_stage(s)})


COMPONENTS: dict[str, Callable[[Path], str]] = {
    "writing_policy": _writing_policy,
    "gates": _gates,
    "prompt_registry": _prompt_registry,
    "route_table": _route_table,
    "final_contract": _final_contract,
    "route_defaults": _route_defaults,
}
WHAT = {
    "writing_policy": POLICY_FILE + " (bands, claim policy, route rows, channel rules)",
    "gates": " + ".join(GATE_FILES) + " (gate taxonomy, pass lines)",
    "prompt_registry": PROMPT_MANIFEST + " (a prompt version bump of any stage)",
    "route_table": ROUTE_TABLE_FILE + " (the T9 / T2 loop mechanics per gate)",
    "final_contract": FINAL_CONTRACT_FILE + " CONTRACT_VERSION / MODE (report -> enforce)",
    "route_defaults": "shared/llm_client/role_config.py SAFE_DEFAULTS of a gated stage",
}


def current(root: Path = REPO_ROOT, names: Optional[Iterable[str]] = None) -> dict[str, str]:
    return {n: COMPONENTS[n](root) for n in (names or COMPONENTS)}


# ── evidence and the lock ─────────────────────────────────────────────────────────────────────────────────────────

def evidence_problem(evidence: Optional[str], root: Path = REPO_ROOT) -> Optional[str]:
    """None when ``evidence`` is acceptable, else why not."""
    kind, _, value = (evidence or "").partition(":")
    kind, value = kind.strip(), value.strip()
    if kind == "eval":
        return None if _UUID_RE.match(value) else "eval evidence needs the run's uuid (shared.prompt_eval_runs.id)"
    if kind == "parity":
        if not (value.startswith("tests/") and value.endswith(".py")):
            return "parity evidence names a test file under tests/ (a replay or parity test)"
        return None if (root / value).is_file() else f"parity test {value} does not exist"
    if kind == "break-glass":
        return None if len(value) >= BREAK_GLASS_MIN else f"a break-glass reason needs at least {BREAK_GLASS_MIN} chars"
    return "evidence must be eval:<uuid>, parity:<tests/...py> or break-glass:<reason>"


def load_lock(path: Path = LOCK_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def check(root: Path = REPO_ROOT, lock: Optional[dict] = None, names: Optional[Iterable[str]] = None) -> list[str]:
    """Every problem of the lock against the tree: a moved component, an unknown or missing entry, bad evidence."""
    lock = lock if lock is not None else load_lock()
    entries = lock.get("components") or {}
    problems: list[str] = []
    if lock.get("version") != LOCK_VERSION:
        problems.append(f"lock version {lock.get('version')!r} is not {LOCK_VERSION!r}")
    wanted = list(names or COMPONENTS)
    for n in sorted(set(entries) - set(COMPONENTS)):
        problems.append(f"lock names an unknown component {n!r}")
    for n, sha in current(root, wanted).items():
        e = entries.get(n)
        if not isinstance(e, dict):
            problems.append(f"{n}: no lock entry ({WHAT[n]}); add one with --accept {n} --evidence ...")
            continue
        why = evidence_problem(e.get("evidence"), root)
        if why:
            problems.append(f"{n}: {why}")
        if e.get("sha") != sha:
            problems.append(
                f"{n} changed ({WHAT[n]}): lock {e.get('sha')} != tree {sha}. A writing-path change needs its "
                f"evidence (ADR 0020): python -m services.quality.change_control --accept {n} "
                f"--evidence 'eval:<run uuid>' | 'parity:tests/<replay test>.py' | 'break-glass:<reason>'")
    return problems


def accept(name: str, evidence: str, refs: str, root: Path = REPO_ROOT, path: Path = LOCK_PATH) -> dict:
    """Record the tree's sha of ``name`` with its evidence (refuses bad evidence)."""
    if name not in COMPONENTS:
        raise ValueError(f"unknown component {name!r} (one of {', '.join(COMPONENTS)})")
    why = evidence_problem(evidence, root)
    if why:
        raise ValueError(why)
    lock = load_lock(path) if path.exists() else {"version": LOCK_VERSION, "components": {}}
    lock.setdefault("components", {})[name] = {"sha": COMPONENTS[name](root), "evidence": evidence.strip(),
                                               "refs": refs.strip()}
    lock["components"] = dict(sorted(lock["components"].items()))
    path.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return lock


# ── route migrations ──────────────────────────────────────────────────────────────────────────────────────────────

def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _leading_comments(sql: str) -> list[str]:
    out = []
    for line in sql.splitlines():
        s = line.strip()
        if not s:
            continue
        if not s.startswith("--"):
            break
        out.append(s)
    return out


def _like_can_match_gated(pattern: str, escape: Optional[str] = None) -> bool:
    """Whether a LIKE pattern can match a stage that starts with a gated prefix: its fixed head (up to the first
    wildcard) is compared with each prefix; a wildcard inside the prefix's length matches anything (conservative)."""
    esc = escape if escape else "\\"
    head, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == esc and i + 1 < len(pattern):
            head.append(pattern[i + 1])
            i += 2
            continue
        if ch in "%_":
            break
        head.append(ch)
        i += 1
    fixed = "".join(head)
    open_end = i < len(pattern)          # a wildcard follows the fixed head
    for p in GATED_STAGE_PREFIXES:
        if fixed.startswith(p) or (p.startswith(fixed) and (open_end or fixed == p)):
            return True
    return False


def _statement_is_gated(stmt: str, *, insert: bool) -> bool:
    """A write to a stage-scoped route table (one whole statement, ``insert`` when the write is an INSERT): True
    unless every stage it selects is a literal outside the gated prefixes. Fails closed on anything it cannot read."""
    literals = [m.group(0)[1:-1].replace("''", "'") for m in _LITERAL_RE.finditer(stmt)]
    if any(is_gated_stage(v.strip().lower()) for v in literals):
        return True
    counter = iter(range(len(literals)))
    masked = _LITERAL_RE.sub(lambda _m: f"'#{next(counter)}'", stmt)
    if re.search(r"\b(?:TRUNCATE|ALTER\s+TABLE)\b", masked, re.IGNORECASE):
        return True                                       # every stage at once
    if re.search(r"\bOR\b|\bNOT\b|<>|!=|~|\bSIMILAR\b|\bANY\b|\bALL\b", masked, re.IGNORECASE):
        return True                                       # a selector the lint does not read
    selectors = 0
    if insert:
        cols = _INSERT_COLS_RE.search(masked)
        if cols is None or re.search(r"\bSELECT\b", masked, re.IGNORECASE):
            return True                                   # no column list, or rows from a query
        if not _STAGE_ANY_RE.search(cols.group(1)):
            return True                                   # no stage column: not a stage selector
        selectors += 1                                    # VALUES of literals, none gated (checked above)
        masked = masked[:cols.start()] + masked[cols.end():]
        masked = re.sub(r"\bON\s+CONFLICT\s*\([^)]*\)", " ", masked, flags=re.IGNORECASE)   # the conflict key
    for m in _STAGE_LIKE_RE.finditer(masked):
        esc = literals[int(m.group(2))] if m.group(2) else None
        if _like_can_match_gated(literals[int(m.group(1))].lower(), esc):
            return True
        selectors += 1
    masked = _STAGE_LIKE_RE.sub(" ", masked)
    selectors += len(_STAGE_EQ_RE.findall(masked)) + len(_STAGE_IN_RE.findall(masked))
    masked = _STAGE_IN_RE.sub(" ", _STAGE_EQ_RE.sub(" ", masked))
    if _STAGE_ANY_RE.search(masked):
        return True                                       # the stage column in a form the lint does not read
    return selectors == 0                                 # no stage selector: a role-wide or table-wide write


def _statements(body: str) -> list[str]:
    return [s for s in body.split(";") if s.strip()]


def migration_needs_header(sql: str) -> list[str]:
    """The route tables a migration writes for a gated stage (empty = no header needed). Fails closed for the
    stage-scoped tables (see the module doc)."""
    body = _strip_comments(sql)
    tables = sorted({m.group(1).lower() for m in _WRITE_RE.finditer(body)})
    if not tables:
        return []
    gated = {t for t in tables if t in GLOBAL_ROUTE_TABLES}
    for stmt in _statements(body):
        for m in _STAGE_WRITE_RE.finditer(stmt):
            table = re.search(r"shared\.(\w+)", m.group(0), re.IGNORECASE).group(1).lower()
            insert = m.group(0).split(None, 1)[0].upper() == "INSERT"
            if table not in gated and _statement_is_gated(stmt, insert=insert):
                gated.add(table)
    return sorted(gated)


def has_evidence_header(sql: str) -> bool:
    return any(_EVAL_HEADER_RE.match(c) or _BREAK_GLASS_HEADER_RE.match(c) for c in _leading_comments(sql))


def _number(name: str) -> Optional[int]:
    m = re.match(r"^(\d+)_", name)
    return int(m.group(1)) if m else None


def migration_problems(root: Path = REPO_ROOT, baseline: int = MIGRATION_BASELINE) -> list[str]:
    problems = []
    for path in sorted((root / MIGRATIONS_DIR).glob("*.sql")):
        n = _number(path.name)
        if n is None or n <= baseline:
            continue
        sql = path.read_text(encoding="utf-8")
        tables = migration_needs_header(sql)
        if tables and not has_evidence_header(sql):
            problems.append(f"{MIGRATIONS_DIR}/{path.name} writes the route config of a gated stage "
                            f"({', '.join(tables)}) without '-- eval: <run uuid>' or '-- break-glass: <reason>' in "
                            f"its leading comment block (ADR 0020 amendment d12)")
    return problems


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m services.quality.change_control", description=__doc__.split("\n")[0])
    ap.add_argument("--accept", metavar="COMPONENT", help="record the tree's sha of this component in the lock")
    ap.add_argument("--evidence", help="eval:<run uuid> | parity:<tests/...py> | break-glass:<reason>")
    ap.add_argument("--refs", default="", help="the issue the change belongs to (AA-<n>)")
    ap.add_argument("--show", action="store_true", help="print the tree's shas")
    a = ap.parse_args(argv)
    if a.show:
        print(json.dumps(current(), indent=2))
        return 0
    if a.accept:
        if not a.evidence:
            ap.error("--accept needs --evidence")
        try:
            accept(a.accept, a.evidence, a.refs)
        except ValueError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        print(f"{a.accept}: recorded in {LOCK_PATH.relative_to(REPO_ROOT)}")
        return 0
    problems = check() + migration_problems()
    for p in problems:
        print(f"ERROR {p}")
    print(f"change control: {len(problems)} problem(s), {len(COMPONENTS)} components")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["COMPONENTS", "GATED_STAGE_PREFIXES", "LOCK_PATH", "LOCK_VERSION", "MIGRATION_BASELINE", "ROUTE_TABLES",
           "accept", "check", "current", "evidence_problem", "has_evidence_header", "is_gated_stage", "load_lock",
           "main", "migration_needs_header", "migration_problems"]
