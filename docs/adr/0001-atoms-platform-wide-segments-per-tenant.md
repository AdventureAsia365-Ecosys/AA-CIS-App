# Atoms are platform-wide; Segment/Score/Route/Hub per-tenant is unresolved tech debt

Atomize used to be a tenant-triggered, per-tenant step (old T5). AA-526 (05/09/2026) moved atom
generation to A3 (`services/export/handler.py::process_export()`, right after a tour enters
`gold_aa_internal.published_tours`) — atoms are now generated exactly once, platform-wide
(`owner_scope='platform'`), the moment a tour becomes Master Content, not once per tenant that
later rewrites it.

Segment matching, Atom Ranking (Score), and Route/Hub detection were NOT moved to that same
platform-wide, computed-once model — `acp_contract.atom_segment.tenant_id` and
`atom_ranking`'s tenant-scoped read are real, enforced foreign keys, not a bug. **They ARE,
however, unresolved tech debt — not a design decision that should stay as-is** (AA-542,
06/09/2026; see Decision below).

**Correction (AA-541, 06/09/2026 — read the real git history, not just AA-526's own summary)**:
this ADR originally said the reason was "two tenants rewriting the same tour into different
brand voices need different Segment groupings." That is **not what the evidence shows** and has
been removed. The real, traced reason:

- Segment was specified and built at AA-509 (`5f6a740`, 01/09/2026), 4 days *before* AA-526 made
  atoms platform-wide. At that time Atom itself was still a per-tenant resource (the old
  tenant-triggered T5 endpoint) — the AA-509 build task's own literal opening sentence
  (`docs/claude_tasks/AA-509-segment-build.md:12-13`) defines Segment as grouping atoms "qua
  nhiều tour của **cùng tenant**" (across multiple tours **of the same tenant**). Per-tenant
  wasn't a considered choice weighed against a shared alternative — the premise a shared Segment
  would even require (shared atoms) didn't exist yet, so the question was never live.
- When AA-526 later made atoms platform-wide, a platform-wide Segment WAS tried
  (`docs/implementation-notes/AA-526.md`'s own "STEP0 correction #3"; commit `5f2b438`, item 3)
  and reverted for two concrete blockers hit in that moment, not a fresh design debate:
  `atom_segment.tenant_id UUID NOT NULL` crashes on a non-UUID `'platform'` value outright, and
  even past that, `atom_ranking.py` (AA-515, already shipped) reads Segments
  `WHERE asg.tenant_id = $1::uuid` for one specific tenant — a platform-scope Segment row would
  be invisible to every tenant's ranking read regardless. Fixing this properly would have meant
  redesigning Ranking/Route/Slate too, out of AA-526's own scope; the atom-read query was
  adjusted instead to keep Segment a per-tenant product built from the now-shared atom pool.
- Atom itself carries no tenant voice to justify per-tenant grouping either way — it's a bare
  `place`+`action` pair extracted from A1's neutral, brand-agnostic rewrite (AA-535), before any
  tenant has touched the tour. The "different voices" framing this ADR used to state does not
  hold up against what an Atom actually contains.

## Status

**Accepted** — Atom platform-wide half.
**Tech debt, fix required — NOT accepted design** — Segment/Score/Route/Hub per-tenant half
(AA-542, 06/09/2026). This ADR records WHY the per-tenant default exists (the history above and
Considered Options below); it does not endorse per-tenant as correct going forward. See Decision
below and `docs/adr/0003-segment-score-route-hub-will-become-platform-wide.md` for the decision
to fix it.

**Segment/Score/Route/Hub scope: superseded — now platform-wide (AA-545); see CONTEXT.md.** The
per-tenant tech debt described above has been resolved: Segment/Score/Route/Hub are computed
once platform-wide, scoped by `tour_id`, not per tenant. CONTEXT.md is the current source of truth.

## Considered Options

At AA-509 (01/09/2026): none — Segment was speced as per-tenant from its first written task
description, before a shared-atom alternative was even possible. At AA-526 (05/09/2026): a
platform-wide Segment was actually attempted and reverted — not on algorithmic grounds (the
Jaccard/verb-match grouping genuinely doesn't need to know which tenant an atom belongs to), but
because `atom_segment`'s `NOT NULL` tenant FK and `atom_ranking`'s already-shipped tenant-scoped
read would have needed a coordinated redesign across 3 modules, which was out of that task's scope.

## Decision (AA-542, 06/09/2026)

Segment/Score/Route/Hub will be redesigned to platform-wide (computed once for the whole Master
Content pool — the same model Atom and Search Demand already use) in a separate design/build
issue — **before** any new Admin or Tenant UI/feature is built on top of the current per-tenant
assumption. The governing layering principle (stated in `CONTEXT.md`'s warning banner, decided
by Nghiệp): **any step that does not read a tenant's own brand voice or a tenant-specific
DFS/keyword signal should not be per-tenant, regardless of what the current code does.**
Segment/Score/Route/Hub read neither. Full decision record and rationale:
`docs/adr/0003-segment-score-route-hub-will-become-platform-wide.md`.

This issue (AA-542) is documentation-only — it does not redesign the schema or code. The
redesign itself (schema/code changes across AA-509/510/511/515's shipped contracts) is out of
scope here and tracked as a future issue.

## Consequences

A future spec or glossary that assumes "Segment/Score/Route are computed once for the whole
Master Content, admin-side" (as AA-539's own initial description did) was, before AA-542,
treated as contradicting the live schema; `CONTEXT.md`'s cross-tenant table (AA-540) documented
the current reality without picking a side. AA-542 now settles the "should it stay this way"
question: **no** — see Decision above. Separately (AA-541): because Segment-matching is pure CPU
(Jaccard/verb-match, no LLM/API call — confirmed, `services/acp_contract/segment_matching.py`
makes zero external calls), the cost of N tenants each independently recomputing an identical
Segment set for the same shared tour is CPU/storage duplication, not a real dollar cost the way
Search Demand's per-call DataForSEO/Bedrock spend was (`docs/adr/0002-search-demand-shared-
across-tenants.md`) — a plausible reason this was never revisited with the same urgency, though
it was never actually evaluated on those terms either. **That** the redesign will happen is now
decided (see Decision above); the **how/when** — the concrete migration/schema plan across
AA-509/510/511/515's shipped contracts — remains open, to be resolved in the future design/build
issue, not by this ADR.
