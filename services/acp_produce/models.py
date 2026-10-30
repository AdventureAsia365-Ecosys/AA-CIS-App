"""
services.acp_produce.models — shared types for N7 Produce (AA-298 gate
stack + AA-369 C1/C2/C3 research/brief models).

GateResult/Piece: minimal on purpose, only what gate_grounding() (F1, P0-1)
and run_gates() (P0-3) need — see AA-298.md.

KeywordRecord/SERPProfile/FAQCandidate/Brief (AA-369): field sets ported
from the aamc/ prototype (docs/AI-gent-for automation works/aa-marketing-v2/
aamc/models.py, spec reference only, never imported by real code) and
cross-checked against what E1 (AA-370, not yet built) will need to read off
Brief (required_h2s + atoms_by_section + framework + word_range) — see
AA-369 STEP 0 comment on Linear. `Brief.gap_statement` stays Optional[str]
(None when the Bedrock satellite call fails or is skipped) — it is
supplementary content, never a condition of TOPIC REJECTED (that's the
demand law in compile_brief(), services/acp_produce/research.py).
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ADR-2026-029: 1 repair attempt per failing gate is allowed, but the whole
# piece is held after this many repair ROUNDS total (a round = repair + full
# re-run of every gate, P0-3) regardless of which gate(s) kept failing.
# AA-396 follow-up (option C, dynamic repair budget, gates.py::
# compute_repair_budget()): this is now the BASE budget for the common
# single-gate-failure case, not always the final per-piece ceiling.
REPAIR_TOTAL_MAX = 3

# AA-396 follow-up: hard ceiling on the DYNAMIC budget regardless of how many
# gates start out failing. Calibrated against the real N7 corpus (9 held
# pieces, docs/implementation-notes/AA-391-report-data.json) -- the worst
# case actually observed is 4 simultaneous failing gates (budget 6 under
# compute_repair_budget()'s formula); 8 leaves headroom for one more
# simultaneous failure than anything seen in production while still
# bounding a pathological piece's Sonnet-repair spend, not leaving it
# unbounded.
REPAIR_BUDGET_CAP = 8


class GateResult(BaseModel):
    gate: str
    passed: bool
    violations: list[str] = Field(default_factory=list)


class RepairRoundLog(BaseModel):
    """AA-396 follow-up: one entry per `repair_fn` call inside a piece's
    `run_gates()` loop (gates.py) -- which gate was targeted, its violations
    at the time, and whether that gate passed on the following round's full
    gate-stack re-run ("passed"/"failed"/"exception" if `repair_fn` itself
    raised). Persisted on the `Piece` (not only structlog'd) so a human
    reviewing a held piece has the whole repair history in the same place
    they already look (`acp_deliver.pieces`), without a separate log query
    -- see gates.py::run_gates()'s own docstring for what still needs manual
    promotion from here (the N8 output-rules table is retired, AA-923; learned rules: app ADR 0010)."""
    round: int
    gate_targeted: str
    violations: list[str] = Field(default_factory=list)
    outcome: Literal["attempted", "passed", "failed", "exception"] = "attempted"


class FAQItem(BaseModel):
    """E4 output (AA-371) — one answered FAQ pair. `question`/`source_id` are
    carried through from `FAQCandidate` (AA-369) verbatim, never
    model-invented; `answer` is the one LLM-written part."""
    question: str
    answer: str
    source_id: str


class Piece(BaseModel):
    piece_id: str
    body_tagged: str
    channel: Literal["blog", "facebook", "tiktok"] = "blog"
    status: str = "in_progress"  # in_progress | passed | held
    gate_ledger: list[GateResult] = Field(default_factory=list)
    repair_count: int = 0
    held_reason: Optional[str] = None
    # E4 (AA-371) output — Pydantic-only, deliberately NOT a DB column
    # (acp_deliver.pieces has no slot for either; body_tagged already carries
    # the human-readable rendering, these are computed sidecars for whatever
    # future export/publish path needs structured Q/A or schema.org markup).
    faq_items: list[FAQItem] = Field(default_factory=list)
    faq_jsonld: Optional[dict] = None
    # AA-396 follow-up (option C, dynamic repair budget): set once by
    # run_gates() from the piece's FIRST gate-stack run, never recomputed
    # mid-loop -- see gates.py::compute_repair_budget()/run_gates().
    # `repair_budget` below is the one exception (AA-415): it MAY be
    # extended by +1 (capped at REPAIR_BUDGET_CAP) when a gate absent from
    # this initial count is discovered failing mid-loop -- see run_gates()'s
    # own AA-415 docstring paragraph for why. `initial_failing_gate_count`
    # itself never changes after this line runs, only `repair_budget` can.
    initial_failing_gate_count: Optional[int] = None
    repair_budget: Optional[int] = None
    repair_log: list[RepairRoundLog] = Field(default_factory=list)


# ---------------------------------------------------------------- AA-369 C1/C2/C3

class KeywordRecord(BaseModel):
    """C1 output. `volume=None` (confidence="heuristic") is a stated unknown,
    never replaced with a fabricated number — same truth-domain rule as the
    aamc/ prototype this is ported from."""
    keyword: str
    location: str  # SOURCE market, not destination
    volume: Optional[int] = None
    confidence: Literal["dfs", "heuristic"] = "heuristic"
    cpc: Optional[float] = None
    retrieved_at: Optional[date] = None


class SERPProfile(BaseModel):
    """C2 output. `word_count_range` is B13 (AA-298 Nhóm 4, tracked as
    AA-327): the aamc/ prototype declared this field but never populated it,
    so every Brief silently fell back to the (900, 1400) default in
    compile_brief(). AA-369 deliberately does NOT fix this (Nghiep, AA-369
    IMPLEMENT scope note: "KHÔNG chủ động sửa gì khác thuộc AA-327 (B10, B12,
    B13)") — the field stays unset here too, on purpose, and
    test_aa369_research.py has a test that documents/detects the gap rather
    than closing it. Fix belongs to AA-327."""
    keyword: str
    location: str
    intent_verdict: Literal["informational", "commercial", "transactional", "mixed", "unknown"] = "unknown"
    word_count_range: Optional[tuple[int, int]] = None
    paa_questions: list[str] = Field(default_factory=list)
    related_searches: list[str] = Field(default_factory=list)
    top10_domains: list[str] = Field(default_factory=list)
    competitor_present: bool = False
    confidence: Literal["dfs", "heuristic"] = "heuristic"


class FAQCandidate(BaseModel):
    question: str
    source_id: str  # REQUIRED atom/facts id; unanswerable PAA is dropped upstream, logged to unknown_ledger


class Brief(BaseModel):
    """C3 output. `gap_statement`/`atoms_by_section` (LLM-assisted in the
    aamc/ prototype) are built deterministically by AA-369's compile_brief()
    except gap_statement, which is the one genuinely semantic piece (see
    module docstring) — Bedrock satellite Haiku, best-effort, never blocks
    demand-law reject."""
    brief_id: str
    slot_id: str
    keyword: str
    demand: KeywordRecord
    serp: Optional[SERPProfile] = None
    demand_reason: Optional[str] = None  # required when no demand evidence (C3 law)
    required_h2s: list[str] = Field(default_factory=list)
    faq_candidates: list[FAQCandidate] = Field(default_factory=list)
    gap_statement: Optional[str] = None
    atoms_by_section: dict[str, list[str]] = Field(default_factory=dict)
    facts_ids: list[str] = Field(default_factory=list)
    framework: str
    word_range: tuple[int, int] = (900, 1400)
    variance_directives: list[str] = Field(default_factory=list)
    cta_target: str
    language: str = "en"
    internal_links: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- AA-370 E1 outline

class OutlineSection(BaseModel):
    """E1 output. One per `Brief.required_h2s` entry — `atom_ids` is read
    straight off `Brief.atoms_by_section[title]` (empty list if that section
    picked up no atoms; AA-370 STEP 0 flagged a pre-existing AA-369 overflow
    quirk where atoms beyond `len(required_h2s)` all land on the LAST
    section instead of being spread out — left as-is here, see AA-370.md)."""
    title: str
    atom_ids: list[str] = Field(default_factory=list)
    goal: str
