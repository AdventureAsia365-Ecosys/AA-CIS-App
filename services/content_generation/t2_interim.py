"""S271 W0 (root ADR 0020, AA-900 l16) — the T2 interim profile: one switch, until T2 gets its own linear orchestrator.

The S271 audit found the T2 status, retries and best-draft choice driven by ``t2_judge``: a source-blind brand judge
that rewards added detail, answered by four different models (unpinned) and repeated up to median-of-3 plus a Jev
tie-break, while a T3 failure could re-run the whole T2 graph (worst case about 96 model requests per version).
ADR 0020: "T2's judge becomes report-only". With ``T2_INTERIM`` on (the default):

1. **Judge report-only.** ``t2_judge`` still scores every draft (``judge_breakdown``, ``llm_call_log``), but the
   draft's ``quality_score`` is validate's alone: the judge does not cap it, does not merge feedback into the retry,
   and does not feed the T3 revision (``judge_node``, ``tenant_pipeline._revision_feedback``).
2. **No Jev tie-break** (``t2_judge_tiebreak``) and **no median-of-3** (the AA-714 near-band repeat and
   ``T2_JUDGE_REPEAT=median3``): one judge call per draft.
3. **No revalidate judge:** ``revalidate_node`` re-validates a flag-fixed T2 draft without a second judge call.
4. **No T3 full-graph rewrite rounds:** T3 keeps its code checks, the meta-only regenerate and the sentence repair;
   when those cannot clear a draft, the best candidate is kept and escalated (review row) instead of re-running
   the whole T2 graph.
5. **Status from validate + T3 hard codes:** ``ai_generated`` only when validate's score clears the line AND T3 left
   no hard (structural) code; else ``needs_review`` (the tenant still receives the version). The master's
   ``quality_score`` is no longer copied onto a version with no score of its own (NULL instead).
6. **Dashes before the final check:** the em-dash fix (``t2_dashes``) runs inside T3 on every candidate, before its
   checks, so the saved text is the checked text (before: after the last check, never re-checked).

``T2_INTERIM=off`` (worker task def) restores the pre-S271 behaviour of every item above in one move (break glass).
The elephant-riding ``policy_hold`` (D8) and the closed hitl exit are not part of this switch: they are policy.
A1 is never affected.
"""
from __future__ import annotations

import os

T2_INTERIM_ENV = "T2_INTERIM"
_OFF = ("0", "false", "off", "no")


def enabled() -> bool:
    """True unless ``T2_INTERIM`` is set to off / 0 / false / no (read per call, like the other T2 switches)."""
    return os.environ.get(T2_INTERIM_ENV, "").strip().lower() not in _OFF


def active_for(state: dict) -> bool:
    """The interim profile applies to this graph state: a tenant rewrite with the switch on."""
    return bool(state.get("is_tenant_rewrite")) and enabled()
