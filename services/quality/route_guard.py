"""AA-1042 (S268, Quality guard) — content written by a fallback model is marked in QA, and held for review when the
stage asks for it.

Tech lead decision S267: a stage may fall back to another model (model-first chains, CIS 366); what that model wrote
must be visible to QA. It rides on the ``degraded[]`` mechanism of AA-925 (``services/quality/degraded.py``) under the
name ``fallback_writer``:

- severity ``count`` (the default): the output is MARKED — A1 stores it in ``generated_content.metadata.degraded``,
  T9 adds the non-blocking ``degraded`` flag (``content_piece.flags``, shown on Tenant QA > Content quality) and a
  ``repair_log`` entry; the running job counts it in ``shared.job.result.degraded``;
- severity ``hard`` when the stage's ``hold_fallback_writes`` is on (``shared.llm_route_policy``, default off): A1
  adds ``DEGRADED_DETECTOR`` (the existing hold → Review Queue path); T9 labels the piece ``held`` and T2 marks the
  version held (``tenant_tour_versions.judge_breakdown.fallback_writer``) — both admin-only: the tenant still
  receives the content (product rule S232), the label only asks QA to review it.

Who tells it: the gateway (``shared/llm_client/client.py`` ``_note_fallback_write``) calls
``note_fallback_write(stage, model, position)`` when a WRITER-role request is answered by route position > 0 (a
later model of the route; the same model on another provider is not a fallback model). The note goes to the job's
degraded collector and, when one is bound (``collect_output``), to the output's collector: T9 binds one per piece,
T2 one per version (the graph's writer and every T3 repair round; ``t2_mark``). The A1 graph does not need the
collector: its writer state already carries ``fallback_used`` (``a1_entries``). In a T2 graph state the entry is
always ``count``: the T2 hold is the version's admin-only label, never a block inside the graph.

Pure apart from the cached policy read (``route_watch.hold_fallback_writes``, never raises).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator, Optional

from services.quality import degraded

FALLBACK_WRITER = degraded.FALLBACK_WRITER
DETECTOR_PREFIX = "route:"
A1_WRITER_STAGE = "s1_generate"

_output: ContextVar[Optional[list]] = ContextVar("route_guard_output", default=None)


def severity(stage: Optional[str]) -> str:
    """``hard`` when the stage holds content written by a fallback model, else ``count``. Never raises."""
    try:
        from shared.llm_client import route_watch
        return "hard" if route_watch.hold_fallback_writes(stage) else "count"
    except Exception:  # noqa: BLE001 — the mark still lands, the hold fails open
        return "count"


@contextmanager
def collect_output() -> Iterator[list]:
    """Bind a per-output collector (a list shared by every task / thread that copies the context, like
    ``degraded.collect``) around the work that writes ONE output (a T9 piece)."""
    box: list = []
    token = _output.set(box)
    try:
        yield box
    finally:
        _output.reset(token)


def note_fallback_write(stage: Optional[str], model: Optional[str], position: int) -> None:
    """The gateway's note: a writer request of ``stage`` was answered by ``model`` at route position ``position``
    (> 0). Counted in the job's degraded collector; appended to the bound output collector. Never raises."""
    sev = severity(stage)
    degraded.note(FALLBACK_WRITER, stage=stage, severity=sev)
    box = _output.get()
    if box is not None:
        box.append({"stage": stage or "-", "model": model, "position": int(position), "severity": sev})


def entries(notes: Optional[Iterable[dict]] = None) -> list[dict]:
    """The output's ``fallback_writer`` degraded entries, one per (stage, severity), from ``notes`` (default: the
    bound output collector). [] when no fallback model wrote."""
    notes = list(_output.get() or []) if notes is None else list(notes)
    out: list[dict] = []
    for n in notes:
        out = degraded.add(out, FALLBACK_WRITER, detector=f"{DETECTOR_PREFIX}{n.get('stage') or '-'}",
                           detail=f"{n.get('model') or '?'} at route position {int(n.get('position') or 0) + 1}",
                           severity=n.get("severity") or "count")
    return out


def writer_stage(state: dict) -> str:
    """The writer stage of an A1 / T2 graph state (``generate_stage``; A1 by default)."""
    return state.get("generate_stage") or A1_WRITER_STAGE


def is_tenant_rewrite(state: dict) -> bool:
    """A T2 graph state (a tenant rewrite: its own writer stage)."""
    return bool(state.get("is_tenant_rewrite")) or writer_stage(state) != A1_WRITER_STAGE


def a1_entries(state: dict) -> list[dict]:
    """A1 / T2: the ``fallback_writer`` entry when the graph's writer was answered by a fallback model
    (``fallback_used``, set by the gateway for route position > 0). ``count`` in a T2 state (its hold is the
    version's label, ``t2_mark``). Pure apart from the cached policy read."""
    if not state.get("fallback_used"):
        return []
    stage = writer_stage(state)
    model = state.get("model_used") or "?"
    sev = "count" if is_tenant_rewrite(state) else severity(stage)
    return [degraded.entry(FALLBACK_WRITER, detector=f"{DETECTOR_PREFIX}{stage}",
                           detail=f"{model} (fallback model)", severity=sev)]


def t2_mark(notes: Optional[Iterable[dict]]) -> Optional[dict]:
    """T2: the admin-only mark a version stores in ``judge_breakdown.fallback_writer`` from its collector's notes
    (None when no fallback model wrote it). ``held`` when one of the writer stages holds fallback writes. Pure."""
    lst = entries(notes or [])
    if not lst:
        return None
    return {"held": degraded.is_hard(lst), "entries": lst}


def with_a1_entries(state: dict) -> dict:
    """``state`` with its ``fallback_writer`` entry put in ``degraded`` (replacing an earlier one: the graph may
    finish twice on one state). Pure apart from the cached policy read."""
    found = a1_entries(state)
    detector = f"{DETECTOR_PREFIX}{writer_stage(state)}"
    lst = [dict(e) for e in (state.get("degraded") or []) if e.get("detector") != detector]
    if not found and len(lst) == len(state.get("degraded") or []):
        return state
    return {**state, "degraded": lst + found}
