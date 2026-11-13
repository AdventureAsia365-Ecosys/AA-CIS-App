#!/usr/bin/env python3
"""AA-1097 code-health ratchets: function complexity and file size may not get worse.

Two ratchets, one committed baseline (.github/code-health-baseline.json), limits and scope in
.github/code-health.json:

  complexity  Python: ruff rule C901 (mccabe); TypeScript / JavaScript: ESLint's core `complexity` rule, through
              .github/scripts/code_health_eslint.mjs and the frontend's own node_modules. A function is keyed by
              "<path>::<qualified name>". A function over the limit that is not in the baseline fails (new code stays
              under the limit); a baseline function may not score higher than its baseline value.
  file size   a source file over `max_lines` that is not in the baseline fails; a baseline file may not grow.
              Exempt paths (generated code, migrations, fixtures, catalogues, vendored code) carry a reason.

The baseline only shrinks. `check` also fails when an entry is stale (the function is gone or back under the limit,
the file shrank) so the baseline always equals today's offenders: run `update`, which only removes entries and lowers
values, and commit the baseline. Against a base commit (`--base`, CI passes the PR base) `check` fails when the
baseline gained an entry or a value went up, or a limit was raised. A function moved to another file keeps its
entry under the new path (same qualified name, score not higher); a baseline file split into parts may list the parts
while their total stays within the removed file's lines.

  python3 .github/scripts/code_health.py check  [--kind python,typescript,files] [--base REF] [--allow-missing-tools]
  python3 .github/scripts/code_health.py update [--kind ...]     # shrink the baseline after an improvement
  python3 .github/scripts/code_health.py report [--kind ...]     # distribution of today's scores (choosing a limit)
  python3 .github/scripts/code_health.py init   [--kind ...]     # write a new baseline from scratch (first adoption only)

Standard library only; ruff and the frontend's node_modules are the measuring tools.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_PATH = ".github/code-health.json"
BASELINE_PATH = ".github/code-health-baseline.json"
ESLINT_SCRIPT = ".github/scripts/code_health_eslint.mjs"
KINDS = ("python", "typescript", "files")
SELF = "python3 .github/scripts/code_health.py"
RUFF_MESSAGE = re.compile(r"^`(?P<name>.+)` is too complex \((?P<score>\d+) > \d+\)")


class ToolMissing(RuntimeError):
    """A measuring tool (ruff, the frontend's node_modules) is not installed."""


# --- scope ---------------------------------------------------------------------------------------------------------


def matches(path: str, globs: list) -> bool:
    """True when PATH matches one of GLOBS (strings or {"glob": ...} entries); `*` also crosses "/"."""
    for g in globs:
        pattern = g["glob"] if isinstance(g, dict) else g
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def git_files(root: Path) -> list[str]:
    # Tracked files plus new files not ignored yet, so a new file is checked before its first commit.
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root,
                         capture_output=True, check=True).stdout
    return sorted({p for p in out.decode().split("\0") if p and (root / p).is_file()})


def in_scope(files: list[str], section: dict, exclude_key: str = "exclude") -> list[str]:
    return [f for f in files if matches(f, section.get("include", [])) and not matches(f, section.get(exclude_key, []))]


# --- measuring -----------------------------------------------------------------------------------------------------


