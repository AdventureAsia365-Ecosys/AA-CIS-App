# AA-494 — Year-Scope (Old N4 Runway Map) Legacy Code Investigation — STEP0

**Type:** Read-only investigation. No code changes, no migrations, no commits, no PR in this
pass — confirms/refutes a claim made secondhand in `docs/claude_tasks/AA-494-design-atom-angle-
piece-reuse.md`'s Decision 6, whose evidence base that document itself flagged as unverified.

**Headline answer:** The old N4 runway-map algorithm is **NOT dead code** — it survived the
N0-N8 → A0-A3/T0-T11 rewrite intact, lives at `services/acp_planning/runway.py`, and is
**currently live and wired into T7's own tenant-facing endpoints** (`api/routers/
v1_planning.py`), plus T8's angle-gate CTA lookup and the feedback-loop trip-reallocation
module. This directly overturns the task's framing premise ("the year-scope compute logic
appears to have been left behind, not ported, when T7 was rewritten") — it was ported, and it
is in active production use today, not merely "surviving in disconnected form."

A second, more consequential finding: the "new connector layer between RunwayMap and atom-
level planning" that AA-494's Decision 6 flagged as **genuinely new, undesigned work** already
exists and is already live — `services/acp_planning/allocator.py::compute_slot_grid()` already
takes a computed `RunwayMap` + per-tenant `atoms_by_trip` and produces month-level slots, and
`v1_planning.py` already calls it this way. Decision 6 of the AA-494 design document needs to be
re-read and substantially revised in light of this before any build session starts on it — see
"Correction to AA-494 design doc" at the end of this file.

## Step-by-step findings

### Step 1 — search for surviving N4/runway code
```
grep -rln "runway_map\|yearly_runway\|N4\|runway.py" services/ --include="*.py"
```
11 files matched (see raw output above in session). Not all are the runway module itself — most
are files that reference N4/N5/N6 in docstrings/comments while implementing the downstream
pieces (quarter/month/allocator). The runway module itself is `services/acp_planning/runway.py`.

### Step 2/3 — read `runway.py` in full
- Module docstring: **"N4 Runway Map (1x/year + invalidation). Ported from aamc/planning.py's
  runway_map()/D1 (aa-marketing-v2 research build)."** This confirms the file is literally the
  ported old-architecture N4 logic, not a rewrite from scratch.
- `compute_runway_map(tenant_id, year, trips, markets) -> RunwayMap` (line 155) is the pure
  compute core — no DB, no LLM, takes a real `year: int`, produces a `RunwayMap` of
  `RunwayCell(destination, market, month, stage)` across all 12 months × every destination ×
  every configured market, with stage ∈ {TOFU, MOFU, BOFU, OFF} derived from booking-window
  distance geometry (`_stage_for_dist()`, `_offsets_for_destination()`) — not a hardcoded ratio.
  This matches the AA-494 design doc's Decision 6 description of the algorithm exactly.
- **Two real documented bug-fix rounds are present in the code itself, not just claimed**: B9
  (family-detection field bug + single-representative-trip offset bug) and B11 (MOFU band
  unreachable for common season-spacing geometry) — both with root-cause explanations in the
  module docstring and inline in `_offsets_for_destination()`/`_stage_for_dist()`. Both cite
  `docs/implementation-notes/AA-301.md`.
- `parse_duration_days()`'s docstring confirms the 749-row `duration_raw` survey claim: "Built
  from a live survey of 749 non-null v_trip_registry.duration_raw rows (aa_internal tenant,
  2026-07-22)."
- `fetch_trips(tenant_id, pool)` (line 205) has the TEMP hack, confirmed verbatim: reads
  `acp_contract.v_trip_registry` with **no `WHERE tenant_id = $1` filter** — every tenant sees
  the same full `aa_internal`-owned catalog (763 trips as of that comment). Comment dated
  2026-08-13, attributed to "Nghiep decision, AA-323 round 6 Phần B," explicit rationale
  quoted in the code: a per-tenant filter returned 0 eligible trips for every non-aa_internal
  tenant including ones with valid Gate-A onboarding, because the filter checked the wrong
  column; Nghiep's call was to keep the platform-wide catalog until Marketplace/N1 licensing is
  actually built and wired into trip eligibility, with an explicit **"REVISIT WHEN N1 SHIPS —
  see AA-309"** marker. `tenant_id` remains a required parameter of every function signature
  (used for `RunwayMap.tenant_id` and passed through), only the SQL WHERE clause is gone.
- `runway_map()` (async wrapper, line 228) is the only DB-touching function; everything else is
  pure and unit-testable, matching the design doc's claim.

**This IS the same file** referenced in Việc 2's earlier STEP0 as one of the 3 files AA-448 kept
"pure, tenant-safe, unchanged" — confirmed by direct read, not by re-derivation.

### Step 4 — `year_plan` table schema (migration 112)
Confirmed: `acp_shared.year_plan` has exactly 4 columns — `year_plan_id` (PK), `tenant_id` (FK),
`year`, `created_at`. No computable fields (no cells, no stage matrix, no trips_hash). The
table's own COMMENT ON TABLE is explicit: **"pure grouping wrapper around 4 quarter_plan rows
for one tenant/year. Carries no approval/status of its own."** This part of the original premise
is correct — `year_plan` the DB row is inert, and the runway MAP itself is never persisted
anywhere; it's recomputed fresh on every call from live `trips` data (in-memory only, per
`RunwayMap.trips_hash` used for staleness detection, not storage).

### Step 5 — AA-301/AA-320 references
Confirmed present across `runway.py`, `constants.py`, `models.py`, `quarter.py`, `allocator.py`
— all citing AA-301 (the original N4/N5/N6 build) as the source of the port and its documented
self-chosen decisions, and AA-320 (Gate B) for the separate DB-persistence approval layer built
on top of the pure compute functions. No dead/orphaned reference found — every AA-301 citation
sits next to code that is still imported and called today.

### Step 6 — does anything read `year_plan` beyond the grouping FK, and is runway actually wired
into T7's live call chain?
- `quarter.py`'s only touch of `year_plan` is the get-or-create block in
  `save_quarter_plan_version()` (~line 367-396) — INSERT-or-SELECT the grouping row, link
  `quarter_plan.year_plan_id`. Confirms Việc 2's STEP0 was correct: `year_plan` itself is inert
  beyond grouping.
- **However**, `api/routers/v1_planning.py` — T7's real, live, tenant-facing router — imports
  `compute_runway_map` directly (line 52) and calls it **twice**:
  - Line 77-81: computes `runway = compute_runway_map(tenant_id, year, trips, config.markets)`
    then passes it straight into `compute_quarter_plan(..., runway, atoms_by_trip, ...)` — i.e.
    the quarter-plan endpoint tenants actually hit already uses the year-scope runway map as a
    scoring input.
  - Line 199-204: computes `runway` again and passes it into
    `compute_slot_grid(tenant_id, year, month, config.channels, config.capacity_posts_per_week,
    quarter_plan, runway, trips_by_id, atoms_by_trip, config.markets[0])` — the month-level slot
    grid endpoint also already consumes the runway map, combined with per-tenant
    `atoms_by_trip` (via `fetch_tenant_atoms_by_trip`, confirmed tenant-scoped elsewhere in this
    file's own header comment, distinct from the buggy platform-wide fetchers AA-445-02 found).
  - `compute_slot_grid()` itself lives in `services/acp_planning/allocator.py:121`.
- Two more live call sites found beyond T7 itself: `services/acp_angle_gate/service.py`
  (`_compute_and_persist_slot_cta()`, AA-451 — T8's CTA backfill) and `services/acp_planning/
  trip_reallocation.py` (`suggest_trip_reallocation()`, AA-448 round 6 feedback loop) both
  import and call `compute_runway_map()` directly, each with their own tenant-scoped trip/atom
  fetchers.

## Direct answers to the task's questions

**Does the old N4 runway-map algorithm still exist in this codebase, dead or live?**
It exists, and it is **live**, not dead — actively imported and called by at least 4 real
modules (`v1_planning.py` ×2 call sites, `acp_angle_gate/service.py`, `trip_reallocation.py`),
including T7's own production quarter-plan and slot-grid endpoints.

**Is it sound enough to port forward into T7's tenant-scoped shape, or was it built for the old
platform-shared-atom model in a way that doesn't translate?**
It already IS in T7's tenant-scoped shape — no porting work is needed for the compute function
itself. `compute_runway_map()` takes `tenant_id` as a real parameter, has no coupling to
`owner_scope`/atom-ownership at all (it operates on `Trip` objects — destination/period/
duration/lifecycle_stage — none of which changed meaning under ADR-2026-038), and is already
combined with per-tenant atom data (`atoms_by_trip` from `fetch_tenant_atoms_by_trip()`) inside
both `compute_quarter_plan()` and `compute_slot_grid()`. The one real, already-documented defect
is `fetch_trips()`'s unconditional-catalog TEMP hack (Step 3 above) — a data-fetching scope
issue, not an algorithmic one, and it already has a tracking marker ("REVISIT WHEN N1 SHIPS —
AA-309") independent of this investigation.

**If no trace exists, say so plainly.** Not applicable — a full, live trace exists; nothing here
is inferred from the Weekly Report's prose alone. Every claim in the Weekly Report's "Live...
fully deterministic code, no AI cost" description is independently confirmed by direct file
read: pure Python, no LLM call anywhere in `runway.py`, `$0` cost claim holds.

## Correction to AA-494 design doc (flag only, no design proposed here per STEP0 scope)

`docs/claude_tasks/AA-494-design-atom-angle-piece-reuse.md` Decision 6 currently states:

> "The old N5/N6 layer (quarter.py/allocator.py) that used to bridge this gap was built for the
> platform-shared-atom model and has never been wired to per-tenant atoms. This bridge is
> genuinely new design work..."

This is **not accurate as written** — `quarter.py::compute_quarter_plan()` and
`allocator.py::compute_slot_grid()` are the current, live, already-tenant-scoped N5/N6
successors, already wired to per-tenant atoms (`atoms_by_trip` from `fetch_tenant_atoms_by_trip
()`) and already consuming `RunwayMap` as an input, called today from T7's real router. The
"connector layer between RunwayMap and atom-level planning" Decision 6 asks a future session to
design **already exists in production**. Before any build session starts on Decision 6, it needs
a fresh design pass that starts from "what does `compute_slot_grid()` already do and where does
it fall short of the flexibility Nghiep described (atom available anywhere in the month, not
locked to one week)" — not from "build a connector from scratch." This file does not attempt
that redesign — flagging it here per this STEP0's scope (no design, no code).

The design doc's Decision 6 caveat section ("re-read `services/acp_planning/runway.py` directly
before writing any code") should also be updated: `runway.py` itself needed no correction — the
correction is entirely in Decision 6's characterization of the layer *above* runway.py.
