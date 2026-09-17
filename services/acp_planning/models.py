"""
services.acp_planning.models — schemas for N4 (RunwayMap), N5 (QuarterPlan),
N6 (SlotGrid). Ported from aamc/models.py (aa-marketing-v2 research build).

tenant_id is a required field on every per-computation artifact (RunwayMap,
QuarterPlan, SlotGrid) — N4/N5/N6 run per-tenant, never cross-tenant
(AA-301 decision). Atoms stay platform-scoped (D3, owner_scope='platform') —
no tenant_id here; tenancy is inherited via tour_id -> raw_tours.tenant_id.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

FunnelStage = Literal["TOFU", "MOFU", "BOFU", "OFF"]
# AA-449 — extended from 4 to 7 values (STEP0, docs/claude_audit/
# AA-449-00-step0-t8-angle-gate-investigation.md §5): T8's Bang-2 channel-style reference table
# has 7 channels; Slot.channel only supported 4 of them, and since acp_shared.tenant_config.
# channels is unconstrained free text at the DB layer, a tenant configuring e.g. "linkedin"
# would previously make compute_slot_grid() raise a real Pydantic ValidationError the moment it
# tried to construct a Slot — not theoretical, confirmed by reading the code. Names match
# services/acp_angle_gate/channel_style.py's CHANNEL_STYLE keys exactly (snake_case for the two
# multi-word channels) — keep both in sync if either changes.
Channel = Literal[
    "blog", "facebook", "tiktok", "email", "linkedin", "instagram", "landing_page", "ads",
]
Distinctiveness = Literal["HIGH", "MED", "LOW"]
LifecycleStage = Literal["active", "phasing_out", "retired"]


class QuarterPlanNotApprovedError(Exception):
    """Gate B: a QuarterPlan must be 'approved' (approved=True on the model) before N6 can
    allocate from it. AA-448 Option A: for a tenant-created plan this happens automatically the
    instant they finalize it (no human approval step) — the check/type is kept exactly as-is
    (admin_atoms.py/admin_produce.py both still depend on it), only the old "human (Ms. Thu)
    must approve" semantics changed. See docs/implementation-notes/
    AA-448-t7-content-planning.md."""


# ---------------------------------------------------------------- inputs (read from DB, not computed)
class Trip(BaseModel):
    """One row of acp_contract.v_trip_registry, already scoped to a tenant."""
    id: UUID
    name: str
    destination: Optional[str] = None
    period: Optional[str] = None
    duration_raw: Optional[str] = None
    itinerary_source: Optional[str] = None
    lifecycle_stage: LifecycleStage = "active"
    trip_url: Optional[str] = None
    url_alive: Optional[bool] = None


class AtomRecord(BaseModel):
    """One row of acp_contract.tour_atoms. No tenant_id (D3 — platform-scoped)."""
    atom_id: str
    trip_id: UUID
    text: str
    activity_type: Optional[str] = None  # AA-379 — decompose enum (trek|bike|food|culture|stay|transit|other)
    distinctiveness: Distinctiveness = "LOW"
    deleted: bool = False
    weight: float = 1.0
    cooldown_until: dict[str, Any] = Field(default_factory=dict)
    usage_log: list[Any] = Field(default_factory=list)


# ---------------------------------------------------------------- N4
class RunwayCell(BaseModel):
    destination: str
    market: str
    month: int  # 1..12
    stage: FunnelStage


class RunwayMap(BaseModel):
    tenant_id: UUID
    year: int
    cells: list[RunwayCell] = Field(default_factory=list)
    trips_hash: Optional[str] = None
    unknowns: list[str] = Field(default_factory=list)

    def stage(self, destination: str, market: str, month: int) -> FunnelStage:
        for c in self.cells:
            if c.destination == destination and c.market == market and c.month == month:
                return c.stage
        return "OFF"


# ---------------------------------------------------------------- N5
class BigRock(BaseModel):
    rock_id: str
    trip_id: UUID
    title: str
    atom_ids: list[str] = Field(default_factory=list)
    atomization_contract: dict[str, int] = Field(default_factory=dict)


class TripScore(BaseModel):
    """AA-323 Gap 1 — per-trip N5 scoring, exposed so a human reviewing or
    overriding the auto-selection can see why a trip was (or wasn't) chosen.
    Covers every non-retired, non-excluded trip considered for the quarter,
    not just the ones that made the cut — the create-plan UI needs the full
    candidate list to render add/remove checkboxes. `reason` is a short
    English label naming the dominant scoring factor (never the algorithm's
    internal weights — those stay fixed, per AA-323 decision #3)."""
    trip_id: UUID
    name: str
    destination: Optional[str] = None
    score: float
    runway_fit: float
    richness: float
    distinctiveness_score: float
    dfs_relevance_score: float = 0.5  # AA-448 round 1 — 4th scoring term; 0.5 = SIGNAL_SCORE_MAP["MED"]
    engagement_adjustment_score: float = 0.5  # AA-448 round 6 — 5th term; 0.5 = no feedback data yet
    forced: bool
    selected: bool
    reason: str


class QuarterPlan(BaseModel):
    tenant_id: UUID
    year: int
    quarter: int
    trip_ids: list[UUID] = Field(default_factory=list)
    forced_specials: list[UUID] = Field(default_factory=list)
    big_rocks: list[BigRock] = Field(default_factory=list)
    destination_shares: dict[str, float] = Field(default_factory=dict)
    thin_trip_notes: list[str] = Field(default_factory=list)
    capacity_note: Optional[str] = None
    trips_hash: Optional[str] = None
    trip_scores: list[TripScore] = Field(default_factory=list)
    # Gate B — must be True before N6 can allocate. AA-448 Option A: a tenant's own plan sets
    # this automatically the instant they finalize (approved_by="tenant:<id>"), no human step.
    approved: bool = False
    approved_by: Optional[str] = None


# ---------------------------------------------------------------- N6
class Slot(BaseModel):
    slot_id: str
    week: int
    channel: Channel
    kind: Literal["evergreen", "campaign", "reactive_hold"]
    trip_id: Optional[UUID] = None
    atom_ids: list[str] = Field(default_factory=list)
    funnel_stage: FunnelStage = "TOFU"
    framework: Optional[str] = None
    cta_target: Optional[str] = None
    topic_hint: Optional[str] = None
    keyword_seed: Optional[str] = None  # B6 fix — per-slot, never trip-wide-shared


class SlotGrid(BaseModel):
    tenant_id: UUID
    year: int
    month: int
    slots: list[Slot] = Field(default_factory=list)
    capacity_note: Optional[str] = None
    trips_hash: Optional[str] = None


# ---------------------------------------------------------------- recompute trigger (shared N4/N5/N6)
def compute_trips_hash(trips: list[Trip]) -> str:
    """Deterministic fingerprint of (trip_id, period, lifecycle_stage) — a
    change to any of these on any trip means N4/N5/N6 are stale."""
    payload = sorted((str(t.id), t.period or "", t.lifecycle_stage) for t in trips)
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def needs_recompute(previous_hash: Optional[str], current_trips: list[Trip]) -> bool:
    """True = N4/N5/N6 are stale and must be recomputed. Only marks staleness
    — does not trigger any job itself (issue AA-301: 'đánh dấu, không tự
    trigger job')."""
    return previous_hash != compute_trips_hash(current_trips)