def python_qualnames(source: str) -> dict[tuple[int, str], str]:
    """(def line, name) -> qualified name ("Class.method", "outer.inner"); a repeated name gets "#2", "#3"..."""
    found: list[tuple[int, str, str]] = []

    def visit(body: list, prefix: list[str]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((node.lineno, node.name, ".".join([*prefix, node.name])))
                visit(node.body, [*prefix, node.name])
            elif isinstance(node, ast.ClassDef):
                visit(node.body, [*prefix, node.name])
            else:
                for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                    child = getattr(node, field, None)
                    if isinstance(child, list):
                        visit(child, prefix)

    visit(ast.parse(source).body, [])
    seen: dict[str, int] = {}
    names: dict[tuple[int, str], str] = {}
    for line, name, qual in found:
        seen[qual] = seen.get(qual, 0) + 1
        names[(line, name)] = qual if seen[qual] == 1 else f"{qual}#{seen[qual]}"
    return names


def find_ruff() -> list[str]:
    explicit = os.environ.get("CODE_HEALTH_RUFF")
    if explicit:
        return [explicit]
    on_path = shutil.which("ruff")
    if on_path:
        return [on_path]
    try:
        import importlib.util

        if importlib.util.find_spec("ruff") is not None:
            return [sys.executable, "-m", "ruff"]
    except (ImportError, ValueError):
        pass
    raise ToolMissing("ruff is not installed (pip install ruff==<the version in .github/code-health.json>)")


def measure_python(root: Path, section: dict, files: list[str], names: bool = True) -> dict[str, int]:
    """Every function's C901 score, keyed "<path>::<qualified name>" (or "<path>:<line>" when NAMES is false)."""
    paths = in_scope(files, section)
    if not paths:
        return {}
    ruff = find_ruff()
    version = subprocess.run([*ruff, "--version"], capture_output=True, text=True).stdout.strip()
    want = section.get("ruff_version")
    if want and version.split()[-1:] != [want]:
        print(f"warning: {version or 'ruff'} differs from the pinned ruff {want}; scores may differ from CI")
    diagnostics: list[dict] = []
    for i in range(0, len(paths), 1500):
        cmd = [*ruff, "check", "--isolated", "--no-cache", "--exit-zero", "--select", "C901",
               "--config", "lint.mccabe.max-complexity=0", "--target-version", section.get("target_version", "py312"),
               "--output-format", "json", *paths[i:i + 1500]]
        run = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
        if run.returncode != 0:
            raise RuntimeError(f"ruff failed: {run.stderr.strip()}")
        diagnostics.extend(json.loads(run.stdout or "[]"))
    scores: dict[str, int] = {}
    qualnames: dict[str, dict] = {}
    for d in diagnostics:
        rel = Path(d["filename"]).resolve().relative_to(root.resolve()).as_posix()
        m = RUFF_MESSAGE.match(d.get("message", ""))
        if d.get("code") != "C901" or not m:
            raise RuntimeError(f"ruff could not analyse {rel}: {d.get('message')}")
        row, name, score = d["location"]["row"], m["name"], int(m["score"])
        if not names:
            scores[f"{rel}:{row}"] = score
            continue
        if rel not in qualnames:
            qualnames[rel] = python_qualnames((root / rel).read_text(encoding="utf-8"))
        scores[f"{rel}::{qualnames[rel].get((row, name), f'{name}@{row}')}"] = score
    return scores


def measure_typescript(root: Path, section: dict, files: list[str]) -> dict[str, int]:
    """Every function's ESLint complexity score, keyed "<path>::<qualified name>"."""
    scores: dict[str, int] = {}
    for d in section.get("dirs", []):
        prefix = d.rstrip("/") + "/"
        paths = [f[len(prefix):] for f in in_scope(files, section) if f.startswith(prefix)]
        if not paths:
            continue
        if not (root / d / "node_modules" / "eslint").is_dir():
            raise ToolMissing(f"{d}/node_modules is missing (cd {d} && npm ci)")
        if not shutil.which("node"):
            raise ToolMissing("node is not installed")
        run = subprocess.run(["node", str(root / ESLINT_SCRIPT), str(root / d)], input=json.dumps(paths),
                             capture_output=True, text=True)
        if run.returncode != 0:
            raise RuntimeError(f"{ESLINT_SCRIPT} failed for {d}: {run.stderr.strip()[-2000:]}")
        out = json.loads(run.stdout)
        if out["errors"]:
            first = out["errors"][0]
            raise RuntimeError(f"ESLint could not parse {d}/{first['path']}: {first['message']} "
                               f"({len(out['errors'])} error(s))")
        for fn in out["functions"]:
            scores[f"{prefix}{fn['path']}::{fn['name']}"] = fn["score"]
    return scores


def count_lines(data: bytes) -> int:
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def measure_files(root: Path, section: dict, files: list[str]) -> dict[str, int]:
    return {f: count_lines((root / f).read_bytes()) for f in in_scope(files, section, "exempt")}


def measure(kind: str, root: Path, config: dict, files: list[str]) -> dict[str, int]:
    section = config[kind]
    if kind == "python":
        return measure_python(root, section, files)
    if kind == "typescript":
        return measure_typescript(root, section, files)
    return measure_files(root, section, files)


def limit_of(kind: str, config: dict) -> int:
    return int(config[kind]["max_lines" if kind == "files" else "max_complexity"])


# --- comparing (pure) ----------------------------------------------------------------------------------------------


def compare(kind: str, current: dict[str, int], limit: int, baseline: dict[str, int]) -> tuple[list[str], dict]:
    """Problems of CURRENT (every measured item) against BASELINE, and the shrunk baseline `update` would write."""
    unit = "lines" if kind == "files" else "complexity"
    over = {k: v for k, v in current.items() if v > limit}
    problems: list[str] = []
    for key in sorted(over):
        if key not in baseline:
            what = "file" if kind == "files" else "function"
            problems.append(f"NEW    {key}: {unit} {over[key]} > limit {limit}. A new or newly over-limit {what} must "
                            f"stay within {limit}: split it.")
        elif over[key] > baseline[key]:
            problems.append(f"WORSE  {key}: {unit} {over[key]} > baseline {baseline[key]}. A baseline entry may not "
                            f"grow: bring it back to {baseline[key]} or below (better: under {limit}).")
        elif over[key] < baseline[key]:
            problems.append(f"TIGHT  {key}: {unit} {over[key]} < baseline {baseline[key]}. Improved: run "
                            f"`{SELF} update` and commit {BASELINE_PATH}.")
    for key in sorted(set(baseline) - set(over)):
        state = "gone" if key not in current else f"now {current[key]} <= limit {limit}"
        problems.append(f"STALE  {key}: {state}. Run `{SELF} update` and commit {BASELINE_PATH}.")
    shrunk = {k: min(v, over[k]) for k, v in baseline.items() if k in over}
    return problems, shrunk


def compare_with_base(kind: str, head: dict[str, int], base: dict[str, int]) -> list[str]:
    """The baseline may only shrink against the base commit: no new entry, no higher value."""
    problems: list[str] = []
    added = {k: v for k, v in head.items() if k not in base}
    removed = {k: v for k, v in base.items() if k not in head}
    for key in sorted(k for k in head if k in base and head[k] > base[k]):
        problems.append(f"RAISED {key}: baseline {base[key]} -> {head[key]}. The baseline only shrinks.")
    if kind == "files":
        if added and sum(added.values()) > sum(removed.values()):
            problems.append(f"ADDED  {', '.join(sorted(added))}: new baseline file(s) with {sum(added.values())} lines "
                            f"but only {sum(removed.values())} lines of removed entries. A new file stays within the "
                            "limit; a split baseline file's parts may not total more than the file had.")
        return problems
    pool = {}
    for key, value in removed.items():
        pool.setdefault(key.split("::", 1)[-1], []).append(value)
    for key in sorted(added):
        name = key.split("::", 1)[-1]
        candidates = sorted(v for v in pool.get(name, []) if v >= added[key])
        if candidates:
            pool[name].remove(candidates[0])  # a moved function keeps its entry under the new path
            continue
        problems.append(f"ADDED  {key}: {added[key]}. The baseline only shrinks; a new function stays within the limit "
                        "(a moved function keeps its qualified name and may not score higher).")
    return problems


# --- baseline / config io ------------------------------------------------------------------------------------------


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_baseline(root: Path, baseline: dict) -> None:
    ordered = {kind: dict(sorted(baseline.get(kind, {}).items())) for kind in KINDS}
    (root / BASELINE_PATH).write_text(json.dumps(ordered, indent=1) + "\n", encoding="utf-8")


def git_show_json(root: Path, ref: str, path: str) -> dict | None:
    run = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=root, capture_output=True, text=True)
    return json.loads(run.stdout) if run.returncode == 0 else None


