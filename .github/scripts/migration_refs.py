#!/usr/bin/env python3
"""Dangling migration references (AA-1024 l22, S271 lessons guard G10).

A migration renumbered on the latest main (lane 316 -> main max + 1 = 314, lessons S259 / S265) keeps its name, so the
tests, docs and comments that named the old ``316_name.sql`` still pass a ``\\b316\\b`` search for the new number and
are missed: 12-file and 6-file rework rounds. This check reads the lines a PR ADDS (``git diff -U0 BASE HEAD``, every
file) and fails on a ``NNN_name`` / ``NNN_name.sql`` token that names

  - a migration of this owner whose file is ``MMM_name.sql`` on the PR head, with MMM != NNN (a renumber left behind), or
  - a file of this owner the PR removes or renames (on the base, gone on the head).

A token whose name is not a migration of this owner (another owner's file, a fixture name) is never checked, and a
line that really means the old number (a history note) carries ``old-mig-ok``.

    python3 .github/scripts/migration_refs.py --migrations-dir api/migrations --base "$BASE_SHA"

Exit 1 on a dangling reference, 0 otherwise. Standard library only; identical file in AA-CIS-App, AA-Booking,
AA-Partner (called from migration_preflight.py) and AA-TripPlanner-Web (its own CI step).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

FILE_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
REF_RE = re.compile(r"(?<![\w])(\d{3})_([a-z][a-z0-9_]*)(?:\.sql)?(?![\w])")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
MARKER = "old-mig-ok"


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """(path, line number on the head, text) of every added line of a ``git diff -U0`` text."""
    out, path, line, header = [], None, 0, False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            header, path = True, None
            continue
        if header:      # file header lines until the first hunk ("--- a/x", "+++ b/x", index, mode, Binary ...)
            if raw.startswith("+++ "):
                target = raw[4:].strip()
                path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            m = HUNK_RE.match(raw)
            if m:
                header, line = False, int(m.group(1))
            continue
        m = HUNK_RE.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            out.append((path, line, raw[1:]))
            line += 1
    return out


def dangling(lines: list[tuple[str, int, str]], head_names: list[str], base_names: list[str]) -> list[str]:
    """Error lines for the tokens that name a renumbered or removed migration of this owner."""
    by_name: dict[str, set] = {}
    for n in head_names:
        m = FILE_RE.match(n)
        if m:
            by_name.setdefault(m.group(2), set()).add(m.group(1))
    head, gone = set(head_names), {n for n in base_names if FILE_RE.match(n)} - set(head_names)
    out = []
    for path, num, text in lines:
        if MARKER in text:
            continue
        for m in REF_RE.finditer(text):
            number, name = m.group(1), m.group(2)
            fname = f"{number}_{name}.sql"
            if fname in head:
                continue
            if name in by_name:
                now = ", ".join(f"{x}_{name}.sql" for x in sorted(by_name[name]))
                out.append(f"{path}:{num}: `{m.group(0)}` names migration number {number}, but the file is {now} "
                           f"(renumbered?). Rename the reference, or mark a history note with {MARKER}")
            elif fname in gone:
                out.append(f"{path}:{num}: `{m.group(0)}` names {fname}, which this PR removes or renames")
    return out


def git(*args: str) -> str:
    """git's stdout as text. Bytes that are not UTF-8 (a Latin-1 fixture, a binary-ish file git prints as text)
    become U+FFFD instead of crashing the check: only ``NNN_name`` tokens matter, and they are ASCII."""
    out = subprocess.run(["git", *args], check=True, capture_output=True).stdout
    return out.decode("utf-8", errors="replace")


def check(migrations_dir: str, base: str) -> list[str]:
    mig = migrations_dir.rstrip("/")
    head_names = sorted(f.name for f in Path(mig).glob("*.sql"))
    try:
        base_names = [Path(x).name for x in git("ls-tree", "--name-only", f"{base}:{mig}").split()]
    except subprocess.CalledProcessError:
        base_names = []
    diff = git("-c", "core.quotePath=false", "diff", "-U0", "--no-color", "--no-ext-diff", base, "HEAD")
    return dangling(added_lines(diff), head_names, base_names)


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--migrations-dir", required=True)
    p.add_argument("--base", required=True, help="base commit (PR base or merge-group base)")
    a = p.parse_args(argv)
    errors = check(a.migrations_dir, a.base)
    for e in errors:
        print(f"::error::Migration references: {e}")
    if not errors:
        print("migration references: no added line names a renumbered or removed migration")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
