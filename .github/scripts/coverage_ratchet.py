#!/usr/bin/env python3
"""AA-879 coverage ratchet for the "Unit Tests" job.

Reads the line rate from a Cobertura report (pytest --cov-report=xml) and the ratchet from
.github/coverage-ratchet.json:

- fails when the measured total is below `floor` (the hard minimum);
- fails when this change LOWERS `floor` compared with the base branch (`--base <rev>`): the floor
  only moves up;
- warns (does not fail) when the total is below the monthly goal
  (`baseline + monthly_step x whole months since set_on`, capped at `cap`);
- prints a notice when the total is at least floor + 1, so the next PR can raise the floor.

Stdlib only. TODAY_OVERRIDE=YYYY-MM-DD for tests.
Usage: coverage_ratchet.py coverage.xml [--config .github/coverage-ratchet.json] [--base <git rev>]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET


def measured_percent(xml_path: str) -> float:
    root = ET.parse(xml_path).getroot()
    return float(root.attrib["line-rate"]) * 100.0


def months_between(start: dt.date, end: dt.date) -> int:
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(0, months)


def monthly_goal(cfg: dict, today: dt.date) -> float:
    start = dt.date.fromisoformat(cfg["set_on"])
    goal = cfg.get("baseline", cfg["floor"]) + cfg.get("monthly_step", 0) * months_between(start, today)
    return min(goal, cfg.get("cap", 100))


def base_floor(base: str, config_path: str) -> float | None:
    try:
        raw = subprocess.run(
            ["git", "show", f"{base}:{config_path}"], check=True, capture_output=True, text=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None  # the file is new on this branch, or no base available
    return float(json.loads(raw)["floor"])


def evaluate(total: float, cfg: dict, today: dt.date, prior_floor: float | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    floor = float(cfg["floor"])
    if prior_floor is not None and floor < prior_floor:
        errors.append(f"coverage floor lowered from {prior_floor:g} to {floor:g}; the ratchet only goes up")
    if total < floor:
        errors.append(f"coverage {total:.2f}% is below the floor {floor:g}%")
    goal = monthly_goal(cfg, today)
    if total < goal:
        notes.append(f"::warning::coverage {total:.2f}% is below this month's goal {goal:g}% (floor {floor:g}%)")
    if total >= floor + 1:
        notes.append(
            f"::notice::coverage {total:.2f}% is at least floor + 1: raise 'floor' in .github/coverage-ratchet.json"
        )
    return errors, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xml")
    ap.add_argument("--config", default=".github/coverage-ratchet.json")
    ap.add_argument("--base", default=None)
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    today = dt.date.fromisoformat(os.environ.get("TODAY_OVERRIDE") or dt.date.today().isoformat())
    total = measured_percent(args.xml)
    prior = base_floor(args.base, args.config) if args.base else None
    errors, notes = evaluate(total, cfg, today, prior)

    print(f"coverage {total:.2f}% | floor {cfg['floor']}% | goal this month {monthly_goal(cfg, today):g}%")
    for n in notes:
        print(n)
    for e in errors:
        print(f"::error::{e}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(
                f"### Coverage ratchet\n\n- Measured: {total:.2f}%\n- Floor: {cfg['floor']}%\n"
                f"- Goal this month: {monthly_goal(cfg, today):g}%\n"
                f"- Result: {'FAIL' if errors else 'pass'}\n"
            )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
