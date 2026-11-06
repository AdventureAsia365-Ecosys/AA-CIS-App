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

S271 W0 (AA-900 l13) — instrument marks. A measuring answer (a judge, validate or decide stage) is an instrument:
its verdict only means something from the calibrated model. The gateway (``client._mark_degraded_answer``, after the
route answered) asks ``answer_marks`` for the marks of every reply and puts them on the reply
(``LLMResponse.route_degraded``, so a caller can read them, and through ``billed_fields`` into the ledger row's
``quality_signal.degraded``); each instrument mark is also noted here (``note_instrument``: the job's degraded
collector and the bound output collector). The marks:

- ``fallback_instrument`` (reason ``rung``): a judge / validate / decide stage answered by route position > 0;
- ``fallback_instrument`` (reason ``uncalibrated_model``): answered by nova-lite at any position;
- ``same_family_pair`` (reason ``same_family``): a judge answer of the same model family as a writer reply of the
  same output (``family_rule.trace_models``, bound per output by ``collect_output``: T9 per piece, T2 per version,
  S1 per tour in ``v1_pipeline._rewrite_tour``);
- a writer answered by position > 0 gets ``fallback_writer`` on the reply too (the note above is unchanged).

What each caller does with an instrument mark (severity ``count``, W0 changes no hold):

- T9 (``service._degraded_final``): ``entries()`` lists them next to ``fallback_writer``: the piece gets the
  non-blocking ``degraded`` flag and a ``repair_log`` entry; ``apply_t9_hold`` holds only on a hard entry (unchanged);
- S1 (``graph`` final node -> ``with_a1_entries``): they land in ``state.degraded`` (stored in
  ``generated_content.metadata.degraded``); ``apply_a1_hold`` adds ``DEGRADED_DETECTOR`` only on a hard entry
  (unchanged);
- T2 (``t2_rewrite_job``): ``t2_instrument_mark`` is stored in ``judge_breakdown.route_degraded`` (admin-only), never
  in the version's held label (``t2_mark`` keeps only the writer entries, as before);
- every job: ``shared.job.result.degraded`` counts them (``degraded.note``).

Pure apart from the cached policy read (``route_watch.hold_fallback_writes``, never raises) and the catalog read of a
model's family (``family_rule.family_of``, cached).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator, Optional

from services.quality import degraded

FALLBACK_WRITER = degraded.FALLBACK_WRITER
FALLBACK_INSTRUMENT = degraded.FALLBACK_INSTRUMENT
SAME_FAMILY_PAIR = degraded.SAME_FAMILY_PAIR
INSTRUMENT_NAMES = (FALLBACK_INSTRUMENT, SAME_FAMILY_PAIR)
DETECTOR_PREFIX = "route:"
A1_WRITER_STAGE = "s1_generate"
# S271 W0: the roles whose answer is a measurement, and the models that are not a calibrated instrument for any of them
INSTRUMENT_ROLES = ("judge", "validate", "decide")
UNCALIBRATED_INSTRUMENT_MODELS = frozenset({"nova-lite"})
REASON_RUNG = "rung"
REASON_UNCALIBRATED = "uncalibrated_model"
REASON_SAME_FAMILY = "same_family"

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
    ``degraded.collect``) around the work that writes ONE output (a T9 piece, a T2 version, an S1 tour). S271 W0: also
    binds the output's model trace (``family_rule.trace_models(reuse=True)``: an eval runner's own trace is kept), so
    the gateway can mark a judge of the same family as this output's writer."""
    from shared.llm_client import family_rule
    box: list = []
    token = _output.set(box)
    try:
        with family_rule.trace_models(reuse=True):
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


def _model_key(label: Optional[str]) -> str:
    from shared.llm_client import family_rule
    return family_rule.model_key_of(label)


def answer_marks(stage: Optional[str], role: Optional[str], model: Optional[str], position: Optional[int],
                 writer_labels: Iterable[Optional[str]] = (), models: Optional[Iterable] = None) -> list[dict]:
    """S271 W0: the degraded marks of one gateway reply (``LLMResponse.route_degraded`` entries), [] for a clean one.
    ``model`` = the reply's label, ``position`` = its route position, ``writer_labels`` = the writer replies of the
    same output so far (the bound trace). ``models`` (catalog rows) makes the family lookup pure. Never raises."""
    try:
        key = _model_key(model)
        pos = position if isinstance(position, int) and not isinstance(position, bool) and position >= 0 else 0
        base = {"stage": stage or "-", "model": key or "?", "position": pos}
        out: list[dict] = []
        if role in INSTRUMENT_ROLES:
            if pos > 0:
                out.append({"name": FALLBACK_INSTRUMENT, "reason": REASON_RUNG, **base})
            if key in UNCALIBRATED_INSTRUMENT_MODELS:
                out.append({"name": FALLBACK_INSTRUMENT, "reason": REASON_UNCALIBRATED, **base})
        elif role == "writer" and pos > 0:
            out.append({"name": FALLBACK_WRITER, "reason": REASON_RUNG, **base})
        if role == "judge" and model:
            from shared.llm_client import family_rule
            writers = [w for w in dict.fromkeys(writer_labels or ()) if w]
            common = family_rule.shared_families(writers, [model], models) if writers else []
            if common:
                out.append({"name": SAME_FAMILY_PAIR, "reason": REASON_SAME_FAMILY, **base,
                            "families": common, "writers": [_model_key(w) for w in writers]})
        return out
    except Exception:  # noqa: BLE001 — a QA mark never breaks the call it marks
        return []


