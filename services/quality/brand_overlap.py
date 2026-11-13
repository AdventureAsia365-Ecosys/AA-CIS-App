"""AA-1079 (S277) — deterministic cross-brand overlap check: how much of the same tour two brands wrote alike.

No LLM. The judge's ``cross_brand_distinct`` is one model's estimate from one draft and gave 6.0 to every version of
two brands (AA-741 phase 3 §5.2); the audit measured distinctness instead with word 5-gram Jaccard between the brands'
versions of one tour (8 rewritten tours: mean 0.18, 0.09-0.37). This module is that measurement as code, plus the
split the audit could not make: the overlap that is NOT in the source (the Master text both brands rewrote), so text
both keep from the Master (place names, day titles, meals) is told apart from text one brand shares with another.

- ``tour_text(content)``: the text of a T2 ``rewritten_content`` / Master row (every string value, status keys out).
- ``shingles(text, n)``: the set of word n-grams (lower case, letters / digits / apostrophes).
- ``cross_brand_overlap({brand: text}, source=None)``: per brand pair the Jaccard, the Jaccard beyond the source and
  each side's share copied from the source; the pairs at or above the warn line (``t2.cross_brand_overlap_warn`` in
  services/quality/thresholds.yaml, uncalibrated) and identical texts (Jaccard >= ``IDENTICAL``: a policy hold keeps
  the Master verbatim by design, so it is reported, not judged).
"""
from __future__ import annotations

import itertools
import re
from statistics import mean
from typing import Any, Optional

from services.quality.registry import threshold

SHINGLE_N = 5
IDENTICAL = 0.95
def default_warn_line() -> float:
    """thresholds.yaml t2.cross_brand_overlap_warn; S279 L2: a setting, read at call time."""
    return float(threshold("t2.cross_brand_overlap_warn"))
_WORD_RE = re.compile(r"[a-z0-9']+")
_SKIP_KEYS = frozenset({"status", "trip_type"})


def tour_text(content: Any) -> str:
    """Every string value of a nested content object, in order (dict keys and status-like keys left out)."""
    out: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                if k not in _SKIP_KEYS:
                    walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(content)
    return "\n".join(out)


def shingles(text: str, n: int = SHINGLE_N) -> frozenset:
    words = _WORD_RE.findall((text or "").lower())
    return frozenset(tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1)))


def jaccard(a: frozenset, b: frozenset) -> Optional[float]:
    return None if not a or not b else len(a & b) / len(a | b)


def containment(a: frozenset, b: frozenset) -> Optional[float]:
    """Share of ``a``'s shingles that are also in ``b``."""
    return None if not a or not b else len(a & b) / len(a)


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 4)


def cross_brand_overlap(texts: dict[str, str], source: Optional[str] = None, *, n: int = SHINGLE_N,
                        warn_line: Optional[float] = None) -> dict:
    """The overlap check for one tour written by several brands (pure). ``texts``: brand -> text; ``source``: the
    text they all rewrote (optional). Brands with no words are listed under ``empty`` and left out of the pairs."""
    line = default_warn_line() if warn_line is None else float(warn_line)
    sh = {b: shingles(t, n) for b, t in sorted(texts.items())}
    empty = sorted(b for b, s in sh.items() if not s)
    sh = {b: s for b, s in sh.items() if s}
    src = shingles(source, n) if source else frozenset()
    pairs = []
    for a, b in itertools.combinations(sorted(sh), 2):
        j = jaccard(sh[a], sh[b])
        beyond = jaccard(sh[a] - src, sh[b] - src) if src else None
        pairs.append({"pair": f"{a}~{b}", "jaccard": _r(j), "jaccard_beyond_source": _r(beyond),
                      "identical": bool(j is not None and j >= IDENTICAL),
                      "warn": bool(j is not None and j >= line and j < IDENTICAL)})
    copied = {b: _r(containment(s, src)) for b, s in sh.items()} if src else {}
    vals = [p["jaccard"] for p in pairs if p["jaccard"] is not None]
    return {"shingle": n, "warn_line": line, "brands": sorted(sh), "empty": empty, "pairs": pairs,
            "copied_from_source": copied,
            "jaccard_mean": _r(mean(vals)) if vals else None, "jaccard_max": _r(max(vals)) if vals else None,
            "warn_pairs": [p["pair"] for p in pairs if p["warn"]],
            "identical_pairs": [p["pair"] for p in pairs if p["identical"]],
            "verdict": ("identical" if any(p["identical"] for p in pairs)
                        else "overlap" if any(p["warn"] for p in pairs) else "distinct" if pairs else "n/a")}


def overlap_report(tours: dict[str, dict], sources: Optional[dict[str, str]] = None, *,
                   n: int = SHINGLE_N) -> dict:
    """Many tours (pure): ``tours`` = tour key -> {brand: text}; ``sources`` = tour key -> source text. Per tour the
    check, per brand pair the mean over tours, and the totals with and without identical (policy-hold) tours."""
    per_tour = {t: cross_brand_overlap(by, (sources or {}).get(t), n=n) for t, by in sorted(tours.items())}
    per_pair: dict[str, list] = {}
    for rec in per_tour.values():
        if rec["identical_pairs"]:
            continue
        for p in rec["pairs"]:
            if p["jaccard"] is not None:
                per_pair.setdefault(p["pair"], []).append(p["jaccard"])
    rewritten = [p["jaccard"] for rec in per_tour.values() if not rec["identical_pairs"] for p in rec["pairs"]
                 if p["jaccard"] is not None]
    return {"shingle": n, "warn_line": default_warn_line(), "tours": per_tour,
            "per_pair_mean": {k: _r(mean(v)) for k, v in sorted(per_pair.items())},
            "rewritten": {"tours": sum(1 for r in per_tour.values() if not r["identical_pairs"] and r["pairs"]),
                          "pairs": len(rewritten), "jaccard_mean": _r(mean(rewritten)) if rewritten else None,
                          "jaccard_max": _r(max(rewritten)) if rewritten else None},
            "identical_tours": sorted(t for t, r in per_tour.items() if r["identical_pairs"]),
            "warn_tours": sorted(t for t, r in per_tour.items() if r["warn_pairs"])}
