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
  Owner per schema and per `shared.*` table (CIS also owns `shared.destinations` since ADR 0002):
  `AA-Ecosys/docs/database/schema-ownership.md`. A migration here is applied to Dev from its
  identical copy in `AA-CIS-Infra/db/migrations/cis/` by the **DB Migrate** workflow, before the
  code that needs it merges.

## Architecture at a glance

- **Backend**: Python (FastAPI-style routers under `api/routers/`, services under `services/`),
  running on **ECS** (cluster `aa-cis-dev-cluster`, service `aa-cis-dev-api`) fronted by ALB +
  API Gateway. Postgres (RDS) holds the schemas; some steps use Bedrock for LLM work.
- **Frontend**: two Next.js apps (AA-885, S243), each its own Vercel project, both deployed by CI
  (not one of the 5 required checks — see Deploy & CI):
  - `frontend/` = the **Admin** console (`app/admin/*`), Vercel `aa-cis-frontend`,
    `https://aa-cis.lumiguides.it.com`, workflow `Vercel Deploy` (job "Vercel deploy").
  - `portal/` = the **Tenant portal** (`portal/app/portal/*`), Vercel `aa-cis-portal` (Root Directory
    `portal`), `https://portal.lumiguides.it.com`, workflow `Vercel Deploy (portal)`. See "Tenant portal
    app" below.
  - Admin pages: `dashboard`, `upload` (A0), `s1-rewrite`, `review` (A2 Review Queue — table
    view with a Dismiss action, AA-626; migrated to the UI kit + react-query with multi-select
    bulk regenerate and non-blocking regenerate, AA-662 PR 2 / AA-719), `master-content` (AA-718:
    server-side filter/sort/paginate + filter-aware stat cards + windowed cost + derived pipeline
    status, via react-query — see A3 note below), `atom-curation` (Social Content tabs
    01-05), `tenant-activity` (tabs 06-08), `platform-stats` (includes gate telemetry per tenant
    and channel, AA-615), `llm-usage` (the **External Spend** page, AA-622/623), `tenants`,
    `jobs`, `decisions` (**Jev Decisions**, AA-660), `photos` (**Photos**, AA-708 — Drive photo sync, unmatched queue), `api-docs` (**API Docs**,
    admin-only read-only OpenAPI reader built on the kit: `GET /admin/openapi.json` through the BFF, since the
    AA-577 `/docs`, `/redoc`, `/openapi.json` need the X-Admin-Secret header on ECS), `settings` (pipeline gates, **Brand
    Identity** editor, SEO config, LLM model per stage + read-only Jev questions per stage). `run-health` was removed with N7/N8 (AA-603).
  - Admin navigation (AA-663): **Overview** (Dashboard) · **Content** (Upload, Rewrite S1,
    Review Queue, Master Content) · **Intelligence** (Social Content) · **Tenants** ·
    **Operations** (External Spend, Jobs, Jev Decisions, API Docs) · **Settings**. Admin-only items are hidden for other
    roles (same list as `lib/rbac.ts` PAGE_RULES, enforced by `proxy.ts`). The legacy `(internal)` route group
    (`/upload`, `/review`, `/catalog`, `/brand`) is deleted; those URLs and `/admin/brand`
    redirect in `next.config.ts`.
  - Portal pages (`portal/app/portal/*`): `dashboard`, `t0-brand`, `t1-rewrite`, `t4-pool` ("My Catalog Tours" /
    `CatalogTab`, migrated to the UI kit + react-query, AA-662 PR 3), `t7-planning` (titled
    "Social Content", renders the Slate), `t8-angle-gate` (T8+T9 wizard), `t10-review` (My
    Content), `t11-publish`, `marketplace`, `billing`, `activity`, `settings`, `api`, `help/facebook`.
    The portal works on phones (drawer below 900px, AA-605).
  - **Tenant portal app (AA-885)**: `portal/` is a second Next.js app (same Next/React versions, its own
    `package-lock.json`, dev port 3004). Paths keep the `/portal/*` prefix on the new host (`/` → `/portal/dashboard`);
    the admin host answers `/portal`, `/portal/*` and `/tenant-login` with **308** to the same path on
    `https://portal.lumiguides.it.com` (`frontend/next.config.ts`, `PORTAL_BASE_URL` in `frontend/lib/apps.ts`)
    and serves no portal page or tenant BFF any more.
    - Its own tenant-only BFF: `app/api/auth/tenant-login` + `tenant-logout` (the 5 httpOnly cookies
      `cis_role`, `cis_tenant_token`, `cis_tenant_id`, `cis_tenant_name`, `cis_tenant_plan`, **host-only** on the
      portal host — no `Domain`), `app/api/tenant/me`, `app/api/tenant/[...path]` (verified tenant Bearer only),
      `app/api/playground/proxy`. **No admin secret**: the portal project has no `ADMIN_SECRET`,
      `INTERNAL_API_KEY` or `COGNITO_*` (a test checks the source). `API_URL` defaults to the Dev API, so the
      Vercel project needs no env var. CORS is unchanged: the browser only calls its own origin, the BFF
      calls the API server to server.
    - Page gate `portal/proxy.ts` (Next 16 Proxy): `/tenant-login` public; `/portal/*` needs a token
      `/auth/verify-tenant` accepts (cached ≤ 20 s, `portal/lib/auth-server.ts`); refused → cookies cleared,
      back to `/tenant-login`; backend unreachable → fail closed outside development.
    - Security headers on every response (`portal/next.config.ts`): HSTS, `nosniff`, `Referrer-Policy`,
      `X-Frame-Options: DENY`, a minimal CSP (`frame-ancestors 'none'; base-uri 'self'; form-action 'self';
      object-src 'none'`), `Permissions-Policy`. `/privacy` → `https://adventure.asia/privacy-policy`
      (307); `/terms` is a holding page (no terms document exists yet, AA-820). Links in the sidebar and
      on `/tenant-login`.
    - Staff have **no** access to the tenant portal (no staff session there; a support view is future
      work). Staff sign in on the admin host (the tenant-login "← Staff login" link).
    - **Vendored UI kit** (byte copies, guarded by `tests/unit/test_aa885_portal_split.py`): a change to
      any of these admin files must be copied into `portal/` in the same PR, or the unit tests go red —
      `portal/app/_kit/tokens.ts`, `portal/app/_kit/primitives.tsx`, `portal/app/_kit/Modal.tsx`,
      `portal/app/_kit/Drawer.tsx`, `portal/app/_kit/Toast.tsx`, `portal/app/_kit/api.ts`,
      `portal/app/_kit/useMediaQuery.ts`, `portal/app/_kit/Providers.tsx`, `portal/app/_kit/DataTable.tsx`,
      `portal/app/_kit/csv.ts`, `portal/app/_kit/savedViews.ts`, `portal/app/_brand/tokens.ts`,
      `portal/lib/bffPath.ts`, `portal/lib/proxyResponse.ts` (each copied from the same path under
      `frontend/`). `portal/app/_kit/index.ts` (barrel of just these), `portal/app/globals.css` and
      `portal/lib/auth-server.ts` are portal-owned.
    - Not moved yet: the public white-label page `/trip/[tourId]` stays on the admin app (stored `trip_url`s
      point at the admin host); no Sentry on the portal yet.
  - Shared brand tokens (adventure.asia: Fahkwang/Poppins, gold `#DB9628`, pill buttons, logo)
    live in `frontend/app/_brand/tokens.ts` and are used by admin, portal and login pages
    (AA-605; the portal has a guarded byte copy). New pages should reuse them.
  - **UI kit (AA-662, `frontend/app/_kit/`)**: the shared component library for UI v2, built on
    TanStack Table (`@tanstack/react-table`) and brand tokens (inline styles, no Tailwind
    classNames). Exports `DataTable` (per-column filter, multi-sort, column show/hide, pagination,
    sticky header, row selection + bulk actions, saved views in localStorage per user, CSV export),
    `Drawer`, `Modal`/`ConfirmModal`, `PageHeader`, `Tabs`, `Badge`/`StatusBadge` (one `tone`
    vocabulary; `StatusBadge` is driven by job-runner status strings), `EmptyState`, `ErrorState`,
    `Skeleton`, `ToastProvider`/`useToast`, `ScrollX` (S243), `InfoTip` (S243: (i) button + tooltip, hover / focus /
    tap to open, Escape or a tap elsewhere to close, text tied by `aria-describedby`, hoverable bubble, 24 px hit
    area), and `apiGet`/`apiSend` fetch
    helpers. New pages import
    from `app/_kit`. The legacy primitives (`admin/_components/adminUi.tsx`,
    `auditPanels.tsx`, `FilterBar.tsx`, `Pagination.tsx`, portal `_components/ui.tsx`,
    `Skeleton.tsx`) still stand — pages are migrated to the kit one at a time (Review Queue and My
    Content first). Living reference: the **UI Kit tab in admin Settings** (`/admin/settings?tab=ui-kit`,
    `app/admin/_components/SettingsKitTab.tsx`) — renders every primitive with static data.
  - **Data fetching (AA-662)**: `@tanstack/react-query` is the standard for UI v2, wired app-wide
    via a single `QueryClientProvider` in `app/_kit/Providers.tsx`, mounted from the server root
    layout (`app/layout.tsx`) around all routes. Defaults: no refetch-on-focus, one retry, 30s
    stale. Pages that poll opt in per-query with `refetchInterval` (replaces the old hand-rolled
    `useState`/`useEffect`/`setInterval` fetch). Older pages keep their hand-rolled fetch until
    migrated.
- **AI/Bedrock**: LLM calls (rewrite, research, gates) route through the ecosystem Bedrock path
  (acc3 primary → acc1 fallback); the cross-account trust is provisioned in AA-CIS-Infra.
  Embeddings (Cohere Embed v4) run natively on acc2, through the GLOBAL cross-region profile
  `global.cohere.embed-v4:0` since migration 238 (AA-856: the `us.` quota is fixed at 20 rpm, the global
  one is adjustable; one quota shared by f10_embed, a3_place_embed and TripPlanner search). Claude cannot be invoked from a local shell — the satellite roles only trust the
  ECS task role, so real LLM measurements run inside the `api` container via ECS exec.
- **Model per stage is DB-driven**: `shared.llm_role_config` (20s cache; `SAFE_DEFAULTS` in
  `shared/llm_client/role_config.py` are only a fallback), editable in admin Settings.
- **Model Catalog** (`shared.llm_model_catalog`, AA-658, ADR 0005): the admin dropdown options and
  LLM prices come from it (`pricing.py` is the fallback). Non-legacy Model Keys (e.g. `sonnet-5`)
  are called with the Bedrock Converse API on the acc3 satellite.
