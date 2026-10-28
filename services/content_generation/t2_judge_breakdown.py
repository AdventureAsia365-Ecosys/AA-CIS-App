"""AA-795 (report 12 F-f) — the judge breakdown a T2 version records (`tenant_tour_versions.judge_breakdown`, CIS 300).

Before this, a T2 version kept one number (`quality_score`) and the judge's reasons lived nowhere an admin could read:
`llm_call_log.quality_signal` holds numbers only, and `job.result` is runner state (retention deletes it, a rerun
overwrites it). The breakdown is the kept draft's verdict: brand fit, cross-brand distinctness, mission, the judge's
feedback, the gate it was scored under, and the **binding axis** — which number set the stored score.

Two pieces:
  * ``axes(...)`` — called by ``judge_node`` on a T2 draft; the numbers only the judge node knows (validate's score
    before the min, the judge score before a tie-break, the mission cap). Carried in graph state as
    ``judge_t2_axes`` (declared in ContentState, part of ``t2_repair.DRAFT_KEYS``).
  * ``build(...)`` — called by the T2 job right before the save, from the kept draft's ``_rewrite_tour`` result.

Admin-only data: the column is in ``_tenant_projection.TENANT_HIDDEN_VERSION_FIELDS`` and its key matches
``TENANT_FORBIDDEN_KEY_RE`` ("judge").
"""
from __future__ import annotations

from typing import Any, Optional

VERSION = 1
FEEDBACK_MAX = 1200

# Binding axes, in tie order: on a tie the earlier one is reported (a mission cap equal to brand fit changed nothing).
BRAND_FIT, CROSS_BRAND, MISSION, TIEBREAK, VALIDATE, UNJUDGED = (
    "brand_fit", "cross_brand", "mission", "tiebreak", "validate", "unjudged")


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def judge_axis(*, gate: str, brand_fit: float, distinct: Optional[float], mission_present: Optional[bool],
               has_mission: bool, mission_cap: Optional[float]) -> str:
    """Which judge number set the judge score under `gate` (judge_node.t2_judge_score's rule)."""
    candidates: list[tuple[str, float]] = [(BRAND_FIT, float(brand_fit))]
    if gate == "combined" and distinct is not None:
        candidates.append((CROSS_BRAND, float(distinct)))
    if gate in ("brand_fit_mission", "combined") and has_mission and not mission_present \
            and mission_cap is not None:
        candidates.append((MISSION, float(mission_cap)))
    low = min(v for _, v in candidates)
    return next(name for name, v in candidates if v == low)


def axes(*, gate: str, brand_fit: float, distinct: Optional[float], mission_present: Optional[bool],
         has_mission: bool, mission_cap: Optional[float], mission_source: Optional[str],
         judge_score_raw: float, judge_score: float, validate_score: Optional[float],
         tiebreak: Optional[dict]) -> dict:
    """The `judge_t2_axes` state value of one judged T2 draft. `judge_score_raw` is the gate's score before the
    tie-break, `judge_score` after it; the draft's score is min(validate_score, judge_score)."""
    binding = judge_axis(gate=gate, brand_fit=brand_fit, distinct=distinct, mission_present=mission_present,
                         has_mission=has_mission, mission_cap=mission_cap)
    if tiebreak and _num(judge_score) != _num(judge_score_raw):
        binding = TIEBREAK
    if validate_score is not None and float(validate_score) < float(judge_score):
        binding = VALIDATE
    return {"gate": gate, "validate_score": _num(validate_score), "judge_score_raw": _num(judge_score_raw),
            "mission_cap": _num(mission_cap) if has_mission else None, "mission_source": mission_source,
            "binding": binding}


def build(result: dict, *, final_score: Optional[float], score_source: str = "rewrite",
          brand_claims: Optional[dict] = None, chosen_attempt: Optional[int] = None) -> dict:
    """The `judge_breakdown` a T2 version stores, from the kept draft's `_rewrite_tour` result.

    `final_score` is the stored `quality_score` (`score_source` = "source_fallback" when the rewrite scored 0 and the
    job fell back to the A1 score). A draft the judge never scored (no brand signals, judge outage) is `judged:
    false`, binding `unjudged`."""
    r = result or {}
    ax = r.get("judge_t2_axes") if isinstance(r.get("judge_t2_axes"), dict) else {}
    judged = r.get("judge_brand_fit") is not None
    out: dict = {
        "version": VERSION,
        "judged": judged,
        "brand_fit": _num(r.get("judge_brand_fit")),
        "cross_brand_distinct": _num(r.get("judge_cross_brand_distinct")),
        "mission_present": r.get("judge_mission_present"),
        "judge_score": _num(r.get("judge_score")),
        "validate_score": ax.get("validate_score"),
        "final_score": _num(final_score),
        "score_source": score_source,
        "gate": ax.get("gate"),
        "mission_source": ax.get("mission_source"),
        "binding": ax.get("binding") if judged and ax.get("binding") else UNJUDGED,
        "feedback": (str(r.get("judge_feedback") or "")[:FEEDBACK_MAX]) or None,
    }
    if ax.get("judge_score_raw") is not None and ax.get("judge_score_raw") != out["judge_score"]:
        out["judge_score_raw"] = ax["judge_score_raw"]
    if ax.get("mission_cap") is not None:
        out["mission_cap"] = ax["mission_cap"]
    if chosen_attempt is not None:
        out["attempt"] = chosen_attempt
    if brand_claims is not None:
        out["brand_claims"] = brand_claims
    return out
