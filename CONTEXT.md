# AA-CIS-App — Repo Context

The core application of the Adventure Asia (AA) ecosystem: the **CIS (Content Intelligence
System)** backend + admin/tenant frontends. It is where a tour becomes shared master content and
where a marketplace tenant turns that content into channel-specific published content. This is the
B2B/admin side of the ecosystem — distinct from AA-TripPlanner-Web (the B2C consumer trip
planner) and AA-CIS-Infra (the Terraform that provisions the AWS resources this app runs on).

> This file has two layers. The **Repo Context** below describes the whole repo — stack, layout,
> deploy, boundaries. Everything from **"Content Pipeline Domain"** onward is the detailed domain
> glossary for the Master Content → Tenant Content pipeline (AA-539/540), kept in sync with the
> code (last full sync: 25/09/2026, covering the work from 15/09 to 25/09).

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
- **Frontend**: Next.js app with an **Admin** surface (`app/admin/*`) and a **Tenant portal**
  (`app/(tenant)/portal/*`). Deployed on **Vercel** (check the Vercel CI status on the PR for
  deploy success; Vercel is not one of the 5 required checks).
  - Admin pages: `dashboard`, `upload` (A0), `s1-rewrite`, `review` (A2 Review Queue — table
    view with a Dismiss action, AA-626), `master-content`, `atom-curation` (Social Content tabs
    01-05), `tenant-activity` (tabs 06-08), `platform-stats` (includes gate telemetry per tenant
    and channel, AA-615), `llm-usage` (the **External Spend** page, AA-622/623), `tenants`,
    `brand`, `settings` (LLM model per stage). `run-health` was removed with N7/N8 (AA-603).
  - Portal pages: `dashboard`, `t0-brand`, `t1-rewrite`, `t4-pool`, `t7-planning` (titled
    "Social Content", renders the Slate), `t8-angle-gate` (T8+T9 wizard), `t10-review` (My
    Content), `t11-publish`, `marketplace`, `billing`, `activity`, `settings`. The portal works
    on phones (drawer below 900px, AA-605).
  - Shared brand tokens (adventure.asia: Fahkwang/Poppins, gold `#DB9628`, pill buttons, logo)
    live in `frontend/app/_brand/tokens.ts` and are used by admin, portal and login pages
    (AA-605). New pages should reuse them.
- **AI/Bedrock**: LLM calls (rewrite, research, embeddings, gates) route through the ecosystem
  Bedrock path (acc3 primary → acc1 fallback); the cross-account trust is provisioned in
  AA-CIS-Infra. Claude cannot be invoked from a local shell — the satellite roles only trust the
  ECS task role, so real LLM measurements run inside the `api` container via ECS exec.
- **Model per stage is DB-driven**: `shared.llm_role_config` (20s cache; `SAFE_DEFAULTS` in
  `shared/llm_client/role_config.py` are only a fallback), editable in admin Settings.
  Current choices, each decided from a real A/B run:
  - `s1_generate` (A1 admin rewrite) = Haiku 4.5; `s1_flag_fix` / `s1_itinerary_nudge` = Haiku.
  - `t2_generate` (T2 tenant rewrite, AA-620) = Sonnet — follows a tenant's brand style guide
    noticeably better, at ~11-13x Haiku's cost per call.
  - `t5_atomize` (A3 atomize, AA-619) = Haiku — same atom count and grounding as Sonnet at ~1/4
    the cost; Sonnet atomize had been ~82% of the acc3 bill.
  - Judge = GPT-4.1 on the OpenAI API (a deliberately different vendor from the writer).
  - Tour writer output ceiling is `GENERATE_MAX_TOKENS=8192` (AA-639); at 4096 long tours were
    truncated and rewritten.
