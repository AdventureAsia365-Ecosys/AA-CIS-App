#!/usr/bin/env python3
"""AA-925 (Q-2) — Q0 "red on base": every NEW labelled case of a PR must FAIL on the branch it merges into.

Design §6.1 step (c): a fix starts from a case that is red on the base tip, so the corpus only grows with
cases that prove a real gap (a case green on base tests nothing the base did not already do). Also: a truth
label cannot be rewritten in passing — deleting a case or changing an existing case's label needs the
``q0-relabel`` PR label (and an independent review, which the orchestrator asks for).

Run in CI (ci.yml, job "Unit Tests", pull_request only) with the PR merge ref checked out at fetch-depth 2:

    python .github/scripts/q0_red_on_base.py --base "$(git rev-parse HEAD^1)"

``HEAD^1`` of a pull_request merge commit is the base-branch tip the PR merges into. Steps:

1. Read the case files (tests/unit/quality/cases/*.json) at HEAD (the working tree) and at the base
   (``git show``). No corpus on the base → print ``bootstrap`` and exit 0 (the PR that adds the corpus).
2. ``new = head − base``, ``removed = base − head``, ``relabelled`` = same id whose label fields changed
   (``expected``, ``class_id``, ``detector``, ``severity``, ``chain``, ``expect``, ``input``, ``llm_stub``), plus
   changed or removed shared fixtures (``_fixtures.json``), plus a changed harness file (``HARNESS_FILES``:
   the runners, their stubs and ``problems()`` decide what a label means). Any of these without the label → fail.
3. ``git worktree add <tmp>/base <base>``; copy HEAD's tests/unit/quality/ and tests/unit/conftest.py over it.
4. In that worktree, print and check that ``services`` imports from the base worktree, then run pytest on
   the node ids ``tests/unit/quality/test_q0_corpus.py::test_case[<id>]`` with ``--junitxml``.
5. Every new case must have a junit ``<failure>``. An ``<error>``, a skip, a pass or a missing test case is
   NOT red: the script fails and names the case (``green`` / ``inconclusive``).
6. Exemption: ``red_on_base: false`` only for a negative twin (``expected`` in no_fire/keep) whose ``twin_of``
   is a NEW case of the same PR with a positive label (fire/drop).

The PR labels come from the ``PR_LABELS`` env var (a JSON list, set from the event payload in ci.yml — never
interpolated into the shell). Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

CASES_DIR = "tests/unit/quality/cases"
FIXTURES = "_fixtures.json"
CORPUS_TEST = "tests/unit/quality/test_q0_corpus.py"
RELABEL_LABEL = "q0-relabel"
LABEL_FIELDS = ("expected", "class_id", "detector", "severity", "chain", "expect", "input", "llm_stub")
# The harness decides what a label MEANS (the runners and their stubs, ``problems()``, the corpus test): an edit
# there can weaken every case without touching a case file, so it needs the label too (review of PR 3).
HARNESS_FILES = ("tests/unit/quality/chain.py", "tests/unit/quality/test_q0_corpus.py")
POSITIVE = ("fire", "drop")
NEGATIVE = ("no_fire", "keep")


def git(*args: str, cwd: str | None = None, check: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def _parse_files(files: dict[str, str]) -> tuple[dict[str, dict], dict]:
    cases: dict[str, dict] = {}
    fixtures: dict = {}
    for name, text in sorted(files.items()):
        data = json.loads(text)
        if name == FIXTURES:
            fixtures = data
            continue
        if name.startswith("_"):
            continue
        for c in data:
            if c["id"] in cases:
                raise SystemExit(f"duplicate case id {c['id']!r} ({name})")
            cases[c["id"]] = c
    return cases, fixtures


def head_corpus(root: str = ".") -> tuple[dict[str, dict], dict] | None:
    d = Path(root) / CASES_DIR
    if not d.is_dir():
        return None
    return _parse_files({p.name: p.read_text(encoding="utf-8") for p in d.glob("*.json")})


def base_corpus(base: str, root: str = ".") -> tuple[dict[str, dict], dict] | None:
    listing = git("ls-tree", "--name-only", f"{base}:{CASES_DIR}", cwd=root, check=False)
    names = [n for n in listing.split() if n.endswith(".json")]
    if not names:
        return None
    return _parse_files({n: git("show", f"{base}:{CASES_DIR}/{n}", cwd=root) for n in names})


def diff(head: dict[str, dict], base: dict[str, dict], head_fx: dict, base_fx: dict) -> dict:
    new = sorted(set(head) - set(base))
    removed = sorted(set(base) - set(head))
    relabelled = sorted(i for i in set(head) & set(base)
                        if any(head[i].get(f) != base[i].get(f) for f in LABEL_FIELDS))
    fixtures_changed = sorted(k for k in base_fx if head_fx.get(k) != base_fx[k])
    return {"new": new, "removed": removed, "relabelled": relabelled, "fixtures_changed": fixtures_changed}


def harness_changed(base: str, root: str = ".") -> list[str]:
    """Harness files whose text differs between the base and HEAD (a file new at HEAD with no base copy is the
    bootstrap, not a change)."""
    out = []
    for rel in HARNESS_FILES:
        before = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=root, capture_output=True, text=True)
        if before.returncode != 0:
            continue
        p = Path(root) / rel
        if not p.exists() or p.read_text(encoding="utf-8") != before.stdout:
            out.append(rel)
    return out


def exempt(case: dict, head: dict[str, dict], new: set[str]) -> bool:
    if case.get("red_on_base", True) is not False:
        return False
    twin = head.get(case.get("twin_of") or "")
    return (case.get("expected") in NEGATIVE and twin is not None and twin["id"] in new
            and twin.get("expected") in POSITIVE)


def junit_outcomes(xml_path: str, ids: list[str]) -> dict[str, str]:
    """case id → failure | error | skipped | passed | missing, read from pytest's junit xml."""
    out = {i: "missing" for i in ids}
    if not os.path.exists(xml_path):
        return out
    root = ET.parse(xml_path).getroot()
    for tc in root.iter("testcase"):
        name = tc.get("name") or ""
        if not name.startswith("test_case[") or not name.endswith("]"):
            continue
        cid = name[len("test_case["):-1]
        if cid not in out:
            continue
        kinds = {child.tag for child in tc}
        out[cid] = ("error" if "error" in kinds else "failure" if "failure" in kinds
                    else "skipped" if "skipped" in kinds else "passed")
    return out


