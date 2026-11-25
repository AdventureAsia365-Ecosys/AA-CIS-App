#!/usr/bin/env python3
"""The admin frontend's npm audit gate: fail on any high or critical advisory, unless it is accepted with a reason
and an expiry. AA-1111 (S294): a copy of AA-Partner .github/scripts/npm_audit_gate.py (only these two lines differ).

`npm audit --audit-level=high` cannot accept one advisory, so an advisory with no patched version (S284:
GHSA-fc5q-39xc-vjpf, picomatch ReDoS, every version affected) blocked every pull request. This gate reads
`npm audit --json` and walks each high / critical package down to the advisories that cause it. An advisory passes only
when `.github/npm-audit-allow.json` lists it:

    {"allow": [{"id": "GHSA-xxxx-xxxx-xxxx", "expires": "YYYY-MM-DD", "packages": ["name", ...],
                "reason": "why it is safe for now, and what removes it"}]}

An entry needs a non-empty reason, an expiry at most MAX_DAYS_AHEAD days ahead and the packages it may affect; on the
expiry day it stops counting and the gate turns red again, so no acceptance is permanent (same rule as .trivyignore,
AA-879). The advisory still fails when it reaches a package outside `packages` (a new path, e.g. a runtime one).

Fails closed: a report that is not a version-2 npm audit report (npm error, network failure, empty output), a high or
critical package whose cause cannot be traced to an advisory, and a count that differs from the report's own
metadata all fail.
Stdlib only.

Usage: npm audit --json | npm_audit_gate.py [allowlist] (default .github/npm-audit-allow.json).
TODAY_OVERRIDE=YYYY-MM-DD for tests.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

MAX_DAYS_AHEAD = 90
FAILING = ("high", "critical")
GHSA = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}")


NOT_FAILING = ("info", "low", "moderate")


def load_allow(path: str, today: dt.date) -> tuple[dict[str, tuple[dt.date, set[str]]], list[str], list[str]]:
    """-> (advisory id -> (expiry, packages), errors, notes). Expired entries are left out with a note."""
    if not os.path.exists(path):
        return {}, [], []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    allowed: dict[str, tuple[dt.date, set[str]]] = {}
    errors: list[str] = []
    notes: list[str] = []
    for i, e in enumerate(data.get("allow", [])):
        where = f"{path} entry {i + 1}"
        aid, reason, exp, pkgs = str(e.get("id", "")), e.get("reason"), e.get("expires"), e.get("packages")
        if not GHSA.fullmatch(aid):
            errors.append(f"{where}: id must be a GHSA id, got {aid!r}")
            continue
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{where} ({aid}): a reason (text) is required")
            continue
        if not isinstance(pkgs, list) or not pkgs or not all(isinstance(p, str) and p for p in pkgs):
            errors.append(f"{where} ({aid}): packages must list the package names it may affect")
            continue
        try:
            expires = dt.date.fromisoformat(str(exp))
        except ValueError:
            errors.append(f"{where} ({aid}): expires must be YYYY-MM-DD, got {exp!r}")
            continue
        if (expires - today).days > MAX_DAYS_AHEAD:
            errors.append(f"{where} ({aid}): expires {expires} is more than {MAX_DAYS_AHEAD} days ahead")
            continue
        if expires > today:
            allowed[aid] = (expires, set(pkgs))
        else:
            notes.append(f"{aid}: allowlist entry expired on {expires}, no longer accepted")
    return allowed, errors, notes


def report_problems(report: object) -> list[str]:
    """Why the input is not a usable npm audit (v2) report; empty when it is."""
    if not isinstance(report, dict):
        return ["the npm audit output is not a JSON object"]
    if "error" in report:
        return [f"npm audit failed: {str(report.get('message') or report['error'])[:300]}"]
    if report.get("auditReportVersion") != 2:
        return [f"not an npm audit v2 report (auditReportVersion={report.get('auditReportVersion')!r})"]
    if not isinstance(report.get("vulnerabilities"), dict):
        return ["the report has no vulnerabilities object"]
    meta = (report.get("metadata") or {}).get("vulnerabilities")
    if not isinstance(meta, dict):
        return ["the report has no metadata.vulnerabilities counts"]
    listed = sum(1 for v in report["vulnerabilities"].values() if v.get("severity") in FAILING)
    expected = sum(int(meta.get(k) or 0) for k in FAILING)
    if listed != expected:
        return [f"the report lists {listed} high/critical packages but its metadata counts {expected}"]
    return []


def failing_advisories(report: dict) -> dict[str, set[str]]:
    """-> advisory id -> the packages it makes high / critical (walking `via` chains to the advisories)."""
    vulns = report.get("vulnerabilities", {})
    found: dict[str, set[str]] = {}

    def walk(name: str, seen: set[str]) -> set[str]:
        if name in seen or name not in vulns:
            return set()
        seen.add(name)
        ids: set[str] = set()
        for via in vulns[name].get("via", []):
            if isinstance(via, dict):
                if via.get("severity") not in NOT_FAILING:
                    m = GHSA.search(str(via.get("url", "")))
                    ids.add(m.group(0) if m else str(via.get("url") or via.get("source")))
            else:
                ids |= walk(str(via), seen)
        return ids

    for name, v in vulns.items():
        if v.get("severity") in FAILING:
            ids = walk(name, set())
            for aid in ids or {f"UNRESOLVED:{name}"}:
                found.setdefault(aid, set()).add(name)
    return found


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else ".github/npm-audit-allow.json"
    today = dt.date.fromisoformat(os.environ.get("TODAY_OVERRIDE") or dt.date.today().isoformat())
    allowed, errors, notes = load_allow(path, today)
    for n in notes:
        print(f"note {n}")
    try:
        report = json.load(sys.stdin)
        errors.extend(report_problems(report))
    except json.JSONDecodeError as exc:
        report = None
        errors.append(f"the npm audit output is not JSON ({exc})")
    blocking = 0
    found = failing_advisories(report) if not errors else {}
    for aid, pkgs in sorted(found.items()):
        entry = allowed.get(aid)
        outside = sorted(pkgs - entry[1]) if entry else []
        if entry and not outside:
            print(f"accepted {aid} until {entry[0]} ({len(pkgs)} packages): {', '.join(sorted(pkgs))}")
        else:
            blocking += 1
            why = f" (not in its allowlist packages: {', '.join(outside)})" if entry else ""
            print(f"FAIL {aid}{why} ({len(pkgs)} packages): {', '.join(sorted(pkgs))}")
    for e in errors:
        print(f"FAIL {e}")
    if blocking or errors:
        print(f"npm audit gate: {blocking} advisory(ies) not accepted, {len(errors)} report or allowlist error(s)")
        return 1
    print("npm audit gate: no unaccepted high or critical advisory")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