- **Cost observability (epic AA-616)**:
  - `shared.llm_call_log` records account (acc1/acc3), provider and `fallback_used` per call
    (AA-617), with correct Haiku 4.5 pricing and cache tokens (AA-635).
  - `shared.dfs_call_log` records each DataForSEO call, its real `cost` and cache hits (AA-618).
    The DFS cache TTL is 7 days (AA-625).
  - `shared.dfs_balance_snapshot` + a daily Lambda alert on low DFS balance (AA-627).
  - `shared.cost_explorer_snapshot` holds real AWS Cost Explorer figures for acc1/acc2/acc3,
    fetched on demand through `POST /admin/cost-explorer/check` (AA-623). acc2 calls CE
    directly; acc1/acc3 via assume-role (`shared/aws_client/cost_explorer.py`).
  - The admin **External Spend** page compares estimated spend (token logs) with the real AWS
    bill, splitting Bedrock from infrastructure per account, over any date range.
  - Rule for manual scripts that call an LLM: go through `LLMClient.generate()` and log to
    `llm_call_log` with `stage="adhoc_<issue>"` (steering rule, from AA-635).
- **Bedrock Batch for S1 (AA-606)**: `shared/llm_client/bedrock_batch.py` +
  `services/content_generation/s1_batch.py`, endpoints `POST /admin/s1-batch/submit` and
  `GET /admin/s1-batch/{job_id}`. Built and merged, but **never run for real**: AWS has not yet
  enabled Batch Inference on acc3 (support case 178979743800653, blocker AA-624). Batch atomize
  (AA-621) waits on the same blocker.
- **Plans and quota (AA-640)**: the tour-rewrite quota comes only from `shared.membership_plans`
  (starter 50 / growth 200 / business 500). Over-quota rewrites are allowed, logged as
  `rewrite_over_quota` and billed as overage. `PLAN_LIMITS` in code now only holds RPM.
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
  hubs, subjects, pieces, facts), migrations under `api/migrations/` (latest: 167).
- **Removed as dead code (15-21/09)**: the N7/N8 produce/deliver write path and its 5 tables
  (AA-603; `acp_deliver.tenant_tour_pages` is kept), the `/v1/s1-from-atom` route (AA-611), and
  the atom "star" (AA-609; the DB column `tour_atoms.starred` still exists). Do not rebuild on
  any of these.

## Deploy & CI

- **Backend (ECS)**: shipped via CI; a new task definition rolls out on the `aa-cis-dev-api`
  service. This repo has **branch protection with 5 required CI jobs** — merge PRs with
  `gh pr merge --auto` (auto-merge once checks pass). Agents must not push to `main` directly.
- **Frontend (Vercel)**: preview per PR, production on merge.
- **Migrations are not applied by CI/CD.** Deploy only builds the image and updates the ECS
  task definition. Apply each new `api/migrations/NNN_*.sql` by hand through ECS exec into the
  `api` container, using Python + asyncpg with the admin secret `aa-cis/dev/rds` (the container
  has no `psql`; the app-user secret is filtered by RLS and sees 0 platform rows).
- **Verify the frontend with a full `next build`**: `tsc --noEmit` and eslint miss some type
  errors that break the Vercel build. The CI Lint job only runs flake8 on Python
  (`--max-line-length=120`).
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
`tenant_id` did not make these tour-agnostic, just tenant-agnostic). The Ownership table further
down was corrected to match on 25/09/2026.

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

### Platform-derived content (computed once from the Master Content pool, shared by all tenants)

**Segment**:
A group of Atoms (usually from different tours) that describe the same real-world moment,
matched deterministically on place-token-similarity + verb-match on action — never an LLM/
embedding call, because `segment_id` must stay stable across re-runs; pure CPU, no external API
cost either way. **Platform-wide since AA-545 (migration 146)**: `acp_contract.atom_segment` has
no `tenant_id`; new `segment_id`s are `sha256(place|verb)` (rows minted before 146 keep their old
opaque id). A Segment can span many tours and every tenant reads the same set. Carries two
cached columns for Score: `questions_count` (AA-610, migration 163) and `contested`
(AA-631, migration 165) — `NULL` means "recompute next time". History of the per-tenant period:
`docs/adr/0001-*.md`, `docs/adr/0003-*.md`.
_Avoid_: cluster, group, topic.

