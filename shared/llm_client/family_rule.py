"""AA-1024 A4 (root ADR 0009 decision 12) — the family-based writer / judge rule.

The rule: a judge never scores text written by a model of its own family (the maker: ``shared.llm_model_family``,
``llm_model_catalog.family``, migration 320; ``CatalogModel.family_key`` = family, else vendor). Writer stages stay on
Anthropic models until the qualification ladder says otherwise (decision 12's writer clause), so ``WRITER_FAMILY`` is
the family every writer route starts on and the family no judge may have.

Where it is checked:
- the admin (``api/routers/admin_llm_ops.py``): a judge stage cannot select a model of ``WRITER_FAMILY``, a writer
  stage only selects one (the vendor_direct last resort of migration 327 excepted, as before);
- the eval runners, before a run (``stage_pair_conflicts``: the writer stage's primary against every model of the
  judge stage's route) and at call time (``trace_models`` + ``shared_families``: the models that really wrote an
  item against the model that really judged it). A writer route may end on a model of another maker (the S263 T-3
  last resort, GPT on the OpenAI API) while the judge route is on that maker too, so only the call-time check can
  see a same-family pair; the R2 runner (``services/writing_memory/r2.py``) and the S1 regression eval
  (``services/eval/regression.py``) refuse that verdict.

``trace_models``: while bound, every successful ``LLMClient.generate`` reply records ``(role, model label)`` here
(``client.py``, one line after the route ran; shadows are not recorded). The list is shared by reference, so calls
made in ``asyncio.to_thread`` / tasks started inside the block (copies of the context) are recorded too. Nothing is
recorded when no trace is bound (production traffic).

A model label the catalog does not know (a test double, a retired key) has no family and never makes a conflict:
the rule refuses what it can prove, it does not guess.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator, Optional

import structlog

from .models import VENDOR_LABEL_PREFIXES

logger = structlog.get_logger()

# ADR 0009 decision 12: writer stages stay on Anthropic models (Sonnet 5.5 for S1) until the ladder shows
# non-inferiority and an amendment is accepted. A family key of shared.llm_model_family (migration 320).
WRITER_FAMILY = "anthropic"

_LABEL_PREFIXES = ("satellite-", *VENDOR_LABEL_PREFIXES)


class SameFamilyJudge(ValueError):
    """A judge of the same model family as the writer whose text it would score (ADR 0009 decision 12)."""


def model_key_of(label: Optional[str]) -> str:
    """The catalog Model Key of an ``LLMResponse.model_used`` label ("satellite-", "anthropic-api-" and
    "azure-openai-" prefixes stripped)."""
    key = str(label or "").strip()
    for prefix in _LABEL_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


def _by_key(models: Optional[Iterable]) -> Optional[dict]:
    return None if models is None else {m.model_key: m for m in models}


def family_of(label: Optional[str], models: Optional[Iterable] = None) -> Optional[str]:
    """The family of a model key or reply label: from ``models`` (catalog rows) when given, else the catalog cache
    (``catalog.get_model_sync``: sync, may read the database once). None for a key the catalog does not know."""
    key = model_key_of(label)
    if not key:
        return None
    by_key = _by_key(models)
    if by_key is not None:
        m = by_key.get(key)
    else:
        from .catalog import get_model_sync
        try:
            m = get_model_sync(key)
        except Exception as e:  # noqa: BLE001 — an unreadable catalog proves nothing
            logger.warning("llm_family_rule_catalog_unreadable", model=key[:80], error=str(e)[:200])
            m = None
    return m.family_key if m is not None else None


def shared_families(writer_labels: Iterable[Optional[str]], judge_labels: Iterable[Optional[str]],
                    models: Optional[Iterable] = None) -> list[str]:
    """The families a writer model and a judge model have in common (sorted), [] when the pair is allowed. Pure when
    ``models`` is given."""
    models = list(models) if models is not None else None
    writers = {f for f in (family_of(w, models) for w in writer_labels) if f}
    judges = {f for f in (family_of(j, models) for j in judge_labels) if f}
    return sorted(writers & judges)


def check_pair(writer_labels: Iterable[Optional[str]], judge_labels: Iterable[Optional[str]], *,
               models: Optional[Iterable] = None, what: str = "the judge") -> None:
    """Raise SameFamilyJudge when a writer model and a judge model share a family."""
    writer_labels, judge_labels = list(writer_labels), list(judge_labels)
    common = shared_families(writer_labels, judge_labels, models)
    if common:
        raise SameFamilyJudge(
            f"{what} ({', '.join(model_key_of(j) for j in judge_labels if j)}) is of the same model family "
            f"({', '.join(common)}) as the writer ({', '.join(model_key_of(w) for w in writer_labels if w)}); a judge "
            "must be of another family than the text it scores (ADR 0009 decision 12)")


# ── call-time trace ────────────────────────────────────────────────────────────────────────────────────────────────

_TRACE: ContextVar[Optional[list]] = ContextVar("llm_family_rule_trace", default=None)


@contextmanager
def trace_models() -> Iterator[list]:
    """Bind a fresh trace: yields the list that collects ``(role, model label)`` of every successful gateway reply
    made inside the block."""
    seen: list = []
    token = _TRACE.set(seen)
    try:
        yield seen
    finally:
        _TRACE.reset(token)


def note_model(role: Optional[str], label: Optional[str]) -> None:
    """Record one reply in the bound trace (no-op without one). Never raises."""
    seen = _TRACE.get()
    if seen is None or not label:
        return
    try:
        seen.append((str(role or ""), str(label)))
    except Exception:  # noqa: BLE001 — the trace never fails a call
        pass


def labels(seen: Iterable, role: str) -> list[str]:
    """The model labels of one role in a trace, in call order, without repeats."""
    return list(dict.fromkeys(label for r, label in seen if r == role))


# ── static check of two stage routes (before an eval run) ────────────────────────────────────────────────────────

def route_conflicts(writer_row: Optional[dict], judge_rows: Iterable[dict], models: Iterable) -> list[str]:
    """Pure: "<judge stage>: <model> (<family>)" for each model of a judge stage's route (primary and fallbacks) whose
    family is the writer stage's primary model's family. The writer's fallbacks are left to the call-time check: a
    writer route may end on another maker as a last resort (migration 327) that the run may never reach."""
    if not writer_row:
        return []
    models = list(models)
    writer_family = family_of(writer_row.get("model_id"), models)
    if not writer_family:
        return []
    out = []
    for row in judge_rows:
        for key in [row.get("model_id"), *(row.get("fallback_model_ids") or [])]:
            if key and family_of(key, models) == writer_family:
                out.append(f"{row.get('stage')}: {key} ({writer_family})")
    return out


async def stage_pair_conflicts(writer_stage: Optional[str], judge_stages: Iterable[str], *,
                               writer_model: Optional[str] = None, models: Optional[list] = None) -> list[str]:
    """``route_conflicts`` on the live route rows (``list_stage_configs``) and catalog (``models``, else
    ``list_models``). ``writer_model`` (an eval's explicit model, e.g. the regression's model_tier) stands in for the
    writer stage's primary. Raises when the route or the catalog cannot be read (the caller refuses the run: it cannot
    be checked)."""
    from .catalog import list_models
    from .role_config import list_stage_configs

    judge_stages = list(judge_stages)
    rows = {r["stage"]: r for r in await list_stage_configs()}
    if models is None:
        models = await list_models()
    writer_row = {"stage": writer_stage, "model_id": writer_model} if writer_model else rows.get(writer_stage or "")
    return route_conflicts(writer_row, [rows[s] for s in judge_stages if s in rows], models)


__all__ = ["SameFamilyJudge", "WRITER_FAMILY", "check_pair", "family_of", "labels", "model_key_of", "note_model",
           "route_conflicts", "shared_families", "stage_pair_conflicts", "trace_models"]
