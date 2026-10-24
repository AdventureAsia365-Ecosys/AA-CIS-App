#!/usr/bin/env python3
"""AA-926 (Quality System v1, design §4.6): mark quality-relevant changes.

Reads services/quality/paths.yaml, lists the files this change touches on those paths, the tiers they
belong to (Q0 / Q2 / R0 / Q3) and a sha256 over the sorted (path, blob sha) pairs. The hash is what a
later `quality/q2` status will carry so a PASS survives fixups outside the quality paths (AA-919).

Report only: writes the step summary and a ::notice, always exits 0. No labels, no statuses, no
`services` import (standard library + PyYAML).

Base commit: BASE_SHA (pull_request.base.sha) or BEFORE_SHA (push event.before); when that is empty or
the all-zero SHA (a branch's first push) it falls back to `git merge-base origin/main HEAD`.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
import sys

ZERO = "0" * 40
TIERS = ("Q0", "Q2", "R0", "Q3")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def _base() -> str:
    for var in ("BASE_SHA", "BEFORE_SHA"):
        sha = (os.environ.get(var) or "").strip()
        if sha and sha != ZERO:
            try:
                _git("cat-file", "-e", f"{sha}^{{commit}}")
                return sha
            except subprocess.CalledProcessError:
                pass
    return _git("merge-base", "origin/main", "HEAD")


def _blob(path: str) -> str:
    try:
        return _git("rev-parse", f"HEAD:{path}")
    except subprocess.CalledProcessError:
        return "deleted"


def main() -> int:
    import yaml

    with open("services/quality/paths.yaml", encoding="utf-8") as fh:
        entries = (yaml.safe_load(fh) or {}).get("paths") or []
    base = _base()
    changed = [p for p in _git("diff", "--name-only", f"{base}...HEAD").splitlines() if p]
    matched: dict[str, set[str]] = {}
    for entry in entries:
        tiers = {t for t, v in (entry.get("tiers") or {}).items() if v and t in TIERS}
        for path in changed:
            if any(fnmatch.fnmatchcase(path, g) for g in entry.get("globs") or []):
                matched.setdefault(path, set()).update(tiers)
    pairs = sorted(f"{p} {_blob(p)}" for p in matched)
    digest = hashlib.sha256("\n".join(pairs).encode("utf-8")).hexdigest() if pairs else "none"
    touched = [t for t in TIERS if any(t in s for s in matched.values())]
    lines = ["### Quality paths (AA-926, report only)", "",
             f"Base `{base[:12]}`, {len(changed)} changed file(s), {len(matched)} on quality paths.", "",
             f"- Tiers touched: **{', '.join(touched) or 'none'}**",
             f"- Quality-path diff hash: `{digest}`", ""]
    if matched:
        lines += ["| Path | Tiers |", "|---|---|"]
        lines += [f"| `{p}` | {', '.join(t for t in TIERS if t in matched[p])} |" for p in sorted(matched)]
        lines.append("")
    text = "\n".join(lines)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    print(f"::notice title=quality-paths::tiers={','.join(touched) or 'none'} hash={digest}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # report only — never fail the job
        print(f"::warning title=quality-paths::could not compute the quality-path report: {e}")
        sys.exit(0)
