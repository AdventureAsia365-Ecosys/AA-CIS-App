# Segment/Score/Route/Hub will become platform-wide (decision to fix the tech debt)

- **Status:** Accepted (06/09/2026, AA-542). Implemented by AA-545: Segment, Score, Route and Hub are computed
  platform-wide (see ADR 0001 and CONTEXT.md). Status line added 19/10/2026 (AA-888) from the decision record.

`docs/adr/0001-atoms-platform-wide-segments-per-tenant.md` records WHY Segment/Score/Route/Hub
ended up per-tenant (a carried-over historical default from before Atom became platform-wide,
plus a reverted attempt during AA-526 that hit a scope wall). That ADR does not, by itself,
decide what to do about it. This ADR is that decision, kept separate on purpose (AA-542,
06/09/2026) so the historical record (0001) and the forward-looking commitment (this one) don't
get conflated.

## Context

Nghiệp asked the direct question that settled this: "which step actually uses a tenant's own
brand voice?" Working back through the pipeline (see `CONTEXT.md`'s ownership table, AA-540),
the answer identifies a single layering principle:

**Any step that does NOT read a tenant's own brand voice or a tenant-specific DFS/keyword signal
should NOT be per-tenant, regardless of what the current code does.**

Checked against that principle:

- **Atom** — reads A1's neutral, brand-agnostic rewrite (AA-535). No tenant voice. Platform-wide
  since AA-526. ✅ Matches the principle.
- **Search Demand** — a fact about the outside world (search volume, PAA), not tenant content.
  Platform-wide cache since its own design (migration 130). ✅ Matches the principle.
- **Segment** — matched deterministically on place-token-similarity + verb-match over Atom data
  (place+action pairs). Never reads brand voice or a tenant-specific DFS signal. **Per-tenant
  today. ❌ Does not match the principle.**
- **Score (Atom Ranking)** — a rank-sum over Segment + Search Demand signals. Neither of its
  inputs is tenant-voiced. **Per-tenant today. ❌ Does not match the principle.**
- **Route / Hub** — grouped from ranked Segments by day-span/tour-id family. No tenant voice
  involved. **Per-tenant today. ❌ Does not match the principle.**
- **Slate / Subject onward (Goal, Angle, Write, Gate, Piece, Publish)** — genuinely reads a
  tenant's brand identity, Goal choice, and Angle choice. **Per-tenant, and correctly so** — out
  of scope for this ADR.

Segment/Score/Route/Hub are the only steps in the chain that are per-tenant in the live schema
while failing the principle above. That gap is what AA-509 (speced before Atom was
platform-wide) and AA-526 (attempted the fix, reverted for scope reasons) left behind — see
ADR-0001 for the full trace.

## Decision

Segment, Score (Atom Ranking), Route, and Hub will be redesigned to be computed once,
platform-wide, for the whole Master Content pool — the same model Atom and Search Demand already
use — instead of being recomputed independently per tenant. This will happen in a **separate
design/build issue**, not this one: AA-542 is documentation-only and does not touch schema or
code (see `docs/adr/0001-*.md`'s Decision section for the same scope boundary).

**Until that redesign issue is designed and built, no new Admin or Tenant UI/feature may be built
on the assumption that Segment/Score/Route/Hub is, or should remain, per-tenant.** `CONTEXT.md`
carries this as a standing warning at the top of the file so it surfaces on every read, not just
this ADR.

The redesign issue will need to resolve, at minimum (not decided here):
- How `atom_segment.tenant_id NOT NULL` and `atom_ranking`'s tenant-scoped read (the two concrete
  blockers AA-526 hit) get migrated without breaking already-shipped AA-509/510/511/515
  contracts.
- Whether Route/Hub's per-tenant identity composites (`route_id = tenant_id:tour_id:day-range`,
  `hub.tenant_id`) can be safely re-keyed platform-wide, or need a compatibility/migration path
  for existing rows.
- Whether Slate/Subject (which correctly stay per-tenant) need any interface change to consume a
  platform-wide Score/Route instead of their current per-tenant read.

## Considered Options

- **Leave as per-tenant, formally accept it as correct design.** Rejected — it fails the
  layering principle Nghiệp set, and the only reasons it has stayed per-tenant this long
  (sequencing accident at AA-509, scope-avoidance at AA-526) are not technical justifications;
  ADR-0001 traces both.
- **Redesign immediately, inside AA-542.** Rejected — explicitly out of scope for this issue per
  its own instructions; a schema/code change touching 3+ already-shipped modules deserves its
  own scoped design/build issue, not a doc-fix issue.
- **Redesign platform-wide in a separate, future issue; freeze new per-tenant-assuming build in
  the meantime.** **Chosen.** Lets Claude Code and any human keep working on unrelated parts of
  the pipeline without waiting on the redesign, while preventing the debt from compounding (no
  new feature gets built that would need to be re-migrated later).

## Consequences

- `CONTEXT.md`'s Segment/Score/Route/Hub definitions, its ownership table (rows 3/5/6), and its
  Open Question 1 resolution all now point here and carry the same warning, so any future read
  of that file surfaces this decision before new per-tenant-assuming work starts.
- T7 (Planning), T8/T9 (which read Route/Segment indirectly via Slate/Subject), and any future
  admin-oversight UI over Segment/Score/Route/Hub should treat their current per-tenant shape as
  provisional, not a stable contract to build further abstractions on top of.
- The redesign issue itself is not created by this ADR — Nghiệp decides when to schedule it. This
  ADR only fixes what the documentation says the design **should** be, per the earlier
  Consequences note in ADR-0001 this replaces the ambiguity of.
