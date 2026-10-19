# AA-494 Decision 6 correction — compute_slot_grid() week-flexibility check — STEP0

**Type:** Read-only investigation. No code changes, no migrations, no commits, no PR.
Scope: answer ONE open question left by `AA-494-year-scope-legacy-code-investigation.md`
— does `compute_slot_grid()`'s existing output already let an atom be used in any week
within its assigned month (Nghiệp's described requirement), or does it hard-lock an
atom to one specific week? The wiring/tenant-scoping question is already closed by the
prior file and is NOT re-investigated here.

**Headline answer:** `compute_slot_grid()` DOES produce week-level assignments, and
they ARE hard-locked — to an essentially arbitrary round-robin slot, not anything
derived from the atom itself — at T7 plan-compute time. **However this lock turns out
not to matter for the actual tenant self-service write path**: T9 (content-writing)
never reads `Slot.week`, `SlotGrid`, or `acp_v2_slots` at all. The week-lock only binds
inside a separate, still-not-triggered automated pipeline (N7 weekly production via
`acp_v2_runs`/`acp_v2_slots`), not the live T8→T9 flow a tenant actually uses today.

## Step 1 — `compute_slot_grid()` (`services/acp_planning/allocator.py`), full trace

- **Produces week-level assignments, not just month buckets.** `weeks = [1, 2, 3, 4]`
  (week-of-month numbering, 1-4 — explicitly NOT ISO week, per `create_weekly_produce_run()`'s
  own docstring) is fixed up front. Inside the closure `make_slot()` (line 177):
  ```python
  week = weeks[slot_n % len(weeks)]
  ```
  `week` is assigned by a plain round-robin over a module-local `slot_n` counter that
  increments once per slot created — **it has no relationship to the atom, trip,
  channel, or any content property**. It is essentially an arbitrary bucket assignment
  driven purely by iteration order (which destination/trip is being cycled through when
  this particular slot happens to be created).
- **`week` is a hard field on the returned `Slot`**, baked in at construction time
  (`Slot(slot_id=..., week=week, channel=channel, ...)`, line 244) — not optional, not
  a range, not re-derivable later from anything else on the object.
- **The atom(s) for that slot are ALSO locked at this same moment**, not just the week:
  `chosen = pool[:n_atoms]` (line 221) picks specific atoms from the eligible pool
  right then, and `atom_ids=[a.atom_id for a in chosen]` (line 247) is baked into the
  same `Slot` alongside its `week`. So both "which week" and "which atom" are decided
  together, once, at `compute_slot_grid()` call time — an atom is never left as "valid
  for any week this month, pick later."
- **This is then persisted with the week as a real column**, not just JSON payload:
  `persist_slot_grid()` (line 363) writes `s.week` into `acp_shared.acp_v2_slots.week`
  directly (INSERT columns: `slot_id, run_id, tenant_id, week, month, channel, kind,
  tour_id, payload`), and `_deterministic_slot_id()` (line 74) hashes `week` into the
  slot's own id — so the week-lock is structural at the DB layer too, not just an
  in-memory convenience.
- **Nothing in `compute_slot_grid()` re-opens this once computed.** Re-running
  `compute_slot_grid()`/`allocate_month()` for the same month recomputes the WHOLE
  grid fresh from scratch (round-robin restarts at `slot_n = 0`), so a specific atom
  could land on a different week on a re-run — but there is no mechanism, in this
  function or anywhere else read in this investigation, for "same atom, but let me
  choose a different week within the same month for a second write." The lock is real
  within a single computed grid, and recomputing doesn't add flexibility, it just
  reshuffles which fixed lock applies.

**Answer to Q1's three sub-questions:**
- Week-level assignments ARE produced (not just month-level).
- The week is chosen once and IS locked — not soft, not derived at write-time. It's a
  hard field, persisted to a real DB column.
- Data structure: `Slot.week: int` is a required, non-optional field — hard, not
  soft/derived.

## Step 2 — `compute_quarter_plan()` (`services/acp_planning/quarter.py`), one level up

Read in full. `compute_quarter_plan()` operates strictly at **quarter → trip selection**
granularity: it scores trips, picks `trip_ids`, computes `destination_shares`, flags
`big_rocks` (candidate atom clusters for a trip, not scheduled). **It contains no
month or week subdivision at all** — no `week` field, no month-level loop, nothing
downstream of it in this same file touches week-level commitments. Week-level slotting
happens exclusively one layer down, inside `compute_slot_grid()`/`allocator.py` (a
separate function/module), never inside `quarter.py` itself or anything that reads its
output before `compute_slot_grid()` is called.

**Answer to Q2:** No further week-level subdivision exists in `quarter.py` or upstream
of `compute_slot_grid()`. The month→week split is `compute_slot_grid()`'s job alone.

## Step 3 — T9's actual read path (does it read a locked week, or re-derive eligibility?)

Confirmed by direct grep, not memory: `services/acp_content_writing/` (T9's real
package — `service.py`, `generate.py`, `prompts.py`, `quality_gates.py`,
`framework_rubrics.py`) contains **zero references** to `Slot`, `SlotGrid`,
`acp_v2_slots`, or `fetch_due_slots` anywhere.

T9's actual entry point is T8's `create_request(tenant_id, atom_id, channel, pool,
year=None, month=None)` (`services/acp_angle_gate/service.py:159`) — `atom_id` and
`channel` are supplied directly by the caller (the tenant portal UI, presumably from a
T6 atom-picker), **not read from any pre-computed Slot**. The ONLY place this whole
chain touches slot data at all is the optional, best-effort CTA lookup:
```python
_SLOT_CTA_QUERY = """
    SELECT payload ->> 'cta_target' AS cta_target
    FROM acp_shared.acp_v2_slots
    WHERE tenant_id = $1 AND channel = $2 AND payload -> 'atom_ids' ? $3
    ORDER BY created_at DESC
    LIMIT 1
"""
```
This query filters by `tenant_id`, `channel`, and whether the atom appears in that
slot's `atom_ids` array — **it has no `week` or `month` predicate at all** (confirmed
by direct read of the query, both the persisted-lookup version and
`_compute_and_persist_slot_cta()`'s freshly-computed fallback). It exists purely to
find a `cta_target` string to pre-fill; it never blocks or gates which week a write can
happen in, and a request with no matching slot at all still proceeds (T9 has its own
"ask the tenant for a CTA" fallback for that case, per the module's own comments).

**Answer to Q3:** T9 does not read a pre-locked week assignment. It re-derives
eligibility from nothing more than `(tenant_id, atom_id, channel)`, supplied directly
by the caller. The week-lock inside `compute_slot_grid()`'s `Slot` objects is invisible
to this path entirely.

## Step 4 — does the direct question even apply?

`compute_slot_grid()` genuinely does hard-lock an atom to one specific week (Step 1) —
so in isolation, the answer to "does the existing function support flexible any-week
use" is **no, it does not; the lock is real and structural.**

But that lock only has a live effect within the **separate, downstream N7 automated
weekly-production pipeline** — `acp_v2_runs`/`acp_v2_slots`, written via
`persist_slot_grid()`/`allocate_and_persist_week()`. That pipeline's own trigger was
never built (`create_weekly_produce_run()`'s AA-378 note and `allocate_and_persist_week
()`'s own docstring both say explicitly: "a future weekly trigger — explicitly NOT
built in this issue — no router, no cron/EventBridge"). So today, nothing calls this
path automatically; the week-lock exists in the code and, if it did run, would persist
real column data — but it is not currently the mechanism a tenant's actual T8→T9 write
goes through at all.

**The real answer for AA-494's actual purpose (does a tenant's real write flow honor
"any week within the month"?):** the question doesn't apply to the live tenant
self-service path, because that path was never gated by week to begin with —
`create_request()`/T9 accept any `(atom_id, channel)` a tenant supplies at any time,
with no week check anywhere in the call chain. The flexibility Nghiệp described is
**already the de facto behavior of the live write path**, not because
`compute_slot_grid()` was designed for it, but because T9 simply never consults
`compute_slot_grid()`'s week field. The one place the hard week-lock genuinely bites is
the still-dormant automated N7 slot-scheduling pipeline, which is a different, not-yet-
active system.

## What this means for AA-494 Decision 6 (flag only — no redesign proposed here)

Two separate, previously conflated things now need to be told apart when Decision 6 is
rewritten in Claude Chat:
1. **The tenant self-service write path (T8→T9) already has the flexibility Nghiệp
   asked for** — trivially, because it was never wired to week-level data at all. No
   new design work is needed here for that specific behavior.
2. **The automated N7 weekly-production pipeline's `compute_slot_grid()` still hard-
   locks week+atom together**, and that pipeline has no trigger yet. If/when N7's
   automated scheduling is built (a separate, not-yet-scoped piece of work), THAT is
   where the "atom flexible within its month" design question will actually need
   answering — and `compute_slot_grid()`'s current round-robin week assignment (which
   is arbitrary, not atom-driven) would need real reconsideration then, not before.

This file does not judge whether N7's automated pipeline should be built, when, or how
its week-assignment should change — that's future design work, out of this STEP0's
scope.