def run_on_base(base: str, ids: list[str], root: str = ".", python: str = sys.executable) -> dict[str, str]:
    tmp = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    wt = tempfile.mkdtemp(prefix="q0-base-", dir=tmp)
    shutil.rmtree(wt)
    git("worktree", "add", "--detach", wt, base, cwd=root)
    try:
        src_q = Path(root) / "tests/unit/quality"
        dst_q = Path(wt) / "tests/unit/quality"
        if dst_q.exists():
            shutil.rmtree(dst_q)
        shutil.copytree(src_q, dst_q, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(Path(root) / "tests/unit/conftest.py", Path(wt) / "tests/unit/conftest.py")
        env = {**os.environ, "PYTHONPATH": wt, "PYTHONDONTWRITEBYTECODE": "1"}
        where = subprocess.run([python, "-c", "import services, os; print(os.path.realpath(services.__file__))"],
                               cwd=wt, env=env, capture_output=True, text=True)
        loc = where.stdout.strip()
        print(f"base checkout {wt}: services imports from {loc or where.stderr.strip()}")
        if not loc.startswith(os.path.realpath(wt) + os.sep):
            raise SystemExit("services is not imported from the base worktree — the red-on-base run would "
                             "test the PR's own code")
        xml = os.path.join(wt, "q0-base-junit.xml")
        nodes = [f"{CORPUS_TEST}::test_case[{i}]" for i in ids]
        subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *nodes],
                       cwd=wt, env=env)
        return junit_outcomes(xml, ids)
    finally:
        git("worktree", "remove", "--force", wt, cwd=root, check=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Q0: every new labelled case must fail on the base tip")
    ap.add_argument("--base", required=True, help="base-branch tip SHA (HEAD^1 of the PR merge ref)")
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with pytest's output in the CI log
    labels = set(json.loads(os.environ.get("PR_LABELS") or "[]") or [])

    head = head_corpus(a.root)
    if head is None:
        print("no Q0 corpus at HEAD — nothing to check")
        return 0
    base = base_corpus(a.base, a.root)
    if base is None:
        print("bootstrap: the base has no Q0 corpus (this PR adds it) — red-on-base starts with the next PR")
        return 0
    (head_cases, head_fx), (base_cases, base_fx) = head, base
    d = diff(head_cases, base_cases, head_fx, base_fx)
    d["harness_changed"] = harness_changed(a.base, a.root)
    print(json.dumps({k: v for k, v in d.items()}, indent=1))
    rc = 0
    touched = d["removed"] or d["relabelled"] or d["fixtures_changed"] or d["harness_changed"]
    if touched and RELABEL_LABEL not in labels:
        print(f"FAIL: existing truth labels changed {d['relabelled']}, removed {d['removed']}, shared fixtures "
              f"changed {d['fixtures_changed']}, harness changed {d['harness_changed']} — add the "
              f"'{RELABEL_LABEL}' label (an independent review follows)")
        rc = 1
    elif touched:
        print(f"'{RELABEL_LABEL}' label present: label changes accepted for independent review")
    new = set(d["new"])
    to_run = [i for i in d["new"] if not exempt(head_cases[i], head_cases, new)]
    exempted = [i for i in d["new"] if i not in to_run]
    wrong_exempt = [i for i in d["new"] if head_cases[i].get("red_on_base", True) is False
                    and not exempt(head_cases[i], head_cases, new)]
    if wrong_exempt:
        print(f"FAIL: red_on_base:false is only for a negative twin of a NEW positive case: {wrong_exempt}")
        rc = 1
    if exempted:
        print(f"exempt negative twins (not run on base): {exempted}")
    if not to_run:
        print("no new case to run on the base")
        return rc
    outcomes = run_on_base(a.base, to_run, a.root)
    green = [i for i, o in outcomes.items() if o == "passed"]
    inconclusive = {i: o for i, o in outcomes.items() if o not in ("failure", "passed")}
    red = [i for i, o in outcomes.items() if o == "failure"]
    print(f"red on base: {red}")
    if green:
        print(f"FAIL: new case(s) GREEN on the base tip (they prove nothing the base lacks): {green}")
        rc = 1
    if inconclusive:
        print(f"FAIL: new case(s) inconclusive on the base (an error / skip / missing is not red): {inconclusive}")
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
