"""
services.acp_planning.quarter — N5 Quarter Plan (1x/quarter).

Ported from aamc/planning.py's plan_quarter()/D3 (aa-marketing-v2 research
build). Scores tours -> chooses quarter focus + big rocks. Pure Python,
$0 LLM.

Fixes applied during the port (see docs/implementation-notes/AA-301.md):
  B4 — special-tour matching used substring containment (`s in name.lower()`),
       which both false-negatived ("sapa trekking" not in "sapa valley trek")
       and false-positived on partial word matches. Replaced with token
       overlap + prefix fuzzy match (_fuzzy_match).
  B5 — THIN_TRIP_ATOM_MIN existed in config but no code anywhere capped a
       thin trip's content share. _cap_thin_trip_shares() now caps and
       redistributes the freed share proportionally to non-thin trips.

Gate B (Ms. Thu must approve the quarter plan, REQUIRED, NEVER auto) is
QuarterPlan.approved — compute_slot_grid() (N6, allocator.py) refuses to run against an
unapproved plan (QuarterPlanNotApprovedError). No acp_shared.acp_hitl_requests
row is created for this — that table is FK'd to acp_shared.acp_runs(run_id),
an ACP-B2B "pipeline run" concept N4-N6 don't have (they're periodic
per-tenant computations, not runs); reusing it would mean inventing a fake
run_id. Self-chosen decision, see AA-301 implementation notes.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Optional
from uuid import UUID

from services.acp_shared.atom_constants import THIN_TRIP_ATOM_MIN

from .constants import QUARTER_SCORE_WEIGHTS, SIGNAL_SCORE_MAP, THIN_TRIP_MAX_SHARE
from .models import AtomRecord, BigRock, QuarterPlan, RunwayMap, Trip, TripScore, compute_trips_hash

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TOKEN_MIN_PREFIX = 4
_FUZZY_MATCH_THRESHOLD = 0.5


def _tokens(s: str) -> set[str]:
    return set(_TOKEN_RE.findall(s.lower()))


def _tokens_fuzzy_equal(a: str, b: str, min_prefix: int = _TOKEN_MIN_PREFIX) -> bool:
    if a == b:
        return True
    if len(a) >= min_prefix and len(b) >= min_prefix:
        return a[:min_prefix] == b[:min_prefix]
    return False


def _fuzzy_match(special: str, trip_name: str, trip_destination: Optional[str],
                  threshold: float = _FUZZY_MATCH_THRESHOLD) -> bool:
    """B4 fix — token overlap + prefix fuzzy match instead of substring
    containment. 'sapa trekking' now matches 'Sapa Valley Trek' (token
    'trek'/'trekking' share a 4-char prefix) at >=50% of the special's own
    tokens matched, instead of silently dropping the special (previous bug:
    'sapa trekking' in 'sapa valley trek'.lower() -> False)."""
    special_tokens = _tokens(special)
    if not special_tokens:
        return False
    candidate_tokens = _tokens(trip_name) | _tokens(trip_destination or "")
    matched = sum(
        1 for st in special_tokens
        if any(_tokens_fuzzy_equal(st, ct) for ct in candidate_tokens)
    )
    return (matched / len(special_tokens)) >= threshold


def _cap_thin_trip_shares(
    shares: dict[str, float], atom_counts: dict[str, int],
) -> tuple[dict[str, float], list[str]]:
    """B5 fix — a destination whose live atom count is below
    THIN_TRIP_ATOM_MIN has its content share capped at THIN_TRIP_MAX_SHARE.
    Freed share is redistributed proportionally across non-thin
    destinations so shares still sum to ~1.0 (redistribution behavior is
    not specified by the issue — self-chosen, see AA-301 implementation
    notes)."""
    notes: list[str] = []
    thin: dict[str, float] = {}
    normal: dict[str, float] = {}
    for dest, share in shares.items():
        if atom_counts.get(dest, 0) < THIN_TRIP_ATOM_MIN:
            thin[dest] = share
        else:
            normal[dest] = share

    capped: dict[str, float] = {}
    freed = 0.0
    for dest, share in thin.items():
        new_share = min(share, THIN_TRIP_MAX_SHARE)
        if new_share < share:
            notes.append(
                f"'{dest}' is thin ({atom_counts.get(dest, 0)} atoms < {THIN_TRIP_ATOM_MIN}) "
                f"— share capped {share:.2f} -> {new_share:.2f}")
            freed += share - new_share
        capped[dest] = new_share

    normal_total = sum(normal.values()) or 1.0
    for dest, share in normal.items():
        capped[dest] = share + freed * (share / normal_total)

    return capped, notes


def _score_reason(runway_fit: float, richness: float, dist: float, dfs_score: float,
                   engagement_score: float, forced: bool) -> str:
    """AA-323 Gap 1 — short English label naming the dominant scoring factor.
    Weights mirror compute_quarter_plan()'s own score formula (QUARTER_SCORE_WEIGHTS,
    constants.py) so the label reflects what actually drove the ranking, not raw component
    values a reviewer would have to interpret themselves.

    AA-448 round 1 — added `dfs_score` as a 4th labelable factor. Round 6 — added
    `engagement_score` (real post-publish feedback, rolled up from tour_atoms.weight) as a 5th,
    same tie-break logic below unchanged either time.

    AA-323 round 6, Phần C — fixed a tie-break bug found in round 5's live
    audit: plain `max(contributions, key=...)` always resolves a tie to the
    FIRST dict key regardless of which component actually tied for highest,
    which mislabeled 562/763 real trips as "High runway fit" while their
    actual runway_fit was 0.0 (the single most common case — most trips have
    neither a BOFU/MOFU runway window yet nor curated atoms). Now: an
    all-zero (or tied-at-zero) contribution set gets its own honest label
    instead of defaulting to whichever key happens to be listed first, and a
    genuine tie among nonzero contributors reads as "Balanced" rather than
    naming only one of the tied factors."""
    if forced:
        return "Manually added"
    contributions = {
        "High runway fit (BOFU/MOFU window this quarter)": runway_fit * QUARTER_SCORE_WEIGHTS["runway_fit"],
        "Rich atom pool": richness * QUARTER_SCORE_WEIGHTS["richness"],
        "High-distinctiveness atoms": dist * QUARTER_SCORE_WEIGHTS["distinctiveness"],
        "Strong search demand (DFS)": dfs_score * QUARTER_SCORE_WEIGHTS["dfs_relevance"],
        "Strong real engagement (feedback)": engagement_score * QUARTER_SCORE_WEIGHTS["engagement_adjustment"],
    }
    top_value = max(contributions.values())
    if top_value <= 0.0:
        return "No runway or atom signal yet this quarter"
    top_keys = [k for k, v in contributions.items() if v == top_value]
    if len(top_keys) > 1:
        return "Balanced score (multiple factors tied)"
    return top_keys[0]


def compute_quarter_plan(
    tenant_id: UUID, year: int, quarter: int, trips: list[Trip], markets: list[str],
    capacity_posts_per_week: int, specials: list[str], runway: RunwayMap,
    atoms_by_trip: dict[UUID, list[AtomRecord]],
    excludes: Optional[set[UUID]] = None,
    dfs_relevance_by_trip: Optional[dict[UUID, str]] = None,
) -> QuarterPlan:
    """Pure computation — no DB, no LLM, 100% unit-testable.

    `excludes` (AA-323 Gap 1 — manual N5 override, decision #3): trip ids a
    human has chosen to remove from consideration. Excluded trips are still
    scored and returned in `trip_scores` (so the UI can show them as an
    unchecked candidate a reviewer could re-add), but never enter `ranked`
    for selection — same effect as if capacity were computed without them.
    Does NOT change the scoring weights themselves, only which trips are
    eligible to be chosen.

    `dfs_relevance_by_trip` (AA-448 round 1) — already-scored HIGH/MED/LOW per trip_id (see
    services/acp_shared/dfs_relevance.py's fetch_dfs_relevance_by_tour(), which does the actual
    seo_context DB read + thresholding; this pure function only consumes the result, same
    division of labor as `atoms_by_trip`). A missing/None dict, or a trip_id absent from it,
    scores as "MED" — a flat, equal contribution that does not change any existing caller's
    RELATIVE trip ranking when they don't pass real data (see
    docs/implementation-notes/AA-448-t7-content-planning.md Decision 2).

    `engagement_adjustment` (AA-448 round 6, the 5th term) is NOT a separate parameter — it is
    derived directly from `atoms_by_trip`'s own `AtomRecord.weight` field (already fetched,
    already read by N6's allocator; this is the first time N5 reads it too). No new plumbing
    needed: `rollup_atom_weights()` (services/acp_shared/content_metrics.py) writes
    `tour_atoms.weight` from real feedback ahead of time; this function just averages a trip's
    atoms' current weight and normalizes it. A trip with no feedback-adjusted atoms yet (the
    common case early on) scores the neutral 0.5 midpoint, same convention as dfs_relevance's
    own "MED" default — this feature changes nothing about existing rankings until real feedback
    data exists."""
    q_months = [(quarter - 1) * 3 + i for i in (1, 2, 3)]
    excludes = excludes or set()
    dfs_relevance_by_trip = dfs_relevance_by_trip or {}

    scored: list[tuple[float, Trip, bool, float, float, float, float, float, bool]] = []
    for t in trips:
        if t.lifecycle_stage == "retired":
            continue
        atoms = atoms_by_trip.get(t.id, [])
        dest = t.destination or t.name
        runway_fit = sum(
            1 for m in q_months for mk in markets if runway.stage(dest, mk, m) in ("BOFU", "MOFU")
        ) / (len(q_months) * len(markets) or 1)
        richness = min(len(atoms) / 10, 1.0)
        dist = sum(SIGNAL_SCORE_MAP[a.distinctiveness] for a in atoms) / (len(atoms) or 1)
        dfs_score = SIGNAL_SCORE_MAP[dfs_relevance_by_trip.get(t.id, "MED")]
        # AA-448 round 6 — engagement_adjustment: avg atom.weight (aamc-style [0.25, 2.0] range,
        # 1.0 = neutral/no feedback yet) normalized so weight=1.0 -> exactly 0.5, matching the
        # SIGNAL_SCORE_MAP "MED" midpoint every other term already uses.
        avg_weight = sum(a.weight for a in atoms) / len(atoms) if atoms else 1.0
        engagement_score = min(1.0, avg_weight / 2.0)
        forced = any(_fuzzy_match(s, t.name, t.destination) for s in specials)
        score = (
            runway_fit * QUARTER_SCORE_WEIGHTS["runway_fit"]
            + richness * QUARTER_SCORE_WEIGHTS["richness"]
            + dist * QUARTER_SCORE_WEIGHTS["distinctiveness"]
            + dfs_score * QUARTER_SCORE_WEIGHTS["dfs_relevance"]
            + engagement_score * QUARTER_SCORE_WEIGHTS["engagement_adjustment"]
            + (1.0 if forced else 0.0)
        )
        is_excluded = t.id in excludes
        scored.append((score, t, forced, runway_fit, richness, dist, dfs_score, engagement_score, is_excluded))

    eligible = [x for x in scored if not x[8]]
    eligible.sort(key=lambda x: -x[0])

    max_trips = max(2, min(len(eligible), capacity_posts_per_week + 1))
    chosen = eligible[:max_trips]
    chosen_ids = {t.id for _, t, _, _, _, _, _, _, _ in chosen}
    capacity_note = None
    if len(eligible) > max_trips:
        capacity_note = (
            f"{len(eligible)} eligible trips at {capacity_posts_per_week} posts/wk — "
            f"focusing on {max_trips} trips (applied).")

    trip_scores = [
        TripScore(
            trip_id=t.id, name=t.name, destination=t.destination,
            score=round(score, 3), runway_fit=round(runway_fit, 3),
            richness=round(richness, 3), distinctiveness_score=round(dist, 3),
            dfs_relevance_score=round(dfs_score, 3),
            engagement_adjustment_score=round(engagement_score, 3),
            forced=forced, selected=(t.id in chosen_ids and not is_excluded),
            reason=_score_reason(runway_fit, richness, dist, dfs_score, engagement_score, forced),
        )
        for score, t, forced, runway_fit, richness, dist, dfs_score, engagement_score, is_excluded in
        sorted(scored, key=lambda x: -x[0])
    ]

    plan = QuarterPlan(
        tenant_id=tenant_id, year=year, quarter=quarter,
        trip_ids=[t.id for _, t, _, _, _, _, _, _, _ in chosen],
        forced_specials=[t.id for _, t, forced, _, _, _, _, _, _ in chosen if forced],
        capacity_note=capacity_note,
        trips_hash=compute_trips_hash(trips),
        trip_scores=trip_scores,
    )

    total_score = sum(s for s, _, _, _, _, _, _, _, _ in chosen) or 1
    raw_shares: dict[str, float] = {}
    dest_atom_counts: dict[str, int] = {}
    for s, t, _, _, _, _, _, _, _ in chosen:
        dest = t.destination or t.name
        raw_shares[dest] = raw_shares.get(dest, 0.0) + s / total_score
        dest_atom_counts[dest] = dest_atom_counts.get(dest, 0) + len(atoms_by_trip.get(t.id, []))

    capped_shares, thin_notes = _cap_thin_trip_shares(raw_shares, dest_atom_counts)
    plan.destination_shares = {k: round(v, 2) for k, v in capped_shares.items()}
    plan.thin_trip_notes = thin_notes

    for _, t, _, _, _, _, _, _, _ in chosen[:3]:
        highs = [a for a in atoms_by_trip.get(t.id, []) if a.distinctiveness == "HIGH" and not a.usage_log]
        if len(highs) >= 2:
            plan.big_rocks.append(BigRock(
                rock_id=f"rock_{uuid.uuid4().hex[:10]}", trip_id=t.id,
                title=f"{t.name}: definitive guide",
                atom_ids=[a.atom_id for a in highs[:6]],
                atomization_contract={"social": 4, "email": 1, "lead_magnet": 1}))

    return plan


_ATOM_ROW_QUERY = """
    SELECT atom_id, tour_id, text, activity_type, distinctiveness,
           deleted, weight, cooldown_until, usage_log
    FROM acp_contract.tour_atoms
    WHERE owner_scope = $1 AND NOT deleted AND NOT is_empty_marker
