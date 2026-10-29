#!/usr/bin/env python3
"""CI "Migration preflight" (AA-993 M4b, root ADR 0017 decision 1; design D2.1; critic points 7, 17).

Tells the author of a PR that adds a migration whether the file will be auto-applied (expand) or needs a person
(contract / destructive), and fails the PR on the cases that would leave Dev stuck:

  1. an edit, delete or rename of a migration file already copied to AA-CIS-Infra (copied = immutable; a revert is
     code-only, skill aa-ship);
  2. a new file whose number is not the owner ``main`` max + 1 (then + 2, … for several files). The merge queue
     re-runs this check on the latest ``main``. A deliberate lower number (a reserved one) needs the PR label
     ``migration:out-of-order``: it is copied with that label and never auto-applied;
  3. a readiness raise to a key whose file is not expand: contract and destructive files keep the MIG-4 order
     (migration-only PR -> manual DB Migrate apply -> code PR), otherwise every Deploy Dev of this app waits at
     ``schema-gate`` until a person applies the file;
  4. a readiness raise to a key with no migration file in the PR head;
  5. a lint error of the pinned AA-CIS-Infra runner (``run_migration.py --inspect``: directives, the expand static
     lint, the X1 role lint) or of its ownership lint, and an expand file the classify replay finds changing an
     existing object (exit 14 of ``db/expand_check.py --classify``, the evidence the Infra copy PR's required
     "Expand classify" check would also refuse).

  python3 .github/scripts/migration_preflight.py --app booking --migrations-dir migrations \\
      --readiness-file backend/app/db.py --base "$BASE_SHA" [--infra-tools infra] [--infra-copies DIR] \\
      [--classify-json classify.json --classify-status 0] [--allow-out-of-order]

Readiness keys use the same reading as ``schema_gate.py`` (booking / crm: ``REQUIRED_SCHEMA_VERSIONS``; cis:
``LEGACY_CIS_BASELINE`` + 1 … ``REQUIRED_CIS_SCHEMA``). Without ``--infra-tools`` only the header is read (no lint);
without ``--infra-copies`` every file on the base commit counts as copied (fail closed). Without classify evidence a
file is expand when its header says so and the lint passes; the copy PR's classify and Migration Apply's re-classify
stay the authority, and ``schema-gate`` is the backstop. Exit 1 on any refusal, 0 otherwise. Standard library only;
identical file in AA-CIS-App, AA-Booking and AA-Partner; tests: the repo's unit test ``test_migration_preflight.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema_gate as sg  # noqa: E402  (same readiness reading as the deploy gate)

FILE_RE = re.compile(r"^([0-9]{3})_[^/]*\.sql$")
DIRECTIVE_RE = re.compile(r"^\s*--\s*migrate:\s*(.+?)\s*$", re.IGNORECASE)
INFRA_DIR = {"cis": "cis", "booking": "booking", "crm": "crm"}     # db/migrations/<dir>/ in AA-CIS-Infra


@dataclass
class FileReport:
    name: str
    key: str
    header: str = "none"                # expand | destructive | none
    lint_errors: list = field(default_factory=list)
    lint_warnings: list = field(default_factory=list)
    classify: str = ""                  # expand | contract | "" (no evidence)
    findings: list = field(default_factory=list)

    @property
    def klass(self) -> str:
        if self.header == "destructive":
            return "destructive"
        if self.header == "expand" and not self.lint_errors and self.classify != "contract":
            return "expand"
        return "contract"


@dataclass
class Result:
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    files: list = field(default_factory=list)
    raised: list = field(default_factory=list)


# ── pure parts ──────────────────────────────────────────────────────────────────────────────────────────────────────


def header_of(sql: str) -> str:
    """Light reading of the leading ``-- migrate:`` block (the pinned runner is the authority when present)."""
    flags = set()
    for line in sql.splitlines():
        s = line.strip()
        if s and not s.startswith("--"):
            break
        m = DIRECTIVE_RE.match(line)
        if m:
            flags.add(re.split(r"\s+--", m.group(1), maxsplit=1)[0].strip().lower())
    if "destructive" in flags:
        return "destructive"
    return "expand" if "expand" in flags else "none"


def parse_name_status(text: str) -> dict:
    """``git diff --name-status --no-renames`` lines -> {"A": [base names], "M": [...], "D": [...]} (file names)."""
    out: dict = {"A": [], "M": [], "D": []}
    for line in text.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        name = Path(path.strip()).name
        if not FILE_RE.match(name):
            continue
        kind = status.strip()[:1]
        out.setdefault("M" if kind in ("M", "T") else kind, []).append(name)
    return out


def number_errors(base_names: list, added: list, allow_out_of_order: bool) -> tuple:
    """(errors, notes) for the numbers of the added files against the base max."""
    errors, notes = [], []
    base_numbers = [int(FILE_RE.match(n).group(1)) for n in base_names if FILE_RE.match(n)]
    expected = max(base_numbers, default=0) + 1
    seen: set = set()
    for name in sorted(added):
        n = int(FILE_RE.match(name).group(1))
        if n in seen or n in base_numbers:
            errors.append(f"`{name}`: number {n:03d} is already taken on main")
        elif n == expected:
            expected += 1
        elif n < expected and allow_out_of_order:
            notes.append(f"`{name}`: out of order (label migration:out-of-order); it is copied with that label and "
                         "never auto-applied: a person applies it with DB Migrate")
        else:
            errors.append(f"`{name}`: number {n:03d} is not the next one ({expected:03d} = main max + 1). Renumber "
                          "it on the latest main; a deliberate lower (reserved) number needs the PR label "
                          "migration:out-of-order")
        seen.add(n)
    return errors, notes


def immutable_errors(changes: dict, copies: set | None) -> tuple:
    errors, notes = [], []
    for kind, verb in (("M", "edits"), ("D", "deletes or renames")):
        for name in changes.get(kind, []):
            if copies is None or name in copies:
                where = "already copied to AA-CIS-Infra" if copies is not None else "on main (the Infra copy list " \
                    "was not readable, so it counts as copied)"
                errors.append(f"this PR {verb} `{name}`, {where}: a copied migration is immutable. Undo the change; "
                              "fix forward with a new file, and revert code without touching the file (skill aa-ship)")
            else:
                notes.append(f"this PR {verb} `{name}`, which is on main but not copied to AA-CIS-Infra yet; the "
                             "migration-sync bot copies main within 10 min, so expect the Copy Guard to refuse it")
    return errors, notes


def assess(app: str, base_names: list, head_names: list, changes: dict, base_required: set, head_required: set,
           reports: dict, copies: set | None, allow_out_of_order: bool = False) -> Result:
    r = Result()
    r.errors, r.notes = immutable_errors(changes, copies)
    e, n = number_errors(base_names, changes.get("A", []), allow_out_of_order)
    r.errors += e
    r.notes += n
    r.raised = sorted(head_required - base_required, key=sg.key_number)
    head_keys = {sg.norm_key(app, FILE_RE.match(x).group(1)): x for x in head_names if FILE_RE.match(x)}
    added_keys = {sg.norm_key(app, FILE_RE.match(x).group(1)) for x in changes.get("A", [])}
    for key in r.raised:
        if key not in head_keys:
            r.errors.append(f"readiness now requires {key}, but this PR has no migration file for it")
    for name in sorted(changes.get("A", [])):
        rep = reports[name]
        r.files.append(rep)
        for err in rep.lint_errors:
            r.errors.append(f"`{name}`: {err}")
        if rep.header == "expand" and rep.classify == "contract":
            r.errors.append(f"`{name}` has the expand header but the classify replay found it changing an existing "
                            f"object: {'; '.join(rep.findings[:5]) or 'see the classify log'}. Change it, or drop the "
                            "header and ship it as a contract file (MIG-4)")
        if rep.key in r.raised and rep.klass != "expand":
            r.errors.append(f"`{name}` is {rep.klass} and this PR also raises readiness to {rep.key}: a "
                            f"{rep.klass} file keeps the MIG-4 order. Ship the migration alone, let a person apply it "
                            "with DB Migrate, then raise readiness in the code PR")
        if rep.key not in head_required:
            r.notes.append(f"`{name}` ({rep.key}) is not required by readiness: Deploy Dev does not wait for it, so "
                           "code must not depend on it until a PR raises readiness")
    stale = [k for k in r.raised if k in head_keys and k not in added_keys]
    if stale:
        r.notes.append(f"readiness raised to {', '.join(stale)} (files already on main): Deploy Dev waits until "
                       "they are registered on Dev")
    return r


def verdict_line(rep: FileReport) -> str:
    if rep.klass == "expand":
        return ("auto-applied by AA-CIS-Infra Migration Apply after the bot's copy PR merges (when DB_AUTO_APPLY is "
                "true; otherwise one manual DB Migrate apply)")
    return "copied by the bot, never auto-applied: a person applies it with DB Migrate (MIG-4)"


def summary_markdown(r: Result) -> str:
    out = ["### Migration preflight", ""]
    if r.files:
        out += ["| file | key | header | lint | classify | class | apply |", "|---|---|---|---|---|---|---|"]
        for f in r.files:
            lint = "errors" if f.lint_errors else ("warnings" if f.lint_warnings else "ok")
            out.append(f"| `{f.name}` | {f.key} | {f.header} | {lint} | {f.classify or 'no evidence'} | "
                       f"**{f.klass}** | {verdict_line(f)} |")
        out.append("")
    if r.raised:
        out.append(f"- readiness raised to: {', '.join(r.raised)}")
    out += [f"- note: {n}" for n in r.notes]
    out += [f"- **refused:** {e}" for e in r.errors]
    if not r.files and not r.raised and not r.errors:
        out.append("- no migration added and no readiness change: nothing to check")
    return "\n".join(out) + "\n"


# ── I/O ─────────────────────────────────────────────────────────────────────────────────────────────────────────────


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def inspect_with_infra(app: str, name: str, sql: str, tools: Path) -> tuple:
    """(header, errors, warnings) from the pinned Infra runner and ownership lint, on a copy at db/migrations/<dir>/."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "db" / "migrations" / INFRA_DIR[app] / name
        path.parent.mkdir(parents=True)
        path.write_text(sql, encoding="utf-8")
        p = subprocess.run([sys.executable, str(tools / "db" / "run_migration.py"), "--inspect", str(path)],
                           capture_output=True, text=True)
        try:
            data = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return header_of(sql), [f"Infra --inspect gave no result: {(p.stderr or p.stdout).strip()[:300]}"], []
        header = "destructive" if data.get("destructive") else ("expand" if data.get("expand") else "none")
        errors, warnings = list(data.get("errors") or []), list(data.get("warnings") or [])
        o = subprocess.run([sys.executable, str(tools / "db" / "check_migration_ownership.py"), str(path)],
                           capture_output=True, text=True)
        for line in o.stdout.splitlines():
            if line.startswith("FAIL"):
                errors.append("ownership: " + line.split(": ", 1)[-1])
        if o.returncode not in (0, 1):
            errors.append(f"ownership lint crashed: {o.stderr.strip()[:300]}")
    return header, errors, warnings


