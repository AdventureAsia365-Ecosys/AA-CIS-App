#!/usr/bin/env python3
"""AA-902 — blocking resource-ID check: this repo is public, so no tracked file may carry an AWS
account ID, an ARN with an account, a VPC resource ID or an API Gateway host.

Patterns (generic, no real ID is written here; the first three mirror the root docs-lint tool
AA-Ecosys tools/docs/check_docs_text.py, the fourth is stricter because the root tool allows it in docs):
  * ARN with account ID      arn:aws:<service>:<region>:<12 digits>:
  * VPC resource ID          sg-/subnet-/vpc-/vpce-/rtb-/igw-/nat-/eni-/acl-/eipalloc-/tgw- + 8 or 17 hex
  * API Gateway host         <10 chars>.execute-api.
  * Account ID in a name     <name>-<12 digits>  (bucket names embed the account: aa-cis-bronze-<account>)
  * 12-digit account ID      any standalone 12-digit number, also after / . or : (UUIDs are
                             blanked first because their last group is 12 hex digits)

AWS documentation example accounts (123456789012, 111122223333, 444455556666, 777788889999) are
allowed: tests use them as fakes.

Exceptions live in .github/resource-id-allowlist.txt, one per line:
  <path> <max hits> <expires YYYY-MM-DD> <reason>
A file on the list may carry at most <max hits> findings until the date; past the date, or with more
findings, the check fails. There is no inline ignore marker.

Usage:
  python3 .github/scripts/check_resource_ids.py               # every tracked text file
  python3 .github/scripts/check_resource_ids.py FILE [...]    # only these files (scratch tests)
Exit 0 = clean, 1 = findings (printed as file:line kind, the ID masked).
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

ALLOWLIST = ".github/resource-id-allowlist.txt"
DOC_EXAMPLE_ACCOUNTS = {"123456789012", "111122223333", "444455556666", "777788889999"}
MAX_BYTES = 2_000_000  # larger files are build output or data, not hand-written source

_HEX_ID = r"[0-9a-f]{8}(?:[0-9a-f]{9})?"
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ARN with account ID", re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:(\d{12}):")),
    ("VPC resource ID", re.compile(
        rf"(?<![\w-])(?:sg|subnet|vpc|vpce|rtb|igw|nat|eni|acl|eipalloc|tgw)-{_HEX_ID}(?![\w-])")),
    ("API Gateway host", re.compile(r"(?<![\w-])[a-z0-9]{10}\.execute-api\.")),
    ("account ID in a resource name", re.compile(r"(?<![\w-])[a-z][a-z0-9]*(?:-[a-z0-9]+)*-(\d{12})(?![\w-])")),
]
# A UUID's last group is 12 hex digits, often all decimal in test fixtures: not an account.
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
STANDALONE_ID = re.compile(r"(?<![\w-])(\d{12})(?![\w-])")


def _mask(text: str) -> str:
    return re.sub(r"\d{12}", lambda m: m.group(0)[:3] + "*" * 9, text)


def scan_line(line: str) -> list[tuple[str, str]]:
    """(kind, masked match) for every finding on one line; doc example accounts are skipped."""
    found: list[tuple[str, str]] = []
    line = UUID.sub("<uuid>", line)
    for kind, pattern in PATTERNS:
        for m in pattern.finditer(line):
            account = m.group(1) if m.groups() else None
            if account in DOC_EXAMPLE_ACCOUNTS:
                continue
            found.append((kind, _mask(m.group(0))))
    if not found:
        for m in STANDALONE_ID.finditer(line):
            if m.group(1) not in DOC_EXAMPLE_ACCOUNTS:
                found.append(("12-digit account ID", _mask(m.group(0))))
    return found


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable: images, fonts, xlsx
    return [(n, kind, frag) for n, line in enumerate(text.splitlines(), 1) for kind, frag in scan_line(line)]


def load_allowlist(root: Path, today: str) -> tuple[dict[str, int], list[str]]:
    allowed: dict[str, int] = {}
    errors: list[str] = []
    path = root / ALLOWLIST
    if not path.exists():
        return allowed, errors
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=3)
        if (len(parts) < 4 or not parts[1].isdigit()
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[2])):
            errors.append(f"{ALLOWLIST}:{n}: expected '<path> <max hits> <expires YYYY-MM-DD> <reason>'")
            continue
        relpath, max_hits, expires, reason = parts
        if expires < today:
            errors.append(f"{ALLOWLIST}:{n}: entry for {relpath} expired on {expires} ({reason}) — "
                          "remove the IDs from the file or renew the entry with a reason")
            continue
        allowed[relpath] = int(max_hits)
    return allowed, errors


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
    return [f for f in out.stdout.decode("utf-8").split("\0") if f]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="files to scan (default: every tracked file)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    today = os.environ.get("TODAY_OVERRIDE") or datetime.date.today().isoformat()

    allowed, errors = load_allowlist(root, today)
    files = args.files or tracked_files(root)
    findings = 0
    for rel in files:
        path = Path(rel) if args.files else root / rel
        hits = scan_file(path)
        if not hits:
            continue
        rel_key = os.path.relpath(path.resolve(), root) if args.files else rel
        limit = allowed.get(rel_key)
        if limit is not None and len(hits) <= limit:
            print(f"allowed until listed date: {rel_key} ({len(hits)}/{limit} findings)")
            continue
        for n, kind, frag in hits:
            findings += 1
            print(f"::error file={rel_key},line={n}::{kind} '{frag}' in a public repo; use a name, a "
                  "placeholder (<acc2>, arn:aws:...:<account>:...) or read it at run time")
    for e in errors:
        print(f"::error::{e}")
    print(f"resource-ids: {len(files)} files checked, {findings} findings, {len(errors)} allowlist errors")
    return 1 if findings or errors else 0


if __name__ == "__main__":
    sys.exit(main())
