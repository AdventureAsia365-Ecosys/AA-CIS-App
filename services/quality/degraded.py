"""AA-925 (Q-2, design §6.3) — ``degraded[]``: every fail-open path that ran on an output, counted.

A detector that could not run (Jev down, a judge outage, grounding that crashed, a day check with nothing to
compare) used to leave no trace: the output looked exactly like one that passed. Each such path now adds a
named entry to the output's ``degraded`` list:

    {"name": "jev_unavailable", "severity": "hard", "detector": "grounding", "count": 12, "detail": "..."}

Severity (``REGISTRY``):

* ``hard``  — the output is held, never counted as a pass: A1 adds the hard code ``DEGRADED_DETECTOR``
  (``apply_a1_hold``: ``_is_publishable``, ``human_edit_gate`` and the review summary read it through the
  existing hard-code path), T9 labels an approved piece ``held`` (admin-only; the tenant still receives it).
* ``block`` — counted; a wave-level 5% threshold is later work (design §6.3, not this issue).
* ``soft`` / ``count`` — counted only.

Jev verdicts: ``zone == "error"`` is degraded; ``zone == "skipped"`` (mode off, tenant not on the allow-list)
is not. An error on an ``enforce`` question is hard; ``mode == "off"`` with ``zone == "error"`` (config error,
``decide_failed``, ``decide_sync`` on a loop) is hard too, because the real mode is unknown; a ``shadow``
question's error is counted only.

Two scopes:

* per output — ``add`` / ``replace_detector`` return a new list (LangGraph state is replaced, not mutated);
  stored in ``generated_content.metadata.degraded`` (A1), ``content_piece.flags`` + ``repair_log`` (T9).
* per job — ``collect()`` binds a collector (a contextvar, like ``call_log.bind_job``) around a job handler;
  ``shared.llm_client.decide`` calls ``note_verdicts`` for every Jev error, so A3, ingestion and the S1
  prefetch are counted without touching their code. The job worker writes it to ``shared.job.result.degraded``.

One ``quality_degraded`` log event per output (``emit``) with the aggregated counts — never one per unit: an
outage would otherwise write tens of thousands of lines.

Pure: no writer module is imported.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterable, Iterator, Optional

import structlog

logger = structlog.get_logger()

HOLD_CODE = "DEGRADED_DETECTOR"

JEV_UNAVAILABLE = "jev_unavailable"
GROUNDING_NOT_RUN = "grounding_not_run"
GROUNDING_NO_SOURCE = "grounding_no_source"
F9_JUDGE_UNAVAILABLE = "f9_judge_unavailable"
DAY_CHECK_SKIPPED = "day_check_skipped"
JUDGE_UNAVAILABLE = "judge_unavailable"
BRAND_AUDIT_UNAVAILABLE = "brand_audit_unavailable"
REPAIR_CONTRACT = "repair_contract"

SEVERITIES = ("hard", "block", "soft", "count")

# name -> default severity. jev_unavailable is resolved per verdict mode (jev_severity).
REGISTRY: dict[str, str] = {
    JEV_UNAVAILABLE: "hard",
    GROUNDING_NOT_RUN: "hard",
    GROUNDING_NO_SOURCE: "block",
    F9_JUDGE_UNAVAILABLE: "hard",
    DAY_CHECK_SKIPPED: "block",
    JUDGE_UNAVAILABLE: "block",
    BRAND_AUDIT_UNAVAILABLE: "count",   # it already fails closed (manual_check)
    REPAIR_CONTRACT: "soft",            # the repair was reverted; the text is the pre-repair text
}


def jev_severity(mode: Optional[str]) -> str:
    """hard for an enforce question or an unknown mode (``off`` + error, None); a shadow error is counted."""
    if mode == "shadow":
        return "count"
    return "hard"


def is_jev_error(verdict: Any) -> bool:
    """A Jev verdict that is an outage (zone error) — never a skip (mode off / not allow-listed)."""
    return verdict is not None and getattr(verdict, "zone", None) == "error"


# ── per output ───────────────────────────────────────────────────────────────────────────────────


def entry(name: str, *, detector: str, count: int = 1, detail: Optional[str] = None,
          severity: Optional[str] = None) -> dict:
    if name not in REGISTRY:
        raise KeyError(f"unknown degraded name {name!r} (add it to REGISTRY)")
    sev = severity or REGISTRY[name]
    if sev not in SEVERITIES:
        raise ValueError(f"unknown severity {sev!r}")
    out = {"name": name, "severity": sev, "detector": detector, "count": int(count)}
    if detail:
        out["detail"] = str(detail)[:300]
    return out


def add(lst: Optional[Iterable[dict]], name: str, *, detector: str, count: int = 1,
        detail: Optional[str] = None, severity: Optional[str] = None) -> list[dict]:
    """A new list with the entry added; the same (name, detector, severity) sums its count."""
    new = entry(name, detector=detector, count=count, detail=detail, severity=severity)
    out: list[dict] = []
    merged = False
    for e in lst or []:
        e = dict(e)
        if not merged and (e.get("name"), e.get("detector"), e.get("severity")) == \
                (new["name"], new["detector"], new["severity"]):
            e["count"] = int(e.get("count") or 0) + new["count"]
            if new.get("detail"):
                e["detail"] = new["detail"]
            merged = True
        out.append(e)
    if not merged:
        out.append(new)
    return out


def replace_detector(lst: Optional[Iterable[dict]], detector: str, entries: Iterable[dict]) -> list[dict]:
    """A new list where ``detector``'s entries are the given ones: a detector that re-runs on the latest
    content (validate, a grounding re-check) reports what is true NOW, not the sum of every run."""
    out = [dict(e) for e in (lst or []) if e.get("detector") != detector]
    for e in entries:
        out = add(out, e["name"], detector=detector, count=e.get("count", 1), detail=e.get("detail"),
                  severity=e.get("severity"))
    return out


def is_hard(lst: Optional[Iterable[dict]]) -> bool:
    return any(e.get("severity") == "hard" and int(e.get("count") or 0) > 0 for e in (lst or []))


def hard_names(lst: Optional[Iterable[dict]]) -> list[str]:
    return sorted({e["name"] for e in (lst or []) if e.get("severity") == "hard" and int(e.get("count") or 0) > 0})


def counts(lst: Optional[Iterable[dict]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in lst or []:
        out[e["name"]] = out.get(e["name"], 0) + int(e.get("count") or 0)
    return out


def emit(lst: Optional[Iterable[dict]], **ctx) -> None:
    """ONE ``quality_degraded`` event for an output, with the aggregated counts (no-op when empty)."""
    lst = list(lst or [])
    if not lst:
        return
    logger.warning("quality_degraded", counts=counts(lst), hard=hard_names(lst),
                   detectors=sorted({e.get("detector") for e in lst if e.get("detector")}), **ctx)


def jev_entries(errors: Iterable[dict], *, detail: Optional[str] = None) -> list[dict]:
    """``jev_unavailable`` entries from a list of ``{"mode": ...}`` error records, one per severity."""
    by_sev: dict[str, int] = {}
    for err in errors:
        sev = jev_severity(err.get("mode"))
        by_sev[sev] = by_sev.get(sev, 0) + 1
    return [{"name": JEV_UNAVAILABLE, "severity": sev, "count": n, "detail": detail}
            for sev, n in sorted(by_sev.items())]


# ── A1: the one hold mechanism ───────────────────────────────────────────────────────────────────


def apply_a1_hold(state: dict) -> dict:
    """End of the A1 graph (revalidate → ``_apply_grounding_recheck``). A hard degraded entry adds the hard
    code ``DEGRADED_DETECTOR`` (graph._HARD_BLOCK_CODES — NOT the Sonnet-retry class: a stronger model cannot
    fix an outage) and sets ``manual_check``. Every reader (``_is_publishable``, ``human_edit_gate``, the
    review summary) sees it through the existing hard-code path. Logs one ``quality_degraded`` event."""
    lst = state.get("degraded") or []
    emit(lst, stage="a1", tour=(state.get("tour") or {}).get("tour_id"))
    if not is_hard(lst):
        return state
    codes = list(dict.fromkeys(list(state.get("failure_codes") or []) + [HOLD_CODE]))
    feedback = state.get("feedback") or ""
    why = f"{HOLD_CODE}: {', '.join(hard_names(lst))} did not run — re-run once the detector is healthy"
    out = {**state, "failure_codes": codes, "brand_audit_status": "manual_check",
           "feedback": f"{feedback}; {why}" if feedback else why}
    if state.get("revalidate_ran"):
        out["revalidate_passed"] = False
    return out


# ── T9: the final step ───────────────────────────────────────────────────────────────────────────


def t9_entries(gate_ledger: Iterable[dict], repair_items_contract: int = 0) -> list[dict]:
    """The final attempt's degraded entries: the F9 judge outage marker on its ledger result (an outage on
    attempt 1 and a good attempt 2 is not degraded) and the repair-contract reverts of the job."""
    out: list[dict] = []
    for g in gate_ledger or []:
        name = g.get("degraded") if isinstance(g, dict) else None
        if name:
            out = add(out, name, detector=str(g.get("gate") or "t9"))
    if repair_items_contract:
        out = add(out, REPAIR_CONTRACT, detector="t9_repair", count=repair_items_contract)
    return out


def apply_t9_hold(status: str, held_reason: Optional[str], lst: list[dict]) -> tuple[str, Optional[str]]:
    """An approved piece with a hard degraded entry is labelled held (admin-only — the tenant still receives
    it, CONTEXT.md T9). Reverses AA-770's "an F9 outage is a warn, not a hold" (D1–D11, AA-925)."""
    if status == "approved" and is_hard(lst):
        return "held", f"degraded: {', '.join(hard_names(lst))}"
    return status, held_reason