def classify_entries(path: str, status: int | None, app: str) -> tuple:
    """({file name: (verdict, findings)}, note) from db/expand_check.py --out JSON."""
    if not path or not Path(path).is_file():
        return {}, "no classify evidence (replay not run or no result): header + lint decide here, the copy PR's " \
                   "Expand classify decides in AA-CIS-Infra"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data.get("files") if isinstance(data, dict) else data
    out = {}
    for e in entries or []:
        owner, _, name = str(e.get("file", "")).partition("/")
        if owner != INFRA_DIR[app]:
            continue
        ok = (e.get("header") == "expand" and not e.get("lint_errors") and not e.get("findings")
              and not e.get("error"))
        if e.get("error") and not e.get("findings"):
            continue                    # a replay error is no evidence either way
        out[name] = ("expand" if ok else "contract", list(e.get("findings") or []) + list(e.get("lint_errors") or []))
    note = "" if status in (0, 14, None) else f"classify exited {status}: files it could not rehearse have no evidence"
    return out, note


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--app", required=True, choices=sorted(INFRA_DIR))
    p.add_argument("--migrations-dir", required=True)
    p.add_argument("--readiness-file", required=True)
    p.add_argument("--base", required=True, help="base commit (PR base or merge-group base)")
    p.add_argument("--infra-tools", default="", help="AA-CIS-Infra checkout at the pinned SHA")
    p.add_argument("--infra-copies", default="", help="AA-CIS-Infra main db/migrations/<owner>/ (a directory)")
    p.add_argument("--classify-json", default="")
    p.add_argument("--classify-status", type=int, default=None)
    p.add_argument("--allow-out-of-order", action="store_true")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    a = p.parse_args(argv)

    mig = a.migrations_dir.rstrip("/")
    base_names = [Path(x).name for x in git("ls-tree", "--name-only", f"{a.base}:{mig}").split()]
    head_names = sorted(f.name for f in Path(mig).glob("*.sql"))
    changes = parse_name_status(git("diff", "--name-status", "--no-renames", a.base, "HEAD", "--", mig))
    try:
        base_src = git("show", f"{a.base}:{a.readiness_file}")
        base_required = sg.readiness_keys(a.app, base_src)
    except (subprocess.CalledProcessError, ValueError):
        base_required = set()
    head_required = sg.readiness_keys(a.app, Path(a.readiness_file).read_text(encoding="utf-8"))
    copies = None
    if a.infra_copies and Path(a.infra_copies).is_dir():
        copies = {f.name for f in Path(a.infra_copies).glob("*.sql")}
    tools = Path(a.infra_tools) if a.infra_tools and (Path(a.infra_tools) / "db" / "run_migration.py").is_file() \
        else None
    verdicts, classify_note = classify_entries(a.classify_json, a.classify_status, a.app)

    reports = {}
    for name in changes.get("A", []):
        sql = (Path(mig) / name).read_text(encoding="utf-8")
        rep = FileReport(name, sg.norm_key(a.app, FILE_RE.match(name).group(1)))
        if tools is not None:
            rep.header, rep.lint_errors, rep.lint_warnings = inspect_with_infra(a.app, name, sql, tools)
        else:
            rep.header = header_of(sql)
        if name in verdicts:
            rep.classify, rep.findings = verdicts[name]
        reports[name] = rep

    r = assess(a.app, base_names, head_names, changes, base_required, head_required, reports, copies,
               a.allow_out_of_order)
    if changes.get("A"):
        if tools is None:
            r.notes.append("the pinned AA-CIS-Infra tools were not available: header only, no runner lint")
        if classify_note:
            r.notes.append(classify_note)
    if (changes.get("M") or changes.get("D")) and copies is None:
        r.notes.append("the AA-CIS-Infra copy list was not readable: every file on main counts as copied")
    md = summary_markdown(r)
    print(md)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    for e in r.errors:
        print(f"::error::Migration preflight: {e}")
    return 1 if r.errors else 0


if __name__ == "__main__":
    if shutil.which("git") is None:
        print("::error::git is needed")
        sys.exit(2)
    sys.exit(main())
