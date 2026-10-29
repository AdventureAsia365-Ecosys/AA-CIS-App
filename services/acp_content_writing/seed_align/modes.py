"""AA-789 tier 1 — per-class mode, routing under the existing gates, and the hold rule.

Each class has its own mode (worker task-def env), default `off`:
- `off`: the check does not run. With every class off the gate ledger is byte for byte what it was (invariant I2),
  and spaCy is never loaded.
- `shadow`: the check runs; its candidates are recorded only (`run_quality_gates` → outcome["seed_align"], stored in
  writer_input), never in the ledger. Nothing blocks, nothing is repaired, nothing is paid for.
- `repair`: each candidate is a blocking finding `sa:<class>: <reason>: '<sentence>'` under its class's existing
  gate (F17_trip_fact, F18_rarity, F13_leak, F13_repeat), so it gets the one sentence repair those gates get. It
  never holds a piece (`never_holds`): when the repair fails the piece ships with the finding as a flag.
- `hold`: like repair, and a finding that survives its repair may hold the piece. Only the four fact classes may
  ever reach it (`HOLD_CAPABLE`: optional told as planned, planned told as optional, near told as at, world share),
  and only once a class is in `HOLD_PROMOTED`, which is changed by a promotion PR with the evidence table (plan §3:
  G1 offline, G2 shadow, G3 ≥ 50 labelled fires with ≤ 1 false, after the S246 freeze, ~18/11). Until then an env
  value `hold` reads as `repair`.

Env: `T10_SA_MODE` sets every class; `T10_SA_MODE_<CLASS>` (e.g. `T10_SA_MODE_NEAR_AS_AT`) overrides one.
No new gate is added (S246 freeze): the finding prefix `sa:` is the only new thing a ledger reader sees.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

from services.acp_content_writing.seed_align import checks as sa_checks
from services.acp_content_writing.seed_align import nlp as sa_nlp
from services.acp_content_writing.seed_align import seed_graph
from services.quality import registry as qr

logger = structlog.get_logger()

OFF, SHADOW, REPAIR, HOLD = "off", "shadow", "repair", "hold"
MODES = (OFF, SHADOW, REPAIR, HOLD)
MODE_ENV = "T10_SA_MODE"
FINDING_PREFIX = "sa:"
# The existing gate a class's findings sit under (plan §2, "Gate it lives under"). Class 11 belongs to the FAQ step.
CLASS_GATE = {
    sa_checks.OPTIONAL_AS_PLANNED: qr.F17_TRIP_FACT,
    sa_checks.PLANNED_AS_OPTIONAL: qr.F17_TRIP_FACT,
    sa_checks.NEAR_AS_AT: qr.F17_TRIP_FACT,
    sa_checks.DIFFICULTY_PROMISE: qr.F17_TRIP_FACT,
    sa_checks.WORLD_SHARE: qr.F18_RARITY,
    sa_checks.SCOPELESS_SUPERLATIVE: qr.F18_RARITY,
    sa_checks.BRIEF_RECIPE_LEAK: qr.F13_LEAK,
    sa_checks.NAMING_NOTE_LEAK: qr.F13_LEAK,
    sa_checks.PARAPHRASED_REPEAT: qr.F13_REPEAT,
    sa_checks.ORPHAN_OPENER: qr.F13_REPEAT,
}
# The fact classes that may ever hold (plan §3 rule 1). Style and integrity classes stop at repair.
HOLD_CAPABLE = frozenset({sa_checks.OPTIONAL_AS_PLANNED, sa_checks.PLANNED_AS_OPTIONAL, sa_checks.NEAR_AS_AT,
                          sa_checks.WORLD_SHARE})
# Classes promoted to hold by a PR that carries the G1-G3 evidence. Empty: nothing may hold before the S246 freeze
# ends (~18/11) and before a class has its 50 labelled shadow fires.
HOLD_PROMOTED: frozenset = frozenset()


def _env_for(cls: str) -> str:
    return f"{MODE_ENV}_{cls.upper()}"


def class_mode(cls: str) -> str:
    """The mode a class runs in: its own env, else the global one, else off. An unknown value reads as off; `hold`
    reads as `repair` unless the class may hold and is promoted."""
    raw = os.environ.get(_env_for(cls))
    if raw is None or not raw.strip():
        raw = os.environ.get(MODE_ENV, "")
    mode = raw.strip().lower()
    if mode not in MODES or cls not in CLASS_GATE or cls not in sa_checks.BUILT_CLASSES:
        return OFF
    if mode == HOLD and not (cls in HOLD_CAPABLE and cls in HOLD_PROMOTED):
        return REPAIR
    return mode


def active_modes() -> dict[str, str]:
    """{class: mode} for every built class whose mode is not off."""
    return {c: m for c in sa_checks.BUILT_CLASSES if (m := class_mode(c)) != OFF}


def finding_class(violation: str) -> Optional[str]:
    """The class of an `sa:` finding text, else None."""
    v = str(violation or "")
    if not v.startswith(FINDING_PREFIX):
        return None
    return v[len(FINDING_PREFIX):].split(":", 1)[0].strip() or None


def finding_may_hold(violation: str) -> bool:
    """May this finding hold a piece? Every non-`sa:` finding: yes (the gate decides, as before). An `sa:` finding:
    only when its class is hold-capable, promoted and running in hold mode."""
    cls = finding_class(violation)
    if cls is None:
        return True
    return cls in HOLD_CAPABLE and cls in HOLD_PROMOTED and class_mode(cls) == HOLD


@dataclass
class SeedAlignRun:
    """One run over one attempt: `live` = {gate: [finding text]} for the classes in repair / hold, `record` = what
    writer_input keeps (every candidate with its mode, shadow included)."""
    live: dict[str, list[str]] = field(default_factory=dict)
    record: dict = field(default_factory=dict)


def _seed_of(trip_ctx, hygiene_ctx) -> Optional[str]:
    seed = getattr(trip_ctx, "seed", None) if trip_ctx is not None else None
    if not seed and hygiene_ctx is not None:
        seed = getattr(hygiene_ctx, "seed", None) or getattr(getattr(hygiene_ctx, "lexicon", None), "own_text", None)
    return seed


def _brief_lines(brief_texts: Optional[dict], trip_ctx, hygiene_ctx) -> list[str]:
    """The writer-instruction texts: the brand pack and angle brief fields (hygiene.brief_texts: {key: [text]}) and
    the brand's audience line (its age range must not come back as copy)."""
    out: list[str] = []
    for v in (brief_texts or {}).values():
        out += [str(x) for x in (v if isinstance(v, (list, tuple)) else [v]) if x]
    for ctx in (trip_ctx, hygiene_ctx):
        audience = getattr(ctx, "audience", None) if ctx is not None else None
        if audience:
            out.append(f"Audience: {audience}")
    return out


