"""AA-793 — T2 best draft wins + targeted repair (S235 report 12 §2.3 F-c, F-d).

S235 measured on 14 tenant rewrites: the job saved the LAST draft, not the best (three versions lost
a 7.0 draft, one was saved at 3.0), and every repair round was a full rewrite from scratch. This
module holds the switches and the small, pure pieces both layers share:

- the content graph (``graph.build_graph(t2_drafts=True)``, tenant rewrites only): ``track_best_node``
  remembers the highest-scoring draft after each judge; ``restore_best_node`` puts it back when the
  retries run out (the ``hitl`` exit used to keep the last draft); ``increment_retry_t2`` hands that
  best draft to the next writer call so it REVISES it with the feedback instead of starting over;
- the T3 QA gate (``tenant_pipeline.run_t3_qa_gate``): the same "best draft wins" choice across T3
  rounds, a sentence-level repair for a grounding-only failure, and the background-fact allowance.

Switches (env, read per call, worker task def — same pattern as T2_JUDGE_ANCHORS):

- ``T2_BEST_DRAFT``       default ON. Only chooses among drafts already written and scored: no new
                          text, no new LLM call, no gate relaxed. A saved draft can no longer score
                          below an earlier one.
- ``T2_TARGETED_REPAIR``  default ON. A retry revises the best draft (the writer sees it plus the
                          judge/T3 feedback); a grounding-only T3 failure gets A1's sentence repair
                          (``grounding.repair``, stage ``t2_flag_fix``) instead of a whole-tour
                          rewrite. Every output still goes through the same validate / judge / T3
                          checks, and best-draft keeps a worse revision from being saved.
- ``T3_BACKGROUND_FACTS`` default OFF. Lets T3 accept past years, centuries and elevations of named
                          places (``acp_produce.t3_background``). It relaxes a truth gate, so it is
                          switched on deliberately (worker env) for the AA-793 rerun.

"off" / "0" / "false" / "no" turns a default-on switch off; "on" / "1" / "true" / "yes" turns a
default-off switch on. A1 (``is_tenant_rewrite`` false) is never affected by any of them.
"""
from __future__ import annotations

import copy
import json
import os

import structlog

logger = structlog.get_logger()

T2_BEST_DRAFT_ENV = "T2_BEST_DRAFT"
T2_TARGETED_REPAIR_ENV = "T2_TARGETED_REPAIR"
T3_BACKGROUND_FACTS_ENV = "T3_BACKGROUND_FACTS"

_ON = ("1", "true", "on", "yes")
_OFF = ("0", "false", "off", "no")


def _flag(env: str, default: bool) -> bool:
    raw = os.environ.get(env, "").strip().lower()
    if raw in _ON:
        return True
    if raw in _OFF:
        return False
    return default


def best_draft_enabled() -> bool:
    return _flag(T2_BEST_DRAFT_ENV, True)


def targeted_repair_enabled() -> bool:
    return _flag(T2_TARGETED_REPAIR_ENV, True)


def background_facts_enabled() -> bool:
    return _flag(T3_BACKGROUND_FACTS_ENV, False)


def t2_drafts_graph_enabled() -> bool:
    """Whether a tenant rewrite runs the draft-tracking graph variant at all."""
    return best_draft_enabled() or targeted_repair_enabled()


# The fields that make up one judged draft. Restoring a draft restores exactly these (never
# retry_count, cost_usd or token counters, which keep counting the whole run).
DRAFT_KEYS = (
    "generated", "quality_score", "feedback", "failure_codes", "sub_scores", "passed_count",
    "failed_count", "judge_brand_fit", "judge_cross_brand_distinct", "judge_mission_present",
    "judge_feedback", "judge_score", "judge_tiebreak", "model_used", "fallback_used", "satellite_account",
    "itinerary_day_ratios", "itinerary_nudges_used",
)

# The writer-facing fields of a draft (metadata such as seo_keywords_used is not shown back).
_REVISE_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries",
                  "seo_title", "seo_meta", "trip_type")


def _score(state_or_draft: dict) -> float:
    try:
        return float(state_or_draft.get("quality_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def snapshot(state: dict) -> dict:
    snap = {k: copy.deepcopy(state[k]) for k in DRAFT_KEYS if k in state}
    snap["attempt"] = state.get("retry_count", 0)
    return snap


def track_best_node(state: dict) -> dict:
    """After each judge: log the attempt's score and keep the best draft so far (strictly higher
    score wins, so on a tie the earlier draft stays). An empty draft (JSON parse failure) is never
    best."""
    score = _score(state)
    scores = list(state.get("draft_scores") or [])
    scores.append({"attempt": state.get("retry_count", 0), "score": score,
                   "judge_score": state.get("judge_score"),
                   "revision": bool(state.get("previous_draft"))})
    out = {**state, "draft_scores": scores}
    best = state.get("best_draft")
    if state.get("generated") and (not best or score > _score(best)):
        out["best_draft"] = snapshot(state)
    return out


def restore_best_node(state: dict) -> dict:
    """The ``hitl`` exit (retries used up, still below the line): save the best draft, not the last.
    No-op when T2_BEST_DRAFT is off or the last draft is already the best."""
    best = state.get("best_draft")
    if not best_draft_enabled() or not best or _score(best) <= _score(state):
        return state
    logger.info("t2_best_draft_restored", restored_attempt=best.get("attempt"),
                restored_score=_score(best), last_attempt=state.get("retry_count", 0),
                last_score=_score(state), tenant_id=state.get("tenant_id"))
    restored = {k: copy.deepcopy(best[k]) for k in DRAFT_KEYS if k in best}
    return {**state, **restored, "error": "", "best_draft_restored": True}


def increment_retry_t2(state: dict) -> dict:
    """The T2 retry edge: count the retry and, with T2_TARGETED_REPAIR on, give the writer the draft
    to revise — the best one so far (T2_BEST_DRAFT on) or the last one — with that draft's own
    feedback (validate issues + judge feedback, merged by judge_node)."""
    out = {**state, "retry_count": state.get("retry_count", 0) + 1}
    if not (state.get("is_tenant_rewrite") and targeted_repair_enabled()):
        return out
    base = state.get("best_draft") if best_draft_enabled() else None
    base = base or (snapshot(state) if state.get("generated") else None)
    if base and base.get("generated"):
        out["previous_draft"] = copy.deepcopy(base["generated"])
        if base.get("feedback"):
            out["feedback"] = base["feedback"]
    return out


def revise_block(previous_draft: dict | None) -> str:
    """The user-prompt block that turns a retry into a revision of `previous_draft`."""
    if not previous_draft:
        return ""
    draft = {k: previous_draft[k] for k in _REVISE_FIELDS if previous_draft.get(k) not in (None, "", [])}
    if not draft:
        return ""
    return (
        "\n\nYOUR PREVIOUS DRAFT (revise it, do not start over):\n"
        f"{json.dumps(draft, ensure_ascii=False)}\n"
        "Revise this draft: keep every passage the feedback below does not touch, change only what "
        "the feedback asks for, and keep the brand voice. Every rule above still applies. Return the "
        "complete tour in the output format required above."
    )