def note_instrument(mark: dict) -> None:
    """The gateway's note of one instrument mark (``answer_marks``, name in ``INSTRUMENT_NAMES``): counted in the
    job's degraded collector, appended to the bound output collector. Severity ``count`` (W0 holds nothing new).
    Never raises."""
    try:
        name = mark.get("name")
        if name not in INSTRUMENT_NAMES:
            return
        sev = degraded.REGISTRY[name]
        degraded.note(name, stage=mark.get("stage"), severity=sev)
        box = _output.get()
        if box is not None:
            box.append({"name": name, "stage": mark.get("stage") or "-", "model": mark.get("model"),
                        "position": int(mark.get("position") or 0), "severity": sev,
                        "reason": mark.get("reason"), "families": list(mark.get("families") or [])})
    except Exception:  # noqa: BLE001
        pass


def _detail(n: dict) -> str:
    where = f"{n.get('model') or '?'} at route position {int(n.get('position') or 0) + 1}"
    name = n.get("name") or FALLBACK_WRITER
    if name == SAME_FAMILY_PAIR:
        return f"{n.get('model') or '?'} judged a writer of its own family ({', '.join(n.get('families') or [])})"
    if name == FALLBACK_INSTRUMENT and n.get("reason") == REASON_UNCALIBRATED:
        return f"{where} (uncalibrated instrument)"
    return where


def entries(notes: Optional[Iterable[dict]] = None) -> list[dict]:
    """The output's route degraded entries (``fallback_writer``, and since S271 W0 ``fallback_instrument`` /
    ``same_family_pair``), one per (name, stage, severity), from ``notes`` (default: the bound output collector).
    [] when nothing was marked. The detector of an instrument entry is ``route:<stage>`` too."""
    notes = list(_output.get() or []) if notes is None else list(notes)
    out: list[dict] = []
    for n in notes:
        out = degraded.add(out, n.get("name") or FALLBACK_WRITER,
                           detector=f"{DETECTOR_PREFIX}{n.get('stage') or '-'}", detail=_detail(n),
                           severity=n.get("severity") or "count")
    return out


def writer_notes(notes: Optional[Iterable[dict]]) -> list[dict]:
    return [n for n in (notes or []) if (n.get("name") or FALLBACK_WRITER) == FALLBACK_WRITER]


def instrument_notes(notes: Optional[Iterable[dict]]) -> list[dict]:
    return [n for n in (notes or []) if n.get("name") in INSTRUMENT_NAMES]


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
    (None when no fallback model wrote it). ``held`` when one of the writer stages holds fallback writes. Pure.
    S271 W0: writer notes only (the instrument marks are ``t2_instrument_mark``'s)."""
    lst = entries(writer_notes(notes))
    if not lst:
        return None
    return {"held": degraded.is_hard(lst), "entries": lst}


def t2_instrument_mark(notes: Optional[Iterable[dict]]) -> Optional[dict]:
    """S271 W0 — T2: the admin-only ``judge_breakdown.route_degraded`` of a version: the instrument marks of its
    judge / validate calls (None when there are none). Never part of the held label. Pure."""
    lst = entries(instrument_notes(notes))
    return {"entries": lst} if lst else None


def with_a1_entries(state: dict) -> dict:
    """``state`` with its ``fallback_writer`` entry put in ``degraded`` (replacing an earlier one: the graph may
    finish twice on one state). S271 W0: plus, on an A1 state, the instrument entries of the bound output collector
    (``v1_pipeline._rewrite_tour`` binds one per tour), replacing earlier ones by (name, detector). Pure apart from
    the cached policy read."""
    found = a1_entries(state)
    detector = f"{DETECTOR_PREFIX}{writer_stage(state)}"
    instruments = [] if is_tenant_rewrite(state) else entries(instrument_notes(_output.get() or []))
    replaced = {(e["name"], e["detector"]) for e in instruments}
    lst = [dict(e) for e in (state.get("degraded") or [])
           if not (e.get("detector") == detector and e.get("name", FALLBACK_WRITER) == FALLBACK_WRITER)
           and (e.get("name"), e.get("detector")) not in replaced]
    if not found and not instruments and len(lst) == len(state.get("degraded") or []):
        return state
    return {**state, "degraded": lst + found + instruments}