def config_problems(config: dict) -> list[str]:
    problems = []
    for kind in KINDS:
        for key in ("exclude", "exempt"):
            for entry in config.get(kind, {}).get(key, []):
                if not isinstance(entry, dict) or not str(entry.get("reason", "")).strip():
                    problems.append(f"CONFIG {kind}.{key}: every entry is {{\"glob\", \"reason\"}} with a reason: {entry}")
    return problems


def base_problems(root: Path, ref: str, config: dict, baseline: dict, kinds: list[str]) -> list[str]:
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=root,
                      capture_output=True).returncode != 0:
        print(f"notice: base {ref} is not available: the shrink-only check against the base is skipped")
        return []
    base_config = git_show_json(root, ref, CONFIG_PATH)
    base_baseline = git_show_json(root, ref, BASELINE_PATH)
    if base_config is None or base_baseline is None:
        print(f"notice: {ref} has no code-health baseline yet (this change creates it)")
        return []
    problems = []
    for kind in kinds:
        if kind in base_config and limit_of(kind, config) > limit_of(kind, base_config):
            problems.append(f"LIMIT  {kind}: raised from {limit_of(kind, base_config)} to {limit_of(kind, config)}. "
                            "Limits only go down.")
        problems += [f"[{kind}] {p}" for p in compare_with_base(kind, baseline.get(kind, {}),
                                                                base_baseline.get(kind, {}))]
    return problems


