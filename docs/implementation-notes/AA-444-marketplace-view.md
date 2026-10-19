# AA-444 — Marketplace view tổng hợp (tenant_tour_versions + tour_atoms)

Implementation notes, written incrementally while building (not batched at the end).

## Decisions

1. **Route naming — `GET /v1/marketplace`, new file `api/routers/v1_marketplace.py`.**
   ADR-2026-038 §0.3 explicitly frames Marketplace as "view tổng hợp T4+T6" — NOT a
   distinct new T-series stage in its own right (T4 = Tenant Tour Pool, T6 = Atom
   Curation already own those two tables). Giving it a `T<n>` slug would misrepresent
   the ADR's own framing (implying a pipeline stage that doesn't exist). Chose the
   `/v1/*` prefix (not `/admin/marketplace/*`, already taken by the OLD pre-tenant
   `admin_marketplace.py` — see Decision 3) matching `v1_tours.py`'s own convention
   for tenant-JWT-only, read-mostly resources (`/v1/tours/pool`, `/v1/tours/my-versions`)
   — this endpoint reuses that exact `get_tenant()` dependency (imported from
   `v1_tours.py`, not redefined) since it is auth-identical: tenant Bearer JWT only, no
   staff/admin path (this view has no cross-tenant use case — A4 oversight, mentioned
   in the ADR as one motivation, is a separate future admin-tier view, not this one).

2. **Frontend route — `/portal/marketplace` (no T-number), new `MarketplaceTab.tsx`.**
   Same reasoning as (1): not a T-stage. Styled directly off `AtomsTab.tsx` (T6) per
   the task's own suggested template — reuses this portal's `ui.tsx` tokens
   (Card/Badge/LoadingScreen/EmptyState), no admin styling. No action buttons (view is
   read-only per the task's own framing — T1/T6 already own the mutation UI for
   selecting/curating).

3. **`marketplace_portfolios` — NOT fully deprecated; narrowed, and clearly re-labeled.**
   Full grep before any change (see below) found the table's only real writer/reader
   paths — `admin_marketplace.py` (`save_portfolio`/`finalize_portfolio`) and
   `admin.py`'s `POST /admin/tenants/{id}/seed-atoms` — both serve the N1 **pre-tenant**
   onboarding flow (AA-309/AA-330, D4 Mode A: staff curates a draft tour list for a
   tenant that doesn't exist yet, then seeds `tenant_atom_state` from it once the real
   tenant is created). This is a real, live, hard **FK dependency**, not just an old
   habit: `acp_shared.tenant_onboarding.portfolio_id` is `NOT NULL REFERENCES
   acp_shared.marketplace_portfolios(portfolio_id)` (migration 098). Removing this
   table's writes would break tenant onboarding outright, and the task explicitly
   forbids a migration in this issue ("KHÔNG viết migration xóa bảng/cột"), so there is
   no schema-change path available here to lift that FK today. **This is exactly the
   "case cannot be transitioned immediately" the task asked to be reported, not
   silently skipped** — see the grep table below for the full disposition of every
   usage found.

   What *did* change: every place whose comment described this table as if it were
   (or could be mistaken for) a live "what is this tenant currently using" source has
   been corrected to point at the new view (`GET /v1/marketplace`) and clarify the
   table's real, narrower remaining role (N1 onboarding seed source only). No
   read/write logic was touched in `admin_marketplace.py` or `admin.py`'s
   `seed_tenant_atoms` — only comments/docstrings.

## Grep result — every `marketplace_portfolios` usage, before any change

