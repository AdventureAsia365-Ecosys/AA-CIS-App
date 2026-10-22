#!/usr/bin/env python3
"""AA-879: every accepted Trivy finding in .trivyignore carries an expiry and a reason.

The image scan in the "Docker Build Check" job fails on any fixable CRITICAL/HIGH finding
(`exit-code: 1`, `ignore-unfixed: true`). The only way to accept one is a line in `.trivyignore`:

    # <reason: why it is safe for now, and what removes it>
    CVE-2026-12345 exp:2026-12-31

`exp:` is Trivy's own syntax: after that date Trivy stops ignoring the ID and the scan turns red
again. This check makes the expiry and the reason mandatory and caps how far ahead an expiry may be,
so no ignore is permanent. Stdlib only.

Usage: check_trivyignore.py [path] (default .trivyignore). TODAY_OVERRIDE=YYYY-MM-DD for tests.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

MAX_DAYS_AHEAD = 90
ENTRY = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9._:/-]*)(?P<rest>(\s+\S+)*)\s*$")
EXP = re.compile(r"(?:^|\s)exp:(\d{4}-\d{2}-\d{2})(?:\s|$)")


def check(path: str, today: dt.date) -> list[str]:
    errors: list[str] = []
    if not os.path.exists(path):
        return errors
    prev_comment = False
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                prev_comment = False
                continue
            if line.startswith("#"):
                prev_comment = len(line.lstrip("#").strip()) > 0
                continue
            m = ENTRY.match(line)
            if not m:
                errors.append(f"{path}:{n}: cannot parse '{line}'")
                prev_comment = False
                continue
            vuln = m.group("id")
            exp = EXP.search(line)
            if not exp:
                errors.append(f"{path}:{n}: {vuln} has no 'exp:YYYY-MM-DD' (every ignore must expire)")
            else:
                try:
                    until = dt.date.fromisoformat(exp.group(1))
                except ValueError:
                    errors.append(f"{path}:{n}: {vuln} has an invalid date '{exp.group(1)}'")
                else:
                    if until < today:
                        errors.append(
                            f"{path}:{n}: the ignore for {vuln} expired on {until}"
                            " — fix the image or renew it with a new reason"
                        )
                    elif (until - today).days > MAX_DAYS_AHEAD:
                        errors.append(
                            f"{path}:{n}: {vuln} expires {until}, more than {MAX_DAYS_AHEAD} days ahead"
                        )
            if not prev_comment:
                errors.append(f"{path}:{n}: {vuln} needs a '# reason' comment on the line above")
            prev_comment = False
    return errors


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else ".trivyignore"
    today = dt.date.fromisoformat(os.environ.get("TODAY_OVERRIDE") or dt.date.today().isoformat())
    errors = check(path, today)
    for e in errors:
        print(f"::error::{e}")
    if not errors:
        print(f"{path}: every entry has a reason and an expiry within {MAX_DAYS_AHEAD} days.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
