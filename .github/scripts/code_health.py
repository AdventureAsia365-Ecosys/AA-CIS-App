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
the file is gone or back under the limit), so the baseline lists only today's offenders: run `update`, which only removes
entries and lowers values, and commit the baseline. An entry that improved but is still over the limit is a printed
notice, not a failure, so editing a big baseline file does not force a baseline edit (parallel branches would conflict
on that line); `update` lowers it whenever someone runs it. Against a base commit (`--base`, CI passes the PR base,
pre-push passes origin/main; the check uses the merge-base with HEAD, so a baseline that shrank on main after the branch
point is not read as a raise) `check` fails when the baseline gained an entry or a value went up, or a limit was raised.
A function moved to another file keeps its entry under the new path (same qualified name, score not higher); a
baseline file split into parts may list the parts while their total stays within the removed file's lines.
Ordinals: a repeated name gets "#2", "#3"... in source order ("Page.map()#3", "Store.size#2"). Inserting a simple
callback above a baseline one renumbers it, so entries are paired by family (the key without its "#n") before
anything is reported: within one family the over-limit functions are matched to the baseline entries, each to an
entry scoring at least as much. A renumbered entry is a notice and `update` re-keys it.

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


ORDINAL = re.compile(r"#\d+$")


def family(kind: str, key: str) -> str:
    """The key without its ordinal ("a.tsx::Page.map()#3" -> "a.tsx::Page.map()"); a file is its own family."""
    return key if kind == "files" else ORDINAL.sub("", key)


def match_scores(items: list[tuple[str, int]], entries: list[tuple[str, int]]
                 ) -> tuple[list[tuple[str, int, str, int]], list[tuple[str, int]], list[tuple[str, int]]]:
    """Pair as many ITEMS as possible with distinct ENTRIES scoring at least as much.

    Smallest item first, each takes the smallest entry that still covers it (an entry with the same key wins a tie),
    which pairs the most items. Returns (pairs as (item key, score, entry key, value), unpaired items, unused entries).
    """
    left = sorted(entries, key=lambda kv: (kv[1], kv[0]))
    pairs, unpaired = [], []
    for key, score in sorted(items, key=lambda kv: (kv[1], kv[0])):
        fits = [i for i, (_, value) in enumerate(left) if value >= score]
        if not fits:
            unpaired.append((key, score))
            continue
        entry_key, value = left.pop(min(fits, key=lambda i: (left[i][1], left[i][0] != key)))
        pairs.append((key, score, entry_key, value))
    return pairs, unpaired, left


def compare(kind: str, current: dict[str, int], limit: int, baseline: dict[str, int]
            ) -> tuple[list[str], list[str], dict]:
    """Problems and notices of CURRENT (every measured item) against BASELINE, and the baseline `update` would write.

    Problems: NEW (over the limit, no baseline entry covers it), WORSE (scores above its baseline entry) and STALE (a
    baseline entry no over-limit item needs any more). Notices: TIGHT (improved, still over the limit) and RENUM (an
    ordinal shifted; the item is covered by a sibling entry of its family). `update` writes the paired items at their
    current score: it removes and lowers entries and re-keys renumbered ones, it never adds or raises.
    """
    unit = "lines" if kind == "files" else "complexity"
    what = "file" if kind == "files" else "function"
    over = {k: v for k, v in current.items() if v > limit}
    families: dict[str, tuple[list, list]] = {}
    for key, value in over.items():
        families.setdefault(family(kind, key), ([], []))[0].append((key, value))
    for key, value in baseline.items():
        families.setdefault(family(kind, key), ([], []))[1].append((key, value))
    problems: list[tuple[str, str]] = []
    notices: list[str] = []
    shrunk: dict[str, int] = {}
    for items, entries in families.values():
        pairs, unpaired, unused = match_scores(items, entries)
        for key, score, entry_key, value in pairs:
            shrunk[key] = score
            if entry_key != key:
                notices.append(f"RENUM  {entry_key} -> {key}: {unit} {score} <= baseline {value} (an ordinal shifted). "
                               f"`{SELF} update` re-keys it.")
            elif score < value:
                notices.append(f"TIGHT  {key}: {unit} {score} < baseline {value}. Improved: `{SELF} update` lowers "
                               "the entry (optional).")
        unused = sorted(unused, key=lambda kv: kv[1])
        for key, score in sorted(unpaired, key=lambda kv: -kv[1]):
            if not unused:
                problems.append((key, f"NEW    {key}: {unit} {score} > limit {limit}. A new or newly over-limit {what} "
                                      f"must stay within {limit}: split it."))
                continue
            # The family still has an entry, just not a big enough one: the item got worse (its own entry first).
            entry_key, value = unused.pop(next((i for i, kv in enumerate(unused) if kv[0] == key), len(unused) - 1))
            shrunk[key] = value  # update never raises
            was = "" if entry_key == key else f" (entry {entry_key}, an ordinal shifted)"
            problems.append((key, f"WORSE  {key}: {unit} {score} > baseline {value}{was}. A baseline entry may not "
                                  f"grow: bring it back to {value} or below (better: under {limit})."))
        for key, _ in unused:
            state = ("gone" if key not in current else f"now {current[key]} <= limit {limit}" if key not in over
                     else "renumbered, its family has fewer over-limit items")
            problems.append((f"~{key}", f"STALE  {key}: {state}. Run `{SELF} update` and commit {BASELINE_PATH}."))
    return [p for _, p in sorted(problems)], sorted(notices), shrunk