- **Stage Route** (AA-659, ADR 0006): each stage has a model plus an ordered fallback list and an
  optional shadow model (A/B, `shared.llm_shadow_log`). Only models the route lists are ever
  tried. No judge has its own hardcoded model path any more. **S235 rule (Nghiệp, 14/10/2026,
  migration 224): Bedrock first; an OpenAI platform API model may only be the LAST fallback of a
  route (a Bedrock outage), never a primary or a shadow; GPT-4.1 is retired.** Judge routes on Dev:
  - `s1_judge`, `t2_judge`, `t7_debate_judge`, `t10_judge`: GPT-5.6 Luna (Bedrock acc3) → GPT-6 Luna
    (Bedrock acc3) → GPT-6 Luna (OpenAI API, last resort); shadow GPT-6 Luna at the admin-set rate.
  - `s1_brand_audit`, `t2_brand_audit`: GPT-5.6 Luna → GPT-6 Luna (no OpenAI last resort).
  - No acc1 fallback for the Luna models (OpenAI-on-Bedrock is enabled on acc3 only); the
    acc3 → acc1 satellite chain (the stage's `account_route` picks which satellite goes first)
    applies to the legacy Haiku/Sonnet keys. They no longer try acc2-native Bedrock first (AA-843:
    acc2 is a channel-program account and always refuses Anthropic models); the env var
    `LLM_LEGACY_ACC2_NATIVE=on` restores that first attempt if acc2 ever gains model access.
  - A call served by the OpenAI API logs `llm_openai_fallback_used` and is recorded in `llm_call_log`
    with `provider = 'openai'`, `fallback_used = true`.
  - **Brand audit fails closed** (AA-714): a model/schema error gives `manual_check` +
    `BRAND_AUDIT_UNAVAILABLE` (held for review), never `pass`.
  - **Where a GPT model runs**: on Bedrock only on acc3 (acc1/acc2 have no OpenAI models), or on the
    OpenAI platform API (`gpt-6-luna-openai`, last resort only). `gpt-4.1` is disabled (migration 224)
    and admin cannot re-enable it; `LLMClient._call_openai` refuses any model whose catalog row is not
    enabled, so the legacy Haiku/Sonnet chain no longer falls back to GPT-4.1. Admin rejects an
    OpenAI-API model as primary, shadow or non-last fallback. Settings › LLM Models shows each route.
- **Every model call goes through the gateway** (`shared/llm_client/`, AA-685): text through
  `LLMClient.generate(stage=...)`, embeddings through `shared.llm_client.embed.embed(stage, texts)`
  (catalog `api_style='embed'`, stage role `embed`). The caller writes the `llm_call_log` row.
  `tests/unit/test_aa685_no_raw_model_calls.py` fails on any raw Bedrock/OpenAI call outside
  `shared/llm_client/`; the only allowed exception is the legacy judge path in `judge_client.py`
  used by comparison scripts. Stages added: `a0_column_map`, `f10_embed`, and `tp_compose` /
  `tp_search_embed` for AA-TripPlanner-Web, which reads the same tables (migration 172).
  Current choices, each decided from a real A/B run:
  - `s1_generate` (A1 admin rewrite) = Haiku 4.5; `s1_flag_fix` / `s1_itinerary_nudge` = Haiku.
  - `t2_generate` (T2 tenant rewrite, AA-620) = Sonnet — follows a tenant's brand style guide
    noticeably better, at ~11-13x Haiku's cost per call.
  - `t5_atomize` (A3 atomize — the stage key is a pre-AA-526 name) = **GPT-6 Luna as a `validate`
    stage, Haiku fallback** (AA-757, migration 213, ADR 0008): S224 offline A/B on 477 days, no
    invented detail, ~6x cheaper than Haiku. History: Sonnet → Haiku at AA-619 (same quality, ~1/4
    the cost). Atomize goes through the gateway since AA-757 (#642); before that it called
    `invoke_claude` directly.
  - Judges = OpenAI models (a deliberately different vendor from the Anthropic writer): GPT-5.6 Luna →
    GPT-6 Luna on Bedrock acc3, GPT-6 Luna on the OpenAI API only as the last resort (see Stage Route).
  - Tour writer output ceiling is `GENERATE_MAX_TOKENS=8192` (AA-639); at 4096 long tours were
    truncated and rewritten.
- **Jev decision layer (AA-660, design `docs/architecture/at-series-v2-design.md`)**: stages ask
  Jev (TypeSafe System One) typed questions through `shared/llm_client/decide.py` —
  `decide(stage, subject_key, state, question_keys, tenant_id=)`, plain HTTP, never raises.
  - Questions are rows in `shared.decision_question` (wording, `off`/`shadow`/`enforce`,
    `accept_floor`/`reject_ceiling`, `calibration_ref`). A DB CHECK refuses `enforce` without a
    calibration record.
  - Each verdict lands in a zone (accept / grey / reject / error / skipped). Only an enforce-mode,
    confident verdict changes what a stage does; grey and error keep the stage's existing rule
    (fail-open).
  - Tenant content goes to TypeSafe only for tenants in `shared.jev_tenant_allowlist` (the AA test
    tenants) until the DPA/ZDR is confirmed.
  - Every verdict is logged in `shared.decision_log`, and each call in `llm_call_log` (provider
    `typesafe`, role `validate`), so Jev shows in External Spend. Admin page: `/admin/decisions`.
    Key: secret `aa-cis/dev/typesafe`. Migration 181.
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
- **Cost guard (AA-649, migration 168)**: `shared/cost_guard.py` checks `shared.spend_budget`
  (per provider: `global` + `job:<kind>` rows; per-run and per-day USD, `hard_stop`, `alert_pct`)
  before every paid external call of a guarded job (today `segment_research`: DataForSEO + Bedrock).
  Each cap stops or only alerts according to the row that sets it: the DFS daily cap (`global`) is
  **alert-only** since 29/09/2026 (KAN-90: "control, not limit"), the per-run cap
  (`job:segment_research`, $5 DFS / $2 Bedrock) still stops a run. Edited via
  `PUT /admin/budgets/{provider}/{scope}` (no settings screen yet).
  Alerts: `maybe_alert` writes a `spend_budget_alert` bell row (one unread per provider per 24 h)
  and, since AA-828, queues one `ops_alert_email` job that the worker sends through SES
  (`shared/ops_alerts.py`, `shared/aws_client/ses_mailer.py`; env `ALERT_EMAIL_TO`,
  `SES_FROM_ADDRESS`, `SES_CONFIGURATION_SET` from AA-CIS-Infra). At most one e-mail per budget,
  threshold and UTC day (job idempotency key); unset env = no e-mail, one log line.
- **Durable job runner (AA-650, ADR 0001 decision 3)**: long work is a row in `shared.job`
  (migration 174), not an in-process asyncio task, so a deploy/restart re-queues it instead of
  losing it.
  - `shared/jobs/`: `queue.py` (enqueue with idempotency key, claim `FOR UPDATE SKIP LOCKED` under
    an advisory lock, lease + heartbeat, retry with backoff, reaper, cancel/retry), `registry.py`
    (`@job_kind(name, concurrency, max_attempts)`, `JobContext` for progress/cost), `worker.py`.
  - **Table shape (AA-751 / X6, ADR 0003a decision 2, migration 242)**: `shared/jobs/job_table_template.sql`
    is the reference DDL of every owner's `<schema>.job` (CIS `shared.job`, Booking `booking.job` keeps a
    byte copy, a future `tripplanner.job`); no runner applies it, each owner's migration instantiates it.
    `shared/jobs/job_table_shape.py` renders it and compares a live table through `pg_catalog` (columns,
    CHECKs, FK, indexes by definition, trigger, reloptions; names ignored), run by
    `tests/integration/test_aa751_job_table_template.py`. Since 242 `shared.job` enforces
    `job_kind_chk` (kind `^(?!bk_|tp_)[a-z][a-z0-9_]*$`: lower-case snake_case, never another owner's
    prefix), `job_lease_chk` (a `running` row has `locked_by` and `locked_until`; test helpers that insert
    running rows must set a lease) and `job_attempts_chk` (`attempt >= 0`, `max_attempts >= 1`); the
    BEFORE UPDATE trigger `job_set_updated_at` sets `updated_at` on every UPDATE (writers need not); queue
    autovacuum `scale_factor 0.02`, `threshold 50` (C5.1b as amended by AA-751). Changing the template =
    copy it to AA-Booking in the same session + one migration per owner.
  - Job kinds live in `services/jobs/` (`segment_research`, `t2_rewrite`, `t9_write`, `a3_atomize`,
    `s1_seo_prefetch`, `photo_sync`, `s1_rewrite`, `s1_batch_ingest`, `revalidate`, `recompute`,
    `place_cards_build`, `t7_slate`, `t8_angles`, and the place pipeline `place_extract`,
    `place_geocode`, `tour_graph_build` — AA-712, the five `retention_*` kinds — AA-825,
    `ops_alert_email` — AA-828). Every claim merges `progress.attempt_started_at` (AA-751, F18):
    `started_at` keeps the first attempt's start, so per-attempt age reads the progress key.
  - **Background kinds (AA-712)**: `@job_kind(..., background=True)` kinds never take the worker's
    last `BACKGROUND_RESERVE` (2) free slots (`Worker.claim_caps`), so a place-pipeline backlog cannot
    starve a wave's `a3_atomize` (claim is FIFO across kinds). Guarded by `test_aa737`. The rule is the
    pure `worker.claim_caps_for(max_parallel, caps, free_slots, running_background, background)`, also
    used by the backlog sample (below), so the two cannot drift.
  - **Retention (AA-825, migration 233, data standards DM-7 / C5.4)**: one background kind per log
    table in `services/jobs/retention_job.py` — `retention_llm_call_log` (180 d, daily rollup
    `shared.llm_call_daily`), `retention_tenant_api_usage` (90 d, monthly rollup
    `shared.tenant_api_usage_monthly`), `retention_job` (succeeded 30 d; failed/cancelled/stopped_budget
    90 d), `retention_decision_log` (180 d = Jev `CACHE_MAX_AGE_DAYS`; rows with a human `outcome`
    kept), `retention_cost_explorer_snapshot` (rows superseded by a newer fetch of the same day).
    Policies in `services/retention/policy.py`. Rollups are written before raw rows are deleted;
    batches of <= 5,000 rows by (ts, id) keyset, archived first to
    `s3://aa-cis-bronze-005097885195/archive/<table>/yyyy/mm/` as gzipped JSONL (pyarrow is not a
    dependency), never `VACUUM FULL`; rows referenced by a foreign key are never deleted. A manual run
    (`POST /admin/job-runner/retention/run`) is a DRY RUN unless `dry_run=false` + `max_rows` (the
    approved count). The ops-probe Lambda calls `POST /admin/job-runner/retention/schedule` once a
    day (skipped while a rerun wave is open): a kind stays DRY RUN until a live run with the same
    windows has succeeded, and a scheduled run whose candidates exceed 2x the last live run is HELD
    (bell `platform.retention.held`). Status: `GET /admin/job-runner/retention`.
    **Manual override** (S239, to prove rollup + archive before data ages): the same POST with
    `older_than` (a UTC date) enqueues `trigger=manual_override` with that cutoff instead of the window,
    only for the two rolled-up kinds, only where `ENVIRONMENT` is dev/test, cutoff >= 31 days old, DRY
    RUN by default, live at most 1,000 rows; it never counts as the last live run (schedule stays DRY
    RUN). Every live run of a rolled-up kind stops with nothing deleted if a day/month older than the
    cutoff is still missing from the rollup, and every archive object is HEAD-verified (Content-MD5 on
    the PUT) before its batch is deleted.
  - **Ops metrics sampler (AA-832, AA-751)**: `api/core/ops_metrics_sampler.py`, a background task of
    the **api** (not the worker) started in `api/main.py`'s lifespan after the main pool, cancelled
    before it closes, off when `ENVIRONMENT=test`, never raises. Every 300 s (first tick 30 s after
    start) it makes one PutMetricData per namespace (boto3 in a thread, connect 3 s / read 5 s, one
    attempt) and logs one `ops_metrics_sampled` line with the values (`rds_error` / `retired_error` /
    `jobs_error` when a part fails; the other parts still run). Each part runs under
    `asyncio.wait_for(PART_TIMEOUT_S = 60)` (the main pool has no `command_timeout`), so a hung query
    is a `TimeoutError` in the line and the next tick retries. Before the backlog sample it cancels
    queued/running rows of `shared.jobs.registry.RETIRED_KINDS` (kinds whose handler was removed;
    `ops_rds_role_connections` today, error `kind removed (AA-751)`, `retired_cancelled` in the line),
    so a rolling-deploy leftover neither pages as starved nor blocks the deploy gate:
    - `AA/RDS`, dimension `Role`: `RoleConnections` (Count) + `RoleConnectionLimitPct` (Percent) for
      every LOGIN role with a `CONNECTION LIMIT` (`pg_roles` LEFT JOIN `pg_stat_activity`, discovered
      each tick; the main pool's aa_cis_admin sees every role's sessions);
    - `AA/Jobs/CIS`, no dimensions (`shared/jobs/backlog.py`): `OverdueRunningJobs` = running rows
      whose current attempt (`progress.attempt_started_at`, else `started_at`) is older than 2 x the
      kind's `expected_seconds` (600 if unknown); `OldestQueuedAgeSeconds` = capacity-aware age of the
      longest-**starved** due queued row, 0 if none. A row is starved when no worker is live
      (`shared.job_worker`, `WORKER_ALIVE_SECONDS` rule), when no live worker has its kind in `caps`
      (unregistered), or when a live, non-yielding worker has a free slot, `claim_caps_for` keeps the
      kind and the kind is under its global cap. The age runs from the latest of `run_after` and the
      moment capacity last freed (latest finish / re-queue, the worker's start; for no live worker,
      the last time one was seen), so a full wave or the few seconds before the next poll read 0.
    AA-CIS-Infra alarms on both (`observability_ecosys.tf`, emails on ALARM only; `*-sampling-stopped`
    fires when no sample arrives). This replaced the AA-832 chain (ops-probe Lambda every 5 min ->
    `POST /admin/job-runner/rds-role-connections/schedule` -> job `ops_rds_role_connections` -> worker),
    both now removed. `@job_kind(..., deploy_gate_exempt=True)` stays as a mechanism (the pre-deploy
    guard `GET /admin/job-runner/deploy-gate` skips and lists such kinds in `exempt_kinds`); no kind
    uses it today.
  - The worker runs as its own ECS service `aa-cis-dev-worker` (`python -m shared.jobs.worker`,
    AA-651); the API task sets `JOB_WORKER_IN_API=false`. The in-API loop (FastAPI lifespan) still
    exists for local/dev use.
  - Slot budget (AA-737): the service worker runs `JOB_WORKER_MAX_PARALLEL` jobs at once (default
    8), with the default thread executor and DB pool sized to match. Per-kind caps are global
    across workers: `s1_rewrite` 6, `a3_atomize` 1 (deliberate — platform-wide recompute + Cohere
    pacing). Keep `s1_rewrite` cap + `a3_atomize` cap below the slot budget, or rewrites starve
    atomize for a whole wave (S217).
  - A graceful shutdown releases running jobs (attempt not counted); a crash is caught by the
    reaper after the 90 s lease.
  - After a deploy the old task drains for ~5 min; its worker stops claiming as soon as a worker of a
    newer ECS task-definition revision is live (AA-711, `job_worker.task_revision`, migration 199).
  - Job cost (AA-652 follow-up, migration 177): the worker binds the job id around each handler
    (`call_log.bind_job`), so every `llm_call_log` row the job causes carries `job_id`. Shown cost
    = `shared.job.cost_usd` (non-LLM spend the handler reports, e.g. DataForSEO) + the sum of
    those log rows, computed on read (`queue.LLM_COST_SQL`).
  - Admin: `/admin/jobs` page, API `/admin/job-runner/*`. (`/admin/jobs/{id}` is still the older
    `shared.pipeline_jobs` poll for A1 run-tour.) The page lists with `fields=summary` (S241:
    payload/result come back null; the drawer reads them from `GET /jobs/{id}`) and polls every 30 s
    only while the summary shows queued/running jobs (Vercel Fluid CPU, S240 audit).
  - Observability (AA-687, migration 178): each worker upserts its row in `shared.job_worker`
    (start, ~every 15 s, `stopped_at` at shutdown; reaper totals + last reaped ids). Read-only for
    the page (`GET /admin/job-runner/workers`); the queue never reads it. `GET /jobs/{id}` adds
    `links` (tenant versions / pieces the job wrote, payload tour), `GET /jobs/{id}/llm-calls`
    lists the job's `llm_call_log` rows. Each kind has `expected_seconds` (flag, never a stop);
    long kinds report `progress.{step,done,total}` (a3_atomize since AA-688).
  - **T2 tenant rewrite is a job** (`t2_rewrite`, AA-652): `tenant_tour_versions.job_id`; a job
    that ends without success marks the version `status='failed'` (kind hook `on_terminal`), and
    the portal shows "Writing failed" + Retry (`POST /v1/tours/versions/{id}/retry`) instead of
    an endless "Writing…". **T9 write is a job** too (`t9_write`, `content_piece.job_id`; a job that
    ends without success marks the piece `failed`, which the portal already shows with Retry).
    **A3 atomize is a job** (`a3_atomize`, one tour at a time), enqueued by publish
    (`process_export`) and by `POST /admin/atoms/atomize` (one job per tour).
  - **Recompute stage registry (AA-735 nac 3, ADR 0003 layer B + C)**: the platform-wide
    Segment/Score/Route recompute is a declared stage registry in `services/recompute/`, not two
    hand-written chains. Each stage (`segment`, `landing`, `score`, `route`) is a pure
    `run(ctx, scope)` that declares the tables it reads, the tables it writes and its
    `write_strategy` (`segment`/`landing` upsert; `score`/`route` versioned-swap — the SQL itself
    is unchanged, only declared). The orchestrator `run_stages(names, scope, pool=, progress=)`
    runs a named list in order. Two lists: `TOUR_STAGES` (segment→landing→score→route, a tour's
    publish / atom edit) and `PLATFORM_STAGES` (landing→score→route, platform score+route only).
    The `recompute` job and the `a3_atomize` path (its inline segment matching) run their work
    only through `run_stages`; the thin wrappers `recompute_segment_score_route` /
    `recompute_rankings_and_routes` in `services/export/handler.py` map the stage results back to
    the Jobs-page result keys (`{segment, ranking, route}` / `{ranking, route}`).
- **Bedrock Batch for S1 (AA-606)**: `shared/llm_client/bedrock_batch.py` +
  `services/content_generation/s1_batch.py`, endpoints `POST /admin/s1-batch/submit` and
  `GET /admin/s1-batch/{job_id}`. Built and merged, but **never run for real**: AWS has not yet
  enabled Batch Inference on acc3 (support case 178979743800653, blocker AA-624). Batch atomize
  (AA-621) waits on the same blocker.
- **Plans and quota (AA-640)**: the tour-rewrite quota comes only from `shared.membership_plans`
  (starter 50 / growth 200 / business 500). Over-quota rewrites are allowed, logged as
  `rewrite_over_quota` and billed as overage. `PLAN_LIMITS` in code now only holds the plan's
  default RPM; an admin can override one tenant's RPM (`PATCH /admin/tenants/{id}
  {"rate_limit_rpm": n}`, AA-766, audited as `tenant.rate_limit_changed`).
- **Billing (AA-798, migration 223)**: `shared.v_tenant_monthly_usage.llm_cost_usd` = month-to-date
  `SUM(shared.llm_call_log.cost_usd)` for the tenant (shadow and NULL-tenant rows excluded). Admin
  billing and the admin tenant list read that column as-is (one definition, `api/routers/_billing.py`).
  Migration 233 (AA-825) made the view's month filters sargable (range on the raw column, index
  `tenant_api_usage (tenant_id, called_at)`), same columns and numbers. The admin tenant usage
  history (`GET /admin/tenants/{id}/usage`) reads `shared.tenant_api_usage_monthly` for rolled-up
  months and raw rows for the rest.
  No plan attached → plan fields are null (no invented Starter defaults). Tenants never see cost
  (`/v1/billing` has no `llm_cost_usd`, AA-796).
- **Pipeline routes are admin-only (AA-799)**: every `/v1/pipeline/*` route requires the admin secret
  (`_require_admin`, a router-wide dependency in `api/routers/v1_pipeline.py`); a tenant JWT gets 403,
  no auth 401. The edge (API Gateway / ALB) does not route `/v1/pipeline` today. `/run` has no caller
  (deletion candidate).
- **Live writing progress (AA-637, `docs/adr/0004-*.md`)**:
  - Tenant writing jobs run in the background: the T2 tour rewrite and the T9 post write.
  - While running, each job binds a stream sink (`shared/llm_client/stream_sink.py`).
  - Writer-stage LLM calls stream through that sink into a Redis snapshot
    (`services/acp_shared/writing_progress.py`, key `wp:{tenant}:{kind}:{job}`, 1h TTL).
  - The portal polls `GET /v1/progress/{tour|piece}/{id}` (`LiveWriter.tsx`).
  - Only piece text reaches the snapshot (AA-668): a writer-stage call whose reply is not piece
    text (the T9 sentence repair's JSON patch) runs under `stream_sink.suppressed()`, and the job
    publishes the text it keeps with `progress_show()` (after a repair splice and before the save),
    so the final snapshot is the saved post / tour version.
  - Both jobs show in the same right-side **Writer panel** (AA-668, `WriterPanel.tsx`'s
    `WriterDrawer` shell): Social Content (Slate → goal → angle → write) and the T2 tour rewrite
    from My Catalog Tours (`TourRewritePanel.tsx`). Closing the panel never stops the job;
    `useWriteWatcher` polls it and raises a toast when it settles.
  - This is a view only: persisted rows are unchanged. There is no SSE, and API Gateway is
    unchanged.
- **Schemas** (owned here): `silver_aa_internal` / `gold_aa_internal` (raw→published tours),
  `acp_contract.*` and `acp_shared.*` (the pipeline tables — atoms, segments, ranking, routes,
  hubs, subjects, pieces, facts), `geo` (place pipeline, AA-712), migrations under `api/migrations/`.
- **Removed as dead code (15-21/09)**: the N7/N8 produce/deliver write path and its 5 tables
  (AA-603; `acp_deliver.tenant_tour_pages` is kept), the `/v1/s1-from-atom` route (AA-611), and
  the atom "star" (AA-609; the DB column `tour_atoms.starred` still exists). Do not rebuild on
  any of these.
- **ACP v1 run model is gone (AA-751, 16/10)**: `acp_shared.acp_runs` was dropped by migration 121;
  its last readers (`GET /admin/acp/stuck-runs`, `POST /admin/acp/runs/{run_id}/force-fail`,
  `services/acp_shared/cost_utils.py`, `services/acp_canary/`) are deleted. Stuck or long work is a
  `shared.job` row (reaper + admin Jobs page). `tests/unit/test_aa751_no_acp_runs_reader.py` fails
  on any new SQL against `acp_runs`. `infrastructure/canary/` (acc1 EventBridge skeleton) is deleted
  too: its schedule and Lambda no longer exist (checked 16/10/2026).

- **Photos (AA-708, migration 198)**: the `photo_sync` job copies the Jira CON board's PHOTOS
  Drive folders (public, read with a Google API key from `aa-cis/dev/gdrive-photo-reader`) to S3
  (`photos/<country>/<file id>-{1600,600}.webp`) and records `shared.place_photo`, matched to a tour
  (folder name) and, on request, to a `shared.destinations` place (file name). Images are public via
  `GET /content/photos/{id}?size=` (302 to presigned S3). With `match_destinations` it fills empty
  `shared.destinations.cover_image_url` (read by the TripPlanner) — off until the destinations are
  re-extracted from the rerun tours.
- **Place pipeline (AA-712, ADR 0007, migration 226)**: CIS — not TripPlanner — computes each live
  tour's day skeleton (start / end / overnight), places (= itinerary components, stable uuid5 ids),
  geocoded destinations and the tour graph (stops, nodes, edges, legs, junctions) into the CIS-internal
  schema `geo`, keyed by `source_id` (catalog identity). Jobs (`services/jobs/place_*`,
  `tour_graph_job.py`; logic in `services/places/`): `place_extract` per tour (after A3 atomize,
  set-active / restore, admin backfill `POST /admin/places/backfill`, ≤ 50 tours, dry-run cost
  estimate, refused during a wave); `place_geocode` (debounced, the ONLY writer of
  `shared.destinations` coordinates) — the model proposes a point (`a3_place_locate`), **Geoapify**
  (OpenStreetMap data; S236 decision, all countries; `GEOCODER_PROVIDER` = `geoapify` default |
  `model_only` for tests / emergency; `mapbox_permanent` / `amazon_location` fail as not supported —
  Mapbox is the basemap only, Amazon Location is not used) confirms it with the bake-off acceptance
  rule (`services/places/geomatch.py`: top 3, name match, no admin-area types, ≤ 25 km / 50 km for long
  features; `rank.confidence` recorded, never a gate) → `located_by='geoapify'`, `geo_precision='point'`;
  otherwise the model's point stays (`llm`, `geo_precision='area'` — never drawn as a pin; hard kinds
  such as temple / pass / lake / village get `review_reason` for `/admin/places`). Generic phrases
  ("ger camp", "local market") are not geocoded. Free plan 3,000 credits/day: ≤ 5 req/s, retry on
  429/5xx, daily credit guard 2,500 (UTC day, `geo.geocode_call`). Key: Secrets Manager
  `aa-cis/dev/geoapify` (plain string), worker task role reads it; never logged. **Attribution
  required wherever these coordinates are shown (CIS admin and every consumer, TripPlanner first):
  "Powered by Geoapify" (link) + "© OpenStreetMap contributors"** — also kept per row in
  `geocoder_meta.attribution` (Geoapify storage terms). Hard country/bbox guard, transit-aware outlier
  rule → `suspect`; admin-fixed (`manual`) rows are never overwritten; `tour_graph_build` (debounced, versioned swap, current + 2
  kept). Stages `a3_place_days` (Sonnet, Luna 20% shadow), `a3_place_locate`, `a3_place_embed`
  (Cohere 1536-d). `shared.destinations` is unique on `(name_norm, country, locality_norm)` (same
  name in two countries = two places). Other apps will read it only through `catalog.*` views
  (migration 228, next slice); TripPlanner's own extraction is frozen.

## Deploy & CI

- **Backend (ECS)**: shipped via CI; a new task definition rolls out on the `aa-cis-dev-api`
  service. This repo has **branch protection with 5 required CI jobs** — merge PRs with
  `gh pr merge --auto` (auto-merge once checks pass). Agents must not push to `main` directly.
- **Frontend (Vercel)**: deploys are created by CI, not the Vercel Git integration (S241 — Hobby
  cap ~100 deployments / 24 h for the whole team, canceled ones included).
  `frontend/vercel.json` sets `git.deploymentEnabled: false`; `.github/workflows/vercel-deploy.yml`
  runs `vercel deploy` (CLI pinned) from the repo root only when a PR or a push to `main` touches
  `frontend/**`: PR → preview of the head commit + one PR comment with the URL, then the UI smoke;
  `main` → production (+ a 200 check with the bypass header); `workflow_dispatch` → preview or
  production (production only from `main`). Needs the repo secret `VERCEL_TOKEN` (team-scoped);
  without it the job fails red on every target (nothing else deploys the frontend). A PR
  that touches no frontend file gets no preview. The check to look at is "Vercel deploy".
- **Portal (Vercel, AA-885)**: `.github/workflows/vercel-deploy-portal.yml` is the same workflow for
  `portal/**` (project id from the repo **variable** `VERCEL_PORTAL_PROJECT_ID`, red while unset; production
  check with the repo secret `VERCEL_PORTAL_BYPASS_SECRET`), check "Vercel deploy (portal)", then
  `ui-smoke-portal.yml` on the preview. The path filters are disjoint: a portal-only PR deploys once, an
  admin-only PR once, a PR touching both twice per push — count it against the Hobby cap (~100/day for
  the team, 4 projects). `portal/**` is not in `deploy-dev.yml`, so a portal change never redeploys ECS.
  `portal/vercel.json` also sets `git.deploymentEnabled: false`.
- **UI smoke (AA-732, `.github/workflows/ui-smoke.yml`)**: called by `vercel-deploy.yml` after a
  successful preview deploy (`workflow_call`, inputs `base_url` + `sha`), or run by hand
  (`workflow_dispatch`); Vercel's `deployment_status` no longer arrives. A Playwright suite (`tests/e2e/smoke/ui-smoke.spec.ts`, project `smoke`)
  opens the key admin pages as a logged-in admin and fails the check on any HTTP/console error,
  failing same-origin `/api/` call, or a blank page (no table rows and no kit empty state).
  Screenshots (desktop 1440 + mobile 390, light + dark) and the HTML report upload as an artifact
  and link from one PR comment. Secrets: `VERCEL_AUTOMATION_BYPASS_SECRET` (preview SSO bypass),
  `E2E_ADMIN_USERNAME`, `E2E_ADMIN_PASSWORD`. The bypass header is sent only to the BASE_URL
  origin (origin-scoped route), never to a third-party host. The admin suite also checks that the
  old portal URLs answer 308 to the portal host (`aa885-portal-redirect.spec.ts`). Not a
  required check.
- **Portal UI smoke (AA-885, `.github/workflows/ui-smoke-portal.yml`)**: Playwright project
  `smoke-portal` (`tests/e2e/smoke-portal/`) against a portal preview: host checks without a session
  (redirects, forged cookies refused, no admin routes = 404, security headers, `/privacy` + `/terms`), and
  logged in as a test tenant (API key in `E2E_TENANT_API_KEY`, copied by hand from Secrets Manager
  `aa-cis/dev/e2e-tenant/<slug>` — re-copy after a key rotation) every portal page with the shared
  `runPageCheck` + a 390 px no-sideways-scroll check, screenshots 1440 + 390, and the host-only cookie check.
  Logged-in tests skip without the key. The preview needs `VERCEL_PORTAL_BYPASS_SECRET` (passed to the
  helpers as `VERCEL_AUTOMATION_BYPASS_SECRET`). Lint: `portal-lint` CI job (non-required, + `tsc`). e2e specs carry no hardcoded credentials (shared helper
  `tests/e2e/helpers/auth.ts` reads the env). ESLint enforces UI-kit conventions in
  `frontend/eslint.config.mjs` (`frontend-lint` CI job, non-required).
- **Cognito E2E (AA-816 E1, `.github/workflows/e2e-cognito.yml`)**: `workflow_dispatch` on `main` only,
  GitHub environment `e2e`, OIDC role from secret `E2E_MAIL_ROLE_ARN` (Infra `aa-cis-dev-e2e-mail-reader`,
  read-only: List + Get on `s3://aa-cis-dev-e2e-mail-005097885195/inbound/`, no delete; 1-day expiry). Playwright
  project `cognito` (`tests/e2e/cognito/`, always the production host — the only registered callback): staff
  user "E2E Cognito" (`e2e@e2e-mail.lumiguides.it.com` — the SES test mailbox itself, reviewer, Booking none; a
  TEMPORARY exception to the `@adventure.asia` rule, see Staff roles) signs in by email code (`tests/e2e/helpers/mailbox.ts` reads the code from S3), lands under `/admin`, signs out,
  no silent re-login. The sign-in test skips unless `E2ECOGNITO=1` (set only by the workflow); only screenshots
  are uploaded (reports would carry the typed code). Not on any PR.
- **Migrations are not applied by Deploy Dev.** Deploy only builds the image and updates the ECS
  task definitions. Migrations are applied only through the AA-CIS-Infra **DB Migrate** workflow
  (`check` → `apply`) from a byte-identical copy in `db/migrations/cis/` (S225); never by hand. A
  migration that the new code needs is applied before that code merges.
- **`catalog.*` views are frozen from the X1 S1 merge until X1 S22** (AA-852 / AA-860, X1 plan XD9,
  `AA-Ecosys/docs/audits/2026-10-17-S241-x1/x1-plan.md`). The views belong to the NOLOGIN role
  `catalog_owner`; DB Migrate logs in as the RDS master until S22, the master has not been a member of
  `catalog_owner` since 225 and must never become one (the `catalog_owner_aa_master_read` policies
  would reach the CIS runtime). The old 225/235 `GRANT catalog_owner TO CURRENT_USER` dance is refused
  by the AA-CIS-Infra role lint for files ≥ 243. So no migration here may `CREATE/ALTER/DROP VIEW
  catalog.*` until S22: the lint asks for the recipe below and DB Migrate refuses a recipe file at
  `check` (exit 8, "login role is not a member of catalog_owner"). From S22 (`db_migrator`, a member
  of `catalog_owner`, is the login) a catalog view change uses this recipe, never `RESET ROLE` (it
  returns to `db_migrator` and leaves mis-owned objects):
  `GRANT CREATE ON SCHEMA catalog TO catalog_owner; SET LOCAL ROLE catalog_owner; CREATE OR REPLACE
  VIEW catalog.v_… AS …; SET LOCAL ROLE cis_owner; REVOKE CREATE ON SCHEMA catalog FROM catalog_owner;`
  A catalog change that cannot wait for S22 is Nghiệp's call (plan XD9 alternatives).
- **The CIS runtime role is `cis_app` (X1, AA-860; api + worker switch in S18).** Migration 244 grants it
  DML / sequences / non-trigger function EXECUTE in the 11 CIS runtime schemas (not `catalog`, `booking`,
  `tripplanner`, `public`), default privileges `FOR ROLE aa_cis_admin` for later objects, read-only
  `shared.schema_versions`, and a permissive `<table>_cis_app_all FOR ALL TO cis_app USING (true) WITH CHECK
  (true)` policy on every RLS table, ENABLE-only **and** FORCE (the master reads FORCE tables unfiltered
  today; most code sets no GUC). **Every new RLS table in those schemas needs that policy in the same
  migration** (CI standing check `tests/integration/test_x1_cis_app_privileges.py`), and a guard asserting an
  exact policy set on an RLS table must allow it. The conftest replay skips 244; the X1 tests apply and undo it.
- **CIS schemas and objects belong to the NOLOGIN role `cis_owner` from migration 245 (X1 S21, AA-860).** 245 moves
  the 12 CIS schemas and every object the RDS master owned in them (the `catalog.*` views stay `catalog_owner`'s;
  `public.*` and the extensions stay the master's, they belong to the Infra `roles/` dir). From then on DB Migrate runs
  every `cis/` file as `cis_owner` (it switches to the live owner of schema `shared`; from X1 S22 the login is
  `db_migrator`), and default privileges `FOR ROLE cis_owner` give `cis_app` what a file creates. Rules for every
  later CIS migration: never `RESET ROLE` (go back with `SET LOCAL ROLE cis_owner`), no `SECURITY DEFINER`, no
  `CREATE/ALTER/DROP ROLE|EXTENSION` (→ Infra `roles/`). `cis_owner` is **filtered by FORCE RLS** (unlike the master,
  it is not in `rds_superuser`), and every view without `security_invoker` reads its base tables as its owner, so 245
  gives it a permissive `<table>_cis_owner_all FOR ALL TO cis_owner USING (true) WITH CHECK (true)` policy on the 8
  FORCE tables: a migration that FORCEs RLS on another table adds both `<t>_cis_app_all` and `<t>_cis_owner_all` in
  the same file (CI standing checks in `tests/integration/test_x1_cis_app_privileges.py`), and a guard asserting an
  exact policy set must allow both. Only the master and `db_migrator` may be members of `cis_owner` (a member reads
  every tenant of those tables). The conftest replay skips 245; `test_aa860_migration_245.py` applies it as an
  RDS-like master after 244 and undoes it. Rollback is a prepared Infra `roles/` undo (a `cis/` file runs as
  `cis_owner` and cannot give objects back). **CIS migration freeze** from the Infra copy of 245 until X1 S22's 246
  probe passes.
- **Verify the frontend with a full `next build`**: `tsc --noEmit` and eslint miss some type
  errors that break the Vercel build. The CI Lint job only runs flake8 on Python
  (`--max-line-length=120`).
- **Health and version (AA-884)**: `GET /health` returns `git_sha` (the image's `APP_GIT_SHA`, the merge commit) and
  `GET /health/ready` (public, 5 s in-process cache) returns 200 only when the DB answers and the newest numeric CIS
  migration in `shared.schema_versions` is ≥ `api/core/readiness.py` `REQUIRED_CIS_SCHEMA` (bump it in the PR whose
  code needs a newer migration). Deploy Dev's smoke asserts both, and that a live worker reports the same SHA
  (`shared.job_worker.git_sha`, written at worker start).
- **Definition of "done" for a backend issue** (program rule): merge + green CI is not enough.
  Also verify (a) Dev deploy **rollout COMPLETED** (ECS running the new taskDef, old deployment
  drained) and (b) the **live endpoint** (curl → HTTP 200 + correct shape). Only then is it Done.

## Contracts with other apps

CIS is the content system of record (ADR 0001 / 0002). Other apps read what it publishes; they never
write CIS schemas. Owners per schema: `AA-Ecosys/docs/database/schema-ownership.md`.

| Consumer | Reads from CIS | Writes into shared | How it is granted |
|---|---|---|---|
| AA-TripPlanner-Web | `gold_aa_internal.published_tours` (tenant RLS, AA tenant), `acp_contract.v_active_tour_atoms` (place/activity columns), `shared.destinations` (read-only), photo views `shared.v_destination_photos` / `v_tour_photos` | `shared.llm_call_log` (rows tagged `quality_signal.app = "tripplanner"`) | CIS migrations 173 (gateway tables + log), 180 / 216 (atoms), 201 (destinations read-only, photo views) to role `tripplanner` |
| AA-TripPlanner-Web | Model Gateway config `shared.llm_role_config`, `shared.llm_model_catalog` (stages `tp_*`) | — | CIS 173 |
| AA-Booking, AA-TripPlanner-Web | **Catalog read model**, schema `catalog` (AA-679): `v_tour` (live AA tours, key `(source_system, source_id)` = `('cis', raw_tours.source_group_id)`, `content_hash`), `v_tour_day` (parsed canonical itinerary), `v_tour_place` (active-atom places + place card + destination lat/lng), `v_tour_photo`, `v_destination`, `v_destination_photo`. **Coordinates carry an attribution duty**: wherever a consumer shows `lat`/`lng` from these views (map pins, area circles), it must show "Powered by Geoapify" (link) + "© OpenStreetMap contributors" (AA-712, Geoapify Free-plan + ODbL terms); the place views of migration 228 expose `located_by` / `geo_precision` so a consumer draws `area` points as areas, not pins. Contract `AA-Ecosys/docs/contracts/catalog.tour.v1.json`. New consumers read only `catalog.*`; TripPlanner's older grants above migrate to it. Events `catalog.tour.published/updated/trashed` (outbox) are the next AA-679 slice | its own `booking.*` | CIS 225 to roles `aa_app_user`, `tripplanner`, `booking` (unguarded). Views owned by NOLOGIN role `catalog_owner` (no BYPASSRLS, no members), which reads `published_tours` / `raw_tours` through RLS policy `catalog_owner_aa_master_read` (AA master tenant only); consumers need not set `app.tenant_id`. 225 also makes `tenant_isolation` on those two tables treat `app.tenant_id = ''` as no tenant (NULLIF) instead of raising |
| Ops-probe Lambda (AA-CIS-Infra) | `POST /admin/dfs-balance/check`, `/admin/jev-canary/check`, `/admin/jev/402-sweep`, `/admin/waves/check` daily | — | admin secret |

CIS as a consumer (AA-858): the `/admin/overview` business view reads `booking.v_ops_funnel_daily`,
`booking.v_ops_case_status` (Booking bk-017) and `tripplanner.v_ops_traffic_daily` (TripPlanner tp-015) — aggregate
views the producers own and grant to `cis_app` in their own migrations (contract `ops.overview.v1`, AA-Ecosys
`docs/contracts/ops.overview.v1.md`; AA-CIS-Infra `db/owners.yaml` consumer_grants). CIS never reads `booking.*` /
`tripplanner.*` tables.

Platform services other apps may reuse (already used by TripPlanner where noted): the Model Gateway
and `llm_call_log` (TripPlanner), the durable job runner `shared.job` (CIS only for now — a second
enqueuing repo is AA-751), the cost guard `shared.spend_budget`, the Jev decision layer, and rerun
waves (`/admin/waves`, AA-727).

## Staff roles and permissions (AA-656)

One staff identity model. Every staff user is a row in `shared.admin_users` (role enum
`shared.admin_role`: `admin`, `reviewer`, `content` — migration 218 — and the read-only `viewer` — migration
249, AA-878; identity columns from migration 247:
`email` lower-case `@adventure.asia` only — one TEMPORARY exception until the Cognito E2E work is done (AA-816,
S243 option B, root ADR 0006a): the exact address `e2e@e2e-mail.lumiguides.it.com` as a reviewer, not an owner,
no password, no Booking grant (migration 250 CHECK + `staff_directory.E2E_TEST_STAFF_EMAIL` /
`require_e2e_test_scope` + the WORKFORCE triggers) —, `display_name`, `cognito_iss` + `cognito_sub`, `last_login_at`
("last seen"), `is_owner`; `password_hash` nullable, NULL fails closed). A staff member's role in another app
lives in `shared.staff_app_grant (admin_user_id, app, role)` (`app` = `booking`, roles `admin` / `sales` /
`contracting` / `content_creator`). Root ADR 0006 + amendment 0006a are the design; tenants are separate
(`cis_tenant_token`, role `tenant`).

**Sign-in (ADR 0006 Phase 1, live on Dev).** Staff sign in with an email one-time code on Cognito WORKFORCE
(pool `us-west-1_bCcFvbSOS`) through managed login on our own domain `https://staff-auth.lumiguides.it.com`
(AA-branded; the prefix `amazoncognito.com` domain tripped a Safe Browsing false positive). `/login` shows
"Sign in with company email" and never redirects by itself.
- The BFF (`frontend/lib/cognito.ts`, `lib/cognitoRoutes.ts`, routes `app/api/auth/{signin,callback}/{cognito,
  cognito-breakglass}`, `app/api/auth/refresh`) runs authorization code + PKCE (state + nonce in the 5-minute
  `cis_oauth_tx` cookie) with a confidential client. Redirect URIs come from a fixed allow-list
  (`aa-cis.lumiguides.it.com`, localhost in development); any other host (Vercel previews) gets
  `cognito_host` and only the password form. Domain, issuer and client ids are code constants with
  `COGNITO_*` env overrides; only the two client secrets are set in Vercel (Production).
- Cookies: `cis_admin_token` holds the Cognito **access** token (15 min; the cookie lives for the refresh window
  so `proxy.ts` can see it expired), `cis_staff_rt` the refresh token (httpOnly, `path=/api/auth`, 12 h,
  1 h for break-glass; the window is not extended by rotation). Refresh happens only in `/api/auth/refresh`
  (single-flight; `proxy.ts` redirects page navigations there, `SessionKeepAlive` POSTs before expiry).
  `cis_role` / `cis_user` are display hints set by the server, never trusted. Logout revokes the refresh token
  and goes through Cognito `/logout`.
- A sign-in completes only after `/auth/verify-admin` accepts the token; a refusal lands on
  `/login?error=no_access` with no session cookies.

**Verification (backend).** `api/core/identity_verifier.py` (app-neutral, to be copied byte-identical into
AA-Booking) + `api/core/identity_config.py` (CIS surface) + `api/core/staff_identity.py` (resolver):
- issuer chosen by the **unverified `iss`**; only the WORKFORCE issuer is accepted for staff (any other `iss` →
  401, a JWT without `iss` → the legacy HS256 path); RS256 only, `client_id` ∈ {`cis-admin`, `breakglass`},
  `token_use=access`, no `aud`, leeway ≤ 60 s; one JWKS cache per issuer, prefetched at start-up
  (`identity_jwks_prefetched`), unknown `kid` refetched at most once a minute, last good keys kept;
- the row is found by `(iss, sub)`; on the first sign-in it is linked **once** by `lower(email)` to an active
  row with no Cognito identity (`email_verified` is trusted from the pre token trigger, which refuses
  unverified or off-domain addresses; WORKFORCE access tokens do not carry the claim, ADR 0006a A7). A row
  already bound to another `sub` is never rebound (401; rebinding is an owner action);
- role = the highest `cis-*` group (`admin > reviewer > content`); no `cis-*` group → 403 even with `booking-*`
  groups; a different DB role is logged as `staff_role_drift`, not denied; inactive or unknown row → 401;
  verifier/JWKS/directory outage → 503. Lookups are cached 60 s per `(iss, sub)`.
- `api/middleware/staff_bearer.py` (registered right after the rate limiter) handles every staff path: a
  verified Cognito Bearer **overwrites** `x-admin-role` / `x-admin-user-id` from the verified principal
  (client copies dropped), sets the `current_staff` contextvar and strips `Authorization`; an invalid
  WORKFORCE Bearer → 401; no Bearer or a non-WORKFORCE token → unchanged. The BFF forwards the Bearer on
  `/api/admin/*`, `/api/pipeline/*`, `/api/tenant/pipeline/brand-identity` and the staff prefixes of
  `/api/tenant/*`. `X-Admin-Secret` is still required on every staff call until the cutover.

**Legacy password login (until the cutover).** `/auth/admin-login` (HS256 JWT signed with `JWT_SECRET`) stays.
`LEGACY_ADMIN_LOGIN_ENABLED` (default on; only `0` / `false` / `no` / `off` switch it off, same parsing in the
API and the BFF) gates **only** the CIS UI password form + BFF `/api/auth/login` (404 when off) and the HS256
path of `/auth/verify-admin`. `/auth/admin-login` and `/auth/booking-token` stay on for AA-Booking until its
Phase 2. Previews must keep the flag on (they cannot be Cognito callbacks). For an HS256 session
`/auth/verify-admin` re-reads `shared.admin_users` (cached 60 s), so a deactivation or demotion lands within
~6 min (BFF verify cache 5 min + 60 s). Cutover preconditions (ADR 0006): every active row has an email and a
`cognito_sub` from that person's own sign-in, managed-login break-glass tested; then the secret check becomes
principal-aware, `ADMIN_SECRET` is rotated and the flag goes off.

**Staff directory writes (AA-816).** `api/services/staff_directory.py`, routes in `api/routers/admin_platform.py`:
create (`AdminCreateUser` with `MessageAction=SUPPRESS` + `email_verified=true`, `@adventure.asia` only, no
password), roles (CIS role + Booking grant, mirrored to `cis-*` / `booking-*` groups, add before remove),
disable (`AdminDisableUser`, then `AdminUserGlobalSignOut`), enable, sign-out ("reset" = re-enable + global
sign-out; staff have no password) and `/sync` (retry). DB first, then Cognito; a failed Cognito step answers
502 "saved, Cognito pending" with an idempotent retry. Every change is audited in `acp_shared.audit_log`.
- `staff.write` = role `admin` **and** a Cognito session (`auth = cognito`; legacy password sessions only read)
  **and** `admin_users.is_owner`. The owner flag is set out of band (`scripts/link_staff_identity.py`), never
  through the API. Writes lock the owner rows and re-read the actor in the transaction; the last active owner
  cannot be demoted or disabled; nobody demotes or disables themselves.
- The break-glass row (`admin@adventure.asia`, username `admin`) is read-only in the UI (403 on every write).
  Break-glass itself is password + TOTP through `/api/auth/signin/cognito-breakglass` (no button), disabled
  between uses; runbook in root ADR 0006a A4.
- The CIS task role (api and worker) may call only the directory allow-list on WORKFORCE (`identity_iam.tf`,
  with an explicit Deny on password/attribute/MFA/auth/delete actions); the writer also refuses other
  operations in code. Residual risk (ADR 0006a A8): IAM cannot stop `AdminCreateUser` with a temporary password
  or `RESEND`; the reconcile job is the detective control.
- New staff get no Cognito mail (SUPPRESS); the staff invitation email is in progress (follow-up).

**Reconcile job.** `staff_directory_reconcile` on `shared.job` (`services/staff_reconcile.py`,
`services/jobs/staff_reconcile_job.py`): report only, `ListUsers` / `ListUsersInGroup` only (no `AdminGetUser`,
it counts as MAU). Reports drift (Cognito users missing from the directory, rows missing from Cognito,
group/role/email drift, rows bound to another sub or issuer, non-`CONFIRMED` users, break-glass state) and
**cutover readiness** (active rows without an email or `cognito_sub`). On demand: Staff page "Check now"
(`POST /admin/platform/staff-directory/reconcile`, read `GET …/health`, `staff.read`); the daily run goes
through the machine route `POST /admin/job-runner/staff-reconcile/schedule` once the ops-probe Lambda calls it
(Infra follow-up). Job results reach non-admin roles redacted. `GET …/health` also returns `readiness_now`:
the readiness recomputed from the directory rows on every read (`staff_reconcile.readiness_from_rows`, no Cognito
call), so it matches the Staff header right after a disable or invite; only the Cognito drift is "as of" the last
run (S243).

The matrix lives in `frontend/lib/rbac.ts` and is read by `frontend/proxy.ts` (pages; the Next 16 Proxy on the Node.js runtime,
`middleware.ts` until S243), the BFF proxies
(`requireAdmin(req, API_RULES.x)` → 401 bad/missing token, 403 wrong role) and the admin nav (an item
shows only when the role may open its page).

**Admin console layout (AA-883, S241 design option A).** One console (`aa-cis-frontend`), nav model
`frontend/app/_kit/adminNav.tsx` in two sections named after who a page serves: **Platform** (Overview,
People & access › Staff, Tenants, Jobs, AI & spend, Developer) and **CIS Content** (Pipeline, Assets,
Intelligence, Tenant output). Section headers fold and show only when the role sees both sections.
The other apps are links from the typed registry `frontend/lib/apps.ts` (Booking admin, TripPlanner
public site; no tenant-portal link until there is a real support view, since the portal needs a tenant
session); deep links are built only there, never from a return URL, never with a
token. New platform pages live under `/admin/platform/*` (backend `api/routers/admin_platform.py`).

**Platform apps, audit and cost (AA-884).** Router `api/routers/admin_platform_ops.py` (read only, admin-only POLICY
entries; no deploy/scale button, no AWS/Vercel/GitHub write token):
- `GET /admin/platform/apps[/{app_id}]` (`ops.read`) — `api/services/platform_apps.py` `collect_apps()`: one card per
  app from the backend registry `api/core/app_registry.py` (code constants). CIS: API SHA + task revision, live
  workers' SHA (parity), the deploy gate (`shared/jobs/worker.py` `deploy_gate_status`, same as the CI guard), schema
  readiness. Booking: HTTP `/booking/health` + `/booking/health/ready` (2 s). TripPlanner: no health endpoint yet;
  today's customer-facing LLM spend (TP `ONLINE_STAGES`, `quality_signal->>'app' = 'tripplanner'`) vs the mirrored
  default cap (5 USD; the live cap is a TP env var). Cached 60 s in Redis, built only on a page view (no poller).
  No ECS/Lambda describe calls, no cross-schema reads.
  **One health definition (S243):** a card's `status` is `overall.status` from `api/services/app_health.py`
  `overall_status(probe, traffic)` — the worst known signal of the app's own check (`probe`; none for TripPlanner) and
  its CloudWatch traffic of the last 24 h (`traffic_section`, the AA-858 health section and its Redis key, shared by
  both pages). A health check that does not answer counts as "degraded" (basis "Health check (did not answer)");
  firing CloudWatch alarms are "degraded", not "down" ("down" = a health endpoint answering with an error). `/admin/overview/ops` copies each card's `overall` onto its health tile, so Overview and Apps always
  show the same status, in the same words (`frontend/lib/platform.ts` `APP_STATUS_LABEL`). Do not judge app health
  another way on a new page.
- **Staff audit trail.** `acp_shared.audit_log` is append-only since CIS 248: UPDATE/DELETE/TRUNCATE raise 42501;
  a BEFORE INSERT trigger chains every new row (`chain_seq`, `prev_hash`, `row_hash` = sha256 of a canonical text,
  under `pg_advisory_xact_lock(884, 1)` — so a writer inside a transaction writes its audit row LAST; needs READ
  COMMITTED). `api/middleware/staff_audit.py` (inside `StaffBearerMiddleware`, outside the rate limiter) adds one
  `staff.request` / `machine.request` row per non-GET call on a staff path that carries the admin secret: actor
  from the verified Cognito identity, else `x-admin-user-id` marked `details.auth = legacy_unverified`, method,
  route template, status (denied calls included); never the body or query values. `GET /admin/platform/audit`
  (`audit.read`, filters + keyset cursor, staff scope by default) and `GET /admin/platform/audit/verify`
  (`audit.read`; Python recompute of the chain, `api/services/platform_audit.py`, must stay equal to the trigger;
  a row without `chain_seq` created after 248 was applied — a trigger bypass — also makes it `ok: false`).
  The RDS master can still bypass the triggers until the X1 runtime role and the 245 owner transfer; the chain
  makes such an edit detectable, not impossible. Integration tests delete their own audit rows only through
  `tests/integration/_audit_cleanup.py`.
- **Cost per app** — the one cost module of the console: `api/services/platform_cost.py` `cost_by_app()` (AA-858's
  overview card imports it; do not compute spend another way). Row labels/hints (`ROW_LABELS` / `ROW_HINTS`) and
  `ai_outside_aws_usd` (= AI − AI already on the acc2 bill) come from this module, so both pages show AWS bill + AI
  outside the AWS bill = Total with one wording (`frontend/lib/costWording.ts`); the Cost page's KPI strip shows the
  deduction (S243). `ai_spend_by_provider()` gives the same month's AI per provider family (all `bedrock*` = bedrock;
  `typesafe`, how Jev decisions are logged, = jev)
  for the legacy `/admin/overview` provider table. AWS = acc2 Cost Explorer grouped by the `App` tag
  (`shared.cost_explorer_app_snapshot`, CIS 248; untagged is its own row with services), AI = LLM gateway estimates
  by `quality_signal->>'app'` from `shared.llm_call_log` + `shared.llm_call_daily`; acc2 Bedrock is subtracted from
  AI so it is not counted twice; acc1/acc3 Bedrock CE actuals are a cross-check line. `GET /admin/platform/cost`
  (`spend.read`) refreshes a month whose snapshot misses days or is > 24 h old (and may still change) with one CE
  request (~$0.01) behind a Redis lock, at most once a day per window (a Redis attempt marker, also after an empty
  or failed answer); `POST /admin/cost-explorer/check` (the "Refresh from AWS" button) also
  fetches the App tag (+1 request). No scheduler yet.
- **Pages** (admin only, kit + react-query, no polling; types in `frontend/lib/platform.ts`, links from
  `frontend/lib/apps.ts` `APP_REGISTRY`): `/admin/platform/apps` (nav group **Apps**; one card per app, Refresh =
  `?refresh=1`), `/admin/platform/audit` (People & access › **Audit log**; server filters, keyset "load older", chain
  badge, Legacy badge for `legacy_unverified` rows, link to Booking's own audit log) and `/admin/platform/cost`
  (AI & spend › **Cost per app**; month picker, "Refresh from AWS" = `POST /admin/cost-explorer/check` for the month,
  ~$0.04). Notes: `docs/implementation-notes/AA-884-ui.md`.

**Ops overview (AA-858).** `GET /admin/overview/ops` (`ops.read`, not a machine route; the CI smoke keeps
`GET /admin/overview`) feeds the business view at the top of `/admin/overview`. Package `api/services/ops_overview/`,
four independent sections, each cached in Redis on its own key (`admin:overview:ops:<section>:v1`) and each degrading
to `{status: "unavailable", reason, message}` instead of failing the call:
- `health` (`cloudwatch.py`, 5 min): one `GetMetricData` call (16 metrics: API Gateway Count/5XX/4XX/Latency p95 for
  `aa-cis-<env>-api` and `aa-booking-<env>-api` with `Stage = <env>`, Lambda Invocations/Errors/Throttles/Duration p95
  for `aa-tripplanner-<env>-browse|assembly`) + `DescribeAlarms` mapped by name prefix (`TargetTracking-*` skipped);
  read-only IAM on the task role (AA-CIS-Infra `ecs_task_infra_status_readonly`). Never called without Redis
  (`cache_offline`); a `SET NX` lock lets one request compute while the others get the last good value.
- `tripplanner` / `booking` (`activity.py`, 5 min): only the producer contract views above + `shared.llm_call_log`
  stage `tp_search_embed` for AI searches (cache misses only). A missing view or grant → `contract_view_missing`.
- `cost` (`cost.py`, 15 min): built only on AA-884's `platform_cost.cost_by_app` (this month + last month), relabelled
  for a business reader; no CE code of its own.
Cost: ≈ $0.0002 per health refresh, worst case ≈ $0.60/month; CE as AA-884. No new env var (names derive from
`ENVIRONMENT`).
Every number on `/admin/overview` (business view and the CIS sections) names its source in a `SourceHint` (kit
`InfoTip`); the texts, in plain business words with no table or metric names, live only in
`frontend/app/admin/overview/_components/sources.ts` (guard `tests/unit/test_aa858_overview_sources.py`; e2e smoke
`aa858-ops-overview.spec.ts` (e)/(f) checks every `[data-kpi]` tile and `[data-num-col]` column has one). The API's
`sources` field keeps the engineering detail and is not shown. The ops cards are not polled: their texts say
"updated on the server every 5 minutes (press Refresh to load the latest)"; the AWS bill part of Cost per app is
fetched about once a day.

| Area | admin | reviewer | content | viewer | tenant |
|---|---|---|---|---|---|
| Content work pages: Upload, Rewrite (S1) | ✓ | ✓ | ✓ | — | — |
| Content pages: Review Queue, Master Content, Settings (viewer: read only, write controls hidden by `can()`) | ✓ | ✓ | ✓ | read | — |
| Overview, Photos, Social Content (atom curation), Content Trace, A4 Quality (`/admin/platform-stats`), Content Quality, SEO Intelligence, Tenants, External Spend, Jobs, Jev Decisions, API Docs | ✓ | — | — | — | — |
| Platform pages `/admin/platform/*` (AA-883 Staff directory; writes need `staff.write`, owners only) | ✓ | — | — | — | — |
| BFF `/api/admin/openapi.json` and `/api/admin/platform/*` (any method; `lib/rbac.ts` API_READ_RULES, backend re-checks the role) | ✓ | — | — | — | — |
| Backend docs `openapi.json`, `docs`, `redoc` through the staff branch of `/api/tenant/*` (`lib/rbac.ts` isApiDocsPath) | ✓ | — | — | — | — |
| Tenant portal `/portal/*` — **portal app** (`portal.lumiguides.it.com`, AA-885; no staff access) | — | — | — | — | ✓ |
| BFF proxies `/api/admin`, `/api/pipeline`, `/api/tour-full`, `/api/tenant/pipeline/brand-identity`, `/api/tenant/*` (admin app; `/api/tenant/*` is staff-only since AA-885) | ✓ | ✓ | ✓ | GET only | — |
| AA-Booking token `POST /auth/booking-token` | ✓ | ✓ | ✓ | 403 | — |
| `/api/tenant/*`, `/api/tenant/me`, `/api/playground/proxy` — **portal app** BFF, tenant JWT only | — | — | — | — | ✓ |

**Read-only `viewer` (AA-878).** The role behind the UI-smoke E2E account (`e2e-claude-code-viewer`, a legacy
password row; password only in Secrets Manager `aa-cis/dev/e2e-test-viewer`; created with
`scripts/create_staff_user.py --password-from-secret`). Capabilities `content.read` only. It is refused on
**every** non-GET twice: the BFF `requireAdmin()` answers 403 before attaching the secret (`lib/rbac.ts`
`READ_ONLY_ROLES`), and the backend `denial_reason` refuses `READ_ONLY_ROLES` on any method but GET/HEAD/OPTIONS
whatever the route's capability (the notification bell's PUTs need `content.read`, the viewer still cannot clear
it). Admin-only reads stay refused by POLICY; the role-filtered open reads (search, bell, tenant details) treat
it like reviewer/content. No Booking access (`/auth/booking-token` → 403, `staff_authz.NO_BOOKING_ROLES`). Not
assignable from the Staff page (`staff_directory.CIS_ROLES`, `lib/rbac.ts` `ASSIGNABLE_STAFF_ROLES`) and no
Cognito group (a `cis-viewer` group is deferred to AA-816 E1); the reconcile report lists the row under cutover
readiness (no email / sub), a legacy-only exception. UI smoke runs in **viewer mode** when the GitHub
`E2E_ADMIN_*` secrets hold this account (the role comes from the login answer): readable pages get the full page
check, admin-only pages must redirect to `/admin/review`, a BFF write and the staff list must answer 403.

AA-Booking staff roles map onto the same three: `admin` → admin, `sales` → reviewer tier,
`content_creator` → content (the CIS `viewer` maps to nothing: no Booking token). Reviewer and content reach the backend through the same admin secret;
what they may call there is narrowed by the backend capabilities below (AA-882), not by the BFF yet
(`canWriteAdminPath` still allows writes by default until GĐ4b).

**Backend capabilities (AA-882, S241 GĐ1 + GĐ3).** `api/core/staff_authz.py` holds `ROLE_PERMS` (role →
capabilities: `content.read/write` and `review.act` for every staff role; `master.admin`,
`atoms.write`, `photos.write`, `a4.unpublish`, `quality.write`, `content.settings.write`, `tenant.*`,
`ops.*`, `ai.admin`, `spend.*`, `staff.read`, `audit.read` for admin; `staff.write` is an owner flag,
not a role permission) and `POLICY` keyed by `(method, route template)`. The router-level dependency
`staff_policy` (secret check, then policy) refuses with 403 a role that lacks the route's capability;
`staff_policy_if_staff` does the same only for a request carrying the valid secret, on the routers
whose own guard also admits a tenant JWT or answers 401 (`admin_pipeline`, `admin_atoms`,
`/v1/pipeline`, `/v1/rules`, `/v1/social`). The role is the `x-admin-role` the BFF forwards
from the verified token; for a Cognito Bearer the `staff_bearer` middleware overwrites it from the
verified principal (header trust remains only for legacy and secret-only requests until the cutover). The secret **without** a role header is the *machine*
principal (CI deploy preflight/smoke, the ops-probe Lambda), allowed only on `MACHINE_ROUTES`.
Every staff proxy that carries the secret (`/api/admin/*`, `/api/pipeline/*`, `/api/tour-full/*`,
`/api/tenant/pipeline/brand-identity` and the staff branch of `/api/tenant/*`) forwards `x-admin-role`
(a test lists them); a staff proxy without it would act as the machine. Since GĐ3 every staff write
(`/admin/*`, `/v1/pipeline|rules|social/*`) has a POLICY entry, and a GET has one only when its data
is admin-only (tenant accounts and billing, spend incl. the Jev credit ledger and the shadow cost
report, system overview/infra, rerun waves, every tenant's A4 logs / content quality / atoms (`tenant.read`),
the staff directory `GET /admin/platform/staff[/{staff_id}]` = `staff.read`, AA-883 GĐ2); other GETs
stay open to every staff role. Three open reads filter by role inside the handler: `GET
/admin/tenants/{id}/details` is open only for the aa_internal tenant (Master Content) and `tenant.read`
otherwise; `GET /admin/search` returns tenants only with `tenant.read` and jobs only with `ops.read`;
the notification bell (`/admin/notifications*`, one global list with one `is_read`) shows, counts and
clears for a role without `ops.read` only the rows targeted at a content role, never a `platform.*` or
`spend_budget_alert` row. The UI mirrors the capabilities in `lib/rbac.ts` `ROLE_PERMS` / `can()` (kept
equal by a test) to hide controls the backend would refuse. `tests/unit/test_aa882_route_policy.py` enforces it (every staff write
has a policy, every staff router runs the policy, `/admin/platform/*` has no open read, snapshot
`tests/unit/snapshots/aa882_staff_route_policy.json`). Behaviour change for reviewer/content: Master
Content lifecycle (activate/deactivate/trash/restore) and Settings brand writes are admin-only (§5.1
of the S241 admin-console design). Staff roles here and in `lib/rbac.ts` are kept equal by a test. Every secret-bearing proxy builds its backend path with
`frontend/lib/bffPath.ts` `proxyPath()`, which refuses a dot, empty or slash-carrying segment (Next.js decodes
`%2F` inside a segment) and percent-encodes the rest, so the path the rbac rules checked is the path the backend routes. Booking staff log in through the CIS
legacy password login (`/auth/admin-login`, kept for Booking until ADR 0006 Phase 2); Booking keeps a thin
profile keyed by `admin_users.id` and verifies a CIS-issued JWT with `aud=booking` (root ADR 0004, O1).

### Booking staff token (AA-680)

CIS is the staff identity provider for AA-Booking. Two public endpoints, both on `app` in
`api/main.py`, key handling in `api/core/booking_jwt.py`:

- `POST /auth/booking-token` — needs a CIS **HS256** staff session (a Cognito session cannot mint one until
  Booking Phase 2): the `cis_admin_token` JWT as an `Authorization: Bearer` header or the cookie. The staff row is re-read from `shared.admin_users`
  (must be `is_active`; the role comes from the row, not the session). Returns
  `{access_token, token_type: "Bearer", expires_in: 900}`. 401 for no/invalid/tenant session or an
  inactive user; 503 when the signing key is not configured.
- `GET /auth/.well-known/jwks.json` — the public keys (`kty=RSA`, `use=sig`, `alg=RS256`, `kid`),
  current key first, then `previous` keys, then pre-published `next` keys.
  `Cache-Control: public, max-age=600`.

Token: RS256, header `kid`. Claims `iss=https://api-cis.lumiguides.it.com`, `aud=booking`,
`sub=<admin_users.id>`, `name` and `preferred_username` (= username; the token carries no email),
`cis_role` (`admin|reviewer|content`), `iat`, `nbf`, `exp` (+15 min), `jti`.
Booking must verify signature via the JWKS (select by `kid`), pin `alg=RS256`, and require `iss`,
`aud=booking` and `exp`. Booking MUST re-fetch the JWKS once when a token's `kid` is not in its
cached copy (rate-limited, e.g. at most once per 60 s), then reject if the `kid` is still unknown.
Booking maps `cis_role` to its own roles (see the mapping above).

Key material: Secrets Manager `BOOKING_JWT_SECRET_NAME` (default
`aa-cis/dev/booking-jwt-signing-key`), JSON `{"kid", "private_key_pem", "previous": [{"kid",
"public_key_pem"}], "next": [{"kid", "public_key_pem"}]}`, cached 10 min in-process and read off
the event loop (bounded 2 s connect / 3 s read timeouts). A missing or malformed secret gives 503 on
both endpoints and never blocks startup; a blank or repeated `kid` in `previous`/`next` counts as
malformed (the last good keys keep serving and an error is logged). Rotation, three steps so no
verifier ever sees an unknown `kid`: (1) add the new public key under `next`, wait ≥ 20 min (CIS key
cache 10 + Booking JWKS cache 10); (2) put the new private key and kid in place, move the old public
key to `previous`, empty `next`; (3) after ≥ 25 min (key cache 10 + token 15) drop the `previous`
entry.
This is separate from the HS256 `JWT_SECRET` used for CIS sessions — Booking never receives that.

### Tenant lifecycle (AA-797)

A tenant (`shared.tenants`) is in one of three states, derived from two columns
(`tenant_status()` in `api/routers/admin.py`):

- **active**: `is_active = true`, `cancelled_at IS NULL`.
- **suspended**: `is_active = false`, `cancelled_at IS NULL`. Reversible through
  `PATCH /admin/tenants/{id} {"is_active": ...}`. The rate-limit middleware returns 403 "Tenant is
  deactivated" to the tenant's JWT. The PATCH clears the 30 s Redis `tenant_meta:{id}` cache, so
  this applies at once.
- **offboarded**: `cancelled_at` set. Reached only through `POST /admin/tenants/{id}/offboard`,
  which needs a reason. That call revokes the key (`api_key_hash = 'REVOKED_' || id`), sets
  `is_active = false`, removes the tenant from the Jev allow-list (snapshotted in the audit row) and
  cancels its QUEUED `shared.job` rows (running jobs finish). It deletes no data or setting.

PATCH and generate-key on an offboarded tenant return 409. `POST /admin/tenants/{id}/restore`
(Nghiệp, 14/10/2026) brings it back whole: it clears the offboard markers, sets `is_active`,
re-adds the snapshotted Jev allow-list row and issues a **new** API key (the old one stays
revoked); plan and settings are unchanged, and cancelled jobs are listed, not re-enqueued. GDPR
data deletion is not automated (follow-up).

Every admin write to a tenant (create, plan, status, key, brand identity, config, offboard,
restore) is admin-role only. The BFF enforces this through `lib/rbac.ts` `API_WRITE_RULES`, and the
backend re-checks `x-admin-role`. Each write leaves one `acp_shared.audit_log` row
(`AdminTenantAuditAction`, actor `admin:{x-admin-user-id}`). `plan_id` (→ `shared.membership_plans`)
is set on create and on every plan change. `GET /admin/tenants` lists only active tenants unless
`include_inactive=true`.

**Tenant 360 (AA-666, AA-766)** — the inline panel on `/admin/tenants` (no separate page). Settings
edits plan / RPM (PATCH) and the planning config (`PUT /admin/tenants/{id}/config`: markets,
channels, posts/week). Admin-only read endpoints in `api/routers/admin_tenant360.py`:
`/cost` (month-to-date `llm_call_log` per stage, $/piece per channel, $/tour for T2, plan revenue
vs LLM cost → margin; same cost definition as billing), `/quality` (T10 gate verdicts per gate from
`content_piece.gate_ledger`, held reasons, tenant edit rate on T2 versions) and `/markets` (SEO
markets from `tenant_seo_config`, `segment_research_log` coverage over the places in the tenant's
tours, planning markets, the tenant's `unmapped_market_requests`). None of it is in a tenant API.

### Tenant DB role (`aa_app_user`, AA-817 / AA-859)

Tenant routes moved off the main pool run on the **tenant pool** (`acquire_scoped_conn`, per-route
Redis flag, fail closed). It logs in as `aa_app_user` (`aa-cis/dev/rds-app-user`) and sets
`app.tenant_id` for the transaction; isolation is RLS, not the query's own `WHERE tenant_id`.

- **RLS template** (229): `ENABLE ROW LEVEL SECURITY` without FORCE (the owner, i.e. the main pool and
  the worker, stays exempt), policy `tenant_isolation` `FOR ALL TO aa_app_user`, USING and WITH CHECK on
  `NULLIF(current_setting('app.tenant_id', true), '')::uuid` (`''` = no rows, never an error). A table
  without a tenant column goes through its parent: `acp_shared.angle_gate_option` matches options of
  `angle_gate_request` rows of the tenant (241).
- **No privilege without RLS** (241): `aa_app_user` holds no privilege, table or column level, on any
  relation without RLS. The one exception is the cross-app catalog contract: `SELECT` on the
  `catalog.v_*` views owned by `catalog_owner` (225 / 228), which other apps read too. A new table the
  tenant pool needs gets RLS and its grants in the same migration. Enforced by
  `NO_PRIVILEGE_WITHOUT_RLS_SQL` (`tests/integration/_rls_fixture_helpers.py`), the same query as 241's
  guard 4 and the AA-859 probe.
- Write grants are column grants where a route writes only some columns (`tenant_integrations`
  connect / test, `angle_gate_option.chosen`), so a tenant cannot move a row to another tenant or
  channel even inside its own rows.
- **Routes on the tenant pool** (flag in `api/core/aa544_tenant_pool.py`, `fail_closed=True`, one log
  line per request with `used_tenant_pool=`). All ON on Dev since S240 (16/10/2026).
  **Flags fail closed (S241 D-12):** only an explicit OFF value puts a route back on the main pool;
  `true`, a missing key, any other value, a Redis error or a read slower than 0.5 s all mean the tenant
  pool (`aa817_tenant_flag_read_failed` at ERROR on a failed read, `aa817_tenant_flag_unrecognized` at
  ERROR on a value that is neither ON nor OFF). The decision is read once per request (memoised on
  `request.state`), so a multi-block route (unpublish, disconnect) never switches pools mid-request.
  Rollback = `SET <key> false`, no deploy. Accepted OFF values (after trim + lower-case): `false` (what
  `aa859_flag.py` writes), `0`, `off`, `no`. `DEL` is not a rollback (it means ON). Confirm any rollback
  with `used_tenant_pool=False` in Logs Insights. **A Redis-only rollback does not survive a Redis node
  reboot / replacement / FLUSHALL** (single node, no persistence: every key is lost, so every route goes
  back ON with no log line). For an incident rollback that must stick, also add the key to the api env
  var `TENANT_POOL_FORCE_ADMIN_FLAGS` (comma-separated flag keys, ECS task definition, read once at
  process start; needs a task-definition deploy): a listed key is OFF whatever Redis says
  (`aa817_tenant_flag_forced_admin` INFO once per key per process; an unknown key logs
  `aa817_tenant_flag_force_admin_unknown_keys` at ERROR). A newly wired route is ON from its first deploy:
  to ship it dark, `SET <key> false` before the deploy, and add its constant to `ALL_TENANT_POOL_FLAGS`
  and to the `aa859_flag.py` ALLOWED set. `acquire_scoped_conn` defaults to `fail_closed=True`.
  The api's Redis client has 0.5 s socket connect / read timeouts and one retry
  (`api/core/redis_client.py`), so a hung Redis fails fast in the rate-limit middleware too.

  | Route(s) | Flag | Notes |
  |---|---|---|
  | GET `/v1/publish-log/pending` | `aa544:stage1:publish_pending_pool` | |
  | GET `/v1/content-writing/pieces/{id}` (+ `/export`) | `aa544:stage5:get_piece_pool` | |
  | GET `/v1/publish-log` | `aa544:publish_log_list_pool` | |
  | DELETE `/v1/integrations/{wordpress,facebook}` | `aa544:integration_disconnect_pool` | two short transactions around the Secrets Manager tombstone; a double click is 200/200 (`aa859_integration_disconnect_raced`) |
  | GET `/v1/content-writing/requests/{id}/latest-piece` | `aa817:content_piece_reads_pool` | |
  | PATCH `/v1/content-writing/pieces/{id}` (W2) | `aa859:piece_edit_pool` | audit after commit on MAIN |
  | POST `/v1/angle-gate/requests/{id}/choose`, `/reopen` (W3) | `aa859:angle_gate_decide_pool` | one transaction, request locked first (same order as the T8 worker); response re-read on MAIN; needs 241 |
  | DELETE `/v1/publish-log/{id}` (W4, unpublish) | `aa859:unpublish_pool` | three short blocks; Secrets Manager and the remote delete hold no connection |
  | GET `/v1/tours/pool`, `/my-versions`, `/versions/{id}` (W5a) | `aa859:t2_version_reads_pool` | job status from a second MAIN query keyed by job ids returned through RLS; `/{id}/full` tenant branch, `/{id}`, `/versions/{id}/export-docx` still on MAIN (D5b) |
  | PATCH `/v1/tours/versions/{id}` (W6) | `aa859:t2_version_write_pool` | `pg_advisory_xact_lock(859, hashtext(tenant:published_tour))` + `MAX + 1`; UNIQUE backstop → 409 `version_conflict` |
  | GET/POST `/v1/integrations/{wordpress,facebook}`, POST `.../test` (W9) | `aa859:integration_connect_pool` | Secrets Manager / remote HTTP hold no connection; the UPSERT writes only 241's column grants; needs 241 |

  A 241 rollback must `SET ... false` the two "needs 241" flags first (and list them in
  `TENANT_POOL_FORCE_ADMIN_FLAGS`), as 241's rollback comment STEP 0 says.
- **MAIN exceptions** (code comment `# AA-859 MAIN exception: <reason>`): `write_audit_log`
  (`acp_shared.audit_log` has no RLS and is never granted; written after the tenant transaction
  commits, best-effort on the tenant path); enqueue-in-transaction routes (angle-gate goal, T2 rewrite /
  retry, content-writing write, slate refresh); `shared.job` status lookups (no tenant column); tenant
  self-config and usage reads (`/v1/quota`, `/v1/billing`, `shared.tenants` limits) keyed on the JWT
  `sub`; the angle-gate `fetch_request` re-read (subject / segment / route have no grant until wave 4);
  staff branches.
- **`/admin/atoms*` is staff only** (AA-859, S240): the AA-431 tenant-JWT branch was removed with no flag
  after 30 days of WAF logs showed 0 Bearer callers (1,041 calls, all `X-Admin-Secret`). A Bearer token
  gets 403 and logs `aa859_admin_atoms_tenant_refused`. No tenant route writes `acp_contract.tour_atoms`.
- **Sizing:** tenant pool `max_size` 2 per api task, api autoscaling max 7 (AA-CIS-Infra #156), so 14 ≤
  `aa_app_user` CONNECTION LIMIT 15. `aa859_tenant_pool_timing` (wait/hold ms) and the 60 s
  `aa859_main_pool_gauge` line are the inputs for any later resize. Per-role connections are alarmed
  (`AA/RDS RoleConnectionLimitPct`, AA-832, published by the api's ops metrics sampler, AA-751).

## Boundaries — what this repo does NOT do

- It does not provision AWS resources or OIDC roles (that is AA-CIS-Infra).
- It does not run the B2C map trip planner (that is AA-TripPlanner-Web).
- **Only CIS writes content** (root ADR 0002, 01/10/2026): tours, atoms, places
  (`shared.destinations`), itinerary places / tour graph, photos. Other apps read views and write
  only their own schema (+ `shared.llm_call_log` telemetry); the `tripplanner` DB role is read-only on
  `shared.destinations` (migration 201). The place pipeline (extraction, geocoding, tour graph) still
  lives in the TripPlanner, frozen until it moves to CIS (AA-712).

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
`docs/adr/0001-*.md`, `docs/adr/0003-*.md`. **A Segment never spans two countries** (tour country,
`raw_tours.country`): AA-695 vetoes a cross-country pair, and AA-857 holds the rule for the whole
connected group (an unknown-country atom or pseudo-Segment can no longer bridge two countries).
A `segment_id` is the mint of the moment that FIRST created it, not a promise about its current
label: an A3 run relabels only ids it claimed (its atoms' prior Segments + its pool) or ids with no
member left, re-mints a fresh id that collides with another country's Segment or an alias old id
(`sha256(id|country|aa857|n)`), and rolls back if a written id would gain a second country among
its live atoms (AA-857). Subjects stored on an id that later merged resolve through
`atom_segment_alias` to the live id (an id that still has members is live, never followed).
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
many tours contain it), Questions (PAA questions that land on it by embedding match — atom and
question embeddings are fetched in one batched pre-pass, up to 96 texts per Cohere call, and
cached in `atom_embedding` / `question_embedding`, AA-688) and Said
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
   - _brand-fit_: the same brand-fit scorer (judge route) the T2 judge uses
     (`services/content_generation/brand_fit.py`), cached per
     `(tenant, candidate, brand_version)` in `acp_shared.debate_brand_fit_cache`.
   - Safety rules: never cuts more than 50% of a list; an error on a candidate lets it pass; if
     Debate fails entirely the Slate behaves as before.
3. Bar check per Channel, only for the tenant's enabled Channels (`acp_shared.tenant_config.channels`,
   set in admin Tenant 360 > Planning config; no row = every Channel), then the place rule
   (AA-840, replaced AA-632's one-place-per-Slate set): Channels take turns, strongest candidate
   first; a place appears at most once per Channel and on at most `MAX_CHANNELS_PER_PLACE` (3)
   Channels of one Slate. A Channel the tenant switches off loses its `proposed` rows on the next
   proposal and they are hidden from `GET /v1/slate` at once (picked/used rows stay).

A Segment Subject is offered and written only from its **seed atom** (AA-857,
`services/acp_shared/subject_seed.py`): a live member on the tenant's own tours whose place IS the
Subject's place (normalised, alias / place-card aware; graded so an exact name beats a member
that only names the place in its parenthetical; a word-set subset/superset — the relation A3 groups
by, since `canonical_place` is the shortest wording — is the last-resort tier, non-generic names in
one known country only), in one country — never the first member by `atom_id`. A Segment with no such member is not offered (log `slate_subject_skipped`) and cannot
be picked.
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
the severity first. S233: blog shape gates F3/F5/F7 are `warn` (F7 auto-drops FAQ pairs that
restate the body); new blocking gates F0_complete (cut-off text), F2_brand_forbidden (the
tenant's own forbidden words) and F11_brand_name (another brand / the platform speaking); an F9
judge outage is a warn, not a hold. Count gate failures from `gate_ledger`, never `held_reason`
(it names only the first blocking gate). `services/acp_shared/grounding.py` is shared by T3 and T9 and reads numbers with units ("4,460m") correctly (AA-639).
S235 (AA-785): T9/T10 F1 (`quality_gates.gate_grounding`) calls
`find_novel_numeric_claims(..., year_forms=True)`. In that mode a year range ("1637–38") or a decade
("the late 1630s") is read as years, and it passes when the seed has the year: for a range, the full
end year, or the start year when the end is start + 1; for a decade, any seed year inside it.
Otherwise F1 reports the full year or decade ('1652', '1880s'). T3, A1 and `source_facts` keep the
default strict digit-run reading. F1 stays blocking. (AA-776: an F4_seo_surface-only ledger is fixed
by the SEO title/meta fit (`repair_mode: seo_fields`), never by a full rewrite, unless the F4
violation is about the slug.)
S234 (AA-768): `services/acp_content_writing/hygiene.py` owns the deterministic text rules (scope
lexicon + out-of-scope gazetteer from the TRIP name and "other trips" lines, `LEAK_RE`,
`SOCIAL_PROOF_RE`, repeats, hashtag spelling). It fixes what is safe in place before the gates
(logged as the `F13_autofix` flag) and two gates report the rest: **F12_scope** (a place outside
the SCOPE in title/meta/headings/body; warn for one round, `F12_SCOPE_BLOCKING`) and
**F13_hygiene** (warn). Blogs also drop FAQ pairs the seed cannot answer (age, fitness, price…),
logged under `F7_faq_autofix`.
S234 (AA-771): LinkedIn has a **channel spec** by brand register (`services/acp_shared/channel_spec.py`:
consumer_social / editorial / warm_practical / observational, from the voice adjectives then brand_type) —
word band 120–250 (editorial 300), ≤3 hashtags, first line ≤140 chars; hygiene.autofix applies the hashtag cap
and first-line split. **F14_social_shape** (LinkedIn shape; warn, blocks only above 1.3× the band → full
rewrite), **F15_voice** (stock-phrase hard list; blocks on attempt 1 only, for one sentence repair, and
never holds a piece — `hold_failure` skips it) and **F15_voice_warn** (trade register, audience-number leak,
generic or repeated opener; warn only, never repaired). All pass for every other channel. The F9 judge
gets the brand's voice adjectives and no longer favours a calm house voice.
S234 round 2 (AA-768): **F13_leak** (blocking, sentence-repairable) — writer-instruction wording ("A Sentence
on…", "This guide covers…", the SCOPE's day span), the brand pack's audience ranges ("25–40", "seven-to-sixteen")
and a piece opening on a pronoun with no referent; hygiene.autofix trims headings and deletes such a sentence
only when no pronoun is orphaned. A part-of-trip piece's writer gets the **scoped trip name** (no duration, no
out-of-scope places); the whole trip's length and "the journey concludes" are F12_scope hits. **F15_voice**
also counts contrast frames (>1 per 150 words) and lines shared with up to 5 earlier pieces of the same request,
on every channel; a style-only repair runs on `t9_repair`. Contract gates last in the ledger, owned by one-call
fix steps (`t9_repair`) and never sent to a rewrite: **F16_faq** (blog ends with "## FAQ" and ≥3 pairs after
the droppers; one FAQ regeneration per writer text) and **F14_first_line** (LinkedIn hook still over the
spec after the split and one first-sentence rewrite). Every sentence repair, FAQ regeneration and first-line
rewrite is reverted when it leaves a new repeat or an orphaned pronoun (`sentence_repair.splice_checked`). An
approved piece's leftover style-only failure is stored `blocking: false, waived: true` in `gate_ledger`.
S234 round 3 (AA-768): `services/acp_content_writing/trip_facts.py` owns the trip-fact rules. **F13_repeat**
(blocking, sentence-repairable) — a figure told twice, an echoed phrase, an orphan line, a moment repeated under
another moment's heading (autofix deletes the safe ones first; every repair is integrity-checked for the same and
deletes the new later duplicate before it reverts). **F17_trip_fact** — an optional activity written as fixed, a
time / ordinal / position the seed lacks, a family brand (audience with children) pitching a moment without the
seed's access caveat, a role the seed dates as ended written in the present or a wrong span; **F18_rarity** —
rarity / uniqueness claims and quantifiers stronger than the seed's. Both blocking and sentence-repairable; a
title/meta-only finding is a warning (the SEO fit refuses such a meta). The full trip's places outside the scoped
days (`v_active_tour_atoms`) join the out-of-scope gazetteer. F15_voice adds cross-round stock lines and brand-pack /
angle-brief phrases on every channel. The configured CTA line is restored word for word after autofix. When the
only blocking failures are F15/F13_repeat/F17/F18 the repair runs on `t9_repair`; an attempt-3 repair call that
fails cuts the flagged sentence in code instead of holding.
S234 round 4 (AA-768): `services/acp_content_writing/text_integrity.py` owns the **final pass** — the last step
before the gates read an attempt AND before the piece is stored (after every repair, revert and the tag strip):
whitespace normalised, exactly one CTA (a reworded copy next to the literal line is cut), later duplicates cut
(paraphrased ones too), lower-case fragments cut; what it cannot fix is `hygiene.find_issues()["repeat"]` —
the same list F13_repeat reads — and holds the piece (fail closed). A far repeat blocks only when it is a
real one (`hygiene.strict_repeat`: a copy, Jaccard ≥ 0.5, or nothing new); two shared trigrams alone (a place
phrase, the keyword) stay an F13_hygiene warning. The CTA paraphrase search covers only the closing sentences
next to the literal line (never the hook), and the literal line is matched across sentence breaks like
`has_cta_literal`. The admin sentence apply path returns what an edit newly breaks as an F13_repeat warning. Before it, `trip_facts.fix_trip_facts`
folds a "Thin moment"'s own heading into the section before and cuts an interpretation appended to its closing
mention, deletes a family-brand reassurance that plays down a seed hazard, and removes a background-only place
made the reader's gateway / base (FAQ included); the rest is F17. A trip place the seed's place background names
is not out of scope. The blog SEO title may not repeat a title of the tenant's last 50 blog pieces (F4); only the
blog gets an SEO title/meta (the fit no longer runs for other channels).
S235 (AA-786) — **F9 v1.1** (`F9_PROMPT_VERSION = "f9_v1.1"`): the judge reads the piece without
citation tags (F1/F5 keep them) and, for blogs, the search title and meta description. New
product-truth code **TEXT_INTEGRITY** quotes each broken sentence and goes to sentence repair, not a
full rewrite. A non-pass F9 verdict with no checkable reason is a warn, never a hold. A claim that is
not in the body at all but is in the title/meta is a `title …`/`meta …` warn owned by the SEO fit. On
LinkedIn, at the last attempt, the sentences behind an F9 product-truth failure are cut (never the
hook or the CTA); F9 is waived only when every flagged sentence was cut and the post stays in its
length band, otherwise it stays blocking and the piece is held for the admin (the tenant still
receives it, AA-770). A CTA failure still holds.
S239 (AA-777) — **F9 sticky pass**: one set per write job (`f9_passed_sentences`, passed to
`run_quality_gates`) keeps the body sentences an F9 verdict of that piece passed; a later verdict that
flags one of them while it is unchanged has that claim dropped (`F9_sticky_pass` log, `sticky_pass` on the
F9 entry). Only a verdict with a checkable reason adds to the set; an outage adds nothing. **Blog truth
cut**: on a blog's attempt 3, when attempt 2 was a sentence repair of a failing F1/F9 gate, the F1/F9
items still failing are cut in code (`sentence_repair.delete_flagged` + `tidy_after_cut`) instead of a
second repair call; other items still go to the repair, and the gates re-judge the cut text. A cut that
would take the blog under `_BLOG_MIN_CHARS` (or break its FAQ / ending) is refused: the old repair path
runs and the piece stays held if it fails. The S234 attempt-3 fallback cut obeys the same floor on blogs.
S239 r6 follow-up — a sentence-repair replacement that ADDS a number, time / ordinal / position or (blog)
pace word the sentence and the source lack is not spliced (`sentence_repair.new_specifics`, the F1/F17
functions themselves); `plan()` merges two findings on one sentence (truth gate first) instead of dropping
the second; the sticky key carries the previous sentence only for a referent-led sentence ('It…', 'These…',
'There…', 'Then…', 'But it…', 'The <noun>…') and a multi-sentence judge quote is sticky when each sentence is;
the blog attempt-3 cut also takes F17 'cut it' items once F17 was repaired before. A sentence whose finding
says to keep it (played-down hazard 'keep the hazard', family caveat 'add ONE') is never cut, merged or not,
and an empty repair of a hazard sentence is not spliced; a multi-sentence cut checks every sentence in it
(hook, CTA, FAQ question) and the sentence after it (orphaned pronoun).
_Avoid_: check, validator, rule (Gate is the specific automated-check object; "rule" is generic).

**Piece**:
One row of generated content for one (Subject, Goal, Angle) — carries `attempt_number` (max 3;
attempt 3 is a sentence-level repair only, on the same row — the AA-485 buffer retry row is gone),
`gate_ledger` (every Gate's result on the persisted attempt), `status`
(`approved`/`held`/`failed`), and (blog only) a `publish_log` row once T11 ships it.
Each failed attempt's `repair_log` entry also keeps that attempt's own trimmed ledger
(`repair_log[].gate_ledger`: gate, passed, blocking, at most 5 violations; AA-791,
`gate_eval.ledger_snapshot`), so gate precision and recall can be measured across attempts; pieces
written before AA-791 have only the persisted attempt's ledger. A **Label** is an admin's human
verdict on one Piece (`acp_shared.content_piece_label`, migration 231, one row per Piece and
labeller: `as_is`/`light`/`heavy`/`reject`, per-class defects, minutes to fix, reason, and the
sha256 of the text labelled, so a later admin edit shows as changed). It is the ground truth gates
are re-scored against (AA-783 criteria); admin-only, set from the Content Quality drawer, no tenant
column and no `aa_app_user` grant.
`held` is an **admin-only quality label** (AA-770, S233): the tenant always receives the content
and can export and publish a held Piece like an approved one. **Tenants never see internal gate
data**: the tenant-safe projection (`_tenant_safe_piece`) drops `status`, `held_reason`,
`gate_ledger`, `repair_log` and `flags`, exposing only `ready_state` (`ready` / `not_ready`, where
`not_ready` means there is no content yet). The same rule covers T2 tour versions (see **Tenant
projection** below). `writer_input` (migration 220) stores what the writer was given. Admins
see everything on **Content Quality** (`/admin/content-quality`, AA-769), plus Content Trace and
Platform Stats. An **admin** (staff role `admin` only) can cut or rewrite one flagged sentence of
an approved/held Piece from that drawer (S234): *suggest* is one cheap LLM call (stage
`t9_repair`, logged with `content_piece_id`, nothing saved); *apply* is deterministic (span-text
match under `FOR UPDATE`, the replacement re-checked by F1/F2/brand-forbidden/F11 and — with the
stored `writer_input` — F12_scope/F13_hygiene as warnings only, a near-verbatim neighbour blocks;
splice + tidy, using the
same sentence primitives as the T9 hygiene in `hygiene.py`)
and writes `content_text` plus an `acp_shared.audit_log` row
(`content_piece.admin_sentence_cut|_rewritten|_edited`, before/after) — the audit log is the
Piece's edit history. `gate_ledger`/`flags` stay as the writer produced them, and a published
post is not re-synced. The replacement is cleaned like pipeline output (no citation tag, no
markdown on plain-text channels) before it is checked and saved. An admin can also regenerate a
request's 3 T8 angles (`POST /admin/content-quality/requests/{id}/regenerate-angles`): it clones
the request (same tenant, atom, subject, route, channel, CTA, goal) and runs the `t8_angles` job on
the clone, so the old request, its option rows and its pieces' angle labels never change.
_Avoid_: post, draft, content (all used loosely elsewhere; Piece is the specific DB row).

**Tenant projection (AA-770, AA-796)**:
Every tenant-JWT read of tours and tour versions goes through `api/routers/_tenant_projection.py`,
the single tenant rule:
- Stored status: T2 still stores `status='needs_review'` when the score is < 7.0 (or the T3 QA gate
  escalated). That stored value is the admin signal (admins read it in `/admin/content-quality` and
  in the tenant rewrite activity). Tenant reads map it to `ai_generated` ("Ready to review");
  Marketplace shows it as `ready`.
- Hidden fields: no tenant API returns `quality_score`, `quality_score_id`, `qa_*`, judge or writer
  output (`generated_content`, `quality_scores`), the A1 review state of `raw_tours`, or LLM/Jev cost
  (`/v1/billing` dropped `llm_cost_usd`, reversing AA-636).
- Pool: newest-first only, no quality sort or filter.
- Admin path: `/v1/tours/{id}/full` keeps the full before/after payload only for the X-Admin-Secret
  path.
- Test: `tests/unit/test_aa796_tenant_projection.py` key-walks the tenant routes.
- Silent correction (AA-802): an **admin** can cut / rewrite / edit one sentence of a T2 version
  from the Content Quality tour-versions drawer (`/admin/content-quality/tour-versions/{id}/sentences/suggest|apply`).
  This works only for finished AI text the tenant has not decided on (`ai_generated` / `needs_review`),
  only on their latest version, and never on a `tenant_edit` version. The row is updated **in place**
  (`rewritten_content` only, by `id AND tenant_id`; 0 rows → 409). There is no new version, no quota use
  and no tenant-visible mark. The history is the `audit_log` row
  (`tenant_tour_version.admin_sentence_*`, sentence and whole section before/after). Both drawers
  (T9 piece, T2 version) warn the admin when the tenant took a copy away (published, or exported;
  the T2 DOCX export writes `tenant_tour_version.exported`) before a later admin edit. Nothing
  re-syncs that copy.

**Channel**:
One of 8 values: `blog`, `linkedin`, `facebook`, `instagram`, `tiktok`, `email`,
`landing_page`, `ads`. Each has its own Slate Bar and Gate count (blog ≠ the other 7).
_Avoid_: platform, network (Channel is this project's own term for the publishing surface).

**Facts Entry (tenant scope)**:
One hand-written, sourced claim a tenant writes for themselves only (their own pricing,
cancel/rebook terms, deals) — visible only to that tenant, never to others. Same table as the
platform-scope entry above; `tenant_id` is required here, NULL there (enforced by a DB CHECK).
_Avoid_: Fact (bare) — always say which scope.

### Model operations (platform)

**Stage**:
One named place in the code that calls a model for one purpose (`t9_write`, `s1_judge`,
`t5_atomize`...). Admin picks the model per Stage; every call is logged under its Stage.
_Avoid_: step, node, role (Role is a separate property: writer / judge / validate).

**Model Key**:
The short, stable name the platform uses for one model (`haiku`, `sonnet-5`, `gpt-5.6-luna`),
independent of vendor ids, inference-profile prefixes or which AWS account serves it.
_Avoid_: model id, tier (the old name for the same thing).

**Model Catalog**:
The single list of models the platform can call, with each Model Key's vendor, how it is called,
which accounts serve it, its price, and whether it is enabled. The admin dropdowns and every cost
figure come from it. See ADR 0005.
_Avoid_: model list, pricing table.

**Stage Route**:
The ordered list of Model Keys a Stage tries (primary first, then fallbacks), plus an optional
shadow model that runs alongside for comparison and never changes the result. See ADR 0006.
_Avoid_: fallback chain (the old hardcoded Haiku/Sonnet chain), routing.

### Jev decisions (platform)

**Jev Question**:
One fixed, typed question a Stage asks Jev (TypeSafe) about a subject: yes/no (noul), pick one
label (choice), or a level on a scale (score). Its wording is data, versioned, and it carries its
own Mode and Floors. See ADR 0007.
_Avoid_: rule, check, gate (a Gate is a T10 check), judge (a Judge writes feedback; Jev never writes).

**Verdict**:
Jev's answer to one Jev Question about one subject: a probability (or a pick and its confidence)
and the Zone it falls in. Every Verdict is kept, whatever its Zone.
_Avoid_: answer, judgment, decision.

**Zone**:
Where a Verdict falls: accept (confident yes), reject (confident no), grey (not confident),
error (Jev could not answer), skipped (not asked). Only accept and reject can change what a Stage
does; grey and error keep the Stage's existing rule.
_Avoid_: result, outcome.

**Floor**:
The two probability limits of a Jev Question — accept floor (≥ means confident yes) and reject
ceiling (≤ means confident no). Set from a Calibration Record, placed on the side of the costlier
mistake.
_Avoid_: threshold, cut-off.

**Mode**:
Whether a Jev Question is asked and used: off (not asked), shadow (asked and kept, never acted on),
enforce (acted on when the Verdict is in accept or reject).
_Avoid_: status, enabled.

**Calibration Record**:
The written evidence for a Jev Question's Floors: a labelled sample of real subjects, reviewed by
a person, with the precision at each Floor. A Jev Question cannot be set to enforce without one.
_Avoid_: tuning, eval.

**Act (on a Verdict)**:
What an enforced, confident Verdict may change: exclude something from a purchase (DataForSEO) or
from a count (Score, questions landed), or block or send content back for repair before it is
published. It never deletes data and never unpublishes published content.
_Avoid_: block (alone), delete, filter.

## Pipeline stages

### A-series (Admin, master content — computed once, shared by every tenant)

- **A0 — Upload**: raw tour ingestion (`raw_tours`, `pipeline_status='ingested'`).
- **A0 note**: tours with an empty itinerary are skipped at upload and flagged "No Itinerary"
  (AA-604). As of the 16/09 data reset: 793 raw tours kept, 30 empty-itinerary rows trashed,
  763 ready for S1.
- **A1 — Generic Rewrite (S1)**: the admin S1 pipeline rewrites A0's raw content into
  `generated_content` with a neutral, brand-agnostic voice (see AA-535) — the shared base every
  tenant later re-voices for their own brand. Flow: generate → validate → judge →
  (retry ≤3 or HITL) → brand_audit → **grounding** → flag_fix → revalidate. Can run per tour or,
  once AWS enables it, as a Bedrock Batch job (see Repo Context).
- **A1 grounding** (AA-691, `services/content_generation/grounding.py`): every sentence of
  subtitle / summary / highlights / itinerary bodies is checked against the **raw source**. A
  number the source never states (after reading travel-time conversions, clock formats and glued
  figures) is `UNSUPPORTED_NUMBER`; the Jev Question `a1_claim_supported` can clear it (accept
  side) and, once calibrated on its reject side, raise `UNSUPPORTED_CLAIM`. Violations are
  rewritten sentence by sentence against the source (one `s1_flag_fix` call); what is still
  unsupported after revalidate → `manual_check` (A2). Master content is the one place this check
  exists — T3 grounds tenant rewrites against master content, not the raw source.
- **A2 — Admin QA Gate**: A1 rows that failed auto-validation (`status='hitl'`), reviewed via the
  Review Queue before they can reach A3. A row can also be **dismissed** (hidden without
  approving; a future real failure is enqueued again — AA-626, `review_status_enum` value
  `dismissed`).
- **A3 — Master Content Pool**: `gold_aa_internal.published_tours` — a tour is "in Master
  Content" once it lands here. Atomize (see Atom above) now fires automatically right after a
  tour is published here (AA-526, `services/export/handler.py::process_export()`), as a
  fire-and-forget background task, `owner_scope='platform'`.
  - The admin **Master Content** page reads `GET /admin/tenants/{aa_internal}/details`
    (`admin.py::get_tenant_details`, internal branch). AA-718 added server-side
    `page`/`page_size`/`sort`/`sort_dir`/`country`/`master_status`/`score`/`search` to it (the
    tour list was a client-side filter over a fixed `LIMIT 200`, which dropped the newest rows
    past 200). The summary now carries a filter-aware `catalog_total` + `avg_quality` (true
    `COUNT`/`AVG` over the same WHERE), a 30-day-windowed `llm_cost_window_usd` (reconciles with
    External Spend) alongside the all-time figure, and a `pagination` block. Each pipeline run
    gets a read-only derived `display_status` (`_derive_run_display_status`): a stored
    `ingesting` run that is actually finished shows **completed**, an ingest-only batch shows
    **ingested**, a partial one shows **running** — the stored column is never written from this
    GET. One-off backfill (S211) recomputed `pipeline_runs.tours_passed` from the real published
    count (26 rows were stale); it did not touch any run `status` (15 of 17 `ingesting` runs are
    genuinely not-yet-run, 751 tours still at `pipeline_status='ingested'`). The tenant (B2B)
    branch of the endpoint is unchanged.
- **A4 — Cross-Tenant Oversight**: admin-side supervision OVER tenant-published content — Trust
  Ramp (graduated autonomy per tenant) and the force-unpublish safety net (`admin_a4.py`). Not
  part of the A0→A3 production line; it watches T-series output instead.

### T-series, old (Tenant rewrite tour, T0-T4)

- **T0 — Brand Identity**: tenant sets up their own brand voice/rules (`/portal/t0-brand`),
  incl. a Default Call to Action (`tenant_brand_rules.default_cta`, migration 220). The fields
  become the **brand pack** (`services/acp_shared/brand_pack.py`, AA-771) read by T8, T9 and the
  T10 judge — compiled deterministically at read time; a hand-written `system_prompt` still heads
  the judge rubric, otherwise the rubric is compiled from the T0 fields (no Adventure Asia
  fallback for tenants).
  - **CTA review claims (AA-819 follow-up).** The tenant's CTA is published word for word (F19
    exempts it). When a tenant saves one that claims reviews or ratings (T0 default CTA, or the CTA
    typed for one T9 write), the save still happens, the response carries `cta_warning`, the portal
    shows a plain warning (live via `POST /v1/content-writing/cta-check`), and AA admins get one
    bell row per tenant + CTA text (`tenant.cta.review_claim`). Rule:
    `authenticity.cta_review_claims()`; writer: `services/acp_shared/cta_review_notice.py`.
- **T1 — Tour Selection / Rewrite trigger**: tenant picks an A3 (Master Content) tour and fires
  the rewrite (`/portal/t1-rewrite`, `PoolTab.tsx`'s "Rewrite" button).
- **T2 — Rewrite (execution)**: the LLM rewrite of the picked tour into the tenant's own brand
  voice. It has no dedicated UI route and runs as the durable `t2_rewrite` job on the worker (AA-652).
  - **Writer prompt (AA-792).** T2 has its own writer prompt: `prompts.T2_SYSTEM_PROMPT` plus a
    CLIENT BRAND block (`brand_pack.render_brand_block`), assembled by
    `batch_prompt.build_t2_system_prompt`. These builders are chosen only when `is_tenant_rewrite`.
    The user prompt uses `build_rewrite_prompt(voice="tenant")`. The tenant's core idea
    (`tenant_brand_rules.core_idea`) is the lens for the overview and each day's framing.
  - **Shared rules.** T2 keeps A1's truth and structure rules as the same prompt pieces: core
    forbidden words, structure, source fidelity and the meta band. It drops A1's neutral catalogue
    voice. The A1 prompts are pinned byte-for-byte by `tests/unit/test_aa792_a1_prompt_golden.py`.
  - **Brand loading.** The brand is loaded once, through `brand_pack.fetch_brand_pack_conn` (AA-771).
  - **Itinerary nudge.** The length nudge keeps the draft's tenant voice. T2 flag-fix repairs speak as
    the tenant's editorial fixer.
  - **Writer stage.** `t2_generate` (Sonnet).
  - **Judge.** `t2_judge`. The gate is chosen by env `T2_JUDGE_GATE` (`brand_fit_mission` default,
    `brand_fit`, `combined`; `services/content_generation/judge_node.py`). Default: judge_score =
    brand_fit, capped at 6.0 when the tenant's own mission (core idea) is absent from the draft; a
    tenant without a core idea is never mission-capped. Cross-brand distinctness is logged in
    `llm_call_log.quality_signal` and does not gate.
  - **Judge repeats (AA-801).** Env `T2_JUDGE_REPEAT`: `single` (default) = one call, plus the AA-714
    median of 3 only when the first brand_fit lands in 6–8; `median3` = three parallel calls on every
    draft, median brand_fit/distinct and majority mission (a tie counts as absent), raw calls logged in
    `quality_signal.judge_runs`. `median3` stays off until the AA-801 measurement is in. A call that
    was billed but then failed (malformed JSON) is dropped from the verdict and its cost is still
    summed into the logged `cost_usd` (`judge_runs.failed_cost_usd`); when all three fail nothing is
    logged, as with a single failed call. Each call samples the stage-route shadow on its own, so a
    route with a shadow sees up to 3 shadow calls per judgement in `median3`.
  - **Rubric anchors (AA-801 option b).** Env `T2_JUDGE_ANCHORS` (`on`/`1`/`true`; default off) adds
    tenant-specific score anchors (what a 4 / 6 / 7 / 8 / 10 looks like, built deterministically from the
    T0 brand pack: name, core idea, audience, voice, on-brand example, forbidden words) and turns
    mission_present into a yes/no test that only counts with a verbatim itinerary quote
    (`mission_evidence`, >= 4 words), checked in code against the itinerary digest the judge saw — a
    paraphrased or invented quote overrules the yes (`evidence_verified=false`); both are logged in
    `quality_signal` when on. The yes bar is the overview + at least half of the days reflecting the
    core idea (the brand_fit 7 anchor's threshold, so an anchored 7 is not mission-capped; the
    un-anchored test keeps "most days"); the quote only has to show the idea in one day, and is asked
    for before the verdict. `evidence_verified` only proves the quote exists in the digest (it catches
    invented quotes), not that it reflects the idea. Combines with `median3`; A1 is untouched.
    Parsing for every judge caller (A1 s1_judge, T2, Debate, flag on or off): a boolean answer given
    as a string counts as yes only for true/yes/y/1 — "false"/"no"/"partially" are no; JSON bools and
    a missing key (default yes) are unchanged.
    The judge model itself is only changed through the `t2_judge` stage route; `score_brand_fit`'s
    `model_override` is refused on any stage but `adhoc_*` (measurement scripts).
  - **Best draft wins (AA-793).** Env `T2_BEST_DRAFT` (default on): a tenant rewrite runs the graph
    variant `build_graph(t2_drafts=True)`, which keeps the highest-scoring judged draft
    (`t2_repair.track_best_node`) and saves it at the `hitl` exit instead of the last one. Env
    `T2_TARGETED_REPAIR` (default on): a retry revises that best draft with its own feedback
    ("YOUR PREVIOUS DRAFT (revise it…)") instead of a blind re-roll. Both off = the old graph. A1
    never takes this variant. Judge rows log `quality_signal.attempt` / `revision`.
  - **Brand-audit fallback.** When the judge did not run, the audit uses the tenant's own rubric
    (`compile_rubric`), never Adventure Asia's identity.
  - **Stored status.** A version scoring < 7.0 (or escalated by T3) is stored `needs_review` — an
    admin signal only; tenants see it through the tenant projection (see **Tenant projection**).
  - **Live view and billing.** The tenant watches the rewrite live (streamed text, AA-637). Each
    rewrite counts against the plan quota; over-quota rewrites are billed (AA-640).
- **T3 — QA Gate**: an automatic validate→repair loop (max 2 repairs) on T2's output. There is no
  manual tenant approval step: `status='approved'` is T3's own automatic result, not a button.
  - **Blocking and repair (AA-639).** T3 blocks only on `_HARD_BLOCK_CODES` and passes the specific
    failure feedback into the repair.
  - **Deterministic fixes.** An over-long SEO title is fixed with `fit_seo_title()` instead of a tour
    rewrite. Since AA-794, seo_meta gets the same deterministic fit as the graph
    (`fit_seo_meta_final`) before a length code can trigger a rewrite. That fit tries a clause cut,
    then a word cut only before a phrase boundary, then a fact clause. The fact clause never adds an
    "in <country>" tail on a multi-country tour.
  - **Best draft and targeted repair (AA-793).** Every checked draft is a candidate: a clean one
    returns at once; when rounds run out the best candidate (clean, then score) is saved, not the
    last, and each round repairs the best so far. With `T2_TARGETED_REPAIR` a grounding-only failure
    gets one sentence repair (`grounding.repair`, stage `t2_flag_fix`, Haiku) instead of a rewrite;
    a structural failure gets a revision of the draft with the T3 findings plus the judge feedback.
    `job.result.t3` records the chosen attempt and every draft's score.
  - **Background facts (AA-793).** Env `T3_BACKGROUND_FACTS` (default off): the numeric check
    accepts place background (`acp_produce/t3_background.py`): past years, centuries and elevations
    >= 1,000 m of a named place, never in a first-person, accommodation/service or climb/gain
    sentence; durations, times, distances, prices and counts stay violations (writer source
    fidelity rule A/B). A bare 4-digit year needs a year cue before it and never a comma, plural
    noun or currency after it; a metre figure next to a walking / route word is a distance.
  - **Cost.** A typical long tour takes 1-2 writer calls, about $0.10-0.18 (previously 4 calls, about
    $0.31).
  - **Jev clearing (AA-699).** For Jev allow-listed tenants, each numeric hit is first checked with
    `a1_claim_supported` against the master content (stage `t3_grounding`). A confident "supported"
    verdict clears the hit without a rewrite.
  - **T2 judge tie-break.** Near the retry line it asks `a1_brand_fit` (stage `t2_judge_tiebreak`,
    shadow).
  - **Other tenants.** `skipped`; T3 is unchanged.
- **T4 — Pool**: the tenant's own rewritten-tours pool (`/portal/t4-pool`) — a T3-approved tour
  here is what a tenant can build T7+ content from.

### T-series, new (per-tenant social content, ported from Ms. Thư's `aa-social-media`, T5-T11)

- **T5 — Atomize**: **historical label, superseded.** Originally a tenant-triggered, per-tenant
  atomize step; AA-526 (05/09/2026) moved atom generation to A3 (platform-wide, admin side, see
  above) and removed the tenant-triggered endpoint entirely. Do not build anything new against
  "T5" as a tenant-facing stage.

> **⚠ Naming trap — atomize is A3 platform only (Nghiệp, S224).** A tour is atomized **once, when
> it is published to Master** (`a3_atomize` job from `process_export`), or when an admin re-runs it
> (`POST /admin/atoms/atomize`). Tenants **never** atomize: a tenant rewrites a tour (T2/T3) and then
> uses the **platform** atoms, Segments, routes and hubs of that tour to plan topics and angles and
> write social content. Adding or removing a tour only **recomputes** Segment/Score/Route/Hub — it
> does not re-atomize. Names that still say otherwise are leftovers from before AA-526:
> `services/acp_produce/tenant_pipeline.py::run_t5_atomize(tenant_id, …)` (only ever called with
> `"platform"`), the LLM stage key `t5_atomize`, and the `tenant_tour_version_id` column of
> `atomize_day_fingerprint` (holds `generated_content.id`). Verify a pipeline claim by grepping the
> real callers, not by reading names (S224 lesson). Rename tracked on AA-757.
- **T6 — Atom Curation**: **historical label, superseded.** Was a tenant-facing atom star/delete
  UI (`/portal/t6-atoms`); removed at AA-527, replaced by the admin-only `/admin/atom-curation`
  page (platform-scope atoms only — this is exactly the page AA-527 built into the wrong role
  the first time, which is why this glossary exists).
- **T7 — Slate** (`/portal/t7-planning`, titled "Social Content"): the tenant's ranked,
  per-Channel list of Subjects to pick from, read from the platform-wide Score/Route data (see
  Slate above). The older slot-grid UI on this page was removed (AA-519). Debate's brand-fit cut
  asks the Jev Question `t7_topic_fits_brand` before the Luna call on a cache miss (AA-700, shadow).
- **T8 — Angle Gate**: Goal + Angle selection (`/portal/t8-angle-gate`, one continuous wizard
  covering T8 and T9 together — no separate T9 route). Each PAA answer an Angle claims is checked
  with `t8_angle_answers` before the ranking (AA-700, shadow). S234 (AA-771): an Angle's
  `best_final_style` is shape + which seed details to lead with — never a voice, an opening line or an
  audience other than the brand's; `sanitize_best_final_style` fixes those in code ('Open with X' →
  'Lead with X', a literal first line or a trade-facing sentence is cut) before the Angle
  is saved (and again on stored Angles when a non-blog T9 prompt is built). A Route request writes its
  Angles from a digest of the whole walk (day order, fact counts, ANCHOR CANDIDATES / THIN MOMENTS); an
  Angle anchored on a thin moment ranks last, and the call is retried only when all 3 are thin.
- **T9 — Write**: Piece generation, fires automatically the instant an Angle is chosen; the
  text streams live to the tenant (AA-637). The seed (`atom_seed.py`) carries the atom's
  **Place Card** (AA-772: display name, locality/region/country, naming note, checked background
  facts — `acp_shared.place_card`, drafted by `a3_place_card` + checked by `a3_place_card_check` in the
  T8 job (request atom) and the `place_cards_build` job — T9 only reads cards (S234). One draft per
  place: free rule skip (unnamed / logistics places), atoms grouped by folded place + country, reuse
  through `place_card_alias` / core name, and misses cached in `place_card_miss`; a generic name such
  as "City Palace" keeps its locality in the key and is never reused across tours by name),
  `Day N`, same-country sibling evidence labelled "other trips", and a SCOPE line (which days and
  places the piece covers). S234: sibling lines only when the card has <3 facts (≤2, none naming
  another place); a Route's moments are in day order, skip Segments ranking excluded, carry no
  siblings, merge same-place moments into one block, and a moment with <3 facts is tagged thin. Facts Entries are filtered by `facts.country`; S233 removed the
  per-fact Jev call (no Jev on the T axis). S234 round 2 (`acp_shared.seed_canon`): a Route seed
  keeps one value per fact across its cards (same measure, different number → a verified card wins,
  then the place's own card, then the place with more moments, then the first; numberless
  contradictions are kept), an overnight moment loses its card when its region matches no other
  moment of that day (the card guessed the famous region), and within a day moments run
  overnight-last, then in A3 extraction order. The SEO keyword's product words (retreat, cruise,
  trek … — `acp_content_writing.seo_grounding`) must be in the seed, else the next candidate or the
  keyword cut to its place words; a meta's product nouns must be in the seed or the body (SEO fit
  and F4).
- **T10 — Quality Gates**: inline within T9 (not a separate request) for the automatic Gate run.
  No Jev on T10: the AA-701 shadow questions were turned off in S234 (no signal) and their
  observer removed; migration 194's `t10_gates` question rows stay at `mode=off` as history.
  `/portal/t10-review` is now **My Content** (AA-614): the tenant's pieces with no gate/held
  details, displayed text is copy-locked (select/copy/right-click blocked, with a "Use Export"
  hint; the edit textarea stays editable). AA-669 migrated it to the UI-kit DataTable
  (columns/filters by channel·status·goal/sort/saved views/CSV) with a right-side drawer for the
  piece (content + context + flags + inline edit + exports), and bulk actions (export selected,
  publish selected eligible pieces) via react-query.
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
| 4 | Research/DFS (Search Demand) | **Admin-owned data, admin-triggered only** (AA-646: `POST /admin/segment-research/run`; no tenant action buys DataForSEO — T2 rewrite only reads the cached `seo_context` row, KAN-90 follow-up 29/09/2026) | Once per `(keyword, market)` / `(place, market)` — NOT per tenant | **Yes — the clearest, most consequential cross-tenant mechanism in the pipeline.** `acp_contract.search_demand` (migration 130:20-27) and `acp_contract.segment_research_log` (migration 130:40-38) have **no `tenant_id` column at all**. `_cached_volume()`/`_store_volume()` (`services/acp_contract/segment_research.py:229-252`) key purely on `(keyword, market)`; `_stale_markets()` (`:440-450`) and `run_segment_research()`'s own skip check (`:476-484`, `if not stale: return`) key on `(canonical_place, market)`. Confirmed real: if tenant A already researched "Kyoto"/`japan` inside `FRESH_FOR` (182 days, `:69`), tenant B's entire LLM ReAct loop for the same place+market is skipped — not just the DataForSEO call, the Bedrock cost too. Deliberate (migration 130:13-19: "a keyword's search volume in a market is a fact about the outside world, not tenant content"), not an oversight. |
| 5 | Score (Atom Ranking) | **Admin (platform)** | One pass per buyer market (6 markets) over the whole Segment pool | **Yes — shared rows (AA-545).** `acp_contract.atom_ranking` primary key `(market, tour_id, segment_id)`, no `tenant_id`. A tenant with several markets merges the matching rows at read time in the Slate. 4-axis rank-sum since AA-610. |
| 6 | Route / Hub | **Admin (platform)** | Route: rebuilt whole per tour (old rows superseded). Hub: persists across rebuilds | **Yes — shared rows (AA-545).** `acp_contract.route` identity `(tour_id, first_day, last_day)` and `acp_contract.hub`, both without `tenant_id`. |
| 7 | Slate (Subject) | **Tenant** | Per tenant | **No.** `acp_shared.subject.tenant_id UUID NOT NULL` (migration 133:19). The similarly-named but unrelated `acp_contract.route_pick` (the Route-pick snapshot, renamed from `acp_contract.subject` at migration 132 — "a different, unrelated concept," migration 133:8) is also `tenant_id UUID NOT NULL` (migration 131:74, pre-rename) — both fully isolated. |
| 8 | Goal / Angle / Write / Gate / Review / Publish | **Tenant** | Per tenant, per request/Piece | **Mostly no, one deliberate exception.** `acp_shared.angle_gate_request.tenant_id` (migration 113:36), `content_piece.tenant_id` (migration 115:36), `publish_log.tenant_id` (migration 116:27) all `NOT NULL`. Exception: the T10 cannibalization Gate (F10) deliberately reads EVERY other tenant's approved Pieces — `find_similar_pieces(cross_tenant=True, ...)`, `_CROSS_TENANT_QUERY` has no `tenant_id` filter at all (`services/acp_shared/piece_similarity.py:46-56, 67-95`), consumed by `gate_cannibalization()` at the 0.92 cosine-similarity threshold. This is a cross-tenant **read/compare**, not a cache — the embedding itself is never shared: `compute_embedding()` (`services/acp_shared/content_embedding.py`) makes one fresh Bedrock call per Piece, every time, for every tenant, with no caching layer at all. |
| 9 | Facts Entry | **Both** (one table, two scopes) | Platform scope: once, by admin. Tenant scope: per tenant | **Yes for `scope='platform'` rows, by design.** `acp_shared.facts` (migration 145:33-46), `scope` column (`'platform'`\|`'tenant'`), `tenant_id IS NULL iff scope='platform'` (CHECK, migration 145:44-46). RLS (migration 229, AA-817, for the tenant pool role `aa_app_user`; the owner/main pool is exempt): `facts_platform_read` FOR SELECT `USING (scope = 'platform')` — platform rows are readable by every tenant — and `tenant_isolation` FOR ALL `USING`/`WITH CHECK (scope = 'tenant' AND tenant_id = NULLIF(app.tenant_id, '')::uuid)` — a tenant reads and writes only its own rows and can never write, delete or re-scope a platform fact (145's FOR ALL policy allowed that). |

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
