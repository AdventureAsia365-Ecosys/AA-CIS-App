#!/usr/bin/env python3
"""AA-756 — offline estimate of the `a1_claim_supported` input-token trim.

Pure Python, no DB and no network: it reuses the grounding module's own `sentence_units`,
`should_skip`, `source_text` and `unit_source` (the SAME code the pipeline runs, so the estimate
cannot drift from behaviour) to count, per a JSON list of ``{"tour": {...}, "generated": {...}}``:

  - units before/after the skip rule (how many sentences Jev is asked),
  - total source characters before/after (the whole-tour source once per asked unit vs the
    per-unit source) — a proxy for the input-token cut on this question.

Usage:
    python scripts/aa756_grounding_tokens.py path/to/samples.json

The samples JSON is a list; each item is {"tour": <raw source dict>, "generated": <grounded dict>}.
``estimate`` is importable and unit-tested; running the file as a script just prints the totals.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.content_generation.grounding import (  # noqa: E402
    sentence_units,
    should_skip,
    source_text,
    unit_source,
)


def estimate(samples: list[dict[str, Any]]) -> dict[str, int]:
    """Count units and source characters before/after the AA-756 trim over ``samples``.

    Before: every unit is asked, each with the whole-tour ``source_text`` → source chars =
    len(source_text) * units. After: skipped units are not asked, and each asked unit uses its
    ``unit_source`` → source chars = sum(len(unit_source)) over asked units."""
    units_before = units_after = chars_before = chars_after = 0
    for item in samples:
        tour = item.get("tour") or {}
        generated = item.get("generated") or {}
        units = sentence_units(generated)
        full = source_text(tour)
        for u in units:
            units_before += 1
            chars_before += len(full)
            if should_skip(u["sentence"]):
                continue
            units_after += 1
            chars_after += len(unit_source(tour, u))
    return {
        "units_before": units_before,
        "units_after": units_after,
        "units_skipped": units_before - units_after,
        "chars_before": chars_before,
        "chars_after": chars_after,
    }


def _pct(before: int, after: int) -> float:
    return 0.0 if before == 0 else round(100.0 * (before - after) / before, 1)


def _format(stats: dict[str, int]) -> str:
    lines = [
        f"units before skip : {stats['units_before']}",
        f"units after skip  : {stats['units_after']}  "
        f"(skipped {stats['units_skipped']}, -{_pct(stats['units_before'], stats['units_after'])}%)",
        f"source chars before: {stats['chars_before']}",
        f"source chars after : {stats['chars_after']}  "
        f"(-{_pct(stats['chars_before'], stats['chars_after'])}%)",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/aa756_grounding_tokens.py <samples.json>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        samples = json.load(fh)
    print(_format(estimate(samples)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
