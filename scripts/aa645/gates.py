"""AA-645 — which step-1 code-check codes are GATES for the S1 writer screen and the A/B runs, defined once.

Gate = a code with ~100% precision on the 50 golden pages (step-1 report): a page with one is not publishable, so a
writer must reach ~0 before its panel score counts. Report-only = a code that is measured and shown, never a gate
(DAY_PLACE_MISMATCH is a DAY_* code but report-only: 11/50 golden pages, mostly titles naming the next day's stop).
Not a page fault = the source's own problem (SOURCE_DAY_CONTRADICTION) or a skipped check.

Pure, no imports: `score_screen.py` runs on a laptop without the app's dependencies. The day-oracle names are
pinned against `services/quality/day_oracle.py` by tests/unit/test_aa645_screen.py.
"""
from __future__ import annotations

DAY_GATE_CODES = frozenset({
    "DAY_SUBDAY_SPLIT", "DAY_OUTPUT_DUPLICATE", "DAY_OUTPUT_ORDER", "DAY_OUTPUT_GAP", "DAY_OUTPUT_EXTRA",
    "DAY_DEPARTURE_MIDTRIP", "DAY_COUNT_VS_DURATION",
})
GATE_CODES = DAY_GATE_CODES | frozenset({
    "META_CUT", "META_LENGTH", "TEXT_TRUNCATED", "NUMBER_NOT_IN_SOURCE", "INCLUSION_CONTRADICTED",
})
REPORT_CODES = frozenset({"NUMBER_NOUN_MISMATCH", "NAME_NOT_IN_SOURCE", "DAY_PLACE_MISMATCH"})
NOT_A_PAGE_FAULT = frozenset({"SOURCE_DAY_CONTRADICTION", "DAY_CHECK_SKIPPED"})


def is_gate(code: str) -> bool:
    """True for a gate code. A DAY_* code the oracle adds later is a gate unless it is listed as report-only."""
    if code in GATE_CODES:
        return True
    return code.startswith("DAY_") and code not in REPORT_CODES and code not in NOT_A_PAGE_FAULT


def is_report(code: str) -> bool:
    return code in REPORT_CODES


def gate_label(code: str) -> str:
    """The summary bucket of a gate code (every day-structure gate counts as one DAY_* bucket)."""
    return "DAY_*" if code.startswith("DAY_") else code


def page_gate_codes(findings) -> set[str]:
    """The gate codes of one page's findings."""
    return {f["code"] for f in findings or [] if is_gate(f["code"])}