"""


def _parse_jsonb(value, default):
    """asyncpg has no jsonb codec registered on this app's connections
    (same gap AA-314 already found and fixed for src_highlights elsewhere,
    api/routers/v1_tours.py) — JSONB columns arrive as raw JSON-encoded
    strings, not parsed dict/list. Parse manually at the point of use."""
    if isinstance(value, str):
        return json.loads(value) if value else default
    return value if value is not None else default


def _row_to_atom(row) -> AtomRecord:
    return AtomRecord(
        atom_id=row["atom_id"], trip_id=row["tour_id"], text=row["text"],
        activity_type=row["activity_type"],
        distinctiveness=row["distinctiveness"] or "LOW",
        deleted=row["deleted"], weight=float(row["weight"]),
        cooldown_until=_parse_jsonb(row["cooldown_until"], {}),
        usage_log=_parse_jsonb(row["usage_log"], []),
    )


async def fetch_atoms_by_trip(tenant_id: UUID, pool) -> dict[UUID, list[AtomRecord]]:
    """`tenant_id` is passed as a plain str (matches `tour_atoms.owner_scope`'s column
    type — free-text per ADR-2026-038 Hướng B, not a UUID FK). AA-461: filters by
    `owner_scope`, NOT `raw_tours.tenant_id` — a tenant's T5 atoms rewritten from a
    shared-pool tour (raw_tours.tenant_id = aa_internal) always carry
    owner_scope = the rewriting tenant's own id, so filtering by the tour's original
    owner made those atoms invisible to N5/N6 quarter planning. Same fix shape as
    `tenant_pool.fetch_tenant_atoms_by_trip()` (AA-448, T7's own parallel function that
    routed around this same bug for its slot-grid endpoint instead of fixing it here).

    AA-516 note: its only 2 real callers (`plan_quarter()` here, `allocator.allocate_month()`)
    were both deleted as dead N4-N6 admin-triggered-quarter-plan code — this function is now
    ALSO unreferenced outside its own unit test (test_aa301_quarter.py). Not deleted:
    out of the exact scope Nghiệp confirmed for this cleanup (only fetch_trips/runway_map/
    plan_quarter/allocate_month/allocate_month_from_db/allocate_and_persist_week were named).
    Flagged as a real follow-up candidate, not removed speculatively."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_ATOM_ROW_QUERY, str(tenant_id))
    by_trip: dict[UUID, list[AtomRecord]] = {}
    for r in rows:
        atom = _row_to_atom(r)
        by_trip.setdefault(atom.trip_id, []).append(atom)
    return by_trip


