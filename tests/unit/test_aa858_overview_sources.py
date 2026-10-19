"""AA-858 (S243) — guards for the "where does this number come from" tooltips on /admin/overview.

- Every source text on the page lives in ``frontend/app/admin/overview/_components/sources.ts`` and is written in
  plain business words: no table / view names, no snake_case codes, no migration numbers, no account names
  (S243 UI verify, finding 11 rule).
- The overview components never pass an inline string to ``SourceHint`` (so a new number cannot slip in a raw
  table name next to it); they pass a constant from ``sources.ts`` (or the cost module's own plain row hint).
- The kit ``InfoTip`` behind ``SourceHint`` is a real, focusable button tied to its text by ``aria-describedby``.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OVERVIEW = REPO / "frontend" / "app" / "admin" / "overview"
SOURCES_TS = OVERVIEW / "_components" / "sources.ts"
PRIMITIVES = REPO / "frontend" / "app" / "_kit" / "primitives.tsx"

# Things a business reader should never see in a source text.
FORBIDDEN = [
    (re.compile(r"\b[a-z]+_[a-z0-9_]+\b"), "snake_case identifier (table, column or metric name)"),
    (re.compile(r"\b[a-z_]+\.[a-z_]+\b"), "schema.table name"),
    (re.compile(r"\b(?:tp|bk)-\d+\b", re.I), "migration number"),
    (re.compile(r"\bacc\d\b", re.I), "AWS account code"),
    (re.compile(r"\b(?:5XXError|4XXError|GetMetricData|DescribeAlarms|Invocations)\b"), "raw CloudWatch metric name"),
    (re.compile(r"\baa-[a-z]+-"), "AWS resource name"),
]


def _string_literals(ts: str) -> list[str]:
    """Contents of every "…" and `…` literal in a TS file, comments and imports dropped, ${…} holes removed."""
    body = "\n".join(
        line for line in ts.splitlines() if not line.lstrip().startswith(("//", "import ", "*", "/*"))
    )
    out: list[str] = []
    for m in re.finditer(r'`((?:[^`\\]|\\.)*)`|"((?:[^"\\]|\\.)*)"', body, re.S):
        text = m.group(1) if m.group(1) is not None else m.group(2)
        out.append(re.sub(r"\$\{[^}]*\}", " ", text))
    return out


def test_source_texts_are_plain_words():
    texts = _string_literals(SOURCES_TS.read_text(encoding="utf-8"))
    assert len(texts) >= 30, "sources.ts should hold every source text of the page"
    problems = []
    for t in texts:
        for rx, why in FORBIDDEN:
            hit = rx.search(t)
            if hit:
                problems.append(f"{why}: {hit.group(0)!r} in {t[:80]!r}")
    assert not problems, "\n".join(problems)


def test_source_hints_take_constants_not_inline_text():
    offenders = []
    for path in sorted(OVERVIEW.rglob("*.tsx")):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"<SourceHint\s+source=\{?\s*([\"`])", src):
            line = src.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(REPO)}:{line}")
    assert not offenders, "SourceHint with an inline source text (put it in sources.ts): " + ", ".join(offenders)


def test_every_overview_card_imports_its_sources():
    for name in ("AppHealthStrip", "TripPlannerCard", "BookingFunnelCard", "CostByAppCard"):
        src = (OVERVIEW / "_components" / f"{name}.tsx").read_text(encoding="utf-8")
        assert 'from "./sources"' in src, f"{name} must take its source texts from sources.ts"
        assert "source={source}" not in src, f"{name} must not show the backend's engineering source string"
    page = (OVERVIEW / "page.tsx").read_text(encoding="utf-8")
    assert 'from "./_components/sources"' in page


def test_info_tip_is_accessible():
    src = PRIMITIVES.read_text(encoding="utf-8")
    start = src.index("export function InfoTip(")
    body = src[start:src.index("\nexport ", start + 1)]
    for needle in ('type="button"', "aria-describedby={tipId}", 'role="tooltip"', "useId()", '"Escape"',
                   ":focus-visible", "onClick", 'pointerType === "mouse"', 'position: "fixed"'):
        assert needle in body, f"InfoTip lost {needle}"