def t9_flag(lst: list[dict]) -> Optional[dict]:
    """The admin flag (``content_piece.flags``; never a blocking ledger gate — that would trigger a rewrite and
    confuse the AA-528 guard)."""
    if not lst:
        return None
    return {"gate": "degraded", "passed": False, "blocking": False, "repairable": False,
            "counts": counts(lst), "hard": hard_names(lst),
            "violations": [f"{e['name']} ({e['detector']}) x{e['count']}" for e in lst]}


# ── per job (central Jev counter) ────────────────────────────────────────────────────────────────

_collector: ContextVar[Optional[dict]] = ContextVar("quality_degraded_collector", default=None)


@contextmanager
def collect() -> Iterator[dict]:
    """Bind a per-job collector. The dict is shared (mutable) by every task / thread that copies the context
    (asyncio tasks, ``asyncio.to_thread``, LangGraph's executor), the same propagation as call_log.bind_job."""
    box: dict = {}
    token = _collector.set(box)
    try:
        yield box
    finally:
        _collector.reset(token)


def note(name: str, *, stage: Optional[str], severity: str, count: int = 1) -> None:
    box = _collector.get()
    if box is None:
        return
    key = f"{name}:{stage or '-'}:{severity}"
    box[key] = box.get(key, 0) + int(count)


def note_verdicts(stage: Optional[str], verdicts: Iterable[Any]) -> None:
    """Called by ``decide`` for every decision: each zone=error verdict is one ``jev_unavailable``."""
    for v in verdicts:
        if is_jev_error(v):
            note(JEV_UNAVAILABLE, stage=stage, severity=jev_severity(getattr(v, "mode", None)))


def job_summary(box: Optional[dict]) -> Optional[dict]:
    """``shared.job.result.degraded``: {"jev_unavailable:a3_atomize:hard": 4, ...} plus totals, or None."""
    if not box:
        return None
    totals: dict[str, int] = {}
    for key, n in box.items():
        name = key.split(":", 1)[0]
        totals[name] = totals.get(name, 0) + n
    return {"by_stage": dict(sorted(box.items())), "totals": totals}


def merge_job_result(result: Any, box: Optional[dict]) -> Any:
    """The job result with ``degraded`` added when the collector counted anything (dict results only)."""
    summary = job_summary(box)
    if summary is None:
        return result
    if result is None:
        return {"degraded": summary}
    if isinstance(result, dict):
        return {**result, "degraded": summary}
    return result
