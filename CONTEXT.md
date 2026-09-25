# AA-CIS-App — Repo Context

The core application of the Adventure Asia (AA) ecosystem: the **CIS (Content Intelligence
System)** backend + admin/tenant frontends. It is where a tour becomes shared master content and
where a marketplace tenant turns that content into channel-specific published content. This is the
B2B/admin side of the ecosystem — distinct from AA-TripPlanner-Web (the B2C consumer trip
planner) and AA-CIS-Infra (the Terraform that provisions the AWS resources this app runs on).

> This file has two layers. The **Repo Context** below describes the whole repo — stack, layout,
> deploy, boundaries. Everything from **"Content Pipeline Domain"** onward is the detailed domain
> glossary for the Master Content → Tenant Content pipeline (AA-539/540) and is kept verbatim.

## What it is (role in the AA ecosystem)

- **Product surface**: two audiences in one app — **Admin** (AA staff running master content and
  cross-tenant oversight, the A-series) and **Tenant** (marketplace tenants publishing their own
  channel content, the T-series).
- **Position**: the hub product. It **owns** the master content pool and the whole content
  pipeline; TripPlanner reads the shared `shared.destinations` reference data but never drives
  this pipeline. AA-CIS-Infra provisions the cloud resources this app runs on but contains no app
  logic.
- **Ownership split**: this repo owns its application code and its DB schemas/migrations
  (`acp_*`, `*_aa_internal`, `acp_shared.*`, `acp_contract.*`). AWS resources (VPC/RDS/ECS/ALB/
  API GW/Lambda/OIDC) are owned by **AA-CIS-Infra** — "App owns code, Infra owns resources."

## Architecture at a glance

- **Backend**: Python (FastAPI-style routers under `api/routers/`, services under `services/`),
  running on **ECS** (cluster `aa-cis-dev-cluster`, service `aa-cis-dev-api`) fronted by ALB +
  API Gateway. Postgres (RDS) holds the schemas; some steps use Bedrock for LLM work.
- **Frontend**: Next.js app with an **Admin** surface (`app/admin/*`, e.g. atom-curation) and a
  **Tenant portal** (`app/portal/*`, the T0-T11 stages). Deployed on **Vercel** (check the Vercel
  CI status on the PR for deploy success).
- **AI/Bedrock**: LLM calls (rewrite, research, embeddings, gates) route through the ecosystem
  Bedrock path (acc3 primary → acc1 fallback); the cross-account trust is provisioned in
  AA-CIS-Infra.
- **Live writing progress (AA-637, `docs/adr/0004-*.md`)**:
  - Tenant writing jobs run in the background: the T2 tour rewrite and the T9 post write.
  - While running, each job binds a stream sink (`shared/llm_client/stream_sink.py`).
  - Writer-stage LLM calls stream through that sink into a Redis snapshot
    (`services/acp_shared/writing_progress.py`, key `wp:{tenant}:{kind}:{job}`, 1h TTL).
  - The portal polls `GET /v1/progress/{tour|piece}/{id}` (`LiveWriter.tsx`).
  - This is a view only: persisted rows are unchanged. There is no SSE, and API Gateway is
    unchanged.
- **Schemas** (owned here): `silver_aa_internal` / `gold_aa_internal` (raw→published tours),
  `acp_contract.*` and `acp_shared.*` (the pipeline tables — atoms, segments, ranking, routes,
  hubs, subjects, pieces, facts), migrations under the repo's `migrations/`.

## Deploy & CI

- **Backend (ECS)**: shipped via CI; a new task definition rolls out on the `aa-cis-dev-api`
  service. This repo has **branch protection with 5 required CI jobs** — merge PRs with
  `gh pr merge --auto` (auto-merge once checks pass). Agents must not push to `main` directly.
- **Frontend (Vercel)**: preview per PR, production on merge.
- **Definition of "done" for a backend issue** (program rule): merge + green CI is not enough.
  Also verify (a) Dev deploy **rollout COMPLETED** (ECS running the new taskDef, old deployment
  drained) and (b) the **live endpoint** (curl → HTTP 200 + correct shape). Only then is it Done.

## Boundaries — what this repo does NOT do