| File:location | What it does | Disposition |
|---|---|---|
| `api/routers/admin_marketplace.py` (module docstring, `save_portfolio()` INSERT, `finalize_portfolio()` SELECT+UPDATE) | Staff-only (`x-admin-secret`) draft/finalize portfolio flow, pre-tenant N1 onboarding (D4 Mode A) | **Kept, unchanged logic.** FK dependency (see Decision 3). Docstring updated to point at the new view and narrow its stated scope. |
| `api/routers/admin.py:122` (comment, `create_tenant()`) | Describes how a new tenant's tours get seeded ("seeded from a finalized ... marketplace_portfolios row") | **Comment updated** — clarified this is the N1 onboarding seed step specifically, not a description of the tenant's ongoing/current tour set (that's `GET /v1/marketplace` now). |
| `api/routers/admin.py:~917` (`seed_tenant_atoms()`, SELECT portfolio_id/tour_ids/status) | Validates the portfolio is finalized before seeding `tenant_atom_state` | **Kept, unchanged logic.** Same FK dependency. Comment added noting the deprecation-for-other-purposes + why this one call site is the exception. |
| `api/routers/admin.py:~1032` (comment, `get_tenant_mirror()`) | Already said "never reads atom_snapshot ... can go stale" | **Comment extended** — added an explicit pointer to `GET /v1/marketplace` as the real live source, since that's now a real answer to the question this comment was raising. |
| `tests/unit/test_aa309_tenant_onboarding.py` | Tests the N1 onboarding flow (kept as-is) | No change — tests the kept code path. |
| `tests/verify_scripts/aa389_gate_a_bypass_verify.py` | Live-verify script for Gate A (kept flow) | No change. |
| `api/migrations/097_*.sql`, `098_*.sql` | Table definition + FK | No change (no migration allowed in this task; FK is exactly why full deprecation isn't possible yet). |
| `frontend/app/admin/marketplace/*` | Staff-only UI calling `admin_marketplace.py` (N1 onboarding curation tool) | No change — different feature/audience than the new tenant-facing page, not in scope. |
| `.claude/CLAUDE.md` (DB SCHEMA section) | Lists `marketplace_portfolios` under `acp_shared.*` | Updated with a one-line pointer to this note + the new view, so a future session doesn't reintroduce it as a live tenant-state source. |

**Post-change grep is expected to still show the same file set** (admin_marketplace.py,
admin.py, the two migrations, the two kept tests) — this is the reported exception, not
an oversight. Zero *new* read/write call sites were added anywhere.

## Changed

- New: `api/routers/v1_marketplace.py` — `GET /v1/marketplace`.
- New: `frontend/app/(tenant)/portal/marketplace/page.tsx` +
  `frontend/app/(tenant)/portal/_components/MarketplaceTab.tsx`.
- `frontend/app/(tenant)/portal/_components/Sidebar.tsx` + `layout.tsx` — new nav entry.
- `api/main.py` — registered the new router.
- Comment-only edits: `api/routers/admin_marketplace.py`, `api/routers/admin.py`,
  `.claude/CLAUDE.md`.
- New tests: `tests/unit/test_aa444_marketplace_view.py`.

## Tradeoffs

- Grouped by **latest version per published tour** (`DISTINCT ON (published_tour_id)
  ORDER BY version_number DESC`), not every historical version — the ADR's own
  wording ("tour đã chọn") reads as "which tours is this tenant currently working
  with", matching `CatalogTab.tsx`'s (T4) own "current state per tour" framing;
  full version history already has a home (T4's own `VersionHistory.tsx`). Flagged
  as a deliberate choice, not re-litigated with Nghiep given the task's own
  read-only/no-new-decision framing.
- Atom aggregate is a `LEFT JOIN` (0-atom tours included, not filtered out) —
  deliberately surfaces the gap where a tenant has a rewritten tour but T5 atomize
  hasn't produced anything yet (still useful signal for T7 planning, per the ADR's
  own stated purpose (b)/(c)), rather than hiding it.
- `runway_months()` is computed per tour using the tenant's real
  `shared.tenants.posts_per_week` (not client-supplied) — same pure function AA-440
  confirmed reusable, no new formula written.

## Should know

- This view is intentionally **separate** from `admin_marketplace.py`'s catalog
  browse (`_CATALOG_QUERY`) — that query browses the **whole platform catalog**
  (any tour, pre-selection) for pre-tenant onboarding; this new endpoint shows only
  tours a specific tenant has **already** selected/rewritten (`tenant_tour_versions`
  rows must exist). Different question, correctly not sharing a query.
- No new migration. Verified live (see Verify section) that both source tables are
  already tenant-scoped with real data for a real tenant before writing the query.

## Verify — real results

**Unit tests**: `pytest tests/unit/` — 1367 passed, 0 failed (1359 pre-existing +
8 new in `test_aa444_marketplace_view.py`). No regressions.

**Lint**: `flake8` clean on every changed/new file. `tsc --noEmit` clean across the
whole frontend (0 errors) after adding `MarketplaceTab.tsx`/`marketplace/page.tsx`
and touching `Sidebar.tsx`/`layout.tsx`.

**App boot sanity**: `python -c "import api.main"` — app builds its route table
successfully with `/v1/marketplace` present alongside the still-live
`/admin/marketplace/*` routes (confirms the router registration + import chain,
including the `from api.routers.v1_tours import get_tenant` reuse, has no circular-
import issue).

**Live DB query, real tenants** (S3-mediated ECS exec pattern, `aa-cis-dev-db`,
23/08/2026 — ECS `aa-cis-dev-api` and RDS were both already running at session
start, desired=1/running=1 and `available` respectively, not started by this
session). Ran the exact `_MARKETPLACE_QUERY` SQL (copied verbatim from
`v1_marketplace.py`) directly against the real DB for the two real tenants the
task suggested, found via a real live scan (no tenant guessed):

| Tenant (slug) | tenant_id | Tours returned | Atom counts |
|---|---|---|---|
| `test-agency` | `9fb0a3db-59aa-468a-a082-ded01ac50bee` | 2 (Tokyo Bay bicycle tour, Laos 14-day) | 15, 0 — the 0-atom row confirms the deliberate LEFT JOIN (a rewritten tour with no atoms yet still shows up) |
| `test-n1-flow` | `6fbaf284-e3cd-4b4b-b53b-c9a04e8fae8e` | 1 (Southern Laos 5-day) | 8 |

**Isolation check** (the task's explicit "không lẫn tenant khác" requirement):
- The two tenants' result sets share **zero** `tour_id`s (disjoint sets) — no
  cross-tenant tour leakage even by coincidence in this data.
- For every tour with `atom_count > 0`, independently re-queried
  `count(*) FROM tour_atoms WHERE tour_id=<t> AND owner_scope=<this tenant>` — matched
  the view's own count exactly (15=15, 8=8) — **and** separately confirmed
  `count(*) ... WHERE owner_scope <> <this tenant> AND owner_scope <> 'platform'` on
  the same `tour_id` = **0** for both, i.e. no other tenant's atoms on the same
  tour leaked into either tenant's count.

## Post-merge / post-deploy live verification (23/08/2026)

PR #197 merged (squash) into `main` at Nghiep's explicit instruction ("CI green thì
merge luôn"). `Deploy Dev` (run 32626080852) completed successfully — new ECS task
definition `aa-cis-dev-api:125`, task started 14:41:54 +07, `healthStatus: HEALTHY`,
workflow's own smoke test passed.

Live-verified the real deployed endpoint (`https://api-cis.lumiguides.it.com/v1/marketplace`,
not just the DB query directly) using real minted tenant JWTs (`api.routers.auth._create_jwt`,
same secret the real app decodes with), via the S3-mediated ECS exec pattern, from inside the
freshly-deployed task itself:

| Call | Result |
|---|---|
| `test-n1-flow` real JWT, `GET /v1/marketplace` | **200**, `tenant_id` matches, `posts_per_week: 3`, 1 tour ("Southern Laos: Plateau, Temples & the Four Thousand Islands — 5 Days"), `atom_count: 8`, `runway_months: 0` — **exact match** to the pre-merge direct-DB-query verify above |
| No `Authorization` header | **401** `{"detail":"Not authenticated"}` — FastAPI-level rejection, confirms JWT check happens in-app (same AA-432 pattern as every other `/v1/*` route) |
| `test-agency` real JWT, `GET /v1/marketplace` | **403** `{"detail":"Tenant is deactivated"}` |

The `test-agency` 403 is **not a bug in this endpoint** — confirmed by making the
identical call, same JWT, against the pre-existing unrelated route
`GET /v1/tours/pool?page_size=1` on the same live deploy: **same 403, same exact
body**. `shared.tenants.is_active=false` for `test-agency` today (pre-existing state,
not something this task touched) trips `rate_limit_middleware` before either route's
own handler ever runs — proof this new endpoint correctly sits behind the same
shared auth/activity gate as every other tenant route, not a defect introduced here.

## `marketplace_portfolios` grep, before vs. after

Identical file set both times
(`admin_marketplace.py`, `admin.py`, migrations 097/098, the two pre-existing kept
tests, plus this task's own new test which only asserts the string's *absence* from
the new query, and the `.claude/CLAUDE.md` deprecation note added by this task) —
confirmed no new read/write call site was introduced anywhere, and the one
un-transitioned call site (`seed_tenant_atoms()`) is exactly the FK-bound exception
flagged in the Decisions section, not a missed spot.