def run(content_text: str, *, trip_ctx=None, hygiene_ctx=None, brief_texts: Optional[dict] = None,
        modes: Optional[dict[str, str]] = None) -> Optional[SeedAlignRun]:
    """Seed alignment for one attempt, or None when every class is off (the gate stack does not change at all).
    Fails open: an error or a missing spaCy model leaves the ledger as it was and is recorded."""
    modes = active_modes() if modes is None else {c: m for c, m in modes.items() if m != OFF}
    if not modes:
        return None
    started = time.monotonic()
    out = SeedAlignRun(record={"modes": dict(sorted(modes.items())), "candidates": []})
    seed = _seed_of(trip_ctx, hygiene_ctx)
    if not seed or not (content_text or "").strip():
        out.record["status"] = "no_seed"
        return out
    try:
        briefs = _brief_lines(brief_texts, trip_ctx, hygiene_ctx)
        graph = seed_graph.build(seed, briefs)
        cands = sa_checks.find_candidates(content_text, graph, names=set(graph.place_names), classes=modes.keys())
    except Exception as exc:  # fail open: the deterministic gates above are unchanged
        logger.warning("seed_align_failed", error=str(exc)[:300])
        out.record["status"] = "error"
        out.record["error"] = str(exc)[:300]
        return out
    if cands is None:
        out.record["status"] = "nlp_unavailable"
        return out
    out.record["status"] = "ok"
    out.record["nlp"] = sa_nlp.NLP_VERSION
    out.record["ms"] = int((time.monotonic() - started) * 1000)
    for c in cands:
        mode = modes.get(c.cls, OFF)
        gate = CLASS_GATE[c.cls]
        out.record["candidates"].append({"cls": c.cls, "mode": mode, "gate": gate, "reason": c.reason,
                                         "sentence": c.sentence[:300]})
        if mode in (REPAIR, HOLD):
            out.live.setdefault(gate, []).append(f"{FINDING_PREFIX}{c.cls}: {c.reason}: '{c.sentence[:300]}'")
    return out


def _quoted_sentence(violation: str) -> str:
    v = str(violation)
    i = v.find(": '")
    return v[i + 3:].rstrip("'").strip() if i >= 0 else ""


def apply(sources: list[dict], sa: Optional[SeedAlignRun]) -> list[dict]:
    """`sources` (the legacy results, before merge_ledger) with the live findings appended to their gates. A finding
    whose sentence a deterministic finding of the same gate already quotes is dropped (one repair per sentence)."""
    if sa is None or not sa.live:
        return sources
    out = []
    for g in sources:
        extra = sa.live.get(g.get("gate")) if isinstance(g, dict) else None
        if not extra:
            out.append(g)
            continue
        quoted = {_quoted_sentence(v) for v in g.get("violations") or []}
        add = [v for v in extra if _quoted_sentence(v) not in quoted]
        if not add:
            out.append(g)
            continue
        out.append({**g, "passed": False, "violations": list(g.get("violations") or []) + add, "repairable": True,
                    "blocking": True})
    return out


__all__ = ["OFF", "SHADOW", "REPAIR", "HOLD", "MODES", "MODE_ENV", "FINDING_PREFIX", "CLASS_GATE", "HOLD_CAPABLE",
           "HOLD_PROMOTED", "class_mode", "active_modes", "finding_class", "finding_may_hold", "SeedAlignRun", "run",
           "apply"]