- It does not provision AWS resources or OIDC roles (that is AA-CIS-Infra).
- It does not run the B2C map trip planner (that is AA-TripPlanner-Web).
- It does not own `shared.destinations` mutations from the planner side; within CIS it owns its
  own `*_aa_internal` / `acp_*` schemas.

---

# Content Pipeline Domain

The Master Content → Tenant Content pipeline: how one tour becomes the platform's shared master
content (Admin, A-series), and how a tenant turns a picked tour into channel-specific published
content (Tenant, T-series). Built AA-539 (06/09/2026), after AA-527 shipped a page that mixed up
this exact boundary from having no single written glossary. Other subsystems (Excel ingestion,
LLM/model routing, marketplace/billing, legacy N0-N8) are out of scope for this file — split into
their own `CONTEXT.md` under a `CONTEXT-MAP.md` if/when they need one.

## ✅ Segment/Score/Route/Hub are now platform-wide (AA-545, shipped 06/09/2026)

**Update (AA-551, 07/09/2026): this used to be an open tech-debt warning here — it is DONE, not
pending.** AA-542 (same day, commit `43c18c0`) flagged Segment/Score/Route/Hub's then-per-tenant
schema as tech debt requiring a redesign; AA-545 (commit `15743a9`, merged later THE SAME DAY)
shipped that redesign — migration `146_segment_score_route_hub_platform_wide.sql` drops
`tenant_id` from `acp_contract.atom_segment`/`atom_ranking`/`route`/`hub` outright. A reader
landing on the older wording (still present verbatim in this file's git history, and in the
Ownership table row text further down) would wrongly re-block new work behind a freeze that no
longer applies — confirmed directly from `api/routers/admin_dashboard.py`'s own already-updated
SQL/docstrings (no `tenant_id` anywhere in any of the 4 queries) while building AA-551's
platform-wide `/admin/atom-curation` page against this exact data.

The layering principle that motivated the fix, decided by Nghiệp: **any step that does NOT read a
tenant's own brand voice or a tenant-specific DFS/keyword signal should NOT be per-tenant.**
Segment/Score/Route/Hub read neither — they operate purely on Atom (platform-wide, A3) and Search
Demand (platform-wide cache) inputs. They are now computed once for the whole Master Content pool,
still scoped by `tour_id` (a Route is inherently a day-span within ONE tour's itinerary — dropping
`tenant_id` did not make these tour-agnostic, just tenant-agnostic) — see the Ownership table
further down, which still needs a follow-up correction pass of its own (out of AA-551's scope;
flagged, not fixed here).

**Still true and unchanged by AA-545**: Slate (`acp_shared.subject`) is a genuine, deliberate
per-tenant exception — a Subject is one specific tenant's proposal, never shared.

## Language

### Master content (Admin side)

**Atom**:
One concrete, verbatim-derived place-and-activity pair from a tour's itinerary text (`place` +
`action`) — never a summary, paraphrase, or invented detail. Generated once, platform-wide, at
A3 (see Pipeline Stages) — not per tenant.
_Avoid_: fact, moment, detail.

**Facts Entry (platform scope)**:
One hand-written, sourced claim not present in any tour's itinerary text (price, season, visa
requirement, typical transfer time) that AA-admin writes ONCE for every tenant to cite from.
Distinct from Facts Entry (tenant scope) below — same table (`acp_shared.facts`), `scope` column
tells them apart, never merged.
_Avoid_: Fact (bare), reference data.

### Tenant content (per-tenant, built once a tenant picks/rewrites a tour)