**Search Demand**:
The cached DataForSEO signal (search volume + People Also Ask) behind a Segment's place, bought
by an LLM research loop. Cached and looked up by `(keyword, market)` — or `(place, market)` for
the loop-skip freshness check — with NO tenant scoping at all. Since AA-631 it also stores the
organic result domains of the same SERP call (`search_demand.serp_domains`, no extra DFS cost).
When PAA or SERP data for a keyword is re-fetched, the `questions_count` / `contested` caches of
every Segment claiming that keyword are reset to `NULL` (AA-630/631) — never recomputed inline.
_Avoid_: DFS, keyword research (both used loosely elsewhere for the same underlying calls).

**Score** (Atom Ranking):
The rank-sum of a Segment's **four** signals (AA-610): Demand (search volume), Recurrence (how
many tours contain it), Questions (PAA questions that land on it by embedding match) and Said
(how much the itinerary already says about it). Deterministic, no LLM. Persisted on
`acp_contract.atom_ranking`, **platform-wide since AA-545**: primary key
`(market, tour_id, segment_id)`, one full pass per buyer market (US/UK/AU/DE/FR/NL). Every
downstream consumer (Route, Slate) reads this value, never recomputes it. A change to one tour
only recomputes the Questions signal for that tour's Segments; the rest is read from the cache
(AA-610 scope fix: cold start ~6 min, warm ~19 s, down from >6 h).
_Avoid_: rank, weight, priority.