def compare_with_base(kind: str, head: dict[str, int], base: dict[str, int]) -> list[str]:
    """The baseline may only shrink against the base commit: no new entry, no higher value.

    Functions are compared per family of the qualified name without path and ordinal, so a function moved to another
    file, or renumbered, keeps its entry as long as it does not score higher than the entry it takes over.
    """
    problems: list[str] = []
    if kind == "files":
        added = {k: v for k, v in head.items() if k not in base}
        removed = {k: v for k, v in base.items() if k not in head}
        for key in sorted(k for k in head if k in base and head[k] > base[k]):
            problems.append(f"RAISED {key}: baseline {base[key]} -> {head[key]}. The baseline only shrinks.")
        if added and sum(added.values()) > sum(removed.values()):
            problems.append(f"ADDED  {', '.join(sorted(added))}: new baseline file(s) with {sum(added.values())} lines "
                            f"but only {sum(removed.values())} lines of removed entries. A new file stays within the "
                            "limit; a split baseline file's parts may not total more than the file had.")
        return problems
    families: dict[str, tuple[list, list]] = {}
    for key, value in head.items():
        families.setdefault(ORDINAL.sub("", key.split("::", 1)[-1]), ([], []))[0].append((key, value))
    for key, value in base.items():
        families.setdefault(ORDINAL.sub("", key.split("::", 1)[-1]), ([], []))[1].append((key, value))
    for items, entries in families.values():
        for key, value in match_scores(items, entries)[1]:
            if key in base:
                problems.append(f"RAISED {key}: baseline {base[key]} -> {value}. The baseline only shrinks.")
            else:
                problems.append(f"ADDED  {key}: {value}. The baseline only shrinks; a new function stays within the "
                                "limit (a moved function keeps its qualified name and may not score higher).")
    return sorted(problems)


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
    # Compare with the branch point, not the base tip: a baseline that shrank on main after this branch was cut is not
    # this branch raising it (pre-push passes origin/main; in CI the PR base is already an ancestor, so it is the same).
    fork = subprocess.run(["git", "merge-base", ref, "HEAD"], cwd=root, capture_output=True, text=True)
    if fork.returncode == 0 and fork.stdout.strip():
        ref = fork.stdout.strip()
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
    problems, notices, shrunk = compare(kind, current, limit, old)
    if command == "update":
        new_baseline[kind] = shrunk
        print(f"{kind}: baseline {len(old)} -> {len(shrunk)} entries")
        problems = [p for p in problems if p.startswith(("NEW", "WORSE"))]
    else:
        print(f"{kind}: {len(current)} measured, limit {limit}, {len(over)} over the limit (baseline {len(old)})")
        for notice in notices:
            print(f"[{kind}] {notice}")
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
