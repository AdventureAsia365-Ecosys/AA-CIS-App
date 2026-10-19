# AA-494 — Atom/Angle/Piece Reuse Model — Design Document

**Status:** Design complete. Build Step 1 (the `choose_angle()` unset-others prerequisite
fix, Decision 3's mandatory blocker) has SHIPPED — merged, deployed, live-verified; see
`docs/implementation-notes/AA-494.md`. Nothing else in this doc is built yet. Do not
start building the rest without re-reading this doc in full — the 6 decisions below
interlock; building one in isolation risks rework on the others.

**Linear:** AA-494. **Blocks:** AA-469 Việc 2 (T7 edit) and Việc 4 (T8-T11 fixes) —
those should not be built until this schema lands.

**Source audits (read if a decision's rationale needs re-checking):**
- docs/claude_audit/atom-angle-piece-reuse-model-investigation.md (original STEP0)
- docs/claude_audit/AA-494-status-dependency-map.md (status field dependency map)
- docs/claude_audit/AA-494-year-scope-legacy-code-investigation.md (runway.py/
  fetch_trips() STEP0 — completed 29/08/2026; confirmed compute_runway_map() is live
  and sound, corrected Decision 6's connector-layer claim — see Decision 6 below)
- docs/claude_audit/AA-494-decision6-slot-grid-week-flexibility-investigation.md
  (follow-up STEP0 — confirmed compute_slot_grid()'s week-lock is real but does not
  reach T9's actual write path; also folded into Decision 6 below)

## 1. Problem statement

T8 (AA-449) was built as a strict one-shot: `create_request()` → `set_goal_and_
generate()` → `choose_angle()` → done, with no path back once a request reaches
`approved`. Nghiệp's actual product requirement is N:N reuse — one atom should be
writable many times over (different goal, different occasion, different channel), the
3 angles generated per (atom, goal) should all persist so a later return to that pair
can pick from the 2 unused ones without a new LLM call, and channel should be a
per-write choice rather than something locked in at request creation. Concrete example
Nghiệp gave: a week's slot has 4 candidate atoms, only 3 posts/week are allowed, so 1
atom gets written and published while the other 3 sit untouched; if the plan is edited
later, those 3 untouched atoms should not need to be re-computed or regenerated from
scratch — they're still valid, unused inventory.

## 2. Six decisions (final, do not re-open without new evidence)

### Decision 1 — Channel moves to content_piece
Channel is no longer chosen at request creation. It is chosen at write time, alongside
the angle. One angle can now be used to write for multiple channels across multiple
pieces.

**Schema impact:** remove `channel` from `acp_shared.angle_gate_request` (or keep the
column but stop writing to it going forward — decide during migration write-up whether
a clean drop or a deprecate-in-place is safer given existing rows; see Section 5).
Add `channel` to `content_piece` if not already present (check current schema first —
Việc 4's audit found content_piece already tracks channel somewhere; confirm exact
column before assuming this is new).

### Decision 2 — content_piece keeps 3 foreign keys
`content_piece` stores `request_id`, `angle_gate_option_id`, AND `channel` — all three,
not derived via JOIN. Deliberate denormalization for cheap history queries.

**Schema impact:** add `angle_gate_option_id UUID REFERENCES acp_shared.angle_gate_option(option_id)`
to `content_piece` if not present. Confirm `angle_gate_option` table's real PK name
from migration 113 before writing this FK (do not assume `option_id` — verify).

### Decision 3 — angle_gate_request.status: add new value, don't remove status
Scenario A wins over Scenario B (dropping status down to content_piece), because
content_piece rows don't exist until T9 write starts — too late to answer "has an
angle been chosen for this request" at the point T8's UI needs to know it.

**Concrete mechanism:**
- Status column stays a CHECK-constrained TEXT (confirmed free to extend, not a hard
  enum) on `acp_shared.angle_gate_request`.
- Add a new value — proposed name `reusable` — reached from `approved` once at least
  one content_piece has been successfully created from this request. (Exact trigger
  condition — "on first successful piece" vs. "immediately after choose_angle()" —
  needs to be decided during migration write-up; recommend the former, since a request
  that's approved-but-never-written arguably shouldn't yet advertise itself as
  "reusable" for a second write.)
- `services/acp_content_writing/service.py::start_write()`'s existing guard
  (`status == "approved"`) needs widening to accept `status IN ("approved", "reusable")`
  — do not remove the guard, just extend the accepted set.

**Mandatory prerequisite fix (blocks this decision, not optional):**
`choose_angle()` in `services/acp_angle_gate/service.py` sets `chosen=true` on the
selected angle_gate_option row but never sets `chosen=false` on the other two. This is
currently harmless (nothing re-reads angle choice after `approved`, per the one-way
ratchet), but the moment `reusable` status allows picking a *different* previously-
unused angle for a second write, `content_writing`'s code that resolves "which angle
was chosen" (need to re-locate this exact read in T9's service.py — grep `chosen` in
`services/acp_content_writing/`) will silently pick the WRONG option (first
`chosen=true` row by insertion order, not the most recently chosen one) unless this is
fixed first. **Fix this as its own small PR before any of Decision 3's other schema
changes land.**

### Decision 4 — Angle-generation prompt gets prior-piece history (within-tenant, hybrid)
When generating angles for an atom that already has prior content_piece rows (same
tenant), the prompt should include:
1. A short 1-2 sentence summary per prior piece — generated once, at piece-creation
   time, by the same LLM call that writes the piece (near-zero marginal cost — one
   extra output field in the existing write_content()/rewrite_with_feedback() call).
   Persist this on content_piece (new column, e.g. `content_summary TEXT`).
2. An embedding vector of the full piece text — for deep-check filtering, not routine
   inclusion in every angle-gen prompt. Only pulled and compared in full when a
   similarity check flags a candidate as suspiciously close (see Decision 5's shared
   mechanism).

**Schema impact:** add `content_summary TEXT` and `content_embedding vector(1536)` (or
whatever dimension matches the actual embedding model chosen — do not assume OpenAI's
1536 without checking which embedding model AA_Ecosys uses; check ADR log / AA-62 for
the original pgvector column's dimension, migration 041, even though that table was
since dropped — the dimension choice should stay consistent with whatever embedding
call the platform already knows how to make) to `content_piece`.

**Prompt change:** `services/acp_angle_gate/prompts.py::build_user_prompt()` needs a
new optional block — "PRIOR PIECES FROM THIS ATOM" — populated from
`content_summary` of all pieces where `content_piece.angle_gate_option_id`'s parent
request shares the same `atom_id` as the current request, same tenant. Keep this block
short (summaries only) per the cost reasoning above; do not inline full piece text here.

### Decision 5 — pgvector similarity: built fresh, shared mechanism for both tiers
Do not resurrect the dead `content_embedding` column from migration 041 (AA-62) — it's
gone (one of its two host tables was dropped in migration 121) and was designed for a
pre-T-series architecture. Build a new embedding column (see Decision 4's schema
addition — same column serves both purposes) and one shared similarity-check function
used for:
- **Within-tenant (high priority):** compare a new piece's embedding against the same
  tenant's prior pieces from the same atom, at generation or pre-publish time — exact
  trigger point (angle-gen time as an advisory signal vs. T10 gate as a hard check)
  needs to be decided during build, not assumed here.
- **Cross-tenant (low priority, confirmed low real risk):** each tenant's atoms already
  differ in content from T2's tenant-specific rewrite before T5 atomizes them — two
  tenants choosing the same source tour do not produce similar atoms already. Residual
  risk is two independently-written pieces from two different atoms coincidentally
  converging. This tier's check can run less frequently / at lower priority than
  within-tenant — e.g. an A4-driven periodic scan rather than a per-piece gate — since
  it is not a per-write blocker on the product's core flow.

**Do not build this as two separate systems.** One embedding column, one similarity
function, two call sites with different urgency/frequency.

### Decision 6 — REVISED 29/08/2026 (S166): T7 year-scope — keep compute_runway_map(),
the connector already exists live; real gap is the missing T8→slot-grid wiring
This decision was corrected after 2 follow-up STEP0 passes (Claude Code, direct code
read) surfaced that the original version below was wrong on its central claim:
- `docs/claude_audit/AA-494-year-scope-legacy-code-investigation.md`
- `docs/claude_audit/AA-494-decision6-slot-grid-week-flexibility-investigation.md`

**What the original Decision 6 got right, unchanged:**
`services/acp_planning/runway.py::compute_runway_map()` is sound and should NOT be
rewritten. It already:
- Operates at true year scope (`year: int` param, computes a 12-month × destination ×
  market matrix of TOFU/MOFU/BOFU/OFF stages using booking-window distance geometry,
  not a hardcoded ratio).
- Has been through 2 real bug-fix rounds (B9, B11) grounded in real data (a 749-row
  `duration_raw` survey) — confirmed present in the code itself, not just claimed.
- Is a pure function operating on `Trip` objects — it has no coupling to the
  atom/tenant ownership model, so it does not conflict with ADR-2026-038's per-tenant
  atom change.

**The real bug is in `fetch_trips()`** (same file), NOT in the compute function:
- It currently reads `acp_contract.v_trip_registry` unconditionally — the full
  platform Master Content catalog (763 trips, `aa_internal`-owned) — via a documented
  TEMP hack from 2026-08-13 ("REVISIT WHEN N1 SHIPS"). This means runway maps are
  computed against tours the tenant may never have actually rewritten/adopted.
- **Fix:** `fetch_trips()` must read tenant-scoped data — tours the tenant has actually
  written via T1→T2→T3→T4, not raw Master Content. Reuse the Marketplace view pattern
  from AA-444 (`tenant_tour_versions` JOIN `tour_atoms`, filtered by real
  `tenant_id`/`owner_scope`) — this is the same fix direction already validated for
  the analogous `fetch_atoms_by_trip()` bug found during AA-448's STEP0. Do not
  reinvent a different tenant-scoping query; follow the established Marketplace-view
  pattern for consistency.

**What the original Decision 6 got WRONG (do not repeat this claim):** it described a
connector layer between `RunwayMap` and atom-level planning as "genuinely new design
work" that "has never been wired to per-tenant atoms." **This is false.**
`services/acp_planning/quarter.py::compute_quarter_plan()` and `services/
acp_planning/allocator.py::compute_slot_grid()` already exist, are live, already take
`RunwayMap` + tenant-scoped `atoms_by_trip` (from `fetch_tenant_atoms_by_trip()`) as
input, and are already called by T7's real router (`api/routers/v1_planning.py`, both
the quarter-plan endpoint and the slot-grid endpoint). No connector needs to be built
from scratch — it exists and runs in production today.

**The real, confirmed gap (a genuine bug to fix, not an intentional separation —
confirmed with Nghiệp 29/08):** `compute_slot_grid()` hard-locks week+atom together via
an arbitrary round-robin (`week = weeks[slot_n % 4]`, no relationship to the atom
itself), persisted into `acp_shared.acp_v2_slots.week`. But the real T9 write path —
`create_request()` in `services/acp_angle_gate/service.py` — currently reads **no
slot data at all**: a tenant picks `atom_id` + `channel` directly, with zero reference
to `Slot`/`SlotGrid`/`acp_v2_slots` anywhere in that call chain (confirmed by grep —
`services/acp_content_writing/` likewise has zero references). The T8→T9 connection
*to the slot-grid's output* is the missing piece, not the slot-grid computation
itself, and not a new connector between RunwayMap and atoms.

**Product model confirmed with Nghiệp, 29/08 (tentative — needs Ms. Thư sign-off
before build):**
- Slots from `compute_slot_grid()` are a **priority suggestion** surfaced in the
  tenant's weekly view (e.g. "week 3 of September has N pre-arranged atom+channel
  slots to prioritize writing") — **not a hard gate.**
- The tenant can still freely pick any OTHER atom that is still **available within the
  month** (not yet used up) and start T8 (Goal → DFS/PAA+brand → 3 angles → pick 1 +
  channel) → T9 → T10, without being locked to the exact assigned slot/week.
- `compute_slot_grid()`'s arbitrary round-robin week assignment does **not** need
  fixing — it's a display hint, not a constraint, so the arbitrariness is harmless.
- **N7 (the dormant automated weekly-production pipeline) is fully out of scope for
  AA-494 — do not reference it going forward.** It has no trigger built (no cron/
  EventBridge), and the week-lock inside `compute_slot_grid()` only has a live effect
  inside that still-inactive pipeline, not the real tenant self-service path.

**UI/UX design — T7 slot view + T8 atom-picker, approved 29/08/2026 (resolves this
Decision's open items below):**

Corrected data model — a prior misreading (48 posts/month) was caught before it spread;
recording the correct version here so a future session doesn't re-derive the wrong one
from stale context:
- 1 month = 4 weeks × `capacity_posts_per_week` slots/week. `capacity_posts_per_week`
  is a real tenant-configurable value (`shared.tenants.posts_per_week`, 1-14, AA-384),
  not a hardcoded constant — the numbers below use one illustrative tenant's example
  (3/week), not a platform-wide fixed rate.
- Illustrative example: 4 weeks × 3 slots/week = **12 posts/month** (NOT 48 — 48 is
  only the total count of atom-*suggestions displayed*: 12 slots × 4 suggested
  atoms/slot).
- **1 slot = 1 post.** The 4 atoms shown per slot (`compute_slot_grid()`'s eligible-pool
  selection, `n_atoms` atoms per slot) are 4 ALTERNATIVE choices — pick one to write —
  never 4 separate things to write. A slot never produces 4 posts.
- The tenant can also bypass all 4 suggested atoms and pick any other atom "free" within
  the month to fill that same slot instead.
- **Channel is NOT attached to the slot in the UI**, even though `compute_slot_grid()`
  does assign a `channel` field internally. Channel is chosen for real in T8, after
  angle selection — never suggested at the slot level, because that internal assignment
  is arbitrary round-robin (confirmed via STEP0, see this Decision's "real, confirmed
  gap" section above), and surfacing it would falsely imply the system computed
  something meaningful there.
- An atom that reaches an approved (T10-passed) `content_piece` becomes "used" for that
  calendar month and drops out of the free-atom list for that month — see the
  atom-availability rule immediately below for the exact definition.

Approved UI flow (4-level drill-down): **Year** (4 quarters shown) → pick quarter →
**3 months** → pick month → **4 weeks** → pick week → **3 slot-cards** shown side by
side (Post 1/2/3) → click a slot to expand it → two separate areas appear below:
1. "Suggested for this slot" — 4 atom cards, click to pick one.
2. "Other atoms free this month" — row-list with a search box (filter by
   destination/name), no scroll cap since a month can have dozens of free atoms.

UX details approved:
- Breadcrumb (Year▸Quarter▸Month▸Week) at the top, clickable at any level to jump back.
- A slot-card that's already been written flips to a success/green state, showing which
  atom was used and the write date.
- The two atom-selection areas (suggested vs. free) stay visually separate — never
  merged into one list.
- Picking an atom does NOT auto-navigate — it only highlights (accent border +
  checkmark). A separate "Start writing" button (fixed at the bottom, disabled until an
  atom is picked) confirms the choice before handing off to the real T8 flow (Goal → 3
  angles → pick 1 + channel).

**Atom-availability rule for the month — DECIDED 29/08/2026 (named rule; implement
against this exact wording when this part is built — the query/migration itself is
NOT written yet, that's separate future build work):**

An atom is "used" (locked) for calendar month X if there exists at least one
`content_piece` originating from that atom with `status` at or beyond `approved` (i.e.
it has passed T10) whose `created_at` falls within calendar month X — using the actual
write date, not the slot's pre-assigned plan month.

Rationale: slots are a display-only suggestion (per this Decision's revised model, not
a binding schedule) — locking by planned month would produce nonsensical cases, e.g. a
tenant who writes late, in a different month than planned, would see the atom wrongly
still "free" in the month they actually used it.

A `content_piece` that never reaches `approved` (rejected/held at T10, or abandoned)
does NOT lock the atom — it stays free for the same month.

**Still open, NOT designed yet — resolve before building this part:**
1. Whether Ms. Thư needs to review the UI/UX flow above from a content/brand angle
   before build, beyond Nghiệp's technical approval — not decided, do not assume
   either answer.
2. The actual DB query/migration implementing the atom-availability rule above, and the
   query backing the T7 slot-view's drill-down screens — design is done (this section),
   but nothing here has been written as real SQL/migration yet.

**Caveat (unchanged — still correct):** re-read `services/acp_planning/runway.py`
directly before writing any code against its specific mechanics — the module itself
needed no correction from either follow-up STEP0; the correction was entirely in this
Decision's characterization of the layer above it.

## 3. Cross-decision dependencies (why order matters)

1. **choose_angle() bug fix must land before Decision 3's status changes** — otherwise
   Decision 3 ships a feature (pick a different angle later) that immediately produces
   wrong behavior in T9.
2. **Decision 1+2 (channel/FK changes) and Decision 3 (status) touch the same tables**
   (`angle_gate_request`, `content_piece`) — bundle into one migration where sensible,
   rather than three separate migrations each requiring their own review/deploy cycle,
   per the repo's existing "batch related schema changes" convention (see AA-341's
   "làm 1 lượt, không làm lẻ" precedent).
3. **Decision 4/5 (embedding columns) can be added to the same content_piece migration**
   as Decision 2's `angle_gate_option_id` column — same table, same migration, avoid a
   second ALTER TABLE pass.
4. **Decision 6 (runway/fetch_trips fix) is schema-independent of Decisions 1-5** — it
   touches `services/acp_planning/runway.py` and `Trip`-fetching, not
   `angle_gate_request`/`content_piece`. Can be built in parallel by a different
   session/branch without conflict, but the "new connector layer" part should wait
   until Decisions 1-2's schema (channel-on-piece, option-on-piece) lands, since the
   connector's atom-prioritization output likely needs to know about per-channel
   piece history to give good suggestions (avoid designing the connector against a
   schema that's about to change underneath it).

## 4. Suggested build order for next session

1. Fix `choose_angle()` unset-others bug (small, standalone PR).
2. One migration: add `channel` to content_piece (if missing) + `angle_gate_option_id`
   FK + `content_summary` + `content_embedding` columns; extend `angle_gate_request
   .status` CHECK constraint to add `reusable`. Verify exact current column state
   first (STEP0-style check, don't assume nothing already exists) before writing DDL.
3. Backend: widen `start_write()`'s status guard; wire the new "prior pieces" prompt
   block into `build_user_prompt()`; wire content_summary generation into the
   write-content LLM call.
4. Backend: build the shared embedding/similarity function (Decision 5) — decide exact
   trigger points during this step, not before.
5. Frontend: AngleGateTab.tsx changes to support choosing channel at write time and
   selecting a previously-unused angle from a `reusable` request — this will need its
   own small design pass (which UI state shows "2 angles still available") before
   coding, not assumed complete by this document.
6. Separately (can run in parallel with 1-5): fix `fetch_trips()` tenant-scoping. Then
   build the T8→slot-grid wiring — the UI/UX flow and atom-availability rule are
   already designed (see Decision 6's UI/UX section), so this step is real
   implementation, not a fresh design pass: write the atom-availability query/
   migration, wire `compute_slot_grid()`'s output into the T7 slot-view's 4-level
   drill-down, build the T8 atom-picker's two-area (suggested vs. free) selection.
   Get Ms. Thư's content/brand sign-off before or during this build (still open, see
   Decision 6).
7. Only after 1-5 land: resume AA-469 Việc 2 (T7 edit) and Việc 4 (T8-T11 fixes), now
   against the corrected schema.

## 5. Open questions for next session (not yet decided, flag before building)

- Exact trigger condition for `approved → reusable` transition (first successful piece
  vs. immediately after angle choice).
- Whether to drop `angle_gate_request.channel` outright or deprecate-in-place (existing
  rows have a value there; decide if it matters for any historical query).
- Exact embedding model/dimension to standardize on for `content_embedding`.
- Exact trigger point for within-tenant similarity check (advisory at angle-gen vs.
  hard gate at T10).
- AngleGateTab.tsx UX for surfacing reusable angles/channels — not designed at all yet
  (distinct from the T7 slot-view/T8 atom-picker UI, which IS now designed — see
  Decision 6).
- Ms. Thư's sign-off on Decision 6's product model (slots as suggestion, not gate) —
  confirmed with Nghiệp 29/08, still tentative pending her review.
- Ms. Thư's sign-off on the T7 slot-view/T8 atom-picker UI/UX (Decision 6) from a
  content/brand angle, beyond Nghiệp's technical approval — a separate open question,
  not decided either way.
- The actual DB query/migration implementing the atom-availability rule (Decision 6) —
  the rule itself is decided and worded exactly; the SQL/migration is not written yet.