**Route**:
A consecutive-day span (2-5 days) of one tour's ranked, non-excluded Segments — a Blog-only
concept (the only channel scored at Route grain instead of Segment grain). Rebuilt whole on
every re-run (old rows are superseded, never deleted). Platform-wide since AA-545: identity is
`(tour_id, first_day, last_day)`, no `tenant_id`.
_Avoid_: itinerary segment, leg, journey (that's Hub).

**Hub**:
The marketer's unit of choice: a persistent, human-named journey ("Nakasendo Way: The Kiso
Valley from Kyoto") that a family of Routes belongs to. Persists across Route rebuilds — reused
by `route_detection.py` when the same tour-id family regroups, never deleted, even when
orphaned (no Route currently maps to it), because a Subject that already snapshotted the name
still needs it to mean something. Platform-wide since AA-545.
_Avoid_: journey, family, group (Route Family, a distinct upstream concept, has no column here).

### Tenant content (per tenant, from the Slate onward)

**Subject**:
One proposable (Segment or Route) × Channel pair, with a `state` (proposed/picked/used/cut), a
`score` (copied from Atom Ranking or Route.score, never recomputed), and `cleared_bar_reason`
(why it passed/failed that Channel's numeric Bar). One row = one thing a tenant can pick.
_Avoid_: proposal, candidate, slot (Slot is the older, deprecated N7 term — do not reuse).

**Slate**:
The tenant-facing mechanism/screen (portal "Social Content", `GET /v1/slate`) that proposes
Bar-cleared Subjects for a Channel. `services/acp_shared/slate.py::propose_slate()` runs, in order:

1. Fetch candidates (Segments/Routes) for the tenant's markets. A declared market outside the
   6 supported ones is no longer silently replaced by US: it is returned as
   `unmatched_markets`, shown as a banner to the tenant, and recorded in
   `shared.unmapped_market_requests` for admin (AA-629).
2. **Debate** (AA-631, `services/acp_shared/debate.py`) — two advisory cuts before choosing:
   - _contested_: share of a keyword's SERP taken by "everywhere" domains (wikipedia,
     tripadvisor…). Deterministic and cached; cut threshold 0.5, measured on real SERP data.
   - _brand-fit_: the same GPT-4.1 brand-fit scorer the T2 judge uses
     (`services/content_generation/brand_fit.py`), cached per
     `(tenant, candidate, brand_version)` in `acp_shared.debate_brand_fit_cache`.
   - Safety rules: never cuts more than 50% of a list; an error on a candidate lets it pass; if
     Debate fails entirely the Slate behaves as before.
3. Bar check per Channel, with one shared `seen_places` set so the same place is not proposed
   on two Channels in one Slate (AA-632).
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
An automated check run inline right after Write (T9/T10) — up to 9 for `channel='blog'`, 6 for
the other 7 channels. Since AA-613 each failure has a **severity**: `block` (product truth —
facts, grounding, CTA), `warn` (brand/style, e.g. generic AI wording, brand-fit) or `note`. Only
`block` triggers a rewrite (max 2 retries). Never assume "failed a gate" means "held" — check
the severity first. `services/acp_shared/grounding.py` is shared by T3 and T9 and reads numbers with units ("4,460m") correctly (AA-639).
_Avoid_: check, validator, rule (Gate is the specific automated-check object; "rule" is generic).

**Piece**:
One row of generated content for one (Subject, Goal, Angle) — carries `attempt_number` (max 2),
`gate_ledger` (every Gate's result on the persisted attempt), `status`
(`approved`/`held`/`failed`), and (blog only) a `publish_log` row once T11 ships it.
`held` now only means "a product-truth problem is still unresolved after ≤2 retries"; the
tenant always gets the best version. **Tenants never see internal gate data**: the tenant-safe
projection (`_tenant_safe_piece`) drops `status`, `held_reason`, `gate_ledger`, `repair_log` and
exposes only `ready_state` (`ready` / `not_ready`, where `not_ready` means there is no content
yet). Publishing a `held` Piece returns 422. Admins see the full gate data in Content Trace and
Platform Stats.
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
- **A0 note**: tours with an empty itinerary are skipped at upload and flagged "No Itinerary"
  (AA-604). As of the 16/09 data reset: 793 raw tours kept, 30 empty-itinerary rows trashed,
  763 ready for S1.
- **A1 — Generic Rewrite (S1)**: the admin S1 pipeline rewrites A0's raw content into
  `generated_content` with a neutral, brand-agnostic voice (see AA-535) — the shared base every
  tenant later re-voices for their own brand. Flow: generate → validate → judge →
  (retry ≤3 or HITL) → brand_audit → flag_fix → revalidate. Can run per tour or, once AWS
  enables it, as a Bedrock Batch job (see Repo Context).
- **A2 — Admin QA Gate**: A1 rows that failed auto-validation (`status='hitl'`), reviewed via the
  Review Queue before they can reach A3. A row can also be **dismissed** (hidden without
  approving; a future real failure is enqueued again — AA-626, `review_status_enum` value
  `dismissed`).
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
  brand voice — no dedicated UI route, runs as part of T1's trigger. Writer stage
  `t2_generate` (Sonnet); the judge now scores brand-fit for tenants with a real brand profile
  (AA-612a fixed the brand fields that were not being loaded). The tenant watches the rewrite
  live (streamed text, AA-637). Counts against the plan quota; over-quota is billed (AA-640).
- **T3 — QA Gate**: automatic validate→repair loop (max 2 repairs) on T2's output — no manual
  tenant approval step; `status='approved'` is T3's own automatic result, not a button. Since
  AA-639 T3 only blocks on `_HARD_BLOCK_CODES`, passes the specific failure feedback into the
  repair, and fixes an over-long SEO title deterministically (`fit_seo_title()`) instead of
  rewriting the tour. Typical long tour: 1-2 writer calls, ~$0.10-0.18 (was 4 calls, ~$0.31).
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
- **T7 — Slate** (`/portal/t7-planning`, titled "Social Content"): the tenant's ranked,
  per-Channel list of Subjects to pick from, read from the platform-wide Score/Route data (see
  Slate above). The older slot-grid UI on this page was removed (AA-519).
- **T8 — Angle Gate**: Goal + Angle selection (`/portal/t8-angle-gate`, one continuous wizard
  covering T8 and T9 together — no separate T9 route).
- **T9 — Write**: Piece generation, fires automatically the instant an Angle is chosen; the
  text streams live to the tenant (AA-637).
- **T10 — Quality Gates**: inline within T9 (not a separate request) for the automatic Gate run.
  `/portal/t10-review` is now **My Content** (AA-614): a flat list of the tenant's pieces with
  no gate/held details, displayed text is copy-locked (select/copy/right-click blocked, with a
  "Use Export" hint; the edit textarea stays editable).
- **T11 — Publish / Export**: tenant-facing WordPress publish, blog Channel only today
  (`/portal/t11-publish`); the other 7 Channels have no publish step built yet. Blog markdown is
  converted to an HTML fragment before it is sent to WordPress (AA-613 fixed raw `##`/`**`
  appearing on the site). Export has two modes: a full HTML document and a bare HTML fragment
  for pasting into a CMS. Every export and publish writes an audit row.

## Ownership / frequency / cross-tenant table (AA-540)

Every step in the A0→A3→T5-T11 chain, answered from real code/schema reads (no inference) —
this is the table that resolves Open Question 1 below.

| # | Step | Owner | Computed | Cross-tenant mechanism |
|---|------|-------|----------|-------------------------|
| 1 | Master Content: Raw Tour→Published Tour (A0-A4) | **Admin** | Once per tour, platform-wide | **Yes — the strongest form: one shared row set.** `silver_aa_internal.raw_tours`/`generated_content`, `gold_aa_internal.published_tours` all carry `tenant_id`, but for Master Content it is always the `aa_internal` sentinel (`_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"`, `api/routers/admin_pipeline.py:71`) — every real tenant reads the exact same rows, not a copy. |
| 2 | Atom (atomize @ A3) | **Admin** | Once per published tour | **Yes.** `owner_scope='platform'` (`services/export/handler.py:44-65` `_run_a3_atomize_background()` → `services/acp_produce/tenant_pipeline.py::run_t5_atomize("platform", ...)`). Every tenant's Segment build reads the SAME `acp_contract.tour_atoms` rows (`services/acp_contract/segment_matching.py:395-412`, `WHERE ta.owner_scope = 'platform' AND ta.tour_id IN (... that tenant's own tenant_tour_versions ...)`). The skip-cache `acp_contract.atomize_day_fingerprint` (migration 128, no `tenant_id` column at all) is keyed on `generated_content.id` for A3 atoms (`services/export/handler.py:246`, `version_id=str(row["id"])`) — also platform-wide now, correctly so (column name is a stale holdover from the pre-AA-526 per-tenant path, where it held a real `tenant_tour_version_id`). |
| 3 | Segment | **Admin (platform)** | Once for the whole Master Content pool, re-matched when a tour's atoms change | **Yes — shared rows (AA-545, migration 146).** `acp_contract.atom_segment` has no `tenant_id`; new `segment_id = sha256(place\|verb)`. Every tenant's Slate reads the same Segment set. Was per-tenant from AA-509 (01/09) to AA-545 (06/09) — history in `docs/adr/0001-*.md` / `0003-*.md`. Cached `questions_count` (163) and `contested` (165) columns are platform-wide too. |
| 4 | Research/DFS (Search Demand) | **Admin-owned data, Tenant-triggered loop** | Once per `(keyword, market)` / `(place, market)` — NOT per tenant | **Yes — the clearest, most consequential cross-tenant mechanism in the pipeline.** `acp_contract.search_demand` (migration 130:20-27) and `acp_contract.segment_research_log` (migration 130:40-38) have **no `tenant_id` column at all**. `_cached_volume()`/`_store_volume()` (`services/acp_contract/segment_research.py:229-252`) key purely on `(keyword, market)`; `_stale_markets()` (`:440-450`) and `run_segment_research()`'s own skip check (`:476-484`, `if not stale: return`) key on `(canonical_place, market)`. Confirmed real: if tenant A already researched "Kyoto"/`japan` inside `FRESH_FOR` (182 days, `:69`), tenant B's entire LLM ReAct loop for the same place+market is skipped — not just the DataForSEO call, the Bedrock cost too. Deliberate (migration 130:13-19: "a keyword's search volume in a market is a fact about the outside world, not tenant content"), not an oversight. |
| 5 | Score (Atom Ranking) | **Admin (platform)** | One pass per buyer market (6 markets) over the whole Segment pool | **Yes — shared rows (AA-545).** `acp_contract.atom_ranking` primary key `(market, tour_id, segment_id)`, no `tenant_id`. A tenant with several markets merges the matching rows at read time in the Slate. 4-axis rank-sum since AA-610. |
| 6 | Route / Hub | **Admin (platform)** | Route: rebuilt whole per tour (old rows superseded). Hub: persists across rebuilds | **Yes — shared rows (AA-545).** `acp_contract.route` identity `(tour_id, first_day, last_day)` and `acp_contract.hub`, both without `tenant_id`. |
| 7 | Slate (Subject) | **Tenant** | Per tenant | **No.** `acp_shared.subject.tenant_id UUID NOT NULL` (migration 133:19). The similarly-named but unrelated `acp_contract.route_pick` (the Route-pick snapshot, renamed from `acp_contract.subject` at migration 132 — "a different, unrelated concept," migration 133:8) is also `tenant_id UUID NOT NULL` (migration 131:74, pre-rename) — both fully isolated. |
| 8 | Goal / Angle / Write / Gate / Review / Publish | **Tenant** | Per tenant, per request/Piece | **Mostly no, one deliberate exception.** `acp_shared.angle_gate_request.tenant_id` (migration 113:36), `content_piece.tenant_id` (migration 115:36), `publish_log.tenant_id` (migration 116:27) all `NOT NULL`. Exception: the T10 cannibalization Gate (F10) deliberately reads EVERY other tenant's approved Pieces — `find_similar_pieces(cross_tenant=True, ...)`, `_CROSS_TENANT_QUERY` has no `tenant_id` filter at all (`services/acp_shared/piece_similarity.py:46-56, 67-95`), consumed by `gate_cannibalization()` at the 0.92 cosine-similarity threshold. This is a cross-tenant **read/compare**, not a cache — the embedding itself is never shared: `compute_embedding()` (`services/acp_shared/content_embedding.py`) makes one fresh Bedrock call per Piece, every time, for every tenant, with no caching layer at all. |
| 9 | Facts Entry | **Both** (one table, two scopes) | Platform scope: once, by admin. Tenant scope: per tenant | **Yes for `scope='platform'` rows, by design.** `acp_shared.facts` (migration 145:33-46), `scope` column (`'platform'`\|`'tenant'`), `tenant_id IS NULL iff scope='platform'` (CHECK, migration 145:44-46). RLS policy (migration 145:70) `USING (scope = 'platform' OR tenant_id::text = current_setting('app.tenant_id', true))` — platform rows bypass the tenant filter entirely and are readable by every tenant's T9 write; tenant rows stay fully isolated. |

### Cross-tenant mechanisms — full inventory (AA-540's specific ask)

Since AA-545 the dividing line is simple: **everything up to Route/Hub is shared platform data;
everything from the Slate onward is per tenant.** The shared parts:

1. **The Master Content pool** (row 1) and **the Atom pool** (row 2) — the shared source every
   tenant reads from directly.
2. **Segment, Score, Route, Hub** (rows 3/5/6) — platform-wide rows, no `tenant_id`.
3. **Search Demand + its research-freshness log** (row 4) — a shared cache keyed on
   `(keyword, market)` / `(place, market)`, now also holding SERP domains (AA-631).
4. **Facts Entry, `scope='platform'` rows only** (row 9) — shared by explicit design, RLS-enforced.

Plus one **cross-tenant read (not a cache)**: the F10 cannibalization Gate (row 8) compares a new
Piece's embedding against every other tenant's approved content to block near-duplicates.

Per-tenant tables added 15-25/09 that sit next to the shared data:
`acp_shared.debate_brand_fit_cache` (keyed by tenant, AA-631) and
`shared.unmapped_market_requests` (AA-629).

## Open questions

1. ~~Segment/Route/Score scope contradicts this issue's own boundary statement.~~ **Closed by
   AA-545 (migration 146, 06/09/2026)**: Segment/Score/Route/Hub are platform-wide. The
   per-tenant period and the reasoning are kept in `docs/adr/0001-*.md` (Atom/Segment),
   `0002-*.md` (Search Demand shared) and `0003-*.md` (decision to go platform-wide).
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
  that Segment/Score/Route/Hub per-tenant was tech debt; carried out by AA-545.
- `docs/adr/0004-live-writing-progress-via-redis-poll.md` — live writing progress (AA-637).
- `docs/implementation-notes/AA-526.md`, `AA-527.md`, `AA-529.md`, `AA-540.md` — build records
  for the A3 atomize move, the admin atom-curation page, Facts Entry, and this table
  respectively.