**Segment**:
A group of Atoms (usually from different tours) that describe the same real-world moment,
matched deterministically on place-token-similarity + verb-match on action — never an LLM/
embedding call, because `segment_id` must stay stable across re-runs; pure CPU, no external API
cost either way. Built per tenant today, from that tenant's own picked/rewritten tours — NOT a
single platform-wide Segment set. **This is tech debt that needs fixing, not correct design**
(see the warning at the top of this file): Segment-matching never reads a tenant's own brand
voice or DFS signal, so per-tenant here fails the layering principle above — Segment should be
platform-wide, computed once for the whole Master Content pool, the same as Atom and Search
Demand. Per-tenant is a carried-over historical default: Segment was speced/built (AA-509,
01/09/2026) 4 days before Atom became platform-wide (AA-526, 05/09/2026), back when Atom itself
was still per-tenant — the "same tenant" framing was simply the only one that existed yet. A
platform-wide Segment was actually tried during AA-526 and reverted only because
`atom_segment.tenant_id`'s `NOT NULL` FK and `atom_ranking`'s already-shipped tenant-scoped read
would have needed a coordinated redesign across 3 modules, out of that task's scope — see
`docs/adr/0001-*.md` (corrected 06/09/2026, AA-541) for the full trace and
`docs/adr/0003-*.md` (AA-542) for the decision to fix it. **Do not build new Segment-dependent
UI/features against the current per-tenant assumption** — a platform-wide redesign issue is
required first.
_Avoid_: cluster, group, topic.

**Search Demand**:
The cached DataForSEO signal (search volume + People Also Ask) behind one of a tenant's
Segments' places, bought by an LLM research loop. Cached and looked up by `(keyword, market)` —
or `(place, market)` for the loop-skip freshness check — with NO tenant scoping at all: the one
deliberate cross-tenant cache in this pipeline (see Cross-Tenant Mechanisms below).
_Avoid_: DFS, keyword research (both used loosely elsewhere for the same underlying calls).

**Score** (Atom Ranking):
The rank-sum of a Segment's three demand/relevance signals (Search Demand is one of them),
computed once a tenant's Segments exist. Persisted on `acp_contract.atom_ranking`; every
downstream consumer (Route, Slate) reads this value, never recomputes it. Fully per-tenant
today — inherits Search Demand's cross-tenant cache as an input, but the ranking row itself
never is. **Same tech debt as Segment** (see the warning at the top of this file): ranking a
Segment never reads a tenant's own brand voice or DFS signal directly, so it should become
platform-wide once Segment does — not a separate decision, the same one (`docs/adr/0003-*.md`).
_Avoid_: rank, weight, priority.