# --- commands ------------------------------------------------------------------------------------------------------


def percentile(values: list[int], p: int) -> int:
    return values[min(len(values) - 1, len(values) * p // 100)] if values else 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("check", "update", "report", "init"))
    parser.add_argument("--kind", default=",".join(KINDS), help="comma-separated: python,typescript,files")
    parser.add_argument("--base", help="base commit: the baseline may only shrink against it (check)")
    parser.add_argument("--allow-missing-tools", action="store_true",
                        help="a missing ruff / node_modules is a visible skip instead of a failure")
    parser.add_argument("--root", default=".", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def run_kind(command: str, kind: str, current: dict[str, int], limit: int, baseline: dict, new_baseline: dict
             ) -> list[str]:
    """One kind of one command: prints its summary line, updates NEW_BASELINE, returns its problems."""
    over = {k: v for k, v in current.items() if v > limit}
    if command == "report":
        values = sorted(current.values())
        print(f"{kind}: {len(values)} measured, limit {limit}, {len(over)} over the limit, "
              + " ".join(f"p{p}={percentile(values, p)}" for p in (50, 75, 90, 95, 97, 99))
              + f" max={values[-1] if values else 0}")
        return []
    if command == "init":
        new_baseline[kind] = over
        print(f"{kind}: baseline written with {len(over)} entries (limit {limit})")
        return []
    old = baseline.get(kind, {})
    problems, shrunk = compare(kind, current, limit, old)
    if command == "update":
        new_baseline[kind] = shrunk
        print(f"{kind}: baseline {len(old)} -> {len(shrunk)} entries")
        problems = [p for p in problems if p.startswith(("NEW", "WORSE"))]
    else:
        print(f"{kind}: {len(current)} measured, limit {limit}, {len(over)} over the limit (baseline {len(old)})")
    return [f"[{kind}] {p}" for p in problems]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    config = load_json(root / CONFIG_PATH)
    baseline = load_json(root / BASELINE_PATH)
    kinds = [k for k in args.kind.split(",") if k]
    if not config or any(k not in KINDS for k in kinds):
        print(f"error: {CONFIG_PATH} is missing" if not config else f"error: unknown kind in {args.kind}")
        return 2
    kinds = [k for k in kinds if k in config]
    files = git_files(root)
    problems = config_problems(config)
    new_baseline = {k: dict(v) for k, v in baseline.items()}
    for kind in kinds:
        try:
            current = measure(kind, root, config, files)
        except ToolMissing as exc:
            if not (args.allow_missing_tools and args.command == "check"):
                print(f"error: {kind}: {exc}")
                return 2
            print(f"SKIP   {kind}: {exc}")
            continue
        problems += run_kind(args.command, kind, current, limit_of(kind, config), baseline, new_baseline)
    if args.command in ("update", "init"):
        write_baseline(root, new_baseline)
    if args.command == "check" and args.base:
        problems += base_problems(root, args.base, config, baseline, kinds)
    return finish(args.command, problems)


def finish(command: str, problems: list[str]) -> int:
    if command == "report":
        return 0
    for p in problems:
        print(p)
    if problems:
        print(f"\ncode-health: {len(problems)} problem(s). Limits and exemptions: {CONFIG_PATH}; "
              f"baseline: {BASELINE_PATH} (only shrinks).")
        return 1
    print("code-health: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
