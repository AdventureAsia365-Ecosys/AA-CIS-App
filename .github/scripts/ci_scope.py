#!/usr/bin/env python3
"""What a pull request's CI run needs (S271, CI minutes): the scope outputs that gate the jobs of ci.yml.

    python3 .github/scripts/ci_scope.py --event "$EVENT" [--base REV] \
        --area frontend=frontend/ --area integration=api/migrations/,tests/integration/ ...

Writes `<name>=true|false` lines to $GITHUB_OUTPUT (when set) and a short report to stdout:

- `tests`: false only for a docs-only pull request (every changed path is under docs/ or ends in .md);
- one output per `--area NAME=PREFIX[,PREFIX...]`: true when a changed path is a PREFIX (a file) or starts with it
  (a PREFIX ending in "/" is a folder).

Fail closed: every output is true for any event other than pull_request (merge_group, workflow_dispatch: the full
run), when the diff cannot be read, when it is empty, and when the pull request changes anything under .github/
(workflows and CI scripts are exercised by the full run). A job gated on an output tests `!= 'false'`, so a failed or
skipped scope step (empty output) runs the job too.

On pull_request the checkout is GitHub's merge commit: its first parent is the base tip, so `HEAD^1..HEAD` is exactly
what the merge adds (the default --base). Standard library only.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

ALWAYS_FULL = ".github/"


def is_docs(path: str) -> bool:
    """A path whose change cannot affect code: the docs folder or a Markdown file anywhere."""
    return path.startswith("docs/") or path.endswith(".md")


def touches(path: str, prefixes: list[str]) -> bool:
    return any(path.startswith(p) if p.endswith("/") else path == p for p in prefixes)


def decide(event: str, files: list[str] | None, areas: dict[str, list[str]]) -> tuple[dict[str, bool], str]:
    """The outputs and the reason. ``files`` None = the diff could not be read."""
    names = ["tests", *areas]
    if event != "pull_request":
        return {n: True for n in names}, f"{event or 'no event'}: every job runs"
    if files is None:
        return {n: True for n in names}, "the diff could not be read: every job runs"
    if not files:
        return {n: True for n in names}, "empty diff: every job runs"
    if any(f.startswith(ALWAYS_FULL) for f in files):
        return {n: True for n in names}, f"changes under {ALWAYS_FULL}: every job runs"
    out = {"tests": not all(is_docs(f) for f in files)}
    for name, prefixes in areas.items():
        out[name] = out["tests"] and any(touches(f, prefixes) for f in files)
    why = "docs-only pull request" if not out["tests"] else f"{len(files)} changed path(s)"
    return out, why


def changed_files(base: str) -> list[str] | None:
    try:
        res = subprocess.run(["git", "diff", "--name-only", "--no-renames", base, "HEAD"],
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"::warning::git diff {base} HEAD failed ({exc}): every job runs")
        return None
    return [line for line in res.stdout.splitlines() if line.strip()]


def parse_area(spec: str) -> tuple[str, list[str]]:
    name, sep, rest = spec.partition("=")
    prefixes = [p.strip() for p in rest.split(",") if p.strip()]
    if not sep or not name.isidentifier() or name == "tests" or not prefixes:
        raise argparse.ArgumentTypeError(f"--area must be NAME=PREFIX[,PREFIX...] (NAME not 'tests'), got {spec!r}")
    return name, prefixes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--event", default="", help="github.event_name")
    ap.add_argument("--base", default="HEAD^1", help="the base commit of the diff (default: the merge's first parent)")
    ap.add_argument("--area", action="append", default=[], type=parse_area, help="NAME=PREFIX[,PREFIX...]")
    a = ap.parse_args(argv)
    areas = dict(a.area)
    files = changed_files(a.base) if a.event == "pull_request" else []
    out, why = decide(a.event, files, areas)
    lines = [f"{k}={'true' if v else 'false'}" for k, v in out.items()]
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print(f"CI scope: {why}")
    for line in lines:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