def approve_quarter_plan(plan: QuarterPlan, approved_by: str) -> QuarterPlan:
    """Gate B — the only way a QuarterPlan may become allocatable. Never
    called automatically anywhere in this module."""
    plan.approved = True
    plan.approved_by = approved_by
    return plan


class QuarterPlanVersionNotFoundError(Exception):
    """Raised by approve_quarter_plan_version — version_id has no matching
    acp_shared.quarter_plan_version row."""


class QuarterPlanVersionNotPendingError(Exception):
    """Raised by approve_quarter_plan_version — version_id exists but is not
    in 'pending' status (already approved/rejected). Approval must never
    silently no-op (AA-320)."""


async def save_quarter_plan_version(plan: QuarterPlan, pool, source: str = "standard") -> UUID:
    """AA-320 Gate B persist — DB-backed counterpart to compute_quarter_plan().
    Creates acp_shared.quarter_plan on first call for (tenant_id, year, quarter),
    reuses it on every later call. Always appends a new quarter_plan_version
    (never overwrites — re-planning a quarter keeps every prior version
    queryable) with approval_status='pending'. Does not touch
    current_version_id — that only moves on approve_quarter_plan_version().

    AA-448 round 6 (Shape 1) — also ensures a parent acp_shared.year_plan row exists for
    (tenant_id, year) and links this quarter_plan row to it (year_plan_id), get-or-create,
    idempotent. This is the ONLY place that needs to do this: this function is the sole choke
    point every quarter_plan row is created through (confirmed — the only OTHER production
    caller, admin.py's create_quarter_plan, is retired by this same task; admin_atoms.py's
    preview-slotgrid never calls this function, it only reads). Purely additive — every other
    column/row this function already writes keeps its exact prior behavior."""
    payload = json.dumps(plan.model_dump(mode="json"))
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO acp_shared.year_plan (tenant_id, year)
                VALUES ($1, $2)
                ON CONFLICT (tenant_id, year) DO NOTHING
                """,
                plan.tenant_id, plan.year,
            )
            year_plan_id = await conn.fetchval(
                "SELECT year_plan_id FROM acp_shared.year_plan WHERE tenant_id = $1 AND year = $2",
                plan.tenant_id, plan.year,
            )
            await conn.execute(
                """
                INSERT INTO acp_shared.quarter_plan (tenant_id, year, quarter, year_plan_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tenant_id, year, quarter) DO UPDATE
                    SET year_plan_id = COALESCE(acp_shared.quarter_plan.year_plan_id, EXCLUDED.year_plan_id)
                """,
                plan.tenant_id, plan.year, plan.quarter, year_plan_id,
            )
            plan_id = await conn.fetchval(
                """
                SELECT plan_id FROM acp_shared.quarter_plan
                WHERE tenant_id = $1 AND year = $2 AND quarter = $3
                """,
                plan.tenant_id, plan.year, plan.quarter,
            )
            next_version_no = await conn.fetchval(
                """
                SELECT COALESCE(MAX(version_no), 0) + 1
                FROM acp_shared.quarter_plan_version
                WHERE plan_id = $1
                """,
                plan_id,
            )
            version_id = await conn.fetchval(
                """
                INSERT INTO acp_shared.quarter_plan_version
                    (plan_id, version_no, payload, source, approval_status)
                VALUES ($1, $2, $3::jsonb, $4, 'pending')
                RETURNING version_id
                """,
                plan_id, next_version_no, payload, source,
            )
    return version_id


async def approve_quarter_plan_version(version_id: UUID, approved_by: str, pool) -> None:
    """AA-320 Gate B persist — fully separate DB-backed approval path. Does
    NOT call approve_quarter_plan() (the in-memory Gate B function above);
    that function has its own callers (e.g. admin_atoms.py's preview-slotgrid
    demo) and stays untouched. Raises rather than no-ops on a bad version_id
    or a non-pending version, since a silent no-op here would let a caller
    believe an approval happened when it didn't."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT plan_id, approval_status
                FROM acp_shared.quarter_plan_version
                WHERE version_id = $1
                FOR UPDATE
                """,
                version_id,
            )
            if row is None:
                raise QuarterPlanVersionNotFoundError(
                    f"quarter_plan_version {version_id} not found")
            if row["approval_status"] != "pending":
                raise QuarterPlanVersionNotPendingError(
                    f"quarter_plan_version {version_id} is '{row['approval_status']}', "
                    "not 'pending' — cannot approve")

            await conn.execute(
                """
                UPDATE acp_shared.quarter_plan_version
                SET approval_status = 'approved', approved_by = $2, approved_at = now()
                WHERE version_id = $1
                """,
                version_id, approved_by,
            )
            await conn.execute(
                """
                UPDATE acp_shared.quarter_plan
                SET current_version_id = $2
                WHERE plan_id = $1
                """,
                row["plan_id"], version_id,
            )


async def fetch_approved_quarter_plan(
    tenant_id: UUID, year: int, quarter: int, pool,
) -> Optional[QuarterPlan]:
    """AA-320 Gate B persist — reads the tenant/year/quarter's current
    (approved) version's payload back into a QuarterPlan, with .approved
    forced True so it satisfies compute_slot_grid()'s existing Gate B check
    (allocator.py) unchanged. Returns None (never raises) when no plan or no
    approved version exists yet — the caller decides the error response."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT qpv.payload, qpv.approved_by
            FROM acp_shared.quarter_plan qp
            JOIN acp_shared.quarter_plan_version qpv ON qpv.version_id = qp.current_version_id
            WHERE qp.tenant_id = $1 AND qp.year = $2 AND qp.quarter = $3
              AND qpv.approval_status = 'approved'
            """,
            tenant_id, year, quarter,
        )
    if row is None:
        return None
    payload = _parse_jsonb(row["payload"], {})
    plan = QuarterPlan(**payload)
    plan.approved = True
    plan.approved_by = row["approved_by"]
    return plan


async def fetch_current_version_no(
    tenant_id: UUID, year: int, quarter: int, pool,
) -> Optional[int]:
    """AA-323 round 5 — Việc 2. A separate lightweight lookup (not folded into
    fetch_approved_quarter_plan's return shape, to avoid touching that
    function's existing contract/callers/tests) so the Preview screen can show
    which version it's reading — with several approved versions accumulating
    per tenant/quarter (approve never revokes an older version's
    approval_status, it only moves quarter_plan.current_version_id — see
    approve_quarter_plan_version above), "8 trips" alone doesn't tell a human
    which one they're looking at."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT qpv.version_no
            FROM acp_shared.quarter_plan qp
            JOIN acp_shared.quarter_plan_version qpv ON qpv.version_id = qp.current_version_id
            WHERE qp.tenant_id = $1 AND qp.year = $2 AND qp.quarter = $3
              AND qpv.approval_status = 'approved'
            """,
            tenant_id, year, quarter,
        )


async def fetch_quarter_plan_version(version_id: UUID, pool) -> Optional[dict]:
    """AA-323 round 6, Phần A — read one SPECIFIC persisted version by its
    version_id, regardless of approval_status (unlike
    fetch_approved_quarter_plan, which only ever returns the tenant/quarter's
    CURRENT approved version). version_id is a global primary key, so this
    needs no tenant_id/year/quarter from the caller — the row itself carries
    them (via the quarter_plan join), which is what lets the Preview screen
    resolve a historical version from just its id in the URL.

    Returns None when the id doesn't exist. Does NOT enforce Gate B (that a
    version must be 'approved' to feed N6) — same division of responsibility
    as fetch_approved_quarter_plan not raising: this is a pure read, the
    caller (admin_atoms.py's preview-slotgrid) decides what a non-approved
    version means for its use case."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT qp.tenant_id, qp.year, qp.quarter, qpv.version_no,
                   qpv.payload, qpv.approval_status, qpv.approved_by
            FROM acp_shared.quarter_plan_version qpv
            JOIN acp_shared.quarter_plan qp ON qp.plan_id = qpv.plan_id
            WHERE qpv.version_id = $1
            """,
            version_id,
        )
    if row is None:
        return None
    payload = _parse_jsonb(row["payload"], {})
    plan = QuarterPlan(**payload)
    plan.approved = row["approval_status"] == "approved"
    plan.approved_by = row["approved_by"]
    return {
        "tenant_id": row["tenant_id"], "year": row["year"], "quarter": row["quarter"],
        "version_no": row["version_no"], "approval_status": row["approval_status"],
        "plan": plan,
    }


async def fetch_quarter_plan_version_history(
    tenant_id: UUID, year: int, quarter: int, pool,
) -> list[dict]:
    """AA-469 Việc 2 — list EVERY version_no ever saved for this tenant/year/quarter (not just
    the one `current_version_id` points at, which `fetch_current_version_no()` above already
    covers), for a tenant-facing history view. As STEP0 confirmed live (docs/claude_audit/
    AA-469-viec2-step0-t7-edit-history-investigation.md §8), a tenant/quarter can accumulate
    several `approved` versions over time — `approve_quarter_plan_version()` never revokes an
    older version's `approval_status`, it only moves the pointer — so a tenant browsing history
    needs the full list, not just "the current one" or "the most recent one".

    Ordered newest-first (`version_no DESC`) — a history view reads top-to-bottom as "most
    recent first", the same convention most changelog/version-picker UIs use. Pure read, same
    division-of-responsibility as `fetch_quarter_plan_version()` just above: does not enforce
    Gate B, does not decide what a non-approved version means for the caller."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT qpv.version_id, qpv.version_no, qpv.approval_status, qpv.approved_by,
                   qpv.approved_at, qpv.created_at, qpv.source
            FROM acp_shared.quarter_plan_version qpv
            JOIN acp_shared.quarter_plan qp ON qp.plan_id = qpv.plan_id
            WHERE qp.tenant_id = $1 AND qp.year = $2 AND qp.quarter = $3
            ORDER BY qpv.version_no DESC
            """,
            tenant_id, year, quarter,
        )
    return [dict(r) for r in rows]


__all__ = [
    "compute_quarter_plan", "fetch_atoms_by_trip",
    "approve_quarter_plan", "save_quarter_plan_version",
    "approve_quarter_plan_version", "fetch_approved_quarter_plan",
    "fetch_current_version_no", "fetch_quarter_plan_version",
    "fetch_quarter_plan_version_history",
    "QuarterPlanVersionNotFoundError", "QuarterPlanVersionNotPendingError",
]