**Route**:
A consecutive-day span (2-5 days) of one tour's ranked, non-excluded Segments — a Blog-only
concept (the only channel scored at Route grain instead of Segment grain). Rebuilt whole
(delete+insert) on every re-run; never accumulated. Per-tenant today — **same tech debt as
Segment/Score, not a re-validated design** (see the warning at the top of this file and
`docs/adr/0003-*.md`).
_Avoid_: itinerary segment, leg, journey (that's Hub).

**Hub**:
The marketer's unit of choice: a persistent, human-named journey ("Nakasendo Way: The Kiso
Valley from Kyoto") that a family of Routes belongs to. Persists across Route rebuilds — reused
by `route_detection.py` when the same tour-id family regroups, never deleted, even when
orphaned (no Route currently maps to it), because a Subject that already snapshotted the name
still needs it to mean something. Per-tenant today — **same tech debt as Segment/Score/Route**
(see the warning at the top of this file and `docs/adr/0003-*.md`).
_Avoid_: journey, family, group (Route Family, a distinct upstream concept, has no column here).

**Subject**:
One proposable (Segment or Route) × Channel pair, with a `state` (proposed/picked/used/cut), a
`score` (copied from Atom Ranking or Route.score, never recomputed), and `cleared_bar_reason`
(why it passed/failed that Channel's numeric Bar). One row = one thing a tenant can pick.
_Avoid_: proposal, candidate, slot (Slot is the older, deprecated N7 term — do not reuse).

**Slate**:
The tenant-facing mechanism/screen that proposes Bar-cleared Subjects for a Channel. Currently
Bar-check only (deterministic threshold pass/fail per Channel) — has NO Debate/reasoning layer
(no "why this beats that Subject" narrative); a known, deliberate gap, not a bug to silently fix.
_Avoid_: Weekly Slots (the pre-AA-511 name — fully replaced, never use again).

**Goal**:
One of a fixed 8-value list (name/description/logic/marketing_term, e.g. "Promotion" →
AIDA) a tenant picks before writing — verbatim from Nghiệp's own "Bang 1" table, not invented.
_Avoid_: objective, intent, campaign type.

**Angle**:
One of 3 LLM-generated framing options (name/why_it_works/formula_fit/best_final_style) offered
per (Subject, Channel, Goal) — the tenant picks one before Write runs.
_Avoid_: approach, take, framing (used loosely elsewhere; Angle is the specific picked object).

**Gate**:
An automated pass/fail check run inline right after Write (T9/T10) — up to 9 for `channel=
'blog'`, 6 for the other 7 channels. Each Gate is `blocking` (fails the Piece, up to 1 rewrite
before it's held) or non-blocking/`flag`-only (visible, never blocks) — never assume "failed a
gate" means "held," check `blocking` first.
_Avoid_: check, validator, rule (Gate is the specific automated-check object; "rule" is generic).

**Piece**:
One row of generated content for one (Subject, Goal, Angle) — carries `attempt_number` (max 2),
`gate_ledger` (every Gate's result on the persisted attempt), `status`
(`approved`/`held`/`failed`), and (blog only) a `publish_log` row once T11 ships it.
_Avoid_: post, draft, content (all used loosely elsewhere; Piece is the specific DB row).

**Channel**:
One of 8 values: `blog`, `linkedin`, `facebook`, `instagram`, `tiktok`, `email`,
`landing_page`, `ads`. Each has its own Slate Bar and Gate count (blog ≠ the other 7).
_Avoid_: platform, network (Channel is this project's own term for the publishing surface).

**Facts Entry (tenant scope)**:
One hand-written, sourced claim a tenant writes for themselves only (their own pricing,
cancel/rebook terms, deals) — visible only to that tenant, never to others. Same table as the
platform-scope entry above; `tenant_id` is required here, NULL there (enforced by a DB CHECK).
_Avoid_: Fact (bare) — always say which scope.

## Pipeline stages

### A-series (Admin, master content — computed once, shared by every tenant)

- **A0 — Upload**: raw tour ingestion (`raw_tours`, `pipeline_status='ingested'`).
- **A1 — Generic Rewrite (S1)**: the admin S1 pipeline rewrites A0's raw content into
  `generated_content` with a neutral, brand-agnostic voice (see AA-535) — the shared base every
  tenant later re-voices for their own brand.
- **A2 — Admin QA Gate**: A1 rows that failed auto-validation (`status='hitl'`), reviewed via the
  Review Queue before they can reach A3.
- **A3 — Master Content Pool**: `gold_aa_internal.published_tours` — a tour is "in Master
  Content" once it lands here. Atomize (see Atom above) now fires automatically right after a
  tour is published here (AA-526, `services/export/handler.py::process_export()`), as a
  fire-and-forget background task, `owner_scope='platform'`.
- **A4 — Cross-Tenant Oversight**: admin-side supervision OVER tenant-published content — Trust
  Ramp (graduated autonomy per tenant) and the force-unpublish safety net (`admin_a4.py`). Not
  part of the A0→A3 production line; it watches T-series output instead.

### T-series, old (Tenant rewrite tour, T0-T4)

- **T0 — Brand Identity**: tenant sets up their own brand voice/rules (`/portal/t0-brand`).
- **T1 — Tour Selection / Rewrite trigger**: tenant picks an A3 (Master Content) tour and fires
  the rewrite (`/portal/t1-rewrite`, `PoolTab.tsx`'s "Rewrite" button).
- **T2 — Rewrite (execution)**: the actual LLM rewrite of the picked tour into the tenant's own
  brand voice — no dedicated UI route, runs as part of T1's trigger.
- **T3 — QA Gate**: automatic validate→repair loop (max 2 repairs) on T2's output — no manual
  tenant approval step; `status='approved'` is T3's own automatic result, not a button.
- **T4 — Pool**: the tenant's own rewritten-tours pool (`/portal/t4-pool`) — a T3-approved tour
  here is what a tenant can build T7+ content from.

### T-series, new (per-tenant social content, ported from Ms. Thư's `aa-social-media`, T5-T11)

- **T5 — Atomize**: **historical label, superseded.** Originally a tenant-triggered, per-tenant
  atomize step; AA-526 (05/09/2026) moved atom generation to A3 (platform-wide, admin side, see
  above) and removed the tenant-triggered endpoint entirely. Do not build anything new against
  "T5" as a tenant-facing stage.
- **T6 — Atom Curation**: **historical label, superseded.** Was a tenant-facing atom star/delete
  UI (`/portal/t6-atoms`); removed at AA-527, replaced by the admin-only `/admin/atom-curation`
  page (platform-scope atoms only — this is exactly the page AA-527 built into the wrong role
  the first time, which is why this glossary exists).
- **T7 — Planning**: tenant-scoped Segment/Route/Score computation + slot-grid
  (`/portal/t7-planning`) — the first REAL per-tenant stage once a T4 tour has A3 atoms to build
  from.
- **T8 — Angle Gate**: Goal + Angle selection (`/portal/t8-angle-gate`, one continuous wizard
  covering T8 and T9 together — no separate T9 route).
- **T9 — Write**: Piece generation, fires automatically the instant an Angle is chosen.
- **T10 — Quality Gates**: inline within T9 (not a separate request) for the automatic Gate run;
  a tenant-facing review screen for held Pieces exists separately at `/portal/t10-review`
  (AA-501).
- **T11 — Publish**: tenant-facing WordPress publish, blog Channel only today
  (`/portal/t11-publish`); the other 7 Channels have no publish step built yet.

## Ownership / frequency / cross-tenant table (AA-540)

Every step in the A0→A3→T5-T11 chain, answered from real code/schema reads (no inference) —
this is the table that resolves Open Question 1 below.

| # | Step | Owner | Computed | Cross-tenant mechanism |
|---|------|-------|----------|-------------------------|
| 1 | Master Content: Raw Tour→Published Tour (A0-A4) | **Admin** | Once per tour, platform-wide | **Yes — the strongest form: one shared row set.** `silver_aa_internal.raw_tours`/`generated_content`, `gold_aa_internal.published_tours` all carry `tenant_id`, but for Master Content it is always the `aa_internal` sentinel (`_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"`, `api/routers/admin_pipeline.py:71`) — every real tenant reads the exact same rows, not a copy. |
| 2 | Atom (atomize @ A3) | **Admin** | Once per published tour | **Yes.** `owner_scope='platform'` (`services/export/handler.py:44-65` `_run_a3_atomize_background()` → `services/acp_produce/tenant_pipeline.py::run_t5_atomize("platform", ...)`). Every tenant's Segment build reads the SAME `acp_contract.tour_atoms` rows (`services/acp_contract/segment_matching.py:395-412`, `WHERE ta.owner_scope = 'platform' AND ta.tour_id IN (... that tenant's own tenant_tour_versions ...)`). The skip-cache `acp_contract.atomize_day_fingerprint` (migration 128, no `tenant_id` column at all) is keyed on `generated_content.id` for A3 atoms (`services/export/handler.py:246`, `version_id=str(row["id"])`) — also platform-wide now, correctly so (column name is a stale holdover from the pre-AA-526 per-tenant path, where it held a real `tenant_tour_version_id`). |
| 3 | Segment | **Tenant** | Fresh, per tenant, every run | **No — and this is tech debt to fix, not a design conclusion (AA-541/AA-542, see `docs/adr/0003-*.md`).** `acp_contract.atom_segment.tenant_id UUID NOT NULL` (migration 129:49), `segment_id = sha256(tenant_id, canonical_place, canonical_action)` (129:42-46) — tenant_id folded into the hash so two tenants can't collide, but this is a carried-over historical default: Segment was speced/built (AA-509, `5f6a740`, 01/09/2026) 4 days before Atom became platform-wide (AA-526, 05/09/2026), when Atom itself was still per-tenant (`docs/claude_tasks/AA-509-segment-build.md:12-13`, "gom atom... của cùng tenant"). A platform-wide Segment was tried during AA-526 and reverted only because `atom_segment.tenant_id`'s `NOT NULL` FK crashes on `'platform'`, and `atom_ranking.py` (already shipped, AA-515) reads Segments `WHERE tenant_id=$1` for one specific tenant regardless (`docs/implementation-notes/AA-526.md`, "STEP0 correction #3"; commit `5f2b438`) — a scope/redesign-avoidance reason, not an algorithm requirement (Jaccard/verb-match, pure CPU, never touches tenant identity). Two tenants who both pick the same tour still each get a full, independent re-derivation — no cache or dedup between them, and no real dollar cost either way since there's no external API call. |
| 4 | Research/DFS (Search Demand) | **Admin-owned data, Tenant-triggered loop** | Once per `(keyword, market)` / `(place, market)` — NOT per tenant | **Yes — the clearest, most consequential cross-tenant mechanism in the pipeline.** `acp_contract.search_demand` (migration 130:20-27) and `acp_contract.segment_research_log` (migration 130:40-38) have **no `tenant_id` column at all**. `_cached_volume()`/`_store_volume()` (`services/acp_contract/segment_research.py:229-252`) key purely on `(keyword, market)`; `_stale_markets()` (`:440-450`) and `run_segment_research()`'s own skip check (`:476-484`, `if not stale: return`) key on `(canonical_place, market)`. Confirmed real: if tenant A already researched "Kyoto"/`japan` inside `FRESH_FOR` (182 days, `:69`), tenant B's entire LLM ReAct loop for the same place+market is skipped — not just the DataForSEO call, the Bedrock cost too. Deliberate (migration 130:13-19: "a keyword's search volume in a market is a fact about the outside world, not tenant content"), not an oversight. |
| 5 | Score (Atom Ranking) | **Tenant** | Per tenant, once Segments exist | **No row-sharing — same tech debt as Segment (#3), see `docs/adr/0003-*.md`.** `acp_contract.atom_ranking`, `PRIMARY KEY (tenant_id, tour_id, segment_id)` (migration 130:60-76) — inherits Segment's (#3) isolation, so two tenants can never share a ranking row. Its only cross-tenant surface is reading from Search Demand (#4) as an input signal. |
| 6 | Route / Hub | **Tenant** | Route: rebuilt whole per tenant per run. Hub: persists per tenant across rebuilds | **No — same tech debt as Segment/Score, see `docs/adr/0003-*.md`.** `acp_contract.route.tenant_id UUID NOT NULL` (migration 131:46); `route_id` is the deterministic composite `tenant_id:tour_id:first_day-last_day` (migration 131:32-34) — tenant_id baked into the identity, same isolation pattern as Segment. `acp_contract.hub.tenant_id UUID NOT NULL` (migration 131:18), `hub_id` a random UUID PK — no cross-tenant reuse. |
| 7 | Slate (Subject) | **Tenant** | Per tenant | **No.** `acp_shared.subject.tenant_id UUID NOT NULL` (migration 133:19). The similarly-named but unrelated `acp_contract.route_pick` (the Route-pick snapshot, renamed from `acp_contract.subject` at migration 132 — "a different, unrelated concept," migration 133:8) is also `tenant_id UUID NOT NULL` (migration 131:74, pre-rename) — both fully isolated. |
| 8 | Goal / Angle / Write / Gate / Review / Publish | **Tenant** | Per tenant, per request/Piece | **Mostly no, one deliberate exception.** `acp_shared.angle_gate_request.tenant_id` (migration 113:36), `content_piece.tenant_id` (migration 115:36), `publish_log.tenant_id` (migration 116:27) all `NOT NULL`. Exception: the T10 cannibalization Gate (F10) deliberately reads EVERY other tenant's approved Pieces — `find_similar_pieces(cross_tenant=True, ...)`, `_CROSS_TENANT_QUERY` has no `tenant_id` filter at all (`services/acp_shared/piece_similarity.py:46-56, 67-95`), consumed by `gate_cannibalization()` at the 0.92 cosine-similarity threshold. This is a cross-tenant **read/compare**, not a cache — the embedding itself is never shared: `compute_embedding()` (`services/acp_shared/content_embedding.py`) makes one fresh Bedrock call per Piece, every time, for every tenant, with no caching layer at all. |
| 9 | Facts Entry | **Both** (one table, two scopes) | Platform scope: once, by admin. Tenant scope: per tenant | **Yes for `scope='platform'` rows, by design.** `acp_shared.facts` (migration 145:33-46), `scope` column (`'platform'`\|`'tenant'`), `tenant_id IS NULL iff scope='platform'` (CHECK, migration 145:44-46). RLS policy (migration 145:70) `USING (scope = 'platform' OR tenant_id::text = current_setting('app.tenant_id', true))` — platform rows bypass the tenant filter entirely and are readable by every tenant's T9 write; tenant rows stay fully isolated. |

### Cross-tenant mechanisms — full inventory (AA-540's specific ask)

Exactly **3** deliberate cross-tenant mechanisms exist across the whole A0→T11 chain (rows 1/2/4/9
above); everything else (rows 3/5/6/7, and 8 outside its one named exception) is 100% isolated
per tenant, by construction (tenant_id folded into the identity/hash itself wherever a collision
was structurally possible, not just filtered at query time):

1. **The Master Content pool itself** (row 1) and **the Atom pool** (row 2) — not a cache, THE
   shared source every tenant reads from directly.
2. **Search Demand + its research-freshness log** (row 4) — a real shared cache, keyed on
   `(keyword, market)` / `(place, market)`, no `tenant_id` column anywhere in either table.
3. **Facts Entry, `scope='platform'` rows only** (row 9) — shared by explicit design, RLS-enforced.

Plus one **cross-tenant read (not a cache)**: the F10 cannibalization Gate (row 8) compares a new
Piece's embedding against every other tenant's approved content to block near-duplicates —
confirmed no embedding computation is ever cached or shared, only this one comparison query
reaches across the tenant boundary.

**No other table in the T5-T11 schema lacks a `tenant_id` column** besides the three named above
(`search_demand`, `segment_research_log`, `atomize_day_fingerprint` — the last one legitimately
platform-scoped post-AA-526, see row 2) and `angle_gate_option` (a child row of
`angle_gate_request`, scoped indirectly through its parent FK, not independently shared).

## Open questions

1. ~~Segment/Route/Score scope contradicts this issue's own boundary statement.~~ **RESOLVED as
   to current-state fact (AA-540, 06/09/2026)** — see the table above. Confirmed by direct
   code/schema read (not inference): only the Master Content pool, the Atom pool, Search Demand,
   and platform-scope Facts Entry are genuinely cross-tenant. Segment/Score/Route/Hub/Slate/
   Subject/route_pick and Goal→Publish are fully per-tenant, each isolated by a real `tenant_id`
   FK or (where a collision was structurally possible) by folding `tenant_id` into the row's own
   derived identity. `docs/adr/0001-atoms-platform-wide-segments-per-tenant.md` already recorded
   the Atom/Segment half of this; `docs/adr/0002-search-demand-shared-across-tenants.md` (AA-540)
   records the Search Demand half.
   **Superseded — do not read as design endorsement (AA-542, 06/09/2026)**: confirming the
   current per-tenant reality is a fact about the code is NOT the same as confirming it is
   correct. Segment/Score/Route/Hub being per-tenant is documented **tech debt** (see the
   warning at the top of this file), not a design conclusion — a separate design/build issue is
   required to redesign them platform-wide before further work assumes per-tenant is permanent.
   See `docs/adr/0003-segment-score-route-hub-will-become-platform-wide.md` for the decision.
2. **T5/T6 labels are now historical/dead** (see Pipeline Stages above) — any UI epic spec that
   still refers to "T5" or "T6" as a tenant-facing stage should be corrected to "A3 atomize" /
   "admin atom curation" respectively before build starts.

## Related documents

- `docs/investigation/aa-social-media-audit.md` — the field-by-field comparison against Ms.
  Thư's origin repo (Segment/Route/Slate/Subject/Piece) this glossary's Tenant-side definitions
  are grounded in.
- `docs/adr/0001-atoms-platform-wide-segments-per-tenant.md` — the ADR for Open Question 1's
  Atom/Segment half; also records why the Segment/Score/Route/Hub per-tenant default exists.
- `docs/adr/0002-search-demand-shared-across-tenants.md` — the ADR for Open Question 1's Search
  Demand half (AA-540).
- `docs/adr/0003-segment-score-route-hub-will-become-platform-wide.md` — the decision (AA-542)
  that Segment/Score/Route/Hub per-tenant is tech debt to be redesigned platform-wide, and the
  build-freeze on new per-tenant-assuming UI/features until that redesign issue lands.
- `docs/implementation-notes/AA-526.md`, `AA-527.md`, `AA-529.md`, `AA-540.md` — build records
  for the A3 atomize move, the admin atom-curation page, Facts Entry, and this table
  respectively.
