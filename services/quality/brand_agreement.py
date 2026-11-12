"""AA-1079 (S277) — judge agreement against the brand labels, and the threshold decision it supports (pure).

The instrument under test says pass / fail (a 0-10 score at a threshold, or a boolean gate); the label says
pass / fail (``brand_labels.label_passes``, strict or lenient). The positive class is FAIL: the judge's job in T7
Debate and the T2 brand-fit score is to catch off-brand text, so recall = off-brand texts the judge caught and
precision = judge fails that the label confirms.

The decision rule follows the bar the policy registry already sets for an uncalibrated brand judge (writing policy
D9: it returns as a gate only at kappa >= 0.6 on >= 40 labelled pairs): ``GATE_KAPPA`` and ``GATE_MIN_PAIRS``. Below
the bar the judge stays a signal (Debate rank only, T2 report only); the decision itself is the tech lead's.
"""
from __future__ import annotations

from typing import Iterable, Optional

GATE_KAPPA = 0.6
GATE_MIN_PAIRS = 40
MIN_PER_CLASS = 5
MODERATE_KAPPA = 0.4
SWEEP = tuple(round(4.0 + 0.5 * i, 1) for i in range(12))        # 4.0 .. 9.5


def _ratio(a: int, b: int) -> Optional[float]:
    return None if b == 0 else round(a / b, 4)


def agreement(pairs: Iterable[tuple[bool, bool]]) -> dict:
    """``pairs`` = (judge_pass, label_pass). Confusion with FAIL as the positive class, accuracy, Cohen's kappa."""
    tp = fp = fn = tn = 0
    for judge_pass, label_pass in pairs:
        if not judge_pass and not label_pass:
            tp += 1
        elif not judge_pass and label_pass:
            fp += 1
        elif judge_pass and not label_pass:
            fn += 1
        else:
            tn += 1
    n = tp + fp + fn + tn
    po = _ratio(tp + tn, n)
    kappa = None
    if n:
        pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (n * n)
        kappa = None if pe == 1 else round(((tp + tn) / n - pe) / (1 - pe), 4)
    return {"n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "accuracy": po, "kappa": kappa,
            "recall_fail": _ratio(tp, tp + fn), "precision_fail": _ratio(tp, tp + fp),
            "label_fail": tp + fn, "label_pass": fp + tn, "judge_fail": tp + fp, "judge_pass": fn + tn}


def sweep(scored: list[tuple[float, bool]], thresholds: Iterable[float] = SWEEP) -> list[dict]:
    """``scored`` = (score, label_pass). One row per threshold: judge pass = score >= threshold."""
    return [{"threshold": t, **agreement((s >= t, lp) for s, lp in scored)} for t in thresholds]


def best_threshold(rows: list[dict], current: float) -> Optional[dict]:
    """The sweep row with the highest kappa (ties: the one closest to ``current``, then the lower one)."""
    ok = [r for r in rows if r.get("kappa") is not None]
    if not ok:
        return None
    return sorted(ok, key=lambda r: (-r["kappa"], abs(r["threshold"] - current), r["threshold"]))[0]


def decision(n: int, label_fail: int, label_pass: int, best: Optional[dict], current: float) -> dict:
    """The proposed threshold decision for one instrument (the tech lead decides): status, proposed threshold, why."""
    if n < GATE_MIN_PAIRS or label_fail < MIN_PER_CLASS or label_pass < MIN_PER_CLASS:
        return {"status": "insufficient_labels", "proposed_threshold": None, "keep": "rank_only",
                "why": (f"{n} labelled pairs ({label_fail} off-brand, {label_pass} on-brand); the bar is "
                        f">= {GATE_MIN_PAIRS} pairs with >= {MIN_PER_CLASS} of each. Add labels before any cut.")}
    if best is None:
        return {"status": "no_signal", "proposed_threshold": None, "keep": "rank_only",
                "why": "the judge gives one answer for every text: kappa is undefined."}
    # A pass/fail gate (T10 F9) has no score to cut on: its agreement row carries no threshold (AA-1079 S278:
    # the first measure with enough labels crashed here on KeyError).
    k, t = best["kappa"], best.get("threshold")
    if k >= GATE_KAPPA:
        if t is None:
            return {"status": "calibrated", "proposed_threshold": None, "keep": "gate_allowed",
                    "why": f"kappa {k} reaches {GATE_KAPPA} on {n} pairs: the pass/fail gate agrees with the labels."}
        return {"status": "calibrated", "proposed_threshold": t, "keep": "cut_allowed",
                "why": (f"kappa {k} at threshold {t} (current {current}) reaches {GATE_KAPPA} on {n} pairs: the cut "
                        f"may come back at {t} (T7_DEBATE_MODE=cut, t7.brand_fit_cut = {t}).")}
    level = "moderate" if k >= MODERATE_KAPPA else "weak"
    at = "" if t is None else f" at threshold {t}"
    return {"status": f"not_calibrated_{level}", "proposed_threshold": t, "keep": "rank_only",
            "why": (f"best kappa {k}{at} (current {current}) is below {GATE_KAPPA}: keep Debate rank "
                    f"only and the T2 brand-fit score report only.")}
