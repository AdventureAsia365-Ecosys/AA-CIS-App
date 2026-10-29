"""AA-919 S257 — the judge calibration report: pure ($0, no I/O) helpers shared by the read-only kappa diagnosis
(`scripts/aa919/kappa_diag.py`) and the judge-model comparison harness (`scripts/aa919/judge_compare.py`).

The R2 runner itself only needs `eval_checks.cohen_kappa` / `kappa_by_stratum`; this module adds what a human reads
when the gate says "not calibrated": the confusion matrix, the recall of each label class, kappa_max, leave-one-out
range (a few items decide the gate?), score AUC (does the 0-10 score separate the labels even when the verdict does
not?) and the per-stratum rows. Inputs are verdicts, scores, codes and sample ids, never text.

`calibration_verdict(row)` reads the judge verdict of one stored calibration pair from an `eval_item_result` row as
`r2.py` writes it (`details.calibration` = "same_row" | "only" | {verdict, score, failure_codes, ...}). It is the
part of the S256 diag script that crashed on run 39096169: the script reused one variable for the judge SCORE and the
judge SOURCE text, so `float()` got the source.
"""
import json
from collections import Counter
from typing import Any, Iterable, Optional

from services.writing_memory import eval_checks as ec


def _j(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _score(v: Any) -> Optional[float]:
    """A judge score as a float; None for a missing or non-numeric value (never raises)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def calibration_verdict(row: dict) -> Optional[dict]:
    """{verdict, score, codes, claims} of the calibration pair stored on one control `rejudge` row, or None when the
    row is a planned control with no calibration (an unlabelled item). `same_row` / `only`: the row's own verdict is
    the frozen text's; a dict: a later release, whose row holds the control's verdict and the dict the frozen one's."""
    det = _j(row.get("details")) or {}
    if not isinstance(det, dict):
        det = {}
    cal = det.get("calibration")
    if cal in ("same_row", "only"):
        verdict, score, codes = row.get("verdict"), row.get("score"), row.get("failure_codes")
        claims = det.get("judge_claims")
    elif isinstance(cal, dict):
        verdict, score, codes = cal.get("verdict"), cal.get("score"), cal.get("failure_codes")
        claims = cal.get("judge_claims")
    else:
        return None
    if verdict not in ("pass", "fail"):
        return None
    return {"verdict": verdict, "score": _score(score), "codes": [str(c) for c in (codes or [])],
            "claims": [c for c in (claims or []) if isinstance(c, dict)]}


def hidden_from_judge(ctx: Any, judge_source: str) -> dict[str, int]:
    """Which panel-read fields of a T9 golden input (scope, angle why_it_works, goal description, brand core idea)
    the judge's SOURCE does not hold: {field: chars}. Empty = source parity. Matching on the first 60 chars after
    whitespace folding (the source clips long sections at a line / word boundary)."""
    if not isinstance(ctx, dict):
        return {}
    goal = ctx.get("goal")
    probe = {"scope": ctx.get("scope"), "why_it_works": (ctx.get("chosen") or {}).get("why_it_works"),
             "goal": goal.get("description") if isinstance(goal, dict) else None,
             "core_idea": (ctx.get("brand_audience") or {}).get("core_idea")}
    src = " ".join(str(judge_source or "").split())
    return {k: len(str(v)) for k, v in probe.items() if v and " ".join(str(v).split())[:60] not in src}


def score_auc(scores_pass: Iterable[float], scores_fail: Iterable[float]) -> Optional[float]:
    """P(score of a pass-labelled text > score of a fail-labelled text), ties 0.5; None without both. 0.5 = the score
    does not separate the labels at all."""
    sp, sf = list(scores_pass), list(scores_fail)
    if not sp or not sf:
        return None
    tot = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in sp for b in sf)
    return round(tot / (len(sp) * len(sf)), 3)


def confusion(pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """label -> judge counts: pass_pass, pass_fail, fail_fail, fail_pass."""
    cm = Counter((a, b) for a, b in pairs if a in ("pass", "fail") and b in ("pass", "fail"))
    return {"pass_pass": cm[("pass", "pass")], "pass_fail": cm[("pass", "fail")],
            "fail_fail": cm[("fail", "fail")], "fail_pass": cm[("fail", "pass")]}


def loo_kappa_range(pairs: list[tuple[str, str]]) -> Optional[tuple[float, float]]:
    """(min, max) kappa with one pair left out: a wide range = a few items decide the gate."""
    vals = [k for k in (ec.cohen_kappa(pairs[:i] + pairs[i + 1:]) for i in range(len(pairs))) if k is not None]
    return (min(vals), max(vals)) if vals else None


def score_threshold_kappas(records: Iterable[dict], thresholds=(4, 5, 6, 7, 8, 9)) -> dict[int, Optional[float]]:
    """Counterfactual: kappa if the verdict were `pass iff score >= t` (no model call)."""
    recs = [r for r in records if r.get("score") is not None]
    return {t: ec.cohen_kappa((r["label"], "pass" if r["score"] >= t else "fail") for r in recs) for t in thresholds}


def report(records: Iterable[dict]) -> dict:
    """The calibration report of one judge on one set of pairs. `records` = [{sample, label, judge, score, codes,
    strata}]. Counts, ratios and sample ids only."""
    recs = [r for r in records if r.get("label") in ("pass", "fail") and r.get("judge") in ("pass", "fail")]
    pairs = [(r["label"], r["judge"]) for r in recs]
    kappa = ec.cohen_kappa(pairs)
    labels = ec.label_counts(pairs)
    strata = ec.kappa_by_stratum(recs)
    sp = [r["score"] for r in recs if r["label"] == "pass" and r.get("score") is not None]
    sf = [r["score"] for r in recs if r["label"] == "fail" and r.get("score") is not None]
    loo = loo_kappa_range(pairs)
    return {"pairs": len(pairs), "labels": labels, "kappa": kappa, "kappa_max": ec.kappa_max(pairs),
            "counts": ec.judge_counts(kappa, len(pairs), labels), "confusion": confusion(pairs),
            "pass_recall": strata[0]["pass_recall"], "fail_recall": strata[0]["fail_recall"],
            "loo_kappa": list(loo) if loo else None, "score_auc": score_auc(sp, sf),
            "score_threshold_kappa": score_threshold_kappas(recs),
            "codes": dict(Counter(c for r in recs for c in r.get("codes") or [])),
            "codes_on_pass_labels": dict(Counter(c for r in recs if r["label"] == "pass"
                                                 for c in r.get("codes") or [])),
            "disagreements": sorted(r.get("sample") or "?" for r in recs if r["label"] != r["judge"]),
            "strata": strata, "strata_below": ec.strata_below(strata)}


def strata_lines(rows: Iterable[dict]) -> list[str]:
    """Plain-text lines of `kappa_by_stratum` rows for a terminal report."""
    out = []
    for r in rows:
        k = "-" if r.get("kappa") is None else f"{r['kappa']:.3f}"
        flag = " BELOW" if r.get("below") else ("" if r.get("measurable") else " (too few to measure)")
        out.append(f"  {r['dim'] + ':' + r['value']:52} n={r['n']:3} pass/fail={r['labels']['pass']}/"
                   f"{r['labels']['fail']}  kappa={k}  recall pass={r.get('pass_recall')} fail={r.get('fail_recall')}"
                   f"{flag}")
    return out


__all__ = ["calibration_verdict", "confusion", "hidden_from_judge", "loo_kappa_range", "report", "score_auc",
           "score_threshold_kappas", "strata_lines"]
